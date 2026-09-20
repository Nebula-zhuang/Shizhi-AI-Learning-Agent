"""文件存储：落盘、hash、目录规划、路径解析与清理。

**路径约定（全项目统一）**：数据库与文档结构中保存的路径一律是
**相对 `UPLOAD_DIR` 的相对路径**，绝对路径由 `resolve()` 在运行时拼接。

    源文件        <hash>/source.pdf
    文档结构      <hash>/structure.json
    内嵌图片      <hash>/images/p1_0.png

这样做的好处：更换存储根目录（本地盘 → 对象存储）时无需迁移数据，
且 `image_path` 字段自身即可解析，不依赖调用方先知道文档 hash。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger
from app.ingestion.base import ParsedDocument

logger = get_logger(__name__)

#: Windows 非法字符 + 控制字符，统一替换为下划线
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

#: 图片扩展名归一化（PyMuPDF 返回 jpeg，项目统一用 jpg）
_IMAGE_EXT_ALIASES = {"jpeg": "jpg", "tif": "png", "tiff": "png"}


def upload_root() -> Path:
    """上传根目录，不存在则创建。"""
    root = Path(settings.upload_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


#: 分块大小。1MB 是吞吐与内存峰值的折中：
#: 再小家数多、syscall 密集；再大单块读的瞬时峰值高。
UPLOAD_CHUNK = 1024 * 1024


class UploadTooLarge(Exception):
    """流式接收过程中超过上限。携带已收到的字节数，用于给出准确提示。"""

    def __init__(self, received: int, limit: int) -> None:
        self.received = received
        self.limit = limit
        super().__init__(f"已接收 {received} 字节，超过上限 {limit} 字节")


def temp_dir() -> Path:
    """上传中转目录。

    刻意放在 `upload_root()` **里面**：这样落盘与最终归位在同一个文件系统上，
    `os.replace` 是一次原子改名（瞬间完成），而不是跨盘复制 300MB。
    """
    directory = upload_root() / ".tmp"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


async def stream_to_temp(chunks: AsyncIterator[bytes], *, max_bytes: int) -> tuple[Path, int, str]:
    """把上传流按块写到临时文件，**边写边算 sha256**，超限立刻中断。

    ## 为什么必须流式

    原先的做法是 `await file.read()` 一次性读进内存。30MB 时代没问题，
    300MB 就变成：单次上传常驻 300MB+（读进来一份、算哈希一份、写盘时一份），
    并发几个直接 OOM。分块落盘把内存占用固定在 `UPLOAD_CHUNK` 量级。

    ## 为什么超限要"边写边判"而不是写完再查

    写完再查意味着有人传 5GB 时，我们**先把 5GB 写到磁盘再告诉他不行** ——
    既浪费磁盘，也浪费他上传的时间。这里是每块累加后立刻判断，
    一旦越线就停手并删掉临时文件。

    ## 返回

    `(临时文件路径, 字节数, sha256)` —— 哈希顺手算出来，
    调用方不必为了去重再读一遍文件（那又是一次 300MB 的 IO）。
    """
    target = temp_dir() / f"{os.urandom(12).hex()}.part"
    digest = hashlib.sha256()
    received = 0

    try:
        with target.open("wb") as sink:
            async for chunk in chunks:
                if not chunk:
                    continue
                received += len(chunk)
                if received > max_bytes:
                    raise UploadTooLarge(received, max_bytes)
                digest.update(chunk)
                sink.write(chunk)
    except BaseException:
        # 任何失败（超限、客户端断开、磁盘满）都不留垃圾。
        #
        # ⚠️ **必须走 `discard_temp` 而不是直接 `unlink`。**
        # 这里踩过一次：原来写的是 `target.unlink(missing_ok=True)`，
        # 结果在删除失败的环境下（Windows 上杀毒软件占着句柄、
        # 或者宿主注入的删除拦截）**清理动作自己抛异常，
        # 把原始的 `UploadTooLarge` 覆盖掉** —— 用户看到的是 500 而不是
        # "文件超限"那句能看懂的话。
        # 清理是"尽力而为"的收尾动作，它失败不该改变主要结论。
        discard_temp(target)
        raise

    return target, received, digest.hexdigest()


def finalize_source(temp_path: Path, file_hash: str, file_name: str) -> str:
    """把临时文件移进它该在的目录，返回相对路径。

    用 `os.replace` 而不是"读出来再写过去"：同一文件系统内它是原子改名，
    300MB 也是瞬间完成，而且**中途断电不会留下半个文件**。
    """
    directory = document_dir(file_hash)
    directory.mkdir(parents=True, exist_ok=True)

    suffix = extension_of(file_name) or ".bin"
    destination = directory / f"source{suffix}"
    os.replace(temp_path, destination)

    rel = f"{file_hash}/{destination.name}"
    logger.info("源文件已归档：%s（%d 字节）", rel, destination.stat().st_size)
    return rel


def discard_temp(temp_path: Path | None) -> None:
    """丢弃临时文件。任何中途失败路径都必须调它。"""
    if temp_path is None:
        return
    try:
        temp_path.unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001
        # 清不掉也不能让请求失败 —— 只是留个垃圾文件，`cleanup_temp_files` 会兜住
        logger.warning("临时文件清理失败：%s（%s）", temp_path, exc)


def cleanup_temp_files(*, older_than_seconds: int = 6 * 3600) -> int:
    """清理中转目录里的陈旧文件。

    进程被杀（Ctrl+C、崩溃、断电）时 `discard_temp` 不会执行，
    磁盘上会留下 `.part` 残骸。启动时扫一遍，顺手控制磁盘占用。
    """
    directory = temp_dir()
    now = time.time()
    removed = 0
    for item in directory.glob("*.part"):
        try:
            if now - item.stat().st_mtime < older_than_seconds:
                continue
            item.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        logger.info("已清理 %d 个遗留的上传临时文件", removed)
    return removed


def compute_hash(data: bytes) -> str:
    """文件内容的 sha256，作为去重键与存储目录名。"""
    return hashlib.sha256(data).hexdigest()


def sanitize_filename(name: str, *, max_len: int = 180) -> str:
    """清洗文件名：去掉路径部分与非法字符，防止目录穿越与非法路径。"""
    base = Path(name or "").name
    base = _UNSAFE_CHARS.sub("_", base).strip().strip(".")
    if not base:
        base = "untitled"
    return base[:max_len]


def extension_of(file_name: str) -> str:
    """取小写扩展名（含点）。"""
    return Path(file_name or "").suffix.lower()


def document_dir(file_hash: str) -> Path:
    """某文档的存储目录。"""
    return upload_root() / file_hash


def resolve(rel_path: str) -> Path:
    """把相对 UPLOAD_DIR 的路径解析为绝对路径，并校验不越界。"""
    root = upload_root().resolve()
    target = (root / rel_path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"路径越界，拒绝访问：{rel_path}")
    return target


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def save_source(file_hash: str, file_name: str, data: bytes) -> str:
    """保存原始文件，返回相对路径。"""
    directory = document_dir(file_hash)
    directory.mkdir(parents=True, exist_ok=True)

    suffix = extension_of(file_name) or ".bin"
    target = directory / f"source{suffix}"
    target.write_bytes(data)

    rel = f"{file_hash}/{target.name}"
    logger.info("源文件已保存：%s（%d 字节）", rel, len(data))
    return rel


def normalize_image_ext(ext: str) -> str:
    """图片扩展名归一化。"""
    clean = (ext or "png").lower().lstrip(".")
    return _IMAGE_EXT_ALIASES.get(clean, clean)


def save_image(file_hash: str, page_no: int, index: int, ext: str, data: bytes) -> str:
    """保存一张内嵌图片，返回相对 UPLOAD_DIR 的路径。

    命名规则：images/p{页码}_{页内序号}.{ext} —— 从文件名就能看出它在原文哪一页。
    """
    directory = document_dir(file_hash) / "images"
    directory.mkdir(parents=True, exist_ok=True)

    name = f"p{page_no}_{index}.{normalize_image_ext(ext)}"
    (directory / name).write_bytes(data)
    return f"{file_hash}/images/{name}"


def write_structure(file_hash: str, parsed: ParsedDocument) -> str:
    """把统一文档结构落盘为 JSON，返回相对路径。

    单独落盘而不是塞进数据库的原因：这是一个可能几百 KB 的嵌套结构，
    前端预览与 P3 引用回链都只需要「按需读取」，不适合放进行级存储。
    """
    directory = document_dir(file_hash)
    directory.mkdir(parents=True, exist_ok=True)

    target = directory / "structure.json"
    target.write_text(
        parsed.model_dump_json(indent=2, exclude_none=False),
        encoding="utf-8",
    )
    return f"{file_hash}/structure.json"


def read_structure(file_hash: str) -> dict | None:
    """读取统一文档结构。不存在时返回 None。"""
    target = document_dir(file_hash) / "structure.json"
    if not target.is_file():
        return None
    import json

    return json.loads(target.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 清理
# --------------------------------------------------------------------------- #
def delete_document_files(file_hash: str) -> bool:
    """删除某文档的全部落盘文件（源文件 + 结构 + 图片）。"""
    directory = document_dir(file_hash)
    if not directory.exists():
        return False
    try:
        shutil.rmtree(directory)
        logger.info("已删除文档文件：%s", file_hash)
        return True
    except OSError as exc:
        logger.warning("删除文档文件失败 %s：%s", file_hash, exc)
        return False


def disk_usage(file_hash: str) -> int:
    """目录占用字节数，用于展示。"""
    directory = document_dir(file_hash)
    if not directory.exists():
        return 0
    return sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
