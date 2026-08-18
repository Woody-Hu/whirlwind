"""Sandbox transport endpoint parsing / formatting."""

from __future__ import annotations

import pytest

from argus.transport import Endpoint, parse_endpoint, parse_url


def test_parse_bare_endpoints() -> None:
    assert parse_endpoint("tcp://127.0.0.1:8411") == Endpoint("tcp", "127.0.0.1", 8411)
    assert parse_endpoint("vsock://2:12345") == Endpoint("vsock", "2", 12345)
    assert parse_endpoint("unix:///tmp/argus.sock") == Endpoint("unix", "/tmp/argus.sock")


def test_parse_url_tcp() -> None:
    endpoint, base = parse_url("http://127.0.0.1:8410")
    assert endpoint == Endpoint("tcp", "127.0.0.1", 8410)
    assert base == ""
    endpoint, base = parse_url("http://127.0.0.1:8410/some/base")
    assert endpoint == Endpoint("tcp", "127.0.0.1", 8410)
    assert base == "/some/base"


def test_parse_url_bare_authority_defaults_to_tcp() -> None:
    endpoint, base = parse_url("127.0.0.1:8410")
    assert endpoint == Endpoint("tcp", "127.0.0.1", 8410)
    assert base == ""


def test_parse_url_unix() -> None:
    endpoint, base = parse_url("http+unix:///tmp/argus.sock")
    assert endpoint == Endpoint("unix", "/tmp/argus.sock")
    assert base == ""
    # the socket path is the whole path; agent routes are appended by callers
    endpoint, base = parse_url("http+unix:///tmp/argus.sock/ingest")
    assert endpoint == Endpoint("unix", "/tmp/argus.sock/ingest")
    assert base == ""


def test_parse_url_vsock() -> None:
    endpoint, base = parse_url("http+vsock://2:12345/secret/llm")
    assert endpoint == Endpoint("vsock", "2", 12345)
    assert base == "/secret/llm"


def test_authority_and_roundtrip() -> None:
    endpoint = Endpoint("vsock", "2", 7000)
    assert endpoint.authority() == "2:7000"
    assert parse_endpoint(endpoint.to_url()) == endpoint
    assert parse_url(f"http+{endpoint.to_url()}")[0] == endpoint


@pytest.mark.parametrize(
    "bad",
    ["ftp://x:1", "tcp://noport", "unix://", "http://:nope", "vsock://2:port"],
)
def test_parse_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_url(bad)
