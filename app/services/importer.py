from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Appeal, AppealAnnotation, ImportBatch, Region, SourcePlatform
from app.services.classification import classify_by_rule
from app.services.dedup import duplicate_group_key, exact_content_hash
from app.services.privacy import redact_text
from app.services.rag import upsert_chunk_for_appeal
from app.services.reply_quality import upsert_reply_quality
from app.services.taxonomy import active_taxonomy


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "received_at": (
        "来件时间",
        "留言时间",
        "诉求时间",
        "来信时间",
        "来信（电）时间",
        "来信日期",
        "信访日期",
        "来电时间",
        "写信时间",
        "递交时间",
        "受理时间",
        "案件时间",
        "接收时间",
        "发起时间",
        "发表时间",
        "发布日期",
        "时间",
        "受理日期",
        "提交时间",
        "反映时间",
        "咨询时间",
        "发布时间",
        "日期",
    ),
    "replied_at": ("回复时间", "答复时间", "办理时间", "反馈时间"),
    "appeal_type": (
        "来件类型",
        "留言类型",
        "问题类型",
        "信件类型",
        "来信目的",
        "诉求类型",
        "类别",
    ),
    "title": (
        "来件标题",
        "留言标题",
        "信件标题",
        "来信标题",
        "来信（电）标题",
        "诉求标题",
        "咨询标题",
        "咨询主题",
        "来信主题",
        "信件名称",
        "案件标题",
        "办件标题",
        "办件名称",
        "工单标题",
        "受理标题",
        "问题摘要",
        "标 题",
        "主 题",
        "标题",
        "反映问题",
        "主题",
    ),
    "content": (
        "来件内容",
        "留言内容",
        "信件内容",
        "内容描述",
        "事项内容",
        "发言内容",
        "公开信访内容",
        "来信（电）内容",
        "来信内容",
        "来电内容",
        "诉求描述",
        "具体内容",
        "网民提问",
        "网友留言",
        "信件信息",
        "内容详情",
        "问题描述",
        "诉求问题",
        "留言摘要",
        "主要诉求",
        "咨询内容",
        "工单内容",
        "办件内容",
        "内 容",
        "提问",
        "问题",
        "留言",
        "内容",
        "诉求内容",
    ),
    "department": (
        "回复部门",
        "承办单位",
        "答复单位",
        "回复单位",
        "办理部门",
        "处理部门",
        "办理机构",
        "回复机构",
        "答复部门",
        "处办单位",
        "处理对象",
        "所属部门",
        "受理单位",
        "责任单位",
    ),
    "reply_content": (
        "回复内容",
        "答复意见",
        "答复情况",
        "回复",
        "处理结果",
        "办理结果",
        "回复详情",
        "回复意见",
        "最终回复意见",
        "处理意见",
        "处理描述",
        "处理情况",
        "公示内容",
        "答复内容",
        "部门答复",
        "处办回复",
        "回复告知内容",
        "诉求回复",
        "咨询回复",
        "回答",
    ),
    "external_id": (
        "信件编号",
        "信访件编号",
        "诉求编号",
        "办件编号",
        "信件索引号",
        "业务编号",
        "编号",
        "查询编号",
        "受理编号",
        "流水号",
        "信件ID",
    ),
}

REQUIRED_FIELDS = ("received_at", "content")

CTYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("投诉", ("投诉", "举报", "不满", "反映")),
    ("咨询", ("咨询", "询问", "政策", "办事")),
    ("建议", ("建议", "建言", "意见")),
    ("求助", ("求助", "请求", "困难")),
    ("表扬", ("表扬", "感谢")),
)


@dataclass(frozen=True)
class ImportResult:
    batch_id: int
    rows: int
    inserted: int
    updated: int
    failed: int = 0
    skipped: bool = False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def quarter_of(value) -> str:
    return f"{value.year}-Q{(value.month - 1) // 3 + 1}"


def normalize_ctype(raw: str) -> str:
    text = raw.strip()
    if not text:
        return "其他"
    for normalized, terms in CTYPE_RULES:
        if any(term in text for term in terms):
            return normalized
    return "其他"


def get_or_create_region(
    session: Session,
    province: str,
    city: str,
    district: str = "",
    *,
    province_code: str = "",
    city_code: str = "",
    district_code: str = "",
) -> Region:
    region = session.scalar(
        select(Region).where(
            Region.province == province, Region.city == city, Region.district == district
        )
    )
    if region:
        if province_code:
            region.province_code = province_code
        if city_code:
            region.city_code = city_code
        if district_code:
            region.district_code = district_code
        if not region.prefecture_city:
            region.prefecture_city = city
        if city == "苏州市" and region.longitude is None:
            region.longitude, region.latitude = 120.5853, 31.2989
        return region
    coordinates = (120.5853, 31.2989) if city == "苏州市" else (None, None)
    region = Region(
        province=province,
        city=city,
        district=district,
        province_code=province_code,
        city_code=city_code,
        district_code=district_code,
        prefecture_city=city,
        longitude=coordinates[0],
        latitude=coordinates[1],
    )
    session.add(region)
    session.flush()
    return region


def get_or_create_platform(
    session: Session,
    region: Region,
    code: str,
    name: str,
    source_url: str = "",
) -> SourcePlatform | None:
    code = code.strip()
    if not code:
        return None
    platform = session.scalar(select(SourcePlatform).where(SourcePlatform.code == code))
    if platform:
        platform.region_id = region.id
        if name:
            platform.name = name
        if source_url:
            platform.source_url = source_url
        return platform
    platform = SourcePlatform(
        code=code,
        name=name.strip() or code,
        source_url=source_url.strip(),
        region_id=region.id,
    )
    session.add(platform)
    session.flush()
    return platform


def _datetime_or_none(value: object):
    if value is None or pd.isna(value):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime()


def _text(value: object) -> str:
    return "" if value is None or pd.isna(value) else str(value).strip()


def _resolve_columns(columns: list[object]) -> dict[str, str]:
    normalized = {str(column).strip(): str(column) for column in columns}
    resolved: dict[str, str] = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                resolved[field] = normalized[alias]
                break
    return resolved


def _row_value(row: pd.Series, mapping: dict[str, str], field: str) -> str:
    column = mapping.get(field)
    return _text(row.get(column)) if column else ""


def _external_id(
    row: pd.Series,
    mapping: dict[str, str],
    platform_code: str,
) -> str:
    source_id = _row_value(row, mapping, "external_id")
    if source_id:
        return f"{platform_code}:{source_id}" if platform_code else source_id
    raw = "|".join(
        _row_value(row, mapping, field)
        for field in ("received_at", "title", "content", "department", "reply_content")
    )
    generated = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return f"{platform_code}:generated-{generated}" if platform_code else f"generated-{generated}"


def _read_dataframe(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix == ".parquet":
        return pd.read_parquet(path)
    raise ValueError("仅支持 .xlsx、.xls、.csv 和 .parquet 文件")


def inspect_source(path: Path) -> dict[str, object]:
    dataframe = _read_dataframe(path)
    mapping = _resolve_columns(list(dataframe.columns))
    missing = [field for field in REQUIRED_FIELDS if field not in mapping]
    return {
        "filename": path.name,
        "rows": len(dataframe),
        "columns": [str(column) for column in dataframe.columns],
        "mapping": mapping,
        "missing_required": missing,
    }


def _archive_source(path: Path, source_hash: str) -> Path:
    settings = get_settings()
    destination = settings.archive_dir / source_hash[:2] / f"{source_hash}{path.suffix.lower()}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(path, destination)
    return destination


def _set_rule_annotation(
    appeal: Appeal,
    taxonomy_version_id: int | None = None,
) -> None:
    if appeal.annotation and appeal.annotation.source != "rule":
        return
    result = classify_by_rule(appeal.redacted_title, appeal.redacted_content)
    annotation = appeal.annotation
    if annotation is None:
        annotation = AppealAnnotation()
        appeal.annotation = annotation
    annotation.taxonomy_version_id = taxonomy_version_id
    annotation.topic = result.topic
    annotation.subtopic = result.subtopic
    annotation.auxiliary_labels = [
        {"level": 1, "name": name, "source": "rule"} for name in result.auxiliary_topics
    ]
    annotation.keywords = result.keywords
    annotation.summary = result.summary
    annotation.urgency = result.urgency
    annotation.source = "rule"
    annotation.model_name = "rule-v2-taxonomy-draft"
    annotation.confidence = result.confidence


def backfill_rule_annotations(session: Session, region_id: int | None = None) -> int:
    taxonomy = active_taxonomy(session)
    statement = select(Appeal).outerjoin(AppealAnnotation).where(AppealAnnotation.id.is_(None))
    if region_id is not None:
        statement = statement.where(Appeal.region_id == region_id)
    appeals = list(session.scalars(statement).all())
    for appeal in appeals:
        _set_rule_annotation(appeal, taxonomy.id)
    if appeals:
        session.commit()
    return len(appeals)


def import_excel(
    session: Session,
    path: Path,
    province: str,
    city: str,
    district: str = "",
    *,
    source_platform_code: str = "",
    source_platform_name: str = "",
    source_url: str = "",
    province_code: str = "",
    city_code: str = "",
    district_code: str = "",
) -> ImportResult:
    """Compatibility entry point; now accepts Excel, CSV and Parquet."""
    dataframe = _read_dataframe(path)
    mapping = _resolve_columns(list(dataframe.columns))
    missing = [field for field in REQUIRED_FIELDS if field not in mapping]
    if missing:
        readable = "、".join(missing)
        raise ValueError(f"文件缺少可映射的必需字段：{readable}")

    region = get_or_create_region(
        session,
        province.strip(),
        city.strip(),
        district.strip(),
        province_code=province_code.strip(),
        city_code=city_code.strip(),
        district_code=district_code.strip(),
    )
    platform = get_or_create_platform(
        session,
        region,
        source_platform_code,
        source_platform_name or path.stem,
        source_url,
    )
    taxonomy = active_taxonomy(session)
    source_hash = file_sha256(path)
    previous = session.scalar(
        select(ImportBatch).where(
            ImportBatch.region_id == region.id,
            ImportBatch.source_hash == source_hash,
            ImportBatch.source_platform_id == (platform.id if platform else None),
        )
    )
    if previous:
        backfill_rule_annotations(session, region.id)
        return ImportResult(
            previous.id,
            previous.row_count,
            0,
            0,
            previous.error_count,
            skipped=True,
        )

    archived = _archive_source(path, source_hash)
    batch = ImportBatch(
        region_id=region.id,
        source_platform_id=platform.id if platform else None,
        source_filename=path.name,
        source_hash=source_hash,
        archived_path=str(archived),
        row_count=len(dataframe),
        status="processing",
        schema_report={
            "columns": [str(column) for column in dataframe.columns],
            "mapping": mapping,
            "missing_optional": [
                field for field in FIELD_ALIASES if field not in mapping and field not in REQUIRED_FIELDS
            ],
        },
    )
    session.add(batch)
    session.flush()

    existing_by_id = {
        item.external_id: item
        for item in session.scalars(select(Appeal).where(Appeal.region_id == region.id)).all()
    }
    canonical_by_hash: dict[str, Appeal] = {}
    for item in session.scalars(
        select(Appeal)
        .join(Region)
        .where(
            Region.province == region.province,
            Region.city == region.city,
            Appeal.is_canonical.is_(True),
            Appeal.content_hash != "",
        )
    ).all():
        canonical_by_hash.setdefault(item.content_hash, item)

    inserted = 0
    updated = 0
    failed = 0
    errors: list[dict[str, object]] = []
    platform_code = platform.code if platform else ""

    for row_number, (_, row) in enumerate(dataframe.iterrows(), start=2):
        external_id = _external_id(row, mapping, platform_code)
        received_at = _datetime_or_none(row.get(mapping["received_at"]))
        content = _row_value(row, mapping, "content")
        title = _row_value(row, mapping, "title") or content[:80]
        if received_at is None or not content:
            failed += 1
            if len(errors) < 100:
                errors.append(
                    {
                        "row": row_number,
                        "external_id": external_id,
                        "reason": "来件时间无效或标题正文均为空",
                    }
                )
            continue

        appeal = existing_by_id.get(external_id)
        if appeal is None:
            appeal = Appeal(region_id=region.id, external_id=external_id)
            session.add(appeal)
            existing_by_id[external_id] = appeal
            inserted += 1
        else:
            updated += 1

        raw_type = _row_value(row, mapping, "appeal_type")
        appeal.import_batch_id = batch.id
        appeal.source_platform_id = platform.id if platform else None
        appeal.received_at = received_at
        appeal.replied_at = _datetime_or_none(
            row.get(mapping["replied_at"]) if "replied_at" in mapping else None
        )
        appeal.appeal_type_raw = raw_type
        appeal.appeal_type = normalize_ctype(raw_type)
        appeal.quarter = quarter_of(received_at)
        appeal.title = title
        appeal.content = content
        appeal.department = _row_value(row, mapping, "department")
        appeal.reply_content = _row_value(row, mapping, "reply_content") or None
        appeal.redacted_title = redact_text(appeal.title)
        appeal.redacted_content = redact_text(appeal.content)
        appeal.redacted_reply = redact_text(appeal.reply_content) or None
        appeal.content_hash = exact_content_hash(appeal.title, appeal.content)
        appeal.duplicate_group_key = duplicate_group_key(
            region.city_code or region.city,
            received_at,
            appeal.title,
            appeal.content,
        )
        canonical = canonical_by_hash.get(appeal.content_hash)
        if canonical is not None and canonical is not appeal:
            appeal.is_canonical = False
            appeal.canonical_appeal_id = canonical.id
        else:
            appeal.is_canonical = True
            appeal.canonical_appeal_id = None
            canonical_by_hash[appeal.content_hash] = appeal
        _set_rule_annotation(appeal, taxonomy.id)
        session.flush()
        upsert_reply_quality(session, appeal)
        upsert_chunk_for_appeal(session, appeal)

    if not inserted and not updated and failed:
        session.rollback()
        raise ValueError("文件没有可导入的有效记录")

    batch.inserted_count = inserted
    batch.updated_count = updated
    batch.error_count = failed
    batch.status = "completed_with_errors" if failed else "completed"
    batch.schema_report = {**batch.schema_report, "errors": errors}
    session.commit()
    return ImportResult(batch.id, len(dataframe), inserted, updated, failed)
