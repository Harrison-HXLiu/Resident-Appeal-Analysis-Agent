from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Region, Report
from app.services.agent import _facts_as_text
from app.services.analytics import dashboard_stats
from app.services.deepseek import DeepSeekService


def _local_report(title: str, facts: str, stats: dict[str, object]) -> str:
    topics = "\n".join(
        f"{index}. {item['name']}：{item['count']} 件"
        for index, item in enumerate(stats["topics"][:10], start=1)
    )
    departments = "\n".join(
        f"{index}. {item['name']}：{item['count']} 件"
        for index, item in enumerate(stats["departments"][:10], start=1)
    )
    return (
        f"# {title}\n\n"
        f"## 一、数据概况\n\n{facts}\n\n"
        f"## 二、热点问题初步分布\n\n{topics or '暂无可分析主题。'}\n\n"
        f"## 三、回复部门分布\n\n{departments or '暂无部门数据。'}\n\n"
        "## 四、使用说明\n\n"
        "本报告为系统统计模板生成，主题标签当前可能包含关键词规则初标结果。"
        "接入 DeepSeek 并完成 AI 复核后，可形成包含趋势解读和治理建议的研判报告。\n"
    )


def create_report(
    session: Session,
    region: Region,
    start: str | None = None,
    end: str | None = None,
) -> Report:
    stats = dashboard_stats(session, province=region.province, city=region.city, start=start, end=end)
    scope = f"{region.province}{region.city}"
    period = f"{start or '最早数据'}至{end or '最新数据'}"
    title = f"{scope}居民留言问题分析与汇总报告（{period}）"
    facts = _facts_as_text(stats, f"{scope}，{period}")
    llm = DeepSeekService()
    if llm.enabled:
        try:
            content = llm.complete(
                (
                    "你是政务数据分析报告撰写助手。只依据提供的统计摘要撰写 Markdown 报告，"
                    "结构包含数据范围、总体情况、热点问题、办理情况、治理建议和方法说明。"
                    "不要虚构案例或未提供的因果结论；若标签包含规则初标，要注明局限。"
                ),
                f"报告标题：{title}\n\n统计摘要：\n{facts}",
            )
            generated_by = llm.model_name
        except Exception:
            content = _local_report(title, facts, stats)
            generated_by = "local-template (AI fallback)"
    else:
        content = _local_report(title, facts, stats)
        generated_by = "local-template"

    report = Report(
        region_id=region.id,
        title=title,
        period_start=datetime.fromisoformat(start) if start else None,
        period_end=datetime.fromisoformat(end) if end else None,
        content=content,
        generated_by=generated_by,
    )
    session.add(report)
    session.commit()
    return report
