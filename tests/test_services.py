from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.services.analytics import dashboard_stats
from app.services.classification import classify_by_rule
from app.services.importer import import_excel
from app.services.privacy import redact_text


def test_redact_text_removes_common_identifiers() -> None:
    original = "联系人电话13812345678，身份证320501199001011234，邮箱test@example.com，地址：苏州市某小区12栋。"
    result = redact_text(original)
    assert "13812345678" not in result
    assert "320501199001011234" not in result
    assert "test@example.com" not in result
    assert "苏州市某小区12栋" not in result


def test_rule_classification_finds_transport_topic() -> None:
    result = classify_by_rule("公交站点设置建议", "希望调整公交线路，缓解道路拥堵。")
    assert result.topic == "交通出行"
    assert "公交" in result.keywords


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

