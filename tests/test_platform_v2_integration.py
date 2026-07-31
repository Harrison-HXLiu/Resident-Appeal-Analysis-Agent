from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from docx import Document
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    Appeal,
    AppealAnnotation,
    ChatSession,
    Region,
    ReplyQuality,
)
from app.services.analytics import map_overview
from app.services.chat import cleanup_expired_sessions, create_chat_session
from app.services.duckdb_analytics import aggregate_snapshot
from app.services.report_documents import (
    create_policy,
    create_report_document,
    export_report_docx,
    export_report_pdf,
    pregenerate_standard_reports,
    publish_report,
    validate_report,
)
from app.services.search_index import search_index
from app.services.snapshots import build_quarter_snapshot
from app.services.taxonomy import ensure_draft_taxonomy


def _seed_quarter(session: Session) -> None:
    taxonomy = ensure_draft_taxonomy(session)
    region = Region(
        province="江苏省",
        city="苏州市",
        prefecture_city="苏州市",
        city_code="320500",
    )
    session.add(region)
    session.flush()
    rows = (
        ("A-1", "公交班次不足", "交通出行", "公交地铁", True),
        ("A-2", "公交班次不足", "交通出行", "公交地铁", False),
        ("A-3", "小区物业管理", "住房", "物业服务", True),
    )
    for index, (external_id, title, topic, subtopic, canonical) in enumerate(rows, start=1):
        appeal = Appeal(
            region_id=region.id,
            external_id=external_id,
            received_at=datetime(2025, 1, index),
            replied_at=datetime(2025, 1, index) + timedelta(hours=24),
            appeal_type="投诉",
            appeal_type_raw="投诉",
            quarter="2025-Q1",
            title=title,
            content=f"{title}，请及时处理。",
            department="测试部门",
            reply_content="已核查并将在7日内处理，如有疑问可联系后续渠道。",
            redacted_title=title,
            redacted_content=f"{title}，请及时处理。",
            redacted_reply="已核查并将在7日内处理，如有疑问可联系后续渠道。",
            content_hash=f"hash-{index}",
            duplicate_group_key="same" if index < 3 else "different",
            is_canonical=canonical,
        )
        session.add(appeal)
        session.flush()
        session.add(
            AppealAnnotation(
                appeal_id=appeal.id,
                taxonomy_version_id=taxonomy.id,
                topic=topic,
                subtopic=subtopic,
                keywords=title,
                summary=title,
                confidence=0.9,
            )
        )
        session.add(
            ReplyQuality(
                appeal_id=appeal.id,
                addresses_issue="yes",
                explains_basis="not_applicable",
                provides_action="yes",
                gives_timeline_owner="yes",
                provides_followup="yes",
                score=100,
                evidence={},
            )
        )
    session.commit()


def test_snapshot_search_map_and_atomic_activation(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'platform.sqlite3'}")
    Base.metadata.create_all(engine)
    snapshot_settings = SimpleNamespace(snapshot_dir=tmp_path / "snapshots")
    snapshot_settings.snapshot_dir.mkdir()
    monkeypatch.setattr("app.services.snapshots.get_settings", lambda: snapshot_settings)

    with Session(engine) as session:
        _seed_quarter(session)
        first = build_quarter_snapshot(session, "2025-Q1")
        second = build_quarter_snapshot(session, "2025-Q1")
        session.refresh(first)
        overview = map_overview(session, quarter="2025-Q1")
        breakdown = aggregate_snapshot(
            second.parquet_path,
            dimension="topic_l1",
            filters={"city_code": "320500"},
        )
        hits = search_index(
            second.search_index_path,
            "公交 班次",
            city="苏州市",
            topic="交通出行",
        )

    assert first.status == "superseded"
    assert second.status == "active"
    assert second.row_count == 3
    assert second.canonical_count == 2
    assert second.manifest["duckdb_validation"]["row_count"] == 3
    assert breakdown[0] == {"name": "交通出行", "event_count": 1}
    assert hits
    assert len(overview["cities"]) == 1
    assert overview["unmapped_count"] == 1
    assert "city=" in overview["cities"][0]["report_url"]


def test_report_fact_lock_publish_and_word_export(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'report.sqlite3'}")
    Base.metadata.create_all(engine)
    snapshot_settings = SimpleNamespace(snapshot_dir=tmp_path / "snapshots")
    snapshot_settings.snapshot_dir.mkdir()
    monkeypatch.setattr("app.services.snapshots.get_settings", lambda: snapshot_settings)
    monkeypatch.setattr(
        "app.services.report_documents.get_settings",
        lambda: SimpleNamespace(export_dir=tmp_path),
    )

    with Session(engine) as session:
        _seed_quarter(session)
        snapshot = build_quarter_snapshot(session, "2025-Q1")
        pregenerated = pregenerate_standard_reports(session, snapshot)
        assert pregenerated == {"created": 2, "skipped": 0, "total": 2}
        assert pregenerate_standard_reports(session, snapshot) == {
            "created": 0,
            "skipped": 2,
            "total": 2,
        }
        policy = create_policy(
            session,
            title="测试政策",
            issuing_authority="测试机关",
            content="应及时回应群众诉求并明确措施、责任和时限。",
        )
        report = create_report_document(
            session,
            {
                "report_type": "national",
                "quarter": "2025-Q1",
                "policy_ids": [policy.id],
            },
        )
        assert validate_report(report.current_content, report.fact_pack) == []
        invalid = validate_report(
            report.current_content + "\n未经事实包支持的数据为9999件。[政策:999]",
            report.fact_pack,
        )
        assert any("未支持的数字" in item for item in invalid)
        assert any("事实包之外的政策" in item for item in invalid)
        publish_report(session, report)
        exported = export_report_docx(report)
        exported_pdf = export_report_pdf(report)

    text = "\n".join(paragraph.text for paragraph in Document(exported).paragraphs)
    assert report.status == "published"
    assert "2件" in text
    assert "[政策:" in text
    assert exported_pdf.read_bytes().startswith(b"%PDF-")


def test_expired_chat_sessions_are_deleted(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'chat.sqlite3'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        "app.services.chat.get_settings",
        lambda: SimpleNamespace(session_timeout_minutes=30),
    )
    with Session(engine) as session:
        active = create_chat_session(session)
        expired = ChatSession(
            title="expired",
            expires_at=datetime.now() - timedelta(minutes=1),
        )
        session.add(expired)
        session.commit()
        expired_public_id = expired.public_id
        assert cleanup_expired_sessions(session) == 1
        assert session.scalar(
            select(ChatSession).where(ChatSession.public_id == active.public_id)
        )
        assert session.scalar(
            select(ChatSession).where(ChatSession.public_id == expired_public_id)
        ) is None
