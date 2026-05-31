from __future__ import annotations

import bleach
import markdown as markdown_lib


ALLOWED_TAGS = [
    "a",
    "blockquote",
    "br",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
]
ALLOWED_ATTRIBUTES = {"a": ["href", "title", "target", "rel"], "th": ["align"], "td": ["align"]}


def render_markdown(text: str | None) -> str:
    raw_html = markdown_lib.markdown(
        text or "",
        extensions=["extra", "sane_lists", "nl2br"],
        output_format="html5",
    )
    return bleach.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=["http", "https", "mailto"],
        strip=True,
    )
