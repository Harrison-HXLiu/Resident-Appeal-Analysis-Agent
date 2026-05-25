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
    (re.compile(r"(地址|住址|家庭住址)\s*[:：]\s*[^，。,；;\n]{4,60}"), r"\1：[地址已脱敏]"),
]


def redact_text(value: object | None) -> str:
    if value is None:
        return ""
    text = str(value)
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text

