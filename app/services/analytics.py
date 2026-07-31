from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from time import monotonic
from urllib.parse import quote

from sqlalchemy import and_, case, extract, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Appeal,
    AppealAnnotation,
    CityQuarterAggregate,
    QuarterSnapshot,
    Region,
    ReplyQuality,
)


_DASHBOARD_CACHE: dict[tuple[object, ...], tuple[float, dict[str, object]]] = {}
_DASHBOARD_CACHE_SECONDS = 60


def clear_dashboard_cache() -> None:
    _DASHBOARD_CACHE.clear()


def quarter_bounds(quarter: str) -> tuple[datetime, datetime]:
    try:
        year_text, quarter_text = quarter.upper().split("-Q", 1)
        year, number = int(year_text), int(quarter_text)
        if number not in {1, 2, 3, 4}:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ValueError("季度格式应为 YYYY-Q1 至 YYYY-Q4") from exc
    start_month = (number - 1) * 3 + 1
    start = datetime(year, start_month, 1)
    if number == 4:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, start_month + 3, 1)
    return start, end


def previous_quarter(quarter: str) -> str:
    year_text, quarter_text = quarter.upper().split("-Q", 1)
    year, number = int(year_text), int(quarter_text)
    return f"{year - 1}-Q4" if number == 1 else f"{year}-Q{number - 1}"


def _canonical_condition() -> object:
    return or_(Appeal.is_canonical.is_(True), Appeal.is_canonical.is_(None))


def _filters(
    province: str | None,
    city: str | None,
    start: str | None,
    end: str | None,
    *,
    quarter: str | None = None,
    topic_l1: str | None = None,
    appeal_type: str | None = None,
    canonical_only: bool = True,
) -> list[object]:
    conditions: list[object] = []
    if province:
        conditions.append(Region.province == province)
    if city:
        conditions.append(or_(Region.prefecture_city == city, Region.city == city))
    if quarter:
        conditions.append(Appeal.quarter == quarter)
    if start:
        conditions.append(Appeal.received_at >= datetime.combine(date.fromisoformat(start), time.min))
    if end:
        conditions.append(Appeal.received_at <= datetime.combine(date.fromisoformat(end), time.max))
    if topic_l1:
        conditions.append(AppealAnnotation.topic == topic_l1)
    if appeal_type:
        conditions.append(Appeal.appeal_type == appeal_type)
    if canonical_only:
        conditions.append(_canonical_condition())
    return conditions


def get_appeals(
    session: Session,
    province: str | None = None,
    city: str | None = None,
    start: str | None = None,
    end: str | None = None,
    *,
    quarter: str | None = None,
    topic_l1: str | None = None,
    appeal_type: str | None = None,
    canonical_only: bool = True,
    limit: int | None = None,
) -> list[Appeal]:
    conditions = _filters(
        province,
        city,
        start,
        end,
        quarter=quarter,
        topic_l1=topic_l1,
        appeal_type=appeal_type,
        canonical_only=canonical_only,
    )
    statement = (
        select(Appeal)
        .join(Region)
        .outerjoin(AppealAnnotation)
        .options(joinedload(Appeal.annotation), joinedload(Appeal.region))
        .where(*conditions)
        .order_by(Appeal.received_at.desc())
    )
    if limit:
        statement = statement.limit(limit)
    return list(session.scalars(statement).unique().all())


def _group_ranking(
    session: Session,
    column: object,
    conditions: list[object],
    limit: int,
    *,
    annotation: bool = False,
) -> list[dict[str, object]]:
    count = func.count(Appeal.id)
    statement = select(column, count).join(Region)
    if annotation:
        statement = statement.join(AppealAnnotation)
    rows = session.execute(
        statement.where(*conditions).group_by(column).order_by(count.desc()).limit(limit)
    ).all()
    return [{"name": name or "未填写", "count": int(row_count)} for name, row_count in rows]


def _duration_expression(session: Session):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return extract("epoch", Appeal.replied_at - Appeal.received_at) / 3600
    return (func.julianday(Appeal.replied_at) - func.julianday(Appeal.received_at)) * 24


def _month_expression(session: Session):
    if session.get_bind().dialect.name == "postgresql":
        return func.to_char(Appeal.received_at, "YYYY-MM")
    return func.strftime("%Y-%m", Appeal.received_at)


def _reply_quality_breakdown(
    session: Session,
    conditions: list[object],
) -> list[dict[str, object]]:
    dimensions = (
        ("addresses_issue", "回应核心问题", ReplyQuality.addresses_issue),
        ("explains_basis", "说明依据", ReplyQuality.explains_basis),
        ("provides_action", "给出措施", ReplyQuality.provides_action),
        ("gives_timeline_owner", "明确时限/责任", ReplyQuality.gives_timeline_owner),
        ("provides_followup", "提供后续渠道", ReplyQuality.provides_followup),
    )
    output: list[dict[str, object]] = []
    for key, label, column in dimensions:
        row = session.execute(
            select(
                func.sum(case((column == "yes", 1), else_=0)),
                func.sum(case((column == "no", 1), else_=0)),
                func.sum(case((column == "not_applicable", 1), else_=0)),
                func.sum(
                    case(
                        (or_(column.is_(None), column == "unknown"), 1),
                        else_=0,
                    )
                ),
            )
            .select_from(Appeal)
            .join(Region)
            .outerjoin(AppealAnnotation)
            .outerjoin(ReplyQuality, ReplyQuality.appeal_id == Appeal.id)
            .where(*conditions)
        ).one()
        yes, no, not_applicable, unknown = (int(value or 0) for value in row)
        applicable = yes + no
        output.append(
            {
                "key": key,
                "name": label,
                "yes": yes,
                "no": no,
                "not_applicable": not_applicable,
                "unknown": unknown,
                "applicable": applicable,
                "yes_rate": round(yes / applicable * 100, 2) if applicable else None,
            }
        )
    return output


def dashboard_stats(
    session: Session,
    province: str | None = None,
    city: str | None = None,
    start: str | None = None,
    end: str | None = None,
    *,
    quarter: str | None = None,
    topic_l1: str | None = None,
    appeal_type: str | None = None,
) -> dict[str, object]:
    cache_key = (province, city, start, end, quarter, topic_l1, appeal_type)
    cached = _DASHBOARD_CACHE.get(cache_key)
    if cached and monotonic() - cached[0] < _DASHBOARD_CACHE_SECONDS:
        return cached[1]

    canonical_conditions = _filters(
        province,
        city,
        start,
        end,
        quarter=quarter,
        topic_l1=topic_l1,
        appeal_type=appeal_type,
        canonical_only=True,
    )
    raw_conditions = _filters(
        province,
        city,
        start,
        end,
        quarter=quarter,
        topic_l1=topic_l1,
        appeal_type=appeal_type,
        canonical_only=False,
    )
    responded_condition = and_(
        Appeal.replied_at.is_not(None),
        Appeal.reply_content.is_not(None),
        Appeal.reply_content != "",
    )
    duration_hours = _duration_expression(session)
    aggregate = session.execute(
        select(
            func.count(Appeal.id),
            func.sum(case((responded_condition, 1), else_=0)),
            func.min(Appeal.received_at),
            func.max(Appeal.received_at),
            func.avg(duration_hours).filter(
                and_(Appeal.replied_at.is_not(None), Appeal.replied_at >= Appeal.received_at)
            ),
            func.avg(ReplyQuality.score),
        )
        .join(Region)
        .outerjoin(AppealAnnotation)
        .outerjoin(ReplyQuality, ReplyQuality.appeal_id == Appeal.id)
        .where(*canonical_conditions)
    ).one()
    raw_total = session.scalar(
        select(func.count(Appeal.id))
        .join(Region)
        .outerjoin(AppealAnnotation)
        .where(*raw_conditions)
    ) or 0
    total = aggregate[0] or 0
    responded = aggregate[1] or 0
    month_expr = _month_expression(session)
    monthly_rows = session.execute(
        select(month_expr, func.count(Appeal.id))
        .join(Region)
        .outerjoin(AppealAnnotation)
        .where(*canonical_conditions)
        .group_by(month_expr)
        .order_by(month_expr)
    ).all()
    source_rows = session.execute(
        select(AppealAnnotation.source, func.count(Appeal.id))
        .join(Appeal, AppealAnnotation.appeal_id == Appeal.id)
        .join(Region)
        .where(*canonical_conditions, AppealAnnotation.topic != "")
        .group_by(AppealAnnotation.source)
    ).all()
    topic_sources = {source or "unknown": int(count) for source, count in source_rows}
    result = {
        "total": int(total),
        "event_count": int(total),
        "raw_total": int(raw_total),
        "duplicate_count": max(int(raw_total) - int(total), 0),
        "duplicate_rate": round((raw_total - total) / raw_total * 100, 2) if raw_total else 0,
        "responded": int(responded),
        "pending": int(total - responded),
        "response_rate": round(responded / total * 100, 2) if total else 0,
        "average_response_hours": round(float(aggregate[4]), 1) if aggregate[4] is not None else None,
        "reply_quality_score": round(float(aggregate[5]), 1) if aggregate[5] is not None else None,
        "reply_quality_dimensions": _reply_quality_breakdown(session, canonical_conditions),
        "earliest": aggregate[2],
        "latest": aggregate[3],
        "types": _group_ranking(session, Appeal.appeal_type, canonical_conditions, 10),
        "departments": _group_ranking(session, Appeal.department, canonical_conditions, 12),
        "topics": _group_ranking(
            session, AppealAnnotation.topic, canonical_conditions, 18, annotation=True
        ),
        "subtopics": _group_ranking(
            session, AppealAnnotation.subtopic, canonical_conditions, 20, annotation=True
        ),
        "topic_sources": topic_sources,
        "annotated": sum(topic_sources.values()),
        "monthly": [{"month": month, "count": int(count)} for month, count in monthly_rows],
        "scope": {
            "province": province,
            "city": city,
            "quarter": quarter,
            "start": start,
            "end": end,
            "topic_l1": topic_l1,
            "appeal_type": appeal_type,
            "count_basis": "deduplicated_events",
        },
    }
    _DASHBOARD_CACHE[cache_key] = (monotonic(), result)
    return result


def available_quarters(session: Session) -> list[str]:
    return [
        value
        for value in session.scalars(
            select(Appeal.quarter)
            .where(Appeal.quarter != "")
            .distinct()
            .order_by(Appeal.quarter.desc())
        ).all()
        if value
    ]


def latest_complete_quarter(session: Session) -> str | None:
    active = session.scalar(
        select(QuarterSnapshot)
        .where(QuarterSnapshot.status == "active")
        .order_by(QuarterSnapshot.quarter.desc(), QuarterSnapshot.version.desc())
    )
    if active:
        return active.quarter
    quarters = available_quarters(session)
    return quarters[0] if quarters else None


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
            *([or_(Region.prefecture_city == city, Region.city == city)] if city else []),
            _canonical_condition(),
            or_(
                Appeal.redacted_title.contains(keyword),
                Appeal.redacted_content.contains(keyword),
                Appeal.redacted_reply.contains(keyword),
            ),
        )
        .order_by(Appeal.received_at.desc())
        .limit(limit)
    )
    return list(session.scalars(statement).unique().all())


def available_regions(session: Session) -> list[Region]:
    return list(session.scalars(select(Region).order_by(Region.province, Region.city, Region.district)).all())


def available_prefecture_regions(session: Session) -> list[dict[str, object]]:
    rows = session.execute(
        select(
            Region.province,
            func.coalesce(func.nullif(Region.prefecture_city, ""), Region.city),
            func.max(Region.city_code),
            func.max(Region.longitude),
            func.max(Region.latitude),
        )
        .group_by(Region.province, func.coalesce(func.nullif(Region.prefecture_city, ""), Region.city))
        .order_by(Region.province, func.coalesce(func.nullif(Region.prefecture_city, ""), Region.city))
    ).all()
    return [
        {
            "province": province,
            "city": city,
            "city_code": city_code or "",
            "longitude": longitude,
            "latitude": latitude,
        }
        for province, city, city_code, longitude, latitude in rows
    ]


def map_overview(
    session: Session,
    *,
    quarter: str | None = None,
    topic_l1: str | None = None,
    appeal_type: str | None = None,
) -> dict[str, object]:
    selected_quarter = quarter or latest_complete_quarter(session)
    if not selected_quarter:
        return {"quarter": None, "previous_quarter": None, "cities": [], "unmapped_count": 0}

    active_snapshot = session.scalar(
        select(QuarterSnapshot)
        .where(
            QuarterSnapshot.quarter == selected_quarter,
            QuarterSnapshot.status == "active",
        )
        .order_by(QuarterSnapshot.version.desc())
    )
    current_rows = session.scalars(
        select(CityQuarterAggregate).where(
            CityQuarterAggregate.quarter == selected_quarter,
            CityQuarterAggregate.topic_l1 == (topic_l1 or ""),
            CityQuarterAggregate.appeal_type == (appeal_type or ""),
            *(
                [CityQuarterAggregate.snapshot_id == active_snapshot.id]
                if active_snapshot
                else []
            ),
        )
    ).all()
    previous = previous_quarter(selected_quarter)
    previous_snapshot = session.scalar(
        select(QuarterSnapshot)
        .where(
            QuarterSnapshot.quarter == previous,
            QuarterSnapshot.status == "active",
        )
        .order_by(QuarterSnapshot.version.desc())
    )
    previous_rows = session.scalars(
        select(CityQuarterAggregate).where(
            CityQuarterAggregate.quarter == previous,
            CityQuarterAggregate.topic_l1 == (topic_l1 or ""),
            CityQuarterAggregate.appeal_type == (appeal_type or ""),
            *(
                [CityQuarterAggregate.snapshot_id == previous_snapshot.id]
                if previous_snapshot
                else []
            ),
        )
    ).all()
    previous_by_city = {(item.city_code or item.city): item for item in previous_rows}

    if current_rows:
        coordinates = {
            item["city_code"] or item["city"]: item for item in available_prefecture_regions(session)
        }
        cities: list[dict[str, object]] = []
        unmapped = 0
        for item in current_rows:
            key = item.city_code or item.city
            geo = coordinates.get(key, {})
            if geo.get("longitude") is None or geo.get("latitude") is None:
                unmapped += 1
            previous_item = previous_by_city.get(key)
            growth = (
                round(
                    (item.canonical_count - previous_item.canonical_count)
                    / previous_item.canonical_count
                    * 100,
                    2,
                )
                if previous_item and previous_item.canonical_count
                else None
            )
            cities.append(
                {
                    "province": item.province,
                    "city": item.city,
                    "city_code": item.city_code,
                    "lng": geo.get("longitude"),
                    "lat": geo.get("latitude"),
                    "event_count": item.canonical_count,
                    "raw_count": item.raw_count,
                    "duplicate_rate": item.duplicate_rate,
                    "qoq_growth": growth,
                    "responded": item.responded_count,
                    "response_rate": item.response_rate,
                    "average_response_hours": item.average_response_hours,
                    "reply_quality_score": item.reply_quality_score,
                    "top_topics": item.top_topics,
                    "report_url": (
                        f"/reports?city_code={item.city_code}&city={quote(item.city)}"
                        f"&quarter={selected_quarter}"
                    ),
                }
            )
        return {
            "quarter": selected_quarter,
            "previous_quarter": previous,
            "cities": cities,
            "unmapped_count": unmapped,
        }

    # Development fallback before the first materialized snapshot.
    cities = []
    unmapped = 0
    for region in available_prefecture_regions(session):
        stats = dashboard_stats(
            session,
            province=str(region["province"]),
            city=str(region["city"]),
            quarter=selected_quarter,
            topic_l1=topic_l1,
            appeal_type=appeal_type,
        )
        previous_stats = dashboard_stats(
            session,
            province=str(region["province"]),
            city=str(region["city"]),
            quarter=previous,
            topic_l1=topic_l1,
            appeal_type=appeal_type,
        )
        if not stats["raw_total"]:
            continue
        if region["longitude"] is None or region["latitude"] is None:
            unmapped += 1
        growth = (
            round(
                (stats["event_count"] - previous_stats["event_count"])
                / previous_stats["event_count"]
                * 100,
                2,
            )
            if previous_stats["event_count"]
            else None
        )
        cities.append(
            {
                **region,
                "lng": region["longitude"],
                "lat": region["latitude"],
                "event_count": stats["event_count"],
                "raw_count": stats["raw_total"],
                "duplicate_rate": stats["duplicate_rate"],
                "qoq_growth": growth,
                "responded": stats["responded"],
                "response_rate": stats["response_rate"],
                "average_response_hours": stats["average_response_hours"],
                "reply_quality_score": stats["reply_quality_score"],
                "top_topics": stats["topics"][:5],
                "report_url": (
                    f"/reports?city_code={region['city_code']}&city={quote(str(region['city']))}"
                    f"&quarter={selected_quarter}"
                ),
            }
        )
    return {
        "quarter": selected_quarter,
        "previous_quarter": previous,
        "cities": cities,
        "unmapped_count": unmapped,
    }


def grouped_comparison(
    session: Session,
    quarter: str,
    dimension: str,
    *,
    topic_l1: str | None = None,
) -> list[dict[str, object]]:
    allowed = {
        "macro_region": Region.macro_region,
        "city_tier": Region.city_tier,
        "urban_rural": Region.urban_rural,
        "province": Region.province,
        "city": Region.prefecture_city,
    }
    if dimension not in allowed:
        raise ValueError(f"不支持比较维度：{dimension}")
    column = allowed[dimension]
    conditions = _filters(
        None,
        None,
        None,
        None,
        quarter=quarter,
        topic_l1=topic_l1,
        canonical_only=True,
    )
    rows = session.execute(
        select(
            column,
            func.count(Appeal.id),
            func.avg(ReplyQuality.score),
        )
        .join(Appeal, Appeal.region_id == Region.id)
        .outerjoin(AppealAnnotation, AppealAnnotation.appeal_id == Appeal.id)
        .outerjoin(ReplyQuality, ReplyQuality.appeal_id == Appeal.id)
        .where(*conditions)
        .group_by(column)
        .order_by(func.count(Appeal.id).desc())
    ).all()
    return [
        {
            "name": name or "未知",
            "event_count": int(count),
            "reply_quality_score": round(float(quality), 1) if quality is not None else None,
        }
        for name, count, quality in rows
    ]
