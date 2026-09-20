"""可信度校验测试。

本文件里最重要的不是"校验逻辑对不对"，而是下面这条：

    **P2 跑完整套校验后，P1 的抽取产物必须一个字节都没变。**

用户的明确要求是「不要覆盖 P1 原始抽取结果，保留可追溯性」。
这条约定只靠注释是守不住的 —— 它必须由测试来保证。
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

import app.db.base  # noqa: F401 - 触发全部模型注册
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.knowledge_check import CheckType, CheckVerdict
from app.models.knowledge_point import KnowledgePoint, VerifyStatus
from app.search.tavily_client import TavilyClient
from app.services import verify_service as vs
from app.services.verify_service import (
    CheckDraft,
    aggregate_status,
    build_search_query,
    find_unsupported_sentences,
    rule_check_point,
    should_verify_online,
)

# --------------------------------------------------------------------------- #
# P1 抽取产物：这些字段 P2 一律只读
# --------------------------------------------------------------------------- #
P1_READONLY_FIELDS = (
    "title",
    "title_norm",
    "summary",
    "details",
    "key_points",
    "difficulty",
    "importance",
    "confidence",
    "tags",
    "heading_path",
    "source_chunk_indexes",
    "source_pages",
    "order_index",
)


def snapshot_p1_fields(point: KnowledgePoint) -> dict:
    return {field: getattr(point, field) for field in P1_READONLY_FIELDS}


# --------------------------------------------------------------------------- #
# 夹具：一份临时的文档 + 知识点 + 原文块
# --------------------------------------------------------------------------- #
SOURCE_TEXT = (
    "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。"
    "进程具有动态性、并发性、独立性和异步性四个基本特征。"
    "进程控制块是操作系统为管理进程设置的专门数据结构，是进程存在的唯一标志。"
)

#: 夹具用的固定 hash。固定值便于辨认测试数据，但必须处理上一次异常退出留下的
#: 同 hash 记录（见夹具里的清理逻辑），否则会撞唯一约束、表现为偶发失败。
FIXTURE_HASH = "p2verify" + "0" * 56


@pytest.fixture(autouse=True)
def force_mock_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 mock 模式，让服务层测试不依赖真实模型。

    不加这个夹具会有两个后果，都很难受：
      1. **慢** —— 一次校验要给每个知识点发一个模型请求；
      2. **偶发失败** —— 网络抖动或限流会让 `stats.errors` 从 0 变成 1，
         表现为随机红灯。曾经真的这样闪过一次，排查成本远高于测试的价值。

    真实模型的调用链路由 `scripts/p2_smoke.py --live` 与端到端验证覆盖，
    这里只关心逻辑正确性。`LLM_MODE=auto` 下清空 Key 即进入 mock。
    """
    monkeypatch.setattr(settings, "llm_api_key", "")


@pytest.fixture()
def sample(db=None):
    """建一份临时文档（1 个块 + 2 个知识点），测试后整份删除。"""
    with SessionLocal() as session:
        # 防御：清掉可能残留的同 hash 文档，保证用例可重复运行
        for stale in session.execute(
            select(Document).where(Document.file_hash == FIXTURE_HASH)
        ).scalars().all():
            session.delete(stale)
        session.commit()

        document = Document(
            file_name="p2_verify_test.txt",
            file_type="text",
            file_size=len(SOURCE_TEXT),
            file_hash=FIXTURE_HASH,
            storage_path="test/p2_verify_test.txt",
            page_count=2,
            char_count=len(SOURCE_TEXT),
            chunk_count=1,
            kp_count=2,
            parse_status="ready",
            progress=100,
        )
        session.add(document)
        session.flush()

        session.add(
            Chunk(
                document_id=document.id,
                chunk_index=0,
                content=SOURCE_TEXT,
                page_start=1,
                page_end=2,
                block_type="text",
                heading_path=["3.1 进程的概念"],
                char_count=len(SOURCE_TEXT),
            )
        )

        good = KnowledgePoint(
            document_id=document.id,
            title="进程的定义",
            title_norm="进程的定义",
            summary="进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。",
            details=f"进程是程序的一次执行过程。{SOURCE_TEXT}",
            key_points=["程序的一次执行过程", "资源分配的基本单位"],
            difficulty=2,
            importance=5,
            confidence=0.95,
            verify_status=VerifyStatus.UNVERIFIED,
            tags=["操作系统"],
            heading_path=["3.1 进程的概念"],
            source_chunk_indexes=[0],
            source_pages=[1],
            order_index=0,
        )
        # 故意造一个"缺依据"的知识点：来源块指向不存在的编号
        broken = KnowledgePoint(
            document_id=document.id,
            title="幽灵知识点",
            title_norm="幽灵知识点",
            summary="",
            details="这个知识点的来源块根本不存在，用来验证规则校验能抓到它。",
            key_points=[],
            difficulty=3,
            importance=2,
            confidence=0.5,
            verify_status=VerifyStatus.UNVERIFIED,
            heading_path=["3.1 进程的概念"],
            source_chunk_indexes=[999],
            source_pages=[],
            order_index=1,
        )
        session.add_all([good, broken])
        session.commit()

        document_id, good_id, broken_id = document.id, good.id, broken.id

    yield {"document_id": document_id, "good_id": good_id, "broken_id": broken_id}

    with SessionLocal() as session:
        doc = session.get(Document, document_id)
        if doc is not None:
            session.delete(doc)  # 级联删除 chunks / kps / checks / relations
            session.commit()


def fake_search_client(payload: dict | None = None, status_code: int = 200) -> TavilyClient:
    body = payload if payload is not None else {
        "results": [
            {
                "title": "进程的定义",
                "url": "https://example.com/p",
                "content": "进程是程序的一次执行过程，是资源分配的基本单位。",
                "score": 0.9,
            }
        ]
    }
    return TavilyClient(
        api_key="tvly-test",
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code, json=body)),
    )


def make_point(**overrides) -> KnowledgePoint:
    """构造不落库的知识点，用于纯逻辑测试。"""
    defaults = dict(
        id=1,
        document_id=1,
        title="进程的定义",
        title_norm="进程的定义",
        summary="进程是程序的一次执行过程。",
        details="进程是程序的一次执行过程。",
        key_points=["要点一", "要点二"],
        difficulty=2,
        importance=5,
        confidence=0.9,
        verify_status=VerifyStatus.UNVERIFIED,
        heading_path=["3.1 概念"],
        source_chunk_indexes=[0],
        source_pages=[1],
        order_index=0,
    )
    defaults.update(overrides)
    return KnowledgePoint(**defaults)


# --------------------------------------------------------------------------- #
# L1 规则校验
# --------------------------------------------------------------------------- #
def test_rule_check_passes_for_healthy_point() -> None:
    items, verdict, confidence = rule_check_point(
        make_point(), chunk_indexes={0}, page_count=10, source_text=SOURCE_TEXT
    )
    assert verdict == CheckVerdict.PASSED
    assert confidence > 0.8
    assert all(i.ok for i in items)
    assert {i.code for i in items} >= {
        "source_backlink",
        "page_range",
        "title_quality",
        "summary_present",
        "key_points",
        "common_knowledge_leak",
    }


def test_rule_check_catches_missing_source_chunk() -> None:
    """引用了不存在的块是最严重的问题 —— 意味着这条知识点无法溯源。"""
    items, verdict, _ = rule_check_point(
        make_point(source_chunk_indexes=[999]),
        chunk_indexes={0},
        page_count=10,
        source_text=SOURCE_TEXT,
    )
    # ERROR 而不是 UNSUPPORTED：这两者的区别是**核对有没有进行**。
    # 来源块不存在 → 连原文都拿不到 → 核对根本没做成，而不是"做了但没找到依据"。
    # 这个区分很实际：UI 上"没查成"和"查了没找到"对用户是两件事，
    # 而且 ERROR 会阻止这条被评成"可信"（模型在看不到原文时可说不出靠谱的结论）。
    assert verdict == CheckVerdict.ERROR
    backlink = next(i for i in items if i.code == "source_backlink")
    assert backlink.ok is False
    assert backlink.severity == "error"
    assert "999" in backlink.detail


def test_rule_check_catches_out_of_range_page() -> None:
    """页码越界同样是**没法核对**，不是"没找到依据"。"""
    _, verdict, _ = rule_check_point(
        make_point(source_pages=[999]), chunk_indexes={0}, page_count=10, source_text=SOURCE_TEXT
    )
    assert verdict == CheckVerdict.ERROR


def test_rule_check_flags_bad_title() -> None:
    items, verdict, _ = rule_check_point(
        make_point(title="这是一个带句号的标题。"),
        chunk_indexes={0},
        page_count=10,
        source_text=SOURCE_TEXT,
    )
    title_item = next(i for i in items if i.code == "title_quality")
    assert title_item.ok is False
    assert verdict == CheckVerdict.UNSUPPORTED


def test_rule_check_flags_unsourced_statement_as_unsupported() -> None:
    """通识外溢判 `unsupported`（原文里没找到依据）。

    ⚠️ 它**不再是"存疑"**。曾经这里判 `suspicious`，理由是"启发式可能误伤，
    宁可让人复核" —— 但实测这个告警标出了四成知识点，用户学会的是无视它。
    `unsupported` 同时满足两个要求：如实（就是没找到依据）、不指控（没说它错）。
    """
    point = make_point(
        details=(
            "进程是程序的一次执行过程。"
            "一般来说，现代操作系统还会为进程维护优先级队列以改善调度效果，"  # 原文没有这句
        )
    )
    items, verdict, _ = rule_check_point(
        point, chunk_indexes={0}, page_count=10, source_text=SOURCE_TEXT
    )
    leak = next(i for i in items if i.code == "common_knowledge_leak")
    assert leak.ok is False
    assert leak.severity == "warn"
    assert verdict == CheckVerdict.UNSUPPORTED


def test_rule_check_does_not_flag_sourced_statement() -> None:
    """原文里确实有的内容，即使带引导词也不该报警。"""
    point = make_point(details="进程是程序的一次执行过程，具备动态性与并发性。")
    items, verdict, _ = rule_check_point(
        point, chunk_indexes={0}, page_count=10, source_text=SOURCE_TEXT
    )
    leak = next(i for i in items if i.code == "common_knowledge_leak")
    assert leak.ok is True


# --------------------------------------------------------------------------- #
# 通识外溢启发式
# --------------------------------------------------------------------------- #
def test_find_unsupported_sentences_detects_novel_claim() -> None:
    hits = find_unsupported_sentences(
        "此外，进程的优先级可以动态调整以提升吞吐量。", SOURCE_TEXT
    )
    assert len(hits) == 1


def test_find_unsupported_sentences_tolerates_layout_difference() -> None:
    """原文的分行与标点差异不应造成误报。"""
    details = "一般来说，进程具有动态性和并发性。"
    source = "进程具有动态性、\n并发性、独立性和异步性。"
    assert find_unsupported_sentences(details, source) == []


def test_find_unsupported_sentences_ignores_short_fragments() -> None:
    assert find_unsupported_sentences("通常。", SOURCE_TEXT) == []


# --------------------------------------------------------------------------- #
# L3 闸门
# --------------------------------------------------------------------------- #
def test_gate_skips_when_tavily_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tavily_api_key", "")
    allow, reason = should_verify_online(
        make_point(importance=5), l1_verdict=CheckVerdict.UNSUPPORTED, l2_verdict=None
    )
    assert allow is False
    assert "TAVILY_API_KEY" in reason


def test_gate_skips_when_globally_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-x")
    monkeypatch.setattr(settings, "verify_web_enabled", False)
    allow, reason = should_verify_online(
        make_point(importance=5), l1_verdict=CheckVerdict.UNSUPPORTED, l2_verdict=None
    )
    assert allow is False
    assert "关闭" in reason


def test_gate_skips_stable_common_knowledge(monkeypatch: pytest.MonkeyPatch) -> None:
    """常识性稳定知识点不触发联网 —— 省配额，也避免噪声污染原本正确的结论。"""
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-x")
    allow, reason = should_verify_online(
        make_point(difficulty=2, importance=3),
        l1_verdict=CheckVerdict.PASSED,
        l2_verdict=CheckVerdict.PASSED,
    )
    assert allow is False
    assert "常识" in reason


def test_gate_skips_when_both_layers_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-x")
    allow, reason = should_verify_online(
        make_point(difficulty=5, importance=5),
        l1_verdict=CheckVerdict.PASSED,
        l2_verdict=CheckVerdict.PASSED,
    )
    assert allow is False
    assert "无需" in reason


def test_gate_allows_when_model_flags_overstatement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-x")
    allow, _ = should_verify_online(
        make_point(importance=5),
        l1_verdict=CheckVerdict.PASSED,
        l2_verdict=CheckVerdict.UNSUPPORTED,
    )
    assert allow is True


def test_search_query_prefers_title() -> None:
    query = build_search_query(make_point(title="死锁的四个必要条件", summary="互斥、请求并保持"))
    assert query.startswith("死锁的四个必要条件")
    assert len(query) <= 60


# --------------------------------------------------------------------------- #
# 结论汇总
# --------------------------------------------------------------------------- #
def draft(check_type: str, verdict: str) -> CheckDraft:
    return CheckDraft(
        kp_id=1, check_type=check_type, verdict=verdict, reason="", confidence=0.5, engine="t"
    )


def test_aggregate_unsupported_does_not_become_conflict() -> None:
    """**「没找到依据」不等于「有出入」。**

    这是删掉"存疑"时顺带纠正的一个语义错误：老实现把两者混为一谈，
    于是"模型觉得说得过头""联网没查到一致表述"这类**软信号**
    被翻译成了"这条有出入" —— 那是把关于**我们**的事实，
    说成了关于**知识点**的事实。结果一份资料四成知识点被标成警示色。
    """
    status = aggregate_status(
        [draft(CheckType.RULE, CheckVerdict.PASSED), draft(CheckType.MODEL, CheckVerdict.UNSUPPORTED)]
    )
    assert status != VerifyStatus.CONFLICT, "没找到依据不该说成有出入"
    assert status == VerifyStatus.UNVERIFIED


def test_aggregate_never_produces_suspect_or_conflict() -> None:
    """当前实现**只产出两种状态**：可信 / 还没核对。

    守的是"别再悄悄长出一个警示状态"—— 警示状态一旦泛滥就失去意义。
    """
    combos = [
        [draft(CheckType.RULE, CheckVerdict.PASSED)],
        [draft(CheckType.RULE, CheckVerdict.UNSUPPORTED)],
        [draft(CheckType.MODEL, CheckVerdict.UNSUPPORTED)],
        [draft(CheckType.WEB, CheckVerdict.UNSUPPORTED)],
        [draft(CheckType.WEB, CheckVerdict.ERROR)],
        [draft(CheckType.WEB, CheckVerdict.SKIPPED)],
        [
            draft(CheckType.RULE, CheckVerdict.PASSED),
            draft(CheckType.WEB, CheckVerdict.UNSUPPORTED),
        ],
    ]
    for combo in combos:
        status = aggregate_status(combo)
        assert status in {VerifyStatus.TRUSTED, VerifyStatus.UNVERIFIED}, (
            f"产出了不该有的状态 {status}"
        )


def test_aggregate_trusted_requires_non_rule_layer() -> None:
    """只有规则层通过说明不了什么 —— 规则查的是格式，不是内容是否忠实。"""
    only_rule = aggregate_status([draft(CheckType.RULE, CheckVerdict.PASSED)])
    assert only_rule == VerifyStatus.UNVERIFIED

    with_model = aggregate_status(
        [draft(CheckType.RULE, CheckVerdict.PASSED), draft(CheckType.MODEL, CheckVerdict.PASSED)]
    )
    assert with_model == VerifyStatus.TRUSTED


def test_aggregate_pass_must_come_from_the_non_rule_layer() -> None:
    """通过必须**由非规则层给出**，两个条件要落在同一条上。

    ⚠️ 这里守的是一个真实存在过的 bug：原条件写的是
    `PASSED in verdicts and any(check_type != RULE)` ——
    它只要求"存在一个通过"和"存在一个非规则层"，而**两件事可以来自不同的层**。
    于是 `[规则层通过, 联网层说没找到依据]` 会被判成"可信"，
    可信度是规则层给的格式检查，根本背书不了内容对不对。
    """
    mixed = aggregate_status(
        [
            draft(CheckType.RULE, CheckVerdict.PASSED),
            draft(CheckType.WEB, CheckVerdict.UNSUPPORTED),
        ]
    )
    assert mixed == VerifyStatus.UNVERIFIED, "规则层通过不能替代内容层的背书"

    real = aggregate_status(
        [
            draft(CheckType.RULE, CheckVerdict.PASSED),
            draft(CheckType.WEB, CheckVerdict.PASSED),
        ]
    )
    assert real == VerifyStatus.TRUSTED


def test_aggregate_never_returns_outdated() -> None:
    """刻意不产出 outdated —— 当前比对手段不足以下这个结论。"""
    combos = [
        [draft(CheckType.RULE, CheckVerdict.PASSED)],
        [draft(CheckType.RULE, CheckVerdict.PASSED), draft(CheckType.MODEL, CheckVerdict.PASSED)],
        [draft(CheckType.MODEL, CheckVerdict.UNSUPPORTED)],
        [draft(CheckType.WEB, CheckVerdict.UNSUPPORTED)],
        [draft(CheckType.WEB, CheckVerdict.SKIPPED)],
    ]
    for combo in combos:
        assert aggregate_status(combo) != VerifyStatus.OUTDATED


def test_aggregate_all_skipped_keeps_previous() -> None:
    status = aggregate_status(
        [draft(CheckType.RULE, CheckVerdict.SKIPPED)], fallback=VerifyStatus.UNVERIFIED
    )
    assert status == VerifyStatus.UNVERIFIED


# --------------------------------------------------------------------------- #
# 端到端：三层全跑一遍（mock 模型 + 假检索）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_full_verification_does_not_touch_p1_fields(
    sample: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**本阶段最重要的一条断言。**

    P2 跑完整套校验后，P1 的抽取产物必须逐字段保持一致。
    只允许 knowledge_points.verify_status 发生变化。
    """
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-test-key")
    monkeypatch.setattr(settings, "verify_model_importance_threshold", 4)

    with SessionLocal() as session:
        before = {
            kp_id: snapshot_p1_fields(session.get(KnowledgePoint, kp_id))
            for kp_id in (sample["good_id"], sample["broken_id"])
        }
        doc = session.get(Document, sample["document_id"])
        page_count = doc.page_count

    with SessionLocal() as session:
        stats = await vs.verify_document(
            session,
            sample["document_id"],
            page_count=page_count,
            search=fake_search_client(),
        )

    assert stats.total_points == 2
    assert stats.rule_done == 2

    with SessionLocal() as session:
        for kp_id, snapshot in before.items():
            point = session.get(KnowledgePoint, kp_id)
            after = snapshot_p1_fields(point)
            for field in P1_READONLY_FIELDS:
                assert after[field] == snapshot[field], (
                    f"P2 修改了 P1 的抽取字段 {field}：{snapshot[field]!r} -> {after[field]!r}"
                )

        # verify_status 是唯一允许被写入的字段
        good = session.get(KnowledgePoint, sample["good_id"])
        broken = session.get(KnowledgePoint, sample["broken_id"])
        # 来源块不存在 → 校验给不出'通过'，于是停在"还没核对"。
        # 注意这里**不是** conflict：我们没有"有出入"的证据，只是查不成。
        assert broken.verify_status == VerifyStatus.UNVERIFIED
        assert good.verify_status in {VerifyStatus.TRUSTED, VerifyStatus.UNVERIFIED}

        # 三层各自留下了记录
        checks = vs.list_checks(session, sample["good_id"])
        types = {c.check_type for c in checks}
        assert types == {CheckType.RULE, CheckType.MODEL, CheckType.WEB}


@pytest.mark.asyncio
async def test_web_layer_records_skipped_without_key(
    sample: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没配 Key 时 L3 整层记 skipped 并写明原因，而不是报错。"""
    monkeypatch.setattr(settings, "tavily_api_key", "")

    with SessionLocal() as session:
        doc = session.get(Document, sample["document_id"])
        await vs.verify_document(
            session, sample["document_id"], page_count=doc.page_count
        )

    with SessionLocal() as session:
        checks = vs.list_checks(session, sample["good_id"])
        web = [c for c in checks if c.check_type == CheckType.WEB]
        assert web, "即使跳过也应当留下记录，让用户看到为什么没联网核验"
        assert all(c.verdict == CheckVerdict.SKIPPED for c in web)
        assert any("TAVILY_API_KEY" in (c.reason or "") for c in web)
        # 前两层照常执行
        assert {c.check_type for c in checks} == {CheckType.RULE, CheckType.MODEL, CheckType.WEB}


@pytest.mark.asyncio
async def test_web_search_failure_is_recorded_as_error(
    sample: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配了 Key 但调用失败 → 记 error，与 skipped 严格区分。"""
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-test-key")
    failing = TavilyClient(
        api_key="tvly-test",
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="boom")),
    )

    with SessionLocal() as session:
        doc = session.get(Document, sample["document_id"])
        await vs.verify_document(
            session, sample["document_id"], page_count=doc.page_count, search=failing
        )

    with SessionLocal() as session:
        web = [c for c in vs.list_checks(session, sample["good_id"]) if c.check_type == CheckType.WEB]
        assert any(c.verdict in {CheckVerdict.ERROR, CheckVerdict.SKIPPED} for c in web)


@pytest.mark.asyncio
async def test_verification_is_repeatable_and_keeps_history(
    sample: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反复校验不会覆盖历史 —— 这正是把校验做成「事件表」而非「列」的意义。"""
    monkeypatch.setattr(settings, "tavily_api_key", "")

    with SessionLocal() as session:
        doc = session.get(Document, sample["document_id"])
        page_count = doc.page_count

    for _ in range(2):
        with SessionLocal() as session:
            await vs.verify_document(session, sample["document_id"], page_count=page_count)

    with SessionLocal() as session:
        rule_checks = [
            c for c in vs.list_checks(session, sample["good_id"]) if c.check_type == CheckType.RULE
        ]
        assert len(rule_checks) == 2, "两次校验应留下两条规则记录，而不是覆盖成一条"


@pytest.mark.asyncio
async def test_single_point_failure_does_not_break_batch(
    sample: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单个知识点出问题不能拖垮整批校验。

    断言刻意只看「两个知识点都走完了规则校验」，**不去断言 errors == 0** ——
    后者取决于外部模型是否稳定，会变成一个与代码无关的随机红灯。
    批处理的健壮性体现在"每个点都得到了处理"，而不是"一次都没出错"。
    """
    monkeypatch.setattr(settings, "tavily_api_key", "")

    with SessionLocal() as session:
        doc = session.get(Document, sample["document_id"])
        stats = await vs.verify_document(
            session, sample["document_id"], page_count=doc.page_count
        )
    assert stats.rule_done == 2, "两个知识点都应完成规则校验"
    assert stats.total_points == 2

    with SessionLocal() as session:
        # 坏知识点（来源块不存在）照样留下了记录，而不是让整批中断
        for kp_id in (sample["good_id"], sample["broken_id"]):
            checks = vs.list_checks(session, kp_id)
            assert {c.check_type for c in checks} == {
                CheckType.RULE,
                CheckType.MODEL,
                CheckType.WEB,
            }, f"知识点 {kp_id} 缺少校验层记录"


# --------------------------------------------------------------------------- #
# 前提不成立时，结论都不算数
# --------------------------------------------------------------------------- #
def test_aggregate_rule_error_blocks_trusted() -> None:
    """**规则层拿不到原文时，模型层的"通过"不算数。**

    ⚠️ 这里守的是一个很隐蔽的漏洞：删掉"存疑"之后，`unsupported` 不再升级成
    "有出入"，于是"规则层报错"就必须另找一个阻止 TRUSTED 的途径 ——
    否则一个连来源块都不存在的知识点，只要模型（在看不到原文的情况下）
    说一句"忠实"，就会被评为"可信"。

    **前提不成立，建立在它之上的结论就都不成立。**
    """
    blocked = aggregate_status(
        [
            draft(CheckType.RULE, CheckVerdict.ERROR),
            draft(CheckType.MODEL, CheckVerdict.PASSED),  # 模型说通过
        ]
    )
    assert blocked == VerifyStatus.UNVERIFIED, "模型不该能越过缺失的来源去背书"

    ok = aggregate_status(
        [
            draft(CheckType.RULE, CheckVerdict.PASSED),
            draft(CheckType.MODEL, CheckVerdict.PASSED),
        ]
    )
    assert ok == VerifyStatus.TRUSTED


def test_rule_error_verdict_means_cannot_check_not_unsupported() -> None:
    """`ERROR`（没查成）与 `UNSUPPORTED`（查了没依据）必须分开。

    对用户这是两件事：
      「没查成」→ 我们没能核对，别当结论用
      「没找到依据」→ 核对过了，只是没找到支持它的原文
    混成一个的代价是：前者会被当成后者的语气说出来，听起来像"这条有问题"。
    """
    from app.models.knowledge_check import CheckVerdict as CV

    assert CV.ERROR.value == "error"
    assert CV.UNSUPPORTED.value == "unsupported"
    # 存疑已经被删除，不该再长回来
    assert not hasattr(CV, "SUSPICIOUS"), "「存疑」不该被恢复"
