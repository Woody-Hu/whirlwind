"""SandboxAgent stdlib HTTP plumbing: header parsing edge cases.

Regression guard for the empty-header bug: splitting a request head on CRLF
yields a trailing empty line, which once materialized as a `:` header and was
forwarded verbatim by the LLM relay — uvicorn/h11 rejects such requests with
400, surfacing to users as `turn ended with reason=error`.
"""

from __future__ import annotations

from whirlwind.agent.server import _parse_header_lines


def test_no_empty_header_from_trailing_crlf() -> None:
    lines = [
        "Host: 127.0.0.1:8000",
        "Content-Type: application/json",
        "Content-Length: 75",
        "Connection: close",
        "",  # the artifact of splitting the raw head on \r\n
    ]
    headers = _parse_header_lines(lines)
    assert headers == {
        "host": "127.0.0.1:8000",
        "content-type": "application/json",
        "content-length": "75",
        "connection": "close",
    }
    assert "" not in headers  # the empty name must never exist


def test_blank_lines_and_colon_only_lines_skipped() -> None:
    headers = _parse_header_lines(["", ":", "Accept-Encoding: identity", ""])
    assert headers == {"accept-encoding": "identity"}
