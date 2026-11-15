"""Static catalog of the AI providers supported out of the box.

The catalog is the single source of truth for the provider picker in the UI and
for :func:`seed_provider_configs`, which creates one editable configuration per
provider on first start.  It deliberately contains no secrets: API keys live in
:class:`novel_ai.config.secrets.SecretStore` only.
"""

from __future__ import annotations

from dataclasses import dataclass

from novel_ai.domain.models import ProviderConfig


@dataclass(frozen=True)
class ProviderSpec:
    """Immutable description of one supported provider type."""

    provider_type: str
    display_name: str
    base_url: str
    default_model: str
    context_window: int
    requires_api_key: bool
    description: str = ""


PROVIDER_CATALOG: list[ProviderSpec] = [
    ProviderSpec(
        provider_type="openai",
        display_name="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        context_window=128_000,
        requires_api_key=True,
        description="OpenAI 官方 API，支持 GPT-4o / GPT-4.1 等模型。",
    ),
    ProviderSpec(
        provider_type="deepseek",
        display_name="DeepSeek 深度求索",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
        context_window=128_000,
        requires_api_key=True,
        description="DeepSeek 官方 API，中文创作性价比高，支持 deepseek-chat / deepseek-reasoner。",
    ),
    ProviderSpec(
        provider_type="qwen",
        display_name="通义千问 Qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
        context_window=131_072,
        requires_api_key=True,
        description="阿里云百炼 DashScope 的 OpenAI 兼容接口，提供通义千问系列模型。",
    ),
    ProviderSpec(
        provider_type="kimi",
        display_name="Kimi 月之暗面",
        base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-32k",
        context_window=32_768,
        requires_api_key=True,
        description="月之暗面 Moonshot 的 OpenAI 兼容接口，长文本理解能力强。",
    ),
    ProviderSpec(
        provider_type="anthropic",
        display_name="Anthropic Claude",
        base_url="https://api.anthropic.com",
        default_model="claude-sonnet-4-5",
        context_window=200_000,
        requires_api_key=True,
        description="Anthropic 官方 Messages API，Claude 系列模型擅长长文与细腻文风。",
    ),
    ProviderSpec(
        provider_type="gemini",
        display_name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        default_model="gemini-2.0-flash",
        context_window=1_048_576,
        requires_api_key=True,
        description="Google Gemini 官方 API，超长上下文，适合长篇连载。",
    ),
    ProviderSpec(
        provider_type="ollama",
        display_name="Ollama 本地",
        base_url="http://localhost:11434/v1",
        default_model="llama3.1",
        context_window=32_768,
        requires_api_key=False,
        description="本地 Ollama 运行时（OpenAI 兼容接口），无需 API Key，数据不出本机。",
    ),
    ProviderSpec(
        provider_type="openai_compatible",
        display_name="自定义 OpenAI 兼容",
        base_url="http://localhost:8000/v1",
        default_model="gpt-4o-mini",
        context_window=32_768,
        requires_api_key=False,
        description="任意兼容 OpenAI Chat Completions 协议的服务（vLLM / LM Studio / OneAPI / OpenRouter 等），请按服务商文档填写 Base URL、模型名与密钥。",
    ),
]

_SPEC_BY_TYPE: dict[str, ProviderSpec] = {
    spec.provider_type: spec for spec in PROVIDER_CATALOG
}

# Common spelling variants mapped to canonical catalog keys.
_ALIASES: dict[str, str] = {
    "openai_compat": "openai_compatible",
    "openai_compatible_api": "openai_compatible",
    "openai_api": "openai",
    "deep_seek": "deepseek",
    "moonshot": "kimi",
    "moonshot_ai": "kimi",
    "dashscope": "qwen",
    "tongyi": "qwen",
    "tongyi_qianwen": "qwen",
    "alibaba": "qwen",
    "google": "gemini",
    "google_ai": "gemini",
    "claude": "anthropic",
    "local": "ollama",
    "lm_studio": "openai_compatible",
    "vllm": "openai_compatible",
}


def normalize_provider_type(provider_type: str | None) -> str:
    """Return the canonical provider type for user input or legacy values."""

    value = (provider_type or "").strip().lower()
    value = value.replace("-", "_").replace(" ", "_")
    return _ALIASES.get(value, value)


def spec_for(provider_type: str | None) -> ProviderSpec | None:
    """Look up a catalog entry, tolerating aliases and odd casing."""

    if not provider_type:
        return None
    return _SPEC_BY_TYPE.get(normalize_provider_type(provider_type))


def seed_provider_configs() -> list[ProviderConfig]:
    """Build the first-run provider configurations (never containing keys).

    One configuration is created for every catalog entry.  The first entry is
    marked as the default provider; users may change or delete it like any other.
    """

    configs: list[ProviderConfig] = []
    for index, spec in enumerate(PROVIDER_CATALOG):
        configs.append(
            ProviderConfig.create(
                name=spec.display_name,
                provider_type=spec.provider_type,
                base_url=spec.base_url,
                model=spec.default_model,
                context_window=spec.context_window,
                is_default=index == 0,
            )
        )
    return configs


__all__ = [
    "PROVIDER_CATALOG",
    "ProviderSpec",
    "normalize_provider_type",
    "seed_provider_configs",
    "spec_for",
]
