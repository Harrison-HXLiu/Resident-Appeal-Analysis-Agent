from __future__ import annotations

import hashlib
import math
import os
from array import array
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AppealEmbedding, AppealChunk


class EmbeddingUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddingSearchResult:
    appeal_id: int
    score: float


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def vector_to_bytes(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def bytes_to_vector(payload: bytes) -> list[float]:
    values = array("f")
    values.frombytes(payload)
    return list(values)


def dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


class DashScopeEmbeddingService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.enabled = bool(self.settings.dashscope_api_key.strip())
        self.model_name = self.settings.embedding_model
        self.batch_size = min(max(1, self.settings.embedding_batch_size), 10)
        if self.enabled:
            os.environ["DASHSCOPE_API_KEY"] = self.settings.dashscope_api_key

    def _embedder(self):
        if not self.enabled:
            raise EmbeddingUnavailable("尚未配置 DASHSCOPE_API_KEY。")
        try:
            from llama_index.embeddings.dashscope import DashScopeEmbedding
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "缺少 llama-index-embeddings-dashscope，请先安装 embedding 依赖。"
            ) from exc
        return DashScopeEmbedding(model_name=self.model_name)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        embedder = self._embedder()
        if len(texts) > 10:
            raise EmbeddingUnavailable("DashScope embedding 单批最多 10 条，请调小 EMBEDDING_BATCH_SIZE。")
        try:
            vectors = embedder.get_text_embedding_batch(texts)
        except Exception as exc:
            message = str(exc)
            if "batch size is invalid" in message or "not be larger than 10" in message:
                raise EmbeddingUnavailable(
                    "DashScope embedding 单批最多 10 条，请设置 EMBEDDING_BATCH_SIZE=10 或更小。"
                ) from exc
            raise
        return [normalize([float(value) for value in vector]) for vector in vectors]

    def embed_query(self, query: str) -> list[float]:
        embedder = self._embedder()
        vector = embedder.get_text_embedding(query)
        return normalize([float(value) for value in vector])


def _embedding_text(chunk: AppealChunk) -> str:
    return chunk.search_text[:1800]


def backfill_embeddings(
    session: Session,
    limit: int | None = None,
    service: DashScopeEmbeddingService | None = None,
) -> int:
    service = service or DashScopeEmbeddingService()
    if not service.enabled:
        raise EmbeddingUnavailable("尚未配置 DASHSCOPE_API_KEY。")

    statement = select(AppealChunk).order_by(AppealChunk.id)
    if limit:
        statement = statement.limit(limit)
    chunks = list(session.scalars(statement).all())
    existing = {
        (item.chunk_id, item.text_hash): item
        for item in session.scalars(
            select(AppealEmbedding).where(AppealEmbedding.model_name == service.model_name)
        ).all()
    }
    pending = [
        chunk
        for chunk in chunks
        if (chunk.id, text_hash(_embedding_text(chunk))) not in existing
    ]
    processed = 0
    for offset in range(0, len(pending), service.batch_size):
        batch = pending[offset : offset + service.batch_size]
        texts = [_embedding_text(chunk) for chunk in batch]
        hashes = [text_hash(text) for text in texts]
        vectors = service.embed_texts(texts)
        for chunk, digest, vector in zip(batch, hashes, vectors):
            record = session.scalar(
                select(AppealEmbedding).where(
                    AppealEmbedding.chunk_id == chunk.id,
                    AppealEmbedding.model_name == service.model_name,
                )
            )
            if record is None:
                record = AppealEmbedding(chunk_id=chunk.id, appeal_id=chunk.appeal_id)
                session.add(record)
            record.model_name = service.model_name
            record.text_hash = digest
            record.vector_dim = len(vector)
            record.vector = vector_to_bytes(vector)
            processed += 1
        session.commit()
    return processed


def search_embeddings(
    session: Session,
    query: str,
    city: str | None = None,
    start: str | None = None,
    end: str | None = None,
    top_k: int | None = None,
    service: DashScopeEmbeddingService | None = None,
) -> list[EmbeddingSearchResult]:
    from app.models import Appeal, Region
    from app.services.rag import _date_end, _date_start

    service = service or DashScopeEmbeddingService()
    if not service.enabled:
        return []
    query_vector = service.embed_query(query)
    statement = (
        select(AppealEmbedding)
        .join(Appeal, AppealEmbedding.appeal_id == Appeal.id)
        .join(Region, Appeal.region_id == Region.id)
        .where(AppealEmbedding.model_name == service.model_name)
    )
    if city:
        statement = statement.where(Region.city == city)
    if start:
        statement = statement.where(Appeal.received_at >= _date_start(start))
    if end:
        statement = statement.where(Appeal.received_at <= _date_end(end))
    records = list(session.scalars(statement).all())
    scored = [
        EmbeddingSearchResult(record.appeal_id, dot(query_vector, bytes_to_vector(record.vector)))
        for record in records
    ]
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored[: (top_k or service.settings.embedding_top_k)]
