"""Filesystem locations used by the application.

The data directory can be overridden with ``NOVEL_AI_HOME`` which is also what
the test-suite and portable installations use.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def default_data_dir() -> Path:
    override = os.environ.get("NOVEL_AI_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "NovelAIStudio"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "NovelAIStudio"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / (
        "novel-ai-studio"
    )


@dataclass(frozen=True)
class ConfigPaths:
    root: Path

    @classmethod
    def create(cls, root: str | Path | None = None) -> "ConfigPaths":
        return cls(Path(root).expanduser().resolve() if root else default_data_dir())

    @property
    def database_path(self) -> Path:
        return self.root / "novel.db"

    @property
    def settings_path(self) -> Path:
        return self.root / "settings.json"

    @property
    def secrets_path(self) -> Path:
        return self.root / "secrets.json"

    @property
    def prompts_path(self) -> Path:
        return self.root / "prompt_templates.json"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    def ensure(self) -> "ConfigPaths":
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        return self
