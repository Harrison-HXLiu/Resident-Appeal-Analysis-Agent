from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import AnalysisJob, Appeal, AppealAnnotation, Region
from app.services.classification import TAXONOMY_RULES
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
    topics = list(TAXONOMY_RULES) + ["其他/综合"]
    taxonomy_text = json.dumps(
        {topic: list(subtopics) for topic, subtopics in TAXONOMY_RULES.items()},
        ensure_ascii=False,
    )
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
                    f"topic 必须选自：{topics}。subtopic 必须从该 topic 对应的二级标签中选择。"
                    "若无法归入前17类，topic 选择“其他/综合”，subtopic 留空。"
                    f"一级-二级标签体系为：{taxonomy_text}。"
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
                topic = item.get("topic") if item.get("topic") in topics else "其他/综合"
                allowed_subtopics = set(TAXONOMY_RULES.get(topic, {}))
                raw_subtopic = str(item.get("subtopic", ""))[:120]
                annotation.topic = topic
                annotation.subtopic = raw_subtopic if raw_subtopic in allowed_subtopics else ""
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
