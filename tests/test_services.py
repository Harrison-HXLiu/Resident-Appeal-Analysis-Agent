from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.services.rag as rag
from app.db import Base
from app.models import Appeal, AppealAnnotation, Region
from app.services.agent import _facts_as_text, infer_time_range_from_question
from app.services.analytics import dashboard_stats
from app.services.classification import classify_by_rule
from app.services.embeddings import bytes_to_vector, dot, normalize, vector_to_bytes
from app.services.importer import import_excel
from app.services.markdown import render_markdown
from app.services.privacy import redact_text


def test_redact_text_removes_common_identifiers() -> None:
    original = "联系人电话13812345678，身份证320501199001011234，邮箱test@example.com，地址：苏州市某小区12栋。"
    result = redact_text(original)
    assert "13812345678" not in result
    assert "320501199001011234" not in result
    assert "test@example.com" not in result
    assert "苏州市某小区12栋" not in result


def test_render_markdown_sanitizes_html() -> None:
    html = render_markdown("# 标题\n\n- 条目\n\n<script>alert(1)</script>")
    assert "<h1>标题</h1>" in html
    assert "<li>条目</li>" in html
    assert "<script>" not in html


def test_question_year_infers_time_range_and_facts_include_monthly_trend() -> None:
    assert infer_time_range_from_question("2025年诉求量变化趋势如何？") == (
        "2025-01-01",
        "2025-12-31",
    )
    assert infer_time_range_from_question("25年诉求量变化趋势如何？") == (
        "2025-01-01",
        "2025-12-31",
    )
    assert infer_time_range_from_question("2025年趋势", start="2025-03-01") == (
        "2025-03-01",
        None,
    )
    facts = _facts_as_text(
        {
            "total": 3,
            "responded": 2,
            "response_rate": 66.67,
            "average_response_hours": 12,
            "topics": [{"name": "交通出行", "count": 2}],
            "types": [{"name": "投诉", "count": 3}],
            "departments": [{"name": "交通局", "count": 2}],
            "topic_sources": {"rule": 3},
            "monthly": [{"month": "2025-01", "count": 1}, {"month": "2025-02", "count": 2}],
        },
        "苏州市，2025年",
    )
    assert "分月趋势" in facts
    assert "2025-01 1件" in facts


def test_rule_classification_finds_transport_topic() -> None:
    result = classify_by_rule("公交站点设置建议", "希望调整公交线路，缓解道路拥堵。")
    assert result.topic == "交通出行"
    assert "公交" in result.keywords


def test_embedding_vector_serialization_roundtrip() -> None:
    vector = normalize([3.0, 4.0])
    restored = bytes_to_vector(vector_to_bytes(vector))
    assert len(restored) == 2
    assert round(dot(restored, restored), 5) == 1.0


def test_import_and_statistics_are_idempotent(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    file_path = tmp_path / "sample.xlsx"
    pd.DataFrame(
        [
            {
                "来件时间": datetime(2025, 1, 1, 9, 0),
                "回复时间": datetime(2025, 1, 2, 9, 0),
                "来件类型": "投诉",
                "来件标题": "小区物业问题",
                "来件内容": "物业管理不到位，电话13812345678",
                "回复部门": "住建局",
                "回复内容": "已处理",
                "信件编号": "A001",
            },
            {
                "来件时间": datetime(2025, 1, 3, 9, 0),
                "回复时间": None,
                "来件类型": "建议",
                "来件标题": "公交线路",
                "来件内容": "建议增开公交线路",
                "回复部门": "交通局",
                "回复内容": None,
                "信件编号": "A002",
            },
        ]
    ).to_excel(file_path, index=False)

    with Session(engine) as session:
        first = import_excel(session, file_path, "江苏省", "苏州市")
        second = import_excel(session, file_path, "江苏省", "苏州市")
        stats = dashboard_stats(session, city="苏州市")

    assert first.inserted == 2
    assert second.skipped is True
    assert stats["total"] == 2
    assert stats["responded"] == 1
    assert stats["annotated"] == 2


def test_rag_backfill_and_search(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "rag.sqlite3"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        rag,
        "get_settings",
        lambda: SimpleNamespace(database_url=f"sqlite:///{db_path.as_posix()}", base_dir=tmp_path),
    )

    with Session(engine) as session:
        region = Region(province="江苏省", city="苏州市")
        session.add(region)
        session.flush()
        appeal = Appeal(
            region_id=region.id,
            external_id="R001",
            received_at=datetime(2025, 1, 1, 9, 0),
            replied_at=datetime(2025, 1, 2, 9, 0),
            appeal_type="建议",
            title="公交线路优化",
            content="建议增加公交班次，缓解早高峰拥堵。",
            department="交通局",
            reply_content="已转交公交公司研究。",
            redacted_title="公交线路优化",
            redacted_content="建议增加公交班次，缓解早高峰拥堵。",
            redacted_reply="已转交公交公司研究。",
        )
        session.add(appeal)
        old_appeal = Appeal(
            region_id=region.id,
            external_id="R000",
            received_at=datetime(2024, 1, 1, 9, 0),
            replied_at=datetime(2024, 1, 2, 9, 0),
            appeal_type="建议",
            title="公交线路旧数据",
            content="建议增加公交班次。",
            department="交通局",
            reply_content="已转交公交公司研究。",
            redacted_title="公交线路旧数据",
            redacted_content="建议增加公交班次。",
            redacted_reply="已转交公交公司研究。",
        )
        session.add(old_appeal)
        noise_appeal = Appeal(
            region_id=region.id,
            external_id="R999",
            received_at=datetime(2025, 2, 1, 9, 0),
            replied_at=datetime(2025, 2, 2, 9, 0),
            appeal_type="建议",
            title="其他咨询",
            content="仅咨询办理流程。",
            department="交通局",
            reply_content="感谢留言。",
            redacted_title="其他咨询",
            redacted_content="仅咨询办理流程。",
            redacted_reply="感谢留言。",
        )
        session.add(noise_appeal)
        session.flush()
        session.add(
            AppealAnnotation(
                appeal_id=appeal.id,
                topic="交通出行",
                keywords="公交、拥堵",
                summary="公交线路优化",
                source="rule",
            )
        )
        session.add(
            AppealAnnotation(
                appeal_id=old_appeal.id,
                topic="交通出行",
                keywords="公交",
                summary="公交线路旧数据",
                source="rule",
            )
        )
        session.add(
            AppealAnnotation(
                appeal_id=noise_appeal.id,
                topic="政务服务",
                keywords="咨询",
                summary="其他咨询",
                source="rule",
            )
        )
        session.commit()

        assert rag.backfill_chunks(session) == 3
        results = rag.search_relevant_appeals(session, "公交拥堵", city="苏州市")
        hybrid_results, fts_count, embedding_count = rag.hybrid_search_relevant_appeals(
            session, "公交拥堵", city="苏州市"
        )
        dated_results = rag.search_relevant_appeals(
            session, "公交公司研究", city="苏州市", start="2025-01-01", end="2025-12-31"
        )
        evidence = rag.build_evidence(session, "公交拥堵", city="苏州市", persist=False)
        reply_evidence = rag.build_evidence(session, "公交公司研究", city="苏州市", persist=False)
        reply_intent_evidence = rag.build_evidence(
            session, "回复中提到公交公司研究的问题有哪些", city="苏州市", persist=False
        )
        expected_appeal_id = appeal.id

    assert results
    assert hybrid_results
    assert fts_count >= 1
    assert embedding_count == 0
    assert dated_results
    assert [appeal_id for appeal_id, _ in dated_results] == [expected_appeal_id]
    assert evidence.candidate_count >= 1
    assert evidence.relevant_count >= 1
    assert evidence.selected_sources[0].external_id == "R001"
    assert "R999" not in [source.external_id for source in evidence.selected_sources]
    assert reply_evidence.selected_sources[0].external_id == "R001"
    assert "回复内容" in reply_evidence.selected_sources[0].matched_fields
    assert reply_intent_evidence.selected_sources[0].external_id == "R001"
    assert "回复内容" in reply_intent_evidence.selected_sources[0].matched_fields
