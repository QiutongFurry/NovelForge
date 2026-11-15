"""Anthropic Messages API adapter.

Implements ``POST {base}/v1/messages`` with ``x-api-key`` authentication,
system-prompt extraction and both streaming (SSE) and non-streaming modes.
Only :mod:`novel_ai.providers.base` objects cross the module boundary.
"""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar, Iterable, Iterator

import httpx

from novel_ai.domain.models import ProviderConfig

from .base import (
    BaseProvider,
    ChatMessage,
    GenerationRequest,
    ProviderError,
    ProviderErrorKind,
    StreamEvent,
    TokenUsage,
    classify_http_error,
    network_error,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120.0
_DEFAULT_MAX_TOKENS = 4096

_ANTHROPIC_ERROR_KINDS: dict[str, str] = {
    "authentication_error": ProviderErrorKind.AUTH,
    "permission_error": ProviderErrorKind.AUTH,
    "rate_limit_error": ProviderErrorKind.RATE_LIMIT,
    "overloaded_error": ProviderErrorKind.API,
    "api_error": ProviderErrorKind.API,
    "invalid_request_error": ProviderErrorKind.API,
    "request_too_large": ProviderErrorKind.CONTEXT_LENGTH,
    "not_found_error": ProviderErrorKind.CONFIGURATION,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _iter_sse_blocks(lines: Iterable[str]) -> Iterator[tuple[str | None, str]]:
    """Aggregate raw SSE lines into ``(event_name, data)`` pairs."""

    event_name: str | None = None
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name, data_lines = None, []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        elif line.startswith("event:"):
            event_name = line[6:].strip() or None
        elif line.startswith("id:") or line.startswith("retry:"):
            continue
        elif line.lstrip().startswith("{"):
            data_lines.append(line)
    if data_lines:
        yield event_name, "\n".join(data_lines)


def _split_system_messages(
    messages: list[ChatMessage],
) -> tuple[str, list[dict[str, str]]]:
    """Split system prompts from the conversation and merge same-role turns."""

    system_parts: list[str] = []
    conversation: list[dict[str, str]] = []
    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
            continue
        if not message.content:
            continue
        role = "assistant" if message.role == "assistant" else "user"
        if conversation and conversation[-1]["role"] == role:
            conversation[-1]["content"] = (
                f"{conversation[-1]['content']}\n\n{message.content}"
            )
        else:
            conversation.append({"role": role, "content": message.content})
    return "\n\n".join(system_parts), conversation


def _usage_from_anthropic(payload: Any) -> TokenUsage | None:
    if not isinstance(payload, dict):
        return None
    prompt = _as_int(payload.get("input_tokens"))
    completion = _as_int(payload.get("output_tokens"))
    if prompt == 0 and completion == 0:
        return None
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )


def _error_from_anthropic(error: Any) -> ProviderError:
    """Translate an Anthropic error object into :class:`ProviderError`."""

    if isinstance(error, dict):
        error_type = str(error.get("type") or "")
        message = str(error.get("message") or error_type or "Anthropic API 错误")
        kind = _ANTHROPIC_ERROR_KINDS.get(error_type, ProviderErrorKind.API)
        retryable = error_type in ("rate_limit_error", "overloaded_error")
        detail = json.dumps(error, ensure_ascii=False, default=str)[:1000]
        return ProviderError(
            message, kind=kind, retryable=retryable, detail=detail
        )
    return ProviderError(
        str(error) or "Anthropic API 错误", kind=ProviderErrorKind.API
    )


def _error_from_sse_data(data: str) -> ProviderError:
    text = data.strip()
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return ProviderError(text[:1000] or "流式响应返回错误", kind=ProviderErrorKind.API)
    if isinstance(payload, dict) and "error" in payload:
        return _error_from_anthropic(payload["error"])
    return _error_from_anthropic(payload)


def _block_text(block: Any) -> str:
    if not isinstance(block, dict):
        return ""
    if block.get("type") not in (None, "text"):
        return ""
    text = block.get("text")
    return text if isinstance(text, str) else ""


def _delta_text(delta: Any) -> str:
    if not isinstance(delta, dict):
        return ""
    text = delta.get("text")
    if isinstance(text, str):
        return text
    return ""


def _response_text(data: dict[str, Any]) -> str:
    content = data.get("content")
    if not isinstance(content, list):
        return ""
    parts = [_block_text(block) for block in content]
    return "".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class AnthropicProvider(BaseProvider):
    """Adapter for the Anthropic Messages API."""

    provider_type: ClassVar[str] = "anthropic"
    display_name: ClassVar[str] = "Anthropic Claude"
    requires_api_key: ClassVar[bool] = True
    default_base_url: ClassVar[str] = "https://api.anthropic.com"
    default_model: ClassVar[str] = "claude-sonnet-4-5"
    default_context_window: ClassVar[int] = 200_000
    DEFAULT_API_VERSION: ClassVar[str] = "2023-06-01"

    def __init__(
        self,
        config: ProviderConfig,
        api_key: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(config, api_key)
        self._transport = transport

    # -- configuration helpers -------------------------------------------
    @property
    def _extra(self) -> dict[str, Any]:
        return self.config.extra if isinstance(self.config.extra, dict) else {}

    def _base_url(self) -> str:
        base = (self.config.base_url or "").strip() or self.default_base_url.strip()
        if not base:
            raise ProviderError(
                "Base URL 未配置，请检查供应商配置。",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        return base

    def _endpoint(self) -> str:
        base = self._base_url().rstrip("/")
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    def _api_version(self) -> str:
        value = self._extra.get("anthropic_version") or self.DEFAULT_API_VERSION
        return str(value)

    def _resolve_model(self, request: GenerationRequest) -> str:
        model = (
            request.model or self.config.model or self.default_model or ""
        ).strip()
        if not model:
            raise ProviderError(
                "模型名称未配置，请检查供应商配置。",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        return model

    def _resolve_max_tokens(self, request: GenerationRequest) -> int:
        value = request.max_tokens or self.config.max_tokens or _DEFAULT_MAX_TOKENS
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return _DEFAULT_MAX_TOKENS

    def _resolve_temperature(self, request: GenerationRequest) -> float:
        value = (
            self.config.temperature
            if request.temperature is None
            else request.temperature
        )
        try:
            temperature = float(value)
        except (TypeError, ValueError):
            temperature = self.config.temperature
        # The Messages API only accepts temperatures in [0.0, 1.0].
        return max(0.0, min(1.0, temperature))

    def _resolve_timeout(self, request: GenerationRequest) -> float:
        value = request.timeout if request.timeout is not None else self.config.timeout
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        return timeout if timeout > 0 else _DEFAULT_TIMEOUT

    def _resolve_proxy(self, request: GenerationRequest) -> str:
        return (request.proxy or self.config.proxy or "").strip()

    def _client(self, request: GenerationRequest) -> httpx.Client:
        options: dict[str, Any] = {"timeout": self._resolve_timeout(request)}
        proxy = self._resolve_proxy(request)
        if self._transport is not None:
            options["transport"] = self._transport
            options["trust_env"] = False
        elif proxy:
            options["proxy"] = proxy
        return httpx.Client(**options)

    def _headers(self, *, stream: bool) -> dict[str, str]:
        if not self._api_key:
            raise ProviderError(
                "未配置 API Key", kind=ProviderErrorKind.CONFIGURATION
            )
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": self._api_version(),
        }
        extra_headers = self._extra.get("headers")
        if isinstance(extra_headers, dict):
            headers.update({str(key): str(value) for key, value in extra_headers.items()})
        return headers

    def _build_payload(
        self, request: GenerationRequest, *, stream: bool
    ) -> dict[str, Any]:
        system_text, messages = _split_system_messages(request.messages)
        if not messages:
            raise ProviderError(
                "请求消息为空，无法调用模型。", kind=ProviderErrorKind.CONFIGURATION
            )
        payload: dict[str, Any] = {
            "model": self._resolve_model(request),
            "max_tokens": self._resolve_max_tokens(request),
            "messages": messages,
            "temperature": self._resolve_temperature(request),
            "stream": stream,
        }
        if system_text:
            payload["system"] = system_text
        if request.extra:
            payload.update(request.extra)
        return payload

    # -- public adapter API ----------------------------------------------
    def stream_chat(self, request: GenerationRequest) -> Iterator[StreamEvent]:
        if request.stream:
            yield from self._stream_events(request)
        else:
            yield from self._complete_once(request)

    # -- non-streaming -----------------------------------------------------
    def _complete_once(self, request: GenerationRequest) -> Iterator[StreamEvent]:
        payload = self._build_payload(request, stream=False)
        url = self._endpoint()
        headers = self._headers(stream=False)
        try:
            with self._client(request) as client:
                response = client.post(url, json=payload, headers=headers)
                body = response.text
                if response.status_code >= 400:
                    raise classify_http_error(response.status_code, body)
                try:
                    data = response.json()
                except ValueError as exc:
                    raise ProviderError(
                        "服务端返回了无法解析的响应。",
                        kind=ProviderErrorKind.API,
                        status_code=response.status_code,
                        detail=body[:1000],
                    ) from exc
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise network_error(exc) from exc
        except Exception as exc:  # defensive: adapters only raise ProviderError
            raise ProviderError(
                f"{self.display_name} 调用失败：{exc}",
                kind=ProviderErrorKind.UNKNOWN,
                detail=type(exc).__name__,
            ) from exc

        if not isinstance(data, dict):
            raise ProviderError(
                "服务端返回了非预期的响应格式。",
                kind=ProviderErrorKind.API,
                detail=str(data)[:500],
            )
        error = data.get("error")
        if error is not None:
            raise _error_from_anthropic(error)

        text = _response_text(data)
        usage = _usage_from_anthropic(data.get("usage"))
        finish_reason = data.get("stop_reason")
        if not isinstance(finish_reason, str) or not finish_reason:
            finish_reason = None
        if text:
            yield StreamEvent(type="text", text=text)
        if usage is not None:
            yield StreamEvent(type="usage", usage=usage)
        yield StreamEvent(type="done", finish_reason=finish_reason, usage=usage)

    # -- streaming (SSE) ---------------------------------------------------
    def _stream_events(self, request: GenerationRequest) -> Iterator[StreamEvent]:
        payload = self._build_payload(request, stream=True)
        url = self._endpoint()
        headers = self._headers(stream=True)
        prompt_tokens = 0
        completion_tokens = 0
        finish_reason: str | None = None
        try:
            with self._client(request) as client:
                with client.stream(
                    "POST", url, json=payload, headers=headers
                ) as response:
                    if response.status_code >= 400:
                        body = response.read().decode("utf-8", "replace")
                        raise classify_http_error(response.status_code, body)
                    for event_name, data in _iter_sse_blocks(response.iter_lines()):
                        stripped = data.strip()
                        if not stripped:
                            continue
                        if stripped == "[DONE]":
                            break
                        if event_name == "error":
                            raise _error_from_sse_data(stripped)
                        try:
                            chunk = json.loads(stripped)
                        except (ValueError, TypeError):
                            logger.debug(
                                "Ignoring malformed SSE chunk (%d chars)", len(stripped)
                            )
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        if chunk.get("error") is not None:
                            raise _error_from_anthropic(chunk.get("error"))

                        kind = chunk.get("type") or event_name or ""
                        if kind == "error":
                            raise _error_from_anthropic(chunk)
                        if kind == "message_start":
                            message = chunk.get("message")
                            usage = _usage_from_anthropic(
                                message.get("usage") if isinstance(message, dict) else None
                            )
                            if usage is not None:
                                prompt_tokens = usage.prompt_tokens or prompt_tokens
                                completion_tokens = (
                                    usage.completion_tokens or completion_tokens
                                )
                            continue
                        if kind == "content_block_start":
                            text = _block_text(chunk.get("content_block"))
                            if text:
                                yield StreamEvent(type="text", text=text)
                            continue
                        if kind == "content_block_delta":
                            text = _delta_text(chunk.get("delta"))
                            if text:
                                yield StreamEvent(type="text", text=text)
                            continue
                        if kind == "message_delta":
                            delta = chunk.get("delta")
                            if isinstance(delta, dict):
                                stop = delta.get("stop_reason")
                                if isinstance(stop, str) and stop:
                                    finish_reason = stop
                            usage = _usage_from_anthropic(chunk.get("usage"))
                            if usage is not None:
                                prompt_tokens = prompt_tokens or usage.prompt_tokens
                                # message_delta carries the running output total.
                                completion_tokens = (
                                    usage.completion_tokens or completion_tokens
                                )
                            continue
                        if kind == "message_stop":
                            break
                        # "ping" and future event types are ignored.
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise network_error(exc) from exc
        except Exception as exc:  # defensive: adapters only raise ProviderError
            raise ProviderError(
                f"{self.display_name} 调用失败：{exc}",
                kind=ProviderErrorKind.UNKNOWN,
                detail=type(exc).__name__,
            ) from exc

        final_usage: TokenUsage | None = None
        if prompt_tokens or completion_tokens:
            final_usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )
            yield StreamEvent(type="usage", usage=final_usage)
        yield StreamEvent(
            type="done", finish_reason=finish_reason, usage=final_usage
        )


__all__ = ["AnthropicProvider"]
