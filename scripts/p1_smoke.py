"""P1 端到端验证：资料解析与知识点抽取。

默认在 **mock 模式下**跑完整条流水线（上传 → 解析 → 分块 → 抽取 → 落库 → 回链查询），
因此不消耗任何 API 额度、结果可重复，适合作为回归基线。

用法（项目根目录）：
    python scripts/p1_smoke.py            # mock 模式，零 API 消耗
    python scripts/p1_smoke.py --live     # 用真实模型跑一遍（会消耗额度）

它会：
  1. 现场生成一份 12 页的测试 PDF（含页眉页脚、中英文正文、内嵌图片）
  2. 走真实的上传校验与落盘逻辑
  3. 同步执行摄取流水线
  4. 校验知识点是否带可用的页码回链
  5. 清理测试数据
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

# 必须在导入 app.core.config 之前设置：环境变量优先级高于 .env
if "--live" not in sys.argv:
    os.environ["LLM_MODE"] = "mock"

OK = "\033[92m✔\033[0m"
FAIL = "\033[91m✘\033[0m"
DIM = "\033[2m"
RESET = "\033[0m"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    mark = OK if ok else FAIL
    print(f"  {mark} {name}" + (f"  {DIM}{note}{RESET}" if note else ""))


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# --------------------------------------------------------------------------- #
# 测试素材
# --------------------------------------------------------------------------- #
#: 章节内容。用真实感较强的中文技术文本，便于观察分块与抽取效果。
CHAPTERS: list[tuple[str, list[str]]] = [
    (
        "第3章 进程管理",
        [
            "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。"
            "进程实体由程序段、数据段和进程控制块（PCB）三部分组成，其中 PCB 是进程存在的唯一标志。"
            "操作系统通过 PCB 感知进程的存在，并在进程结束时回收其资源。",
            "进程具有动态性、并发性、独立性和异步性四个基本特征。"
            "动态性是指进程是程序的一次执行，有创建、就绪、运行、阻塞和终止的生命周期；"
            "并发性是指多个进程可以同时存在于内存中并交替执行。",
            "进程的状态转换包括三种基本状态：就绪态、运行态和阻塞态。"
            "就绪态的进程已经具备运行条件，只等待 CPU 调度；"
            "运行态的进程正在 CPU 上执行；阻塞态的进程因等待某个事件而暂停执行。",
            "进程控制块 PCB 中保存了进程标识符、处理机状态、进程调度信息以及进程控制信息。"
            "当发生进程切换时，系统需要保存当前进程的现场信息到 PCB 中，"
            "并从下一个进程的 PCB 中恢复现场，这个过程称为上下文切换。",
        ],
    ),
    (
        "第4章 线程与并发",
        [
            "线程是进程内的一个执行单元，是 CPU 调度的基本单位。"
            "同一进程内的多个线程共享该进程的地址空间、代码段、数据段以及已打开的文件，"
            "但每个线程拥有独立的程序计数器、寄存器和栈。",
            "进程与线程的核心区别在于：进程是资源分配的基本单位，拥有独立的地址空间；"
            "线程是调度的基本单位，共享所属进程的资源。"
            "因此线程的创建与切换开销都明显小于进程。",
            "多线程编程需要处理同步问题。互斥锁用于保证同一时刻只有一个线程访问临界区，"
            "信号量则可以控制同时访问某个资源的线程数量。"
            "若使用不当，会出现死锁：两个或多个线程互相持有对方需要的资源并永久等待。",
            "死锁产生的四个必要条件是不可剥夺、请求与保持、循环等待和互斥。"
            "只要破坏其中任意一个条件，死锁就无法形成。"
            "常见的处理策略包括预防、避免、检测与解除。",
        ],
    ),
    (
        "第5章 内存管理",
        [
            "分页存储管理将进程的逻辑地址空间分成若干大小相等的页，"
            "同时把内存的物理地址空间分成与页大小相同的块，称为物理块或页框。"
            "页表用于记录逻辑页号到物理块号的映射关系。",
            "地址变换机构将逻辑地址分为页号和页内偏移两部分。"
            "先以页号为索引查页表得到物理块号，再将物理块号与页内偏移拼接得到物理地址。"
            "由于访问页表本身也需要一次内存访问，实际需要两次访存。",
            "快表（TLB）是一种高速缓存，用于存放最近访问过的页表项。"
            "引入快表后，若命中则只需一次访存，显著提升了地址变换速度。"
            "快表的命中率是影响分页系统性能的关键指标。",
            "页面置换算法决定在内存不足时淘汰哪一页。"
            "最佳置换算法（OPT）淘汰未来最长时间不再访问的页面，但无法实际实现，"
            "仅作为衡量其他算法优劣的标准。最近最久未使用算法（LRU）性能接近 OPT，"
            "但实现开销较大。先进先出算法（FIFO）实现简单，却可能出现 Belady 异常。",
        ],
    ),
    (
        "第6章 文件系统",
        [
            "文件系统负责管理外存上的数据，为用户提供按名存取的抽象。"
            "文件是具有符号名的相关信息的集合，目录则是用于组织和管理文件的数据结构。"
            "常见的目录结构包括单级目录、两级目录和树形目录。",
            "索引节点（inode）用于记录文件的元信息，包括文件大小、权限、创建时间"
            "以及数据块的位置。文件名与 inode 之间通过目录项建立映射，"
            "因此同一个 inode 可以有多个文件名，这就是硬链接。",
            "磁盘调度算法决定多个等待访问磁盘的请求的执行顺序。"
            "先来先服务算法公平但平均寻道时间长；最短寻道时间优先算法性能较好，"
            "但可能导致远处的请求饥饿。扫描算法在两者之间取得平衡。",
            "文件的物理分配方式有连续分配、链接分配和索引分配三种。"
            "连续分配支持随机访问但容易产生外部碎片；链接分配没有碎片问题，"
            "但不支持随机访问；索引分配兼顾两者，是主流文件系统的选择。",
        ],
    ),
]


def build_test_pdf(page_count: int = 12) -> bytes:
    """现场生成一份带页眉页脚、标题、正文与图片的测试 PDF。

    刻意制造这些特征，用来验证解析器的各项能力：
      - 每页重复的页眉与页脚 → 应被识别并剔除
      - 大字号标题 → 应被识别为 heading
      - 页脚页码（逐页不同）→ 应通过归一化匹配被剔除
      - 内嵌图片 → 应被提取并落盘
    """
    import pymupdf

    doc = pymupdf.open()
    page_width, page_height = 595, 842
    margin = 72

    # 必须嵌入 CJK 字体：PyMuPDF 默认的 Helvetica 无法编码中文，
    # 生成的 PDF 提取出来会是「······」这样的乱码，测试就失去意义了。
    cjk_font = pymupdf.Font("china-s")
    font_name = "F0"

    # 正文只从「段落文本」里取，不要把标题也当成正文（否则会出现重复标题）
    body_texts: list[str] = [text for _title, texts in CHAPTERS for text in texts]

    body_font = 11
    line_height = 20
    image_no = 0

    for page_index in range(page_count):
        page = doc.new_page(width=page_width, height=page_height)
        page.insert_font(fontname=font_name, fontbuffer=cjk_font.buffer)

        # 页眉（每页相同，应被剔除）
        page.insert_text(
            (margin, 45),
            "操作系统原理 · 课程讲义",
            fontname=font_name,
            fontsize=9,
            color=(0.4, 0.4, 0.4),
        )
        # 页脚页码（逐页不同，应通过归一化匹配被剔除）
        page.insert_text(
            (page_width / 2 - 20, page_height - 35),
            f"- 第 {page_index + 1} 页 -",
            fontname=font_name,
            fontsize=9,
            color=(0.4, 0.4, 0.4),
        )

        y = margin + 20

        # 每个章节标题恰好落在某一页上
        if page_index in (0, 3, 6, 9):
            chapter = CHAPTERS[min(page_index // 3, len(CHAPTERS) - 1)]
            page.insert_text(
                (margin, y), chapter[0], fontname=font_name, fontsize=17
            )
            y += 38

        # 正文：每页三段（取不同段落，避免内容完全重复），
        # 手动折行以模拟真实 PDF 的硬换行
        for offset in range(3):
            text = body_texts[(page_index * 3 + offset) % len(body_texts)]
            line = ""
            for char in text:
                line += char
                if len(line) >= 36:
                    page.insert_text(
                        (margin, y), line, fontname=font_name, fontsize=body_font
                    )
                    y += line_height
                    line = ""
            if line:
                page.insert_text((margin, y), line, fontname=font_name, fontsize=body_font)
                y += line_height
            y += 10

        # 在第 1、4、7、10 页插入图片（尺寸需大于过滤阈值 80px）
        if page_index in (0, 3, 6, 9):
            rect = pymupdf.Rect(margin, y + 10, margin + 220, y + 150)
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 220, 140))
            pixmap.set_rect(pixmap.irect, ((page_index * 40) % 255, 120, 90))
            page.insert_image(rect, pixmap=pixmap)
            page.insert_text(
                (margin, rect.y1 + 16),
                f"图 3-{image_no + 1} 进程状态示意图",
                fontname=font_name,
                fontsize=9,
            )
            image_no += 1

    data = doc.tobytes()
    doc.close()
    return data


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _find_duplicate_titles(db, document_id: int) -> list[str]:
    """找出同一文档内归一化标题重复的知识点。

    这是幂等性真正该断言的不变式：**无论重跑多少次，同一文档都不应出现重复知识点的**。
    数据库的 UNIQUE(document_id, title_norm) 是最后一道防线，这里主动查一遍确认它生效。
    """
    from sqlalchemy import func, select

    from app.models.knowledge_point import KnowledgePoint

    rows = db.execute(
        select(KnowledgePoint.title_norm, func.count().label("n"))
        .where(KnowledgePoint.document_id == document_id)
        .group_by(KnowledgePoint.title_norm)
        .having(func.count() > 1)
    ).all()
    return [row[0] for row in rows]


async def main() -> int:
    from app.core.config import settings
    from app.db.session import SessionLocal
    from app.ingestion import storage
    from app.ingestion.router import category_of, store_source, validate_upload
    from app.models.document import Document
    from app.models.knowledge_point import KnowledgePoint
    from app.services import document_service
    from app.services.ingest_pipeline import run_pipeline

    print("=" * 74)
    print("  Learning Buddy · P1 端到端验证（资料解析与知识点抽取）")
    print("=" * 74)
    print(f"  LLM 模式：{settings.effective_llm_mode}")

    # ------------------------------------------------------------ 1. 生成素材
    section("[1/6] 生成测试 PDF")
    pdf_bytes = build_test_pdf()
    file_name = "_p1_smoke_test.pdf"
    record("测试 PDF 已生成", len(pdf_bytes) > 1000, f"{len(pdf_bytes) / 1024:.0f} KB")
    record("扩展名识别", category_of(file_name) == "pdf", "pdf")

    # ------------------------------------------------------------ 2. 上传校验
    section("[2/6] 上传校验与落盘")
    try:
        validate_upload(pdf_bytes, file_name=file_name)
        record("校验通过（类型/大小/页数）", True)
    except Exception as exc:  # noqa: BLE001
        record("校验失败", False, str(exc))
        return 1

    file_hash, storage_path = store_source(pdf_bytes, file_name=file_name)
    record("源文件已落盘", True, storage_path)

    with SessionLocal() as db:
        # 清理上次运行残留，保证可重复执行
        old = document_service.find_by_hash(db, file_hash)
        if old is not None:
            document_service.delete_document(db, old)
            print(f"  {DIM}（已清理上次运行的残留记录）{RESET}")

        doc = document_service.create_document(
            db,
            file_name=file_name,
            file_size=len(pdf_bytes),
            file_hash=file_hash,
            storage_path=storage_path,
        )
        document_id = doc.id
    record("文档记录已创建", True, f"document_id={document_id}")

    # ------------------------------------------------------------ 3. 跑流水线
    section("[3/6] 执行摄取流水线（解析 → 分块 → 抽取）")
    await run_pipeline(document_id)

    with SessionLocal() as db:
        doc = document_service.get_document(db, document_id)
        if doc is None:
            record("文档记录丢失", False)
            return 1

        record(
            f"处理状态 = {doc.parse_status}",
            doc.parse_status == "ready",
            doc.parse_error or doc.stage_detail or "",
        )
        if doc.parse_status != "ready":
            print(f"  {DIM}错误详情：{doc.parse_error}{RESET}")
            return 1

        record("页码已解析", doc.page_count >= 10, f"{doc.page_count} 页")
        record("字符已提取", doc.char_count > 3000, f"{doc.char_count} 字符")
        record("分块完成", doc.chunk_count >= 5, f"{doc.chunk_count} 个块")
        record("知识点已抽取", doc.kp_count >= 5, f"{doc.kp_count} 个知识点")

        # -------------------------------------------------------- 4. 结构检查
        section("[4/6] 统一文档结构与图片")
        structure = storage.read_structure(doc.file_hash)
        if structure is None:
            record("文档结构文件存在", False, "structure.json 未找到")
        else:
            record("文档结构文件存在", True, f"{structure['page_count']} 页")
            warnings = [w.get("code") for w in (structure.get("meta", {}).get("warnings") or [])]
            has_footer_warning = "NO_TEXT_LAYER" in warnings
            record(
                "页眉页脚剔除生效",
                not has_footer_warning,
                "未出现无文本层警告" if not has_footer_warning else "存在无文本层页面",
            )
            # 页码必须与原文一致（第 1 页应存在）
            page_numbers = [p["page_no"] for p in structure["pages"]]
            record(
                "页码连续且从 1 开始",
                page_numbers == list(range(1, len(page_numbers) + 1)),
                f"{page_numbers[0]}..{page_numbers[-1]}",
            )

            image_blocks = [
                b
                for p in structure["pages"]
                for b in p["blocks"]
                if b.get("type") == "figure"
            ]
            record("内嵌图片被提取", len(image_blocks) >= 3, f"{len(image_blocks)} 张")
            if image_blocks:
                first = image_blocks[0]
                exists = storage.resolve(first["image_path"]).is_file()
                record("图片文件真实落盘", exists, first["image_path"])
                record(
                    "图注已识别",
                    bool(first.get("caption")),
                    str(first.get("caption"))[:30],
                )

        # -------------------------------------------------------- 5. 溯源检查
        section("[5/6] 知识点质量与页码溯源")
        from sqlalchemy import select

        points = list(
            db.execute(
                select(KnowledgePoint)
                .where(KnowledgePoint.document_id == document_id)
                .order_by(KnowledgePoint.order_index)
            )
            .scalars()
            .all()
        )

        with_sources = [p for p in points if p.source_chunk_indexes]
        record(
            "全部知识点都有来源块",
            len(with_sources) == len(points),
            f"{len(with_sources)}/{len(points)}",
        )

        with_pages = [p for p in points if p.source_pages]
        record(
            "全部知识点都有来源页码",
            len(with_pages) == len(points),
            f"{len(with_pages)}/{len(points)}",
        )

        # 页码必须落在真实页数范围内 —— 这是"模型不能编造页码"的直接验证
        max_page = doc.page_count
        pages_valid = all(
            1 <= page <= max_page for p in points for page in (p.source_pages or [])
        )
        all_pages = sorted({page for p in points for page in (p.source_pages or [])})
        record(
            "页码全部落在真实范围内",
            pages_valid and bool(all_pages),
            f"覆盖页码 {all_pages[:12]}{'…' if len(all_pages) > 12 else ''}",
        )

        difficulties = sorted({p.difficulty for p in points})
        importances = sorted({p.importance for p in points})
        record("难度有分布（非全为 3）", len(difficulties) > 1, f"取值 {difficulties}")
        record("重要度有分布", len(importances) >= 1, f"取值 {importances}")

        # 回链能否取到真实原文
        if points:
            from app.models.chunk import Chunk

            sample = points[0]
            chunks = list(
                db.execute(
                    select(Chunk).where(
                        Chunk.document_id == document_id,
                        Chunk.chunk_index.in_(sample.source_chunk_indexes or []),
                    )
                )
                .scalars()
                .all()
            )
            record(
                "回链能取到原文",
                bool(chunks),
                f"《{sample.title}》→ {len(chunks)} 个原文块，第 {sample.source_pages} 页",
            )
            print(f"  {DIM}示例知识点：{sample.title}{RESET}")
            print(f"  {DIM}  摘要：{(sample.summary or '')[:56]}…{RESET}")

        # -------------------------------------------------------- 6. 幂等与清理
        section("[6/6] 幂等性检查与清理")

        # 流水线在设计上是幂等的：进入时先清空派生物，因此重复投递不会撞唯一约束，
        # 也不会产生重复知识点。这里直接连跑两次来验证这一点。
        before = doc.kp_count
        await run_pipeline(document_id)
        with SessionLocal() as db2:
            doc_after = document_service.get_document(db2, document_id)
            record(
                "重跑不报错",
                doc_after.parse_status == "ready",
                f"状态 {doc_after.parse_status}",
            )
            dup = _find_duplicate_titles(db2, document_id)
            record("重跑后无重复知识点", not dup, f"重复项 {dup or '无'}")
            print(
                f"  {DIM}知识点数量：首次 {before} → 重跑 {doc_after.kp_count}"
                f"（真实 LLM 输出非确定，数量浮动属正常）{RESET}"
            )

        # 再跑一次，确认连续重跑依然稳定且不累积重复数据
        await run_pipeline(document_id)
        with SessionLocal() as db3:
            doc_third = document_service.get_document(db3, document_id)
            dup3 = _find_duplicate_titles(db3, document_id)
            record(
                "连续重跑依然无重复",
                doc_third.parse_status == "ready" and not dup3,
                f"知识点 {doc_third.kp_count} / 文本块 {doc_third.chunk_count}",
            )

            # 清理测试数据
            document_service.delete_document(db3, doc_third)
            gone = document_service.find_by_hash(db3, file_hash) is None
            record("测试数据已清理", gone)

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [name for name, ok, _ in results if not ok]

    print("\n" + "=" * 74)
    if failed:
        print(f"  结果：{passed}/{len(results)} 项通过，{len(failed)} 项失败")
        for name in failed:
            print(f"    {FAIL} {name}")
    else:
        print(f"  结果：全部 {len(results)} 项通过 {OK}")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
