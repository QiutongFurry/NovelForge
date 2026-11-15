"""Provider configuration management and adapter construction.

The manager is the single entry point used by services and the UI:

* it reads/writes :class:`~novel_ai.domain.models.ProviderConfig` rows through a
  :class:`ProviderConfigStore` (implemented by the storage repository);
* it keeps API keys in :class:`~novel_ai.config.secrets.SecretStore` only;
* it builds ready-to-use adapters through :class:`ProviderRegistry`.

No API key is ever logged or stored inside a provider configuration object.
"""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from novel_ai.config.secrets import SecretStore, mask_secret
from novel_ai.domain.models import ProviderConfig

from .base import BaseProvider, ProviderError, ProviderErrorKind, ProviderTestResult
from .catalog import seed_provider_configs
from .registry import ProviderRegistry

logger = logging.getLogger(__name__)


class ProviderConfigStore(Protocol):
    """Storage operations required from the repository layer."""

    def list_provider_configs(self) -> list[ProviderConfig]: ...

    def get_provider_config(self, provider_id: str) -> ProviderConfig | None: ...

    def save_provider_config(self, config: ProviderConfig) -> ProviderConfig: ...

    def delete_provider_config(self, provider_id: str) -> None: ...

    def set_default_provider(self, provider_id: str) -> None: ...


class ProviderManager:
    """High level API for provider configuration and adapter creation."""

    def __init__(
        self,
        store: ProviderConfigStore,
        secret_store: SecretStore,
        *,
        registry: ProviderRegistry | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.store = store
        self.secret_store = secret_store
        self.registry = registry if registry is not None else ProviderRegistry()
        self._transport = transport

    # -- configuration CRUD -----------------------------------------------
    def list_configs(self) -> list[ProviderConfig]:
        return self.store.list_provider_configs()

    def get_config(self, provider_id: str) -> ProviderConfig | None:
        return self.store.get_provider_config(provider_id)

    def default_config(self) -> ProviderConfig | None:
        """Return the default provider (falling back to the first configured)."""

        configs = self.store.list_provider_configs()
        if not configs:
            return None
        for config in configs:
            if config.is_default:
                return config
        return configs[0]

    def save_config(
        self, config: ProviderConfig, *, api_key: str | None = None
    ) -> ProviderConfig:
        """Persist a configuration; a non-empty ``api_key`` replaces the secret."""

        if api_key is not None and api_key.strip():
            self.secret_store.save_provider_key(config.id, api_key.strip())
        saved = self.store.save_provider_config(config)
        if saved.is_default:
            # Contract: saving ``is_default=True`` must mark it default.
            self.store.set_default_provider(saved.id)
        logger.debug(
            "Saved provider config %s (type=%s, default=%s)",
            saved.id,
            saved.provider_type,
            saved.is_default,
        )
        return saved

    def delete_config(self, provider_id: str) -> None:
        self.store.delete_provider_config(provider_id)
        try:
            self.secret_store.delete_provider_key(provider_id)
        except Exception:  # pragma: no cover - defensive around keyring backends
            logger.warning("Failed to delete secret for provider %s", provider_id)

    def set_default(self, provider_id: str) -> None:
        if self.store.get_provider_config(provider_id) is None:
            raise ProviderError(
                f"供应商配置不存在：{provider_id}",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        self.store.set_default_provider(provider_id)

    # -- API keys -----------------------------------------------------------
    def load_key(self, provider_id: str) -> str | None:
        return self.secret_store.load_provider_key(provider_id)

    def delete_key(self, provider_id: str) -> None:
        self.secret_store.delete_provider_key(provider_id)

    def key_configured(self, provider_id: str) -> bool:
        return bool((self.load_key(provider_id) or "").strip())

    def masked_key(self, provider_id: str) -> str:
        """Display-safe key fingerprint; never returns the full key."""

        return mask_secret(self.load_key(provider_id))

    # -- adapter construction ----------------------------------------------
    def build_provider(
        self,
        config: ProviderConfig | None = None,
        *,
        api_key_override: str | None = None,
    ) -> BaseProvider:
        target = config if config is not None else self.default_config()
        if target is None:
            raise ProviderError(
                "尚未配置任何 AI 供应商，请先在「API 配置」中添加。",
                kind=ProviderErrorKind.CONFIGURATION,
            )
        if api_key_override is not None:
            api_key = api_key_override
        else:
            api_key = self.load_key(target.id)
        return self.registry.create(target, api_key, transport=self._transport)

    def test_connection(
        self,
        config: ProviderConfig,
        *,
        api_key_override: str | None = None,
    ) -> ProviderTestResult:
        """Make a tiny request; never raises for expected failures."""

        try:
            provider = self.build_provider(
                config, api_key_override=api_key_override
            )
            return provider.test_connection()
        except ProviderError as exc:
            return ProviderTestResult(
                ok=False,
                message=exc.user_message,
                model=config.model,
                detail=exc.detail or exc.message,
            )
        except Exception as exc:  # defensive: UI must always get a result
            logger.debug("Unexpected provider test failure: %s", exc)
            return ProviderTestResult(
                ok=False,
                message=f"连接测试失败：{exc}",
                model=config.model,
            )

    # -- first run ----------------------------------------------------------
    def ensure_seeded(self) -> list[ProviderConfig]:
        """Create the catalog's provider configs on first run (idempotent)."""

        existing = self.store.list_provider_configs()
        if existing:
            return existing
        seeded: list[ProviderConfig] = []
        for config in seed_provider_configs():
            saved = self.store.save_provider_config(config)
            if saved.is_default:
                self.store.set_default_provider(saved.id)
            seeded.append(saved)
        logger.info("Seeded %d provider configurations", len(seeded))
        return seeded


__all__ = ["ProviderConfigStore", "ProviderManager"]
