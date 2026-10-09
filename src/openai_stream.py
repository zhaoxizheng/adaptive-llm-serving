from __future__ import annotations

import codecs
import json
import math
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Iterable, Iterator, Mapping


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_STREAM_DURATION_SECONDS = 300.0
DEFAULT_MAX_RESPONSE_BYTES = 16 << 20
DEFAULT_MAX_ERROR_BYTES = 4 << 10
DEFAULT_MAX_SSE_LINE_BYTES = 1 << 20
DEFAULT_MAX_SSE_EVENT_BYTES = 8 << 20
DEFAULT_READ_SIZE = 16 << 10


class SSEError(ValueError):
    """Base class for malformed or unsafe SSE input."""


class SSELimitError(SSEError):
    """Raised when an SSE line or event exceeds a configured bound."""


class SSEJSONError(SSEError):
    """Raised when an SSE data field is not a JSON object."""


class OpenAIProtocolError(SSEError):
    """Raised when a JSON event is not a valid OpenAI streaming chunk."""


class OpenAIHTTPError(RuntimeError):
    """A sanitized HTTP or transport failure."""

    def __init__(
        self,
        message: str,
        *,
        safe_url: str,
        status: int | None = None,
        reason: str | None = None,
        body_excerpt: str | None = None,
    ) -> None:
        super().__init__(message)
        self.safe_url = safe_url
        self.status = status
        self.reason = reason
        self.body_excerpt = body_excerpt


class OpenAIStreamTimeout(TimeoutError):
    """Raised when the total duration allowed for an SSE stream expires."""


@dataclass(frozen=True, slots=True)
class SSEEvent:
    data: str
    event: str | None = None
    id: str | None = None
    retry: int | None = None


@dataclass(frozen=True, slots=True)
class OpenAIChoiceDelta:
    index: int
    content: str | None = None
    reasoning: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class OpenAIStreamChunk:
    choices: tuple[OpenAIChoiceDelta, ...] = ()
    usage: dict[str, Any] | None = None
    done: bool = False
    event: SSEEvent | None = None
    raw: dict[str, Any] | None = None

    @property
    def content(self) -> str | None:
        return self.choices[0].content if self.choices else None

    @property
    def reasoning(self) -> str | None:
        return self.choices[0].reasoning if self.choices else None

    @property
    def tool_calls(self) -> tuple[dict[str, Any], ...]:
        return self.choices[0].tool_calls if self.choices else ()

    @property
    def finish_reason(self) -> str | None:
        return self.choices[0].finish_reason if self.choices else None

    @property
    def choice_index(self) -> int | None:
        return self.choices[0].index if self.choices else None


# A descriptive alternative for clients that think of each decoded SSE item as an event.
OpenAIStreamEvent = OpenAIStreamChunk


class SSEParser:
    """从任意切分的网络字节增量解析 SSE；网络分块不等于字符、行或事件边界。"""

    def __init__(
        self,
        *,
        max_line_bytes: int = DEFAULT_MAX_SSE_LINE_BYTES,
        max_event_bytes: int = DEFAULT_MAX_SSE_EVENT_BYTES,
    ) -> None:
        self._max_line_bytes = _positive_int("max_line_bytes", max_line_bytes)
        self._max_event_bytes = _positive_int("max_event_bytes", max_event_bytes)
        # UTF-8 字符可能跨网络分块；增量解码器保存未完整的字节，禁止逐块独立 decode。
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._line: list[str] = []
        self._line_bytes = 0
        self._event_bytes = 0
        self._pending_cr = False
        self._at_start = True
        self._closed = False
        self._data_lines: list[str] = []
        self._event_type: str | None = None
        self._last_event_id: str | None = None
        self._retry: int | None = None

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[SSEEvent]:
        if self._closed:
            raise ValueError("cannot feed a closed SSE parser")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("SSE chunks must be bytes-like")
        events: list[SSEEvent] = []
        view = memoryview(chunk).cast("B")
        for offset in range(0, len(view), DEFAULT_READ_SIZE):
            text = self._decoder.decode(
                bytes(view[offset : offset + DEFAULT_READ_SIZE]),
                final=False,
            )
            events.extend(self._consume_text(text))
        return events

    def close(self) -> list[SSEEvent]:
        if self._closed:
            return []
        self._closed = True
        events = self._consume_text(self._decoder.decode(b"", final=True))
        if self._pending_cr:
            self._pending_cr = False
            events.extend(self._finish_line())
        elif self._line:
            events.extend(self._finish_line())
        trailing = self._dispatch_event()
        if trailing is not None:
            events.append(trailing)
        return events

    def _consume_text(self, text: str) -> list[SSEEvent]:
        if self._at_start and text:
            self._at_start = False
            if text.startswith("\ufeff"):
                text = text[1:]
        events: list[SSEEvent] = []
        for character in text:
            if self._pending_cr:
                self._pending_cr = False
                events.extend(self._finish_line())
                if character == "\n":
                    continue
            if character == "\r":
                self._pending_cr = True
            elif character == "\n":
                events.extend(self._finish_line())
            else:
                self._line.append(character)
                self._line_bytes += len(character.encode("utf-8"))
                if self._line_bytes > self._max_line_bytes:
                    raise SSELimitError(
                        f"SSE line exceeds {self._max_line_bytes} bytes"
                    )
        return events

    def _finish_line(self) -> list[SSEEvent]:
        line = "".join(self._line)
        line_bytes = self._line_bytes
        self._line.clear()
        self._line_bytes = 0
        # 空行表示 SSE 事件边界；多条 data 行会合并为同一个事件。
        if not line:
            event = self._dispatch_event()
            self._event_bytes = 0
            return [event] if event is not None else []

        self._event_bytes += line_bytes + 1
        if self._event_bytes > self._max_event_bytes:
            raise SSELimitError(f"SSE event exceeds {self._max_event_bytes} bytes")
        if line.startswith(":"):
            return []

        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]

        if field == "data":
            self._data_lines.append(value)
        elif field == "event":
            self._event_type = value
        elif field == "id" and "\x00" not in value:
            self._last_event_id = value
        elif field == "retry" and value and value.isascii() and value.isdecimal():
            try:
                self._retry = int(value)
            except ValueError:
                # Python bounds decimal-to-int conversion; an impractically large
                # retry value is no more useful than any other invalid value.
                pass
        return []

    def _dispatch_event(self) -> SSEEvent | None:
        if not self._data_lines:
            self._event_type = None
            return None
        event = SSEEvent(
            data="\n".join(self._data_lines),
            event=self._event_type,
            id=self._last_event_id,
            retry=self._retry,
        )
        self._data_lines.clear()
        self._event_type = None
        return event


def iter_sse_events(
    chunks: Iterable[bytes],
    *,
    max_line_bytes: int = DEFAULT_MAX_SSE_LINE_BYTES,
    max_event_bytes: int = DEFAULT_MAX_SSE_EVENT_BYTES,
) -> Iterator[SSEEvent]:
    parser = SSEParser(
        max_line_bytes=max_line_bytes,
        max_event_bytes=max_event_bytes,
    )
    for chunk in chunks:
        yield from parser.feed(chunk)
    yield from parser.close()


parse_sse_events = iter_sse_events


def iter_sse_json(
    chunks: Iterable[bytes],
    stop_on_done: bool = True,
    *,
    max_line_bytes: int = DEFAULT_MAX_SSE_LINE_BYTES,
    max_event_bytes: int = DEFAULT_MAX_SSE_EVENT_BYTES,
) -> Iterator[dict[str, Any]]:
    events = iter_sse_events(
        chunks,
        max_line_bytes=max_line_bytes,
        max_event_bytes=max_event_bytes,
    )
    try:
        for event in events:
            if event.data.strip() == "[DONE]":
                if stop_on_done:
                    return
                continue
            try:
                value = json.loads(event.data)
            except json.JSONDecodeError as exc:
                raise SSEJSONError(
                    f"invalid JSON in SSE event at line {exc.lineno}, column {exc.colno}"
                ) from None
            if not isinstance(value, dict):
                raise SSEJSONError("SSE JSON data must be an object")
            yield value
    finally:
        events.close()


decode_sse_json = iter_sse_json


def extract_openai_chunk(payload: Mapping[str, Any]) -> OpenAIStreamChunk:
    if not isinstance(payload, Mapping):
        raise OpenAIProtocolError("OpenAI stream payload must be an object")
    raw_choices = payload.get("choices", [])
    if not isinstance(raw_choices, list):
        raise OpenAIProtocolError("OpenAI stream payload 'choices' must be a list")

    choices: list[OpenAIChoiceDelta] = []
    for position, raw_choice in enumerate(raw_choices):
        if not isinstance(raw_choice, Mapping):
            raise OpenAIProtocolError("each OpenAI stream choice must be an object")
        index = raw_choice.get("index", position)
        if not isinstance(index, int) or isinstance(index, bool):
            raise OpenAIProtocolError("OpenAI stream choice index must be an integer")
        raw_delta_value = raw_choice.get("delta")
        has_delta = isinstance(raw_delta_value, Mapping)
        raw_delta = raw_delta_value
        if raw_delta is None:
            raw_delta = {}
        if not isinstance(raw_delta, Mapping):
            raise OpenAIProtocolError("OpenAI stream choice delta must be an object")

        content = raw_delta.get("content") if has_delta else raw_choice.get("text")
        if content is not None and not isinstance(content, str):
            raise OpenAIProtocolError("OpenAI stream content must be text or null")

        reasoning = raw_delta.get("reasoning_content")
        if reasoning is None and "reasoning_content" not in raw_delta:
            reasoning = raw_delta.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, str):
            raise OpenAIProtocolError("OpenAI stream reasoning must be text or null")

        raw_tool_calls = raw_delta.get("tool_calls", [])
        if raw_tool_calls is None:
            raw_tool_calls = []
        if not isinstance(raw_tool_calls, list) or any(
            not isinstance(tool_call, Mapping) for tool_call in raw_tool_calls
        ):
            raise OpenAIProtocolError(
                "OpenAI stream tool_calls must be a list of objects"
            )
        finish_reason = raw_choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise OpenAIProtocolError("OpenAI finish_reason must be text or null")
        choices.append(
            OpenAIChoiceDelta(
                index=index,
                content=content,
                reasoning=reasoning,
                tool_calls=tuple(dict(tool_call) for tool_call in raw_tool_calls),
                finish_reason=finish_reason,
            )
        )

    raw_usage = payload.get("usage")
    if raw_usage is not None and not isinstance(raw_usage, Mapping):
        raise OpenAIProtocolError("OpenAI stream usage must be an object or null")
    return OpenAIStreamChunk(
        choices=tuple(choices),
        usage=dict(raw_usage) if raw_usage is not None else None,
        raw=dict(payload),
    )


def extract_openai_delta(
    payload: Mapping[str, Any],
    choice_index: int = 0,
) -> dict[str, Any]:
    """Return convenient flat fields for one choice without assembling fragments."""

    chunk = extract_openai_chunk(payload)
    choice = next((item for item in chunk.choices if item.index == choice_index), None)
    return {
        "choice_index": choice_index,
        "content": choice.content if choice is not None else None,
        "reasoning": choice.reasoning if choice is not None else None,
        "tool_calls": list(choice.tool_calls) if choice is not None else [],
        "finish_reason": choice.finish_reason if choice is not None else None,
        "usage": chunk.usage,
    }


def parse_openai_event(event: SSEEvent) -> OpenAIStreamChunk:
    # [DONE] 是协议结束标记，不是 JSON；其他事件先校验 JSON 和 OpenAI 字段类型。
    if event.data.strip() == "[DONE]":
        return OpenAIStreamChunk(done=True, event=event)
    try:
        payload = json.loads(event.data)
    except json.JSONDecodeError as exc:
        raise OpenAIProtocolError(
            f"invalid OpenAI stream JSON at line {exc.lineno}, column {exc.colno}"
        ) from None
    if not isinstance(payload, dict):
        raise OpenAIProtocolError("OpenAI stream JSON must be an object")
    return replace(extract_openai_chunk(payload), event=event)


def iter_openai_events(events: Iterable[SSEEvent]) -> Iterator[OpenAIStreamChunk]:
    iterator = iter(events)
    try:
        for event in iterator:
            chunk = parse_openai_event(event)
            yield chunk
            if chunk.done:
                return
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()


def iter_openai_chunks(
    chunks: Iterable[bytes],
    *,
    max_line_bytes: int = DEFAULT_MAX_SSE_LINE_BYTES,
    max_event_bytes: int = DEFAULT_MAX_SSE_EVENT_BYTES,
) -> Iterator[OpenAIStreamChunk]:
    yield from iter_openai_events(
        iter_sse_events(
            chunks,
            max_line_bytes=max_line_bytes,
            max_event_bytes=max_event_bytes,
        )
    )


iter_openai_stream = iter_openai_chunks


def http_json(
    url: str,
    payload: Any = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    api_key: str | None = None,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_error_bytes: int = DEFAULT_MAX_ERROR_BYTES,
) -> dict[str, Any] | list[Any]:
    timeout = _positive_finite("timeout", timeout)
    max_response_bytes = _positive_int("max_response_bytes", max_response_bytes)
    max_error_bytes = _positive_int("max_error_bytes", max_error_bytes)
    # 无 payload 时发送 GET，否则发送 JSON POST；读取有字节上限，异常也会关闭连接。
    body = None if payload is None else _json_bytes(payload)
    request = _request(
        url,
        data=body,
        headers=_request_headers(headers, api_key, accept="application/json"),
        method="GET" if payload is None else "POST",
    )
    response, secrets = _open(
        request,
        timeout,
        api_key=api_key,
        headers=headers,
        max_error_bytes=max_error_bytes,
    )
    try:
        _raise_for_status(response, url, secrets, max_error_bytes=max_error_bytes)
        raw = _read_limited(response, max_response_bytes, "HTTP response")
    except OpenAIHTTPError:
        raise
    except SSELimitError as exc:
        safe_url = _sanitized_url(url, secrets)
        raise OpenAIHTTPError(
            f"HTTP response from {safe_url} exceeds {max_response_bytes} bytes",
            safe_url=safe_url,
        ) from exc
    except (TimeoutError, socket.timeout, OSError) as exc:
        raise _transport_error(url, exc, secrets) from None
    finally:
        response.close()

    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        location = getattr(exc, "pos", None)
        detail = f" at byte/character {location}" if location is not None else ""
        safe_url = _sanitized_url(url, secrets)
        raise OpenAIHTTPError(
            f"invalid JSON response from {safe_url}{detail}",
            safe_url=safe_url,
        ) from None
    if not isinstance(result, (dict, list)):
        safe_url = _sanitized_url(url, secrets)
        raise OpenAIHTTPError(
            f"JSON response from {safe_url} must be an object or array",
            safe_url=safe_url,
        )
    return result


post_json = http_json


def stream_sse(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    api_key: str | None = None,
    chunk_size: int = DEFAULT_READ_SIZE,
    max_duration: float = DEFAULT_STREAM_DURATION_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_error_bytes: int = DEFAULT_MAX_ERROR_BYTES,
    max_line_bytes: int = DEFAULT_MAX_SSE_LINE_BYTES,
    max_event_bytes: int = DEFAULT_MAX_SSE_EVENT_BYTES,
) -> Iterator[SSEEvent]:
    timeout = _positive_finite("timeout", timeout)
    max_duration = _positive_finite("max_duration", max_duration)
    chunk_size = _positive_int("chunk_size", chunk_size)
    max_response_bytes = _positive_int("max_response_bytes", max_response_bytes)
    max_error_bytes = _positive_int("max_error_bytes", max_error_bytes)
    max_line_bytes = _positive_int("max_line_bytes", max_line_bytes)
    max_event_bytes = _positive_int("max_event_bytes", max_event_bytes)
    # 复制请求后开启流式返回，不修改调用方传入的字典。
    stream_payload = dict(payload)
    stream_payload["stream"] = True
    request = _request(
        url,
        data=_json_bytes(stream_payload),
        headers=_request_headers(headers, api_key, accept="text/event-stream"),
        method="POST",
    )
    # 单次网络操作超时与整个流的期限分别限制；总期限从建立连接前开始计算。
    deadline = time.monotonic() + max_duration
    response, secrets = _open(
        request,
        min(timeout, max_duration),
        api_key=api_key,
        headers=headers,
        max_error_bytes=max_error_bytes,
        deadline=deadline,
        max_duration=max_duration,
    )
    parser = SSEParser(
        max_line_bytes=max_line_bytes,
        max_event_bytes=max_event_bytes,
    )
    total_bytes = 0
    read_deadline_limited = False
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _stream_timeout(url, secrets, max_duration)
        _set_response_timeout(response, min(timeout, remaining))
        _raise_for_status(
            response,
            url,
            secrets,
            max_error_bytes=max_error_bytes,
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _stream_timeout(url, secrets, max_duration)
            # 每次读取最多等待剩余总期限，避免持续有数据的流无限延长运行。
            read_deadline_limited = remaining <= timeout
            _set_response_timeout(response, min(timeout, remaining))
            read_size = min(chunk_size, max_response_bytes + 1 - total_bytes)
            chunk = response.read(read_size)
            if time.monotonic() >= deadline:
                raise _stream_timeout(url, secrets, max_duration)
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise TypeError("HTTP response returned non-bytes data")
            total_bytes += len(chunk)
            if total_bytes > max_response_bytes:
                raise SSELimitError(f"SSE response exceeds {max_response_bytes} bytes")
            # 交给增量解析器形成完整事件；一个网络分块可能产生零个或多个 SSE 事件。
            yield from parser.feed(chunk)
        yield from parser.close()
    except (OpenAIHTTPError, OpenAIStreamTimeout, SSEError, UnicodeDecodeError):
        raise
    except (TimeoutError, socket.timeout, OSError) as exc:
        if (
            read_deadline_limited and _is_timeout_error(exc)
        ) or time.monotonic() >= deadline:
            raise _stream_timeout(url, secrets, max_duration) from None
        raise _transport_error(url, exc, secrets) from None
    finally:
        # 正常结束、异常或调用方关闭生成器时都释放 HTTP 响应，避免连接泄漏。
        response.close()


def stream_json(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    api_key: str | None = None,
    chunk_size: int = DEFAULT_READ_SIZE,
    max_duration: float = DEFAULT_STREAM_DURATION_SECONDS,
    stop_on_done: bool = True,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_error_bytes: int = DEFAULT_MAX_ERROR_BYTES,
    max_line_bytes: int = DEFAULT_MAX_SSE_LINE_BYTES,
    max_event_bytes: int = DEFAULT_MAX_SSE_EVENT_BYTES,
) -> Iterator[dict[str, Any]]:
    events = stream_sse(
        url,
        payload,
        headers,
        timeout,
        api_key=api_key,
        chunk_size=chunk_size,
        max_duration=max_duration,
        max_response_bytes=max_response_bytes,
        max_error_bytes=max_error_bytes,
        max_line_bytes=max_line_bytes,
        max_event_bytes=max_event_bytes,
    )
    try:
        for event in events:
            if event.data.strip() == "[DONE]":
                if stop_on_done:
                    return
                continue
            try:
                value = json.loads(event.data)
            except json.JSONDecodeError as exc:
                raise SSEJSONError(
                    f"invalid JSON in SSE event at line {exc.lineno}, column {exc.colno}"
                ) from None
            if not isinstance(value, dict):
                raise SSEJSONError("SSE JSON data must be an object")
            yield value
    finally:
        events.close()


def stream_openai(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    api_key: str | None = None,
    chunk_size: int = DEFAULT_READ_SIZE,
    max_duration: float = DEFAULT_STREAM_DURATION_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_error_bytes: int = DEFAULT_MAX_ERROR_BYTES,
    max_line_bytes: int = DEFAULT_MAX_SSE_LINE_BYTES,
    max_event_bytes: int = DEFAULT_MAX_SSE_EVENT_BYTES,
) -> Iterator[OpenAIStreamChunk]:
    yield from iter_openai_events(
        stream_sse(
            url,
            payload,
            headers,
            timeout,
            api_key=api_key,
            chunk_size=chunk_size,
            max_duration=max_duration,
            max_response_bytes=max_response_bytes,
            max_error_bytes=max_error_bytes,
            max_line_bytes=max_line_bytes,
            max_event_bytes=max_event_bytes,
        )
    )


def _positive_finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return converted


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _request_headers(
    headers: Mapping[str, str] | None,
    api_key: str | None,
    *,
    accept: str,
) -> dict[str, str]:
    merged = {"Accept": accept, "Content-Type": "application/json"}
    names = {name.lower(): name for name in merged}
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError("HTTP header names and values must be strings")
        old_name = names.get(name.lower())
        if old_name is not None:
            del merged[old_name]
        merged[name] = value
        names[name.lower()] = name
    if api_key is not None and "authorization" not in names:
        merged["Authorization"] = f"Bearer {api_key}"
    return merged


def _request(
    url: str,
    *,
    data: bytes | None,
    headers: Mapping[str, str],
    method: str,
) -> urllib.request.Request:
    regular_headers = {
        name: value
        for name, value in headers.items()
        if not _is_credential_header(name)
    }
    credential_headers = {
        name: value for name, value in headers.items() if _is_credential_header(name)
    }
    request = urllib.request.Request(
        url,
        data=data,
        headers=regular_headers,
        method=method,
    )
    # urllib copies normal headers onto redirected requests. Unredirected headers
    # are sent to the original endpoint but never forwarded to another URL.
    for name, value in credential_headers.items():
        request.add_unredirected_header(name, value)
    return request


def _open(
    request: urllib.request.Request,
    timeout: float,
    *,
    api_key: str | None,
    headers: Mapping[str, str] | None,
    max_error_bytes: int,
    deadline: float | None = None,
    max_duration: float | None = None,
) -> tuple[Any, tuple[str, ...]]:
    secrets = _auth_secrets(request.full_url, api_key, headers)
    remaining = deadline - time.monotonic() if deadline is not None else None
    if remaining is not None and remaining <= 0:
        assert max_duration is not None
        raise _stream_timeout(request.full_url, secrets, max_duration)
    operation_timeout = min(timeout, remaining) if remaining is not None else timeout
    deadline_limited = remaining is not None and operation_timeout == remaining
    try:
        return urllib.request.urlopen(request, timeout=operation_timeout), secrets
    except urllib.error.HTTPError as exc:
        if deadline is not None and time.monotonic() >= deadline:
            exc.close()
            assert max_duration is not None
            raise _stream_timeout(request.full_url, secrets, max_duration) from None
        if deadline is not None:
            excerpt_timeout = min(timeout, deadline - time.monotonic())
            if excerpt_timeout <= 0:
                exc.close()
                assert max_duration is not None
                raise _stream_timeout(request.full_url, secrets, max_duration) from None
            _set_response_timeout(exc, excerpt_timeout)
        try:
            body, truncated = _read_excerpt(exc, max_error_bytes)
        except (TimeoutError, socket.timeout, OSError, TypeError, SSELimitError):
            body, truncated = b"", False
        finally:
            exc.close()
        raise _status_error(
            request.full_url,
            exc.code,
            str(exc.reason) if exc.reason is not None else None,
            body,
            secrets,
            truncated=truncated,
        ) from None
    except (
        urllib.error.URLError,
        TimeoutError,
        socket.timeout,
        OSError,
        ValueError,
    ) as exc:
        timed_out = _is_timeout_error(exc)
        if deadline is not None and (
            (deadline_limited and timed_out) or time.monotonic() >= deadline
        ):
            assert max_duration is not None
            raise _stream_timeout(request.full_url, secrets, max_duration) from None
        raise _transport_error(request.full_url, exc, secrets) from None


def _raise_for_status(
    response: Any,
    url: str,
    secrets: tuple[str, ...],
    *,
    max_error_bytes: int,
) -> None:
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        status = response.getcode()
    if status is None or 200 <= int(status) < 300:
        return
    try:
        body, truncated = _read_excerpt(response, max_error_bytes)
    except (TimeoutError, socket.timeout, OSError, TypeError, SSELimitError):
        # Once a non-success status is known, failures while collecting its optional
        # diagnostic excerpt must not hide the HTTP failure or escape as an SSE error.
        body, truncated = b"", False
    reason = getattr(response, "reason", None)
    raise _status_error(
        url,
        int(status),
        str(reason) if reason is not None else None,
        body,
        secrets,
        truncated=truncated,
    )


def _read_limited(response: Any, limit: int, label: str) -> bytes:
    result = bytearray()
    while len(result) <= limit:
        part = response.read(min(DEFAULT_READ_SIZE, limit + 1 - len(result)))
        if not part:
            break
        if not isinstance(part, (bytes, bytearray, memoryview)):
            raise TypeError(f"{label} returned non-bytes data")
        result.extend(part)
    if len(result) > limit:
        raise SSELimitError(f"{label} exceeds {limit} bytes")
    return bytes(result)


def _read_excerpt(response: Any, limit: int) -> tuple[bytes, bool]:
    data = response.read(limit + 1)
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("HTTP error response returned non-bytes data")
    return bytes(data[:limit]), len(data) > limit


def _stream_timeout(
    url: str,
    secrets: tuple[str, ...],
    max_duration: float,
) -> OpenAIStreamTimeout:
    return OpenAIStreamTimeout(
        f"SSE stream from {_sanitized_url(url, secrets)} exceeded "
        f"{max_duration:g} seconds"
    )


def _set_response_timeout(response: Any, timeout: float) -> None:
    """Best-effort tightening of the stdlib response socket timeout."""

    candidates = [response]
    for path in (
        ("fp", "fp", "raw", "_sock"),
        ("fp", "raw", "_sock"),
        ("fp", "_sock"),
        ("raw", "_sock"),
        ("_sock",),
    ):
        candidate = response
        for attribute in path:
            candidate = getattr(candidate, attribute, None)
            if candidate is None:
                break
        if candidate is not None:
            candidates.append(candidate)

    for candidate in candidates:
        settimeout = getattr(candidate, "settimeout", None)
        if callable(settimeout):
            try:
                settimeout(timeout)
            except (OSError, ValueError):
                pass
            return


def _is_timeout_error(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(
        getattr(exc, "reason", None), (TimeoutError, socket.timeout)
    )


def _status_error(
    url: str,
    status: int,
    reason: str | None,
    body: bytes,
    secrets: tuple[str, ...],
    *,
    truncated: bool = False,
) -> OpenAIHTTPError:
    safe_url = _sanitized_url(url, secrets)
    safe_reason = _redact(reason or "", secrets).strip() or None
    excerpt = _redact_excerpt(
        body.decode("utf-8", errors="replace"),
        secrets,
        truncated=truncated,
    ).strip()
    if len(excerpt) > 1_000:
        excerpt = excerpt[:1_000] + "..."
    elif excerpt and truncated:
        excerpt += "..."
    details = f"HTTP {status}"
    if safe_reason:
        details += f" {safe_reason}"
    details += f" from {safe_url}"
    if excerpt:
        details += f": {excerpt}"
    return OpenAIHTTPError(
        details,
        safe_url=safe_url,
        status=status,
        reason=safe_reason,
        body_excerpt=excerpt or None,
    )


def _transport_error(
    url: str,
    exc: BaseException,
    secrets: tuple[str, ...] = (),
) -> OpenAIHTTPError:
    safe_url = _sanitized_url(url, secrets)
    kind = type(exc).__name__
    return OpenAIHTTPError(
        f"request to {safe_url} failed ({kind})",
        safe_url=safe_url,
    )


def _safe_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        path = parsed.path or "/"
        return urllib.parse.urlunsplit((parsed.scheme, host, path, "", ""))
    except (TypeError, ValueError):
        return "<invalid URL>"


def _sanitized_url(url: str, secrets: tuple[str, ...]) -> str:
    return _redact(_safe_url(url), secrets)


def _is_credential_header(name: str) -> bool:
    normalized = name.lower().replace("_", "-")
    return normalized in {
        "authorization",
        "proxy-authorization",
        "api-key",
        "x-api-key",
        "x-auth-token",
        "x-access-token",
    } or normalized.endswith(("-api-key", "-auth-token", "-access-token"))


def _auth_secrets(
    url: str,
    api_key: str | None,
    headers: Mapping[str, str] | None,
) -> tuple[str, ...]:
    values: set[str] = set()
    if api_key:
        values.add(api_key)
    for name, value in (headers or {}).items():
        if (
            _is_credential_header(name)
            or any(marker in name.lower() for marker in ("key", "token", "auth"))
        ) and value:
            values.add(value)
            scheme, separator, credential = value.partition(" ")
            if separator and scheme.lower() in {"basic", "bearer", "digest"}:
                credential = credential.strip()
                if credential:
                    values.add(credential)
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.username:
            values.add(urllib.parse.unquote(parsed.username))
        if parsed.password:
            values.add(urllib.parse.unquote(parsed.password))
        for name, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if (
                any(marker in name.lower() for marker in ("key", "token", "auth"))
                and value
            ):
                values.add(value)
    except (TypeError, ValueError):
        pass
    return tuple(sorted(values, key=len, reverse=True))


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    result = text
    for secret in secrets:
        result = result.replace(secret, "[REDACTED]")
    result = re.sub(
        r"(?i)\b(authorization|proxy-authorization|x-api-key|api-key)"
        r"([ \t]*[:=][ \t]*)(?:(?:basic|bearer|digest)[ \t]+)?"
        r"[^\s,;\"']+",
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        result,
    )
    return re.sub(r"(?i)\bbearer[ \t]+[^\s,;\"']+", "Bearer [REDACTED]", result)


def _redact_excerpt(
    text: str,
    secrets: tuple[str, ...],
    *,
    truncated: bool,
) -> str:
    if truncated:
        suffix_length = 0
        for secret in secrets:
            for length in range(min(len(secret), len(text)), 0, -1):
                if text.endswith(secret[:length]):
                    suffix_length = max(suffix_length, length)
                    break
        if suffix_length:
            text = text[:-suffix_length] + "[REDACTED]"
    return _redact(text, secrets)
