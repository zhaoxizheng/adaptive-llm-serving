from __future__ import annotations

import io
import json
import socket
import urllib.error
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from src.openai_stream import (
    OpenAIHTTPError,
    OpenAIProtocolError,
    OpenAIStreamTimeout,
    SSEEvent,
    SSEJSONError,
    SSELimitError,
    SSEParser,
    extract_openai_delta,
    http_json,
    iter_openai_chunks,
    iter_sse_events,
    iter_sse_json,
    stream_openai,
    stream_json,
    stream_sse,
)


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        reason: str = "OK",
        read_sizes: list[int] | None = None,
    ) -> None:
        self._body = io.BytesIO(body)
        self.status = status
        self.reason = reason
        self.read_sizes = read_sizes if read_sizes is not None else []
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._body.read(size)

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


class TimeoutResponse(FakeResponse):
    def __init__(self, body: bytes = b"", *, fail_read: bool = False) -> None:
        super().__init__(body)
        self.fail_read = fail_read
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def read(self, size: int = -1) -> bytes:
        if self.fail_read:
            raise socket.timeout("read timed out")
        return super().read(size)


class NestedSocketResponse(FakeResponse):
    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.timeouts: list[float] = []
        socket_like = type("SocketLike", (), {"settimeout": self.timeouts.append})()
        raw = type("RawLike", (), {"_sock": socket_like})()
        buffer = type("BufferLike", (), {"raw": raw})()
        self.fp = type("HTTPResponseLike", (), {"fp": buffer})()


def _one_byte_chunks(value: str) -> Iterator[bytes]:
    for byte in value.encode("utf-8"):
        yield bytes([byte])


def test_sse_parser_handles_every_byte_boundary_utf8_and_crlf() -> None:
    stream = (
        "\ufeff: heartbeat\r\n"
        "id: 42\r\n"
        "event: token\r\n"
        "retry: 0010\r\n"
        "data: hello 😄\r\n"
        "data: second: value\r\n"
        "\r\n"
    )

    assert list(iter_sse_events(_one_byte_chunks(stream))) == [
        SSEEvent(
            data="hello 😄\nsecond: value",
            event="token",
            id="42",
            retry=10,
        )
    ]


def test_sse_parser_supports_mixed_line_endings_and_eof_flush() -> None:
    chunks = [
        b"id: old\rdata: one\ndata:\r\n\r",
        b"event: final\rdata: eof",
    ]

    assert list(iter_sse_events(chunks)) == [
        SSEEvent(data="one\n", id="old"),
        SSEEvent(data="eof", event="final", id="old"),
    ]


def test_sse_parser_ignores_comments_unknown_fields_and_invalid_metadata() -> None:
    raw = (
        b"id: kept\nid: bad\x00id\nretry: -1\nretry: 12x\n"
        b": ignore me\nunknown: value\ndata:value\n\n"
        b"data: next\n\n"
    )

    assert list(iter_sse_events([raw])) == [
        SSEEvent(data="value", id="kept"),
        SSEEvent(data="next", id="kept"),
    ]


def test_sse_parser_does_not_dispatch_field_only_blocks() -> None:
    raw = b"event: ignored\nid: persistent\nretry: 50\n\ndata: yes\n\n"

    assert list(iter_sse_events([raw])) == [
        SSEEvent(data="yes", id="persistent", retry=50)
    ]


def test_sse_parser_close_is_idempotent_and_feed_after_close_fails() -> None:
    parser = SSEParser()
    assert parser.feed(b"data: trailing") == []
    assert parser.close() == [SSEEvent(data="trailing")]
    assert parser.close() == []
    with pytest.raises(ValueError, match="closed"):
        parser.feed(b"data: late\n\n")


@pytest.mark.parametrize(
    ("chunks", "exception"),
    [
        ([b"data: ", b"\xf0\x9f"], UnicodeDecodeError),
        ([b"data: ", b"\xff\n\n"], UnicodeDecodeError),
    ],
)
def test_sse_parser_rejects_incomplete_and_invalid_utf8(
    chunks: list[bytes], exception: type[Exception]
) -> None:
    with pytest.raises(exception):
        list(iter_sse_events(chunks))


def test_sse_parser_enforces_line_and_event_bounds() -> None:
    with pytest.raises(SSELimitError, match="line exceeds"):
        list(iter_sse_events([b"data: long"], max_line_bytes=4))
    with pytest.raises(SSELimitError, match="event exceeds"):
        list(
            iter_sse_events(
                [b"data: a\ndata: b\n\n"],
                max_line_bytes=20,
                max_event_bytes=10,
            )
        )


def test_iter_sse_json_stops_at_done() -> None:
    raw = b'data: {"value":1}\n\ndata: [DONE]\n\ndata: {"value":2}\n\n'

    assert list(iter_sse_json([raw])) == [{"value": 1}]


def test_iter_sse_json_rejects_malformed_or_non_object_data() -> None:
    with pytest.raises(SSEJSONError, match="invalid JSON"):
        list(iter_sse_json([b"data: nope\n\n"]))
    with pytest.raises(SSEJSONError, match="must be an object"):
        list(iter_sse_json([b"data: []\n\n"]))


def test_openai_extraction_preserves_all_fragment_types_usage_and_done() -> None:
    payload = {
        "choices": [
            {
                "index": 1,
                "delta": {
                    "content": "answer",
                    "reasoning_content": "think",
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {"name": "lookup", "arguments": '{"q":'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            },
            {"index": 2, "delta": {"reasoning": "fallback"}},
        ],
        "usage": {"completion_tokens": 3},
    }
    raw = (
        "data: "
        + json.dumps(payload)
        + "\n\n"
        + "data: [DONE]\n\n"
        + 'data: {"choices": [{"delta": {"content": "ignored"}}]}\n\n'
    ).encode()

    chunks = list(iter_openai_chunks(_one_byte_chunks(raw.decode())))

    assert len(chunks) == 2
    assert chunks[0].content == "answer"
    assert chunks[0].reasoning == "think"
    assert chunks[0].choice_index == 1
    assert chunks[0].finish_reason == "tool_calls"
    assert chunks[0].tool_calls[0]["function"]["arguments"] == '{"q":'
    assert chunks[0].choices[1].reasoning == "fallback"
    assert chunks[0].usage == {"completion_tokens": 3}
    assert chunks[1].done is True
    assert chunks[1].raw is None


def test_openai_usage_only_and_flat_extraction() -> None:
    payload = {"choices": [], "usage": {"total_tokens": 9}}
    assert extract_openai_delta(payload) == {
        "choice_index": 0,
        "content": None,
        "reasoning": None,
        "tool_calls": [],
        "finish_reason": None,
        "usage": {"total_tokens": 9},
    }


def test_openai_completions_text_is_used_only_without_delta_mapping() -> None:
    chunks = list(
        iter_openai_chunks(
            [
                b'data: {"choices": ['
                b'{"index": 0, "text": "completion"},'
                b'{"index": 1, "text": "ignored", "delta": {}}]}\n\n'
            ]
        )
    )

    assert [choice.content for choice in chunks[0].choices] == ["completion", None]


def test_openai_extraction_rejects_invalid_shapes_without_echoing_payload() -> None:
    secret = "super-secret-token"
    with pytest.raises(OpenAIProtocolError) as raised:
        list(iter_openai_chunks([f'data: {{"choices": "{secret}"}}\n\n'.encode()]))
    assert secret not in str(raised.value)


def test_http_json_posts_utf8_json_with_timeout_and_closes_response() -> None:
    response = FakeResponse('{"ok": true}'.encode())
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return response

    with patch("src.openai_stream.urllib.request.urlopen", fake_urlopen):
        result = http_json(
            "http://localhost:8000/v1/chat/completions",
            {"prompt": "你好"},
            {"X-Test": "yes"},
            timeout=2.5,
            api_key="token-123",
        )

    request = captured["request"]
    assert result == {"ok": True}
    assert json.loads(request.data.decode()) == {"prompt": "你好"}
    assert request.get_method() == "POST"
    assert request.unredirected_hdrs["Authorization"] == "Bearer token-123"
    assert request.get_header("X-test") == "yes"
    assert captured["timeout"] == 2.5
    assert response.closed


def test_authorization_is_unredirected_so_urllib_does_not_forward_it() -> None:
    response = FakeResponse(b"{}")
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        captured["request"] = request
        return response

    with patch("src.openai_stream.urllib.request.urlopen", fake_urlopen):
        http_json("https://example.com/v1/chat", {}, api_key="token-123")

    request = captured["request"]
    assert "Authorization" not in request.headers
    assert request.unredirected_hdrs["Authorization"] == "Bearer token-123"


def test_stream_json_sets_stream_without_mutating_payload_and_closes_at_done() -> None:
    response = FakeResponse(
        b'data: {"choices": []}\n\ndata: [DONE]\n\ndata: {"late": true}\n\n'
    )
    payload = {"model": "test", "stream": False}
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        captured["request"] = request
        return response

    with patch("src.openai_stream.urllib.request.urlopen", fake_urlopen):
        assert list(stream_json("http://localhost/v1/chat", payload, chunk_size=3)) == [
            {"choices": []}
        ]

    assert payload == {"model": "test", "stream": False}
    assert json.loads(captured["request"].data) == {"model": "test", "stream": True}
    assert captured["request"].get_header("Accept") == "text/event-stream"
    assert response.closed


def test_http_errors_are_useful_bounded_and_never_leak_auth_token() -> None:
    token = "highly-secret-token"
    url = f"https://user:{token}@example.com/v1/chat?api_key={token}"
    error = urllib.error.HTTPError(
        url,
        401,
        f"Bearer {token}",
        {},
        io.BytesIO(f"denied: Bearer {token} and {token}".encode()),
    )

    with patch("src.openai_stream.urllib.request.urlopen", side_effect=error):
        with pytest.raises(OpenAIHTTPError) as raised:
            http_json(url, {}, api_key=token)

    exception = raised.value
    assert exception.status == 401
    assert exception.safe_url == "https://example.com/v1/chat"
    assert "HTTP 401" in str(exception)
    assert "denied" in str(exception)
    assert token not in str(exception)
    assert token not in repr(exception.__dict__)


def test_http_error_truncates_large_body_and_redacts_api_key_header() -> None:
    token = "header-secret"
    response = FakeResponse((f"denied {token} " + "x" * 8_000).encode(), status=429)

    with patch("src.openai_stream.urllib.request.urlopen", return_value=response):
        with pytest.raises(OpenAIHTTPError) as raised:
            http_json("https://example.com/v1/chat", {}, {"X-API-Key": token})

    assert raised.value.status == 429
    assert raised.value.body_excerpt is not None
    assert raised.value.body_excerpt.endswith("...")
    assert token not in str(raised.value)
    assert response.closed


@pytest.mark.parametrize(
    ("header_name", "prefix"),
    [
        ("X-API-Key", ""),
        ("Api-Key", ""),
        ("Proxy-Authorization", "Basic "),
        ("Authorization", "Bearer "),
    ],
)
def test_http_error_redacts_all_supported_credential_header_values(
    header_name: str, prefix: str
) -> None:
    secret = f"{header_name.lower()}-secret"
    header_value = prefix + secret
    response = FakeResponse(
        f"{header_name}: {header_value}; diagnostic".encode(),
        status=403,
        reason=f"rejected {header_name}={header_value}",
    )

    with patch("src.openai_stream.urllib.request.urlopen", return_value=response):
        with pytest.raises(OpenAIHTTPError) as raised:
            http_json(
                "https://example.com/v1/chat",
                {},
                {header_name: header_value},
            )

    exception = raised.value
    assert secret not in str(exception)
    assert secret not in repr(exception.__dict__)
    assert "[REDACTED]" in str(exception)


def test_http_error_excerpt_limit_is_configurable_and_never_sse_limit() -> None:
    response = FakeResponse(b"abcdefghij", status=413, reason="Too Large")

    with patch("src.openai_stream.urllib.request.urlopen", return_value=response):
        with pytest.raises(OpenAIHTTPError) as raised:
            http_json(
                "https://example.com/v1/chat",
                {},
                max_error_bytes=4,
            )

    assert raised.value.status == 413
    assert raised.value.body_excerpt == "abcd..."
    assert response.read_sizes == [5]
    assert response.closed


def test_http_error_excerpt_redacts_secret_cut_by_byte_limit() -> None:
    secret = "abcdefghij"
    response = FakeResponse(secret.encode(), status=403)

    with patch("src.openai_stream.urllib.request.urlopen", return_value=response):
        with pytest.raises(OpenAIHTTPError) as raised:
            http_json(
                "https://example.com/v1/chat",
                {},
                {"Api-Key": secret},
                max_error_bytes=4,
            )

    assert raised.value.body_excerpt == "[REDACTED]..."
    assert "abcd" not in str(raised.value)


def test_http_error_redacts_labeled_credentials_without_known_values() -> None:
    secrets = ("x-secret", "api-secret", "proxy-secret", "auth-secret")
    response = FakeResponse(
        (
            "X-API-Key: x-secret Api-Key=api-secret "
            "Proxy-Authorization: Basic proxy-secret "
            "Authorization: Bearer auth-secret"
        ).encode(),
        status=401,
    )

    with patch("src.openai_stream.urllib.request.urlopen", return_value=response):
        with pytest.raises(OpenAIHTTPError) as raised:
            http_json("https://example.com/v1/chat", {})

    assert all(secret not in str(raised.value) for secret in secrets)
    assert str(raised.value).count("[REDACTED]") == 4


def test_urlopen_http_error_excerpt_limit_is_configurable_and_never_sse_limit() -> None:
    error = urllib.error.HTTPError(
        "https://example.com/v1/chat",
        429,
        "Too Many Requests",
        {},
        io.BytesIO(b"abcdefghij"),
    )

    with patch("src.openai_stream.urllib.request.urlopen", side_effect=error):
        with pytest.raises(OpenAIHTTPError) as raised:
            http_json(
                "https://example.com/v1/chat",
                {},
                max_error_bytes=4,
            )

    assert raised.value.status == 429
    assert raised.value.body_excerpt == "abcd..."


def test_error_excerpt_collection_failure_still_raises_http_error() -> None:
    response = FakeResponse(b"", status=502, reason="Bad Gateway")

    def fail_with_sse_limit(size: int = -1) -> bytes:
        raise SSELimitError("diagnostic body too large")

    response.read = fail_with_sse_limit  # type: ignore[method-assign]
    with patch("src.openai_stream.urllib.request.urlopen", return_value=response):
        with pytest.raises(OpenAIHTTPError) as raised:
            http_json("https://example.com/v1/chat", {})

    assert raised.value.status == 502
    assert raised.value.body_excerpt is None
    assert response.closed


def test_stream_sse_enforces_total_response_body_limit() -> None:
    response = FakeResponse(b"data: {}\n\n")

    with patch("src.openai_stream.urllib.request.urlopen", return_value=response):
        with pytest.raises(SSELimitError, match="response exceeds 8 bytes"):
            list(
                stream_sse(
                    "https://example.com/v1/chat",
                    {},
                    chunk_size=64,
                    max_response_bytes=8,
                )
            )

    assert response.read_sizes == [9]
    assert response.closed


def test_stream_high_level_apis_expose_parser_and_body_limits() -> None:
    cases = [
        (stream_json, b'data: {"value":1}\n\n'),
        (stream_openai, b'data: {"choices":[]}\n\n'),
    ]
    for helper, body in cases:
        response = FakeResponse(body)
        with patch("src.openai_stream.urllib.request.urlopen", return_value=response):
            with pytest.raises(SSELimitError, match="response exceeds"):
                list(helper("https://example.com/v1/chat", {}, max_response_bytes=3))
        assert response.closed

        response = FakeResponse(body)
        with patch("src.openai_stream.urllib.request.urlopen", return_value=response):
            with pytest.raises(SSELimitError, match="line exceeds"):
                list(helper("https://example.com/v1/chat", {}, max_line_bytes=4))
        assert response.closed

        response = FakeResponse(body)
        with patch("src.openai_stream.urllib.request.urlopen", return_value=response):
            with pytest.raises(SSELimitError, match="event exceeds"):
                list(
                    helper(
                        "https://example.com/v1/chat",
                        {},
                        max_line_bytes=100,
                        max_event_bytes=4,
                    )
                )
        assert response.closed


def test_stream_high_level_apis_expose_error_excerpt_limit() -> None:
    for helper in (stream_sse, stream_json, stream_openai):
        response = FakeResponse(b"abcdefghij", status=503, reason="Unavailable")
        with patch("src.openai_stream.urllib.request.urlopen", return_value=response):
            with pytest.raises(OpenAIHTTPError) as raised:
                list(helper("https://example.com/v1/chat", {}, max_error_bytes=4))
        assert raised.value.body_excerpt == "abcd..."
        assert response.closed


def test_stream_total_duration_includes_connection_time() -> None:
    response = FakeResponse(b"data: {}\n\n")

    with (
        patch("src.openai_stream.time.monotonic", side_effect=[10.0, 10.0, 12.0]),
        patch(
            "src.openai_stream.urllib.request.urlopen", return_value=response
        ) as opened,
    ):
        with pytest.raises(OpenAIStreamTimeout, match="exceeded 1 seconds"):
            list(
                stream_sse(
                    "https://example.com/v1/chat",
                    {},
                    timeout=30,
                    max_duration=1,
                )
            )

    assert opened.call_args.kwargs["timeout"] == 1
    assert response.closed


def test_stream_connection_timeout_at_total_deadline_is_stream_timeout() -> None:
    with (
        patch("src.openai_stream.time.monotonic", side_effect=[10.0, 10.0]),
        patch(
            "src.openai_stream.urllib.request.urlopen",
            side_effect=socket.timeout("connect timed out"),
        ) as opened,
    ):
        with pytest.raises(OpenAIStreamTimeout, match="exceeded 1 seconds"):
            list(
                stream_sse(
                    "https://example.com/v1/chat",
                    {},
                    timeout=30,
                    max_duration=1,
                )
            )

    assert opened.call_args.kwargs["timeout"] == 1


def test_stream_read_timeout_uses_remaining_total_duration() -> None:
    response = TimeoutResponse(fail_read=True)

    with (
        patch(
            "src.openai_stream.time.monotonic",
            side_effect=[10.0, 10.0, 10.25, 10.25],
        ),
        patch("src.openai_stream.urllib.request.urlopen", return_value=response),
    ):
        with pytest.raises(OpenAIStreamTimeout, match="exceeded 1 seconds"):
            list(
                stream_sse(
                    "https://example.com/v1/chat",
                    {},
                    timeout=30,
                    max_duration=1,
                )
            )

    assert response.timeouts == pytest.approx([0.75, 0.75])
    assert response.closed


def test_stream_tightens_realistic_nested_stdlib_socket_timeout() -> None:
    response = NestedSocketResponse(b"")

    with (
        patch(
            "src.openai_stream.time.monotonic",
            side_effect=[10.0, 10.0, 10.25, 10.25, 10.5],
        ),
        patch("src.openai_stream.urllib.request.urlopen", return_value=response),
    ):
        assert (
            list(
                stream_sse(
                    "https://example.com/v1/chat",
                    {},
                    timeout=30,
                    max_duration=1,
                )
            )
            == []
        )

    assert response.timeouts == pytest.approx([0.75, 0.75])
    assert response.closed


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), None])
def test_http_helpers_require_bounded_timeout(timeout: Any) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        http_json("http://localhost/test", {}, timeout=timeout)


@pytest.mark.parametrize("value", [0, -1, True, 1.5, None])
@pytest.mark.parametrize(
    "name",
    [
        "chunk_size",
        "max_response_bytes",
        "max_error_bytes",
        "max_line_bytes",
        "max_event_bytes",
    ],
)
def test_stream_limits_require_positive_integers(name: str, value: Any) -> None:
    with pytest.raises(ValueError, match=f"{name} must be a positive integer"):
        list(stream_sse("http://localhost/test", {}, **{name: value}))
