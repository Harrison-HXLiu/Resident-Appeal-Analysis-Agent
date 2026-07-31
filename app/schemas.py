from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ImportBatchRequest(BaseModel):
    path: str
    province: str
    city: str
    district: str = ""
    source_platform_code: str = ""
    source_platform_name: str = ""
    source_url: str = ""
    province_code: str = ""
    city_code: str = ""
    district_code: str = ""


class SnapshotRequest(BaseModel):
    quarter: str

    @field_validator("quarter")
    @classmethod
    def validate_quarter(cls, value: str) -> str:
        if len(value) != 7 or value[4:6].upper() != "-Q" or value[-1] not in "1234":
            raise ValueError("季度格式应为 YYYY-Q1 至 YYYY-Q4")
        int(value[:4])
        return value.upper()


class ReportCreateRequest(BaseModel):
    report_type: Literal["national", "city"]
    mode: Literal["standard", "ad_hoc"] = "standard"
    quarter: str
    city_code: str = ""
    city: str = ""
    topic_l1: str = ""
    appeal_type: str = ""
    policy_ids: list[int] = Field(default_factory=list)

    @field_validator("quarter")
    @classmethod
    def validate_quarter(cls, value: str) -> str:
        return SnapshotRequest(quarter=value).quarter

    @field_validator("city_code", "city")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class ReportUpdateRequest(BaseModel):
    content: str = Field(min_length=1)
    change_note: str = Field(default="", max_length=255)


class PolicyCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    issuing_authority: str = Field(default="", max_length=160)
    source_url: str = ""
    applicable_region: str = Field(default="全国", max_length=80)
    published_at: datetime | None = None
    effective_until: datetime | None = None
    content: str = ""


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=10, max_length=256)
    role: Literal["researcher", "reviewer", "admin"] = "researcher"


class RegionMetadataUpdate(BaseModel):
    province_code: str = Field(default="", max_length=12)
    city_code: str = Field(default="", max_length=12)
    district_code: str = Field(default="", max_length=12)
    prefecture_city: str = Field(default="", max_length=60)
    macro_region: Literal["东部", "中部", "西部", "东北", "未知"] = "未知"
    city_tier: str = Field(default="普通地级市", max_length=30)
    urban_rural: Literal["城镇", "乡村", "未知"] = "未知"
    longitude: float | None = Field(default=None, ge=73, le=136)
    latitude: float | None = Field(default=None, ge=3, le=54)


class TaxonomyLabelUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    definition: str = Field(min_length=1, max_length=4000)
    status: Literal["trial", "candidate", "approved", "rejected"] = "approved"
    include_examples: list[str] | None = Field(default=None, max_length=30)
    exclude_examples: list[str] | None = Field(default=None, max_length=30)


class GoldSampleCreateRequest(BaseModel):
    appeal_ids: list[int] = Field(min_length=1, max_length=2000)


class GoldAnnotationRequest(BaseModel):
    annotator_name: str = Field(default="", max_length=120)
    l1_label_id: int
    l2_label_id: int | None = None
    notes: str = Field(default="", max_length=4000)


class ChatSessionCreateRequest(BaseModel):
    city: str = ""
    quarter: str = ""
    topic_l1: str = ""
    appeal_type: str = ""


class QueryPlan(BaseModel):
    intent: Literal[
        "overview",
        "ranking",
        "trend",
        "comparison",
        "reply_quality",
        "case_search",
        "policy_search",
        "report_lookup",
        "unsupported",
    ] = "overview"
    city: str = ""
    compare_cities: list[str] = Field(default_factory=list, max_length=8)
    quarter: str = ""
    start: str = ""
    end: str = ""
    topic_l1: str = ""
    appeal_type: str = ""
    dimension: Literal["", "macro_region", "city_tier", "urban_rural", "province", "city"] = ""
    metric: Literal[
        "event_count",
        "raw_count",
        "response_rate",
        "response_hours",
        "reply_quality",
    ] = "event_count"
    needs_cases: bool = True
    needs_policies: bool = False


class ChatMessageRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    city: str | None = None
    quarter: str | None = None
    topic_l1: str | None = None
    appeal_type: str | None = None


class EvidenceItem(BaseModel):
    rank: int
    source_type: Literal["appeal", "policy", "report"]
    source_id: str
    title: str
    city: str = ""
    quarter: str = ""
    topic: str = ""
    department: str = ""
    excerpt: str = ""
    reply_excerpt: str = ""
    score: float = 0


class AppealLabel(BaseModel):
    taxonomy_version: str
    primary_l1: str
    primary_l2: str = ""
    auxiliary: list[dict[str, object]] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class ReportFactPack(BaseModel):
    schema_version: Literal["report-fact-pack-v1"]
    report_type: Literal["national", "city"]
    quarter: str
    scope: dict[str, object]
    snapshot: dict[str, object]
    taxonomy: dict[str, object]
    statistics: dict[str, object]
    previous_quarter_statistics: dict[str, object]
    cases: list[dict[str, object]] = Field(default_factory=list)
    policies: list[dict[str, object]] = Field(default_factory=list)
    comparisons: dict[str, object] | None = None
    consensus_suggestions: list[dict[str, object]] | None = None
    national_benchmark: dict[str, object] | None = None
    distinctive_topics: list[dict[str, object]] | None = None
    topic_changes: list[dict[str, object]] | None = None


class ReportVersion(BaseModel):
    report_id: str
    version: int = Field(ge=1)
    status: Literal["draft", "published"]
    content: str
    created_at: datetime


class JobResponse(BaseModel):
    id: str
    job_type: str
    status: str
    progress: int
    message: str = ""
    result: dict[str, object] = Field(default_factory=dict)
