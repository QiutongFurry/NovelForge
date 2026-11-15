"""AI provider abstraction layer.

Public surface used by the rest of the application:

* low-level contracts from :mod:`novel_ai.providers.base`;
* the provider catalog used by the settings UI;
* concrete adapters (OpenAI-compatible family, Anthropic, Gemini);
* :class:`ProviderRegistry` for adapter construction;
* :class:`ProviderManager` for configuration, secrets and connection tests.
"""

from .anthropic import AnthropicProvider
from .base import (
    BaseProvider,
    ChatMessage,
    GenerationRequest,
    GenerationResult,
    ProviderError,
    ProviderErrorKind,
    ProviderTestResult,
    StreamEvent,
    TokenUsage,
    classify_http_error,
    network_error,
    parse_error_message,
)
from .catalog import (
    PROVIDER_CATALOG,
    ProviderSpec,
    normalize_provider_type,
    seed_provider_configs,
    spec_for,
)
from .gemini import GeminiProvider
from .manager import ProviderConfigStore, ProviderManager
from .openai_compat import (
    DeepSeekProvider,
    KimiProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    QwenProvider,
)
from .registry import DEFAULT_ADAPTERS, ProviderRegistry

__all__ = [
    # base
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
    # catalog
    "PROVIDER_CATALOG",
    "ProviderSpec",
    "normalize_provider_type",
    "seed_provider_configs",
    "spec_for",
    # adapters
    "AnthropicProvider",
    "DeepSeekProvider",
    "GeminiProvider",
    "KimiProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "QwenProvider",
    # registry / manager
    "DEFAULT_ADAPTERS",
    "ProviderConfigStore",
    "ProviderManager",
    "ProviderRegistry",
]
