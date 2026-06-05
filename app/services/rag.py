from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import Appeal, AppealAnnotation, AppealChunk, RagAnswerSource, Region, RetrievalLog
from app.services.embeddings import EmbeddingUnavailable, search_embeddings


FTS_TABLE = "appeal_chunks_fts"
QUERY_STOP_PHRASES = (
    "回复内容",
    "部门回复",
    "政府回复",
    "回复中",
    "回复里",
    "来件内容",
    "问题有哪些",
    "有哪些",
    "提到",
    "涉及",
    "相关",
    "主要",
    "具体",
    "情况",
    "问题",
    "苏州市",
    "苏州",
    "2023年",
    "2024年",
    "2025年",
    "2026年",
)
QUERY_STOP_CHARS = ("的", "了", "和", "与", "年")
MIN_MATCHED_TERMS = 2


@dataclass(frozen=True)
class RagSource:
    appeal_id: int
    external_id: str
    title: str
    received_at: str
    appeal_type: str
    department: str
    topic: str
    content_excerpt: str
    reply_excerpt: str
    score: float
    matched_fields: tuple[str, ...] = ()
    rank: int = 0


@dataclass(frozen=True)
class RagEvidence:
    query: str
    candidate_count: int
    embedding_candidate_count: int
    relevant_count: int
    selected_sources: list[RagSource]
    evidence_text: str
    retrieval_log_id: int | None = None


def _sqlite_path() -> Path:
    url = get_settings().database_url
    if url.startswith("sqlite:///./"):
        return get_settings().base_dir / url.removeprefix("sqlite:///./")
    if url.startswith("sqlite:///"):
        return Path(url.removeprefix("sqlite:///"))
    raise RuntimeError("RAG FTS5 currently supports SQLite DATABASE_URL only.")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(_sqlite_path())
    connection.row_factory = sqlite3.Row
    return connection


def ensure_fts_index() -> None:
    with _connect() as connection:
        try:
            connection.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE}
                USING fts5(
                    chunk_id UNINDEXED,
                    appeal_id UNINDEXED,
                    city UNINDEXED,
                    title,
                    content,
                    reply,
                    topic,
                    department,
                    tokenize='trigram'
                )
                """
            )
        except sqlite3.OperationalError:
            connection.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE}
                USING fts5(
                    chunk_id UNINDEXED,
                    appeal_id UNINDEXED,
                    city UNINDEXED,
                    title,
                    content,
                    reply,
                    topic,
                    department,
                    tokenize='unicode61'
                )
                """
            )
        connection.commit()


def rebuild_fts_index() -> None:
    with _connect() as connection:
        connection.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
        connection.commit()
    ensure_fts_index()
    with _connect() as connection:
        connection.execute(f"DELETE FROM {FTS_TABLE}")
        connection.execute(
            f"""
            INSERT INTO {FTS_TABLE}
                (chunk_id, appeal_id, city, title, content, reply, topic, department)
            SELECT
                c.id,
                c.appeal_id,
                r.city,
                c.title,
                c.content_excerpt,
                c.reply_excerpt,
                coalesce(aa.topic, ''),
                a.department
            FROM appeal_chunks c
            JOIN appeals a ON a.id = c.appeal_id
            JOIN regions r ON r.id = a.region_id
            LEFT JOIN appeal_annotations aa ON aa.appeal_id = a.id
            """
        )
        connection.commit()


def _shorten(text: str | None, limit: int) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def build_chunk_text(appeal: Appeal) -> tuple[str, str, str, str]:
    topic = appeal.annotation.topic if appeal.annotation else ""
    title = _shorten(appeal.redacted_title or appeal.title, 180)
    content = _shorten(appeal.redacted_content or appeal.content, 900)
    reply = _shorten(appeal.redacted_reply or appeal.reply_content or "", 600)
    search_text = "\n".join(
        part
        for part in [
            f"标题：{title}",
            f"来件类型：{appeal.appeal_type}",
            f"主题：{topic}",
            f"回复部门：{appeal.department}",
            f"来件内容：{content}",
            f"回复内容：{reply}",
        ]
        if part.strip()
    )
    return search_text, title, content, reply


def backfill_chunks(session: Session, limit: int | None = None) -> int:
    statement = (
        select(Appeal)
        .options(joinedload(Appeal.annotation))
        .outerjoin(AppealChunk)
        .where(AppealChunk.id.is_(None))
        .order_by(Appeal.id)
    )
    if limit:
        statement = statement.limit(limit)
    appeals = list(session.scalars(statement).all())
    for appeal in appeals:
        search_text, title, content, reply = build_chunk_text(appeal)
        session.add(
            AppealChunk(
                appeal_id=appeal.id,
                search_text=search_text,
                title=title,
                content_excerpt=content,
                reply_excerpt=reply,
            )
        )
    if appeals:
        session.commit()
        rebuild_fts_index()
    else:
        ensure_fts_index()
    return len(appeals)


def upsert_chunk_for_appeal(session: Session, appeal: Appeal) -> None:
    if appeal.id is None:
        session.flush()
    search_text, title, content, reply = build_chunk_text(appeal)
    chunk = session.scalar(select(AppealChunk).where(AppealChunk.appeal_id == appeal.id))
    if chunk is None:
        chunk = AppealChunk(appeal_id=appeal.id)
        session.add(chunk)
    chunk.search_text = search_text
    chunk.title = title
    chunk.content_excerpt = content
    chunk.reply_excerpt = reply


def _clean_question(question: str) -> str:
    cleaned = question
    for phrase in QUERY_STOP_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    cleaned = re.sub(r"(?<!\d)\d{2}\s*年", " ", cleaned)
    cleaned = re.sub(r"(?:19|20)\d{2}\s*年", " ", cleaned)
    for char in QUERY_STOP_CHARS:
        cleaned = cleaned.replace(char, " ")
    return cleaned


def _term_variants(question: str) -> list[str]:
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}", _clean_question(question))
    seen: list[str] = []
    for token in tokens:
        variants = [token]
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 2:
            variants.extend(token[index : index + 2] for index in range(len(token) - 1))
            variants.extend(token[index : index + 3] for index in range(len(token) - 2))
        for variant in variants:
            if variant not in seen:
                seen.append(variant)
    return seen


def _query_terms(question: str) -> str:
    terms = _term_variants(question)
    return " OR ".join(terms[:12]) or question.strip()


def _is_reply_intent(question: str) -> bool:
    return any(word in question for word in ("回复", "答复", "办理", "处理结果", "部门怎么说"))


def _date_start(value: str | None) -> str | None:
    if not value:
        return None
    return f"{value} 00:00:00" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else value


def _date_end(value: str | None) -> str | None:
    if not value:
        return None
    return f"{value} 23:59:59" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else value


def _like_fallback(
    session: Session,
    question: str,
    city: str | None,
    start: str | None,
    end: str | None,
    limit: int,
    reply_only: bool = False,
) -> list[tuple[int, float]]:
    tokens = _term_variants(question)[:10]
    if not tokens:
        return []
    statement = select(AppealChunk.appeal_id).join(Appeal).join(Region)
    if city:
        statement = statement.where(Region.city == city)
    if start:
        statement = statement.where(Appeal.received_at >= _date_start(start))
    if end:
        statement = statement.where(Appeal.received_at <= _date_end(end))
    conditions = []
    for token in tokens:
        pattern = f"%{token}%"
        target = AppealChunk.reply_excerpt if reply_only else AppealChunk.search_text
        conditions.append(target.like(pattern))
    statement = statement.where(or_(*conditions)).limit(limit)
    return [(appeal_id, 0.1) for appeal_id in session.scalars(statement).all()]


def _contains_any(value: str, terms: list[str]) -> bool:
    return any(term and term in value for term in terms)


def _matched_term_count(text: str, question: str) -> int:
    terms = _term_variants(question)
    return sum(1 for term in terms if term and term in text)


def _field_matches(
    question: str,
    title: str,
    content: str,
    reply: str,
    topic: str,
    department: str,
) -> tuple[str, ...]:
    terms = _term_variants(question)
    fields: list[str] = []
    if _contains_any(title, terms):
        fields.append("标题")
    if _contains_any(content, terms):
        fields.append("来件内容")
    if _contains_any(reply, terms):
        fields.append("回复内容")
    if _contains_any(topic, terms):
        fields.append("主题")
    if _contains_any(department, terms):
        fields.append("回复部门")
    return tuple(fields)


def search_relevant_appeals(
    session: Session,
    question: str,
    city: str | None = None,
    start: str | None = None,
    end: str | None = None,
    candidate_limit: int = 80,
) -> list[tuple[int, float]]:
    ensure_fts_index()
    query = _query_terms(question)
    params: list[object] = [query, query, query, query, query, query]
    filters = ["fts.reply MATCH ?" if _is_reply_intent(question) else "fts MATCH ?"]
    if city:
        filters.append("fts.city = ?")
        params.append(city)
    if start:
        filters.append("a.received_at >= ?")
        params.append(_date_start(start))
    if end:
        filters.append("a.received_at <= ?")
        params.append(_date_end(end))
    params.append(candidate_limit)
    sql = f"""
        SELECT
            fts.appeal_id,
            bm25(fts, 1.8, 1.2, 1.8, 1.4, 1.1) AS score,
            (
                CASE WHEN fts.title MATCH ? THEN 1.8 ELSE 0 END +
                CASE WHEN fts.content MATCH ? THEN 1.2 ELSE 0 END +
                CASE WHEN fts.reply MATCH ? THEN 2.4 ELSE 0 END +
                CASE WHEN fts.topic MATCH ? THEN 1.1 ELSE 0 END +
                CASE WHEN fts.department MATCH ? THEN 0.8 ELSE 0 END
            ) AS field_boost
        FROM {FTS_TABLE} fts
        JOIN appeals a ON a.id = fts.appeal_id
        WHERE {' AND '.join(filters)}
        ORDER BY (score - field_boost) ASC
        LIMIT ?
    """
    try:
        with _connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        results = [
            (int(row["appeal_id"]), float(row["score"]) - float(row["field_boost"]))
            for row in rows
        ]
        if results:
            return results
        return results or _like_fallback(
            session, question, city, start, end, candidate_limit, reply_only=_is_reply_intent(question)
        )
    except sqlite3.OperationalError:
        return _like_fallback(
            session, question, city, start, end, candidate_limit, reply_only=_is_reply_intent(question)
        )


def hybrid_search_relevant_appeals(
    session: Session,
    question: str,
    city: str | None = None,
    start: str | None = None,
    end: str | None = None,
    candidate_limit: int = 80,
) -> tuple[list[tuple[int, float]], int, int]:
    fts_results = search_relevant_appeals(session, question, city, start, end, candidate_limit)
    merged: dict[int, float] = {}
    for rank, (appeal_id, score) in enumerate(fts_results, start=1):
        merged[appeal_id] = merged.get(appeal_id, 0) + max(0.0, 1.0 - (rank - 1) / max(candidate_limit, 1))

    embedding_count = 0
    try:
        embedding_results = search_embeddings(
            session,
            question,
            city=city,
            start=start,
            end=end,
            top_k=min(40, candidate_limit),
        )
        embedding_count = len(embedding_results)
        for rank, item in enumerate(embedding_results, start=1):
            rank_score = max(0.0, 1.0 - (rank - 1) / max(len(embedding_results), 1))
            merged[item.appeal_id] = merged.get(item.appeal_id, 0) + 1.25 * rank_score + item.score
    except EmbeddingUnavailable:
        embedding_count = 0
    except Exception:
        embedding_count = 0

    ranked = sorted(merged.items(), key=lambda item: item[1], reverse=True)
    return [(appeal_id, -score) for appeal_id, score in ranked[:candidate_limit]], len(fts_results), embedding_count


def _load_sources(session: Session, scored_ids: list[tuple[int, float]], question: str) -> list[RagSource]:
    if not scored_ids:
        return []
    score_map = {appeal_id: score for appeal_id, score in scored_ids}
    order = {appeal_id: index for index, (appeal_id, _) in enumerate(scored_ids)}
    appeals = list(
        session.scalars(
            select(Appeal)
            .options(joinedload(Appeal.annotation), joinedload(Appeal.region))
            .where(Appeal.id.in_(score_map))
        ).all()
    )
    appeals.sort(key=lambda appeal: order[appeal.id])
    sources: list[RagSource] = []
    for appeal in appeals:
        title = _shorten(appeal.redacted_title or appeal.title, 120)
        content = _shorten(appeal.redacted_content or appeal.content, 280)
        reply = _shorten(appeal.redacted_reply or appeal.reply_content or "", 280)
        topic = appeal.annotation.topic if appeal.annotation else "未标注"
        matched_fields = _field_matches(
            question,
            appeal.redacted_title or appeal.title,
            appeal.redacted_content or appeal.content,
            appeal.redacted_reply or appeal.reply_content or "",
            topic,
            appeal.department,
        )
        full_text = "\n".join(
            [
                appeal.redacted_title or appeal.title,
                appeal.redacted_content or appeal.content,
                appeal.redacted_reply or appeal.reply_content or "",
                topic,
                appeal.department,
            ]
        )
        if _matched_term_count(full_text, question) < MIN_MATCHED_TERMS:
            continue
        source = RagSource(
            appeal_id=appeal.id,
            external_id=appeal.external_id,
            title=title,
            received_at=appeal.received_at.strftime("%Y-%m-%d"),
            appeal_type=appeal.appeal_type,
            department=appeal.department,
            topic=topic,
            content_excerpt=content,
            reply_excerpt=reply,
            score=score_map[appeal.id],
            matched_fields=matched_fields,
        )
        sources.append(source)
    return sources


def select_diverse_sources(sources: list[RagSource], limit: int = 12) -> list[RagSource]:
    selected: list[RagSource] = []
    seen_topics: set[str] = set()
    seen_departments: set[str] = set()
    seen_titles: set[str] = set()
    for source in sources:
        title_key = source.title[:28]
        if title_key in seen_titles:
            continue
        if not source.matched_fields:
            continue
        is_diverse = source.topic not in seen_topics or source.department not in seen_departments
        if is_diverse or len(selected) < max(4, limit // 2):
            selected.append(source)
            seen_topics.add(source.topic)
            seen_departments.add(source.department)
            seen_titles.add(title_key)
        if len(selected) >= limit:
            break
    for source in sources:
        if len(selected) >= limit:
            break
        if all(item.appeal_id != source.appeal_id for item in selected):
            selected.append(source)
    return [
        RagSource(
            appeal_id=source.appeal_id,
            external_id=source.external_id,
            title=source.title,
            received_at=source.received_at,
            appeal_type=source.appeal_type,
            department=source.department,
            topic=source.topic,
            content_excerpt=source.content_excerpt,
            reply_excerpt=source.reply_excerpt,
            score=source.score,
            matched_fields=source.matched_fields,
            rank=index,
        )
        for index, source in enumerate(selected, start=1)
    ]


def build_evidence(
    session: Session,
    question: str,
    city: str | None = None,
    start: str | None = None,
    end: str | None = None,
    candidate_limit: int = 80,
    source_limit: int = 12,
    persist: bool = True,
) -> RagEvidence:
    backfill_chunks(session)
    scored_ids, fts_count, embedding_count = hybrid_search_relevant_appeals(
        session, question, city, start, end, candidate_limit
    )
    loaded_sources = _load_sources(session, scored_ids, question)
    sources = select_diverse_sources(loaded_sources, source_limit)
    lines = []
    for source in sources:
        lines.append(
            "\n".join(
                [
                    f"[{source.rank}] 信件编号：{source.external_id}",
                    f"日期：{source.received_at}；类型：{source.appeal_type}；主题：{source.topic}；回复部门：{source.department}",
                    f"标题：{source.title}",
                    f"命中字段：{'、'.join(source.matched_fields) if source.matched_fields else '综合相关'}",
                    f"来件摘要：{source.content_excerpt}",
                    f"回复摘要：{source.reply_excerpt or '暂无回复摘要'}",
                ]
            )
        )
    log_id: int | None = None
    if persist:
        log = RetrievalLog(
            question=question,
            city=city or "",
            start_date=start or "",
            end_date=end or "",
            candidate_count=fts_count,
            embedding_candidate_count=embedding_count,
            selected_count=len(sources),
        )
        session.add(log)
        session.flush()
        log_id = log.id
        for source in sources:
            session.add(
                RagAnswerSource(
                    retrieval_log_id=log.id,
                    appeal_id=source.appeal_id,
                    rank=source.rank,
                    score=source.score,
                    reason="fts5",
                )
            )
        session.commit()
    return RagEvidence(
        query=_query_terms(question),
        candidate_count=fts_count,
        embedding_candidate_count=embedding_count,
        relevant_count=len(loaded_sources),
        selected_sources=sources,
        evidence_text="\n\n".join(lines),
        retrieval_log_id=log_id,
    )
