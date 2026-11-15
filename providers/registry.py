"""Provider registry: maps provider types to adapter implementations.

A single registry instance can be shared by the whole application.  The default
registration covers every provider in :mod:`novel_ai.providers.catalog`; tests or
future plugins may pass a custom mapping to the constructor or call
:meth:`ProviderRegistry.register`.
"""

from __future__ import annotations

import inspect
from typing import Mapping

import httpx

from novel_ai.domain.models import ProviderConfig

from .anthropic import AnthropicProvider
from .base import BaseProvider, ProviderError, ProviderErrorKind
from .catalog import ProviderSpec, normalize_provider_type, spec_for
from .gemini import GeminiProvider
from .openai_compat import (
    DeepSeekProvider,
    KimiProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    QwenProvider,
)

#: Built-in provider type -> adapter class mapping (in catalog order).
DEFAULT_ADAPTERS: dict[str, type[BaseProvider]] = {
    "openai": OpenAIProvider,
    "deepseek": DeepSeekProvider,
    "qwen": QwenProvider,
    "kimi": KimiProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


class ProviderRegistry:
    """Create provider adapters from a :class:`ProviderConfig`."""

    def __init__(
        self,
        adapters: Mapping[str, type[BaseProvider]] | None = None,
    ) -> None:
        self._adapters: dict[str, type[BaseProvider]] = (
            dict(DEFAULT_ADAPTERS) if adapters is None else dict(adapters)
        )

    # -- registration -----------------------------------------------------
    def register(
        self, provider_type: str, adapter: type[BaseProvider]
    ) -> None:
        """Register (or replace) the adapter for ``provider_type``."""

        if not (isinstance(adapter, type) and issubclass(adapter, BaseProvider)):
            raise TypeError("adapter 必须是 BaseProvider 的子类")
        self._adapters[normalize_provider_type(provider_type)] = adapter

    def adapter_class(self, provider_type: str) -> type[BaseProvider]:
        """Resolve a provider type (tolerating aliases) to its adapter class."""

        key = normalize_provider_type(provider_type)
        adapter = self._adapters.get(key)
        if adapter is None:
            raise ProviderError(
                f"不支持的供应商类型：{provider_type or '（空）'}",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        return adapter

    # -- public API (see docs/CONTRACTS.md section 3) ----------------------
    def create(
        self,
        config: ProviderConfig,
        api_key: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> BaseProvider:
        """Instantiate the adapter for ``config``, injecting the API key."""

        adapter = self.adapter_class(config.provider_type)
        parameters = inspect.signature(adapter).parameters
        if "transport" in parameters:
            return adapter(config, api_key, transport=transport)
        # A third-party adapter may not support transport injection.
        return adapter(config, api_key)

    def available_types(self) -> list[ProviderSpec]:
        """Return one :class:`ProviderSpec` per registered provider type."""

        specs: list[ProviderSpec] = []
        for provider_type, adapter in self._adapters.items():
            spec = spec_for(provider_type)
            if spec is None:
                spec = ProviderSpec(
                    provider_type=provider_type,
                    display_name=str(
                        getattr(adapter, "display_name", provider_type)
                    ),
                    base_url=str(getattr(adapter, "default_base_url", "")),
                    default_model=str(getattr(adapter, "default_model", "")),
                    context_window=int(
                        getattr(adapter, "default_context_window", 128_000)
                    ),
                    requires_api_key=bool(
                        getattr(adapter, "requires_api_key", True)
                    ),
                    description="",
                )
            specs.append(spec)
        return specs


__all__ = ["DEFAULT_ADAPTERS", "ProviderRegistry"]
