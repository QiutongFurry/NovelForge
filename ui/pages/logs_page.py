"""Generation log page.

Reads persisted :class:`~novel_ai.domain.models.GenerationLog` rows from the
project service.  Logging itself happens in the generation service/worker, so
this page is read-only and never performs network requests.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from novel_ai.ui.icons import icon
from novel_ai.ui.pages.base import BasePage
from novel_ai.ui.theme import current_palette
from novel_ai.ui.widgets import Badge, Card, EmptyState, MutedLabel, PageHeader

logger = logging.getLogger(__name__)

_COLUMNS: list[str] = [
    "时间",
    "状态",
    "供应商",
    "模型",
    "提示词 tokens",
    "完成 tokens",
    "耗时",
    "错误",
]

_STATUS_MAP: dict[str, tuple[str, str]] = {
    "success": ("成功", "success"),
    "error": ("失败", "danger"),
    "cancelled": ("已取消", "warning"),
}

_LIMIT_OPTIONS: tuple[int, ...] = (50, 100, 200, 500)


class LogsPage(BasePage):
    page_key = "logs"
    page_title = "生成日志"
    page_subtitle = "查看每次 AI 生成的供应商、模型、token 用量与耗时，用于排查失败原因。"

    def __init__(self, ctx, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self.scope = "project"  # project | all
        self._busy = False
        self._build_ui()
        self._connect_signals()
        self.on_project_changed(getattr(ctx, "current_project", None))

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 24)
        root.setSpacing(12)

        self.header = PageHeader(self.page_title, self.page_subtitle)
        self.open_button = QPushButton("打开日志目录")
        self.open_button.setIcon(icon("folder"))
        self.open_button.setProperty("variant", "secondary")
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setIcon(icon("refresh"))
        self.refresh_button.setProperty("variant", "secondary")
        for button in (self.open_button, self.refresh_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.header.add_action(button)
        root.addWidget(self.header)

        controls = Card(padding=12, spacing=8)
        controls_row = QHBoxLayout()
        controls_row.setSpacing(10)
        scope_label = QLabel("范围")
        scope_label.setProperty("role", "caption")
        self.scope_combo = QComboBox()
        self.scope_combo.setMinimumWidth(150)
        limit_label = QLabel("显示条数")
        limit_label.setProperty("role", "caption")
        self.limit_combo = QComboBox()
        for limit in _LIMIT_OPTIONS:
            self.limit_combo.addItem(f"最近 {limit} 条", limit)
        self.limit_combo.setCurrentIndex(1)  # 100 rows by default
        self.count_label = QLabel("共 0 条记录")
        self.count_label.setProperty("role", "hint")
        controls_row.addWidget(scope_label)
        controls_row.addWidget(self.scope_combo)
        controls_row.addSpacing(8)
        controls_row.addWidget(limit_label)
        controls_row.addWidget(self.limit_combo)
        controls_row.addStretch(1)
        controls_row.addWidget(self.count_label)
        controls.content_layout.addLayout(controls_row)
        root.addWidget(controls)

        busy_card = Card(padding=12, spacing=8)
        busy_row = QHBoxLayout()
        busy_row.setSpacing(10)
        self.busy_badge = Badge("空闲", "neutral")
        self.busy_label = MutedLabel("当前没有正在运行的生成任务。")
        busy_row.addWidget(self.busy_badge)
        busy_row.addWidget(self.busy_label, 1)
        busy_card.content_layout.addLayout(busy_row)
        root.addWidget(busy_card)

        self.stack = QStackedWidget()
        self.table = self._build_table()
        self.stack.addWidget(self.table)
        self.empty_page = EmptyState(
            "暂无生成日志",
            "完成一次 AI 生成后，这里会记录供应商、模型、token 用量与耗时。",
            action_text="刷新",
            icon_name="logs",
        )
        self.stack.addWidget(self.empty_page)
        root.addWidget(self.stack, 1)

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(0, len(_COLUMNS))
        table.setHorizontalHeaderLabels(_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setAlternatingRowColors(False)
        table.setMinimumHeight(260)
        header = table.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(True)
        for column in range(len(_COLUMNS) - 1):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        return table

    def _connect_signals(self) -> None:
        self.refresh_button.clicked.connect(self.refresh)
        self.open_button.clicked.connect(self._open_logs_dir)
        self.scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        self.limit_combo.currentIndexChanged.connect(lambda *_: self.refresh())
        self.empty_page.actionClicked.connect(self._on_empty_action)
        try:
            self.ctx.busyChanged.connect(self._on_busy_changed)
        except Exception:  # pragma: no cover - context always exposes the signal
            logger.warning("AppContext.busyChanged is unavailable")

    # -------------------------------------------------------------- refresh
    def refresh(self) -> None:
        try:
            self._refresh_data()
        except Exception:  # pragma: no cover - defensive UI boundary
            logger.exception("Could not refresh the generation log page")

    def _refresh_data(self) -> None:
        project = getattr(self.ctx, "current_project", None)
        self._sync_scope_combo(project)
        limit = int(self.limit_combo.currentData() or 100)

        if project is None and self.scope == "project":
            self.header.set_subtitle("未打开项目 · 暂无项目生成日志")
            self.count_label.setText("0 条记录")
            self._show_empty(
                "尚未打开项目",
                "打开项目后可查看该项目的生成记录；也可以查看全部项目的生成日志。",
                action_text="查看全部项目日志",
            )
            return

        project_id = (
            project.id if (project is not None and self.scope == "project") else None
        )
        logs = []
        error = ""
        try:
            logs = list(self.ctx.project_service.list_logs(project_id, limit))
        except Exception as exc:
            logger.warning("Could not load generation logs: %s", exc)
            error = str(exc)

        if error:
            self.header.set_subtitle(self.page_subtitle)
            self.count_label.setText("读取失败")
            self._show_empty(
                "读取日志失败",
                f"无法读取生成日志：{error}",
                action_text="重试",
            )
            return

        self.count_label.setText(f"共 {len(logs)} 条记录")
        if project is None:
            self.header.set_subtitle(
                f"未打开项目 · 显示全部项目最近 {limit} 条生成日志"
            )
        elif self.scope == "project":
            self.header.set_subtitle(
                f"当前项目：{project.title} · 最近 {limit} 条生成记录"
            )
        else:
            self.header.set_subtitle(f"全部项目 · 最近 {limit} 条生成记录")

        if not logs:
            self._show_empty(
                "暂无生成日志",
                "完成一次 AI 生成后，这里会记录供应商、模型、token 用量与耗时。",
                action_text="刷新",
            )
            return

        self._populate_table(logs)
        self.stack.setCurrentWidget(self.table)

    def _sync_scope_combo(self, project) -> None:  # noqa: ANN001
        self.scope_combo.blockSignals(True)
        try:
            self.scope_combo.clear()
            if project is None:
                self.scope_combo.addItem("全部项目", "all")
                self.scope_combo.setEnabled(False)
                self.scope_combo.setCurrentIndex(0)
            else:
                self.scope_combo.addItem("当前项目", "project")
                self.scope_combo.addItem("全部项目", "all")
                self.scope_combo.setEnabled(True)
                self.scope_combo.setCurrentIndex(1 if self.scope == "all" else 0)
        finally:
            self.scope_combo.blockSignals(False)

    def _populate_table(self, logs: list) -> None:
        palette = self._palette()
        self.table.setUpdatesEnabled(False)
        try:
            self.table.clearContents()
            self.table.setRowCount(len(logs))
            for row, log in enumerate(logs):
                status_key = str(getattr(log, "status", "") or "")
                status_text, color_key = _STATUS_MAP.get(
                    status_key, (status_key or "未知", "text")
                )
                created_at = str(getattr(log, "created_at", "") or "")
                error = str(getattr(log, "error", "") or "")
                values = [
                    self._format_time(created_at),
                    status_text,
                    str(getattr(log, "provider_name", "") or "—"),
                    str(getattr(log, "model", "") or "—"),
                    self._format_number(getattr(log, "prompt_tokens", 0)),
                    self._format_number(getattr(log, "completion_tokens", 0)),
                    self._format_duration(getattr(log, "duration_ms", 0)),
                    self._format_error(error),
                ]
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    )
                    if column == 1:
                        item.setForeground(
                            QColor(palette.get(color_key, palette.get("text", "#E9EDF7")))
                        )
                    if column in (4, 5, 6):
                        item.setTextAlignment(
                            Qt.AlignmentFlag.AlignRight
                            | Qt.AlignmentFlag.AlignVCenter
                        )
                    if column == 0:
                        item.setToolTip(created_at)
                    elif column == 3:
                        item.setToolTip(str(getattr(log, "model", "") or ""))
                    elif column == 7:
                        item.setToolTip(error)
                    self.table.setItem(row, column, item)
        finally:
            self.table.setUpdatesEnabled(True)

    # -------------------------------------------------------------- helpers
    def _palette(self) -> dict[str, str]:
        mode = "dark"
        settings = getattr(self.ctx, "settings", None)
        if settings is not None and getattr(settings, "theme", None):
            mode = str(settings.theme)
        try:
            return current_palette(mode)
        except Exception:  # pragma: no cover - defensive
            return current_palette("dark")

    @staticmethod
    def _format_time(value: str) -> str:
        text = str(value or "")
        if "T" in text:
            text = text.replace("T", " ")
        return text[:19]

    @staticmethod
    def _format_number(value) -> str:  # noqa: ANN001
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            return str(value)
        return f"{number:,}"

    @staticmethod
    def _format_duration(value) -> str:  # noqa: ANN001
        try:
            ms = int(value or 0)
        except (TypeError, ValueError):
            return str(value)
        if ms >= 1000:
            return f"{ms / 1000:.2f} s"
        return f"{ms} ms"

    @staticmethod
    def _format_error(value: str) -> str:
        text = " ".join(str(value or "").split())
        if not text:
            return "—"
        return text[:80] + ("…" if len(text) > 80 else "")

    def _show_empty(self, title: str, description: str, *, action_text: str) -> None:
        self.empty_page.title_label.setText(title)
        self.empty_page.description_label.setText(description)
        button = self.empty_page.action_button
        if button is not None:
            button.setText(action_text)
            button.setVisible(bool(action_text))
        self.stack.setCurrentWidget(self.empty_page)

    # -------------------------------------------------------------- actions
    def _on_scope_changed(self, _index: int = 0) -> None:
        data = str(self.scope_combo.currentData() or "project")
        self.scope = data if data in ("project", "all") else "project"
        self.refresh()

    def _on_empty_action(self) -> None:
        project = getattr(self.ctx, "current_project", None)
        if project is None and self.scope != "all":
            self.scope = "all"
        self.refresh()

    def _on_busy_changed(self, busy: bool, message: str) -> None:
        self._busy = bool(busy)
        if self._busy:
            self.busy_badge.setText("生成中")
            self.busy_badge.set_variant("warning")
            text = str(message or "").strip()
            self.busy_label.setText(
                text or "AI 任务正在后台运行，完成后会自动记录生成日志。"
            )
        else:
            self.busy_badge.setText("空闲")
            self.busy_badge.set_variant("neutral")
            self.busy_label.setText("当前没有正在运行的生成任务。")
            if self.isVisible():
                self.refresh()

    def _open_logs_dir(self) -> None:
        paths = getattr(self.ctx, "paths", None)
        log_dir = getattr(paths, "logs_dir", None)
        if log_dir is None:
            self.ctx.notify("无法定位日志目录。", "warning")
            return
        try:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
        except OSError as exc:
            self.ctx.notify(f"无法创建或打开日志目录：{exc}", "error")
            return
        except Exception as exc:  # pragma: no cover - defensive platform boundary
            self.ctx.notify(f"打开日志目录失败：{exc}", "error")
            return
        if not opened:
            self.ctx.notify(
                f"无法自动打开日志目录，请手动打开：{directory}", "warning"
            )

    # ------------------------------------------------------------ lifecycle
    def on_project_changed(self, project) -> None:  # noqa: ANN001 - BasePage hook
        self.scope = "project"
        self.refresh()

    def on_navigated(self, payload: object = None) -> None:
        self.refresh()


__all__ = ["LogsPage"]
