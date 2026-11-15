"""API key storage.

Preferred backend is the operating-system keyring.  If no keyring backend is
available (common on minimal Linux containers) the application falls back to a
user-only local file and clearly reports that weaker mode in the UI and README.
Keys are never written to the project database, logs, exports or source code.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

_KEYRING_SERVICE = "novel-ai-studio"


class _KeyringLike(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


def mask_secret(secret: str | None) -> str:
    """Return a display-safe fingerprint (never the full secret)."""

    if not secret:
        return "未配置"
    if len(secret) <= 8:
        return "•" * max(4, len(secret))
    return f"{secret[:4]}{'•' * 8}{secret[-4:]}"


class SecretStore:
    """Store provider keys either in the OS keyring or a local JSON file."""

    def __init__(
        self,
        fallback_path: Path,
        *,
        service_name: str = _KEYRING_SERVICE,
        prefer_keyring: bool = True,
        keyring_module: _KeyringLike | None = None,
    ) -> None:
        self.fallback_path = Path(fallback_path)
        self.service_name = service_name
        self._keyring: _KeyringLike | None = None
        if prefer_keyring:
            self._keyring = keyring_module or self._load_keyring()
        self.backend = "keyring" if self._keyring else "local-file"

    @staticmethod
    def _load_keyring() -> _KeyringLike | None:
        try:
            import keyring  # type: ignore[import-not-found]

            try:
                keyring.get_keyring()
            except Exception:  # pragma: no cover - platform keyring quirks
                return None
            return keyring
        except Exception:
            return None

    # -- provider helpers ---------------------------------------------------
    def save_provider_key(self, provider_id: str, api_key: str) -> None:
        self.set_secret(f"provider:{provider_id}", api_key)

    def load_provider_key(self, provider_id: str) -> str | None:
        return self.get_secret(f"provider:{provider_id}")

    def delete_provider_key(self, provider_id: str) -> None:
        self.delete_secret(f"provider:{provider_id}")

    # -- generic API --------------------------------------------------------
    def get_secret(self, name: str) -> str | None:
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(self.service_name, name)
                if value:
                    return value
            except Exception as exc:  # pragma: no cover - backend-specific
                logger.warning("Keyring read failed for %s: %s", name, exc)
        data = self._read_fallback()
        return data.get(name)

    def set_secret(self, name: str, value: str) -> None:
        if not value:
            self.delete_secret(name)
            return
        if self._keyring is not None:
            try:
                self._keyring.set_password(self.service_name, name, value)
                # Remove a stale plain-file copy if the user had an older install.
                data = self._read_fallback()
                if name in data:
                    data.pop(name, None)
                    self._write_fallback(data)
                return
            except Exception as exc:  # pragma: no cover - backend-specific
                logger.warning("Keyring write failed, using local file: %s", exc)
        data = self._read_fallback()
        data[name] = value
        self._write_fallback(data)

    def delete_secret(self, name: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(self.service_name, name)
            except Exception:
                pass
        data = self._read_fallback()
        if name in data:
            data.pop(name, None)
            self._write_fallback(data)

    # -- local fallback -----------------------------------------------------
    def _read_fallback(self) -> dict[str, str]:
        if not self.fallback_path.exists():
            return {}
        try:
            raw = json.loads(self.fallback_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError):
            logger.warning("Local secret file is unreadable; ignoring it")
        return {}

    def _write_fallback(self, data: dict[str, str]) -> None:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.fallback_path.with_suffix(".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(tmp, self.fallback_path)
        try:
            os.chmod(self.fallback_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
