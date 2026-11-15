"""Provider-independent contracts for AI chat completion backends.

The business layer only knows this module.  Each vendor adapter translates these
objects into the vendor wire format and translates streaming responses back into
:class:`StreamEvent` values.  Adding a vendor never requires changes in services
or the UI.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterator, Literal

from novel_ai.config.secrets import mask_secret
from novel_ai.domain.models import ProviderConfig


class ProviderErrorKind:
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CONTEXT_LENGTH = "context_length"
    NETWORK = "network"
    API = "api"
    CONFIGURATION = "configuration"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


_FRIENDLY = {
    ProviderErrorKind.AUTH: "API Key 无效或无权限，请检查配置。",
    ProviderErrorKind.RATE_LIMIT: "请求过于频繁或额度不足，请稍后重试。",
    ProviderErrorKind.TIMEOUT: "请求超时，请检查网络或调大超时时间。",
    ProviderErrorKind.CONTEXT_LENGTH: "上下文超出模型窗口，请缩短内容或更换长上下文模型。",
    ProviderErrorKind.NETWORK: "网络连接失败，请检查 Base URL、代理和网络。",
    ProviderErrorKind.API: "服务端返回错误，请稍后重试。",
    ProviderErrorKind.CONFIGURATION: "供应商配置不完整，请检查 Base URL、模型和 API Key。",
    ProviderErrorKind.CANCELLED: "生成已取消。",
    ProviderErrorKind.UNKNOWN: "调用 AI 服务时发生未知错误。",
}


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = ProviderErrorKind.UNKNOWN,
        status_code: int | None = None,
        retryable: bool = False,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status_code = status_code
        self.retryable = retryable
        self.detail = detail

    @property
    def user_message(self) -> str:
        base = _FRIENDLY.get(self.kind, _FRIENDLY[ProviderErrorKind.UNKNOWN])
        extra = self.message.strip()
        if extra and extra not in base:
            return f"{base}\n{extra}"
        return base

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def merge(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens or other.prompt_tokens,
            completion_tokens=self.completion_tokens or other.completion_tokens,
            total_tokens=self.total_tokens or other.total_tokens,
        )


@dataclass
class GenerationRequest:
    messages: list[ChatMessage]
    model: str
    temperature: float = 0.8
    max_tokens: int = 4096
    timeout: float = 120.0
    proxy: str = ""
    stream: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamEvent:
    type: Literal["text", "usage", "done", "error"]
    text: str = ""
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    error: ProviderError | None = None


@dataclass
class GenerationResult:
    text: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str | None = None


@dataclass
class ProviderTestResult:
    ok: bool
    message: str
    latency_ms: int = 0
    model: str = ""
    detail: str = ""


class BaseProvider(ABC):
    """Base class implemented by every vendor adapter."""

    provider_type: ClassVar[str] = "base"
    display_name: ClassVar[str] = "Base"
    requires_api_key: ClassVar[bool] = True

    def __init__(self, config: ProviderConfig, api_key: str | None = None) -> None:
        self.config = config
        self._api_key = api_key or ""

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def masked_key(self) -> str:
        return mask_secret(self._api_key)

    # -- required adapter API ----------------------------------------------
    @abstractmethod
    def stream_chat(self, request: GenerationRequest) -> Iterator[StreamEvent]:
        """Yield incremental events and finish with type ``done``.

        Implementations must use the configuration's base URL/model defaults
        when the request does not override them and must raise
        :class:`ProviderError` for transport/API failures.
        """
        raise NotImplementedError

    # -- shared helpers -----------------------------------------------------
    def complete(self, request: GenerationRequest) -> GenerationResult:
        """Collect a streaming response into a single result."""

        chunks: list[str] = []
        usage = TokenUsage()
        finish_reason: str | None = None
        for event in self.stream_chat(request):
            if event.type == "text":
                chunks.append(event.text)
            elif event.type == "usage" and event.usage:
                usage = usage.merge(event.usage)
            elif event.type == "done":
                finish_reason = event.finish_reason
            elif event.type == "error" and event.error:
                raise event.error
        text = "".join(chunks)
        if not usage.completion_tokens:
            usage.completion_tokens = max(1, len(text) // 2)
        if not usage.total_tokens:
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return GenerationResult(text=text, usage=usage, finish_reason=finish_reason)

    def build_request(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool | None = None,
        extra: dict[str, Any] | None = None,
    ) -> GenerationRequest:
        cfg = self.config
        return GenerationRequest(
            messages=messages,
            model=model or cfg.model,
            temperature=cfg.temperature if temperature is None else temperature,
            max_tokens=max_tokens or cfg.max_tokens,
            timeout=cfg.timeout,
            proxy=cfg.proxy,
            stream=cfg.stream if stream is None else stream,
            extra=extra or {},
        )

    def test_connection(self) -> ProviderTestResult:
        """Make a tiny, low-cost request to validate endpoint and credentials."""

        started = time.perf_counter()
        messages = [ChatMessage(role="user", content="请只回复：OK")]
        request = self.build_request(
            messages,
            temperature=0.0,
            max_tokens=16,
            stream=False,
        )
        request.timeout = min(max(self.config.timeout, 5.0), 30.0)
        try:
            result = self.complete(request)
            elapsed = int((time.perf_counter() - started) * 1000)
            if result.text.strip():
                return ProviderTestResult(
                    ok=True,
                    message="连接成功，模型已响应。",
                    latency_ms=elapsed,
                    model=self.config.model,
                )
            return ProviderTestResult(
                ok=False,
                message="接口返回成功，但未收到文本内容。",
                latency_ms=elapsed,
                model=self.config.model,
            )
        except ProviderError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            return ProviderTestResult(
                ok=False,
                message=exc.user_message,
                latency_ms=elapsed,
                model=self.config.model,
                detail=exc.detail or exc.message,
            )
        except Exception as exc:  # defensive: adapters normally wrap errors
            elapsed = int((time.perf_counter() - started) * 1000)
            return ProviderTestResult(
                ok=False,
                message=f"连接测试失败：{exc}",
                latency_ms=elapsed,
                model=self.config.model,
            )

    def _auth_headers(self) -> dict[str, str]:
        """Default bearer auth; adapters override when their protocol differs."""

        if not self._api_key:
            if self.requires_api_key:
                raise ProviderError(
                    "未配置 API Key",
                    kind=ProviderErrorKind.CONFIGURATION,
                )
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}


# ---------------------------------------------------------------------------
# Shared error classification for HTTP adapters
# ---------------------------------------------------------------------------


def parse_error_message(body: str, fallback: str = "") -> str:
    """Best-effort extraction of a human readable API error message."""

    if not body:
        return fallback
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return body.strip()[:500] or fallback
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            msg = error.get("message") or error.get("msg")
            if msg:
                return str(msg)[:1000]
        if isinstance(error, str) and error:
            return error[:1000]
        for key in ("message", "msg", "detail", "error_description"):
            value = data.get(key)
            if value:
                return str(value)[:1000]
    return fallback


def classify_http_error(status_code: int, body: str) -> ProviderError:
    message = parse_error_message(body, f"HTTP {status_code}")
    lowered = f"{message} {body}".lower()
    context_markers = (
        "context length",
        "context_length",
        "maximum context",
        "too many tokens",
        "token limit",
        "prompt is too long",
        "input is too long",
        "超出上下文",
    )
    if status_code in (401, 403):
        return ProviderError(message, kind=ProviderErrorKind.AUTH, status_code=status_code)
    if status_code == 429:
        return ProviderError(
            message,
            kind=ProviderErrorKind.RATE_LIMIT,
            status_code=status_code,
            retryable=True,
        )
    if status_code in (400, 413) and any(marker in lowered for marker in context_markers):
        return ProviderError(
            message,
            kind=ProviderErrorKind.CONTEXT_LENGTH,
            status_code=status_code,
        )
    if status_code >= 500:
        return ProviderError(
            message,
            kind=ProviderErrorKind.API,
            status_code=status_code,
            retryable=True,
        )
    kind = (
        ProviderErrorKind.CONFIGURATION
        if status_code in (404, 405)
        else ProviderErrorKind.API
    )
    return ProviderError(message, kind=kind, status_code=status_code)


def network_error(exc: Exception) -> ProviderError:
    """Convert httpx/transport exceptions without importing httpx here."""

    name = type(exc).__name__.lower()
    text = str(exc)
    if "timeout" in name or "timed out" in text.lower():
        return ProviderError(
            f"请求超时：{text}", kind=ProviderErrorKind.TIMEOUT, retryable=True
        )
    return ProviderError(f"网络错误：{text}", kind=ProviderErrorKind.NETWORK, retryable=True)


__all__ = [
    "BaseProvider",
    "ChatMessage",
    "GenerationRequest",
    "GenerationResult",
    "ProviderError",
    "ProviderErrorKind",
    "ProviderTestResult",
    "StreamEvent",
    "TokenUsage",
    "classify_http_error",
    "network_error",
    "parse_error_message",
]
