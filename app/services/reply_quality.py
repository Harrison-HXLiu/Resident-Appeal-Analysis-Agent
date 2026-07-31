from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Appeal, ReplyQuality


@dataclass(frozen=True)
class QualityResult:
    addresses_issue: str
    explains_basis: str
    provides_action: str
    gives_timeline_owner: str
    provides_followup: str
    score: float | None
    evidence: dict[str, object]
    confidence: float


_BASIS_TERMS = ("根据", "依据", "按照", "规定", "条例", "办法", "政策", "文件")
_ACTION_TERMS = ("已处理", "已整改", "已协调", "已转办", "已核查", "已联系", "将开展", "责令", "督促")
_OWNER_TERMS = ("由我局", "由该局", "由属地", "负责", "承办", "部门")
_FOLLOWUP_TERMS = ("联系电话", "如有疑问", "可咨询", "再次反映", "后续", "回访")
_TIMELINE_RE = re.compile(r"(?:\d+\s*(?:日|天|个工作日|月)|本周|本月|近期|尽快|截至)")


def _match(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term in text]


def score_reply_quality(appeal: Appeal) -> QualityResult:
    reply = (appeal.redacted_reply or appeal.reply_content or "").strip()
    if not reply:
        return QualityResult(
            "no",
            "not_applicable",
            "no",
            "not_applicable",
            "no",
            0.0,
            {"reason": "无有效回复"},
            1.0,
        )
    question_terms = {
        item
        for item in re.findall(r"[\u4e00-\u9fff]{2,6}", f"{appeal.redacted_title}{appeal.redacted_content}")
        if item not in {"问题", "希望", "有关", "相关", "情况", "反映"}
    }
    overlaps = [term for term in question_terms if term in reply][:5]
    basis = _match(reply, _BASIS_TERMS)
    actions = _match(reply, _ACTION_TERMS)
    owners = _match(reply, _OWNER_TERMS)
    followups = _match(reply, _FOLLOWUP_TERMS)
    timeline = _TIMELINE_RE.findall(reply)
    is_consultation = appeal.appeal_type in {"咨询", "政策咨询"}

    values = {
        "addresses_issue": "yes" if overlaps or len(reply) >= 80 else "no",
        "explains_basis": "yes" if basis else ("not_applicable" if not is_consultation else "no"),
        "provides_action": "yes" if actions else ("not_applicable" if is_consultation else "no"),
        "gives_timeline_owner": (
            "yes"
            if timeline or owners
            else ("not_applicable" if is_consultation else "no")
        ),
        "provides_followup": "yes" if followups else "no",
    }
    applicable = [value for value in values.values() if value != "not_applicable"]
    score = round(sum(value == "yes" for value in applicable) / len(applicable) * 100, 1)
    evidence = {
        "question_overlap": overlaps,
        "basis": basis,
        "actions": actions,
        "timeline": timeline[:5],
        "owner": owners,
        "followup": followups,
    }
    confidence = min(0.55 + 0.05 * sum(bool(value) for value in evidence.values()), 0.85)
    return QualityResult(**values, score=score, evidence=evidence, confidence=confidence)


def upsert_reply_quality(session: Session, appeal: Appeal) -> ReplyQuality:
    result = score_reply_quality(appeal)
    record = session.scalar(select(ReplyQuality).where(ReplyQuality.appeal_id == appeal.id))
    if record is None:
        record = ReplyQuality(appeal_id=appeal.id)
        session.add(record)
    record.addresses_issue = result.addresses_issue
    record.explains_basis = result.explains_basis
    record.provides_action = result.provides_action
    record.gives_timeline_owner = result.gives_timeline_owner
    record.provides_followup = result.provides_followup
    record.score = result.score
    record.evidence = result.evidence
    record.confidence = result.confidence
    return record
