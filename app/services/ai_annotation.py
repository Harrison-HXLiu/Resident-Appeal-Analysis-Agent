from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import AnalysisJob, Appeal, AppealAnnotation, Region
from app.services.classification import TOPIC_RULES
from app.services.deepseek import DeepSeekService


def refine_annotations_with_ai(
    session: Session,
    city: str | None,
    limit: int,
    llm: DeepSeekService | None = None,
) -> AnalysisJob:
    llm = llm or DeepSeekService()
    job = AnalysisJob(job_type="ai_annotation", status="running")
    session.add(job)
    session.commit()
    topics = list(TOPIC_RULES) + ["其他"]
    statement = (
        select(Appeal)
        .join(Region)
        .join(AppealAnnotation)
        .options(joinedload(Appeal.annotation))
        .where(AppealAnnotation.source == "rule")
        .order_by(Appeal.received_at.desc())
    )
    if city:
        statement = statement.where(Region.city == city)
    statement = statement.limit(min(max(limit, 1), 200))
    appeals = list(session.scalars(statement).all())

    try:
        for offset in range(0, len(appeals), 10):
            chunk = appeals[offset : offset + 10]
            items = [
                {
                    "id": appeal.id,
                    "title": appeal.redacted_title[:160],
                    "content": appeal.redacted_content[:800],
                }
                for appeal in chunk
            ]
            result = llm.complete_json(
                (
                    "你是政府留言数据分类助手。只根据已脱敏留言做分类，不猜测个人信息。"
                    "输出 JSON 对象，键为 items，值为数组。每项包含 id、topic、subtopic、"
                    "keywords（数组）、summary、urgency（一般/较急/紧急）、confidence（0到1）。"
                    f"topic 必须选自：{topics}。"
                ),
                "请分类以下留言并返回 JSON：\n" + json.dumps(items, ensure_ascii=False),
            )
            mapped = {int(item["id"]): item for item in result.get("items", []) if "id" in item}
            for appeal in chunk:
                item = mapped.get(appeal.id)
                if not item:
                    job.failed_count += 1
                    continue
                annotation = appeal.annotation
                annotation.topic = item.get("topic") if item.get("topic") in topics else "其他"
                annotation.subtopic = str(item.get("subtopic", ""))[:120]
                keywords = item.get("keywords", [])
                annotation.keywords = "、".join(str(word) for word in keywords[:8])
                annotation.summary = str(item.get("summary", ""))[:300]
                annotation.urgency = str(item.get("urgency", "一般"))[:30]
                annotation.confidence = float(item.get("confidence", 0.7))
                annotation.source = "deepseek"
                annotation.model_name = llm.model_name
                annotation.updated_at = datetime.now()
                job.processed_count += 1
            session.commit()
        job.status = "completed"
        job.finished_at = datetime.now()
    except Exception as exc:
        job.status = "failed"
        job.message = str(exc)[:1000]
        job.finished_at = datetime.now()
    session.commit()
    return job
