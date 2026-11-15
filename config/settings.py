"""Application settings persisted as human-readable JSON."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AppSettings:
    theme: str = "dark"  # dark | light
    last_project_id: str | None = None
    auto_save_seconds: int = 30
    auto_update_memory: bool = True
    confirm_before_overwrite: bool = True
    context_preview_default: bool = False
    recent_chapter_count: int = 2
    retrieval_top_k: int = 6
    retrieval_enabled: bool = True
    window_geometry: str = ""
    log_level: str = "INFO"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        allowed = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in allowed}
        return cls(**clean)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AppSettingsService:
    """Loads/saves :class:`AppSettings` atomically.

    Corrupt settings never stop application start-up: the invalid file is
    renamed and defaults are used.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("settings root must be an object")
            return AppSettings.from_dict(raw)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Could not read settings, using defaults: %s", exc)
            try:
                backup = self.path.with_suffix(".corrupt.json")
                self.path.replace(backup)
            except OSError:
                pass
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
