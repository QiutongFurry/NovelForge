"""角色设定页面。

左侧是角色列表（``QListWidget`` 卡片式列表），右侧是滚动的人物档案编辑器：
姓名 / 定位 / 年龄 / 外貌 / 性格 / 背景 / 目标 / 成长线 / 当前状态 / 人物关系 / 备注。

数据全部来自 ``AppContext.snapshot.characters``，写入通过
``ProjectService.create_character / save_character / delete_character``，
之后调用 ``ctx.refresh_snapshot()``、``ctx.notify`` 和 ``ctx.dataChanged.emit``。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from novel_ai.domain.models import Character, utc_now
from novel_ai.ui.pages.base import BasePage
from novel_ai.ui.widgets import (
    Card,
    ConfirmDialog,
    EmptyState,
    PageHeader,
    ScrollPage,
    tool_button,
)

logger = logging.getLogger(__name__)


def _section_card(title: str, subtitle: str = "") -> tuple[Card, QGridLayout]:
    card = Card(padding=18, spacing=10)
    title_label = QLabel(title)
    title_label.setObjectName("SectionTitle")
    card.add_widget(title_label)
    if subtitle:
        subtitle_label = QLabel(subtitle)
        subtitle_label.setProperty("role", "caption")
        subtitle_label.setWordWrap(True)
        card.add_widget(subtitle_label)
    grid = QGridLayout()
    grid.setHorizontalSpacing(14)
    grid.setVerticalSpacing(10)
    grid.setColumnStretch(1, 1)
    grid.setColumnMinimumWidth(0, 78)
    card.content_layout.addLayout(grid)
    return card, grid


def _add_row(
    grid: QGridLayout,
    row: int,
    label: str,
    field: QWidget,
    hint: str = "",
) -> int:
    caption = QLabel(label)
    caption.setProperty("role", "caption")
    caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
    caption.setMinimumWidth(72)
    grid.addWidget(caption, row, 0, Qt.AlignmentFlag.AlignTop)

    if hint:
        box = QWidget()
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSpacing(4)
        box_layout.addWidget(field)
        hint_label = QLabel(hint)
        hint_label.setProperty("role", "hint")
        hint_label.setWordWrap(True)
        box_layout.addWidget(hint_label)
        grid.addWidget(box, row, 1)
    else:
        grid.addWidget(field, row, 1)
    return row + 1


class CharactersPage(BasePage):
    """角色档案页：列表 + 表单编辑器。"""

    page_key = "characters"
    page_title = "角色设定"
    page_subtitle = "维护人物档案与关系网，让每个角色在长篇中保持一致。"

    def __init__(self, ctx: Any, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self._characters: list[Character] = []
        self._by_id: dict[str, Character] = {}
        self._current_id: str | None = None
        self._current_character: Character | None = None
        self._dirty = False
        self._loading = False
        self._project_id: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        self._empty_page = self._build_empty_page()
        self._stack.addWidget(self._empty_page)

        self._content = self._build_content()
        self._stack.addWidget(self._content)

        self.refresh()

    # ------------------------------------------------------------------ 构建
    def _build_empty_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(18)
        layout.addWidget(PageHeader(self.page_title, self.page_subtitle))
        empty = EmptyState(
            "尚未打开项目",
            "角色档案需要依附于一个项目。请先到项目工坊打开或创建项目。",
            icon_name="users",
            action_text="去项目工坊",
        )
        empty.actionClicked.connect(lambda: self.ctx.navigate("dashboard"))
        layout.addWidget(empty, 1)
        return page

    def _build_content(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(14)

        self.header = PageHeader(self.page_title, self.page_subtitle)
        self.new_button = tool_button(
            "新建角色", "plus", variant="secondary", tooltip="创建一个新的角色档案"
        )
        self.new_button.clicked.connect(self._create_character)
        self.save_button = QPushButton("保存角色")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save_character)
        self.delete_button = tool_button("删除", "trash", variant="danger", tooltip="删除当前角色")
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._delete_character)
        self.header.add_action(self.new_button)
        self.header.add_action(self.delete_button)
        self.header.add_action(self.save_button)
        layout.addWidget(self.header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)

        # ---------------------------------------------------------- 左侧列表
        left_card = Card(padding=14, spacing=10)
        left_card.setMinimumWidth(236)
        left_card.setMaximumWidth(360)
        left_header = QHBoxLayout()
        left_header.setSpacing(8)
        left_title = QLabel("角色列表")
        left_title.setObjectName("SectionTitle")
        left_header.addWidget(left_title)
        left_header.addStretch(1)
        self.count_label = QLabel("0 个角色")
        self.count_label.setProperty("role", "caption")
        left_header.addWidget(self.count_label)
        left_card.content_layout.addLayout(left_header)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索角色名或定位…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        left_card.content_layout.addWidget(self.search_edit)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("ChapterList")
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.list_widget.currentRowChanged.connect(self._on_row_changed)
        left_card.content_layout.addWidget(self.list_widget, 1)
        splitter.addWidget(left_card)

        # ---------------------------------------------------------- 右侧编辑器
        self._editor_stack = QStackedWidget()
        self._editor_stack.setMinimumWidth(430)

        self._placeholder_page = QWidget()
        placeholder_layout = QVBoxLayout(self._placeholder_page)
        placeholder_layout.setContentsMargins(16, 24, 16, 24)
        self._placeholder = EmptyState(
            "选择或新建一个角色",
            "从左侧选择角色开始编辑；还没有角色时，点击「新建角色」开始搭建人物档案。",
            icon_name="users",
            action_text="新建角色",
        )
        self._placeholder.actionClicked.connect(self._create_character)
        placeholder_layout.addWidget(self._placeholder, 1)
        self._editor_stack.addWidget(self._placeholder_page)

        self._editor_scroll = self._build_editor()
        self._editor_stack.addWidget(self._editor_scroll)
        splitter.addWidget(self._editor_stack)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 900])
        layout.addWidget(splitter, 1)
        return content

    def _build_editor(self) -> ScrollPage:
        scroll = ScrollPage(margins=(2, 0, 8, 10), spacing=14)

        # ---------------------------------------------------------- 基本信息
        basic_card, basic_grid = _section_card("基本信息")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("角色姓名")
        self.role_edit = QLineEdit()
        self.role_edit.setPlaceholderText("主角 / 反派 / 导师 / 配角……")
        self.age_edit = QLineEdit()
        self.age_edit.setPlaceholderText("例如：27 岁，或「外表 20，实际 300 岁」")
        self.active_check = QCheckBox("活跃角色（参与生成上下文）")
        row = 0
        row = _add_row(basic_grid, row, "姓名", self.name_edit, "保存前不能为空。")
        row = _add_row(basic_grid, row, "定位", self.role_edit)
        row = _add_row(basic_grid, row, "年龄", self.age_edit)
        _add_row(basic_grid, row, "状态", self.active_check)
        scroll.add(basic_card)

        # ---------------------------------------------------------- 外貌与性格
        look_card, look_grid = _section_card(
            "外貌与性格", "细节越具体，AI 写出的角色动作和台词越稳定。"
        )
        self.appearance_edit = QPlainTextEdit()
        self.appearance_edit.setPlaceholderText("外貌、穿着、体态、标志性小动作与随身物件。")
        self.appearance_edit.setMinimumHeight(84)
        self.personality_edit = QPlainTextEdit()
        self.personality_edit.setPlaceholderText("性格关键词、说话方式、价值观、弱点与雷区。")
        self.personality_edit.setMinimumHeight(96)
        row = 0
        row = _add_row(look_grid, row, "外貌", self.appearance_edit)
        _add_row(look_grid, row, "性格", self.personality_edit)
        scroll.add(look_card)

        # ---------------------------------------------------------- 背景与目标
        story_card, story_grid = _section_card("背景与目标")
        self.background_edit = QPlainTextEdit()
        self.background_edit.setPlaceholderText("出身、经历、与主线相关的秘密或伏笔。")
        self.background_edit.setMinimumHeight(100)
        self.goals_edit = QPlainTextEdit()
        self.goals_edit.setPlaceholderText("角色当前想要什么？短期目标与长期目标分别是什么？")
        self.goals_edit.setMinimumHeight(76)
        self.arc_edit = QPlainTextEdit()
        self.arc_edit.setPlaceholderText("人物成长弧光：从……到……，关键转折点在哪里。")
        self.arc_edit.setMinimumHeight(76)
        row = 0
        row = _add_row(story_grid, row, "背景", self.background_edit)
        row = _add_row(story_grid, row, "目标", self.goals_edit)
        _add_row(story_grid, row, "成长线", self.arc_edit)
        scroll.add(story_card)

        # ---------------------------------------------------------- 当前状态与备注
        state_card, state_grid = _section_card(
            "当前状态与备注", "当前状态会进入生成上下文，记录角色此刻的位置、处境与心理。"
        )
        self.current_state_edit = QPlainTextEdit()
        self.current_state_edit.setPlaceholderText("例如：身负重伤，藏身城南药铺，怀疑主角身份。")
        self.current_state_edit.setMinimumHeight(76)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("写作备忘、灵感、待补充的设定……")
        self.notes_edit.setMinimumHeight(70)
        row = 0
        row = _add_row(state_grid, row, "当前状态", self.current_state_edit)
        _add_row(state_grid, row, "备注", self.notes_edit)
        scroll.add(state_card)

        # ---------------------------------------------------------- 人物关系
        rel_card = Card(padding=18, spacing=10)
        rel_title = QLabel("人物关系")
        rel_title.setObjectName("SectionTitle")
        rel_card.add_widget(rel_title)
        rel_hint = QLabel(
            "每一行是一段关系：对象（对方姓名）、关系（师徒 / 宿敌 / 恋人……）、备注。"
            "关系会写入角色卡，帮助 AI 保持互动逻辑一致。"
        )
        rel_hint.setProperty("role", "caption")
        rel_hint.setWordWrap(True)
        rel_card.add_widget(rel_hint)

        self.relationship_table = QTableWidget(0, 3)
        self.relationship_table.setHorizontalHeaderLabels(["对象", "关系", "备注"])
        self.relationship_table.verticalHeader().setVisible(False)
        self.relationship_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.relationship_table.setMinimumHeight(140)
        header = self.relationship_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.relationship_table.cellChanged.connect(self._mark_dirty)
        rel_card.add_widget(self.relationship_table)

        rel_actions = QHBoxLayout()
        rel_actions.setSpacing(8)
        add_rel_button = tool_button("添加关系", "plus", variant="secondary")
        add_rel_button.clicked.connect(self._add_relationship_row)
        remove_rel_button = tool_button("删除选中", "trash", variant="ghost")
        remove_rel_button.clicked.connect(self._remove_relationship_row)
        rel_actions.addWidget(add_rel_button)
        rel_actions.addWidget(remove_rel_button)
        rel_actions.addStretch(1)
        rel_card.content_layout.addLayout(rel_actions)
        scroll.add(rel_card)

        scroll.body_layout.addStretch(1)

        # 编辑信号 -> 脏标记（填充表单时由 _loading 闸门拦截）。
        for line_edit in (self.name_edit, self.role_edit, self.age_edit):
            line_edit.textChanged.connect(self._mark_dirty)
        for plain in (
            self.appearance_edit,
            self.personality_edit,
            self.background_edit,
            self.goals_edit,
            self.arc_edit,
            self.current_state_edit,
            self.notes_edit,
        ):
            plain.textChanged.connect(self._mark_dirty)
        self.active_check.toggled.connect(self._mark_dirty)
        return scroll

    # ------------------------------------------------------------------ 生命周期
    def on_project_changed(self, project: Any) -> None:
        if project is None:
            self._project_id = None
            self._dirty = False
            self._current_id = None
            self._current_character = None
            self._update_actions()
            self._stack.setCurrentWidget(self._empty_page)
            return
        if self._project_id != project.id:
            self._project_id = project.id
            self._dirty = False
            self._current_id = None
            self._current_character = None
        self._reload_list(force=True)

    def on_navigated(self, payload: object = None) -> None:
        character_id = payload.get("character_id") if isinstance(payload, dict) else None
        if character_id and not self._dirty:
            self._reload_list(select_id=str(character_id), force=True)
        else:
            self.refresh()

    def refresh(self) -> None:
        if self._dirty:
            return
        self._refresh_snapshot_quietly()
        self._reload_list()

    # ------------------------------------------------------------------ 列表装载
    def _reload_list(self, select_id: str | None = None, *, force: bool = False) -> None:
        if self._dirty and not force:
            return
        project = getattr(self.ctx, "current_project", None)
        if project is None:
            self._characters = []
            self._by_id = {}
            self._stack.setCurrentWidget(self._empty_page)
            return
        self._project_id = project.id

        snapshot = getattr(self.ctx, "snapshot", None)
        if snapshot is None:
            try:
                snapshot = self.ctx.refresh_snapshot()
            except Exception:
                logger.exception("刷新项目快照失败")
                snapshot = None
        characters = list(getattr(snapshot, "characters", []) or [])
        characters.sort(
            key=lambda c: (
                int(getattr(c, "sort_order", 0) or 0),
                getattr(c, "created_at", "") or "",
            )
        )
        self._characters = characters
        self._by_id = {c.id: c for c in characters}

        target_id = select_id or self._current_id
        self._populate_list(target_id)
        self._stack.setCurrentWidget(self._content)
        self._update_actions()

    def _populate_list(self, target_id: str | None) -> None:
        row = -1
        self._loading = True
        try:
            self.list_widget.clear()
            for character in self._characters:
                item = QListWidgetItem(self._item_text(character))
                item.setData(Qt.ItemDataRole.UserRole, character.id)
                tip = (character.personality or character.current_state or "").strip()
                if tip:
                    item.setToolTip(tip)
                self.list_widget.addItem(item)
            self._apply_filter()
            row = self._row_for_id(target_id)
            if row < 0 and self.list_widget.count() > 0:
                row = 0
            if row >= 0:
                self.list_widget.setCurrentRow(row)
        finally:
            self._loading = False
        self._update_count()
        if row >= 0:
            self._load_character(self._row_char_id(row))
        else:
            self._clear_editor()

    def _item_text(self, character: Character) -> str:
        role = (character.role or "未设定定位").strip()
        text = f"{character.name}  ·  {role}"
        if not character.is_active:
            text += "  ·  未激活"
        return text

    def _update_count(self) -> None:
        total = len(self._characters)
        visible = sum(
            1
            for row in range(self.list_widget.count())
            if not self.list_widget.item(row).isHidden()
        )
        if visible != total:
            self.count_label.setText(f"{visible} / {total} 个角色")
        else:
            self.count_label.setText(f"{total} 个角色")

    def _row_for_id(self, character_id: str | None) -> int:
        if not character_id:
            return -1
        for row in range(self.list_widget.count()):
            if self.list_widget.item(row).data(Qt.ItemDataRole.UserRole) == character_id:
                return row
        return -1

    def _row_char_id(self, row: int) -> str | None:
        if row < 0 or row >= self.list_widget.count():
            return None
        value = self.list_widget.item(row).data(Qt.ItemDataRole.UserRole)
        return str(value) if value else None

    def _apply_filter(self) -> None:
        text = self.search_edit.text().strip().lower()
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            character = self._by_id.get(str(item.data(Qt.ItemDataRole.UserRole) or ""))
            haystack = ""
            if character is not None:
                haystack = f"{character.name} {character.role} {character.current_state}".lower()
            item.setHidden(bool(text) and text not in haystack)
        self._update_count()

    # ------------------------------------------------------------------ 选择
    def _on_row_changed(self, row: int) -> None:
        if self._loading:
            return
        character_id = self._row_char_id(row)
        if character_id == self._current_id:
            return
        if character_id is None:
            self._clear_editor()
            return
        if self._dirty and self._current_id and not self._confirm_discard():
            self._restore_selection()
            return
        self._dirty = False
        self._load_character(character_id)

    def _confirm_discard(self) -> bool:
        name = self._current_character.name if self._current_character else "当前角色"
        return ConfirmDialog.ask(
            self,
            "放弃未保存的修改？",
            f"角色「{name}」有未保存的修改，切换后将丢失。",
            confirm_text="放弃修改",
            danger=True,
        )

    def _restore_selection(self) -> None:
        row = self._row_for_id(self._current_id)
        self.list_widget.blockSignals(True)
        try:
            self.list_widget.setCurrentRow(row)
        finally:
            self.list_widget.blockSignals(False)

    def _load_character(self, character_id: str | None) -> None:
        character = self._by_id.get(character_id or "")
        if character is None:
            self._clear_editor()
            return
        self._loading = True
        try:
            self._current_id = character.id
            self._current_character = character
            self.name_edit.setText(character.name or "")
            self.role_edit.setText(character.role or "")
            self.age_edit.setText(character.age or "")
            self.appearance_edit.setPlainText(character.appearance or "")
            self.personality_edit.setPlainText(character.personality or "")
            self.background_edit.setPlainText(character.background or "")
            self.goals_edit.setPlainText(character.goals or "")
            self.arc_edit.setPlainText(character.arc or "")
            self.current_state_edit.setPlainText(character.current_state or "")
            self.notes_edit.setPlainText(character.notes or "")
            self.active_check.setChecked(bool(character.is_active))
            self.relationship_table.setRowCount(0)
            for rel in character.relationships or []:
                if not isinstance(rel, dict):
                    continue
                self._append_relationship_row(
                    str(rel.get("target", "")),
                    str(rel.get("relation", "")),
                    str(rel.get("note", "")),
                )
        finally:
            self._loading = False
        self._dirty = False
        self._editor_stack.setCurrentWidget(self._editor_scroll)
        self._update_actions()
        self._update_header_subtitle()

    def _clear_editor(self) -> None:
        self._current_id = None
        self._current_character = None
        self._dirty = False
        self._editor_stack.setCurrentWidget(self._placeholder_page)
        self._update_actions()
        self.header.set_subtitle(self.page_subtitle)

    def _update_actions(self) -> None:
        has_character = self._current_character is not None
        self.save_button.setEnabled(has_character and self._dirty)
        self.delete_button.setEnabled(has_character)

    def _update_header_subtitle(self) -> None:
        character = self._current_character
        base = self.page_subtitle
        if character is None:
            self.header.set_subtitle(base)
            return
        parts = [character.name or "未命名角色"]
        if character.role.strip():
            parts.append(character.role.strip())
        if not character.is_active:
            parts.append("未激活")
        subtitle = " · ".join(parts)
        if self._dirty:
            subtitle = f"● 有未保存的修改 · {subtitle}"
        self.header.set_subtitle(subtitle)

    # ------------------------------------------------------------------ 编辑
    def _mark_dirty(self, *_args: object) -> None:
        if self._loading or self._current_character is None:
            return
        if self._dirty:
            return
        self._dirty = True
        self._update_actions()
        self._update_header_subtitle()

    def _append_relationship_row(self, target: str = "", relation: str = "", note: str = "") -> None:
        row = self.relationship_table.rowCount()
        self.relationship_table.insertRow(row)
        for column, value in enumerate((target, relation, note)):
            self.relationship_table.setItem(row, column, QTableWidgetItem(value))

    def _add_relationship_row(self) -> None:
        if self._current_character is None:
            return
        self._append_relationship_row()
        last_row = self.relationship_table.rowCount() - 1
        self.relationship_table.setCurrentCell(last_row, 0)
        self.relationship_table.editItem(self.relationship_table.item(last_row, 0))
        self._mark_dirty()

    def _remove_relationship_row(self) -> None:
        if self._current_character is None:
            return
        row = self.relationship_table.currentRow()
        if row < 0:
            row = self.relationship_table.rowCount() - 1
        if row < 0:
            return
        self.relationship_table.removeRow(row)
        self._mark_dirty()

    def _read_relationships(self) -> list[dict[str, str]]:
        relationships: list[dict[str, str]] = []
        for row in range(self.relationship_table.rowCount()):
            values: list[str] = []
            for column in range(3):
                item = self.relationship_table.item(row, column)
                values.append(item.text().strip() if item is not None else "")
            target, relation, note = values
            if not target and not relation and not note:
                continue
            relationships.append(
                {"target": target, "relation": relation, "note": note}
            )
        return relationships

    def _apply_form(self, character: Character) -> bool:
        name = self.name_edit.text().strip()
        if not name:
            self._notify("角色姓名不能为空。", "warning")
            self.name_edit.setFocus()
            return False
        character.name = name
        character.role = self.role_edit.text().strip()
        character.age = self.age_edit.text().strip()
        character.appearance = self.appearance_edit.toPlainText().strip()
        character.personality = self.personality_edit.toPlainText().strip()
        character.background = self.background_edit.toPlainText().strip()
        character.goals = self.goals_edit.toPlainText().strip()
        character.arc = self.arc_edit.toPlainText().strip()
        character.current_state = self.current_state_edit.toPlainText().strip()
        character.notes = self.notes_edit.toPlainText().strip()
        character.is_active = self.active_check.isChecked()
        character.relationships = self._read_relationships()
        return True

    # ------------------------------------------------------------------ 动作
    def _create_character(self) -> None:
        project = getattr(self.ctx, "current_project", None)
        service = getattr(self.ctx, "project_service", None)
        if project is None:
            self._notify("请先在项目工坊打开一个项目。", "warning")
            return
        if service is None:
            self._notify("项目服务尚未就绪，无法创建角色。", "error")
            return
        if self._dirty and not self._confirm_discard():
            return
        try:
            character = service.create_character(project.id, name="新角色")
        except Exception as exc:
            logger.exception("创建角色失败")
            self._notify(f"创建角色失败：{exc}", "error")
            return
        if character is None:
            self._notify("创建角色失败：服务未返回角色。", "error")
            return

        self._dirty = False
        self._refresh_snapshot_quietly()
        self._reload_list(select_id=character.id, force=True)
        self.name_edit.setFocus()
        self.name_edit.selectAll()
        self._notify("已新建角色，编辑后点击「保存」。", "success")
        self._emit_data_changed("characters")

    def _save_character(self) -> None:
        character = self._current_character
        service = getattr(self.ctx, "project_service", None)
        if character is None:
            self._notify("请先选择或新建一个角色。", "warning")
            return
        if service is None:
            self._notify("项目服务尚未就绪，无法保存。", "error")
            return
        if not self._apply_form(character):
            return

        character.updated_at = utc_now()
        try:
            saved = service.save_character(character)
        except Exception as exc:
            logger.exception("保存角色失败")
            self._notify(f"保存角色失败：{exc}", "error")
            return
        self._current_character = saved or character
        self._dirty = False
        self._refresh_snapshot_quietly()
        self._reload_list(select_id=self._current_character.id, force=True)
        self._notify("角色已保存。", "success")
        self._emit_data_changed("characters")

    def save_current(self) -> None:
        """公开保存入口（供 MainWindow 的全局保存快捷键调用）。"""

        self._save_character()

    def _delete_character(self) -> None:
        character = self._current_character
        service = getattr(self.ctx, "project_service", None)
        if character is None:
            return
        if service is None:
            self._notify("项目服务尚未就绪，无法删除。", "error")
            return
        confirmed = ConfirmDialog.ask(
            self,
            "删除角色",
            f"确定删除角色「{character.name}」吗？该角色的档案与关系会一并删除，且无法恢复。",
            confirm_text="删除角色",
            danger=True,
        )
        if not confirmed:
            return
        try:
            service.delete_character(character.id)
        except Exception as exc:
            logger.exception("删除角色失败")
            self._notify(f"删除角色失败：{exc}", "error")
            return

        self._current_id = None
        self._current_character = None
        self._dirty = False
        self._refresh_snapshot_quietly()
        self._reload_list(force=True)
        self._notify("角色已删除。", "success")
        self._emit_data_changed("characters")

    # ------------------------------------------------------------------ 工具
    def _refresh_snapshot_quietly(self) -> None:
        refresh = getattr(self.ctx, "refresh_snapshot", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                logger.exception("刷新项目快照失败")

    def _notify(self, message: str, level: str = "info") -> None:
        notify = getattr(self.ctx, "notify", None)
        if callable(notify):
            notify(message, level)

    def _emit_data_changed(self, domain: str) -> None:
        signal = getattr(self.ctx, "dataChanged", None)
        if signal is not None:
            try:
                signal.emit(domain)
            except Exception:  # pragma: no cover - 防御 stub ctx
                logger.debug("dataChanged 信号不可用", exc_info=True)
