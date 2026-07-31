from __future__ import annotations

import re


PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号已脱敏]"),
    (re.compile(r"(?<!\d)\d{3,4}-?\d{7,8}(?!\d)"), "[电话已脱敏]"),
    (
        re.compile(r"(?<![A-Za-z0-9])\d{6}(?:19|20)\d{2}[01]\d[0-3]\d\d{3}[\dXx](?![A-Za-z0-9])"),
        "[身份证号已脱敏]",
    ),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[邮箱已脱敏]"),
    (re.compile(r"(?<!\d)(?:62|4\d|5[1-5])\d{14,17}(?!\d)"), "[银行卡号已脱敏]"),
    (
        re.compile(r"(?<![A-Z0-9])[\u4e00-\u9fff][A-Z][A-Z0-9]{5}(?![A-Z0-9])"),
        "[车牌号已脱敏]",
    ),
    (
        re.compile(r"(姓名|联系人|业主|当事人)\s*[:：]\s*[\u4e00-\u9fff·]{2,8}"),
        r"\1：[姓名已脱敏]",
    ),
    (re.compile(r"(地址|住址|家庭住址)\s*[:：]\s*[^，。,；;\n]{4,60}"), r"\1：[地址已脱敏]"),
    (
        re.compile(
            r"[\u4e00-\u9fff]{2,20}(?:路|街|巷|道|弄)\d{1,5}号"
            r"(?:[^，。,；;\n]{0,25}(?:栋|幢|单元|室))?"
        ),
        "[详细地址已脱敏]",
    ),
]


def redact_text(value: object | None) -> str:
    if value is None:
        return ""
    text = str(value)
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def contains_sensitive_data(value: object | None) -> bool:
    if value is None:
        return False
    text = str(value)
    return any(pattern.search(text) for pattern, _ in PATTERNS)


def assert_safe_for_external_model(*values: object | None) -> None:
    unsafe = [index for index, value in enumerate(values) if contains_sensitive_data(value)]
    if unsafe:
        raise ValueError(f"外部模型请求仍含未脱敏个人信息，字段序号：{unsafe}")
