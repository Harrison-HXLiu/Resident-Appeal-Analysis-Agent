from __future__ import annotations

import socket

import pytest

from app.services.policy_ingest import _validate_public_https_url


def test_policy_url_rejects_non_https() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        _validate_public_https_url("http://www.gov.cn/example")


def test_policy_url_rejects_private_dns_result(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(ValueError, match="内网或保留地址"):
        _validate_public_https_url("https://policy.example.test/document")
