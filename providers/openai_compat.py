"""Adapters for OpenAI Chat Completions compatible HTTP APIs.

Covers the official OpenAI endpoint and the many services exposing the same
``/chat/completions`` protocol (DeepSeek, Qwen/DashScope, Kimi/Moonshot, Ollama,
vLLM, LM Studio, OneAPI, ...).  Vendor differences are expressed by small
profile subclasses of :class:`OpenAICompatibleProvider`; the wire logic and the
SSE decoder live in one place so every compatible provider behaves consistently.

Adapters accept an injected :class:`httpx.BaseTransport` so the layer can be
tested fully offline with :class:`httpx.MockTransport`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar, Iterable, Iterator

import httpx

from novel_ai.domain.models import ProviderConfig

from .base import (
    BaseProvider,
    GenerationRequest,
    ProviderError,
    ProviderErrorKind,
    StreamEvent,
    TokenUsage,
    classify_http_error,
    network_error,
)

logger = logging.getLogger(__name__)

_DONE_SENTINEL = "[DONE]"
_DEFAULT_TIMEOUT = 120.0
_DEFAULT_MAX_TOKENS = 4096


# ---------------------------------------------------------------------------
# Parsing helpers (module level so tests can exercise them indirectly)
# ---------------------------------------------------------------------------


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _usage_from_openai(payload: Any) -> TokenUsage | None:
    """Translate an OpenAI ``usage`` object (snake or camel cases)."""

    if not isinstance(payload, dict):
        return None
    prompt = _as_int(payload.get("prompt_tokens", payload.get("input_tokens")))
    completion = _as_int(
        payload.get("completion_tokens", payload.get("output_tokens"))
    )
    total = _as_int(payload.get("total_tokens"), prompt + completion)
    if prompt == 0 and completion == 0 and total == 0:
        return None
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total or (prompt + completion),
    )


def _content_to_text(content: Any) -> str:
    """Flatten a message ``content`` value (string or multimodal parts)."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type")
                text = item.get("text")
                if isinstance(text, str) and item_type in (None, "text", "output_text"):
                    parts.append(text)
        return "".join(parts)
    return ""


def _extract_delta_text(choice: dict[str, Any]) -> str:
    """Return incremental text from one streaming ``choices`` element."""

    delta = choice.get("delta")
    if isinstance(delta, dict):
        text = _content_to_text(delta.get("content"))
        if text:
            return text
        legacy = delta.get("text")
        if isinstance(legacy, str) and legacy:
            return legacy
    message = choice.get("message")
    if isinstance(message, dict):
        text = _content_to_text(message.get("content"))
        if text:
            return text
    legacy = choice.get("text")
    if isinstance(legacy, str):
        return legacy
    return ""


def _parse_message_choice(data: dict[str, Any]) -> tuple[str, str | None]:
    """Extract text/finish_reason from a non-streaming chat completion."""

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", None
    choice = choices[0]
    if not isinstance(choice, dict):
        return "", None
    reason = choice.get("finish_reason")
    finish_reason = reason if isinstance(reason, str) and reason else None
    message = choice.get("message")
    if isinstance(message, dict):
        return _content_to_text(message.get("content")), finish_reason
    legacy = choice.get("text")
    if isinstance(legacy, str):
        return legacy, finish_reason
    return "", finish_reason


def _error_from_payload(error: Any) -> ProviderError:
    """Build a :class:`ProviderError` from an API ``error`` payload."""

    if isinstance(error, dict):
        message = str(
            error.get("message")
            or error.get("msg")
            or error.get("type")
            or "服务端返回错误"
        )
        detail = json.dumps(error, ensure_ascii=False, default=str)[:1000]
        code = error.get("code")
        if isinstance(code, int) and code >= 400:
            return classify_http_error(code, detail)
        return ProviderError(message, kind=ProviderErrorKind.API, detail=detail)
    if isinstance(error, str) and error.strip():
        return ProviderError(error.strip()[:1000], kind=ProviderErrorKind.API)
    return ProviderError(
        "服务端返回错误", kind=ProviderErrorKind.API, detail=str(error)[:500]
    )


def _error_from_sse_data(data: str) -> ProviderError:
    """Build an error from an SSE ``event: error`` data line."""

    text = data.strip()
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return ProviderError(text[:1000] or "流式响应返回错误", kind=ProviderErrorKind.API)
    if isinstance(payload, dict) and "error" in payload:
        return _error_from_payload(payload["error"])
    return _error_from_payload(payload)


def iter_sse_blocks(lines: Iterable[str]) -> Iterator[tuple[str | None, str]]:
    """Aggregate raw SSE lines into ``(event_name, data)`` pairs.

    Handles the SSE specification correctly: multiple ``data:`` lines belonging
    to one event are joined with ``\n`` and dispatched on the blank line.  This
    keeps both the common one-JSON-per-line format and pretty-printed multi-line
    JSON working.  Malformed field lines are ignored instead of aborting.
    """

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
            # SSE comment / keep-alive.
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
        elif line.startswith("event:"):
            event_name = line[6:].strip() or None
        elif line.startswith("id:") or line.startswith("retry:"):
            continue
        elif line.lstrip().startswith("{"):
            # Some compatible gateways stream raw JSON lines without "data:".
            data_lines.append(line)
        # Any other field name is not part of the protocol we consume.
    if data_lines:
        yield event_name, "\n".join(data_lines)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OpenAICompatibleProvider(BaseProvider):
    """Generic adapter for OpenAI ``/chat/completions`` compatible APIs."""

    provider_type: ClassVar[str] = "openai_compatible"
    display_name: ClassVar[str] = "自定义 OpenAI 兼容"
    requires_api_key: ClassVar[bool] = False
    default_base_url: ClassVar[str] = "http://localhost:8000/v1"
    default_model: ClassVar[str] = "gpt-4o-mini"
    default_context_window: ClassVar[int] = 32_768
    #: Send ``stream_options.include_usage`` so the final chunk carries usage.
    needs_stream_options: ClassVar[bool] = False

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
        base = base.rstrip("/")
        if not base:
            raise ProviderError(
                "Base URL 未配置，请检查供应商配置。",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        return base

    def _endpoint(self) -> str:
        base = self._base_url()
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

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
        return max(0.0, min(2.0, temperature))

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
        """Build a short-lived client honouring timeout/proxy settings."""

        options: dict[str, Any] = {"timeout": self._resolve_timeout(request)}
        proxy = self._resolve_proxy(request)
        if self._transport is not None:
            options["transport"] = self._transport
            # Injected transports are used by offline tests; never let
            # environment proxies interfere with them.
            options["trust_env"] = False
        elif proxy:
            options["proxy"] = proxy
        return httpx.Client(**options)

    def _headers(self, *, stream: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        headers.update(self._auth_headers())
        extra_headers = self._extra.get("headers")
        if isinstance(extra_headers, dict):
            headers.update({str(key): str(value) for key, value in extra_headers.items()})
        return headers

    def _build_payload(
        self, request: GenerationRequest, *, stream: bool
    ) -> dict[str, Any]:
        if not request.messages:
            raise ProviderError(
                "请求消息为空，无法调用模型。", kind=ProviderErrorKind.CONFIGURATION
            )
        payload: dict[str, Any] = {
            "model": self._resolve_model(request),
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "temperature": self._resolve_temperature(request),
            "max_tokens": self._resolve_max_tokens(request),
            "stream": stream,
        }
        if stream:
            stream_options = self._extra.get("stream_options")
            if isinstance(stream_options, dict):
                payload["stream_options"] = dict(stream_options)
            elif self.needs_stream_options:
                payload["stream_options"] = {"include_usage": True}
        if request.extra:
            # Extra request options (e.g. response_format, top_p) pass through.
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
        if error:
            raise _error_from_payload(error)

        text, finish_reason = _parse_message_choice(data)
        usage = _usage_from_openai(data.get("usage"))
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
        usage = TokenUsage()
        finish_reason: str | None = None
        try:
            with self._client(request) as client:
                with client.stream(
                    "POST", url, json=payload, headers=headers
                ) as response:
                    if response.status_code >= 400:
                        body = response.read().decode("utf-8", "replace")
                        raise classify_http_error(response.status_code, body)
                    for event_name, data in iter_sse_blocks(response.iter_lines()):
                        stripped = data.strip()
                        if not stripped:
                            continue
                        if stripped == _DONE_SENTINEL:
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
                        error = chunk.get("error")
                        if error:
                            raise _error_from_payload(error)

                        chunk_usage = _usage_from_openai(chunk.get("usage"))
                        if chunk_usage is not None:
                            usage = usage.merge(chunk_usage)
                            yield StreamEvent(type="usage", usage=chunk_usage)

                        choices = chunk.get("choices")
                        if not isinstance(choices, list):
                            continue
                        for choice in choices:
                            if not isinstance(choice, dict):
                                continue
                            reason = choice.get("finish_reason")
                            if isinstance(reason, str) and reason:
                                finish_reason = reason
                            text = _extract_delta_text(choice)
                            if text:
                                yield StreamEvent(type="text", text=text)
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

        final_usage = (
            usage
            if (usage.prompt_tokens or usage.completion_tokens or usage.total_tokens)
            else None
        )
        yield StreamEvent(
            type="done", finish_reason=finish_reason, usage=final_usage
        )


# ---------------------------------------------------------------------------
# Concrete profiles
# ---------------------------------------------------------------------------


class OpenAIProvider(OpenAICompatibleProvider):
    provider_type: ClassVar[str] = "openai"
    display_name: ClassVar[str] = "OpenAI"
    requires_api_key: ClassVar[bool] = True
    default_base_url: ClassVar[str] = "https://api.openai.com/v1"
    default_model: ClassVar[str] = "gpt-4o-mini"
    default_context_window: ClassVar[int] = 128_000
    needs_stream_options: ClassVar[bool] = True


class DeepSeekProvider(OpenAIProvider):
    provider_type: ClassVar[str] = "deepseek"
    display_name: ClassVar[str] = "DeepSeek 深度求索"
    default_base_url: ClassVar[str] = "https://api.deepseek.com/v1"
    default_model: ClassVar[str] = "deepseek-chat"
    default_context_window: ClassVar[int] = 128_000


class QwenProvider(OpenAIProvider):
    provider_type: ClassVar[str] = "qwen"
    display_name: ClassVar[str] = "通义千问 Qwen"
    default_base_url: ClassVar[str] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    default_model: ClassVar[str] = "qwen-plus"
    default_context_window: ClassVar[int] = 131_072


class KimiProvider(OpenAIProvider):
    provider_type: ClassVar[str] = "kimi"
    display_name: ClassVar[str] = "Kimi 月之暗面"
    default_base_url: ClassVar[str] = "https://api.moonshot.cn/v1"
    default_model: ClassVar[str] = "moonshot-v1-32k"
    default_context_window: ClassVar[int] = 32_768
    # Moonshot does not document ``stream_options``; parse usage when present.
    needs_stream_options: ClassVar[bool] = False


class OllamaProvider(OpenAICompatibleProvider):
    provider_type: ClassVar[str] = "ollama"
    display_name: ClassVar[str] = "Ollama 本地"
    requires_api_key: ClassVar[bool] = False
    default_base_url: ClassVar[str] = "http://localhost:11434/v1"
    default_model: ClassVar[str] = "llama3.1"
    default_context_window: ClassVar[int] = 32_768
    needs_stream_options: ClassVar[bool] = False


__all__ = [
    "DeepSeekProvider",
    "KimiProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "QwenProvider",
    "iter_sse_blocks",
]
