from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Appeal, AppealAnnotation, ImportBatch, Region
from app.services.classification import classify_by_rule
from app.services.privacy import redact_text


REQUIRED_COLUMNS = ["来件时间", "回复时间", "来件类型", "来件标题", "来件内容", "回复部门", "回复内容"]
ID_COLUMN = "信件编号"


@dataclass(frozen=True)
class ImportResult:
    batch_id: int
    rows: int
    inserted: int
    updated: int
    skipped: bool = False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def get_or_create_region(session: Session, province: str, city: str, district: str = "") -> Region:
    region = session.scalar(
        select(Region).where(
            Region.province == province, Region.city == city, Region.district == district
        )
    )
    if region:
        return region
    region = Region(province=province, city=city, district=district)
    session.add(region)
    session.flush()
    return region


def _datetime_or_none(value: object) -> object | None:
    if value is None or pd.isna(value):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime()


def _text(value: object) -> str:
    return "" if value is None or pd.isna(value) else str(value).strip()


def _external_id(row: pd.Series) -> str:
    if ID_COLUMN in row and _text(row[ID_COLUMN]):
        return _text(row[ID_COLUMN])
    raw = "|".join(_text(row.get(column)) for column in REQUIRED_COLUMNS)
    return f"generated-{hashlib.sha1(raw.encode('utf-8')).hexdigest()}"


def _set_rule_annotation(appeal: Appeal) -> None:
    if appeal.annotation and appeal.annotation.source != "rule":
        return
    result = classify_by_rule(appeal.redacted_title, appeal.redacted_content)
    annotation = appeal.annotation
    if annotation is None:
        annotation = AppealAnnotation()
        appeal.annotation = annotation
    annotation.topic = result.topic
    annotation.keywords = result.keywords
    annotation.summary = result.summary
    annotation.urgency = result.urgency
    annotation.source = "rule"
    annotation.model_name = "rule-v1"
    annotation.confidence = result.confidence


def backfill_rule_annotations(session: Session, region_id: int | None = None) -> int:
    statement = select(Appeal).outerjoin(AppealAnnotation).where(AppealAnnotation.id.is_(None))
    if region_id is not None:
        statement = statement.where(Appeal.region_id == region_id)
    appeals = list(session.scalars(statement).all())
    for appeal in appeals:
        _set_rule_annotation(appeal)
    if appeals:
        session.commit()
    return len(appeals)


def import_excel(
    session: Session,
    path: Path,
    province: str,
    city: str,
    district: str = "",
) -> ImportResult:
    dataframe = pd.read_excel(path)
    missing = [column for column in REQUIRED_COLUMNS if column not in dataframe.columns]
    if missing:
        raise ValueError(f"文件缺少字段：{'、'.join(missing)}")

    region = get_or_create_region(session, province.strip(), city.strip(), district.strip())
    source_hash = file_sha256(path)
    previous = session.scalar(
        select(ImportBatch).where(
            ImportBatch.region_id == region.id, ImportBatch.source_hash == source_hash
        )
    )
    if previous:
        backfill_rule_annotations(session, region.id)
        return ImportResult(previous.id, previous.row_count, 0, 0, skipped=True)

    batch = ImportBatch(
        region_id=region.id,
        source_filename=path.name,
        source_hash=source_hash,
        row_count=len(dataframe),
    )
    session.add(batch)
    session.flush()

    existing = {
        item.external_id: item
        for item in session.scalars(select(Appeal).where(Appeal.region_id == region.id)).all()
    }
    inserted = 0
    updated = 0

    for _, row in dataframe.iterrows():
        external_id = _external_id(row)
        appeal = existing.get(external_id)
        if appeal is None:
            appeal = Appeal(region_id=region.id, external_id=external_id)
            session.add(appeal)
            existing[external_id] = appeal
            inserted += 1
        else:
            updated += 1

        appeal.import_batch_id = batch.id
        appeal.received_at = _datetime_or_none(row["来件时间"])
        if appeal.received_at is None:
            raise ValueError(f"来件时间无效，信件编号：{external_id}")
        appeal.replied_at = _datetime_or_none(row["回复时间"])
        appeal.appeal_type = _text(row["来件类型"])
        appeal.title = _text(row["来件标题"])
        appeal.content = _text(row["来件内容"])
        appeal.department = _text(row["回复部门"])
        appeal.reply_content = _text(row["回复内容"]) or None
        appeal.redacted_title = redact_text(appeal.title)
        appeal.redacted_content = redact_text(appeal.content)
        appeal.redacted_reply = redact_text(appeal.reply_content) or None
        _set_rule_annotation(appeal)

    batch.inserted_count = inserted
    batch.updated_count = updated
    session.commit()
    return ImportResult(batch.id, len(dataframe), inserted, updated)
