"""Qdrant vector store with one collection per customer (hard isolation)."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass

from qdrant_client import QdrantClient, models

from .config import Settings

log = logging.getLogger(__name__)

_NS = uuid.UUID("6f1c7a52-3f7e-4f4b-9a7e-2f2d7c3b9e10")


@dataclass
class Hit:
    score: float
    doc_id: str
    page: int
    chunk_index: int
    text: str
    filename: str
    title: str | None


class DimensionMismatch(RuntimeError):
    pass


class VectorStore:
    def __init__(self, settings: Settings):
        self.s = settings
        self._lock = threading.RLock()
        if settings.qdrant_path:
            settings.qdrant_path.mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(path=str(settings.qdrant_path))
        else:
            self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=60)

    def collection(self, customer: str) -> str:
        return f"{self.s.collection_prefix}{customer.lower()}"

    def ensure_collection(self, customer: str, dim: int) -> None:
        name = self.collection(customer)
        with self._lock:
            if self.client.collection_exists(name):
                info = self.client.get_collection(name)
                existing = info.config.params.vectors
                size = existing.size if hasattr(existing, "size") else None
                if size is not None and size != dim:
                    raise DimensionMismatch(
                        f"Collection {name} has vector size {size} but the embedding model returns {dim}. "
                        f"Run `pdf-mcp reindex --customer {customer} --recreate` after changing embed models."
                    )
                return
            self.client.create_collection(
                name, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE)
            )
            if not self.s.qdrant_path:  # payload indexes are a no-op in embedded mode
                for field in ("doc_id", "filename"):
                    self.client.create_payload_index(name, field, models.PayloadSchemaType.KEYWORD)
            log.info("Created Qdrant collection %s (dim=%d)", name, dim)

    def drop_collection(self, customer: str) -> None:
        with self._lock:
            name = self.collection(customer)
            if self.client.collection_exists(name):
                self.client.delete_collection(name)

    @staticmethod
    def point_id(doc_id: str, chunk_index: int) -> str:
        return str(uuid.uuid5(_NS, f"{doc_id}:{chunk_index}"))

    def upsert(self, customer: str, doc_id: str, vectors: list[list[float]], payloads: list[dict]) -> None:
        name = self.collection(customer)
        points = [
            models.PointStruct(id=self.point_id(doc_id, p["chunk_index"]), vector=v, payload=p)
            for v, p in zip(vectors, payloads, strict=True)
        ]
        with self._lock:
            for i in range(0, len(points), 256):
                self.client.upsert(name, points=points[i : i + 256], wait=True)

    def delete_doc(self, customer: str, doc_id: str) -> None:
        name = self.collection(customer)
        with self._lock:
            if not self.client.collection_exists(name):
                return
            self.client.delete(
                name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
                    )
                ),
                wait=True,
            )

    def search(
        self, customer: str, vector: list[float], limit: int, doc_ids: list[str] | None = None
    ) -> list[Hit]:
        name = self.collection(customer)
        with self._lock:
            if not self.client.collection_exists(name):
                return []
            flt = None
            if doc_ids:
                flt = models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchAny(any=doc_ids))])
            res = self.client.query_points(
                name,
                query=vector,
                limit=limit,
                query_filter=flt,
                with_payload=True,
                score_threshold=self.s.score_threshold,
            )
        return [
            Hit(
                score=float(p.score),
                doc_id=p.payload["doc_id"],
                page=int(p.payload["page"]),
                chunk_index=int(p.payload["chunk_index"]),
                text=p.payload["text"],
                filename=p.payload.get("filename", ""),
                title=p.payload.get("title"),
            )
            for p in res.points
        ]

    def ping(self) -> str:
        with self._lock:
            cols = self.client.get_collections().collections
        return f"ok ({len(cols)} collections)"
