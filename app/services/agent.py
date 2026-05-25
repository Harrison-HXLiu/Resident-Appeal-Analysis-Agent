from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.services.analytics import dashboard_stats
from app.services.deepseek import DeepSeekService
from app.services.privacy import redact_text


def _facts_as_text(stats: dict[str, object], scope: str) -> str:
    topics = "；".join(f"{item['name']} {item['count']}件" for item in stats["topics"][:8])
    types = "；".join(f"{item['name']} {item['count']}件" for item in stats["types"][:6])
    departments = "；".join(f"{item['name']} {item['count']}件" for item in stats["departments"][:8])
    return (
        f"统计范围：{scope}\n"
        f"留言总量：{stats['total']}件；已回复：{stats['responded']}件；"
        f"回复率：{stats['response_rate']}%；平均回复耗时：{stats['average_response_hours']}小时。\n"
        f"主题排行：{topics or '暂无标签'}。\n"
        f"来件类型：{types or '暂无数据'}。\n"
        f"回复部门排行：{departments or '暂无数据'}。\n"
        f"主题标签来源：{json.dumps(stats['topic_sources'], ensure_ascii=False)}。"
    )


def ask_question(
    session: Session,
    question: str,
    city: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[str, str]:
    stats = dashboard_stats(session, city=city, start=start, end=end)
    scope = city or "全部已导入地区"
    if start or end:
        scope += f"（{start or '最早'} 至 {end or '最新'}）"
    facts = _facts_as_text(stats, scope)
    llm = DeepSeekService()
    if not llm.enabled:
        answer = (
            "当前未配置 DeepSeek API Key，先返回数据库统计结果。\n\n"
            + facts
            + "\n\n说明：当前主题排行来自导入时生成的规则初标；配置 API Key 后可在“数据管理”中运行 AI 复核，并生成更完整的研判答复。"
        )
        return answer, "local-statistics"

    try:
        answer = llm.complete(
            (
                "你是居民留言分析助手。回答必须严格基于提供的统计事实；不得编造数量、"
                "城市覆盖范围或案例。若用户询问超出数据范围，明确说明。"
                "主题标签若含 rule 来源，应说明这是初步分类。用简洁中文作答。"
            ),
            f"用户问题（已脱敏）：{redact_text(question)}\n\n可用事实：\n{facts}",
        )
        return answer, llm.model_name
    except Exception:
        answer = (
            "DeepSeek 当前调用失败，已回退为数据库统计结果。\n\n"
            + facts
            + "\n\n说明：可检查 API Key、模型名称或网络连接后重试。"
        )
        return answer, "local-statistics (AI fallback)"
