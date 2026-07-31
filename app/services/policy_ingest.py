from __future__ import annotations

import ipaddress
import socket
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx


MAX_POLICY_BYTES = 12 * 1024 * 1024
MAX_POLICY_REDIRECTS = 5


def extract_policy_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".docx":
        import docx2txt

        return docx2txt.process(str(path)) or ""
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ValueError("政策材料仅支持 PDF、DOCX、TXT 和 Markdown")


def _validate_public_https_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("政策链接必须是有效的 HTTPS 官方地址")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            raise ValueError("政策链接不能指向内网或保留地址")


def fetch_policy_url(url: str) -> tuple[str, bytes, str]:
    current_url = url
    with httpx.Client(
        timeout=20,
        follow_redirects=False,
        headers={"User-Agent": "ResidentAppealResearch/1.0"},
    ) as client:
        response: httpx.Response | None = None
        content = b""
        for redirect_count in range(MAX_POLICY_REDIRECTS + 1):
            _validate_public_https_url(current_url)
            with client.stream("GET", current_url) as streamed:
                if streamed.is_redirect:
                    location = streamed.headers.get("location")
                    if not location:
                        raise ValueError("政策链接返回了无目标的重定向")
                    if redirect_count >= MAX_POLICY_REDIRECTS:
                        raise ValueError("政策链接重定向次数过多")
                    current_url = urljoin(current_url, location)
                    continue
                streamed.raise_for_status()
                declared_size = streamed.headers.get("content-length")
                if declared_size and int(declared_size) > MAX_POLICY_BYTES:
                    raise ValueError("政策材料超过12MB限制")
                chunks: list[bytes] = []
                received = 0
                for chunk in streamed.iter_bytes():
                    received += len(chunk)
                    if received > MAX_POLICY_BYTES:
                        raise ValueError("政策材料超过12MB限制")
                    chunks.append(chunk)
                content = b"".join(chunks)
                response = streamed
                break
        if response is None:
            raise ValueError("无法取得政策材料")
        content_type = response.headers.get("content-type", "").lower()
        final_url = str(response.url)
        if "pdf" in content_type or urlparse(final_url).path.lower().endswith(".pdf"):
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            return text, content, ".pdf"
        if "text/" not in content_type and "html" not in content_type:
            raise ValueError("政策链接返回了不支持的文件类型")
        encoding = response.encoding or "utf-8"
        text = content.decode(encoding, errors="replace")
        if "html" in content_type:
            from html.parser import HTMLParser

            class TextExtractor(HTMLParser):
                def __init__(self) -> None:
                    super().__init__()
                    self.parts: list[str] = []
                    self.ignored = 0

                def handle_starttag(self, tag: str, attrs) -> None:
                    if tag in {"script", "style", "nav"}:
                        self.ignored += 1

                def handle_endtag(self, tag: str) -> None:
                    if tag in {"script", "style", "nav"} and self.ignored:
                        self.ignored -= 1

                def handle_data(self, data: str) -> None:
                    if not self.ignored and data.strip():
                        self.parts.append(data.strip())

            parser = TextExtractor()
            parser.feed(text)
            text = "\n".join(parser.parts)
        return text, content, ".html"
