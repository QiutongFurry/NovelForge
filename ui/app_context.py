"""UI state container shared by all pages.

Pages never construct services themselves.  They receive the single
:class:`AppContext` created in :mod:`novel_ai.app`; this keeps navigation,
project switching and notifications consistent across the app.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from novel_ai.config.paths import ConfigPaths
from novel_ai.config.settings import AppSettings, AppSettingsService
from novel_ai.domain.models import Project, ProjectSnapshot

if TYPE_CHECKING:  # pragma: no cover - typing only
    from novel_ai.providers.manager import ProviderManager
    from novel_ai.prompts.templates import PromptStore
    from novel_ai.services.export_service import ExportService
    from novel_ai.services.generation_service import GenerationService
    from novel_ai.services.project_service import ProjectService
    from novel_ai.services.summary_service import SummaryService
    from novel_ai.storage.repository import NovelRepository

logger = logging.getLogger(__name__)


class AppContext(QObject):
    projectChanged = Signal(object)  # Project | None
    dataChanged = Signal(str)  # project/characters/outline/chapters/memory/providers
    providerChanged = Signal()
    themeChanged = Signal(str)
    navigateRequested = Signal(str, object)  # page_key, payload
    toastRequested = Signal(str, str)  # message, level
    busyChanged = Signal(bool, str)  # busy, message
    usageChanged = Signal(int, int)  # prompt tokens, completion tokens

    def __init__(
        self,
        *,
        paths: ConfigPaths,
        settings: AppSettings,
        settings_service: AppSettingsService,
        repo: "NovelRepository",
        project_service: "ProjectService",
        provider_manager: "ProviderManager",
        prompt_store: "PromptStore",
        export_service: "ExportService",
        generation_service: "GenerationService",
        summary_service: "SummaryService",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.paths = paths
        self.settings = settings
        self.settings_service = settings_service
        self.repo = repo
        self.project_service = project_service
        self.provider_manager = provider_manager
        self.prompt_store = prompt_store
        self.export_service = export_service
        self.generation_service = generation_service
        self.summary_service = summary_service
        self.current_project: Project | None = None
        self.snapshot: ProjectSnapshot | None = None

    # ------------------------------------------------------------------ project
    def open_project(self, project_id: str) -> Project | None:
        snapshot = self.project_service.load_snapshot(project_id)
        if snapshot is None:
            self.notify("项目不存在或已被删除。", "error")
            return None
        self.snapshot = snapshot
        self.current_project = snapshot.project
        self.settings.last_project_id = project_id
        self.save_settings()
        self.projectChanged.emit(self.current_project)
        return self.current_project

    def close_project(self) -> None:
        self.current_project = None
        self.snapshot = None
        self.settings.last_project_id = None
        self.save_settings()
        self.projectChanged.emit(None)

    def update_current_project(self, project: Project) -> None:
        self.current_project = project
        if self.snapshot is not None:
            self.snapshot.project = project
        self.projectChanged.emit(project)

    def refresh_snapshot(self) -> ProjectSnapshot | None:
        if self.current_project is None:
            return None
        snapshot = self.project_service.load_snapshot(self.current_project.id)
        if snapshot is not None:
            self.snapshot = snapshot
            self.current_project = snapshot.project
        return self.snapshot

    def require_project(self) -> Project | None:
        if self.current_project is None:
            self.notify("请先创建或打开一个项目。", "warning")
        return self.current_project

    # ------------------------------------------------------------------ events
    def navigate(self, page_key: str, payload: Any = None) -> None:
        self.navigateRequested.emit(page_key, payload)

    def notify(self, message: str, level: str = "info") -> None:
        """Show a transient notification; level: info/success/warning/error."""

        logger.log(
            logging.ERROR if level == "error" else logging.INFO,
            "notify[%s]: %s",
            level,
            message,
        )
        self.toastRequested.emit(message, level)

    def set_busy(self, busy: bool, message: str = "") -> None:
        self.busyChanged.emit(busy, message)

    def set_usage(self, prompt_tokens: int, completion_tokens: int = 0) -> None:
        self.usageChanged.emit(max(0, prompt_tokens), max(0, completion_tokens))

    # ------------------------------------------------------------------ settings
    def save_settings(self) -> None:
        try:
            self.settings_service.save(self.settings)
        except OSError as exc:
            logger.warning("Could not save settings: %s", exc)

    def set_theme(self, theme: str) -> None:
        if theme not in ("dark", "light"):
            return
        self.settings.theme = theme
        self.save_settings()
        self.themeChanged.emit(theme)

    # ------------------------------------------------------------------ providers
    def provider_config_for_generation(self):
        project = self.current_project
        if project and project.default_provider_id:
            config = self.provider_manager.get_config(project.default_provider_id)
            if config is not None:
                return config
        return self.provider_manager.default_config()

    def mark_data_changed(self, domain: str) -> None:
        self.dataChanged.emit(domain)
