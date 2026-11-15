"""故事大纲页面。

左侧 ``QTreeWidget`` 展示「卷 → 章 → 节拍」三级结构（来自
``AppContext.snapshot.outline``，按 ``order_index`` 排序），右侧是可滚动编辑表单：
标题 / 类型 / 状态 / 摘要 / 目标 / 冲突。

支持：
- 新建卷、章、节拍；
- 上移 / 下移（重新计算同级 ``order_index`` 后调用 ``save_outline_node``）；
- 删除节点（连同子树，先删子节点再删父节点）；
- 「在章节编辑器中打开/创建章节」：按 ``outline_id`` 查找章节，找不到则
  调用 ``create_chapter`` 建立关联章节，然后 ``ctx.navigate("editor", ...)``。

页面在无项目、无大纲、服务异常时都只显示空状态，不抛异常。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from novel_ai.domain.models import OutlineNode, utc_now
from novel_ai.ui.icons import icon
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

KIND_LABELS = {"volume": "卷", "chapter": "章", "beat": "节拍"}
STATUS_LABELS = {"planned": "计划中", "drafting": "写作中", "done": "已完成"}
KIND_ICONS = {"volume": "folder", "chapter": "book", "beat": "sparkles"}


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
    grid.setColumnMinimumWidth(0, 74)
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
    caption.setMinimumWidth(68)
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


class OutlinePage(BasePage):
    """故事大纲页：结构树 + 节点编辑器。"""

    page_key = "outline"
    page_title = "故事大纲"
    page_subtitle = "从卷到章再到节拍，搭建清晰的故事骨架，AI 会按大纲逐章推进。"

    def __init__(self, ctx: Any, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self._nodes: dict[str, OutlineNode] = {}
        self._items: dict[str, QTreeWidgetItem] = {}
        self._current_id: str | None = None
        self._current_node: OutlineNode | None = None
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
            "大纲需要依附于一个项目。请先到项目工坊打开或创建项目。",
            icon_name="outline",
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

        # ---------------------------------------------------------- 页头
        self.header = PageHeader(self.page_title, self.page_subtitle)
        self.new_volume_button = tool_button(
            "新建卷", "plus", variant="secondary", tooltip="在顶层新建一卷"
        )
        self.new_volume_button.clicked.connect(self._create_volume)
        self.new_chapter_button = tool_button(
            "新建章", "plus", variant="secondary", tooltip="在选中卷下或同级新建一章"
        )
        self.new_chapter_button.clicked.connect(self._create_chapter)
        self.new_beat_button = tool_button(
            "新建节拍", "plus", variant="secondary", tooltip="在选中章节下新增一个节拍"
        )
        self.new_beat_button.clicked.connect(self._create_beat)
        self.save_button = QPushButton("保存节点")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(lambda: self._save_current(silent=False))
        self.header.add_action(self.new_volume_button)
        self.header.add_action(self.new_chapter_button)
        self.header.add_action(self.new_beat_button)
        self.header.add_action(self.save_button)
        layout.addWidget(self.header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)

        # ---------------------------------------------------------- 左侧结构树
        tree_card = Card(padding=14, spacing=10)
        tree_card.setMinimumWidth(260)
        tree_card.setMaximumWidth(430)
        tree_header = QHBoxLayout()
        tree_header.setSpacing(8)
        tree_title = QLabel("故事结构")
        tree_title.setObjectName("SectionTitle")
        tree_header.addWidget(tree_title)
        tree_header.addStretch(1)
        self.count_label = QLabel("0 个节点")
        self.count_label.setProperty("role", "caption")
        tree_header.addWidget(self.count_label)
        tree_card.content_layout.addLayout(tree_header)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["结构", "状态"])
        self.tree.setColumnCount(2)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(16)
        self.tree.setAnimated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        tree_header_view = self.tree.header()
        tree_header_view.setStretchLastSection(False)
        tree_header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        tree_header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        tree_header_view.resizeSection(1, 74)
        self.tree.currentItemChanged.connect(self._on_tree_selection_changed)
        tree_card.content_layout.addWidget(self.tree, 1)
        splitter.addWidget(tree_card)

        # ---------------------------------------------------------- 右侧编辑器
        self._editor_stack = QStackedWidget()
        self._editor_stack.setMinimumWidth(430)

        self._placeholder_page = QWidget()
        self._placeholder_layout = QVBoxLayout(self._placeholder_page)
        self._placeholder_layout.setContentsMargins(16, 24, 16, 24)
        self._editor_stack.addWidget(self._placeholder_page)

        # 编辑器页 = 常驻操作栏 + 可滚动的节点表单
        self._editor_page = QWidget()
        editor_layout = QVBoxLayout(self._editor_page)
        editor_layout.setContentsMargins(2, 0, 8, 0)
        editor_layout.setSpacing(8)
        editor_layout.addWidget(self._build_editor_actions())
        self._editor_scroll = self._build_editor()
        editor_layout.addWidget(self._editor_scroll, 1)
        self._editor_stack.addWidget(self._editor_page)
        splitter.addWidget(self._editor_stack)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 900])
        layout.addWidget(splitter, 1)
        return content

    def _build_editor_actions(self) -> QWidget:
        """常驻操作栏：打开正文章节、调整顺序、删除节点。"""

        bar = QWidget()
        action_row = QHBoxLayout(bar)
        action_row.setContentsMargins(0, 2, 0, 4)
        action_row.setSpacing(8)
        self.open_chapter_button = tool_button(
            "在章节编辑器中打开", "editor", variant="secondary"
        )
        self.open_chapter_button.setToolTip("根据当前章节节点查找或创建正文章节并打开编辑器")
        self.open_chapter_button.clicked.connect(self._open_chapter)
        action_row.addWidget(self.open_chapter_button)
        action_row.addStretch(1)
        self.move_up_button = tool_button("上移", "arrow-up", variant="ghost", tooltip="在同级中上移")
        self.move_up_button.clicked.connect(lambda: self._move_current(-1))
        self.move_down_button = tool_button("下移", "arrow-down", variant="ghost", tooltip="在同级中下移")
        self.move_down_button.clicked.connect(lambda: self._move_current(1))
        self.delete_button = tool_button("删除", "trash", variant="danger", tooltip="删除节点及其子节点")
        self.delete_button.clicked.connect(self._delete_current)
        action_row.addWidget(self.move_up_button)
        action_row.addWidget(self.move_down_button)
        action_row.addWidget(self.delete_button)
        return bar

    def _build_editor(self) -> ScrollPage:
        scroll = ScrollPage(margins=(2, 0, 8, 10), spacing=14)
        node_card, node_grid = _section_card(
            "节点编辑", "卷用于分卷，章对应正文章节，节拍是章节内部的情节单元。"
        )
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("节点标题")
        self.title_edit.setClearButtonEnabled(True)
        self.kind_combo = QComboBox()
        for label, value in (("卷", "volume"), ("章", "chapter"), ("节拍", "beat")):
            self.kind_combo.addItem(label, value)
        self.status_combo = QComboBox()
        for label, value in (
            ("计划中", "planned"),
            ("写作中", "drafting"),
            ("已完成", "done"),
        ):
            self.status_combo.addItem(label, value)
        self.summary_edit = QPlainTextEdit()
        self.summary_edit.setPlaceholderText("这一章 / 节拍发生了什么？主要事件、转折与结果。")
        self.summary_edit.setMinimumHeight(110)
        self.goals_edit = QPlainTextEdit()
        self.goals_edit.setPlaceholderText("本节点要完成的叙事目标：推进哪条线、揭示什么信息。")
        self.goals_edit.setMinimumHeight(76)
        self.conflicts_edit = QPlainTextEdit()
        self.conflicts_edit.setPlaceholderText("核心冲突：谁和谁对抗？阻碍、代价与悬念是什么？")
        self.conflicts_edit.setMinimumHeight(76)

        row = 0
        row = _add_row(node_grid, row, "标题", self.title_edit, "保存前不能为空。")
        row = _add_row(node_grid, row, "类型", self.kind_combo)
        row = _add_row(node_grid, row, "状态", self.status_combo)
        row = _add_row(node_grid, row, "摘要", self.summary_edit)
        row = _add_row(node_grid, row, "目标", self.goals_edit)
        _add_row(node_grid, row, "冲突", self.conflicts_edit)

        scroll.add(node_card)
        scroll.body_layout.addStretch(1)

        # 编辑信号 -> 脏标记（填充表单时由 _loading 闸门拦截）。
        self.title_edit.textEdited.connect(self._mark_dirty)
        self.kind_combo.currentIndexChanged.connect(self._mark_dirty)
        self.status_combo.currentIndexChanged.connect(self._mark_dirty)
        for plain in (self.summary_edit, self.goals_edit, self.conflicts_edit):
            plain.textChanged.connect(self._mark_dirty)
        self.kind_combo.currentIndexChanged.connect(lambda *_: self._update_actions())
        return scroll

    # ------------------------------------------------------------------ 生命周期
    def on_project_changed(self, project: Any) -> None:
        if project is None:
            self._project_id = None
            self._current_id = None
            self._current_node = None
            self._dirty = False
            self._nodes = {}
            self._items = {}
            self.tree.clear()
            self._stack.setCurrentWidget(self._empty_page)
            return
        if self._project_id != project.id:
            self._project_id = project.id
            self._current_id = None
            self._current_node = None
            self._dirty = False
        if not self._dirty:
            self._rebuild_tree(force=True)

    def on_navigated(self, payload: object = None) -> None:
        outline_id = payload.get("outline_id") if isinstance(payload, dict) else None
        if outline_id and not self._dirty:
            self._rebuild_tree(select_id=str(outline_id), force=True)
        else:
            self.refresh()

    def refresh(self) -> None:
        if self._dirty:
            return
        self._refresh_snapshot_quietly()
        self._rebuild_tree(select_id=self._current_id)

    # ------------------------------------------------------------------ 树构建
    def _rebuild_tree(self, select_id: str | None = None, *, force: bool = False) -> None:
        if self._dirty and not force:
            return
        project = getattr(self.ctx, "current_project", None)
        if project is None:
            self._nodes = {}
            self._items = {}
            self.tree.clear()
            self._current_id = None
            self._current_node = None
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
        nodes = list(getattr(snapshot, "outline", []) or [])
        self._nodes = {node.id: node for node in nodes}
        self._stack.setCurrentWidget(self._content)
        self._build_tree_items(nodes)

        target_id = select_id or self._current_id
        item = self._items.get(target_id or "")
        if item is None and self.tree.topLevelItemCount() > 0:
            item = self.tree.topLevelItem(0)

        self._loading = True
        try:
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(item)
            self.tree.blockSignals(False)
        finally:
            self._loading = False

        if item is not None:
            node_id = item.data(0, Qt.ItemDataRole.UserRole)
            self._load_node(str(node_id) if node_id else None)
        else:
            self._current_id = None
            self._current_node = None
            self._dirty = False
            self._editor_stack.setCurrentWidget(self._placeholder_page)
            self._update_actions()
            self.header.set_subtitle(self.page_subtitle)
            if not nodes:
                self._set_placeholder(
                    "故事结构还是空的",
                    "先创建第一卷，再在卷下添加章节与节拍，逐步搭起故事骨架。",
                    action_text="新建第一卷",
                    action=self._create_volume,
                )
            else:
                self._set_placeholder(
                    "选择一个节点",
                    "在左侧选择一个卷 / 章 / 节拍开始编辑。",
                )

    def _build_tree_items(self, nodes: list[OutlineNode]) -> None:
        valid_ids = {node.id for node in nodes}
        children: dict[str | None, list[OutlineNode]] = {}
        for node in nodes:
            parent_id = node.parent_id if node.parent_id in valid_ids else None
            children.setdefault(parent_id, []).append(node)
        for group in children.values():
            group.sort(key=lambda n: (int(n.order_index or 0), n.created_at or ""))

        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            self._items = {}
            visited: set[str] = set()

            def add_children(parent_item: QTreeWidgetItem | None, parent_id: str | None, depth: int) -> None:
                if depth > 16:
                    return
                for node in children.get(parent_id, []):
                    if node.id in visited:
                        continue
                    visited.add(node.id)
                    status_text = STATUS_LABELS.get(node.status, node.status or "")
                    item = QTreeWidgetItem([node.title or "(未命名)", status_text])
                    item.setData(0, Qt.ItemDataRole.UserRole, node.id)
                    item.setIcon(0, _kind_icon(node.kind))
                    if node.summary:
                        item.setToolTip(0, node.summary)
                    if node.status == "done":
                        item.setForeground(1, QBrush(Qt.GlobalColor.gray))
                    if parent_item is None:
                        self.tree.addTopLevelItem(item)
                    else:
                        parent_item.addChild(item)
                    self._items[node.id] = item
                    add_children(item, node.id, depth + 1)

            add_children(None, None, 0)
        finally:
            self.tree.blockSignals(False)

        if len(nodes) <= 150:
            self.tree.expandAll()
        else:
            for index in range(self.tree.topLevelItemCount()):
                self.tree.topLevelItem(index).setExpanded(True)
        self.count_label.setText(f"{len(nodes)} 个节点")

    # ------------------------------------------------------------------ 选择 / 装载
    def _on_tree_selection_changed(
        self,
        _current: QTreeWidgetItem | None,
        _previous: QTreeWidgetItem | None,
    ) -> None:
        if self._loading:
            return
        item = self.tree.currentItem()
        node_id = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        node_id = str(node_id) if node_id else None
        if node_id == self._current_id:
            return
        if node_id is None:
            self._current_id = None
            self._current_node = None
            self._dirty = False
            self._editor_stack.setCurrentWidget(self._placeholder_page)
            self._update_actions()
            self.header.set_subtitle(self.page_subtitle)
            self._set_placeholder("选择一个节点", "在左侧选择一个卷 / 章 / 节拍开始编辑。")
            return
        if self._dirty and self._current_id and not self._confirm_discard():
            self._restore_tree_selection()
            return
        self._dirty = False
        self._load_node(node_id)

    def _confirm_discard(self) -> bool:
        title = self._current_node.title if self._current_node else "当前节点"
        return ConfirmDialog.ask(
            self,
            "放弃未保存的修改？",
            f"大纲节点「{title}」有未保存的修改，切换后将丢失。",
            confirm_text="放弃修改",
            danger=True,
        )

    def _restore_tree_selection(self) -> None:
        item = self._items.get(self._current_id or "")
        if item is None:
            return
        self.tree.blockSignals(True)
        try:
            self.tree.setCurrentItem(item)
        finally:
            self.tree.blockSignals(False)

    def _load_node(self, node_id: str | None) -> None:
        node = self._nodes.get(node_id or "")
        if node is None:
            self._current_id = None
            self._current_node = None
            self._editor_stack.setCurrentWidget(self._placeholder_page)
            self._update_actions()
            return
        self._current_id = node.id
        self._current_node = node
        self._loading = True
        try:
            self.title_edit.setText(node.title or "")
            self._select_combo_data(self.kind_combo, node.kind or "chapter")
            self._select_combo_data(self.status_combo, node.status or "planned")
            self.summary_edit.setPlainText(node.summary or "")
            self.goals_edit.setPlainText(node.goals or "")
            self.conflicts_edit.setPlainText(node.conflicts or "")
        finally:
            self._loading = False
        self._dirty = False
        self._editor_stack.setCurrentWidget(self._editor_page)
        self._update_actions()
        self._update_header_subtitle()

    @staticmethod
    def _select_combo_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _set_placeholder(
        self,
        title: str,
        description: str,
        *,
        action_text: str = "",
        action: Any = None,
    ) -> None:
        while self._placeholder_layout.count():
            item = self._placeholder_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        empty = EmptyState(
            title,
            description,
            icon_name="outline",
            action_text=action_text,
        )
        if action is not None and empty.action_button is not None:
            empty.actionClicked.connect(action)
        self._placeholder_layout.addWidget(empty, 1)

    # ------------------------------------------------------------------ 编辑
    def _mark_dirty(self, *_args: object) -> None:
        if self._loading or self._current_node is None:
            return
        if self._dirty:
            return
        self._dirty = True
        self._update_actions()
        self._update_header_subtitle()

    def _update_actions(self) -> None:
        has_node = self._current_node is not None
        self.save_button.setEnabled(has_node and self._dirty)
        self.delete_button.setEnabled(has_node)
        self.move_up_button.setEnabled(has_node)
        self.move_down_button.setEnabled(has_node)
        self.open_chapter_button.setEnabled(self._chapter_node_for_open() is not None)

    def _update_header_subtitle(self) -> None:
        node = self._current_node
        if node is None:
            self.header.set_subtitle(self.page_subtitle)
            return
        path = " / ".join(self._node_path(node))
        subtitle = f"{KIND_LABELS.get(node.kind, '节点')} · {path}"
        if self._dirty:
            subtitle = f"● 有未保存的修改 · {subtitle}"
        self.header.set_subtitle(subtitle)

    def _node_path(self, node: OutlineNode) -> list[str]:
        parts = [node.title or "(未命名)"]
        current = node
        depth = 0
        while depth < 8:
            parent = self._nodes.get(current.parent_id or "")
            if parent is None:
                break
            parts.append(parent.title or "(未命名)")
            current = parent
            depth += 1
        parts.reverse()
        return parts

    def _apply_form(self, node: OutlineNode) -> bool:
        title = self.title_edit.text().strip()
        if not title:
            self._notify("节点标题不能为空。", "warning")
            self.title_edit.setFocus()
            return False
        node.title = title
        kind = self.kind_combo.currentData()
        if kind:
            node.kind = str(kind)
        status = self.status_combo.currentData()
        if status:
            node.status = str(status)
        node.summary = self.summary_edit.toPlainText().strip()
        node.goals = self.goals_edit.toPlainText().strip()
        node.conflicts = self.conflicts_edit.toPlainText().strip()
        node.updated_at = utc_now()
        return True

    def _save_current(self, silent: bool = False) -> bool:
        node = self._current_node
        service = getattr(self.ctx, "project_service", None)
        if node is None:
            if not silent:
                self._notify("请先选择一个节点。", "warning")
            return False
        if service is None:
            if not silent:
                self._notify("项目服务尚未就绪，无法保存。", "error")
            return False
        if not self._apply_form(node):
            return False
        try:
            saved = service.save_outline_node(node)
        except Exception as exc:
            logger.exception("保存大纲节点失败")
            self._notify(f"保存失败：{exc}", "error")
            return False
        self._current_node = saved or node
        self._dirty = False
        self._refresh_snapshot_quietly()
        self._rebuild_tree(select_id=self._current_node.id, force=True)
        if not silent:
            self._notify("大纲节点已保存。", "success")
        self._emit_data_changed("outline")
        return True

    def save_current(self) -> None:
        """公开保存入口（供 MainWindow 的全局保存快捷键调用）。"""

        self._save_current(silent=False)

    def _flush_current(self) -> bool:
        """当前节点有未保存修改时先保存；用于新增 / 移动 / 跳转前。"""

        if not self._dirty:
            return True
        return self._save_current(silent=True)

    # ------------------------------------------------------------------ 新增
    def _create_volume(self) -> None:
        project = getattr(self.ctx, "current_project", None)
        service = getattr(self.ctx, "project_service", None)
        if project is None:
            self._notify("请先在项目工坊打开一个项目。", "warning")
            return
        if service is None:
            self._notify("项目服务尚未就绪，无法新建卷。", "error")
            return
        if not self._flush_current():
            return
        try:
            node = service.create_volume(project.id, title="新卷")
        except Exception as exc:
            logger.exception("新建卷失败")
            self._notify(f"新建卷失败：{exc}", "error")
            return
        if node is None:
            self._notify("新建卷失败：服务未返回节点。", "error")
            return
        self._after_mutation(node.id, "已新建卷，编辑标题后记得保存。")
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def _create_chapter(self) -> None:
        project = getattr(self.ctx, "current_project", None)
        service = getattr(self.ctx, "project_service", None)
        if project is None:
            self._notify("请先在项目工坊打开一个项目。", "warning")
            return
        if service is None:
            self._notify("项目服务尚未就绪，无法新建章节。", "error")
            return
        if not self._flush_current():
            return
        parent_id = self._chapter_parent_id()
        try:
            node = service.create_chapter_outline(project.id, parent_id, title="新章节")
        except Exception as exc:
            logger.exception("新建章节大纲失败")
            self._notify(f"新建章节失败：{exc}", "error")
            return
        if node is None:
            self._notify("新建章节失败：服务未返回节点。", "error")
            return
        self._after_mutation(node.id, "已新建章节大纲。")
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def _create_beat(self) -> None:
        project = getattr(self.ctx, "current_project", None)
        service = getattr(self.ctx, "project_service", None)
        if project is None:
            self._notify("请先在项目工坊打开一个项目。", "warning")
            return
        if service is None:
            self._notify("项目服务尚未就绪，无法新建节拍。", "error")
            return
        chapter = self._chapter_node_for_beat()
        if chapter is None:
            self._notify("请先选择一个章节节点（或在卷下先创建章节）。", "warning")
            return
        if not self._flush_current():
            return
        chapter = self._nodes.get(chapter.id) or chapter
        siblings = self._siblings(chapter)
        next_order = max((int(n.order_index or 0) for n in siblings), default=-1) + 1
        node = OutlineNode.create(
            project.id,
            "新节拍",
            kind="beat",
            parent_id=chapter.id,
            order_index=next_order,
        )
        try:
            saved = service.save_outline_node(node)
        except Exception as exc:
            logger.exception("新建节拍失败")
            self._notify(f"新建节拍失败：{exc}", "error")
            return
        node = saved or node
        self._after_mutation(node.id, "已新建节拍。")
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def _chapter_parent_id(self) -> str | None:
        node = self._current_node
        if node is None:
            return None
        if node.kind == "volume":
            return node.id
        if node.kind == "chapter":
            parent = self._nodes.get(node.parent_id or "")
            return parent.id if parent is not None and parent.kind == "volume" else None
        if node.kind == "beat":
            parent = self._nodes.get(node.parent_id or "")
            if parent is not None and parent.kind == "chapter":
                grand = self._nodes.get(parent.parent_id or "")
                return grand.id if grand is not None and grand.kind == "volume" else None
            if parent is not None and parent.kind == "volume":
                return parent.id
        return None

    def _chapter_node_for_beat(self) -> OutlineNode | None:
        node = self._current_node
        if node is None:
            return None
        if node.kind == "chapter":
            return node
        if node.kind == "beat":
            parent = self._nodes.get(node.parent_id or "")
            return parent if parent is not None and parent.kind == "chapter" else None
        if node.kind == "volume":
            chapters = sorted(
                (
                    candidate
                    for candidate in self._nodes.values()
                    if self._effective_parent(candidate) == node.id
                    and candidate.kind == "chapter"
                ),
                key=lambda candidate: (int(candidate.order_index or 0), candidate.created_at or ""),
            )
            return chapters[-1] if chapters else None
        return None

    def _chapter_node_for_open(self) -> OutlineNode | None:
        node = self._current_node
        if node is None:
            return None
        if node.kind == "chapter":
            return node
        if node.kind == "beat":
            current = node
            depth = 0
            while depth < 8:
                parent = self._nodes.get(current.parent_id or "")
                if parent is None:
                    return None
                if parent.kind == "chapter":
                    return parent
                current = parent
                depth += 1
        return None

    # ------------------------------------------------------------------ 排序 / 删除
    def _effective_parent(self, node: OutlineNode) -> str | None:
        return node.parent_id if node.parent_id in self._nodes else None

    def _siblings(self, node: OutlineNode) -> list[OutlineNode]:
        parent_id = self._effective_parent(node)
        return sorted(
            (
                candidate
                for candidate in self._nodes.values()
                if self._effective_parent(candidate) == parent_id
            ),
            key=lambda candidate: (int(candidate.order_index or 0), candidate.created_at or ""),
        )

    def _move_current(self, delta: int) -> None:
        node = self._current_node
        service = getattr(self.ctx, "project_service", None)
        if node is None or service is None:
            return
        if not self._flush_current():
            return
        node = self._current_node
        if node is None:
            return
        siblings = self._siblings(node)
        order = [sibling.id for sibling in siblings]
        try:
            index = order.index(node.id)
        except ValueError:
            return
        target = index + delta
        if target < 0:
            self._notify("已经是最前面的节点了。", "info")
            return
        if target >= len(order):
            self._notify("已经是最后面的节点了。", "info")
            return
        order[index], order[target] = order[target], order[index]

        changed: list[OutlineNode] = []
        for position, node_id in enumerate(order):
            sibling = self._nodes.get(node_id)
            if sibling is None:
                continue
            if int(sibling.order_index or 0) != position:
                sibling.order_index = position
                sibling.updated_at = utc_now()
                changed.append(sibling)
        if not changed:
            return
        try:
            for sibling in changed:
                service.save_outline_node(sibling)
        except Exception as exc:
            logger.exception("调整大纲顺序失败")
            self._notify(f"调整顺序失败：{exc}", "error")
            return
        self._after_mutation(node.id, "已调整节点顺序。")

    def _delete_current(self) -> None:
        node = self._current_node
        service = getattr(self.ctx, "project_service", None)
        if node is None:
            return
        if service is None:
            self._notify("项目服务尚未就绪，无法删除。", "error")
            return
        subtree_ids = self._postorder_ids(node.id)
        descendants = max(0, len(subtree_ids) - 1)
        message = f"确定删除{KIND_LABELS.get(node.kind, '节点')}「{node.title}」吗？"
        if descendants:
            message += f"\n其下 {descendants} 个子节点也会一并删除。"
        if not ConfirmDialog.ask(
            self,
            "删除大纲节点",
            message,
            confirm_text="删除节点",
            danger=True,
        ):
            return
        parent_id = self._effective_parent(node)
        try:
            for node_id in subtree_ids:
                service.delete_outline_node(node_id)
        except Exception as exc:
            logger.exception("删除大纲节点失败")
            self._notify(f"删除失败：{exc}", "error")
            self._refresh_snapshot_quietly()
            self._rebuild_tree(force=True)
            return

        self._current_id = parent_id
        self._current_node = None
        self._dirty = False
        self._refresh_snapshot_quietly()
        self._rebuild_tree(select_id=parent_id, force=True)
        self._notify("节点已删除。", "success")
        self._emit_data_changed("outline")

    def _postorder_ids(self, root_id: str) -> list[str]:
        result: list[str] = []

        def walk(node_id: str, depth: int) -> None:
            if depth > 16:
                result.append(node_id)
                return
            children = [
                candidate
                for candidate in self._nodes.values()
                if self._effective_parent(candidate) == node_id
            ]
            children.sort(
                key=lambda candidate: (int(candidate.order_index or 0), candidate.created_at or "")
            )
            for child in children:
                walk(child.id, depth + 1)
            result.append(node_id)

        walk(root_id, 0)
        return result

    # ------------------------------------------------------------------ 打开章节
    def _open_chapter(self) -> None:
        project = getattr(self.ctx, "current_project", None)
        service = getattr(self.ctx, "project_service", None)
        chapter_node = self._chapter_node_for_open()
        if project is None or chapter_node is None:
            self._notify("请选择一个章节节点；节拍会归属到它所在的章节。", "warning")
            return
        node_id = chapter_node.id
        if not self._flush_current():
            return
        chapter_node = self._nodes.get(node_id)
        if chapter_node is None:
            self._notify("章节节点已不存在，请刷新后重试。", "warning")
            return

        chapter = None
        snapshot = getattr(self.ctx, "snapshot", None)
        if snapshot is not None:
            chapter = snapshot.chapter_for_outline(chapter_node.id)
        if chapter is None and service is not None:
            try:
                chapters = list(service.list_chapters(project.id) or [])
            except Exception:
                logger.exception("读取章节列表失败")
                chapters = []
            chapter = next(
                (item for item in chapters if getattr(item, "outline_id", None) == chapter_node.id),
                None,
            )

        if chapter is None:
            if service is None:
                self._notify("项目服务尚未就绪，无法创建章节。", "error")
                return
            volume_id = None
            parent = self._nodes.get(chapter_node.parent_id or "")
            if parent is not None and parent.kind == "volume":
                volume_id = parent.id
            try:
                chapter = service.create_chapter(
                    project.id,
                    title=chapter_node.title,
                    volume_id=volume_id,
                    outline_id=chapter_node.id,
                )
            except Exception as exc:
                logger.exception("根据大纲创建章节失败")
                self._notify(f"创建章节失败：{exc}", "error")
                return
            if chapter is None:
                self._notify("创建章节失败：服务未返回章节。", "error")
                return
            self._refresh_snapshot_quietly()
            self._notify(f"已根据大纲创建章节《{chapter_node.title}》。", "success")
            self._emit_data_changed("chapters")

        self.ctx.navigate("editor", {"chapter_id": chapter.id})

    # ------------------------------------------------------------------ 工具
    def _after_mutation(self, select_id: str | None, message: str | None = None) -> None:
        self._dirty = False
        self._refresh_snapshot_quietly()
        self._rebuild_tree(select_id=select_id, force=True)
        if message:
            self._notify(message, "success")
        self._emit_data_changed("outline")

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


def _kind_icon(kind: str) -> Any:
    return icon(KIND_ICONS.get(kind, "outline"))
