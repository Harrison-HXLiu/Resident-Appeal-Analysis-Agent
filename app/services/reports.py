from __future__ import annotations

from datetime import datetime
import logging

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, joinedload

from app.models import Appeal, AppealAnnotation, Region, Report
from app.services.analytics import dashboard_stats
from app.services.deepseek import DeepSeekService


logger = logging.getLogger(__name__)


def _shorten(value: str | None, limit: int = 220) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _name_count(item: dict[str, object]) -> tuple[str, int]:
    return str(item.get("name") or "未分类"), int(item.get("count") or 0)


def _ranking_lines(rows: list[dict[str, object]], limit: int = 10) -> str:
    lines: list[str] = []
    for index, item in enumerate(rows[:limit], start=1):
        name, count = _name_count(item)
        lines.append(f"{index}. {name}：{count} 件")
    return "\n".join(lines)


def _percentage(count: int, total: int) -> str:
    return f"{count / total * 100:.1f}%" if total else "0%"


def _format_date(value: object) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return ""


def _period_label(stats: dict[str, object], start: str | None, end: str | None) -> str:
    start_label = start or _format_date(stats.get("earliest")) or "最早数据"
    end_label = end or _format_date(stats.get("latest")) or "最新数据"
    return f"{start_label}至{end_label}"


def _case_query(
    session: Session,
    region: Region,
    start: str | None,
    end: str | None,
):
    conditions = [Appeal.region_id == region.id]
    if start:
        conditions.append(Appeal.received_at >= datetime.fromisoformat(start))
    if end:
        conditions.append(Appeal.received_at <= datetime.fromisoformat(end))
    return (
        select(Appeal)
        .options(joinedload(Appeal.annotation), joinedload(Appeal.region))
        .outerjoin(AppealAnnotation, AppealAnnotation.appeal_id == Appeal.id)
        .where(*conditions)
    )


def _representative_cases(
    session: Session,
    region: Region,
    stats: dict[str, object],
    start: str | None,
    end: str | None,
    per_topic: int = 2,
    limit: int = 14,
) -> list[Appeal]:
    selected: list[Appeal] = []
    seen: set[int] = set()
    topics = [str(item.get("name") or "") for item in stats.get("topics", [])[:6] if item.get("name")]

    for topic in topics:
        statement = (
            _case_query(session, region, start, end)
            .where(
                AppealAnnotation.topic == topic,
                Appeal.reply_content.is_not(None),
                Appeal.reply_content != "",
            )
            .order_by(Appeal.received_at.desc())
            .limit(per_topic)
        )
        for appeal in session.scalars(statement).unique().all():
            if appeal.id not in seen:
                selected.append(appeal)
                seen.add(appeal.id)
            if len(selected) >= limit:
                return selected

    fallback = (
        _case_query(session, region, start, end)
        .where(Appeal.reply_content.is_not(None), Appeal.reply_content != "")
        .order_by(Appeal.received_at.desc())
        .limit(limit * 2)
    )
    for appeal in session.scalars(fallback).unique().all():
        if appeal.id not in seen:
            selected.append(appeal)
            seen.add(appeal.id)
        if len(selected) >= limit:
            break
    return selected


def _case_lines(cases: list[Appeal], limit: int | None = None) -> str:
    lines: list[str] = []
    for index, appeal in enumerate(cases[: limit or len(cases)], start=1):
        annotation = appeal.annotation
        topic = annotation.topic if annotation and annotation.topic else "未分类"
        subtopic = annotation.subtopic if annotation and annotation.subtopic else ""
        lines.append(
            "\n".join(
                [
                    f"[{index}] 日期：{appeal.received_at:%Y-%m-%d}；类型：{appeal.appeal_type}；主题：{topic}{' / ' + subtopic if subtopic else ''}；回复部门：{appeal.department}",
                    f"标题：{_shorten(appeal.redacted_title or appeal.title, 120)}",
                    f"来件摘要：{_shorten(appeal.redacted_content or appeal.content, 260)}",
                    f"回复摘要：{_shorten(appeal.redacted_reply or appeal.reply_content, 260)}",
                ]
            )
        )
    return "\n\n".join(lines) if lines else "暂无可引用的典型案例。"


def _case_table(cases: list[Appeal], limit: int = 8) -> str:
    rows = ["| 主题 | 群众反映 | 回复部门 | 办理回应 |", "| --- | --- | --- | --- |"]
    for appeal in cases[:limit]:
        annotation = appeal.annotation
        topic = annotation.topic if annotation and annotation.topic else "未分类"
        rows.append(
            "| "
            + " | ".join(
                [
                    topic.replace("|", " "),
                    _shorten(appeal.redacted_title or appeal.title, 70).replace("|", " "),
                    (appeal.department or "").replace("|", " "),
                    _shorten(appeal.redacted_reply or appeal.reply_content, 90).replace("|", " "),
                ]
            )
            + " |"
        )
    return "\n".join(rows)


def _summary_text(stats: dict[str, object], period: str) -> str:
    total = int(stats.get("total") or 0)
    responded = int(stats.get("responded") or 0)
    response_rate = stats.get("response_rate") or 0
    avg_hours = stats.get("average_response_hours")
    top_topics = stats.get("topics", [])[:5]
    topic_summary = "、".join(
        f"{name}（{count}件，占{_percentage(count, total)}）"
        for name, count in (_name_count(item) for item in top_topics)
    )
    avg_label = f"{avg_hours}小时" if avg_hours is not None else "暂无可计算均值"
    return (
        f"统计期为{period}。共纳入居民留言{total}件，其中已有回复{responded}件，"
        f"回复率{response_rate}%，平均回复耗时{avg_label}。"
        f"从主题分布看，关注度较高的议题包括{topic_summary or '暂无稳定主题'}。"
    )


def _local_report(
    title: str,
    stats: dict[str, object],
    period: str,
    cases: list[Appeal],
) -> str:
    total = int(stats.get("total") or 0)
    top_topics = stats.get("topics", [])[:6]
    topic_lines = _ranking_lines(stats.get("topics", []), 10)
    subtopic_lines = _ranking_lines(stats.get("subtopics") or stats.get("topics", []), 10)
    type_lines = _ranking_lines(stats.get("types", []), 8)
    department_lines = _ranking_lines(stats.get("departments", []), 10)
    leading_topic, leading_count = _name_count(top_topics[0]) if top_topics else ("重点民生事项", 0)
    case_table = _case_table(cases)

    return f"""# {title}

## 一、总体判断

{_summary_text(stats, period)}总体看，苏州市居民诉求主要集中在与日常生活体验、城市运行秩序和基层公共服务直接相关的事项上。{leading_topic}居于前列，反映出相关领域既是群众感知最直接的治理触点，也是后续精细化治理应优先关注的方向。报告建议将高频诉求从“逐件办理”进一步提升为“专题治理”，围绕重复反映、跨部门协同和回复闭环建立持续跟踪机制。

## 二、苏州市居民诉求总体态势

### 1. 诉求类型结构

{type_lines or '暂无来件类型统计。'}

从来件类型看，群众诉求既包含投诉举报类问题，也包含咨询、建议和求助事项。投诉类事项通常指向具体生活困扰，需要较强的现场核查和整改闭环；咨询建议类事项则反映居民对政策解释、公共服务供给和城市治理参与的需求。对苏州而言，报告不宜停留在数量排序，而应把高频类型转化为部门办理压力和公共服务改进方向。

### 2. 热点主题分布

{topic_lines or '暂无主题统计。'}

{leading_topic}等高频主题占比较高，说明群众关注点具有明显的民生导向和场景导向。若某一主题占比持续居前，通常意味着问题并非孤立个案，而可能与管理标准、设施供给、执法频率、物业服务或属地协调机制有关。建议后续按主题建立月度台账，持续观察是否存在同一地点、同一主体或同一类型问题反复出现。

### 3. 细分议题观察

{subtopic_lines or '暂无细分议题统计。'}

细分议题能够帮助部门从“领域判断”进入“具体治理动作”。例如环境、物业、交通、市政等领域内部的问题形态差异较大，只有进一步拆到油烟噪声、停车秩序、小区公共设施、道路通行等具体事项，才便于明确牵头部门、协同部门和办理标准。

## 三、重点议题和典型案例

{case_table}

上述案例显示，群众留言通常不是抽象表达，而是围绕具体地点、具体事项和具体影响提出诉求。回复内容中已经包含核查、解释、转办、整改、巡查等办理动作，说明平台具备一定回应基础。下一步的关键，是把答复中的“已要求整改”“将加强巡查”“已反馈属地”等表述转化为可复核的闭环节点，减少同类问题反复发生。

## 四、政府回应和部门办理情况

### 1. 回复效率

统计期内回复率为{stats.get('response_rate') or 0}%，平均回复耗时为{stats.get('average_response_hours') if stats.get('average_response_hours') is not None else '暂无可计算均值'}小时。较高回复率说明留言办理机制总体运转稳定，但回复效率并不等同于问题解决质量。建议在现有统计基础上增加“是否现场核查、是否明确整改措施、是否说明时限、是否存在后续回访”等语义指标，使回复评价从速度指标延伸到治理效果指标。

### 2. 部门承办压力

{department_lines or '暂无回复部门统计。'}

承办量较高的部门往往处于群众诉求的一线触点，也更容易暴露跨部门协同压力。对于涉及属地管理、行业监管和执法处置交叉的问题，应建立“首接部门负责解释、相关部门协同核查、办理结果统一反馈”的机制，避免群众在多部门之间反复沟通。

## 五、风险信号和治理短板

本期数据提示，应重点关注三类信号：一是高频主题下同类场景反复出现，可能说明常态管理不足；二是回复中多次出现转办、协调、督促等表述，可能说明职责链条较长；三是群众反映事项与回复处置之间若缺少复查信息，容易造成“已回复但感受未改善”。这些问题不一定意味着办理不到位，但值得纳入重点跟踪清单。

## 六、工作建议

第一，围绕{leading_topic}等高频领域建立专题治理台账，对重复点位、重复主体、重复事项进行归并分析。第二，对回复内容进行结构化复核，重点识别核查、整改、解释、转办、回访五类动作是否完整。第三，推动承办量较高部门形成月度研判机制，把个案办理中反复出现的问题转化为制度优化。第四，在报告生成中持续引入来件内容和部门回复，形成“群众诉求—部门回应—治理建议”的闭环分析。

## 七、数据说明

本报告基于苏州市政府平台居民留言数据生成，统计期为{period}。报告使用结构化统计、主题分类、典型案例抽取和大模型辅助写作形成初稿，结论主要用于内部研判和专题汇报参考。后续如接入更多城市，可在保留苏州专报的基础上增加跨城市比较模块。
"""


def _system_prompt() -> str:
    return """
你是城市治理研究人员和政务数据分析报告撰写专家。请撰写一份面向领导汇报和内部决策参考的苏州市居民政策诉求分析报告。

写作要求：
1. 报告必须聚焦苏州本地治理，成熟、具体、可汇报，不要写成技术 Demo 说明。
2. 禁止在正文中使用“由于当前样本主要覆盖单一城市”“样本不足”“无法判断全国趋势”“当前 Demo”等削弱报告价值的表述。
3. 可以在末尾“数据说明”中客观说明数据来源和方法，但不要让限制性说明喧宾夺主。
4. 必须同时参考“群众来件内容”和“部门回复内容”，形成“问题表现—部门回应—治理研判—工作建议”的闭环。
5. 案例引用要提炼事实，不要大段复制原文；不要编造不存在的地点、部门、数字或政策。
6. 语言正式、克制、有判断，避免空泛口号。建议全文 3500-5000 字，Markdown 格式。
7. 章节必须使用以下结构：总体判断、苏州市居民诉求总体态势、重点议题和典型案例、政府回应和部门办理情况、风险信号和治理短板、工作建议、数据说明。
""".strip()


def _user_prompt(
    title: str,
    stats: dict[str, object],
    period: str,
    cases: list[Appeal],
) -> str:
    return f"""
请生成报告标题：{title}

统计摘要：
{_summary_text(stats, period)}

来件类型分布：
{_ranking_lines(stats.get('types', []), 10) or '暂无'}

一级主题分布：
{_ranking_lines(stats.get('topics', []), 12) or '暂无'}

细分主题分布：
{_ranking_lines(stats.get('subtopics') or stats.get('topics', []), 12) or '暂无'}

回复部门分布：
{_ranking_lines(stats.get('departments', []), 12) or '暂无'}

月度趋势：
{stats.get('monthly') or '暂无'}

请重点引用以下典型案例。每个案例都包含来件摘要和回复摘要，报告中要体现部门回应，不要只分析投诉本身：
{_case_lines(cases)}

请按以下 Markdown 结构输出，不要更改标题层级：
# {title}

## 一、总体判断
## 二、苏州市居民诉求总体态势
### 1. 诉求类型结构
### 2. 热点主题分布
### 3. 细分议题观察
## 三、重点议题和典型案例
## 四、政府回应和部门办理情况
### 1. 回复效率
### 2. 部门承办压力
## 五、风险信号和治理短板
## 六、工作建议
## 七、数据说明
""".strip()


def create_report(
    session: Session,
    region: Region,
    start: str | None = None,
    end: str | None = None,
) -> Report:
    stats = dashboard_stats(session, province=region.province, city=region.city, start=start, end=end)
    scope = f"{region.province}{region.city}"
    period = _period_label(stats, start, end)
    title = f"{scope}居民政策诉求分析报告（{period}）"
    cases = _representative_cases(session, region, stats, start, end)

    llm = DeepSeekService()
    if llm.enabled:
        try:
            content = llm.complete(
                _system_prompt(),
                _user_prompt(title, stats, period, cases),
                timeout=300,
                max_tokens=9000,
            )
            generated_by = f"{llm.model_name} + suzhou-report-rag"
        except Exception as exc:
            logger.exception("AI report generation failed; retrying with compact evidence.")
            compact_cases = cases[:8]
            try:
                content = llm.complete(
                    _system_prompt(),
                    _user_prompt(title, stats, period, compact_cases),
                    timeout=300,
                    max_tokens=7500,
                )
                generated_by = f"{llm.model_name} + suzhou-report-rag (compact retry)"
            except Exception as retry_exc:
                logger.exception("AI report compact retry failed; using local report template.")
                content = _local_report(title, stats, period, cases)
                generated_by = f"local-suzhou-template (AI fallback: {type(retry_exc).__name__})"
    else:
        content = _local_report(title, stats, period, cases)
        generated_by = "local-suzhou-template"

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
