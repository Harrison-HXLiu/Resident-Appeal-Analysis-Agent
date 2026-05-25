from __future__ import annotations

from dataclasses import dataclass


TOPIC_RULES: dict[str, tuple[str, ...]] = {
    "住房建设": (
        "房屋",
        "住房",
        "小区",
        "物业",
        "开发商",
        "交房",
        "房产",
        "公积金",
        "违建",
        "拆迁",
        "安置房",
        "装修",
    ),
    "交通出行": (
        "公交",
        "地铁",
        "道路",
        "交通",
        "停车",
        "红绿灯",
        "机动车",
        "非机动车",
        "拥堵",
        "出租车",
    ),
    "城市管理": (
        "城管",
        "占道",
        "违停",
        "摊贩",
        "路灯",
        "市容",
        "垃圾",
        "施工",
        "噪音",
        "噪声",
    ),
    "环境保护": ("污染", "环保", "废气", "河道", "污水", "异味", "空气", "排放"),
    "教育服务": ("学校", "教育", "入学", "学区", "幼儿园", "教师", "培训机构", "招生"),
    "医疗卫生": ("医院", "医疗", "医保", "卫生", "看病", "药品", "诊所", "疫苗"),
    "社会保障": ("社保", "养老", "低保", "救助", "残疾", "退休", "保障"),
    "劳动就业": ("工资", "欠薪", "劳动", "就业", "加班", "社保缴纳", "劳动合同"),
    "市场监管": ("消费", "退款", "商家", "价格", "市场监管", "食品", "质量", "欺诈"),
    "公共安全": ("消防", "安全", "公安", "治安", "危险", "报警", "诈骗"),
    "政务服务": ("办证", "审批", "政务", "窗口", "户口", "热线", "不作为"),
    "文旅消费": ("旅游", "景区", "酒店", "文化", "演出", "文旅"),
}


@dataclass(frozen=True)
class RuleResult:
    topic: str
    keywords: str
    summary: str
    urgency: str
    confidence: float


def classify_by_rule(title: str, content: str) -> RuleResult:
    text = f"{title}\n{content}"
    scores: dict[str, list[str]] = {}
    for topic, terms in TOPIC_RULES.items():
        hits = [term for term in terms if term in text]
        if hits:
            scores[topic] = hits

    if not scores:
        topic, hits, confidence = "其他", [], 0.2
    else:
        topic, hits = max(scores.items(), key=lambda item: (len(item[1]), item[0]))
        confidence = min(0.35 + 0.12 * len(hits), 0.85)

    urgent_terms = ("紧急", "生命", "危险", "火灾", "漏电", "坍塌", "无法居住")
    urgency = "较急" if any(term in text for term in urgent_terms) else "一般"
    summary = title.strip()[:100] if title.strip() else content.strip()[:100]
    return RuleResult(
        topic=topic,
        keywords="、".join(hits[:5]),
        summary=summary,
        urgency=urgency,
        confidence=confidence,
    )

