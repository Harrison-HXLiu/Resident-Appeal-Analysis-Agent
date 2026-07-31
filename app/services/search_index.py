from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import AnalysisJob, Appeal, AppealAnnotation, Region


def _tokens(value: str) -> str:
    try:
        import jieba
    except ImportError:
        return " ".join(value)
    return " ".join(token.strip() for token in jieba.cut(value) if token.strip())


def build_search_index(
    session: Session,
    quarter: str,
    destination: Path,
    *,
    job: AnalysisJob | None = None,
) -> dict[str, object]:
    """Build an immutable Tantivy/BM25 index.

    If Tantivy is unavailable the quarterly snapshot still succeeds and the
    application uses the existing database retrieval path.  The manifest makes
    this degraded state explicit instead of silently claiming semantic search.
    """

    try:
        import tantivy
    except ImportError:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "UNAVAILABLE").write_text(
            "tantivy dependency is not installed", encoding="utf-8"
        )
        return {"backend": "database-fallback", "document_count": 0}

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("appeal_id", stored=True)
    builder.add_text_field("external_id", stored=True)
    builder.add_text_field("city", stored=True)
    builder.add_text_field("city_code", stored=True)
    builder.add_text_field("quarter", stored=True)
    builder.add_text_field("topic", stored=True)
    builder.add_text_field("title", stored=True)
    builder.add_text_field("content", stored=True)
    builder.add_text_field("reply", stored=True)
    schema = builder.build()
    index = tantivy.Index(schema, path=str(destination))
    writer = index.writer(heap_size=256_000_000)
    rows = session.execute(
        select(
            Appeal.id,
            Appeal.external_id,
            Region.prefecture_city,
            Region.city,
            Region.city_code,
            AppealAnnotation.topic,
            Appeal.redacted_title,
            Appeal.redacted_content,
            Appeal.redacted_reply,
        )
        .join(Region, Region.id == Appeal.region_id)
        .outerjoin(AppealAnnotation, AppealAnnotation.appeal_id == Appeal.id)
        .where(
            Appeal.quarter == quarter,
            or_(Appeal.is_canonical.is_(True), Appeal.is_canonical.is_(None)),
        )
        .execution_options(yield_per=5000)
    )
    count = 0
    for row in rows:
        writer.add_document(
            tantivy.Document(
                appeal_id=str(row[0]),
                external_id=row[1],
                city=row[2] or row[3],
                city_code=row[4] or "",
                quarter=quarter,
                topic=row[5] or "其他/综合",
                title=_tokens(row[6] or ""),
                content=_tokens(row[7] or ""),
                reply=_tokens(row[8] or ""),
            )
        )
        count += 1
        if job and count % 10000 == 0:
            job.processed_count = count
            job.progress = 85
            session.commit()
    writer.commit()
    index.reload()
    return {"backend": "tantivy-bm25", "document_count": count}


def search_index(
    index_path: str | Path,
    query: str,
    *,
    city: str | None = None,
    topic: str | None = None,
    limit: int = 200,
) -> list[tuple[int, float]]:
    try:
        import tantivy
    except ImportError:
        return []
    path = Path(index_path)
    if not path.exists() or (path / "UNAVAILABLE").exists():
        return []
    index = tantivy.Index.open(str(path))
    searcher = index.searcher()
    parsed = index.parse_query(_tokens(query), ["title", "content", "reply", "topic"])
    results = searcher.search(parsed, limit=max(limit * 2, limit)).hits
    selected: list[tuple[int, float]] = []
    for score, address in results:
        document: dict[str, Any] = searcher.doc(address).to_dict()
        document_city = (document.get("city") or [""])[0]
        document_topic = (document.get("topic") or [""])[0]
        if city and document_city != city:
            continue
        if topic and document_topic != topic:
            continue
        appeal_id = int((document.get("appeal_id") or ["0"])[0])
        if appeal_id:
            selected.append((appeal_id, float(score)))
        if len(selected) >= limit:
            break
    return selected
