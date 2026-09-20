"""流式上传的回归测试。

补这一组的背景：文件上限从 30MB 提到 300MB 时，原来的
`await file.read()`（整份进内存）会变成真实的内存问题 ——
单次上传常驻 300MB+，并发几个直接 OOM。

所以改成"边收边写盘 + 顺带算哈希"。这个文件守住三件事：

  1. **不超限时不整份进内存**（结构上保证：逐块消费）
  2. **超限立刻停手，不留垃圾**（不能先把 5GB 写完再拒绝）
  3. **任何失败路径都不留临时文件**（磁盘泄漏比报错更难发现）
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.ingestion import storage


async def _chunks(*items: bytes):
    for item in items:
        yield item


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def clean_temp():
    """用例前后都清一遍中转目录，避免互相干扰。"""
    directory = storage.temp_dir()
    for item in directory.glob("*.part"):
        item.unlink(missing_ok=True)
    yield directory
    for item in directory.glob("*.part"):
        item.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
def test_streams_chunks_to_disk(clean_temp) -> None:
    """分块写入的总字节数与内容都要正确。"""
    payload = [b"a" * 1000, b"b" * 2000, b"c" * 500]
    path, size, digest = run(storage.stream_to_temp(_chunks(*payload), max_bytes=10_000))

    try:
        assert path.exists()
        assert size == 3500
        assert path.read_bytes() == b"".join(payload)
        # 哈希必须等于"把完整内容一次算出来"的结果 —— 边写边算不能算错
        import hashlib

        assert digest == hashlib.sha256(b"".join(payload)).hexdigest()
    finally:
        path.unlink(missing_ok=True)


def test_ignores_empty_chunks(clean_temp) -> None:
    """空块不该被计入大小（否则大小会虚高，甚至误触发超限）。"""
    path, size, _ = run(storage.stream_to_temp(_chunks(b"x" * 10, b"", b"y" * 10), max_bytes=1000))
    try:
        assert size == 20
    finally:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 超限
# --------------------------------------------------------------------------- #
def test_rejects_when_over_limit(clean_temp) -> None:
    """超限抛 UploadTooLarge，并带上"已收到多少"，好给用户准确提示。"""
    with pytest.raises(storage.UploadTooLarge) as exc:
        run(storage.stream_to_temp(_chunks(b"x" * 800, b"y" * 800), max_bytes=1000))

    assert exc.value.limit == 1000
    # 第二块写进去时就越线了，所以是 1600 而不是 1000
    assert exc.value.received == 1600


def test_over_limit_leaves_no_temp_file(clean_temp) -> None:
    """**关键不变量**：超限之后中转目录必须是干净的。

    不能"先进后出"式地留个 5GB 的残骸 —— 磁盘泄漏比报错更难发现。
    """
    with pytest.raises(storage.UploadTooLarge):
        run(storage.stream_to_temp(_chunks(b"x" * 5000), max_bytes=100))

    assert list(clean_temp.glob("*.part")) == []


def test_stops_reading_once_over_limit(clean_temp) -> None:
    """越线后必须**停止消费**，而不是把剩下的块也读进来再判断。

    这是"边写边判"与"写完再查"的分水岭：后者会先把 5GB 落盘。
    """
    consumed = 0

    async def counting():
        nonlocal consumed
        for _ in range(50):
            consumed += 1
            yield b"z" * 100

    with pytest.raises(storage.UploadTooLarge):
        run(storage.stream_to_temp(counting(), max_bytes=250))

    # 250 字节上限 / 每块 100 字节 → 第 3 块越线，绝不该跑满 50 块
    assert consumed == 3, f"越线后仍在继续读取（消费了 {consumed} 块）"


# --------------------------------------------------------------------------- #
# 失败清理
# --------------------------------------------------------------------------- #
def test_upstream_error_leaves_no_temp_file(clean_temp) -> None:
    """客户端中途断开（迭代器抛异常）也必须清干净。"""

    async def broken():
        yield b"partial"
        raise ConnectionError("客户端断了")

    with pytest.raises(ConnectionError):
        run(storage.stream_to_temp(broken(), max_bytes=10_000))

    assert list(clean_temp.glob("*.part")) == []


def test_discard_temp_is_idempotent(clean_temp) -> None:
    """丢两次不能报错 —— 调用方在多层 finally 里会重复调。"""
    path, _, _ = run(storage.stream_to_temp(_chunks(b"data"), max_bytes=1000))
    storage.discard_temp(path)
    storage.discard_temp(path)  # 第二次不该抛
    storage.discard_temp(None)  # None 也不该抛
    assert not path.exists()


# --------------------------------------------------------------------------- #
# 归档
# --------------------------------------------------------------------------- #
def test_finalize_moves_into_hash_directory(clean_temp, tmp_path, monkeypatch) -> None:
    """归档要把临时文件搬进 `<hash>/source.<ext>`，而不是复制。"""
    monkeypatch.setattr(storage.settings, "upload_dir", str(tmp_path / "uploads"))

    payload = b"%PDF-1.4 fake"
    temp, size, digest = run(storage.stream_to_temp(_chunks(payload), max_bytes=10_000))

    rel = storage.finalize_source(temp, digest, "讲义.pdf")

    try:
        assert rel == f"{digest}/source.pdf"
        absolute = storage.resolve(rel)
        assert absolute.exists()
        assert absolute.read_bytes() == payload
        # 搬走之后临时文件必须没了（是 rename 不是 copy）
        assert not temp.exists()
        assert size == len(payload)
    finally:
        import shutil

        shutil.rmtree(tmp_path / "uploads", ignore_errors=True)


# --------------------------------------------------------------------------- #
# 陈旧残骸清理
# --------------------------------------------------------------------------- #
def test_cleanup_removes_only_stale_files(clean_temp) -> None:
    """启动清理只该动"够旧"的，正在上传的不该被误删。"""
    import time

    fresh = clean_temp / "fresh.part"
    stale = clean_temp / "stale.part"
    fresh.write_bytes(b"x")
    stale.write_bytes(b"x")
    # 把 stale 的修改时间往前拨 8 小时
    old = time.time() - 8 * 3600
    import os

    os.utime(stale, (old, old))

    removed = storage.cleanup_temp_files(older_than_seconds=6 * 3600)

    assert removed >= 1
    assert fresh.exists(), "正在上传的文件被误删了"
    assert not stale.exists()

def test_cleanup_failure_does_not_mask_original_error(clean_temp, monkeypatch) -> None:
    """**清理失败时，原始错误必须原样抛出来。**

    这里踩过一次真实 bug：`except` 里直接写 `target.unlink()`，
    结果在"删除失败"的环境下（Windows 上杀毒软件占着句柄、
    或宿主注入的删除拦截），**清理动作自己抛的 OSError 把
    `UploadTooLarge` 覆盖掉了** —— 用户看到 500 而不是"文件超限"。

    清理是尽力而为的收尾动作，它失败不该改变主要结论。
    这条测试就是钉住这个不变量。
    """

    def exploding_unlink(self, *args, **kwargs):
        raise OSError("模拟删除失败（杀毒软件占着句柄）")

    monkeypatch.setattr(Path, "unlink", exploding_unlink)

    # 原始错误必须是 UploadTooLarge，而不是被清理动作的 OSError 顶掉
    with pytest.raises(storage.UploadTooLarge):
        run(storage.stream_to_temp(_chunks(b"x" * 5000), max_bytes=100))


def test_discard_temp_swallows_oserror(clean_temp, monkeypatch) -> None:
    """`discard_temp` 遇到删除失败只能吞掉并记日志，绝不能往上抛。"""

    def exploding_unlink(self, *args, **kwargs):
        raise OSError("模拟删除失败")

    path, _, _ = run(storage.stream_to_temp(_chunks(b"data"), max_bytes=1000))
    monkeypatch.setattr(Path, "unlink", exploding_unlink)

    # 不该抛
    storage.discard_temp(path)
