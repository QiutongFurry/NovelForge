"""Main application window: sidebar, top status bar, pages and global actions."""

from __future__ import annotations

import base64
import importlib
import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from novel_ai import APP_NAME, __version__
from novel_ai.domain.models import Project
from novel_ai.ui.app_context import AppContext
from novel_ai.ui.icons import icon
from novel_ai.ui.pages.base import BasePage
from novel_ai.ui.theme import apply_theme
from novel_ai.ui.widgets import (
    Badge,
    ConfirmDialog,
    EmptyState,
    Toast,
    divider,
)

logger = logging.getLogger(__name__)

NAV_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "工作台",
        [
            ("dashboard", "项目管理", "home"),
            ("settings", "小说设定", "book"),
            ("characters", "角色管理", "users"),
            ("outline", "大纲管理", "outline"),
            ("editor", "章节创作", "editor"),
        ],
    ),
    ("AI 协作", [("api", "API 配置", "key"), ("prompt", "Prompt 模板", "prompt")]),
    ("故事记忆", [("memory", "上下文与记忆", "memory")]),
    ("系统", [("logs", "运行日志", "logs")]),
]

PAGE_IMPORTS: dict[str, tuple[str, str]] = {
    "dashboard": ("novel_ai.ui.pages.dashboard_page", "DashboardPage"),
    "settings": ("novel_ai.ui.pages.project_settings_page", "ProjectSettingsPage"),
    "characters": ("novel_ai.ui.pages.characters_page", "CharactersPage"),
    "outline": ("novel_ai.ui.pages.outline_page", "OutlinePage"),
    "editor": ("novel_ai.ui.pages.editor_page", "EditorPage"),
    "api": ("novel_ai.ui.pages.api_config_page", "ApiConfigPage"),
    "prompt": ("novel_ai.ui.pages.prompt_page", "PromptPage"),
    "memory": ("novel_ai.ui.pages.memory_page", "MemoryPage"),
    "logs": ("novel_ai.ui.pages.logs_page", "LogsPage"),
}


class UnavailablePage(BasePage):
    def __init__(
        self,
        ctx: AppContext,
        page_key: str,
        title: str,
        error: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(ctx, parent)
        self.page_key = page_key
        self.page_title = title
        layout = QVBoxLayout(self)
        state = EmptyState(
            f"页面暂时不可用：{title}",
            f"{error}\n请检查依赖或运行日志，然后重启应用。",
            icon_name="warning",
        )
        layout.addWidget(state)


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.pages: dict[str, BasePage] = {}
        self.nav_buttons: dict[str, QPushButton] = {}
        self._current_page_key = ""
        self._last_usage = (0, 0)
        self.setWindowTitle(f"{APP_NAME} · NovelForge")
        self.setWindowIcon(icon("sparkles", "#7C9DFF", 64))
        self.setMinimumSize(1180, 740)
        self.resize(1460, 920)
        self._build_ui()
        self._connect_signals()
        self._create_shortcuts()
        self._restore_window_state()
        self._open_last_project()
        self._switch_page("dashboard")
        self._refresh_provider_badge()
        self._on_usage_changed(0, 0)

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        self.central = QWidget()
        self.central.setObjectName("CentralHost")
        self.setCentralWidget(self.central)
        root = QHBoxLayout(self.central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.sidebar = self._build_sidebar()
        root.addWidget(self.sidebar)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self.topbar = self._build_topbar()
        right_layout.addWidget(self.topbar)
        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName("PageStack")
        right_layout.addWidget(self.page_stack, 1)
        root.addWidget(right, 1)

        status = QStatusBar()
        status.setSizeGripEnabled(False)
        self.status_label = QLabel("就绪")
        self.status_label.setProperty("role", "hint")
        status.addWidget(self.status_label, 1)
        self.storage_label = QLabel("本地数据")
        self.storage_label.setProperty("role", "hint")
        self.storage_label.setToolTip(str(self.ctx.paths.root))
        status.addPermanentWidget(self.storage_label)
        self.setStatusBar(status)

        self._create_pages()
        self.toast = Toast(self.central)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(246)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 20, 16, 16)
        layout.setSpacing(6)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        logo_holder = QLabel()
        logo_holder.setFixedSize(38, 38)
        logo_holder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_holder.setPixmap(
            icon("sparkles", self._accent(), 24, mode=self.ctx.settings.theme).pixmap(28, 28)
        )
        logo_holder.setStyleSheet(
            "background: rgba(124,157,255,0.12); border: 1px solid rgba(124,157,255,0.25);"
            "border-radius: 11px;"
        )
        brand_row.addWidget(logo_holder)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_title = QLabel("墨言")
        brand_title.setObjectName("BrandTitle")
        brand_sub = QLabel("AI NOVEL STUDIO")
        brand_sub.setObjectName("BrandSubtitle")
        brand_text.addWidget(brand_title)
        brand_text.addWidget(brand_sub)
        brand_row.addLayout(brand_text, 1)
        layout.addLayout(brand_row)
        layout.addSpacing(12)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for group_title, items in NAV_GROUPS:
            label = QLabel(group_title.upper())
            label.setProperty("role", "caption")
            label.setStyleSheet(
                "letter-spacing: 1px; font-size: 10px; padding: 8px 10px 2px 10px;"
            )
            layout.addWidget(label)
            for page_key, title, icon_name in items:
                button = QPushButton(f"  {title}")
                button.setProperty("nav", "true")
                button.setCheckable(True)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setIcon(icon(icon_name, self._muted(), 18, mode=self.ctx.settings.theme))
                button.setIconSize(QSize(18, 18))
                button.setMinimumHeight(36)
                button.setProperty("page_key", page_key)
                button.clicked.connect(
                    lambda _checked=False, key=page_key: self._switch_page(key)
                )
                self.nav_group.addButton(button)
                self.nav_buttons[page_key] = button
                layout.addWidget(button)
            layout.addSpacing(4)

        layout.addStretch(1)
        layout.addWidget(divider())
        footer = QWidget()
        footer.setObjectName("SidebarFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(0, 8, 0, 0)
        footer_layout.setSpacing(6)
        self.theme_button = QPushButton()
        self.theme_button.setObjectName("ThemeToggle")
        self.theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_button.clicked.connect(self._toggle_theme)
        footer_layout.addWidget(self.theme_button)
        self.secret_hint = QLabel("")
        self.secret_hint.setProperty("role", "caption")
        self.secret_hint.setWordWrap(True)
        footer_layout.addWidget(self.secret_hint)
        version_label = QLabel(f"v{__version__} · 本地优先")
        version_label.setProperty("role", "caption")
        footer_layout.addWidget(version_label)
        layout.addWidget(footer)
        self._update_theme_labels()
        return sidebar

    def _build_topbar(self) -> QFrame:
        topbar = QFrame()
        topbar.setObjectName("TopBar")
        topbar.setFixedHeight(64)
        layout = QHBoxLayout(topbar)
        layout.setContentsMargins(24, 8, 20, 8)
        layout.setSpacing(10)

        project_icon = QLabel()
        project_icon.setPixmap(icon("folder", self._muted(), 16, mode=self.ctx.settings.theme).pixmap(18, 18))
        layout.addWidget(project_icon)
        project_box = QVBoxLayout()
        project_box.setSpacing(0)
        self.project_label = QLabel("未打开项目")
        self.project_label.setStyleSheet("font-size: 14px; font-weight: 600;")
        self.project_meta = QLabel("从项目管理开始你的故事")
        self.project_meta.setProperty("role", "caption")
        project_box.addWidget(self.project_label)
        project_box.addWidget(self.project_meta)
        layout.addLayout(project_box)
        self.settings_shortcut = QPushButton("设定")
        self.settings_shortcut.setProperty("variant", "ghost")
        self.settings_shortcut.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_shortcut.clicked.connect(lambda: self._switch_page("settings"))
        layout.addWidget(self.settings_shortcut)
        layout.addStretch(1)

        self.busy_badge = Badge("空闲", "neutral")
        layout.addWidget(self.busy_badge)

        self.provider_button = QPushButton("未配置模型")
        self.provider_button.setProperty("variant", "ghost")
        self.provider_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.provider_button.setIcon(icon("key", self._muted(), 16, mode=self.ctx.settings.theme))
        self.provider_button.clicked.connect(lambda: self._switch_page("api"))
        layout.addWidget(self.provider_button)

        self.usage_label = QLabel("Tokens 0 / 0")
        self.usage_label.setProperty("role", "hint")
        layout.addWidget(self.usage_label)

        self.export_button = QPushButton("导出")
        self.export_button.setIcon(
            icon("download", self._muted(), 16, mode=self.ctx.settings.theme)
        )
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        menu = QMenu(self.export_button)
        menu.addAction("整书导出为 Markdown", lambda: self._export_book("md"))
        menu.addAction("整书导出为 TXT", lambda: self._export_book("txt"))
        menu.addAction("整书导出为 Word（可选）", lambda: self._export_book("docx"))
        menu.addSeparator()
        menu.addAction("打开导出目录", self._open_exports_dir)
        self.export_button.setMenu(menu)
        self.export_button.setEnabled(False)
        layout.addWidget(self.export_button)

        self.top_theme_button = QPushButton()
        self.top_theme_button.setObjectName("IconButton")
        self.top_theme_button.setFixedSize(32, 32)
        self.top_theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.top_theme_button.setToolTip("切换深色/浅色主题")
        self.top_theme_button.clicked.connect(self._toggle_theme)
        layout.addWidget(self.top_theme_button)
        self._update_theme_labels()
        return topbar

    def _create_pages(self) -> None:
        for page_key, (module_name, class_name) in PAGE_IMPORTS.items():
            title = next(
                (
                    item_title
                    for _group, items in NAV_GROUPS
                    for key, item_title, _icon in items
                    if key == page_key
                ),
                page_key,
            )
            try:
                module = importlib.import_module(module_name)
                page_class = getattr(module, class_name)
                page = page_class(self.ctx)
            except Exception as exc:  # one broken page must not kill the app
                logger.exception("Failed to create page %s", page_key)
                page = UnavailablePage(self.ctx, page_key, title, str(exc))
            self.pages[page_key] = page
            self.page_stack.addWidget(page)
        self._refresh_secret_hint()

    # --------------------------------------------------------------- signals
    def _connect_signals(self) -> None:
        self.ctx.projectChanged.connect(self._on_project_changed)
        self.ctx.providerChanged.connect(self._on_provider_changed)
        self.ctx.themeChanged.connect(self._on_theme_changed)
        self.ctx.toastRequested.connect(self._show_toast)
        self.ctx.busyChanged.connect(self._on_busy_changed)
        self.ctx.usageChanged.connect(self._on_usage_changed)
        self.ctx.navigateRequested.connect(self._on_navigate_requested)

    def _create_shortcuts(self) -> None:
        save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        save_shortcut.activated.connect(self._save_current)
        generate_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        generate_shortcut.activated.connect(self._generate_current)
        nav_shortcut = QShortcut(QKeySequence("Ctrl+K"), self)
        nav_shortcut.activated.connect(lambda: self._switch_page("editor"))

    # ------------------------------------------------------------- navigation
    def _switch_page(self, page_key: str, payload: Any = None) -> None:
        page = self.pages.get(page_key)
        if page is None:
            return
        self._current_page_key = page_key
        self.page_stack.setCurrentWidget(page)
        button = self.nav_buttons.get(page_key)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self._refresh_nav_icons()
        if payload is not None:
            try:
                page.on_navigated(payload)
            except Exception:
                logger.exception("Page %s failed to handle navigation payload", page_key)
        else:
            try:
                page.refresh()
            except Exception:
                logger.exception("Page %s refresh failed", page_key)

    def _on_navigate_requested(self, page_key: str, payload: object) -> None:
        self._switch_page(page_key, payload)

    def _on_project_changed(self, project: Project | None) -> None:
        if project is None:
            self.project_label.setText("未打开项目")
            self.project_meta.setText("从项目管理开始你的故事")
            self.export_button.setEnabled(False)
        else:
            self.project_label.setText(project.title or "未命名作品")
            genre = project.genre or "未分类"
            self.project_meta.setText(
                f"{genre} · 目标 {project.target_words} 字/章 · POV：{project.pov or '未设置'}"
            )
            self.export_button.setEnabled(True)
        self._refresh_provider_badge()
        for key, page in self.pages.items():
            try:
                page.on_project_changed(project)
            except Exception:
                logger.exception("Page %s failed to react to project change", key)
        self._refresh_secret_hint()

    def _on_provider_changed(self) -> None:
        self._refresh_provider_badge()
        current = self.pages.get(self._current_page_key)
        if current is not None:
            try:
                current.refresh()
            except Exception:
                logger.exception("Current page refresh after provider change failed")

    def _refresh_provider_badge(self) -> None:
        config = self.ctx.provider_config_for_generation()
        if config is None:
            self.provider_button.setText("未配置模型")
            self.provider_button.setToolTip("前往 API 配置添加供应商")
            return
        key_ok = self.ctx.provider_manager.key_configured(config.id)
        if key_ok:
            key_text = "密钥已配置"
        else:
            try:
                from novel_ai.providers.catalog import spec_for

                spec = spec_for(config.provider_type)
            except Exception:
                spec = None
            key_text = (
                "本地服务，无需密钥"
                if spec is not None and not spec.requires_api_key
                else "未配置 API Key"
            )
        self.provider_button.setText(f"{config.name} · {config.model[:24]}")
        self.provider_button.setToolTip(
            f"{config.name}\n类型：{config.provider_type}\n模型：{config.model}\n"
            f"Base URL：{config.base_url}\n{key_text}"
        )

    def _refresh_secret_hint(self) -> None:
        try:
            store = self.ctx.provider_manager.secret_store
            backend = getattr(store, "backend", "local-file")
        except Exception:
            backend = "unknown"
        if backend == "keyring":
            self.secret_hint.setText("🔐 密钥保存在系统钥匙串")
        else:
            self.secret_hint.setText("⚠ 密钥保存在本地文件（权限 600）")

    def _refresh_nav_icons(self) -> None:
        for _group, items in NAV_GROUPS:
            for page_key, _title, icon_name in items:
                button = self.nav_buttons.get(page_key)
                if button is None:
                    continue
                color = self._accent() if button.isChecked() else self._muted()
                button.setIcon(icon(icon_name, color, 18, mode=self.ctx.settings.theme))

    # -------------------------------------------------------------------- theme
    def _toggle_theme(self) -> None:
        self.ctx.set_theme("light" if self.ctx.settings.theme == "dark" else "dark")

    def _on_theme_changed(self, mode: str) -> None:
        apply_theme(QApplication.instance(), mode)
        self._update_theme_labels()
        self._refresh_nav_icons()
        self.ctx.notify(f"已切换到{'浅色' if mode == 'light' else '深色'}主题。", "info")

    def _update_theme_labels(self) -> None:
        mode = self.ctx.settings.theme
        icon_name = "sun" if mode == "dark" else "moon"
        label = "浅色模式" if mode == "dark" else "深色模式"
        if hasattr(self, "theme_button"):
            self.theme_button.setText(f"  {label}")
            self.theme_button.setIcon(icon(icon_name, self._muted(), 16, mode=mode))
        if hasattr(self, "top_theme_button"):
            self.top_theme_button.setIcon(icon(icon_name, self._muted(), 18, mode=mode))
            self.top_theme_button.setToolTip(f"切换到{label}")

    def _accent(self) -> str:
        mode = self.ctx.settings.theme
        from novel_ai.ui.theme import current_palette

        return current_palette(mode)["accent"]

    def _muted(self) -> str:
        mode = self.ctx.settings.theme
        from novel_ai.ui.theme import current_palette

        return current_palette(mode)["text_muted"]

    # ----------------------------------------------------------------- signals
    def _show_toast(self, message: str, level: str) -> None:
        self.toast.show_message(message, level)
        if level == "error":
            self.status_label.setText(f"错误：{message[:160]}")
        elif level in ("success", "warning"):
            self.status_label.setText(message[:180])

    def _on_busy_changed(self, busy: bool, message: str) -> None:
        if busy:
            self.busy_badge.setText("生成中")
            self.busy_badge.set_variant("warning")
            self.status_label.setText(message or "AI 任务运行中…")
        else:
            self.busy_badge.setText("空闲")
            self.busy_badge.set_variant("neutral")
            if not self.status_label.text().startswith("错误"):
                self.status_label.setText("就绪")

    def _on_usage_changed(self, prompt_tokens: int, completion_tokens: int) -> None:
        self._last_usage = (prompt_tokens, completion_tokens)
        self.usage_label.setText(
            f"Tokens {prompt_tokens:,} / {completion_tokens:,}"
        )

    # ------------------------------------------------------------------ actions
    def _save_current(self) -> None:
        page = self.pages.get(self._current_page_key)
        if page is None:
            return
        if self._current_page_key == "editor":
            save = getattr(page, "save_current", None)
            if callable(save):
                try:
                    save()
                except Exception:
                    logger.exception("Could not save current chapter")
            return
        for method_name in ("save_current", "save"):
            method = getattr(page, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    logger.exception("Current page save failed")
                break

    def _generate_current(self) -> None:
        if self._current_page_key != "editor":
            self._switch_page("editor")
        page = self.pages.get("editor")
        if page is not None and hasattr(page, "generate_chapter"):
            page.generate_chapter()

    def _open_last_project(self) -> None:
        last_id = self.ctx.settings.last_project_id
        if not last_id:
            return
        try:
            project = self.ctx.open_project(last_id)
        except Exception:
            logger.exception("Could not restore last project")
            return
        if project is None:
            self.ctx.settings.last_project_id = None
            self.ctx.save_settings()

    def _export_book(self, fmt: str) -> None:
        project = self.ctx.current_project
        if project is None:
            self.ctx.notify("请先打开一个项目。", "warning")
            return
        editor = self.pages.get("editor")
        if editor is not None and hasattr(editor, "flush"):
            editor.flush()
        self.ctx.refresh_snapshot()
        snapshot = self.ctx.snapshot
        if snapshot is None:
            self.ctx.notify("项目数据不可用。", "error")
            return
        if not snapshot.chapters:
            self.ctx.notify("当前项目还没有章节，无法导出整书。", "warning")
            return
        formats = {
            "md": ("Markdown (*.md)", ".md", "整书 Markdown"),
            "txt": ("纯文本 (*.txt)", ".txt", "整书 TXT"),
            "docx": ("Word 文档 (*.docx)", ".docx", "整书 Word"),
        }
        file_filter, extension, label = formats.get(fmt, formats["md"])
        default_name = self.ctx.export_service.suggested_filename(
            project.title, extension.lstrip(".")
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"导出{label}",
            str(self.ctx.paths.exports_dir / default_name),
            file_filter,
        )
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() != extension:
            target = target.with_suffix(extension)
        try:
            if fmt == "md":
                self.ctx.export_service.export_book_markdown(snapshot, target)
            elif fmt == "txt":
                self.ctx.export_service.export_book_txt(snapshot, target)
            else:
                self.ctx.export_service.export_book_docx(snapshot, target)
        except Exception as exc:
            logger.exception("Whole book export failed")
            self.ctx.notify(f"导出失败：{exc}", "error")
            return
        self.ctx.notify(f"整书已导出到 {target.name}。", "success")

    def _open_exports_dir(self) -> None:
        self.ctx.paths.exports_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.ctx.paths.exports_dir)))

    # ---------------------------------------------------------------- lifecycle
    def _restore_window_state(self) -> None:
        encoded = self.ctx.settings.window_geometry
        if encoded:
            try:
                self.restoreGeometry(base64.b64decode(encoded.encode("ascii")))
            except Exception:
                logger.debug("Could not restore window geometry", exc_info=True)

    def _save_window_state(self) -> None:
        try:
            encoded = bytes(self.saveGeometry().toBase64()).decode("ascii")
            self.ctx.settings.window_geometry = encoded
            self.ctx.save_settings()
        except Exception:
            logger.exception("Could not save window state")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "toast"):
            self.toast.parent_resized()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        editor = self.pages.get("editor")
        if editor is not None and getattr(editor, "has_running_task", lambda: False)():
            if not ConfirmDialog.ask(
                self,
                "生成任务仍在运行",
                "关闭窗口会取消当前生成任务。取消后已有正文不会被修改。是否关闭？",
                confirm_text="取消任务并关闭",
                danger=True,
            ):
                event.ignore()
                return
            if hasattr(editor, "shutdown") and not editor.shutdown(3500):
                self.ctx.notify("取消任务尚未完成，请稍后重试关闭。", "warning")
                event.ignore()
                return
        # Never destroy a page while its background network task is running.
        for key, page in self.pages.items():
            if page is editor:
                continue
            if getattr(page, "has_running_task", lambda: False)():
                self.ctx.notify(
                    "有连接测试等后台任务正在执行，请等待完成后再关闭。", "warning"
                )
                event.ignore()
                return
        if editor is not None and hasattr(editor, "flush"):
            try:
                editor.flush()
            except Exception:
                logger.exception("Final editor flush failed")
        for key, page in self.pages.items():
            if page is editor:
                continue
            shutdown = getattr(page, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown(1200)
                except Exception:
                    logger.exception("Page %s shutdown failed", key)
        self._save_window_state()
        event.accept()
