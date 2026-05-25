from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Region(Base):
    __tablename__ = "regions"
    __table_args__ = (UniqueConstraint("province", "city", "district", name="uq_region_area"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    province: Mapped[str] = mapped_column(String(60), index=True)
    city: Mapped[str] = mapped_column(String(60), index=True)
    district: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    appeals: Mapped[list["Appeal"]] = relationship(back_populates="region")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), index=True)
    source_filename: Mapped[str] = mapped_column(String(255))
    source_hash: Mapped[str] = mapped_column(String(64), index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="completed")

    region: Mapped[Region] = relationship()
    appeals: Mapped[list["Appeal"]] = relationship(back_populates="import_batch")


class Appeal(Base):
    __tablename__ = "appeals"
    __table_args__ = (
        UniqueConstraint("region_id", "external_id", name="uq_appeal_region_external_id"),
        Index("ix_appeal_region_received", "region_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), index=True)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    external_id: Mapped[str] = mapped_column(String(120))
    received_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    appeal_type: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    department: Mapped[str] = mapped_column(String(160), index=True)
    reply_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    redacted_title: Mapped[str] = mapped_column(Text)
    redacted_content: Mapped[str] = mapped_column(Text)
    redacted_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    region: Mapped[Region] = relationship(back_populates="appeals")
    import_batch: Mapped[ImportBatch | None] = relationship(back_populates="appeals")
    annotation: Mapped["AppealAnnotation | None"] = relationship(
        back_populates="appeal", uselist=False, cascade="all, delete-orphan"
    )


class AppealAnnotation(Base):
    __tablename__ = "appeal_annotations"

    id: Mapped[int] = mapped_column(primary_key=True)
    appeal_id: Mapped[int] = mapped_column(ForeignKey("appeals.id"), unique=True, index=True)
    topic: Mapped[str] = mapped_column(String(80), index=True)
    subtopic: Mapped[str] = mapped_column(String(120), default="")
    keywords: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    urgency: Mapped[str] = mapped_column(String(30), default="一般")
    source: Mapped[str] = mapped_column(String(30), default="rule")
    model_name: Mapped[str] = mapped_column(String(80), default="rule-v1")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    appeal: Mapped[Appeal] = relationship(back_populates="annotation")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    period_start: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    generated_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    region: Mapped[Region] = relationship()


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), default="分析问答")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    region_id: Mapped[int | None] = mapped_column(ForeignKey("regions.id"), nullable=True)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

