"""Chroma 向量库封装。

P0 只做三件事：初始化持久化客户端、管理集合、暴露健康检查。
真正的向量化（embedder）与检索（retriever）在 P3 实现。

为什么要包装一层：
1. 业务代码不直接依赖 chromadb 的 API，将来切换 pgvector / 云端向量库时改动集中在此；
2. chromadb 的导入失败不应拖垮整个应用 —— 所有方法都做了降级处理；
3. 显式传入 embeddings，避免触发 chromadb 默认的 ONNX 本地模型下载（几十 MB 且易失败）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.config import Settings, settings as default_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class VectorStore:
    """Chroma 持久化客户端的轻量封装（embedded 模式，零运维）。"""

    def __init__(self, config: Settings | None = None) -> None:
        self._settings = config or default_settings
        self._client: Any | None = None
        self._import_error: str | None = None

    # ------------------------------------------------------------- 基础属性
    @property
    def persist_dir(self) -> Path:
        return Path(self._settings.chroma_persist_dir)

    @property
    def collection_name(self) -> str:
        return self._settings.chroma_collection

    @property
    def available(self) -> bool:
        """chromadb 是否可导入。"""
        return self._import_chromadb() is not None

    def _import_chromadb(self) -> Any | None:
        try:
            import chromadb  # noqa: PLC0415 - 延迟导入，缩短应用启动时间

            return chromadb
        except Exception as exc:  # noqa: BLE001
            self._import_error = f"{type(exc).__name__}: {exc}"
            return None

    # --------------------------------------------------------------- 客户端
    def client(self) -> Any:
        """获取持久化客户端（懒初始化 + 单例）。"""
        if self._client is not None:
            return self._client

        chromadb = self._import_chromadb()
        if chromadb is None:
            raise RuntimeError(
                f"chromadb 不可用：{self._import_error}。请执行 pip install chromadb。"
            )

        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        logger.info("Chroma 已初始化：%s", self.persist_dir)
        return self._client

    def collection(self, name: str | None = None) -> Any:
        """获取或创建集合。使用余弦距离，适配归一化后的 embedding。"""
        return self.client().get_or_create_collection(
            name=name or self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    # ------------------------------------------------------- 向量维度守卫
    #: 集合 metadata 里记录向量维度的键
    DIM_KEY = "embedding_dim"
    #: 集合 metadata 里记录向量化"身份"的键（provider:model:dim）
    FINGERPRINT_KEY = "embedding_fingerprint"

    def _recreate_collection(self, name: str, dim: int, fingerprint: str = "") -> Any:
        """删除并重建集合，并把维度与向量化身份记进 metadata。"""
        client = self.client()
        try:
            client.delete_collection(name)
        except Exception as exc:  # noqa: BLE001 - 集合不存在时删除失败可忽略
            logger.debug("删除集合 %s 时忽略异常：%s", name, exc)
        metadata: dict[str, Any] = {"hnsw:space": "cosine", self.DIM_KEY: int(dim)}
        if fingerprint:
            metadata[self.FINGERPRINT_KEY] = fingerprint
        return client.get_or_create_collection(name=name, metadata=metadata)

    def enforce_dimension(
        self, dim: int, *, fingerprint: str = "", name: str | None = None
    ) -> str:
        """确保集合能接受 `dim` 维、且由**同一个向量模型**产生的向量。返回处置结果。

        两层检查，缺一不可：

        1. **维度**：Chroma 的集合维度在第一次写入时就固定，之后删光数据也改不了 ——
           再写不匹配的向量会直接报
           `Collection expecting embedding with dimension of 4, got 1024`。
           这个坑真实发生过：P0 的冒烟测试往生产集合写了 4 维探针向量，
           把 `learning_buddy_chunks` 永久钉死在 4 维。

        2. **指纹（`provider:model:dim`）**：维度相同**不代表语义空间相同**。
           切换 embedding 模型、或从 mock 换成真实模型时，维度可能都是 1024，
           于是维度检查放行，而集合里新旧两套语义空间混杂 ——
           检索时距离完全不可比、排序毫无意义，**而且不会报任何错**。
           指纹比对正是为了识别这种"看起来正常"的污染。

        返回：
          - `ok`          维度与指纹都一致
          - `initialized` 集合为空且无记录 → 重建并记录
          - `reset`       维度或指纹不符 → 重建（向量是派生数据，可从 chunks 完全重建）
          - `unknown`     有数据但无记录 → 无法判断，交给写入时的错误兜底
        """
        target = name or self.collection_name
        collection = self.collection(target)
        meta = collection.metadata or {}
        recorded = meta.get(self.DIM_KEY)
        recorded_fp = meta.get(self.FINGERPRINT_KEY)
        count = collection.count()

        mismatch = ""
        if recorded is not None:
            try:
                if int(recorded) != int(dim):
                    mismatch = f"向量维度 {recorded} → {dim}"
            except (TypeError, ValueError):
                recorded = None
        if not mismatch and recorded_fp and fingerprint and recorded_fp != fingerprint:
            mismatch = f"向量化身份 {recorded_fp} → {fingerprint}"

        if not mismatch and recorded is not None:
            # 指纹缺失时补录。**不补的话这个集合永远检测不出模型切换** ——
            # 早于指纹机制创建的集合都属于这一类。
            #
            # 关于 chromadb 的 modify：它是**整体替换** metadata，不是合并。
            # 且带上 `hnsw:space` 会被拒（"Changing the distance function … not supported"）。
            # 实测结论：丢掉 metadata 里的 `hnsw:space` **不影响距离函数** ——
            # 距离函数在集合创建时就固定在内部，metadata 里那条只是记录。
            # 验证方式：手工算余弦距离与 chroma 返回值逐位一致（0.1449 == 0.1449）。
            # 因此这里显式只传我们自己的两个键，不要传 **meta。
            if fingerprint and not recorded_fp:
                try:
                    collection.modify(
                        metadata={self.DIM_KEY: int(dim), self.FINGERPRINT_KEY: fingerprint}
                    )
                    if count > 0:
                        # 已有数据却无从核对它的来历：补录只让"往后"可检测，
                        # 不能证明已有向量就是当前模型产生的。
                        logger.warning(
                            "集合 %s 已有 %d 条向量但未记录向量化身份，已按当前配置补录为 %s。"
                            "若这些向量来自另一个模型，请重建全部资料的索引 —— "
                            "指纹只能防住之后的切换，防不住之前的混杂。",
                            target,
                            count,
                            fingerprint,
                        )
                    else:
                        logger.info("集合 %s 补录向量化身份 %s", target, fingerprint)
                except Exception as exc:  # noqa: BLE001 - 补录失败不影响检索
                    logger.warning("补录集合 %s 的向量化身份失败：%s", target, exc)
            return "ok"

        if recorded is None and count > 0:
            # 有数据、又没记录 —— 不敢贸然删，让写入方去处理可能的维度错误
            return "unknown"

        self._recreate_collection(target, dim, fingerprint)
        if not mismatch:
            logger.info("集合 %s 未记录向量化信息且为空，已按 %d 维重建", target, dim)
            return "initialized"
        logger.warning(
            "集合 %s 的向量化信息已变化（%s），已重建集合"
            "（向量可由原文块完全重建，不存在数据丢失）",
            target,
            mismatch,
        )
        return "reset"

    # ----------------------------------------------------------------- 操作
    def count(self, name: str | None = None) -> int:
        return int(self.collection(name).count())

    def upsert(
        self,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]] | None = None,
        name: str | None = None,
    ) -> int:
        """写入或更新向量。P0 仅用于冒烟测试，P3 起由 ingest Workflow 调用。

        注意：chromadb 1.5+ 会拒绝**空字典**形式的 metadata
        （Expected metadata to be a non-empty dict）。
        因此不传 metadatas 时必须整个省略该参数，而不是传 [{}] ——
        否则这条默认路径会在第一次真正调用时就炸掉。
        """
        payload: dict[str, Any] = {
            "ids": ids,
            "documents": documents,
            "embeddings": embeddings,
        }
        if metadatas:
            payload["metadatas"] = metadatas

        self.collection(name).upsert(**payload)
        return len(ids)

    def peek(self, limit: int = 3, name: str | None = None) -> dict[str, Any]:
        """窥探集合内数据，用于验证写入是否成功。"""
        return self.collection(name).peek(limit=limit)  # type: ignore[no-any-return]

    # ------------------------------------------------------------ Top-K 检索
    def query(
        self,
        embedding: list[float],
        *,
        top_k: int = 5,
        where: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> list[dict[str, Any]]:
        """向量检索，按距离升序返回。

        `where` 用于按文档过滤（如 `{"document_id": 3}`），支持 Chroma 的过滤语法。

        返回 `[{id, document, metadata, distance}]`，**扁平成一条查询的结果** ——
        chromadb 的 query 返回的是"每条查询向量一组结果"的嵌套结构，
        这里只做单向量检索，扁平成平铺列表对调用方更友好。
        """
        if not embedding:
            return []

        payload: dict[str, Any] = {
            "query_embeddings": [embedding],
            "n_results": max(1, top_k),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            payload["where"] = where

        raw = self.collection(name).query(**payload)

        ids = (raw.get("ids") or [[]])[0]
        documents = (raw.get("documents") or [[]])[0]
        metadatas = (raw.get("metadatas") or [[]])[0]
        distances = (raw.get("distances") or [[]])[0]

        hits: list[dict[str, Any]] = []
        for position, item_id in enumerate(ids):
            hits.append(
                {
                    "id": item_id,
                    "document": documents[position] if position < len(documents) else "",
                    "metadata": metadatas[position] if position < len(metadatas) else {},
                    "distance": (
                        float(distances[position]) if position < len(distances) else None
                    ),
                }
            )
        return hits

    # ------------------------------------------------------- 按文档批量管理
    def delete_document(self, document_id: int, *, name: str | None = None) -> None:
        """删除某个文档的全部向量。

        重建索引前必须先调用 —— 否则换 embedding 模型后，同一份资料会同时存在
        新旧两套向量，检索结果混杂两种语义空间，排序完全不可信。
        """
        try:
            self.collection(name).delete(where={"document_id": int(document_id)})
        except Exception as exc:  # noqa: BLE001 - 集合尚不存在时删除应视为成功
            logger.warning("删除文档 %s 的向量时出现问题（可能集合为空）：%s", document_id, exc)

    def document_chunk_indexes(self, document_id: int, *, name: str | None = None) -> set[int]:
        """已索引的 chunk 序号集合。用于判断索引是否完整。

        注意 `include=["metadatas"]` 不能省 —— 传 `include=[]` 时 chromadb 不会返回
        metadata，函数会静默地总是返回空集合，看起来像"索引没建成功"。
        """
        result = self.collection(name).get(
            where={"document_id": int(document_id)}, include=["metadatas"]
        )
        indexes: set[int] = set()
        for metadata in result.get("metadatas") or []:
            value = (metadata or {}).get("chunk_index")
            if isinstance(value, int):
                indexes.add(value)
        return indexes

    def reset(self, name: str | None = None) -> None:
        """删除并重建集合。仅用于测试与调试。"""
        target = name or self.collection_name
        self.client().delete_collection(target)
        self.collection(target)
        logger.warning("集合 %s 已重置", target)

    # ------------------------------------------------------------- 健康检查
    def health(self) -> dict[str, Any]:
        """探测 Chroma 可用性。永不抛异常。"""
        info: dict[str, Any] = {
            "persist_dir": str(self.persist_dir),
            "collection": self.collection_name,
        }
        if not self.available:
            info.update(
                ok=False,
                error=self._import_error or "chromadb 未安装",
                hint="pip install chromadb",
            )
            return info
        try:
            import chromadb  # noqa: PLC0415

            info["chromadb_version"] = getattr(chromadb, "__version__", "unknown")
            info["count"] = self.count()
            info["collections"] = [c.name for c in self.client().list_collections()]
            info["ok"] = True
        except Exception as exc:  # noqa: BLE001
            info.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        return info


vector_store = VectorStore()
