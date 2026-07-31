from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TaxonomyLabel, TaxonomyVersion
from app.services.classification import TAXONOMY_RULES


DRAFT_VERSION = "v1-review-draft"


def ensure_draft_taxonomy(session: Session) -> TaxonomyVersion:
    version = session.scalar(select(TaxonomyVersion).where(TaxonomyVersion.version == DRAFT_VERSION))
    if version:
        other = session.scalar(
            select(TaxonomyLabel).where(
                TaxonomyLabel.taxonomy_version_id == version.id,
                TaxonomyLabel.level == 1,
                TaxonomyLabel.name == "其他/综合",
            )
        )
        if other is None:
            session.add(
                TaxonomyLabel(
                    taxonomy_version_id=version.id,
                    level=1,
                    name="其他/综合",
                    definition=(
                        "无法可靠归入已发布一级类，或同时涉及多个一级类且无法确定主诉求时使用。"
                    ),
                    status="trial",
                    sort_order=len(TAXONOMY_RULES),
                )
            )
            session.commit()
        return version
    version = TaxonomyVersion(
        version=DRAFT_VERSION,
        status="trial",
        description="17个业务一级类加“其他/综合”的研究评审初稿",
    )
    session.add(version)
    session.flush()
    sort_order = 0
    for topic, subtopics in TAXONOMY_RULES.items():
        parent = TaxonomyLabel(
            taxonomy_version_id=version.id,
            level=1,
            name=topic,
            definition="待研究团队补充边界定义",
            status="trial",
            sort_order=sort_order,
        )
        session.add(parent)
        session.flush()
        sort_order += 1
        for sub_index, (subtopic, keywords) in enumerate(subtopics.items()):
            session.add(
                TaxonomyLabel(
                    taxonomy_version_id=version.id,
                    parent_id=parent.id,
                    level=2,
                    name=subtopic,
                    definition="候选二级标签，需研究人员审核后发布",
                    include_examples=list(keywords[:5]),
                    status="candidate",
                    sort_order=sub_index,
                )
            )
    session.add(
        TaxonomyLabel(
            taxonomy_version_id=version.id,
            level=1,
            name="其他/综合",
            definition="无法可靠归入已发布一级类，或同时涉及多个一级类且无法确定主诉求时使用。",
            status="trial",
            sort_order=sort_order,
        )
    )
    session.commit()
    return version


def active_taxonomy(session: Session) -> TaxonomyVersion:
    published = session.scalar(
        select(TaxonomyVersion)
        .where(TaxonomyVersion.status == "published")
        .order_by(TaxonomyVersion.published_at.desc())
    )
    return published or ensure_draft_taxonomy(session)


def can_publish(version: TaxonomyVersion) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if version.gold_sample_size < 1500:
        failures.append("黄金样本少于1500条")
    if version.l1_macro_f1 is None or version.l1_macro_f1 < 0.85:
        failures.append("一级标签宏平均F1未达到0.85")
    if version.l2_macro_f1 is None or version.l2_macro_f1 < 0.75:
        failures.append("二级标签宏平均F1未达到0.75")
    level_one = [item for item in version.labels if item.level == 1]
    if len(level_one) != 18:
        failures.append("一级标签必须包含17个业务类和“其他/综合”")
    if any(item.status not in {"approved", "published"} for item in level_one):
        failures.append("仍有一级标签未经研究团队批准")
    if any(
        not item.definition.strip() or "待研究团队" in item.definition
        for item in level_one
    ):
        failures.append("仍有一级标签缺少已确认的边界定义")
    if any(
        item.level == 2 and item.status not in {"approved", "rejected", "published"}
        for item in version.labels
    ):
        failures.append("仍有二级候选标签未完成批准或拒绝")
    return not failures, failures


def publish_taxonomy(session: Session, version: TaxonomyVersion) -> None:
    from app.services.gold_samples import recompute_taxonomy_metrics

    recompute_taxonomy_metrics(session, version)
    allowed, failures = can_publish(version)
    if not allowed:
        raise ValueError("；".join(failures))
    for item in session.scalars(
        select(TaxonomyVersion).where(TaxonomyVersion.status == "published")
    ).all():
        item.status = "retired"
    version.status = "published"
    version.published_at = datetime.now()
    for label in version.labels:
        if label.status == "approved":
            label.status = "published"
    session.commit()
