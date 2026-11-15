"""Base class for all navigable pages."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QWidget

from novel_ai.domain.models import Project
from novel_ai.ui.app_context import AppContext


class BasePage(QWidget):
    page_key: str = ""
    page_title: str = ""
    page_subtitle: str = ""

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx

    # Lifecycle hooks called by MainWindow.  Pages should refresh gracefully even
    # when no project is open.
    def on_project_changed(self, project: Project | None) -> None:  # noqa: B027
        pass

    def on_navigated(self, payload: object = None) -> None:  # noqa: B027
        pass

    def refresh(self) -> None:  # noqa: B027
        pass
