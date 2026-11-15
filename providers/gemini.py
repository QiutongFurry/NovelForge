"""Google Gemini ``generateContent`` adapter.

Streaming uses the server-sent-events flavour of
``models/{model}:streamGenerateContent?alt=sse``; non-streaming uses
``models/{model}:generateContent``.  The API key is sent as ``x-goog-api-key``
so it never appears in request URLs or logs.
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

#: Keys that belong inside ``generationConfig`` when supplied via request.extra.
_GENERATION_CONFIG_KEYS = {
    "candidateCount",
    "frequencyPenalty",
    "logprobs",
    "maxOutputTokens",
    "mediaResolution",
    "presencePenalty",
    "responseLogprobs",
    "responseMimeType",
    "responseSchema",
    "seed",
    "stopSequences",
    "temperature",
    "thinkingConfig",
    "topK",
    "topP",
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
) -> tuple[str, list[dict[str, Any]]]:
    """Split system prompts from the conversation and merge same-role turns."""

    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
            continue
        if not message.content:
            continue
        role = "model" if message.role == "assistant" else "user"
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].append({"text": message.content})
        else:
            contents.append({"role": role, "parts": [{"text": message.content}]})
    return "\n\n".join(system_parts), contents


def _usage_from_metadata(metadata: Any) -> TokenUsage | None:
    if not isinstance(metadata, dict):
        return None
    prompt = _as_int(metadata.get("promptTokenCount"))
    completion = _as_int(metadata.get("candidatesTokenCount"))
    total = _as_int(metadata.get("totalTokenCount"))
    if prompt == 0 and completion == 0 and total == 0:
        return None
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total or (prompt + completion),
    )


def _candidate_data(data: dict[str, Any]) -> tuple[str, str | None]:
    """Return ``(text, finish_reason)`` of the first candidate."""

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return "", None
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return "", None
    reason = candidate.get("finishReason")
    finish_reason = reason if isinstance(reason, str) and reason else None
    content = candidate.get("content")
    if not isinstance(content, dict):
        return "", finish_reason
    parts = content.get("parts")
    if not isinstance(parts, list):
        return "", finish_reason
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        # "thought" parts carry the model's hidden reasoning; never show them.
        if isinstance(text, str) and not part.get("thought"):
            texts.append(text)
    return "".join(texts), finish_reason


def _prompt_block_reason(data: dict[str, Any]) -> str:
    feedback = data.get("promptFeedback")
    if isinstance(feedback, dict):
        reason = feedback.get("blockReason")
        if isinstance(reason, str) and reason:
            return reason
    candidates = data.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("finishReason") == "SAFETY":
                return "SAFETY"
    return ""


def _error_from_gemini(error: Any) -> ProviderError:
    """Translate a Google error object into :class:`ProviderError`."""

    if isinstance(error, dict):
        message = str(
            error.get("message") or error.get("status") or "Gemini API 错误"
        )
        detail = json.dumps(error, ensure_ascii=False, default=str)[:1000]
        code = error.get("code")
        lowered = detail.lower()
        if isinstance(code, int) and code >= 400:
            if code in (400, 403) and (
                "api key" in lowered or "api_key_invalid" in lowered
            ):
                return ProviderError(
                    message,
                    kind=ProviderErrorKind.AUTH,
                    status_code=code,
                    detail=detail,
                )
            return classify_http_error(code, detail)
        return ProviderError(message, kind=ProviderErrorKind.API, detail=detail)
    return ProviderError(
        str(error) or "Gemini API 错误", kind=ProviderErrorKind.API
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class GeminiProvider(BaseProvider):
    """Adapter for the Google Gemini Generative Language API."""

    provider_type: ClassVar[str] = "gemini"
    display_name: ClassVar[str] = "Google Gemini"
    requires_api_key: ClassVar[bool] = True
    default_base_url: ClassVar[str] = "https://generativelanguage.googleapis.com/v1beta"
    default_model: ClassVar[str] = "gemini-2.0-flash"
    default_context_window: ClassVar[int] = 1_048_576

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

    def _api_base_and_model(self, request: GenerationRequest) -> tuple[str, str]:
        """Normalise base URL/model, tolerating full endpoint URLs in config."""

        raw_base = (self.config.base_url or "").strip() or self.default_base_url
        model = (request.model or self.config.model or self.default_model or "").strip()

        if "/models/" in raw_base:
            head, tail = raw_base.split("/models/", 1)
            if not model:
                model = tail.split(":", 1)[0].split("?", 1)[0].strip()
            raw_base = head
        for marker in (":streamGenerateContent", ":generateContent"):
            if marker in raw_base:
                raw_base = raw_base.split(marker, 1)[0]
        # A pasted endpoint may carry a query (e.g. ?key=... or ?alt=sse).
        raw_base = raw_base.split("?", 1)[0]
        raw_base = raw_base.rstrip("/")
        if raw_base.endswith("/models"):
            raw_base = raw_base[: -len("/models")]

        if model.startswith("models/"):
            model = model[len("models/") :]
        if not raw_base:
            raise ProviderError(
                "Base URL 未配置，请检查供应商配置。",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        if not model:
            raise ProviderError(
                "模型名称未配置，请检查供应商配置。",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        return raw_base, model

    def _endpoint(self, request: GenerationRequest, *, stream: bool) -> str:
        base, model = self._api_base_and_model(request)
        operation = "streamGenerateContent" if stream else "generateContent"
        url = f"{base}/models/{model}:{operation}"
        if stream:
            url += "?alt=sse"
        return url

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
            "x-goog-api-key": self._api_key,
        }
        extra_headers = self._extra.get("headers")
        if isinstance(extra_headers, dict):
            headers.update({str(key): str(value) for key, value in extra_headers.items()})
        return headers

    def _build_payload(
        self, request: GenerationRequest, *, stream: bool
    ) -> dict[str, Any]:
        system_text, contents = _split_system_messages(request.messages)
        if not contents:
            raise ProviderError(
                "请求消息为空，无法调用模型。", kind=ProviderErrorKind.CONFIGURATION
            )

        generation_config: dict[str, Any] = {
            "temperature": self._resolve_temperature(request),
            "maxOutputTokens": self._resolve_max_tokens(request),
        }
        extra = dict(request.extra) if request.extra else {}
        nested = extra.pop("generationConfig", None)
        if isinstance(nested, dict):
            generation_config.update(nested)
        for key in list(extra):
            if key in _GENERATION_CONFIG_KEYS:
                generation_config[key] = extra.pop(key)

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        # Remaining extras (safetySettings, tools, ...) are top-level fields.
        payload.update(extra)
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
        url = self._endpoint(request, stream=False)
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
            raise _error_from_gemini(error)

        text, finish_reason = _candidate_data(data)
        usage = _usage_from_metadata(data.get("usageMetadata"))
        if not text:
            block_reason = _prompt_block_reason(data)
            if block_reason:
                raise ProviderError(
                    f"内容被 Gemini 安全策略拦截（{block_reason}）。",
                    kind=ProviderErrorKind.API,
                )
        if text:
            yield StreamEvent(type="text", text=text)
        if usage is not None:
            yield StreamEvent(type="usage", usage=usage)
        yield StreamEvent(type="done", finish_reason=finish_reason, usage=usage)

    # -- streaming (SSE) ---------------------------------------------------
    def _stream_events(self, request: GenerationRequest) -> Iterator[StreamEvent]:
        payload = self._build_payload(request, stream=True)
        url = self._endpoint(request, stream=True)
        headers = self._headers(stream=True)
        usage: TokenUsage | None = None
        finish_reason: str | None = None
        saw_text = False
        block_reason = ""
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
                            try:
                                parsed = json.loads(stripped)
                            except (ValueError, TypeError):
                                raise ProviderError(
                                    stripped[:1000] or "流式响应返回错误",
                                    kind=ProviderErrorKind.API,
                                ) from None
                            raise _error_from_gemini(
                                parsed.get("error") if isinstance(parsed, dict) else parsed
                            )
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
                        if error is not None:
                            raise _error_from_gemini(error)

                        chunk_usage = _usage_from_metadata(chunk.get("usageMetadata"))
                        if chunk_usage is not None:
                            # Gemini repeats cumulative usage; the latest wins.
                            usage = chunk_usage
                        text, reason = _candidate_data(chunk)
                        if isinstance(reason, str) and reason:
                            finish_reason = reason
                        if text:
                            saw_text = True
                            yield StreamEvent(type="text", text=text)
                        if not block_reason:
                            block_reason = _prompt_block_reason(chunk)
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

        if not saw_text and block_reason:
            raise ProviderError(
                f"内容被 Gemini 安全策略拦截（{block_reason}）。",
                kind=ProviderErrorKind.API,
            )
        if usage is not None:
            yield StreamEvent(type="usage", usage=usage)
        yield StreamEvent(type="done", finish_reason=finish_reason, usage=usage)


__all__ = ["GeminiProvider"]
