from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services.analytics import dashboard_stats
from app.services.deepseek import DeepSeekService
from app.services.privacy import redact_text
from app.services.rag import RagEvidence, build_evidence


@dataclass(frozen=True)
class AskContext:
    facts: str
    evidence: RagEvidence
    evidence_summary: str
    system_prompt: str
    user_prompt: str


def infer_time_range_from_question(
    question: str,
    start: str | None = None,
    end: str | None = None,
) -> tuple[str | None, str | None]:
    if start or end:
        return start, end
    match = re.search(r"((?:20|19)\d{2})\s*年", question)
    if match:
        year = int(match.group(1))
        return f"{year}-01-01", f"{year}-12-31"
    short_match = re.search(r"(?<!\d)(\d{2})\s*年", question)
    if not short_match:
        return start, end
    year = 2000 + int(short_match.group(1))
    return f"{year}-01-01", f"{year}-12-31"


def _monthly_trend_text(stats: dict[str, object], limit: int = 18) -> str:
    rows = stats.get("monthly") or []
    if not rows:
        return "暂无分月趋势数据"
    shown = rows[-limit:]
    series = "；".join(f"{item['month']} {item['count']}件" for item in shown)
    if len(rows) > limit:
        series = f"仅列最近{limit}个月：" + series
    counts = [int(item["count"]) for item in rows]
    peak = max(rows, key=lambda item: int(item["count"]))
    low = min(rows, key=lambda item: int(item["count"]))
    direction = "基本平稳"
    if len(counts) >= 2:
        change = counts[-1] - counts[0]
        threshold = max(sum(counts) / len(counts) * 0.15, 1)
        if change > threshold:
            direction = "总体上升"
        elif change < -threshold:
            direction = "总体下降"
    return f"{series}。趋势粗判：{direction}；峰值月份：{peak['month']} {peak['count']}件；低值月份：{low['month']} {low['count']}件"


def _facts_as_text(stats: dict[str, object], scope: str) -> str:
    topics = "；".join(f"{item['name']} {item['count']}件" for item in stats["topics"][:8])
    types = "；".join(f"{item['name']} {item['count']}件" for item in stats["types"][:6])
    departments = "；".join(f"{item['name']} {item['count']}件" for item in stats["departments"][:8])
    monthly = _monthly_trend_text(stats)
    return (
        f"统计范围：{scope}\n"
        f"留言总量：{stats['total']}件；已回复：{stats['responded']}件；"
        f"回复率：{stats['response_rate']}%；平均回复耗时：{stats['average_response_hours']}小时。\n"
        f"分月趋势：{monthly}。\n"
        f"主题排行：{topics or '暂无标签'}。\n"
        f"来件类型：{types or '暂无数据'}。\n"
        f"回复部门排行：{departments or '暂无数据'}。\n"
        f"主题标签来源：{json.dumps(stats['topic_sources'], ensure_ascii=False)}。"
    )


def prepare_ask_context(
    session: Session,
    question: str,
    city: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> AskContext:
    start, end = infer_time_range_from_question(question, start, end)
    stats = dashboard_stats(session, city=city, start=start, end=end)
    scope = city or "全部已导入地区"
    if start or end:
        scope += f"（{start or '最早'} 至 {end or '最新'}）"
    facts = _facts_as_text(stats, scope)
    evidence = build_evidence(session, question, city=city, start=start, end=end)
    evidence_summary = (
        f"本次关键词候选 {evidence.candidate_count} 条，语义候选 {evidence.embedding_candidate_count} 条，"
        f"选取 {len(evidence.selected_sources)} 条代表性案例。"
    )
    evidence_note = (
        "若检索证据数量较少，只能基于有限样本回答；不要把代表案例数量说成全部问题数量。"
    )
    system_prompt = (
        "你是居民留言分析助手。回答必须严格基于提供的统计事实；不得编造数量、"
        "城市覆盖范围或案例。若用户询问超出数据范围，明确说明。"
        "主题标签若含 rule 来源，应说明这是初步分类。"
        "请综合统计摘要和检索证据作答，优先给出具体问题类型、涉及部门、"
        "代表案例和办理回复特点；引用案例时使用方括号编号，如[1]。用简洁中文作答。"
    )
    user_prompt = (
        f"用户问题（已脱敏）：{redact_text(question)}\n\n"
        f"可用统计事实：\n{facts}\n\n"
        f"RAG 检索说明：{evidence_summary}；过滤后高相关 {evidence.relevant_count} 条。{evidence_note}\n\n"
        f"代表性证据：\n{evidence.evidence_text or '暂无代表性证据'}"
    )
    return AskContext(
        facts=facts,
        evidence=evidence,
        evidence_summary=evidence_summary,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )


def ask_question(
    session: Session,
    question: str,
    city: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[str, str, RagEvidence]:
    context = prepare_ask_context(session, question, city=city, start=start, end=end)
    llm = DeepSeekService()
    if not llm.enabled:
        answer = (
            "当前未配置 DeepSeek API Key，先返回数据库统计结果。\n\n"
            + context.facts
            + "\n\n"
            + context.evidence_summary
            + (
                "\n\n代表性案例：\n" + context.evidence.evidence_text
                if context.evidence.evidence_text
                else "\n\n未检索到足够相关的代表性案例。"
            )
            + "\n\n说明：当前主题排行来自导入时生成的规则初标；配置 API Key 后可生成更完整的研判答复。"
        )
        return answer, "local-statistics + rag", context.evidence

    try:
        answer = llm.complete(context.system_prompt, context.user_prompt)
        return answer, f"{llm.model_name} + rag", context.evidence
    except Exception:
        answer = (
            "DeepSeek 当前调用失败，已回退为数据库统计结果。\n\n"
            + context.facts
            + "\n\n"
            + context.evidence_summary
            + (
                "\n\n代表性案例：\n" + context.evidence.evidence_text
                if context.evidence.evidence_text
                else "\n\n未检索到足够相关的代表性案例。"
            )
            + "\n\n说明：可检查 API Key、模型名称或网络连接后重试。"
        )
        return answer, "local-statistics + rag (AI fallback)", context.evidence
