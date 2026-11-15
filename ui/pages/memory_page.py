"""Context memory page: summaries, character state and key memory anchors.

All writes go through :class:`ProjectService`; the page only edits user owned
content (summaries, character ``current_state`` and manual memory items).  The
context builder reads the same data when assembling generation prompts, so the
hints explain how each tab influences the next generation.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Any, Iterator

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from novel_ai.domain.models import Character, MemoryItem, Summary
from novel_ai.ui.icons import icon
from novel_ai.ui.pages.base import BasePage
from novel_ai.ui.widgets import (
    Badge,
    Card,
    ConfirmDialog,
    EmptyState,
    MutedLabel,
    PageHeader,
    ScrollPage,
    SectionTitle,
)

logger = logging.getLogger(__name__)

_MEMORY_KINDS: list[tuple[str, str]] = [
    ("setting", "设定"),
    ("foreshadow", "伏笔"),
    ("timeline", "时间线"),
    ("item", "道具"),
    ("conflict", "冲突"),
    ("relationship", "关系"),
    ("rule", "规则"),
]
_KIND_LABELS: dict[str, str] = dict(_MEMORY_KINDS)
_MEMORY_STATUSES: list[tuple[str, str]] = [
    ("active", "活跃"),
    ("resolved", "已解决"),
]

#: Payloads accepted by :meth:`on_navigated` to jump to a specific tab.
_TAB_KEYS: dict[str, int] = {
    "global": 0,
    "global_summary": 0,
    "volume": 1,
    "volume_summary": 1,
    "chapter": 2,
    "chapter_summary": 2,
    "characters": 3,
    "character": 3,
    "memory": 4,
    "memories": 4,
    "memory_items": 4,
}

_CJK_PATTERN = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_ESTIMATOR: Any = None


def _estimate_tokens(text: str) -> int:
    """Best effort estimate; uses the context estimator when available."""

    if not text:
        return 0
    global _ESTIMATOR
    if _ESTIMATOR is None:
        try:
            from novel_ai.context.estimator import TokenEstimator

            _ESTIMATOR = TokenEstimator()
        except Exception:  # pragma: no cover - context layer unavailable
            _ESTIMATOR = False
    if _ESTIMATOR:
        try:
            return int(_ESTIMATOR.estimate(text))
        except Exception:  # pragma: no cover - defensive
            pass
    cjk = len(_CJK_PATTERN.findall(text))
    other = max(0, len(text) - cjk)
    return max(1, int(cjk + other / 3.5))


class MemoryPage(BasePage):
    page_key = "memory"
    page_title = "上下文记忆"
    page_subtitle = "维护全局 / 分卷 / 章节摘要、角色当前状态与关键记忆，让 AI 记得住前文。"

    def __init__(self, ctx, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self._loading = False
        self._dirty = False
        self._project_id: str | None = None
        self._characters: list[Character] = []
        self._memories: list[MemoryItem] = []
        self._volumes: list[Any] = []
        self._current_memory_id: str | None = None
        self._build_ui()
        self.on_project_changed(getattr(ctx, "current_project", None))

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 24)
        root.setSpacing(12)

        self.header = PageHeader(self.page_title, self.page_subtitle)
        self.dirty_badge = Badge("已同步", "neutral")
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setIcon(icon("refresh"))
        self.refresh_button.setProperty("variant", "secondary")
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.clicked.connect(self._manual_refresh)
        self.header.add_action(self.dirty_badge)
        self.header.add_action(self.refresh_button)
        root.addWidget(self.header)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.empty_page = EmptyState(
            "尚未打开项目",
            "打开或创建一个项目后，可在此管理 AI 写作所需的全局摘要、"
            "分卷 / 章节摘要、角色当前状态和关键记忆。",
            action_text="前往项目仪表盘",
            icon_name="memory",
        )
        self.empty_page.actionClicked.connect(lambda: self.ctx.navigate("dashboard"))
        self.stack.addWidget(self.empty_page)

        self.workspace = QWidget()
        workspace_layout = QVBoxLayout(self.workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_global_tab(), "全局摘要")
        self.tabs.addTab(self._build_volume_tab(), "卷摘要")
        self.tabs.addTab(self._build_chapter_tab(), "章节摘要")
        self.tabs.addTab(self._build_characters_tab(), "角色状态")
        self.tabs.addTab(self._build_memories_tab(), "关键记忆")
        workspace_layout.addWidget(self.tabs)
        self.stack.addWidget(self.workspace)
        self.stack.setCurrentWidget(self.empty_page)

    # ------------------------------------------------------------- global tab
    def _build_global_tab(self) -> QWidget:
        scroll = ScrollPage(margins=(16, 16, 16, 16), spacing=14)
        card = Card(padding=16, spacing=10)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(SectionTitle("全局摘要"))
        head.addStretch(1)
        self.global_meta = Badge("尚未生成", "neutral")
        head.addWidget(self.global_meta)
        card.content_layout.addLayout(head)
        card.content_layout.addWidget(
            MutedLabel(
                "全书级摘要，参与每次生成的最重要上下文之一。"
                "手动保存后会标记为「用户编辑」，自动摘要不会覆盖你的版本。"
            )
        )
        self.global_edit = QPlainTextEdit()
        self.global_edit.setMinimumHeight(280)
        self.global_edit.setPlaceholderText(
            "例如：主角林越因残剑异动被卷入宗门大比，逐渐发现自己的身世与上古剑冢有关……"
        )
        card.content_layout.addWidget(self.global_edit)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.global_tokens = QLabel("约 0 tokens")
        self.global_tokens.setProperty("role", "hint")
        row.addWidget(self.global_tokens, 1)
        self.global_save_button = QPushButton("保存全局摘要")
        self.global_save_button.setProperty("variant", "primary")
        row.addWidget(self.global_save_button)
        card.content_layout.addLayout(row)
        scroll.add(card)
        self.global_edit.textChanged.connect(
            lambda: self._update_token_label(
                self.global_tokens, self.global_edit.toPlainText()
            )
        )
        self.global_edit.textChanged.connect(self._on_editor_changed)
        self.global_save_button.clicked.connect(self._save_global_summary)
        return scroll

    # ------------------------------------------------------------- volume tab
    def _build_volume_tab(self) -> QWidget:
        scroll = ScrollPage(margins=(16, 16, 16, 16), spacing=14)
        card = Card(padding=16, spacing=10)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(SectionTitle("卷摘要"))
        head.addStretch(1)
        self.volume_meta = Badge("尚未生成", "neutral")
        head.addWidget(self.volume_meta)
        card.content_layout.addLayout(head)
        card.content_layout.addWidget(
            MutedLabel("每个分卷的阶段性摘要；生成该卷内章节时会注入，帮助 AI 回顾本卷主线。")
        )
        selector = QHBoxLayout()
        selector.setSpacing(10)
        selector_label = QLabel("选择分卷")
        selector_label.setProperty("role", "caption")
        selector.addWidget(selector_label)
        self.volume_combo = QComboBox()
        self.volume_combo.setMinimumWidth(260)
        self.volume_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        selector.addWidget(self.volume_combo)
        selector.addStretch(1)
        card.content_layout.addLayout(selector)
        self.volume_edit = QPlainTextEdit()
        self.volume_edit.setMinimumHeight(240)
        self.volume_edit.setPlaceholderText("该分卷的阶段性摘要…")
        card.content_layout.addWidget(self.volume_edit)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.volume_tokens = QLabel("约 0 tokens")
        self.volume_tokens.setProperty("role", "hint")
        row.addWidget(self.volume_tokens, 1)
        self.volume_save_button = QPushButton("保存卷摘要")
        self.volume_save_button.setProperty("variant", "primary")
        row.addWidget(self.volume_save_button)
        card.content_layout.addLayout(row)
        scroll.add(card)
        self.volume_combo.currentIndexChanged.connect(self._load_volume_editor)
        self.volume_edit.textChanged.connect(
            lambda: self._update_token_label(
                self.volume_tokens, self.volume_edit.toPlainText()
            )
        )
        self.volume_edit.textChanged.connect(self._on_editor_changed)
        self.volume_save_button.clicked.connect(self._save_volume_summary)
        return scroll

    # ------------------------------------------------------------ chapter tab
    def _build_chapter_tab(self) -> QWidget:
        scroll = ScrollPage(margins=(16, 16, 16, 16), spacing=14)
        card = Card(padding=16, spacing=10)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(SectionTitle("章节摘要"))
        head.addStretch(1)
        self.chapter_meta = Badge("尚未生成", "neutral")
        head.addWidget(self.chapter_meta)
        card.content_layout.addLayout(head)
        card.content_layout.addWidget(
            MutedLabel(
                "每章生成完成后会自动写入章节摘要；你可以手动修正，"
                "修正后的版本不会被自动摘要覆盖。"
            )
        )
        selector = QHBoxLayout()
        selector.setSpacing(10)
        selector_label = QLabel("选择章节")
        selector_label.setProperty("role", "caption")
        selector.addWidget(selector_label)
        self.chapter_combo = QComboBox()
        self.chapter_combo.setMinimumWidth(320)
        self.chapter_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        selector.addWidget(self.chapter_combo)
        selector.addStretch(1)
        card.content_layout.addLayout(selector)
        self.chapter_edit = QPlainTextEdit()
        self.chapter_edit.setMinimumHeight(240)
        self.chapter_edit.setPlaceholderText("该章节的剧情摘要…")
        card.content_layout.addWidget(self.chapter_edit)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.chapter_tokens = QLabel("约 0 tokens")
        self.chapter_tokens.setProperty("role", "hint")
        row.addWidget(self.chapter_tokens, 1)
        self.chapter_save_button = QPushButton("保存章节摘要")
        self.chapter_save_button.setProperty("variant", "primary")
        row.addWidget(self.chapter_save_button)
        card.content_layout.addLayout(row)
        scroll.add(card)
        self.chapter_combo.currentIndexChanged.connect(self._load_chapter_editor)
        self.chapter_edit.textChanged.connect(
            lambda: self._update_token_label(
                self.chapter_tokens, self.chapter_edit.toPlainText()
            )
        )
        self.chapter_edit.textChanged.connect(self._on_editor_changed)
        self.chapter_save_button.clicked.connect(self._save_chapter_summary)
        return scroll

    # ---------------------------------------------------------- characters tab
    def _build_characters_tab(self) -> QWidget:
        scroll = ScrollPage(margins=(16, 16, 16, 16), spacing=14)
        row = QHBoxLayout()
        row.setSpacing(14)

        left_card = Card(padding=14, spacing=8)
        left_card.setMaximumWidth(300)
        left_head = QHBoxLayout()
        left_head.setSpacing(8)
        left_head.addWidget(SectionTitle("角色"))
        left_head.addStretch(1)
        self.character_count_badge = Badge("0", "neutral")
        left_head.addWidget(self.character_count_badge)
        left_card.content_layout.addLayout(left_head)
        self.character_list = QListWidget()
        self.character_list.setMinimumHeight(280)
        left_card.content_layout.addWidget(self.character_list, 1)
        left_card.content_layout.addWidget(
            MutedLabel("选择角色后即可编辑其「当前状态」，完整人设请在「角色」页面维护。")
        )
        row.addWidget(left_card, 0)

        right_card = Card(padding=14, spacing=10)
        self.character_name_label = QLabel("未选择角色")
        self.character_name_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        right_card.content_layout.addWidget(self.character_name_label)
        self.character_role_label = QLabel("")
        self.character_role_label.setProperty("role", "caption")
        right_card.content_layout.addWidget(self.character_role_label)
        right_card.content_layout.addWidget(
            MutedLabel(
                "这里只记录角色当前所处状态（境界、位置、伤势、心态、关系变化等），"
                "生成时会作为「角色当前状态」注入上下文。"
            )
        )
        self.character_state_edit = QPlainTextEdit()
        self.character_state_edit.setMinimumHeight(240)
        self.character_state_edit.setPlaceholderText(
            "例如：刚突破筑基期，左肩有伤，对苏晴心存戒备。"
        )
        right_card.content_layout.addWidget(self.character_state_edit)
        state_row = QHBoxLayout()
        state_row.setSpacing(10)
        self.character_tokens = QLabel("约 0 tokens")
        self.character_tokens.setProperty("role", "hint")
        state_row.addWidget(self.character_tokens, 1)
        self.character_save_button = QPushButton("保存角色状态")
        self.character_save_button.setProperty("variant", "primary")
        state_row.addWidget(self.character_save_button)
        right_card.content_layout.addLayout(state_row)
        row.addWidget(right_card, 1)
        scroll.add_layout(row)

        self.character_list.currentItemChanged.connect(self._load_character_editor)
        self.character_state_edit.textChanged.connect(
            lambda: self._update_token_label(
                self.character_tokens, self.character_state_edit.toPlainText()
            )
        )
        self.character_state_edit.textChanged.connect(self._on_editor_changed)
        self.character_save_button.clicked.connect(self._save_character_state)
        return scroll

    # ----------------------------------------------------------- memories tab
    def _build_memories_tab(self) -> QWidget:
        scroll = ScrollPage(margins=(16, 16, 16, 16), spacing=14)
        row = QHBoxLayout()
        row.setSpacing(14)

        left_card = Card(padding=14, spacing=8)
        left_card.setMaximumWidth(360)
        left_head = QHBoxLayout()
        left_head.setSpacing(8)
        left_head.addWidget(SectionTitle("关键记忆"))
        left_head.addStretch(1)
        self.memory_count_badge = Badge("0", "neutral")
        left_head.addWidget(self.memory_count_badge)
        left_card.content_layout.addLayout(left_head)
        self.memory_list = QListWidget()
        self.memory_list.setMinimumHeight(280)
        left_card.content_layout.addWidget(self.memory_list, 1)
        memory_buttons = QHBoxLayout()
        memory_buttons.setSpacing(8)
        self.memory_new_button = QPushButton("新增记忆")
        self.memory_new_button.setProperty("variant", "secondary")
        self.memory_delete_button = QPushButton("删除")
        self.memory_delete_button.setProperty("variant", "danger")
        self.memory_delete_button.setEnabled(False)
        memory_buttons.addWidget(self.memory_new_button)
        memory_buttons.addWidget(self.memory_delete_button)
        memory_buttons.addStretch(1)
        left_card.content_layout.addLayout(memory_buttons)
        row.addWidget(left_card, 0)

        right_card = Card(padding=14, spacing=10)
        right_head = QHBoxLayout()
        right_head.setSpacing(8)
        right_head.addWidget(SectionTitle("记忆详情"))
        right_head.addStretch(1)
        self.memory_mode_badge = Badge("新建", "neutral")
        right_head.addWidget(self.memory_mode_badge)
        right_card.content_layout.addLayout(right_head)
        right_card.content_layout.addWidget(
            MutedLabel(
                "伏笔、规则、道具、时间线等长期设定。优先级 1（最低）~ 5（最高）；"
                "上下文预算紧张时高优先级记忆优先保留。"
            )
        )
        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        self.memory_kind = QComboBox()
        for kind, label in _MEMORY_KINDS:
            self.memory_kind.addItem(label, kind)
        self.memory_title = QLineEdit()
        self.memory_title.setPlaceholderText("例如：残剑的来历")
        self.memory_priority = QSpinBox()
        self.memory_priority.setRange(1, 5)
        self.memory_priority.setValue(3)
        self.memory_status = QComboBox()
        for status, label in _MEMORY_STATUSES:
            self.memory_status.addItem(label, status)
        form.addRow("类型", self.memory_kind)
        form.addRow("标题", self.memory_title)
        form.addRow("优先级", self.memory_priority)
        form.addRow("状态", self.memory_status)
        right_card.content_layout.addLayout(form)
        content_label = QLabel("内容")
        content_label.setProperty("role", "caption")
        right_card.content_layout.addWidget(content_label)
        self.memory_content = QPlainTextEdit()
        self.memory_content.setMinimumHeight(170)
        self.memory_content.setPlaceholderText(
            "写下必须遵守的长期设定：伏笔线索、世界规则、道具信息、时间线锚点……"
        )
        right_card.content_layout.addWidget(self.memory_content)
        memory_row = QHBoxLayout()
        memory_row.setSpacing(10)
        self.memory_tokens = QLabel("约 0 tokens")
        self.memory_tokens.setProperty("role", "hint")
        memory_row.addWidget(self.memory_tokens, 1)
        self.memory_save_button = QPushButton("保存记忆")
        self.memory_save_button.setProperty("variant", "primary")
        memory_row.addWidget(self.memory_save_button)
        right_card.content_layout.addLayout(memory_row)
        row.addWidget(right_card, 1)
        scroll.add_layout(row)

        self.memory_list.currentItemChanged.connect(self._on_memory_selected)
        self.memory_new_button.clicked.connect(self._new_memory)
        self.memory_delete_button.clicked.connect(self._delete_memory)
        self.memory_save_button.clicked.connect(self._save_memory)
        self.memory_content.textChanged.connect(
            lambda: self._update_token_label(
                self.memory_tokens, self.memory_content.toPlainText()
            )
        )
        self.memory_title.textChanged.connect(self._on_editor_changed)
        self.memory_content.textChanged.connect(self._on_editor_changed)
        return scroll

    # ------------------------------------------------------------- data load
    def _load_from_snapshot(self) -> None:
        project = getattr(self.ctx, "current_project", None)
        snapshot = getattr(self.ctx, "snapshot", None)
        if project is None or snapshot is None:
            self._project_id = None
            self.stack.setCurrentWidget(self.empty_page)
            self.header.set_subtitle(self.page_subtitle)
            self._dirty = False
            self._update_dirty_badge()
            return
        self._project_id = project.id
        self.stack.setCurrentWidget(self.workspace)
        self.header.set_subtitle(
            f"当前项目：{project.title} · 摘要、角色状态与关键记忆会参与上下文构建"
        )
        self._loading = True
        try:
            self._load_global(snapshot)
            self._load_volumes(snapshot)
            self._load_chapters(snapshot)
            self._load_characters(snapshot)
            self._load_memories(snapshot)
        finally:
            self._loading = False
            self._dirty = False
            self._update_dirty_badge()

    def _load_global(self, snapshot: Any) -> None:
        summary = self._summary_for(snapshot, "global", None)
        with self._suspend_dirty():
            self.global_edit.setPlainText(summary.content if summary else "")
        self._set_summary_meta(self.global_meta, summary)
        self._update_token_label(
            self.global_tokens, self.global_edit.toPlainText()
        )

    def _load_volumes(self, snapshot: Any) -> None:
        ordered = getattr(snapshot, "ordered_outline", None)
        nodes = ordered() if callable(ordered) else list(getattr(snapshot, "outline", []))
        self._volumes = [
            node for node in nodes if getattr(node, "kind", "") == "volume"
        ]
        keep = self.volume_combo.currentData()
        items = [
            (str(getattr(node, "title", "") or "未命名分卷"), node.id)
            for node in self._volumes
        ]
        self._fill_combo(self.volume_combo, items, keep)
        has_volumes = bool(self._volumes)
        self.volume_combo.setEnabled(has_volumes)
        self.volume_edit.setEnabled(has_volumes)
        self.volume_save_button.setEnabled(has_volumes)
        if not has_volumes:
            with self._suspend_dirty():
                self.volume_edit.setPlainText("")
            self.volume_edit.setPlaceholderText("请先在大纲页面创建分卷。")
            self.volume_meta.setText("无分卷")
            self.volume_meta.set_variant("neutral")
            self.volume_tokens.setText("")
            return
        self.volume_edit.setPlaceholderText("该分卷的阶段性摘要…")
        self._load_volume_editor()

    def _load_chapters(self, snapshot: Any) -> None:
        ordered = getattr(snapshot, "ordered_chapters", None)
        chapters = ordered() if callable(ordered) else list(getattr(snapshot, "chapters", []))
        keep = self.chapter_combo.currentData()
        items = [
            (
                str(
                    getattr(chapter, "display_title", "")
                    or getattr(chapter, "title", "")
                    or f"第 {getattr(chapter, 'number', '?')} 章"
                ),
                chapter.id,
            )
            for chapter in chapters
        ]
        self._fill_combo(self.chapter_combo, items, keep)
        has_chapters = bool(chapters)
        self.chapter_combo.setEnabled(has_chapters)
        self.chapter_edit.setEnabled(has_chapters)
        self.chapter_save_button.setEnabled(has_chapters)
        if not has_chapters:
            with self._suspend_dirty():
                self.chapter_edit.setPlainText("")
            self.chapter_edit.setPlaceholderText("项目还没有章节，请先在章节创作页面创建。")
            self.chapter_meta.setText("无章节")
            self.chapter_meta.set_variant("neutral")
            self.chapter_tokens.setText("")
            return
        self.chapter_edit.setPlaceholderText("该章节的剧情摘要…")
        self._load_chapter_editor()

    def _load_characters(self, snapshot: Any) -> None:
        characters = list(getattr(snapshot, "characters", []))
        self._characters = characters
        keep = self._selected_character_id()
        self.character_list.blockSignals(True)
        try:
            self.character_list.clear()
            ordered = sorted(
                characters,
                key=lambda item: (
                    getattr(item, "sort_order", 0),
                    str(getattr(item, "name", "")),
                ),
            )
            for character in ordered:
                label = str(getattr(character, "name", "") or "未命名角色")
                role = str(getattr(character, "role", "") or "")
                if role:
                    label = f"{label} · {role}"
                if not getattr(character, "is_active", True):
                    label = f"{label}（停用）"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, character.id)
                item.setToolTip(
                    str(getattr(character, "current_state", "") or "暂无当前状态")
                )
                self.character_list.addItem(item)
        finally:
            self.character_list.blockSignals(False)
        self.character_count_badge.setText(str(len(characters)))
        target_row = self._row_for_id(self.character_list, keep)
        if target_row < 0 and self.character_list.count():
            target_row = 0
        if target_row >= 0:
            self.character_list.setCurrentRow(target_row)
        self._load_character_editor()

    def _load_memories(self, snapshot: Any) -> None:
        items = list(getattr(snapshot, "memories", []))
        items.sort(
            key=lambda memory: (
                0 if getattr(memory, "status", "active") == "active" else 1,
                -int(getattr(memory, "priority", 3) or 3),
                str(getattr(memory, "created_at", "")),
            )
        )
        self._memories = items
        keep = self._current_memory_id
        self.memory_list.blockSignals(True)
        try:
            self.memory_list.clear()
            for memory in items:
                label = f"[P{int(getattr(memory, 'priority', 3) or 3)}] {memory.title}"
                if getattr(memory, "status", "active") == "resolved":
                    label = f"✓ {label}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, memory.id)
                kind_label = _KIND_LABELS.get(memory.kind, memory.kind)
                item.setToolTip(f"{kind_label} · {memory.content[:160]}")
                self.memory_list.addItem(item)
        finally:
            self.memory_list.blockSignals(False)
        self.memory_count_badge.setText(str(len(items)))
        target_row = self._row_for_id(self.memory_list, keep)
        if target_row < 0 and items:
            target_row = 0
        if target_row >= 0:
            self.memory_list.setCurrentRow(target_row)
            self._on_memory_selected()
        else:
            self._current_memory_id = None
            self._reset_memory_form()
            self.memory_delete_button.setEnabled(False)

    # ----------------------------------------------------- summary editors
    def _load_volume_editor(self, _index: int = 0) -> None:
        volume_id = self.volume_combo.currentData()
        snapshot = getattr(self.ctx, "snapshot", None)
        summary = None
        if snapshot is not None and volume_id:
            summary = self._summary_for(snapshot, "volume", volume_id)
        with self._suspend_dirty():
            self.volume_edit.setPlainText(summary.content if summary else "")
        self._set_summary_meta(self.volume_meta, summary)
        self._update_token_label(self.volume_tokens, self.volume_edit.toPlainText())

    def _load_chapter_editor(self, _index: int = 0) -> None:
        chapter_id = self.chapter_combo.currentData()
        snapshot = getattr(self.ctx, "snapshot", None)
        summary = None
        if snapshot is not None and chapter_id:
            summary = self._summary_for(snapshot, "chapter", chapter_id)
        with self._suspend_dirty():
            self.chapter_edit.setPlainText(summary.content if summary else "")
        self._set_summary_meta(self.chapter_meta, summary)
        self._update_token_label(self.chapter_tokens, self.chapter_edit.toPlainText())

    def _save_global_summary(self) -> None:
        self._save_summary("global", self.global_edit, None, "全局摘要")

    def _save_volume_summary(self) -> None:
        volume_id = self.volume_combo.currentData()
        if not volume_id:
            self.ctx.notify("请先选择要编辑的分卷。", "warning")
            return
        self._save_summary("volume", self.volume_edit, volume_id, "卷摘要")

    def _save_chapter_summary(self) -> None:
        chapter_id = self.chapter_combo.currentData()
        if not chapter_id:
            self.ctx.notify("请先选择要编辑的章节。", "warning")
            return
        self._save_summary("chapter", self.chapter_edit, chapter_id, "章节摘要")

    def _save_summary(
        self,
        scope: str,
        editor: QPlainTextEdit,
        ref_id: str | None,
        label: str,
    ) -> None:
        project = getattr(self.ctx, "current_project", None)
        if project is None:
            self.ctx.notify("请先打开项目。", "warning")
            return
        content = editor.toPlainText().strip()
        if not content:
            self.ctx.notify(f"{label}内容为空，未保存。", "warning")
            return
        try:
            self.ctx.project_service.save_summary(
                project.id,
                scope,
                content,
                ref_id=ref_id,
                strategy="manual",
                is_user_edited=True,
            )
        except Exception as exc:
            logger.exception("Could not save %s summary", scope)
            self.ctx.notify(f"保存{label}失败：{exc}", "error")
            return
        self._after_write(f"{label}已保存，并标记为用户编辑。")

    # ------------------------------------------------------ character state
    def _selected_character(self) -> Character | None:
        character_id = self._selected_character_id()
        if not character_id:
            return None
        return next(
            (item for item in self._characters if item.id == character_id), None
        )

    def _selected_character_id(self) -> str | None:
        item = self.character_list.currentItem()
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def _load_character_editor(self, _current=None, _previous=None) -> None:  # noqa: ANN001
        character = self._selected_character()
        if character is None:
            self.character_name_label.setText("未选择角色")
            self.character_role_label.setText("")
            with self._suspend_dirty():
                self.character_state_edit.setPlainText("")
            self.character_state_edit.setEnabled(False)
            self.character_state_edit.setPlaceholderText("请先在「角色」页面创建角色。")
            self.character_tokens.setText("")
            self.character_save_button.setEnabled(False)
            return
        self.character_name_label.setText(character.name)
        role = str(getattr(character, "role", "") or "")
        active_text = "活跃" if getattr(character, "is_active", True) else "已停用"
        self.character_role_label.setText(
            " · ".join(part for part in (role, active_text) if part)
        )
        self.character_state_edit.setEnabled(True)
        self.character_state_edit.setPlaceholderText(
            "例如：刚突破筑基期，左肩有伤，对苏晴心存戒备。"
        )
        with self._suspend_dirty():
            self.character_state_edit.setPlainText(
                str(getattr(character, "current_state", "") or "")
            )
        self._update_token_label(
            self.character_tokens, self.character_state_edit.toPlainText()
        )
        self.character_save_button.setEnabled(True)

    def _save_character_state(self) -> None:
        project = getattr(self.ctx, "current_project", None)
        character = self._selected_character()
        if project is None:
            self.ctx.notify("请先打开项目。", "warning")
            return
        if character is None:
            self.ctx.notify("请先选择一个角色。", "warning")
            return
        new_state = self.character_state_edit.toPlainText().strip()
        if new_state == str(getattr(character, "current_state", "") or "").strip():
            self.ctx.notify("角色状态没有变化。", "info")
            return
        character.current_state = new_state
        try:
            self.ctx.project_service.save_character(character)
        except Exception as exc:
            logger.exception("Could not save character state")
            self.ctx.notify(f"保存角色状态失败：{exc}", "error")
            return
        self._after_write(f"角色「{character.name}」的当前状态已保存。")

    # --------------------------------------------------------- memory items
    def _find_memory(self, memory_id: str | None) -> MemoryItem | None:
        if not memory_id:
            return None
        return next((item for item in self._memories if item.id == memory_id), None)

    def _on_memory_selected(self, _current=None, _previous=None) -> None:  # noqa: ANN001
        item = self.memory_list.currentItem()
        if item is None:
            self._current_memory_id = None
            self._reset_memory_form()
            self.memory_delete_button.setEnabled(False)
            return
        value = item.data(Qt.ItemDataRole.UserRole)
        self._current_memory_id = str(value) if value else None
        self._load_memory_form()
        self.memory_delete_button.setEnabled(True)

    def _ensure_kind_option(self, kind: str) -> None:
        if kind and self.memory_kind.findData(kind) < 0:
            self.memory_kind.addItem(f"其他（{kind}）", kind)

    def _reset_memory_form(self) -> None:
        with self._suspend_dirty():
            self._ensure_kind_option("setting")
            self.memory_kind.setCurrentIndex(
                max(0, self.memory_kind.findData("setting"))
            )
            self.memory_title.setText("")
            self.memory_content.setPlainText("")
            self.memory_priority.setValue(3)
            self.memory_status.setCurrentIndex(
                max(0, self.memory_status.findData("active"))
            )
        self.memory_mode_badge.setText("新建")
        self.memory_mode_badge.set_variant("neutral")
        self._update_token_label(self.memory_tokens, "")

    def _load_memory_form(self) -> None:
        memory = self._find_memory(self._current_memory_id)
        if memory is None:
            self._reset_memory_form()
            return
        with self._suspend_dirty():
            self._ensure_kind_option(memory.kind)
            self.memory_kind.setCurrentIndex(
                max(0, self.memory_kind.findData(memory.kind))
            )
            self.memory_title.setText(memory.title)
            self.memory_content.setPlainText(memory.content)
            self.memory_priority.setValue(max(1, min(5, int(memory.priority or 3))))
            status = str(getattr(memory, "status", "active") or "active")
            self.memory_status.setCurrentIndex(
                max(0, self.memory_status.findData(status))
            )
        self.memory_mode_badge.setText("编辑")
        self.memory_mode_badge.set_variant("accent")
        self._update_token_label(
            self.memory_tokens, self.memory_content.toPlainText()
        )

    def _new_memory(self) -> None:
        self.memory_list.blockSignals(True)
        try:
            self.memory_list.clearSelection()
            self.memory_list.setCurrentRow(-1)
        finally:
            self.memory_list.blockSignals(False)
        self._current_memory_id = None
        self._reset_memory_form()
        self.memory_delete_button.setEnabled(False)
        self.memory_title.setFocus()

    def _save_memory(self) -> None:
        project = getattr(self.ctx, "current_project", None)
        if project is None:
            self.ctx.notify("请先打开项目。", "warning")
            return
        title = self.memory_title.text().strip()
        if not title:
            self.ctx.notify("请填写记忆标题。", "warning")
            self.memory_title.setFocus()
            return
        content = self.memory_content.toPlainText().strip()
        kind = str(self.memory_kind.currentData() or "setting")
        priority = int(self.memory_priority.value())
        status = str(self.memory_status.currentData() or "active")
        memory = self._find_memory(self._current_memory_id)
        memory_id: str | None = None
        try:
            if memory is None:
                created = self.ctx.project_service.create_memory(
                    project.id,
                    kind,
                    title,
                    content,
                    priority=priority,
                    status=status,
                    is_manual=True,
                )
                memory_id = str(getattr(created, "id", "") or "") or None
                message = "关键记忆已创建。"
            else:
                memory.kind = kind
                memory.title = title
                memory.content = content
                memory.priority = priority
                memory.status = status
                memory.is_manual = True
                self.ctx.project_service.save_memory(memory)
                memory_id = memory.id
                message = "关键记忆已保存。"
        except Exception as exc:
            logger.exception("Could not save memory item")
            self.ctx.notify(f"保存关键记忆失败：{exc}", "error")
            return
        self._after_write(message, memory_id=memory_id)

    def _delete_memory(self) -> None:
        project = getattr(self.ctx, "current_project", None)
        memory = self._find_memory(self._current_memory_id)
        if project is None:
            self.ctx.notify("请先打开项目。", "warning")
            return
        if memory is None:
            self.ctx.notify("请先选择要删除的记忆。", "warning")
            return
        confirmed = ConfirmDialog.ask(
            self,
            "删除关键记忆",
            f"确定删除「{memory.title}」吗？删除后该记忆将不再参与上下文构建，"
            "此操作不可撤销。",
            confirm_text="删除",
            danger=True,
        )
        if not confirmed:
            return
        try:
            self.ctx.project_service.delete_memory(memory.id)
        except Exception as exc:
            logger.exception("Could not delete memory item")
            self.ctx.notify(f"删除关键记忆失败：{exc}", "error")
            return
        self._current_memory_id = None
        self._after_write("关键记忆已删除。")

    # --------------------------------------------------------------- helpers
    def _after_write(self, message: str, *, memory_id: str | None = None) -> None:
        self.ctx.notify(message, "success")
        self._dirty = False
        try:
            self.ctx.refresh_snapshot()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not refresh snapshot: %s", exc)
        self._load_from_snapshot()
        if memory_id is not None:
            self._select_memory(memory_id)
        self._update_dirty_badge()
        mark = getattr(self.ctx, "mark_data_changed", None)
        if callable(mark):
            try:
                mark("memory")
            except Exception:  # pragma: no cover - defensive
                pass

    def _select_memory(self, memory_id: str) -> None:
        row = self._row_for_id(self.memory_list, memory_id)
        if row >= 0:
            self.memory_list.setCurrentRow(row)
            self._on_memory_selected()

    @staticmethod
    def _row_for_id(widget: QListWidget, item_id: str | None) -> int:
        if not item_id:
            return -1
        for row in range(widget.count()):
            item = widget.item(row)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == item_id:
                return row
        return -1

    @staticmethod
    def _summary_for(snapshot: Any, scope: str, ref_id: str | None) -> Summary | None:
        lookup = getattr(snapshot, "summary_for", None)
        if callable(lookup):
            try:
                return lookup(scope, ref_id)
            except Exception:  # pragma: no cover - defensive
                pass
        for item in getattr(snapshot, "summaries", []):
            if item.scope == scope and (item.ref_id or None) == (ref_id or None):
                return item
        return None

    @staticmethod
    def _set_summary_meta(badge: Badge, summary: Summary | None) -> None:
        if summary is None:
            badge.setText("尚未生成")
            badge.set_variant("neutral")
        elif getattr(summary, "is_user_edited", False):
            badge.setText("用户编辑")
            badge.set_variant("warning")
        else:
            badge.setText("自动生成")
            badge.set_variant("neutral")

    @staticmethod
    def _fill_combo(
        combo: QComboBox, items: list[tuple[str, str]], keep: str | None
    ) -> None:
        combo.blockSignals(True)
        try:
            combo.clear()
            for label, data in items:
                combo.addItem(label, data)
            if items:
                index = combo.findData(keep) if keep else -1
                combo.setCurrentIndex(index if index >= 0 else 0)
            else:
                combo.setCurrentIndex(-1)
        finally:
            combo.blockSignals(False)

    def _update_token_label(self, label: QLabel, text: str) -> None:
        stripped = text.strip()
        if not stripped:
            label.setText("约 0 tokens")
            return
        label.setText(
            f"{len(text)} 字符 · 约 {_estimate_tokens(text)} tokens"
        )

    def _on_editor_changed(self) -> None:
        if self._loading:
            return
        if not self._dirty:
            self._dirty = True
            self._update_dirty_badge()

    def _update_dirty_badge(self) -> None:
        if self._dirty:
            self.dirty_badge.setText("有未保存修改")
            self.dirty_badge.set_variant("warning")
        else:
            self.dirty_badge.setText("已同步")
            self.dirty_badge.set_variant("neutral")

    @contextmanager
    def _suspend_dirty(self) -> Iterator[None]:
        previous = self._loading
        self._loading = True
        try:
            yield
        finally:
            self._loading = previous

    # ------------------------------------------------------------- lifecycle
    def _manual_refresh(self) -> None:
        if self._dirty:
            confirmed = ConfirmDialog.ask(
                self,
                "放弃未保存修改？",
                "刷新会从数据库重新加载摘要、角色状态与关键记忆，"
                "当前未保存的修改将会丢失。确定刷新吗？",
                confirm_text="刷新",
                danger=True,
            )
            if not confirmed:
                return
            self._dirty = False
        self.refresh()

    def refresh(self) -> None:
        if self._dirty:
            self._update_dirty_badge()
            return
        try:
            self.ctx.refresh_snapshot()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not refresh snapshot: %s", exc)
        self._load_from_snapshot()

    def on_project_changed(self, project) -> None:  # noqa: ANN001 - BasePage hook
        self._dirty = False
        if project is not None and getattr(self.ctx, "snapshot", None) is None:
            try:
                self.ctx.refresh_snapshot()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Could not refresh snapshot: %s", exc)
        self._load_from_snapshot()

    def on_navigated(self, payload: object = None) -> None:
        if isinstance(payload, str):
            index = _TAB_KEYS.get(payload)
            if index is not None and index < self.tabs.count():
                self.tabs.setCurrentIndex(index)
        self.refresh()


__all__ = ["MemoryPage"]
