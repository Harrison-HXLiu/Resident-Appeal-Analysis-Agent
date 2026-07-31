"""Legacy vector helpers.

Nationwide per-appeal vector backfills are intentionally disabled in v2. The
active retrieval path is a quarterly Tantivy/BM25 index followed by bounded
reranking. Serialization helpers remain for old databases and tests.
"""

from __future__ import annotations

import math
from array import array


class EmbeddingUnavailable(RuntimeError):
    pass


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


def backfill_embeddings(*args, **kwargs) -> int:
    raise EmbeddingUnavailable(
        "全量留言向量回填已禁用；请构建季度 Tantivy/BM25 快照并对有限候选重排。"
    )


def search_embeddings(*args, **kwargs) -> list[object]:
    return []
