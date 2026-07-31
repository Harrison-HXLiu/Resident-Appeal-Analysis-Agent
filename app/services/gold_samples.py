from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Appeal,
    AppealAnnotation,
    GoldAnnotation,
    GoldSample,
    TaxonomyLabel,
    TaxonomyVersion,
    User,
)


FINAL_STATUSES = {"agreed", "arbitrated"}


def _label(
    session: Session,
    version: TaxonomyVersion,
    label_id: int | None,
    level: int,
) -> TaxonomyLabel | None:
    if label_id is None:
        return None
    label = session.scalar(
        select(TaxonomyLabel).where(
            TaxonomyLabel.id == label_id,
            TaxonomyLabel.taxonomy_version_id == version.id,
            TaxonomyLabel.level == level,
        )
    )
    if not label:
        raise ValueError(f"标签 {label_id} 不属于当前标签版本的第{level}级")
    return label


def _validated_labels(
    session: Session,
    sample: GoldSample,
    l1_label_id: int,
    l2_label_id: int | None,
) -> tuple[TaxonomyLabel, TaxonomyLabel | None]:
    l1 = _label(session, sample.taxonomy_version, l1_label_id, 1)
    assert l1 is not None
    l2 = _label(session, sample.taxonomy_version, l2_label_id, 2)
    if l2 and l2.parent_id != l1.id:
        raise ValueError("二级标签不属于所选一级标签")
    return l1, l2


def create_gold_samples(
    session: Session,
    version: TaxonomyVersion,
    appeal_ids: list[int],
) -> list[GoldSample]:
    if version.status == "published":
        raise ValueError("已发布标签版本不能再新增黄金样本")
    unique_ids = list(dict.fromkeys(appeal_ids))
    appeals = {
        item.id: item
        for item in session.scalars(select(Appeal).where(Appeal.id.in_(unique_ids))).all()
    }
    missing = [item for item in unique_ids if item not in appeals]
    if missing:
        raise ValueError(f"留言不存在：{missing[:10]}")
    existing_ids = set(
        session.scalars(
            select(GoldSample.appeal_id).where(
                GoldSample.taxonomy_version_id == version.id,
                GoldSample.appeal_id.in_(unique_ids),
            )
        ).all()
    )
    created = [
        GoldSample(taxonomy_version_id=version.id, appeal_id=appeal_id)
        for appeal_id in unique_ids
        if appeal_id not in existing_ids
    ]
    session.add_all(created)
    session.commit()
    return created


def get_gold_sample(session: Session, version_id: int, sample_id: int) -> GoldSample | None:
    return session.scalar(
        select(GoldSample)
        .options(
            selectinload(GoldSample.taxonomy_version),
            selectinload(GoldSample.appeal),
            selectinload(GoldSample.annotations).selectinload(GoldAnnotation.l1_label),
            selectinload(GoldSample.annotations).selectinload(GoldAnnotation.l2_label),
            selectinload(GoldSample.final_l1_label),
            selectinload(GoldSample.final_l2_label),
        )
        .where(
            GoldSample.id == sample_id,
            GoldSample.taxonomy_version_id == version_id,
        )
    )


def list_gold_samples(
    session: Session,
    version_id: int,
    *,
    status: str = "",
    limit: int = 100,
) -> list[GoldSample]:
    statement = (
        select(GoldSample)
        .options(
            selectinload(GoldSample.appeal),
            selectinload(GoldSample.annotations).selectinload(GoldAnnotation.l1_label),
            selectinload(GoldSample.annotations).selectinload(GoldAnnotation.l2_label),
            selectinload(GoldSample.final_l1_label),
            selectinload(GoldSample.final_l2_label),
        )
        .where(GoldSample.taxonomy_version_id == version_id)
        .order_by(GoldSample.id)
        .limit(max(1, min(limit, 500)))
    )
    if status:
        statement = statement.where(GoldSample.status == status)
    return list(session.scalars(statement).all())


def submit_gold_annotation(
    session: Session,
    sample: GoldSample,
    *,
    annotator_key: str,
    user: User | None,
    l1_label_id: int,
    l2_label_id: int | None,
    notes: str = "",
) -> GoldAnnotation:
    if sample.status in FINAL_STATUSES:
        raise ValueError("该黄金样本已经定稿")
    annotator_key = annotator_key.strip()
    if not annotator_key:
        raise ValueError("必须提供标注人身份")
    _validated_labels(session, sample, l1_label_id, l2_label_id)
    annotations = [
        item for item in sample.annotations if item.role == "annotator"
    ]
    if any(item.annotator_key == annotator_key for item in annotations):
        raise ValueError("同一标注人不能重复提交")
    if len(annotations) >= 2:
        raise ValueError("双人标注已经完成，存在分歧时请进入仲裁")
    annotation = GoldAnnotation(
        sample_id=sample.id,
        user_id=user.id if user else None,
        annotator_key=annotator_key,
        role="annotator",
        l1_label_id=l1_label_id,
        l2_label_id=l2_label_id,
        notes=notes.strip(),
    )
    session.add(annotation)
    session.flush()
    annotations.append(annotation)
    if len(annotations) == 1:
        sample.status = "second_pending"
    else:
        first, second = annotations
        if (first.l1_label_id, first.l2_label_id) == (
            second.l1_label_id,
            second.l2_label_id,
        ):
            sample.status = "agreed"
            sample.final_l1_label_id = first.l1_label_id
            sample.final_l2_label_id = first.l2_label_id
            sample.finalized_by = "双人一致"
            sample.finalized_at = datetime.now()
        else:
            sample.status = "disputed"
    session.commit()
    recompute_taxonomy_metrics(session, sample.taxonomy_version)
    session.refresh(annotation)
    return annotation


def arbitrate_gold_sample(
    session: Session,
    sample: GoldSample,
    *,
    arbitrator_key: str,
    user: User | None,
    l1_label_id: int,
    l2_label_id: int | None,
    notes: str = "",
) -> GoldAnnotation:
    if sample.status != "disputed":
        raise ValueError("只有双人标注不一致的样本可以仲裁")
    arbitrator_key = arbitrator_key.strip()
    annotators = {
        item.annotator_key for item in sample.annotations if item.role == "annotator"
    }
    if not arbitrator_key:
        raise ValueError("必须提供仲裁人身份")
    if arbitrator_key in annotators:
        raise ValueError("仲裁人不能是该样本的两名标注人")
    _validated_labels(session, sample, l1_label_id, l2_label_id)
    annotation = GoldAnnotation(
        sample_id=sample.id,
        user_id=user.id if user else None,
        annotator_key=arbitrator_key,
        role="arbitrator",
        l1_label_id=l1_label_id,
        l2_label_id=l2_label_id,
        notes=notes.strip(),
    )
    session.add(annotation)
    sample.status = "arbitrated"
    sample.final_l1_label_id = l1_label_id
    sample.final_l2_label_id = l2_label_id
    sample.finalized_by = arbitrator_key
    sample.finalized_at = datetime.now()
    session.commit()
    recompute_taxonomy_metrics(session, sample.taxonomy_version)
    session.refresh(annotation)
    return annotation


def _macro_f1(truth: list[str], prediction: list[str]) -> float | None:
    if not truth:
        return None
    scores: list[float] = []
    for label in sorted(set(truth)):
        true_positive = sum(t == label and p == label for t, p in zip(truth, prediction))
        false_positive = sum(t != label and p == label for t, p in zip(truth, prediction))
        false_negative = sum(t == label and p != label for t, p in zip(truth, prediction))
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(2 * true_positive / denominator if denominator else 0.0)
    return round(sum(scores) / len(scores), 4)


def recompute_taxonomy_metrics(
    session: Session,
    version: TaxonomyVersion,
) -> dict[str, int | float | None]:
    samples = list(
        session.scalars(
            select(GoldSample)
            .options(
                selectinload(GoldSample.final_l1_label),
                selectinload(GoldSample.final_l2_label),
                selectinload(GoldSample.appeal).selectinload(Appeal.annotation),
            )
            .where(
                GoldSample.taxonomy_version_id == version.id,
                GoldSample.status.in_(FINAL_STATUSES),
            )
        ).all()
    )
    l1_truth: list[str] = []
    l1_prediction: list[str] = []
    l2_truth: list[str] = []
    l2_prediction: list[str] = []
    for sample in samples:
        predicted = sample.appeal.annotation
        if sample.final_l1_label:
            l1_truth.append(sample.final_l1_label.name)
            l1_prediction.append(predicted.topic if predicted else "")
        if sample.final_l2_label:
            l2_truth.append(sample.final_l2_label.name)
            l2_prediction.append(predicted.subtopic if predicted else "")
    version.gold_sample_size = len(samples)
    version.l1_macro_f1 = _macro_f1(l1_truth, l1_prediction)
    version.l2_macro_f1 = _macro_f1(l2_truth, l2_prediction)
    session.commit()
    status_counts = dict(
        session.execute(
            select(GoldSample.status, func.count(GoldSample.id))
            .where(GoldSample.taxonomy_version_id == version.id)
            .group_by(GoldSample.status)
        ).all()
    )
    return {
        "gold_sample_size": version.gold_sample_size,
        "l1_macro_f1": version.l1_macro_f1,
        "l2_macro_f1": version.l2_macro_f1,
        **{f"status_{key}": value for key, value in status_counts.items()},
    }
