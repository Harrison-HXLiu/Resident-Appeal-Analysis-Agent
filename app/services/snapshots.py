from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AnalysisJob,
    Appeal,
    AppealAnnotation,
    CityQuarterAggregate,
    QuarterSnapshot,
    Region,
    ReplyQuality,
)
from app.services.analytics import clear_dashboard_cache
from app.services.dedup import cluster_near_duplicates
from app.services.duckdb_analytics import validate_snapshot_dataset
from app.services.search_index import build_search_index
from app.services.taxonomy import active_taxonomy


_SAFE_PARTITION_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")


def _partition_name(value: str) -> str:
    return _SAFE_PARTITION_RE.sub("_", value.strip() or "未知")


def _snapshot_rows(session: Session, quarter: str):
    statement = (
        select(
            Appeal.id,
            Appeal.external_id,
            Appeal.received_at,
            Appeal.replied_at,
            Appeal.appeal_type,
            Appeal.appeal_type_raw,
            Appeal.redacted_title,
            Appeal.redacted_content,
            Appeal.department,
            Appeal.redacted_reply,
            Appeal.content_hash,
            Appeal.duplicate_group_key,
            Appeal.is_canonical,
            Region.province,
            Region.city,
            Region.district,
            Region.province_code,
            Region.city_code,
            Region.district_code,
            Region.prefecture_city,
            Region.macro_region,
            Region.city_tier,
            Region.urban_rural,
            AppealAnnotation.topic,
            AppealAnnotation.subtopic,
            AppealAnnotation.auxiliary_labels,
            AppealAnnotation.confidence,
            ReplyQuality.score,
        )
        .join(Region, Region.id == Appeal.region_id)
        .outerjoin(AppealAnnotation, AppealAnnotation.appeal_id == Appeal.id)
        .outerjoin(ReplyQuality, ReplyQuality.appeal_id == Appeal.id)
        .where(Appeal.quarter == quarter)
        .order_by(Appeal.id)
        .execution_options(yield_per=5000)
    )
    for row in session.execute(statement):
        yield {
            "appeal_id": row[0],
            "external_id": row[1],
            "received_at": row[2],
            "replied_at": row[3],
            "appeal_type": row[4],
            "appeal_type_raw": row[5],
            "title": row[6],
            "content": row[7],
            "department": row[8],
            "reply": row[9],
            "content_hash": row[10],
            "duplicate_group_key": row[11],
            "is_canonical": bool(row[12]),
            "province": row[13],
            "city": row[14],
            "district": row[15],
            "province_code": row[16],
            "city_code": row[17],
            "district_code": row[18],
            "prefecture_city": row[19] or row[14],
            "macro_region": row[20] or "未知",
            "city_tier": row[21] or "普通地级市",
            "urban_rural": row[22] or "未知",
            "topic_l1": row[23] or "其他/综合",
            "topic_l2": row[24] or "",
            "auxiliary_labels": json.dumps(row[25] or [], ensure_ascii=False),
            "label_confidence": row[26],
            "reply_quality_score": row[27],
            "quarter": quarter,
        }


def _write_parquet_dataset(
    session: Session,
    quarter: str,
    destination: Path,
    job: AnalysisJob | None = None,
) -> tuple[int, int, int]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - exercised in deployment checks
        raise RuntimeError("创建Parquet快照需要安装 pyarrow") from exc

    writers: dict[str, Any] = {}
    row_count = 0
    canonical_count = 0
    batch: list[dict[str, object]] = []

    def flush_rows(rows: list[dict[str, object]]) -> None:
        if not rows:
            return
        by_province: dict[str, list[dict[str, object]]] = defaultdict(list)
        for item in rows:
            by_province[str(item["province"])].append(item)
        for province, province_rows in by_province.items():
            partition = destination / f"province={_partition_name(province)}"
            partition.mkdir(parents=True, exist_ok=True)
            target = partition / "appeals.parquet"
            table = pa.Table.from_pylist(province_rows)
            writer = writers.get(province)
            if writer is None:
                writer = pq.ParquetWriter(target, table.schema, compression="zstd")
                writers[province] = writer
            writer.write_table(table)

    try:
        for item in _snapshot_rows(session, quarter):
            row_count += 1
            canonical_count += int(bool(item["is_canonical"]))
            batch.append(item)
            if len(batch) >= 5000:
                flush_rows(batch)
                batch.clear()
                if job:
                    job.processed_count = row_count
                    job.progress = min(65, 10 + row_count // 10000)
                    session.commit()
        flush_rows(batch)
    finally:
        for writer in writers.values():
            writer.close()
    total_bytes = sum(path.stat().st_size for path in destination.rglob("*.parquet"))
    return row_count, canonical_count, total_bytes


def _new_aggregate() -> dict[str, object]:
    return {
        "raw": 0,
        "canonical": 0,
        "responded": 0,
        "response_hours": [],
        "quality": [],
        "topics": defaultdict(int),
        "province": "",
        "city": "",
        "city_code": "",
    }


def build_city_aggregates(
    session: Session,
    snapshot: QuarterSnapshot,
    job: AnalysisJob | None = None,
) -> int:
    session.execute(
        delete(CityQuarterAggregate).where(CityQuarterAggregate.snapshot_id == snapshot.id)
    )
    aggregates: dict[tuple[str, str, str], dict[str, object]] = defaultdict(_new_aggregate)
    rows = session.execute(
        select(
            Region.province,
            Region.city,
            Region.prefecture_city,
            Region.city_code,
            Appeal.appeal_type,
            Appeal.is_canonical,
            Appeal.received_at,
            Appeal.replied_at,
            Appeal.reply_content,
            AppealAnnotation.topic,
            ReplyQuality.score,
        )
        .join(Appeal, Appeal.region_id == Region.id)
        .outerjoin(AppealAnnotation, AppealAnnotation.appeal_id == Appeal.id)
        .outerjoin(ReplyQuality, ReplyQuality.appeal_id == Appeal.id)
        .where(Appeal.quarter == snapshot.quarter)
        .execution_options(yield_per=10000)
    )
    processed = 0
    for row in rows:
        processed += 1
        province, city, prefecture, city_code = row[0], row[1], row[2] or row[1], row[3] or ""
        appeal_type = row[4] or "其他"
        is_canonical = bool(row[5])
        topic = row[9] or "其他/综合"
        slices = (("", ""), (topic, ""), ("", appeal_type), (topic, appeal_type))
        for topic_slice, type_slice in slices:
            aggregate = aggregates[(city_code or prefecture, topic_slice, type_slice)]
            aggregate["province"] = province
            aggregate["city"] = prefecture
            aggregate["city_code"] = city_code
            aggregate["raw"] = int(aggregate["raw"]) + 1
            if is_canonical:
                aggregate["canonical"] = int(aggregate["canonical"]) + 1
                aggregate["topics"][topic] += 1  # type: ignore[index]
                if row[7] is not None and row[8]:
                    aggregate["responded"] = int(aggregate["responded"]) + 1
                    if row[7] >= row[6]:
                        aggregate["response_hours"].append(
                            (row[7] - row[6]).total_seconds() / 3600
                        )
                if row[10] is not None:
                    aggregate["quality"].append(float(row[10]))
        if job and processed % 10000 == 0:
            job.processed_count = processed
            job.progress = 75
            session.commit()

    for (_, topic_l1, appeal_type), item in aggregates.items():
        raw_count = int(item["raw"])
        canonical_count = int(item["canonical"])
        responded_count = int(item["responded"])
        response_hours = item["response_hours"]
        quality = item["quality"]
        top_topics = [
            {"name": name, "count": count}
            for name, count in sorted(
                item["topics"].items(), key=lambda entry: entry[1], reverse=True  # type: ignore[union-attr]
            )[:5]
        ]
        session.add(
            CityQuarterAggregate(
                snapshot_id=snapshot.id,
                quarter=snapshot.quarter,
                province=str(item["province"]),
                city=str(item["city"]),
                city_code=str(item["city_code"]),
                topic_l1=topic_l1,
                appeal_type=appeal_type,
                raw_count=raw_count,
                canonical_count=canonical_count,
                duplicate_rate=round((raw_count - canonical_count) / raw_count * 100, 2)
                if raw_count
                else 0,
                responded_count=responded_count,
                response_rate=round(responded_count / canonical_count * 100, 2)
                if canonical_count
                else 0,
                average_response_hours=round(sum(response_hours) / len(response_hours), 1)
                if response_hours
                else None,
                reply_quality_score=round(sum(quality) / len(quality), 1) if quality else None,
                top_topics=top_topics,
            )
        )
    session.flush()
    return len(aggregates)


def build_quarter_snapshot(
    session: Session,
    quarter: str,
    job: AnalysisJob | None = None,
) -> QuarterSnapshot:
    settings = get_settings()
    taxonomy = active_taxonomy(session)
    latest_version = session.scalar(
        select(func.max(QuarterSnapshot.version)).where(QuarterSnapshot.quarter == quarter)
    )
    snapshot = QuarterSnapshot(
        quarter=quarter,
        version=int(latest_version or 0) + 1,
        taxonomy_version_id=taxonomy.id,
        status="building",
    )
    session.add(snapshot)
    session.commit()
    if job:
        job.status = "running"
        job.started_at = datetime.now()
        job.progress = 5
        session.commit()

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{quarter}-", dir=str(settings.snapshot_dir))
    )
    final = settings.snapshot_dir / f"{quarter}-v{snapshot.version}"
    try:
        dataset_path = temporary / "appeals"
        dataset_path.mkdir(parents=True, exist_ok=True)
        deduplication = cluster_near_duplicates(session, quarter)
        session.commit()
        row_count, canonical_count, raw_bytes = _write_parquet_dataset(
            session, quarter, dataset_path, job
        )
        duckdb_validation = validate_snapshot_dataset(
            dataset_path,
            expected_rows=row_count,
            expected_canonical_rows=canonical_count,
        )
        aggregate_count = build_city_aggregates(session, snapshot, job)
        search_path = temporary / "search"
        search_manifest = build_search_index(session, quarter, search_path, job=job)
        manifest = {
            "quarter": quarter,
            "version": snapshot.version,
            "taxonomy_version": taxonomy.version,
            "created_at": datetime.now().isoformat(),
            "row_count": row_count,
            "canonical_count": canonical_count,
            "deduplication": deduplication,
            "aggregate_count": aggregate_count,
            "duckdb_validation": duckdb_validation,
            "search": search_manifest,
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, final)
        session.execute(
            update(QuarterSnapshot)
            .where(
                QuarterSnapshot.quarter == quarter,
                QuarterSnapshot.status == "active",
                QuarterSnapshot.id != snapshot.id,
            )
            .values(status="superseded")
        )
        snapshot.status = "active"
        snapshot.parquet_path = str(final / "appeals")
        snapshot.search_index_path = str(final / "search")
        snapshot.row_count = row_count
        snapshot.canonical_count = canonical_count
        snapshot.raw_bytes = raw_bytes
        snapshot.manifest = manifest
        snapshot.activated_at = datetime.now()
        if job:
            job.progress = 90
            job.processed_count = row_count
            job.result = {"snapshot_id": snapshot.id, **manifest}
        session.commit()
        clear_dashboard_cache()
        return snapshot
    except Exception as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        snapshot.status = "failed"
        snapshot.manifest = {"error": str(exc)}
        if job:
            job.status = "failed"
            job.message = str(exc)[:2000]
            job.finished_at = datetime.now()
        session.commit()
        raise
