"""Chapter editor: chapter list, streaming generation, context and versions."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from novel_ai.domain.models import Chapter, Project, count_words
from novel_ai.services.generation_service import (
    GenerationOptions,
    GenerationOutcome,
    GenerationUpdate,
)
from novel_ai.ui.dialogs.context_preview_dialog import (
    ContextPreviewDialog,
    VersionHistoryDialog,
)
from novel_ai.ui.icons import icon
from novel_ai.ui.pages.base import BasePage
from novel_ai.ui.widgets import (
    Badge,
    Card,
    ConfirmDialog,
    EmptyState,
    MutedLabel,
    PageHeader,
    SectionCard,
    SectionTitle,
    divider,
    set_button_variant,
)
from novel_ai.ui.workers import GenerationWorker, RewriteWorker

logger = logging.getLogger(__name__)


class EditorPage(BasePage):
    page_key = "editor"
    page_title = "章节创作"
    page_subtitle = "为当前章节组装分层上下文，一次生成一章完整正文。"

    def __init__(self, ctx, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self._loading = False
        self._dirty = False
        self._current_chapter: Chapter | None = None
        self._chapters: list[Chapter] = []
        self._generation_thread: QThread | None = None
        self._generation_worker: GenerationWorker | None = None
        self._generation_active = False
        self._generation_project_id: str | None = None
        self._generation_chapter_id: str | None = None
        self._pre_generation_content = ""
        self._pre_generation_title = ""
        self._plan = None
        self._rewrite_thread: QThread | None = None
        self._rewrite_worker: RewriteWorker | None = None
        self._rewrite_active = False
        self._rewrite_discard = False
        self._rewrite_project_id: str | None = None
        self._rewrite_chapter_id: str | None = None
        self._rewrite_selection_start = 0
        self._rewrite_selection_end = 0
        self._build_ui()
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._sync_autosave_interval()
        self._autosave_timer.timeout.connect(self._autosave)
        self.on_project_changed(ctx.current_project)

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(12)

        self.header = PageHeader(
            "章节创作",
            "一次只生成一章。上下文包含全局/卷/章摘要、角色状态、关键记忆、大纲、最近原文与检索片段。",
        )
        root.addWidget(self.header)
        self.context_preview_button = self._button(
            "上下文预览", "memory", self._preview_context
        )
        self.version_button = self._button("版本对比", "history", self._open_versions)
        self.export_button = self._button("导出本章", "download", self._export_chapter)
        self.info_toggle = self._button("信息面板", "info", self._toggle_info, variant="ghost")
        self.info_toggle.setCheckable(True)
        self.info_toggle.setChecked(True)
        for button in (
            self.context_preview_button,
            self.version_button,
            self.export_button,
            self.info_toggle,
        ):
            self.header.add_action(button)

        self.root_stack = QStackedWidget()
        root.addWidget(self.root_stack, 1)
        self.empty_page = EmptyState(
            "还没有打开项目",
            "先在项目管理中创建或打开一部小说，再开始章节创作。",
            action_text="前往项目管理",
            icon_name="book",
        )
        self.empty_page.actionClicked.connect(lambda: self.ctx.navigate("dashboard"))
        self.root_stack.addWidget(self.empty_page)
        self.workspace = self._build_workspace()
        self.root_stack.addWidget(self.workspace)
        self.root_stack.setCurrentWidget(self.empty_page)

    def _build_workspace(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_chapter_panel())
        self.splitter.addWidget(self._build_editor_panel())
        self.info_panel = self._build_info_panel()
        self.splitter.addWidget(self.info_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([252, 760, 320])
        layout.addWidget(self.splitter)
        return page

    def _build_chapter_panel(self) -> QWidget:
        card = Card(padding=14, spacing=10)
        card.setMinimumWidth(220)
        card.setMaximumWidth(330)
        header = QHBoxLayout()
        title = SectionTitle("章节")
        header.addWidget(title)
        header.addStretch(1)
        add_button = QPushButton()
        add_button.setObjectName("IconButton")
        add_button.setIcon(icon("plus", mode=self._theme_mode))
        add_button.setFixedSize(28, 28)
        add_button.setToolTip("新建章节")
        add_button.clicked.connect(self._create_chapter)
        header.addWidget(add_button)
        card.content_layout.addLayout(header)

        chapter_hint = QLabel("选择章节进行编辑，生成时不会跨章输出。")
        chapter_hint.setProperty("role", "hint")
        chapter_hint.setWordWrap(True)
        card.content_layout.addWidget(chapter_hint)

        self.chapter_list = QListWidget()
        self.chapter_list.setObjectName("ChapterList")
        self.chapter_list.currentItemChanged.connect(self._on_chapter_selected)
        self.chapter_list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        card.content_layout.addWidget(self.chapter_list, 1)

        footer = QHBoxLayout()
        self.chapter_count_label = QLabel("0 章")
        self.chapter_count_label.setProperty("role", "hint")
        footer.addWidget(self.chapter_count_label)
        footer.addStretch(1)
        self.delete_chapter_button = self._button(
            "", "trash", self._delete_chapter, variant="ghost", tooltip="删除当前章节"
        )
        self.delete_chapter_button.setFixedSize(30, 30)
        footer.addWidget(self.delete_chapter_button)
        card.content_layout.addLayout(footer)
        return card

    def _build_editor_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        meta_card = Card(padding=16, spacing=10)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.chapter_number_badge = Badge("第 1 章", "accent")
        self.status_badge = Badge("草稿", "neutral")
        self.word_count_label = QLabel("0 字")
        self.word_count_label.setProperty("role", "muted")
        top.addWidget(self.chapter_number_badge)
        top.addWidget(self.status_badge)
        top.addStretch(1)
        top.addWidget(self.word_count_label)
        layout.addLayout(top)

        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("章节标题")
        font = QFont()
        font.setPointSize(15)
        font.setBold(True)
        self.title_edit.setFont(font)
        self.title_edit.setMinimumHeight(38)
        self.title_edit.textChanged.connect(self._on_field_changed)
        layout.addWidget(self.title_edit)

        target_row = QHBoxLayout()
        target_label = QLabel("目标字数")
        target_label.setProperty("role", "muted")
        self.target_spin = QSpinBox()
        self.target_spin.setRange(300, 20000)
        self.target_spin.setSingleStep(100)
        self.target_spin.setValue(2500)
        self.target_spin.setMaximumWidth(120)
        self.target_spin.valueChanged.connect(self._on_field_changed)
        target_row.addWidget(target_label)
        target_row.addWidget(self.target_spin)
        target_row.addSpacing(12)
        requirement_label = QLabel("本章写作要求（可选）")
        requirement_label.setProperty("role", "muted")
        target_row.addWidget(requirement_label)
        target_row.addStretch(1)
        layout.addLayout(target_row)

        self.requirements_edit = QPlainTextEdit()
        self.requirements_edit.setPlaceholderText(
            "例如：雨夜追逐开场，保持双线叙事；让林默第一次怀疑师父；结尾留出反转钩子。"
        )
        self.requirements_edit.setMaximumHeight(62)
        self.requirements_edit.textChanged.connect(self._on_field_changed)
        layout.addWidget(self.requirements_edit)
        layout.addWidget(meta_card)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.save_button = self._button("保存", "save", self._save_manual)
        self.generate_button = self._button(
            "生成本章", "sparkles", self._generate, variant="primary"
        )
        self.generate_button.setMinimumHeight(36)
        self.cancel_button = self._button("取消生成", "stop", self._cancel_generation, variant="danger")
        self.cancel_button.setVisible(False)
        self.rewrite_button = self._button("局部重写", "editor", self._rewrite_selection)
        self.rewrite_button.setToolTip("选中正文中的一段文字后，用 AI 按指令局部润色")
        toolbar.addWidget(self.save_button)
        toolbar.addWidget(self.generate_button)
        toolbar.addWidget(self.cancel_button)
        toolbar.addWidget(self.rewrite_button)
        toolbar.addStretch(1)
        self.provider_hint = QLabel("未配置供应商")
        self.provider_hint.setProperty("role", "hint")
        toolbar.addWidget(self.provider_hint)
        layout.addLayout(toolbar)

        self.error_banner = QFrame()
        self.error_banner.setObjectName("SoftCard")
        error_layout = QHBoxLayout(self.error_banner)
        error_layout.setContentsMargins(12, 8, 12, 8)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #FF8296;")
        error_layout.addWidget(self.error_label, 1)
        self.retry_button = QPushButton("重试生成")
        self.retry_button.setProperty("variant", "danger")
        self.retry_button.clicked.connect(self._retry_generation)
        error_layout.addWidget(self.retry_button)
        hide_error = QPushButton("关闭")
        hide_error.setProperty("variant", "ghost")
        hide_error.clicked.connect(lambda: self.error_banner.hide())
        error_layout.addWidget(hide_error)
        self.error_banner.setVisible(False)
        layout.addWidget(self.error_banner)

        self.editor_stack = QStackedWidget()
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("ChapterEditor")
        self.editor.setPlaceholderText(
            "在此撰写正文，或点击“生成本章”让 AI 根据压缩后的上下文生成完整一章。"
        )
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.selectionChanged.connect(self._update_rewrite_enabled)
        self.editor.setTabChangesFocus(False)
        self.editor_stack.addWidget(self.editor)
        no_chapter = QWidget()
        no_layout = QVBoxLayout(no_chapter)
        no_layout.addStretch(1)
        self.no_chapter_state = EmptyState(
            "还没有章节",
            "新建一章后，即可编辑大纲要求并生成正文。",
            action_text="新建第一章",
            icon_name="editor",
        )
        self.no_chapter_state.actionClicked.connect(self._create_chapter)
        no_layout.addWidget(self.no_chapter_state)
        no_layout.addStretch(1)
        self.editor_stack.addWidget(no_chapter)
        layout.addWidget(self.editor_stack, 1)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setMaximumWidth(180)
        self.progress.setVisible(False)
        self.stream_status = QLabel("就绪")
        self.stream_status.setProperty("role", "hint")
        self.token_status = QLabel("")
        self.token_status.setProperty("role", "hint")
        status_row.addWidget(self.progress)
        status_row.addWidget(self.stream_status, 1)
        status_row.addWidget(self.token_status)
        layout.addLayout(status_row)
        return panel

    def _build_info_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(280)
        scroll.setMaximumWidth(390)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        provider_card = Card(padding=16, spacing=8)
        provider_header = QHBoxLayout()
        provider_header.addWidget(SectionTitle("生成模型"))
        provider_header.addStretch(1)
        self.model_badge = Badge("未配置", "warning")
        provider_header.addWidget(self.model_badge)
        provider_card.content_layout.addLayout(provider_header)
        self.provider_name_label = QLabel("—")
        self.provider_name_label.setProperty("role", "muted")
        self.provider_name_label.setWordWrap(True)
        provider_card.content_layout.addWidget(self.provider_name_label)
        provider_card.content_layout.addWidget(divider())
        self.chapter_status_label = QLabel("等待选择章节")
        self.chapter_status_label.setProperty("role", "hint")
        self.chapter_status_label.setWordWrap(True)
        provider_card.content_layout.addWidget(self.chapter_status_label)
        layout.addWidget(provider_card)

        budget_card = Card(padding=16, spacing=8)
        budget_header = QHBoxLayout()
        budget_header.addWidget(SectionTitle("Token 预算"))
        budget_header.addStretch(1)
        self.usage_badge = Badge("0%", "neutral")
        budget_header.addWidget(self.usage_badge)
        budget_card.content_layout.addLayout(budget_header)
        self.budget_bar = QProgressBar()
        self.budget_bar.setRange(0, 100)
        self.budget_bar.setValue(0)
        budget_card.content_layout.addWidget(self.budget_bar)
        self.budget_detail = QLabel("上下文计划尚未构建")
        self.budget_detail.setProperty("role", "hint")
        self.budget_detail.setWordWrap(True)
        budget_card.content_layout.addWidget(self.budget_detail)
        self.usages_label = QLabel("输入 — · 输出 —")
        self.usages_label.setProperty("role", "muted")
        budget_card.content_layout.addWidget(self.usages_label)
        preview_button = QPushButton("查看完整上下文预览")
        preview_button.setProperty("variant", "ghost")
        preview_button.clicked.connect(self._preview_context)
        budget_card.content_layout.addWidget(preview_button)
        layout.addWidget(budget_card)

        context_card = Card(padding=16, spacing=8)
        context_card.content_layout.addWidget(SectionTitle("记忆快照"))
        self.global_summary_preview = QLabel("暂无全局摘要")
        self.global_summary_preview.setWordWrap(True)
        self.global_summary_preview.setProperty("role", "muted")
        self.global_summary_preview.setMaximumHeight(140)
        context_card.content_layout.addWidget(self.global_summary_preview)
        capture_btn = QPushButton("打开上下文与记忆")
        capture_btn.setProperty("variant", "ghost")
        capture_btn.clicked.connect(lambda: self.ctx.navigate("memory"))
        context_card.content_layout.addWidget(capture_btn)
        layout.addWidget(context_card)

        tips = Card(padding=16, spacing=6)
        tips.content_layout.addWidget(SectionTitle("创作提示"))
        tip = QLabel(
            "每次生成只产出一章。若想调整方向，请优先修改本章大纲与写作要求，"
            "再重新生成；历史版本可在“版本对比”中回滚。"
        )
        tip.setProperty("role", "hint")
        tip.setWordWrap(True)
        tips.content_layout.addWidget(tip)
        layout.addWidget(tips)
        layout.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    # --------------------------------------------------------------- helpers
    @property
    def _theme_mode(self) -> str:
        return self.ctx.settings.theme if self.ctx else "dark"

    def _button(
        self,
        text: str,
        icon_name: str,
        callback,
        *,
        variant: str = "secondary",
        tooltip: str = "",
    ) -> QPushButton:
        button = QPushButton(text)
        if icon_name:
            button.setIcon(icon(icon_name, mode=self._theme_mode))
        button.setProperty("variant", variant)
        button.clicked.connect(callback)
        if tooltip:
            button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _sync_autosave_interval(self) -> None:
        seconds = max(10, int(self.ctx.settings.auto_save_seconds or 30))
        self._autosave_timer.setInterval(seconds * 1000)

    # ---------------------------------------------------------------- project
    def on_project_changed(self, project: Project | None) -> None:
        self._sync_autosave_interval()
        # A project switch while a background task is running must never let the
        # old project's output leak into the new project's editor.
        if self._rewrite_active:
            self._rewrite_discard = True
            self.ctx.notify("项目已切换，局部重写结果将被丢弃。", "warning")
        elif (
            self._generation_active
            and self._generation_project_id
            and (project is None or project.id != self._generation_project_id)
        ):
            self._cancel_generation()
            self.ctx.notify("项目已切换，当前生成任务正在取消。", "warning")
        if project is None:
            self.root_stack.setCurrentWidget(self.empty_page)
            self._current_chapter = None
            self._chapters = []
            self.chapter_list.clear()
            return
        # Saving project settings or refreshing provider info must not tear down
        # the live streaming buffer while the same project is still generating.
        if (
            self._generation_active
            and self._generation_project_id == project.id
        ):
            self.root_stack.setCurrentWidget(self.workspace)
            self._refresh_provider_info()
            return
        self.root_stack.setCurrentWidget(self.workspace)
        self._hide_error()
        self._refresh_provider_info()
        self._reload_chapters()
        self._refresh_memory_panel()

    def on_navigated(self, payload: object = None) -> None:
        if not isinstance(payload, dict):
            return
        chapter_id = payload.get("chapter_id")
        if not chapter_id:
            return
        if self._generation_active:
            self.ctx.notify("生成进行中，完成后再切换章节。", "warning")
            return
        self.on_project_changed(self.ctx.current_project)
        self._select_chapter_by_id(str(chapter_id))

    def refresh(self) -> None:
        if self.ctx.current_project is None:
            return
        self._refresh_provider_info()
        if self._generation_active:
            return
        self._reload_chapters(keep_selection=True)
        self._refresh_memory_panel()

    # --------------------------------------------------------------- chapters
    def _reload_chapters(self, *, keep_selection: bool = False) -> None:
        project = self.ctx.current_project
        if project is None:
            return
        selected_id = self._current_chapter.id if (keep_selection and self._current_chapter) else None
        try:
            self._chapters = self.ctx.project_service.list_chapters(project.id)
        except Exception as exc:
            logger.exception("Could not load chapters")
            self.ctx.notify(f"加载章节失败：{exc}", "error")
            return
        self._loading = True
        self.chapter_list.clear()
        for chapter in self._chapters:
            item = QListWidgetItem(self._chapter_item_text(chapter))
            item.setData(Qt.ItemDataRole.UserRole, chapter.id)
            self.chapter_list.addItem(item)
        self.chapter_count_label.setText(f"{len(self._chapters)} 章")
        self._loading = False
        if self._chapters:
            target_id = selected_id or self._chapters[0].id
            self._select_chapter_by_id(target_id)
        else:
            self._current_chapter = None
            self._set_editor_enabled(False)
            self.editor_stack.setCurrentIndex(1)
            self.chapter_number_badge.setText("尚未创建章节")

    def _select_chapter_by_id(self, chapter_id: str) -> None:
        for row, chapter in enumerate(self._chapters):
            if chapter.id == chapter_id:
                self.chapter_list.setCurrentRow(row)
                return

    def _chapter_item_text(self, chapter: Chapter) -> str:
        title = chapter.title or "未命名"
        status = "定稿" if chapter.status == "final" else "草稿"
        return f"第 {chapter.number} 章 · {title}\n{chapter.word_count} 字 · {status}"

    def _on_chapter_selected(self, current: QListWidgetItem | None, previous) -> None:  # noqa: ANN001
        if self._loading or current is None or self._generation_active:
            return
        chapter_id = current.data(Qt.ItemDataRole.UserRole)
        chapter = next((c for c in self._chapters if c.id == chapter_id), None)
        if chapter is None:
            return
        if self._current_chapter is not None and self._dirty and self._current_chapter.id != chapter.id:
            if not self._save_draft(silent=True):
                # Keep the user's unsaved text visible instead of silently
                # discarding it when persistence fails.
                self._loading = True
                if previous is not None:
                    self.chapter_list.setCurrentItem(previous)
                self._loading = False
                return
        self._load_chapter(chapter)

    def _load_chapter(self, chapter: Chapter) -> None:
        self._loading = True
        self._current_chapter = chapter
        self.title_edit.setText(chapter.title)
        self.target_spin.setValue(chapter.target_words or 2500)
        self.requirements_edit.setPlainText(chapter.requirements or "")
        self.editor.setPlainText(chapter.content or "")
        self.chapter_number_badge.setText(f"第 {chapter.number} 章")
        self._set_status_badge(chapter.status)
        self._update_word_count()
        self._loading = False
        self._dirty = False
        self._set_editor_enabled(True)
        self.editor_stack.setCurrentIndex(0)
        self._hide_error()
        self.chapter_status_label.setText(
            f"第 {chapter.number} 章 · {chapter.title or '未命名'}｜"
            f"{chapter.word_count} 字 · {'定稿' if chapter.status == 'final' else '草稿'}"
        )
        self._refresh_memory_panel()

    def _set_editor_enabled(self, enabled: bool) -> None:
        for widget in (
            self.editor,
            self.title_edit,
            self.target_spin,
            self.requirements_edit,
        ):
            widget.setEnabled(enabled)
        self.save_button.setEnabled(enabled)
        self.generate_button.setEnabled(enabled)
        self.delete_chapter_button.setEnabled(enabled)
        self.rewrite_button.setEnabled(enabled and self._has_selection())
        for button in (self.context_preview_button, self.version_button, self.export_button):
            button.setEnabled(enabled)

    def _set_status_badge(self, status: str) -> None:
        if status == "final":
            self.status_badge.setText("定稿")
            self.status_badge.set_variant("success")
        else:
            self.status_badge.setText("草稿")
            self.status_badge.set_variant("neutral")

    # ----------------------------------------------------------------- fields
    def _on_field_changed(self, *_: object) -> None:
        if self._loading or self._current_chapter is None or self._generation_active:
            return
        self._dirty = True
        if self._autosave_timer.isActive():
            self._autosave_timer.stop()
        self._autosave_timer.start()

    def _on_text_changed(self) -> None:
        if self._loading:
            return
        self._update_word_count()
        self._on_field_changed()

    def _update_word_count(self) -> None:
        words = count_words(self.editor.toPlainText())
        self.word_count_label.setText(f"{words} 字")
        target = max(1, self.target_spin.value())
        ratio = words / target
        color = "#45D5A5" if ratio >= 0.9 else ("#F1C46B" if words else "")
        self.word_count_label.setStyleSheet(f"color: {color};" if color else "")

    def _autosave(self) -> None:
        if self._dirty and self._current_chapter is not None and not self._generation_active:
            self._save_draft(silent=True)

    def _save_draft(self, *, silent: bool = False, create_version: bool = False) -> bool:
        chapter = self._current_chapter
        if chapter is None or self._generation_active:
            return False
        try:
            chapter.title = self.title_edit.text().strip() or f"第{chapter.number}章"
            chapter.content = self.editor.toPlainText()
            chapter.target_words = self.target_spin.value()
            chapter.requirements = self.requirements_edit.toPlainText().strip()
            saved = self.ctx.project_service.save_chapter(
                chapter,
                note="手动保存" if create_version else "自动保存",
                create_version=create_version,
            )
        except Exception as exc:
            logger.exception("Could not save chapter")
            self.ctx.notify(f"保存章节失败：{exc}", "error")
            return False
        self._current_chapter = saved
        self._dirty = False
        self._autosave_timer.stop()
        self._set_status_badge(saved.status)
        self._update_list_item(saved)
        if not silent:
            self.ctx.notify("章节已保存。", "success")
        return True

    def _save_manual(self) -> None:
        if self._save_draft(silent=False, create_version=True):
            self.refresh()

    # Public shortcuts for the main window.
    def save_current(self) -> None:
        self._save_manual()

    def generate_chapter(self) -> None:
        self._generate()

    def _update_list_item(self, chapter: Chapter) -> None:
        for row in range(self.chapter_list.count()):
            item = self.chapter_list.item(row)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == chapter.id:
                item.setText(self._chapter_item_text(chapter))
                self._chapters = [
                    chapter if c.id == chapter.id else c for c in self._chapters
                ]
                break
        self.chapter_status_label.setText(
            f"第 {chapter.number} 章 · {chapter.title or '未命名'}｜"
            f"{chapter.word_count} 字 · {'定稿' if chapter.status == 'final' else '草稿'}"
        )

    # --------------------------------------------------------------- chapters
    def _create_chapter(self) -> None:
        project = self.ctx.current_project
        if project is None:
            self.ctx.navigate("dashboard")
            return
        title, ok = QInputDialog.getText(
            self,
            "新建章节",
            "章节标题（可稍后修改）：",
            text=f"第{len(self._chapters) + 1}章标题",
        )
        if not ok:
            return
        if self._dirty:
            self._save_draft(silent=True)
        try:
            chapter = self.ctx.project_service.create_chapter(
                project.id,
                title.strip() or "未命名章节",
                target_words=self.target_spin.value(),
            )
        except Exception as exc:
            logger.exception("Could not create chapter")
            self.ctx.notify(f"新建章节失败：{exc}", "error")
            return
        self._reload_chapters(keep_selection=False)
        self._select_chapter_by_id(chapter.id)
        self.ctx.notify(f"已创建第 {chapter.number} 章。", "success")

    def _delete_chapter(self) -> None:
        chapter = self._current_chapter
        if chapter is None:
            return
        if not ConfirmDialog.ask(
            self,
            "删除章节",
            f"确定删除“第{chapter.number}章 · {chapter.title}”及其全部版本？该操作不可撤销。",
            confirm_text="删除",
            danger=True,
        ):
            return
        try:
            self.ctx.project_service.delete_chapter(chapter.id)
        except Exception as exc:
            logger.exception("Could not delete chapter")
            self.ctx.notify(f"删除章节失败：{exc}", "error")
            return
        self._current_chapter = None
        self._reload_chapters()
        self.ctx.notify("章节已删除。", "info")

    # --------------------------------------------------------------- generate
    def _generate(self) -> None:
        if self._generation_active:
            return
        project = self.ctx.current_project
        chapter = self._current_chapter
        if project is None or chapter is None:
            self.ctx.notify("请先创建并选择一个章节。", "warning")
            return
        config = self.ctx.provider_config_for_generation()
        if config is None:
            if ConfirmDialog.ask(
                self,
                "尚未配置 AI 供应商",
                "生成前需要配置一个 AI 供应商。现在前往 API 配置页面？",
                confirm_text="去配置",
            ):
                self.ctx.navigate("api")
            return
        # Persist the latest title/requirements/body before building context.
        if not self._save_draft(silent=True):
            return
        if self.editor.toPlainText().strip() and self.ctx.settings.confirm_before_overwrite:
            if not ConfirmDialog.ask(
                self,
                "重新生成本章",
                "本次生成会覆盖当前草稿（旧草稿会先自动保存为版本快照）。是否继续？",
                confirm_text="继续生成",
            ):
                return
        self._pre_generation_content = self.editor.toPlainText()
        self._pre_generation_title = self.title_edit.text()
        options = GenerationOptions(
            target_words=self.target_spin.value(),
            requirements=self.requirements_edit.toPlainText().strip(),
            use_retrieval=self.ctx.settings.retrieval_enabled,
            auto_update_memory=self.ctx.settings.auto_update_memory,
        )
        self._start_generation(project, chapter, options)

    def _start_generation(
        self, project: Project, chapter: Chapter, options: GenerationOptions
    ) -> None:
        self._generation_active = True
        self._generation_project_id = project.id
        self._generation_chapter_id = chapter.id
        self._set_editor_enabled(False)
        self.generate_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("取消生成")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.stream_status.setText("正在准备上下文…")
        self.token_status.setText("")
        self._hide_error()
        self.editor.setReadOnly(True)
        self._loading = True
        self.editor.setPlainText("")
        self._loading = False
        self.ctx.set_busy(True, "AI 正在生成章节…")
        self.chapter_list.setEnabled(False)

        thread = QThread(self)
        worker = GenerationWorker(
            self.ctx.generation_service, project.id, chapter.id, options
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.update.connect(self._on_generation_update)
        worker.finished.connect(self._on_generation_finished)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_generation_thread_finished)
        self._generation_thread = thread
        self._generation_worker = worker
        thread.start()

    def _on_generation_update(self, update: GenerationUpdate) -> None:
        # Ignore stale chunks if the user switched projects/chapters or the page
        # already cancelled this task.
        if not self._generation_active:
            return
        if self._generation_project_id and (
            self.ctx.current_project is None
            or self.ctx.current_project.id != self._generation_project_id
        ):
            return
        if self._generation_chapter_id and (
            self._current_chapter is None
            or self._current_chapter.id != self._generation_chapter_id
        ):
            return
        if update.kind == "stage":
            self.stream_status.setText(update.message)
            self.progress.setRange(0, 0)
            # Once the chapter is saved, cancellation can no longer change the
            # persisted draft; disable it to avoid a misleading action.
            if any(
                marker in update.message
                for marker in ("正文完成", "正在保存草稿", "正在更新摘要")
            ):
                self.cancel_button.setEnabled(False)
                self.cancel_button.setText("正在收尾…")
        elif update.kind == "plan" and update.plan is not None:
            self._plan = update.plan
            self._apply_plan_to_panel(update.plan)
            self.stream_status.setText(
                f"上下文就绪：{update.plan.input_tokens:,} tokens / 预算 {update.plan.input_budget:,}"
            )
            self.progress.setRange(0, 0)
        elif update.kind == "text" and update.text:
            self.editor.moveCursor(QTextCursor.MoveOperation.End)
            self.editor.insertPlainText(update.text)
            self._update_stream_status()
        elif update.kind == "usage" and update.usage is not None:
            self.ctx.set_usage(
                update.usage.prompt_tokens, update.usage.completion_tokens
            )
            self.token_status.setText(
                f"输入 {update.usage.prompt_tokens:,} · 输出 {update.usage.completion_tokens:,} tokens"
            )
        elif update.kind == "memory":
            self.stream_status.setText(update.message or "记忆已更新")
        elif update.kind == "error":
            self.stream_status.setText("生成失败")
            if update.plan is not None:
                self._apply_plan_to_panel(update.plan)
        elif update.kind == "cancelled":
            self.stream_status.setText("生成已取消")

    def _update_stream_status(self) -> None:
        text = self.editor.toPlainText()
        words = count_words(text)
        target = max(1, self.target_spin.value())
        self.word_count_label.setText(f"{words} 字")
        self.stream_status.setText(f"正在流式生成… {words} / {target} 字")
        if words:
            estimated_tokens = max(1, int(len(text) / 1.6))
            self.token_status.setText(f"约 {estimated_tokens:,} tokens 输出中")

    def _on_generation_finished(self, outcome: GenerationOutcome) -> None:
        same_context = (
            self.ctx.current_project is not None
            and self.ctx.current_project.id == self._generation_project_id
            and self._current_chapter is not None
            and self._current_chapter.id == self._generation_chapter_id
        )
        self._generation_active = False
        self.editor.setReadOnly(False)
        self.cancel_button.setText("取消生成")
        self.cancel_button.setEnabled(True)
        self.cancel_button.setVisible(False)
        self.progress.setVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.ctx.set_busy(False)
        self.chapter_list.setEnabled(True)
        self._set_editor_enabled(True)
        self.generate_button.setEnabled(True)
        if not same_context:
            self._generation_worker = None
            return
        if outcome.ok and outcome.chapter is not None:
            self._current_chapter = outcome.chapter
            self._loading = True
            self.editor.setPlainText(outcome.chapter.content)
            self.title_edit.setText(outcome.chapter.title)
            self._loading = False
            self._dirty = False
            self._set_status_badge(outcome.chapter.status)
            self._update_word_count()
            if outcome.plan is not None:
                self._plan = outcome.plan
                self._apply_plan_to_panel(outcome.plan)
            self.stream_status.setText(
                f"本章完成 · {outcome.chapter.word_count} 字"
                + (" · 记忆已更新" if outcome.memory_updated else "")
            )
            self.ctx.refresh_snapshot()
            self._reload_chapters(keep_selection=True)
            self._refresh_memory_panel()
            message = "本章已生成并保存为草稿。"
            if outcome.warning:
                self.ctx.notify(f"{message} {outcome.warning}", "warning")
            else:
                self.ctx.notify(message, "success")
        elif outcome.cancelled:
            self._restore_pre_generation()
            self.stream_status.setText("已取消，原正文保持不变。")
            self.ctx.notify("生成已取消，原有正文未被修改。", "info")
        else:
            self._restore_pre_generation()
            self.stream_status.setText("生成失败")
            self._show_error(outcome.error or "生成失败，请重试。")
            self.ctx.notify(outcome.error or "生成失败，请重试。", "error")
        self._generation_worker = None

    def _restore_pre_generation(self) -> None:
        self._loading = True
        self.editor.setPlainText(self._pre_generation_content)
        self.title_edit.setText(self._pre_generation_title)
        self._loading = False
        self._dirty = False
        self._update_word_count()

    def _on_generation_thread_finished(self) -> None:
        self._generation_thread = None
        if self._generation_worker is not None:
            self._generation_worker = None

    def _cancel_generation(self) -> None:
        if self._generation_worker is not None:
            self._generation_worker.cancel()
            self.cancel_button.setEnabled(False)
            self.stream_status.setText("正在取消，请稍候…")
            self.ctx.set_busy(True, "正在取消生成…")

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_banner.setVisible(True)

    def _hide_error(self) -> None:
        self.error_banner.setVisible(False)

    def _retry_generation(self) -> None:
        self._hide_error()
        self._generate()

    def _repair_stream_controls(self) -> None:
        self.editor.setReadOnly(False)
        self.cancel_button.setVisible(False)
        self.generate_button.setEnabled(True)

    # ------------------------------------------------------------------ info
    def _refresh_provider_info(self) -> None:
        config = self.ctx.provider_config_for_generation()
        if config is None:
            self.provider_hint.setText("未配置供应商")
            self.provider_name_label.setText("请先在 API 配置中添加并设默认供应商。")
            self.model_badge.setText("未配置")
            self.model_badge.set_variant("warning")
            return
        key_configured = self.ctx.provider_manager.key_configured(config.id)
        if key_configured:
            key_state = "密钥已配置"
        else:
            try:
                from novel_ai.providers.catalog import spec_for

                spec = spec_for(config.provider_type)
            except Exception:
                spec = None
            if spec is not None and not spec.requires_api_key:
                key_state = "本地服务，无需密钥"
            else:
                key_state = "⚠ 未配置 API Key"
        self.provider_hint.setText(f"{config.name} · {config.model}")
        self.provider_name_label.setText(
            f"{config.name} · {config.provider_type}\n{config.model} · 上下文窗口 {config.context_window:,}\n{key_state}"
        )
        self.model_badge.setText(config.model[:24])
        self.model_badge.set_variant("accent")
        self.config_hint = config

    def _apply_plan_to_panel(self, plan) -> None:  # noqa: ANN001
        self.plan = plan
        self.budget_bar.setValue(int(plan.usage_ratio * 100))
        self.usage_badge.setText(f"{plan.usage_ratio * 100:.0f}%")
        self.usage_badge.set_variant(
            "danger" if plan.usage_ratio > 0.95 else ("warning" if plan.usage_ratio > 0.8 else "success")
        )
        self.budget_detail.setText(
            f"预计输入 {plan.input_tokens:,} / {plan.input_budget:,} tokens\n"
            f"上下文窗口 {plan.context_window:,} · 预留输出 {plan.reserved_output:,}"
        )
        truncated = len(plan.truncated_sections)
        self.chapter_status_label.setText(
            f"分节 {len(plan.included_sections)} 个 · 检索命中 {len(plan.retrieval_hits)} 个"
            + (f" · {truncated} 个分节被压缩" if truncated else "")
        )
        self._refresh_memory_panel()

    def _toggle_info(self) -> None:
        visible = self.info_toggle.isChecked()
        self.info_panel.setVisible(visible)
        self.info_toggle.setText("隐藏信息" if visible else "信息面板")

    def _refresh_memory_panel(self) -> None:
        if self.ctx.snapshot is None:
            self.global_summary_preview.setText("暂无全局摘要")
            return
        chapter = self._current_chapter
        summary = (
            self.ctx.snapshot.global_summary()
            or (self.ctx.snapshot.chapter_summary(chapter.id) if chapter else "")
            or "尚无全局摘要，生成章节后会自动更新。"
        )
        self.global_summary_preview.setText(_short(summary, 360))
        if chapter is not None:
            self.chapter_status_label.setText(
                f"第 {chapter.number} 章 · {chapter.title or '未命名'}｜"
                f"{chapter.word_count} 字 · {'定稿' if chapter.status == 'final' else '草稿'}"
            )
            active = len(self.ctx.snapshot.active_characters())
            memories = len(self.ctx.snapshot.active_memories())
            if self._plan is None:
                provider = self.ctx.provider_config_for_generation()
                hint = provider.model if provider else "未配置模型"
                self.budget_detail.setText(
                    f"上下文计划尚未构建\n当前模型：{hint}\n角色 {active} · 记忆锚点 {memories}"
                )

    # -------------------------------------------------------------- rewriting
    def _has_selection(self) -> bool:
        try:
            cursor = self.editor.textCursor()
            return bool(cursor and cursor.hasSelection())
        except Exception:
            return False

    def _update_rewrite_enabled(self) -> None:
        if self._generation_active:
            self.rewrite_button.setEnabled(False)
            return
        self.rewrite_button.setEnabled(self._current_chapter is not None and self._has_selection())

    def _rewrite_selection(self) -> None:
        if self._generation_active or self._current_chapter is None:
            return
        cursor = self.editor.textCursor()
        selection = cursor.selectedText().replace("\u2029", "\n")
        if not selection.strip():
            self.ctx.notify("请先选中需要重写的正文段落。", "warning")
            return
        if self.ctx.provider_config_for_generation() is None:
            self.ctx.notify("请先在 API 配置中添加 AI 供应商。", "error")
            return
        instruction, ok = QInputDialog.getMultiLineText(
            self,
            "局部重写",
            "改写要求（例如：加强压迫感，缩短句子，保持人物语气一致）：",
            "保持情节与信息不变，改善画面感、节奏与语言质感，字数与原文接近。",
        )
        if not ok:
            return
        if not self._save_draft(silent=True):
            return
        self._rewrite_selection_start = min(cursor.selectionStart(), cursor.selectionEnd())
        self._rewrite_selection_end = max(cursor.selectionStart(), cursor.selectionEnd())
        self._rewrite_project_id = self.ctx.current_project.id  # type: ignore[union-attr]
        self._rewrite_chapter_id = self._current_chapter.id
        self._rewrite_active = True
        self._rewrite_discard = False
        self._generation_active = True
        self.editor.setReadOnly(True)
        self.rewrite_button.setEnabled(False)
        self.generate_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.stream_status.setText("正在局部重写，请稍候…")
        self.ctx.set_busy(True, "AI 正在重写选段…")
        thread = QThread(self)
        worker = RewriteWorker(
            self.ctx.generation_service,
            self.ctx.current_project.id,  # type: ignore[union-attr]
            self._current_chapter.id,
            selection,
            instruction,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_rewrite_succeeded)
        worker.failed.connect(self._on_rewrite_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(self._finish_rewrite_ui)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_rewrite_thread_finished)
        self._rewrite_thread = thread
        self._rewrite_worker = worker
        thread.start()

    def _on_rewrite_succeeded(self, text: str) -> None:
        current_project = self.ctx.current_project
        if (
            self._rewrite_discard
            or current_project is None
            or current_project.id != self._rewrite_project_id
            or self._current_chapter is None
            or self._current_chapter.id != self._rewrite_chapter_id
        ):
            self.ctx.notify("项目或章节已切换，局部重写结果已丢弃。", "warning")
            return
        cursor = self.editor.textCursor()
        cursor.setPosition(self._rewrite_selection_start)
        cursor.setPosition(
            self._rewrite_selection_end, QTextCursor.MoveMode.KeepAnchor
        )
        cursor.insertText(text)
        self._dirty = True
        self._autosave_timer.start()
        self.ctx.notify("局部重写已应用，可按 Ctrl+S 或等待自动保存。", "success")

    def _on_rewrite_failed(self, message: str) -> None:
        if not self._rewrite_discard:
            self.ctx.notify(message, "error")

    def _finish_rewrite_ui(self) -> None:
        self._rewrite_active = False
        self._generation_active = False
        self.editor.setReadOnly(False)
        self.progress.setVisible(False)
        self.progress.setRange(0, 100)
        self.stream_status.setText("就绪")
        self.ctx.set_busy(False)
        self._set_editor_enabled(True)

    def _on_rewrite_thread_finished(self) -> None:
        self._rewrite_thread = None
        self._rewrite_worker = None
        self._rewrite_active = False

    # -------------------------------------------------------------- dialogs
    def _preview_context(self) -> None:
        if self._current_chapter is None or self.ctx.current_project is None:
            self.ctx.notify("请先选择一个章节。", "warning")
            return
        if self._generation_active:
            self.ctx.notify("生成进行中，暂时无法预览上下文。", "warning")
            return
        config = self.ctx.provider_config_for_generation()
        if config is None:
            self.ctx.notify("请先在 API 配置中添加 AI 供应商。", "warning")
            return
        if not self._save_draft(silent=True):
            return
        options = GenerationOptions(
            target_words=self.target_spin.value(),
            requirements=self.requirements_edit.toPlainText().strip(),
            use_retrieval=self.ctx.settings.retrieval_enabled,
            auto_update_memory=self.ctx.settings.auto_update_memory,
        )
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            plan = self.ctx.generation_service.build_plan(
                self.ctx.current_project.id,
                self._current_chapter.id,
                options,
            )
        except Exception as exc:
            logger.exception("Could not build context preview")
            message = getattr(exc, "user_message", None) or f"构建上下文失败：{exc}"
            self.ctx.notify(str(message), "error")
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._plan = plan
        self._apply_plan_to_panel(plan)
        dialog = ContextPreviewDialog(
            plan,
            self,
            provider_label=f"{config.name} / {config.model}",
        )
        dialog.exec()

    def _open_versions(self) -> None:
        chapter = self._current_chapter
        if chapter is None:
            self.ctx.notify("请先选择一个章节。", "warning")
            return
        if self._dirty:
            self._save_draft(silent=True)
        try:
            versions = self.ctx.project_service.list_versions(chapter.id)
        except Exception as exc:
            logger.exception("Could not load versions")
            self.ctx.notify(f"读取版本失败：{exc}", "error")
            return
        if not versions:
            self.ctx.notify("当前章节还没有历史版本，手动保存或生成后会创建版本。", "info")
            return
        dialog = VersionHistoryDialog(
            versions, chapter.title, self.editor.toPlainText(), self
        )
        if dialog.exec() and dialog.selected_version is not None:
            try:
                restored = self.ctx.project_service.restore_version(
                    chapter.id, dialog.selected_version.id
                )
            except Exception as exc:
                logger.exception("Could not restore version")
                self.ctx.notify(f"恢复版本失败：{exc}", "error")
                return
            self._current_chapter = restored
            self._load_chapter(restored)
            self.ctx.notify(
                f"已恢复版本 v{dialog.selected_version.version_no}。", "success"
            )

    def _export_chapter(self) -> None:
        chapter = self._current_chapter
        project = self.ctx.current_project
        if chapter is None or project is None:
            self.ctx.notify("请先选择一个章节。", "warning")
            return
        if self._dirty:
            self._save_draft(silent=True)
        filename = self.ctx.export_service.suggested_filename(
            f"{project.title}_第{chapter.number}章_{chapter.title}", "md"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出章节",
            str(self.ctx.paths.exports_dir / filename),
            "Markdown (*.md);;纯文本 (*.txt)",
        )
        if not path:
            return
        target = Path(path)
        try:
            if target.suffix.lower() == ".txt":
                text = f"{chapter.display_title}\n\n{chapter.content}\n"
                target.write_text(text, encoding="utf-8")
            else:
                self.ctx.export_service.export_chapter_markdown(
                    chapter, project.title, target
                )
        except Exception as exc:
            logger.exception("Could not export chapter")
            self.ctx.notify(f"导出失败：{exc}", "error")
            return
        self.ctx.notify(f"已导出到 {target.name}", "success")

    # ------------------------------------------------------------------ close
    def flush(self) -> bool:
        """Persist pending editor changes before export or window close."""

        if self._dirty and self._current_chapter is not None:
            return self._save_draft(silent=True)
        return True

    def has_running_task(self) -> bool:
        return self._generation_active

    def shutdown(self, wait_ms: int = 2000) -> bool:
        if not self._generation_active and self._generation_thread is None:
            return True
        if self._generation_worker is not None:
            self._generation_worker.cancel()
        if self._rewrite_thread is not None and self._rewrite_thread.isRunning():
            return self._rewrite_thread.wait(wait_ms)
        thread = self._generation_thread
        if thread is not None and thread.isRunning():
            return thread.wait(wait_ms)
        return True


def _short(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
