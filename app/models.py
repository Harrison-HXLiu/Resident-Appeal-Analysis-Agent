from __future__ import annotations

from datetime import datetime
from typing import Optional
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid_str() -> str:
    return str(uuid.uuid4())


class Region(Base):
    __tablename__ = "regions"
    __table_args__ = (UniqueConstraint("province", "city", "district", name="uq_region_area"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    province: Mapped[str] = mapped_column(String(60), index=True)
    city: Mapped[str] = mapped_column(String(60), index=True)
    district: Mapped[str] = mapped_column(String(60), default="")
    province_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    city_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    district_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    prefecture_city: Mapped[str] = mapped_column(String(60), default="", index=True)
    macro_region: Mapped[str] = mapped_column(String(20), default="未知", index=True)
    city_tier: Mapped[str] = mapped_column(String(30), default="普通地级市", index=True)
    urban_rural: Mapped[str] = mapped_column(String(20), default="未知", index=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    appeals: Mapped[list["Appeal"]] = relationship(back_populates="region")


class SourcePlatform(Base):
    __tablename__ = "source_platforms"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    source_url: Mapped[str] = mapped_column(Text, default="")
    region_id: Mapped[Optional[int]] = mapped_column(ForeignKey("regions.id"), nullable=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    mapping_status: Mapped[str] = mapped_column(String(30), default="confirmed")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    region: Mapped[Optional[Region]] = relationship()


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), index=True)
    source_platform_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("source_platforms.id"), nullable=True, index=True
    )
    source_filename: Mapped[str] = mapped_column(String(255))
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    archived_path: Mapped[str] = mapped_column(Text, default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    schema_report: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="completed")

    region: Mapped[Region] = relationship()
    source_platform: Mapped[Optional[SourcePlatform]] = relationship()
    appeals: Mapped[list["Appeal"]] = relationship(back_populates="import_batch")


class Appeal(Base):
    __tablename__ = "appeals"
    __table_args__ = (
        UniqueConstraint("region_id", "external_id", name="uq_appeal_region_external_id"),
        Index("ix_appeal_region_received", "region_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), index=True)
    import_batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("import_batches.id"))
    source_platform_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("source_platforms.id"), nullable=True, index=True
    )
    external_id: Mapped[str] = mapped_column(String(120))
    received_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    replied_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    appeal_type: Mapped[str] = mapped_column(String(80), index=True)
    appeal_type_raw: Mapped[str] = mapped_column(String(120), default="")
    quarter: Mapped[str] = mapped_column(String(8), default="", index=True)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    department: Mapped[str] = mapped_column(String(160), index=True)
    reply_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    redacted_title: Mapped[str] = mapped_column(Text)
    redacted_content: Mapped[str] = mapped_column(Text)
    redacted_reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    duplicate_group_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    canonical_appeal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("appeals.id"), nullable=True, index=True
    )
    is_canonical: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    region: Mapped[Region] = relationship(back_populates="appeals")
    import_batch: Mapped[Optional[ImportBatch]] = relationship(back_populates="appeals")
    source_platform: Mapped[Optional[SourcePlatform]] = relationship()
    annotation: Mapped[Optional["AppealAnnotation"]] = relationship(
        back_populates="appeal", uselist=False, cascade="all, delete-orphan"
    )


class AppealAnnotation(Base):
    __tablename__ = "appeal_annotations"

    id: Mapped[int] = mapped_column(primary_key=True)
    appeal_id: Mapped[int] = mapped_column(ForeignKey("appeals.id"), unique=True, index=True)
    taxonomy_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("taxonomy_versions.id"), nullable=True, index=True
    )
    topic: Mapped[str] = mapped_column(String(80), index=True)
    subtopic: Mapped[str] = mapped_column(String(120), default="")
    auxiliary_labels: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    keywords: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    urgency: Mapped[str] = mapped_column(String(30), default="一般")
    source: Mapped[str] = mapped_column(String(30), default="rule")
    model_name: Mapped[str] = mapped_column(String(80), default="rule-v1")
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    appeal: Mapped[Appeal] = relationship(back_populates="annotation")
    taxonomy_version: Mapped[Optional["TaxonomyVersion"]] = relationship()


class ReplyQuality(Base):
    __tablename__ = "reply_quality"

    id: Mapped[int] = mapped_column(primary_key=True)
    appeal_id: Mapped[int] = mapped_column(ForeignKey("appeals.id"), unique=True, index=True)
    addresses_issue: Mapped[str] = mapped_column(String(20), default="unknown")
    explains_basis: Mapped[str] = mapped_column(String(20), default="unknown")
    provides_action: Mapped[str] = mapped_column(String(20), default="unknown")
    gives_timeline_owner: Mapped[str] = mapped_column(String(20), default="unknown")
    provides_followup: Mapped[str] = mapped_column(String(20), default="unknown")
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True, index=True)
    evidence: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(30), default="transparent-rule-v1")
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    appeal: Mapped[Appeal] = relationship()


class TaxonomyVersion(Base):
    __tablename__ = "taxonomy_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    l1_macro_f1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    l2_macro_f1: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gold_sample_size: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    labels: Mapped[list["TaxonomyLabel"]] = relationship(
        back_populates="taxonomy_version", cascade="all, delete-orphan"
    )


class TaxonomyLabel(Base):
    __tablename__ = "taxonomy_labels"
    __table_args__ = (
        UniqueConstraint("taxonomy_version_id", "level", "name", name="uq_taxonomy_label_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    taxonomy_version_id: Mapped[int] = mapped_column(
        ForeignKey("taxonomy_versions.id"), index=True
    )
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("taxonomy_labels.id"), nullable=True, index=True
    )
    level: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120), index=True)
    definition: Mapped[str] = mapped_column(Text, default="")
    include_examples: Mapped[list[str]] = mapped_column(JSON, default=list)
    exclude_examples: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    taxonomy_version: Mapped[TaxonomyVersion] = relationship(back_populates="labels")


class GoldSample(Base):
    __tablename__ = "gold_samples"
    __table_args__ = (
        UniqueConstraint(
            "taxonomy_version_id",
            "appeal_id",
            name="uq_gold_sample_taxonomy_appeal",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    taxonomy_version_id: Mapped[int] = mapped_column(
        ForeignKey("taxonomy_versions.id"), index=True
    )
    appeal_id: Mapped[int] = mapped_column(ForeignKey("appeals.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    final_l1_label_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("taxonomy_labels.id"), nullable=True
    )
    final_l2_label_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("taxonomy_labels.id"), nullable=True
    )
    finalized_by: Mapped[str] = mapped_column(String(120), default="")
    finalized_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    taxonomy_version: Mapped[TaxonomyVersion] = relationship()
    appeal: Mapped[Appeal] = relationship()
    final_l1_label: Mapped[Optional[TaxonomyLabel]] = relationship(
        foreign_keys=[final_l1_label_id]
    )
    final_l2_label: Mapped[Optional[TaxonomyLabel]] = relationship(
        foreign_keys=[final_l2_label_id]
    )
    annotations: Mapped[list["GoldAnnotation"]] = relationship(
        back_populates="sample",
        cascade="all, delete-orphan",
    )


class GoldAnnotation(Base):
    __tablename__ = "gold_annotations"
    __table_args__ = (
        UniqueConstraint(
            "sample_id",
            "annotator_key",
            "role",
            name="uq_gold_annotation_sample_annotator_role",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey("gold_samples.id"), index=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True
    )
    annotator_key: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(30), default="annotator")
    l1_label_id: Mapped[int] = mapped_column(ForeignKey("taxonomy_labels.id"))
    l2_label_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("taxonomy_labels.id"), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    sample: Mapped[GoldSample] = relationship(back_populates="annotations")
    user: Mapped[Optional["User"]] = relationship()
    l1_label: Mapped[TaxonomyLabel] = relationship(foreign_keys=[l1_label_id])
    l2_label: Mapped[Optional[TaxonomyLabel]] = relationship(
        foreign_keys=[l2_label_id]
    )


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    period_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    generated_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    region: Mapped[Region] = relationship()


class QuarterSnapshot(Base):
    __tablename__ = "quarter_snapshots"
    __table_args__ = (
        UniqueConstraint("quarter", "version", name="uq_quarter_snapshot_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    quarter: Mapped[str] = mapped_column(String(8), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    taxonomy_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("taxonomy_versions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), default="building", index=True)
    parquet_path: Mapped[str] = mapped_column(Text, default="")
    search_index_path: Mapped[str] = mapped_column(Text, default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    canonical_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_bytes: Mapped[int] = mapped_column(Integer, default=0)
    manifest: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    taxonomy_version: Mapped[Optional[TaxonomyVersion]] = relationship()


class CityQuarterAggregate(Base):
    __tablename__ = "city_quarter_aggregates"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "city_code",
            "topic_l1",
            "appeal_type",
            name="uq_city_quarter_aggregate_slice",
        ),
        Index("ix_city_quarter_lookup", "quarter", "city_code", "topic_l1", "appeal_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("quarter_snapshots.id"), nullable=True, index=True
    )
    quarter: Mapped[str] = mapped_column(String(8), index=True)
    province: Mapped[str] = mapped_column(String(60), default="")
    city: Mapped[str] = mapped_column(String(60), index=True)
    city_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    topic_l1: Mapped[str] = mapped_column(String(80), default="", index=True)
    appeal_type: Mapped[str] = mapped_column(String(80), default="", index=True)
    raw_count: Mapped[int] = mapped_column(Integer, default=0)
    canonical_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_rate: Mapped[float] = mapped_column(Float, default=0)
    responded_count: Mapped[int] = mapped_column(Integer, default=0)
    response_rate: Mapped[float] = mapped_column(Float, default=0)
    average_response_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reply_quality_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    top_topics: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    snapshot: Mapped[Optional[QuarterSnapshot]] = relationship()


class PolicyDocument(Base):
    __tablename__ = "policy_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    issuing_authority: Mapped[str] = mapped_column(String(160), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    applicable_region: Mapped[str] = mapped_column(String(80), default="全国")
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    effective_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    archived_path: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ReportDocument(Base):
    __tablename__ = "report_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, default=_uuid_str)
    report_type: Mapped[str] = mapped_column(String(30), index=True)
    mode: Mapped[str] = mapped_column(String(20), default="standard")
    title: Mapped[str] = mapped_column(String(255))
    quarter: Mapped[str] = mapped_column(String(8), index=True)
    city_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    city: Mapped[str] = mapped_column(String(60), default="")
    snapshot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("quarter_snapshots.id"), nullable=True, index=True
    )
    taxonomy_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("taxonomy_versions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    fact_pack: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    current_content: Mapped[str] = mapped_column(Text, default="")
    generated_by: Mapped[str] = mapped_column(String(120), default="local-template")
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    published_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    revisions: Mapped[list["ReportRevision"]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )


class ReportRevision(Base):
    __tablename__ = "report_revisions"
    __table_args__ = (
        UniqueConstraint("report_id", "version", name="uq_report_revision_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("report_documents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    change_note: Mapped[str] = mapped_column(String(255), default="")
    editor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    report: Mapped[ReportDocument] = relationship(back_populates="revisions")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, default=_uuid_str)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="分析问答")
    context: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class AppealChunk(Base):
    __tablename__ = "appeal_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    appeal_id: Mapped[int] = mapped_column(ForeignKey("appeals.id"), unique=True, index=True)
    search_text: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    content_excerpt: Mapped[str] = mapped_column(Text)
    reply_excerpt: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    appeal: Mapped[Appeal] = relationship()


class RetrievalLog(Base):
    __tablename__ = "retrieval_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    city: Mapped[str] = mapped_column(String(60), default="")
    start_date: Mapped[str] = mapped_column(String(20), default="")
    end_date: Mapped[str] = mapped_column(String(20), default="")
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding_candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RagAnswerSource(Base):
    __tablename__ = "rag_answer_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    retrieval_log_id: Mapped[int] = mapped_column(ForeignKey("retrieval_logs.id"), index=True)
    appeal_id: Mapped[int] = mapped_column(ForeignKey("appeals.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float, default=0)
    reason: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    appeal: Mapped[Appeal] = relationship()
    retrieval_log: Mapped[RetrievalLog] = relationship()


class AppealEmbedding(Base):
    __tablename__ = "appeal_embeddings"
    __table_args__ = (UniqueConstraint("chunk_id", "model_name", name="uq_embedding_chunk_model"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chunk_id: Mapped[int] = mapped_column(ForeignKey("appeal_chunks.id"), index=True)
    appeal_id: Mapped[int] = mapped_column(ForeignKey("appeals.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(120), index=True)
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    vector_dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    chunk: Mapped[AppealChunk] = relationship()
    appeal: Mapped[Appeal] = relationship()


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, default=_uuid_str)
    job_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    region_id: Mapped[Optional[int]] = mapped_column(ForeignKey("regions.id"), nullable=True)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(30), default="researcher", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    user: Mapped[User] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    resource_type: Mapped[str] = mapped_column(String(80), default="")
    resource_id: Mapped[str] = mapped_column(String(80), default="")
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class ModelInvocation(Base):
    __tablename__ = "model_invocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(80))
    model_name: Mapped[str] = mapped_column(String(120))
    purpose: Mapped[str] = mapped_column(String(80), index=True)
    prompt_version: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(30), default="completed")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    estimated_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
