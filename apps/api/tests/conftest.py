"""全局测试夹具（P0/P1/P2/P3 共用）。

**关键约定：向量测试一律使用独立的测试集合，绝不写生产集合
`learning_buddy_chunks`。**

原因不是洁癖：Chroma 的集合维度在**第一次写入时就固定**，之后即使删光数据也无法更改。
若测试往生产集合里写了 4 维假向量，生产集合就被永久钉死在 4 维，
等接入真实的 1024 维 embedding 时会直接报
`Collection expecting embedding with dimension of 4, got 1024`。
P0 的冒烟脚本正是因为写生产集合而踩了这个坑（现已修）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import app.db.base  # noqa: F401 - 触发全部模型注册
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.chunk import Chunk
from app.models.document import Document
from app.rag.embedding import MockEmbeddingProvider
from app.rag.vectorstore import VectorStore

#: 独立的测试集合。名字必须满足 Chroma 的约束：
#: 3-512 个 [a-zA-Z0-9._-] 字符，且**不能以下划线开头**。
TEST_COLLECTION = "lb-test-vectors"

FIXTURE_HASH = "p3index" + "3" * 57

#: 三块内容，第二块与查询词最相关
CHUNKS = [
    "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。程序是静态的指令集合。",
    "线程是进程内部的执行单元，也是处理机调度的基本单位，同一进程内的线程共享地址空间。",
    "死锁的产生必须同时满足互斥、请求并保持、不可剥夺与循环等待四个条件。",
]


@pytest.fixture()
def mock_embedder() -> MockEmbeddingProvider:
    return MockEmbeddingProvider()


@pytest.fixture()
def test_store() -> VectorStore:
    """指向独立测试集合的向量库，用完直接删掉整个集合。"""
    cfg = get_settings().model_copy(update={"chroma_collection": TEST_COLLECTION})
    store = VectorStore(cfg)
    try:
        store.client().delete_collection(TEST_COLLECTION)
    except Exception:  # noqa: BLE001 - 集合不存在时忽略
        pass
    store.client().get_or_create_collection(
        name=TEST_COLLECTION, metadata={"hnsw:space": "cosine"}
    )
    yield store
    try:
        store.client().delete_collection(TEST_COLLECTION)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture()
def seeded_document():
    """一份含 3 个文本块的临时文档，测试后整份删除。"""
    with SessionLocal() as session:
        for stale in (
            session.query(Document).filter(Document.file_hash == FIXTURE_HASH).all()
        ):
            session.delete(stale)
        session.commit()

        document = Document(
            file_name="p3_index_test.txt",
            file_type="text",
            file_size=sum(len(c) for c in CHUNKS),
            file_hash=FIXTURE_HASH,
            storage_path="test/p3_index_test.txt",
            page_count=3,
            char_count=sum(len(c) for c in CHUNKS),
            chunk_count=len(CHUNKS),
            kp_count=0,
            parse_status="ready",
            progress=100,
        )
        session.add(document)
        session.flush()

        for index, text in enumerate(CHUNKS):
            session.add(
                Chunk(
                    document_id=document.id,
                    chunk_index=index,
                    content=text,
                    page_start=index + 1,
                    page_end=index + 1,
                    block_type="text",
                    heading_path=["第三章 进程管理", f"3.{index + 1} 小节"],
                    char_count=len(text),
                )
            )
        session.commit()
        document_id = document.id

    yield {"document_id": document_id, "chunk_count": len(CHUNKS)}

    with SessionLocal() as session:
        doc = session.get(Document, document_id)
        if doc is not None:
            session.delete(doc)
            session.commit()


@pytest.fixture()
def session():
    """一个用完即关的数据库会话。"""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def zero_decimal() -> Decimal:
    return Decimal("0")


# --------------------------------------------------------------------------- #
# P4：Tutor 测试用的假 LLM 与知识点夹具
# --------------------------------------------------------------------------- #
class ScriptedLLM:
    """可编排的假 LLM，让教学闭环的测试完全确定。

    三类调用按 system 提示词区分：
      - 作答评估 → 从 `assessments` 队列依次取（取完则重复最后一项）
      - 教学决策 → `decision_action` 指定；为 None 时**遵循系统倾向**
        （模拟一个守规矩的模型），为 "illegal" 时返回一个非法动作用于测拦截
      - 内容生成 → 返回带标记的固定文本

    刻意做成"按队列返回"而不是返回固定值：连续答对/答错这类场景必须能逐轮指定结果，
    否则测不出连击驱动的动作切换。
    """

    model = "scripted"
    mode = "mock"

    def __init__(
        self,
        assessments: list[dict] | None = None,
        *,
        decision_action: str | None = None,
        decision_payload: dict | None = None,
        fail_on: set[str] | None = None,
    ) -> None:
        self.assessments = list(assessments or [])
        self.decision_action = decision_action
        self.decision_payload = decision_payload
        #: 命中这些类别时抛异常，用于测降级路径（"assessment"/"decision"/"content"）
        self.fail_on = set(fail_on or ())
        self.calls: list[str] = []
        #: 完整的消息列表，逐次调用累积。断言"Prompt 里到底写了什么"要靠它 ——
        #: 例如 P5 要验证"讲法偏好确实被注入"，只看调用类别是看不出来的。
        self.messages: list[list[dict[str, str]]] = []
        self._last_assessment = self._default_assessment()

    def prompts_of(self, kind: str) -> list[str]:
        """取出某一类调用的全部提示词正文，拼成字符串便于断言。"""
        return [
            "\n".join(message.get("content", "") for message in batch)
            for call_kind, batch in zip(self.calls, self.messages)
            if call_kind == kind
        ]

    @staticmethod
    def _default_assessment() -> dict:
        return {
            "correct": True,
            "score": 0.9,
            "confidence": 0.9,
            "level": "mastered",
            "error_type": "none",
            "missing_points": [],
            "misunderstood_points": [],
            "feedback": "回答准确。",
        }

    @staticmethod
    def correct(score: float = 0.95, level: str = "mastered") -> dict:
        return {
            "correct": True,
            "score": score,
            "confidence": 0.9,
            "level": level,
            "error_type": "none",
            "missing_points": [],
            "misunderstood_points": [],
            "feedback": "回答准确。",
        }

    @staticmethod
    def wrong(score: float = 0.2, error_type: str = "concept_confusion") -> dict:
        return {
            "correct": False,
            "score": score,
            "confidence": 0.9,
            "level": "not_mastered",
            "error_type": error_type,
            "missing_points": ["未答到核心点"],
            "misunderstood_points": ["把两个概念混为一谈"],
            "feedback": "核心概念搞混了。",
        }

    def _classify(self, messages: list[dict[str, str]]) -> str:
        system = messages[0]["content"] if messages else ""
        if "作答评估" in system:
            return "assessment"
        if "教学策略" in system:
            return "decision"
        return "content"

    async def chat_json(self, messages, *, max_tokens=None, mock_builder=None):  # noqa: ANN001, ANN202
        kind = self._classify(messages)
        self.calls.append(kind)
        self.messages.append([dict(message) for message in messages])

        if kind in self.fail_on:
            raise TimeoutError(f"scripted timeout on {kind}")

        if kind == "assessment":
            if self.assessments:
                self._last_assessment = self.assessments.pop(0)
            return dict(self._last_assessment)

        if kind == "decision":
            if self.decision_payload is not None:
                return dict(self.decision_payload)
            if self.decision_action == "illegal":
                return {
                    "action": "dance",
                    "reason": "我要跳个舞",
                    "confidence": 0.9,
                }
            if self.decision_action is not None:
                return {
                    "action": self.decision_action,
                    "knowledge_point_id": None,
                    "reason": "按脚本指定",
                    "confidence": 0.7,
                }
            # 默认：遵循系统倾向
            import re

            user = messages[-1]["content"]
            matched = re.search(r"系统倾向 `([a-z]+)`", user)
            return {
                "action": matched.group(1) if matched else "probe",
                "reason": "遵循系统倾向",
                "confidence": 0.7,
            }

        return {"content": f"[脚本化教学内容 #{len(self.calls)}]"}

    async def chat(self, messages, **_kwargs):  # noqa: ANN001, ANN202
        from app.core.llm import LLMResult

        payload = await self.chat_json(messages)
        if isinstance(payload, dict) and "content" in payload:
            text = str(payload["content"])
        else:
            text = str(payload)
        return LLMResult(content=text, model=self.model, mode=self.mode)


FIXTURE_HASH_KP = "p4tutor" + "4" * 57


@pytest.fixture()
def tutor_kp():
    """一个可用于教学闭环的知识点（连带其文档与文本块）。

    学习状态**刻意不预建** —— 让"首次接触"成为默认起点，
    需要预置状态的用例自己调 learner_service 去改。
    """
    from app.models.knowledge_point import KnowledgePoint

    with SessionLocal() as session:
        for stale in (
            session.query(Document).filter(Document.file_hash == FIXTURE_HASH_KP).all()
        ):
            session.delete(stale)
        session.commit()

        document = Document(
            file_name="p4_tutor_test.txt",
            file_type="text",
            file_size=200,
            file_hash=FIXTURE_HASH_KP,
            storage_path="test/p4_tutor_test.txt",
            page_count=2,
            char_count=200,
            chunk_count=2,
            kp_count=1,
            parse_status="ready",
            progress=100,
        )
        session.add(document)
        session.flush()

        for index, text in enumerate(
            [
                "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。",
                "线程是进程内部的执行单元，是处理机调度的基本单位，线程共享进程的地址空间。",
            ]
        ):
            session.add(
                Chunk(
                    document_id=document.id,
                    chunk_index=index,
                    content=text,
                    page_start=index + 1,
                    page_end=index + 1,
                    block_type="text",
                    heading_path=["第三章 进程管理"],
                    char_count=len(text),
                )
            )

        point = KnowledgePoint(
            document_id=document.id,
            title="进程与线程的区别",
            title_norm="进程与线程的区别",
            summary="进程是资源分配的基本单位，线程是处理机调度的基本单位。",
            details=(
                "进程是程序的一次执行过程，是系统进行资源分配和调度的基本单位。"
                "线程是进程内部的执行单元，是处理机调度的基本单位。"
                "同一进程内的线程共享该进程的地址空间，因此线程切换的开销小于进程切换。"
            ),
            key_points=["进程是资源分配的基本单位", "线程是处理机调度的基本单位", "线程共享地址空间"],
            difficulty=3,
            importance=5,
            confidence=Decimal("0.90"),
            verify_status="unverified",
            heading_path=["第三章 进程管理"],
            source_chunk_indexes=[0, 1],
            source_pages=[1, 2],
            order_index=0,
        )
        session.add(point)
        session.commit()

        document_id, kp_id = document.id, point.id

    yield {"document_id": document_id, "kp_id": kp_id}

    with SessionLocal() as session:
        # 先删学习状态与会话（它们的外键指向知识点）
        from app.models.learner_kp_state import LearnerKpState
        from app.models.session import Session as TutorSession

        session.query(LearnerKpState).filter(
            LearnerKpState.knowledge_point_id == kp_id
        ).delete()
        for s in session.query(TutorSession).filter(
            TutorSession.knowledge_point_id == kp_id
        ).all():
            session.delete(s)
        session.commit()

        doc = session.get(Document, document_id)
        if doc is not None:
            session.delete(doc)
            session.commit()
