from __future__ import annotations

from datetime import date, datetime, time
from time import monotonic

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import Appeal, AppealAnnotation, Region


_DASHBOARD_CACHE: dict[tuple[str | None, str | None, str | None, str | None], tuple[float, dict[str, object]]] = {}
_DASHBOARD_CACHE_SECONDS = 60


def clear_dashboard_cache() -> None:
    _DASHBOARD_CACHE.clear()


def _filters(province: str | None, city: str | None, start: str | None, end: str | None) -> list[object]:
    conditions: list[object] = []
    if province:
        conditions.append(Region.province == province)
    if city:
        conditions.append(Region.city == city)
    if start:
        conditions.append(Appeal.received_at >= datetime.combine(date.fromisoformat(start), time.min))
    if end:
        conditions.append(Appeal.received_at <= datetime.combine(date.fromisoformat(end), time.max))
    return conditions


def get_appeals(
    session: Session,
    province: str | None = None,
    city: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[Appeal]:
    statement = (
        select(Appeal)
        .join(Region)
        .options(joinedload(Appeal.annotation), joinedload(Appeal.region))
        .where(*_filters(province, city, start, end))
        .order_by(Appeal.received_at.desc())
    )
    return list(session.scalars(statement).all())


def _group_ranking(
    session: Session,
    column: object,
    conditions: list[object],
    limit: int,
    annotation: bool = False,
) -> list[dict[str, object]]:
    count = func.count(Appeal.id)
    statement = select(column, count).join(Region)
    if annotation:
        statement = statement.join(AppealAnnotation)
    rows = session.execute(
        statement.where(*conditions).group_by(column).order_by(count.desc()).limit(limit)
    ).all()
    return [{"name": name or "未填写", "count": row_count} for name, row_count in rows]


def dashboard_stats(
    session: Session,
    province: str | None = None,
    city: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, object]:
    cache_key = (province, city, start, end)
    cached = _DASHBOARD_CACHE.get(cache_key)
    if cached and monotonic() - cached[0] < _DASHBOARD_CACHE_SECONDS:
        return cached[1]
    conditions = _filters(province, city, start, end)
    responded_condition = and_(
        Appeal.replied_at.is_not(None),
        Appeal.reply_content.is_not(None),
        Appeal.reply_content != "",
    )
    duration_hours = (func.julianday(Appeal.replied_at) - func.julianday(Appeal.received_at)) * 24
    aggregate = session.execute(
        select(
            func.count(Appeal.id),
            func.sum(case((responded_condition, 1), else_=0)),
            func.min(Appeal.received_at),
            func.max(Appeal.received_at),
            func.avg(duration_hours).filter(
                and_(Appeal.replied_at.is_not(None), Appeal.replied_at >= Appeal.received_at)
            ),
        )
        .join(Region)
        .where(*conditions)
    ).one()
    total = aggregate[0] or 0
    responded = aggregate[1] or 0
    monthly_rows = session.execute(
        select(func.strftime("%Y-%m", Appeal.received_at), func.count(Appeal.id))
        .join(Region)
        .where(*conditions)
        .group_by(func.strftime("%Y-%m", Appeal.received_at))
        .order_by(func.strftime("%Y-%m", Appeal.received_at))
    ).all()
    source_rows = session.execute(
        select(AppealAnnotation.source, func.count(Appeal.id))
        .join(Appeal, AppealAnnotation.appeal_id == Appeal.id)
        .join(Region)
        .where(*conditions, AppealAnnotation.topic != "")
        .group_by(AppealAnnotation.source)
    ).all()
    topic_sources = {source or "unknown": count for source, count in source_rows}
    result = {
        "total": total,
        "responded": responded,
        "pending": total - responded,
        "response_rate": round(responded / total * 100, 2) if total else 0,
        "average_response_hours": round(aggregate[4], 1) if aggregate[4] is not None else None,
        "earliest": aggregate[2],
        "latest": aggregate[3],
        "types": _group_ranking(session, Appeal.appeal_type, conditions, 10),
        "departments": _group_ranking(session, Appeal.department, conditions, 12),
        "topics": _group_ranking(session, AppealAnnotation.topic, conditions, 12, annotation=True),
        "topic_sources": topic_sources,
        "annotated": sum(topic_sources.values()),
        "monthly": [{"month": month, "count": count} for month, count in monthly_rows],
    }
    _DASHBOARD_CACHE[cache_key] = (monotonic(), result)
    return result


def search_cases(
    session: Session,
    keyword: str,
    city: str | None = None,
    limit: int = 8,
) -> list[Appeal]:
    keyword = keyword.strip()
    if not keyword:
        return []
    statement = (
        select(Appeal)
        .join(Region)
        .options(joinedload(Appeal.annotation), joinedload(Appeal.region))
        .where(
            *([Region.city == city] if city else []),
            or_(Appeal.redacted_title.contains(keyword), Appeal.redacted_content.contains(keyword)),
        )
        .order_by(Appeal.received_at.desc())
        .limit(limit)
    )
    return list(session.scalars(statement).all())


def available_regions(session: Session) -> list[Region]:
    return list(session.scalars(select(Region).order_by(Region.province, Region.city)).all())
