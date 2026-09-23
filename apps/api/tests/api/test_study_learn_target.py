"""「想学 X」→ 已有知识点（Phase 5C-2）。

## 这一组守什么

把自由学习里识别出的**主题**解析成一个**已有知识点**，好让前端带着
`kpId` 去开一轮真实教学。三个不变量：

| 不变量 | 为什么必须守 |
|---|---|
| **匹配不到就说匹配不到**（`kp_id` 为 None） | 下一步会拿这个 id 去开教学；编一个出来，用户会在**一门完全没想学的课**里被问第一个问题 |
| **只在"自己的资料"里找** | 越权匹配紧接着就是一次真实教学 —— 等于让助教把别人资料里的内容讲出来 |
| **模糊匹配不许误跳** | 「进程」不能跳到「进程与线程」—— 这正是本仓库在 P1 就写下的那条结论 |

## 匹配是三级，逐级放宽，每级都必须唯一
`exact` → `normalized`（复用入库同款 `normalize_title`）→ `contained`（带长度比护栏）
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import SessionLocal
from app.main import app
from app.models.document import Document, ParseStatus
from app.models.knowledge_point import KnowledgePoint
from app.services import document_service, knowledge_service

client = TestClient(app)
PASSWORD = "Shizhi#2026"


# --------------------------------------------------------------------------- #
# 助手
# --------------------------------------------------------------------------- #
def _register(username: str) -> str:
    res = client.post("/api/auth/register", json={"username": username, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["user"]["learner_id"]


def _seed(learner_id: str, titles: list[str]) -> list[int]:
    """给这位学习者造一份 READY 的资料，并在下面挂若干知识点。返回 kp_id 列表。"""
    with SessionLocal() as db:
        doc = document_service.create_document(
            db,
            file_name=f"讲义-{uuid4().hex[:6]}.pdf",
            file_size=123,
            file_hash=f"hash-{uuid4().hex}",
            storage_path="/tmp/does-not-matter",
            owner_learner_id=learner_id,
        )
        # ⚠️ `create_document` 默认是 PENDING；知识点是解析完之后才有的，
        # 匹配也只认"可用资料"（与 `learner_document_ids` 同一口径）。
        doc.parse_status = ParseStatus.READY
        ids: list[int] = []
        for title in titles:
            point = KnowledgePoint(
                document_id=doc.id,
                title=title,
                title_norm=knowledge_service.normalize_title(title),
                summary="测试用",
            )
            db.add(point)
            db.flush()
            ids.append(int(point.id))
        db.commit()
        return ids


def _resolve(topic: str) -> dict:
    res = client.post("/api/study/learn-target", json={"topic": topic})
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------- #
# 一、精确匹配
# --------------------------------------------------------------------------- #
def test_exact_title_match() -> None:
    learner = _register(f"lt{uuid4().hex[:10]}")
    kp_ids = _seed(learner, ["Java 线程", "Java 内存模型"])

    got = _resolve("Java 线程")

    assert got["matched"] is True
    assert got["reason"] == "exact"
    assert got["kp_id"] == kp_ids[0]
    assert got["title"] == "Java 线程"


def test_exact_match_wins_over_a_later_normalized_one() -> None:
    """同名时也必须给出**那一个**，不是"随便一个"。"""
    learner = _register(f"lt{uuid4().hex[:10]}")
    kp_ids = _seed(learner, ["三次握手"])

    got = _resolve("三次握手")

    assert got["kp_id"] == kp_ids[0]
    assert got["reason"] == "exact"


# --------------------------------------------------------------------------- #
# 二、归一化后匹配（复用入库同款 normalize_title）
# --------------------------------------------------------------------------- #
def test_normalized_match_ignores_punctuation_and_case() -> None:
    learner = _register(f"lt{uuid4().hex[:10]}")
    kp_ids = _seed(learner, ["JVM 内存结构"])

    # 全角标点 + 多余空格 + 大小写，归一化后应当撞上
    got = _resolve("jvm内存结构")

    assert got["matched"] is True
    assert got["kp_id"] == kp_ids[0]
    assert got["reason"] == "normalized"


def test_normalized_match_ignores_stopwords() -> None:
    """`normalize_title` 会去掉「的/与/和/及/或/之」—— 与入库时同一套规矩。"""
    learner = _register(f"lt{uuid4().hex[:10]}")
    kp_ids = _seed(learner, ["进程与线程的区别"])

    got = _resolve("进程线程区别")

    assert got["matched"] is True
    assert got["kp_id"] == kp_ids[0]
    assert got["reason"] == "normalized"


# --------------------------------------------------------------------------- #
# 三、包含匹配 —— **带护栏**
# --------------------------------------------------------------------------- #
def test_contained_match_when_topic_is_more_specific() -> None:
    """主题比标题更具体（「Java 线程」⊃「线程」）→ 唯一候选时允许。"""
    learner = _register(f"lt{uuid4().hex[:10]}")
    kp_ids = _seed(learner, ["线程", "文件系统"])

    got = _resolve("Java 线程")

    assert got["matched"] is True
    assert got["kp_id"] == kp_ids[0]
    assert got["reason"] == "contained"


def test_containment_guard_blocks_the_hazard_from_p1_notes() -> None:
    """⚠️ 本仓库 P1 写下的那条结论：「包含关系极易过度合并，
    「进程」会被并进「进程与线程」」。

    那条结论针对**入库时合并**；这里的用途是**跳转时定位**（非破坏性），
    但危险的形状是同一种 —— 所以护栏照挡：
    `normalize('进程')='进程'`（2 字）⊂ `normalize('进程与线程')='进程线程'`（4 字），
    长度比 0.50 < 0.60 → **拒绝**（`进程` 只有 2 字，也没到包含匹配的最短长度）。
    """
    learner = _register(f"lt{uuid4().hex[:10]}")
    _seed(learner, ["进程与线程"])

    got = _resolve("进程")

    assert got["matched"] is False, "「进程」不该被跳到「进程与线程」"
    assert got["kp_id"] is None


def test_ambiguous_containment_is_refused() -> None:
    """多个候选时**不替用户挑** —— 跳错比不跳更糟。

    主题「线程安全与线程池」同时包含了两个知识点标题，命中 2 个 → 放弃。
    """
    learner = _register(f"lt{uuid4().hex[:10]}")
    _seed(learner, ["线程安全", "线程池"])

    got = _resolve("线程安全与线程池")

    assert got["matched"] is False
    assert got["kp_id"] is None
    assert got["reason"] == "ambiguous"


def test_topic_shorter_than_title_is_never_contained() -> None:
    """`主题 ⊂ 标题` 这个方向**一律不匹配**（只看反方向）。

    例：「线程」⊂「Java 线程模型」→ 拒绝。这是 P1 那条结论点名的形状。
    """
    learner = _register(f"lt{uuid4().hex[:10]}")
    _seed(learner, ["Java 线程模型"])

    got = _resolve("线程")

    assert got["matched"] is False, "不该从更具体的主题退到更宽的标题上"
    assert got["kp_id"] is None


def test_ambiguous_normalized_titles_are_refused() -> None:
    """不同资料里存在同名知识点 → 让用户自己选，别替他挑。"""
    learner = _register(f"lt{uuid4().hex[:10]}")
    _seed(learner, ["作用域"])
    _seed(learner, ["作用域"])

    got = _resolve("作用域")

    assert got["matched"] is False
    assert got["reason"] == "ambiguous"
    assert got["kp_id"] is None


# --------------------------------------------------------------------------- #
# 四、无匹配 → 安全降级
# --------------------------------------------------------------------------- #
def test_no_match_returns_no_kp_id() -> None:
    learner = _register(f"lt{uuid4().hex[:10]}")
    _seed(learner, ["三次握手"])

    got = _resolve("星云的形成")

    assert got["matched"] is False
    assert got["reason"] == "none"
    assert got["kp_id"] is None, "**绝不能伪造一个 kp_id**"
    assert got["title"] == ""
    assert got["document_id"] is None


def test_no_documents_at_all_is_not_an_error() -> None:
    """新用户一份资料都没有 —— 匹配不到是正常结果，不是 5xx。"""
    _register(f"lt{uuid4().hex[:10]}")

    got = _resolve("Java 线程")

    assert got["matched"] is False
    assert got["kp_id"] is None


def test_empty_topic_is_rejected_by_validation() -> None:
    """空主题不该进到匹配逻辑（schema 就拦下）。"""
    _register(f"lt{uuid4().hex[:10]}")

    res = client.post("/api/study/learn-target", json={"topic": ""})

    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# 五、归属：**只在"自己的资料"里找**
# --------------------------------------------------------------------------- #
def test_other_learners_knowledge_points_are_invisible() -> None:
    """别人的知识点**不得**被匹配到 —— 匹配上就等于讲别人的资料。"""
    other = _register(f"lt{uuid4().hex[:10]}")
    _seed(other, ["Java 线程"])

    _register(f"lt{uuid4().hex[:10]}")
    got = _resolve("Java 线程")

    assert got["matched"] is False, "匹配到了别人的知识点"
    assert got["kp_id"] is None


def test_pending_documents_are_not_searchable() -> None:
    """资料还没解析完（PENDING）→ 里面的知识点不该被匹配到。"""
    learner = _register(f"lt{uuid4().hex[:10]}")
    with SessionLocal() as db:
        doc = document_service.create_document(
            db,
            file_name="还没解析完.pdf",
            file_size=1,
            file_hash=f"hash-{uuid4().hex}",
            storage_path="/tmp/x",
            owner_learner_id=learner,
        )
        # 故意**不**置成 READY
        db.add(
            KnowledgePoint(
                document_id=doc.id,
                title="半成品知识点",
                title_norm=knowledge_service.normalize_title("半成品知识点"),
                summary="",
            )
        )
        db.commit()
        assert doc.parse_status != ParseStatus.READY

    got = _resolve("半成品知识点")

    assert got["matched"] is False


# --------------------------------------------------------------------------- #
# 六、返回的字段足以喂给 `LearningProvider.startLearning(focus)`
# --------------------------------------------------------------------------- #
def test_response_carries_everything_learning_focus_needs() -> None:
    """`LearningFocus = { kpId, title, documentId }` —— 三个都必须在。"""
    learner = _register(f"lt{uuid4().hex[:10]}")
    kp_ids = _seed(learner, ["协作式调度"])

    got = _resolve("协作式调度")

    assert got["matched"] is True
    assert got["kp_id"] == kp_ids[0]
    assert isinstance(got["title"], str) and got["title"]
    assert isinstance(got["document_id"], int), "documentId 不能缺，前端要用它"

    # 真要能开教学：拿这个 id 去打 tutor/start 不该 404
    res = client.post("/api/tutor/start", json={"knowledge_point_id": got["kp_id"]})
    assert res.status_code != 404, "解析出的 kp_id 必须是一个真实存在的知识点"
