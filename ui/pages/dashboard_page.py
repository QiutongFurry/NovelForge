"""仪表盘 / 项目工坊页面。

页面职责：
- 用卡片流展示所有本地小说项目（最近打开的项目高亮）；
- 展示项目、章节、字数的聚合统计；
- 新建 / 打开 / 删除项目；
- 无项目、服务不可用时优雅降级为空状态，构造函数绝不抛异常。

所有数据都通过 :class:`novel_ai.ui.app_context.AppContext` 暴露的服务读取，
页面本身不直接接触 SQLite，也不做任何网络请求。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from novel_ai.domain.models import Project
from novel_ai.ui.pages.base import BasePage
from novel_ai.ui.widgets import (
    Badge,
    Card,
    ConfirmDialog,
    EmptyState,
    PageHeader,
    ScrollPage,
    StatCard,
    tool_button,
)

logger = logging.getLogger(__name__)

# 项目卡片的最小宽度与列间距；网格列数随窗口宽度在 1~3 列之间自适应。
CARD_MIN_WIDTH = 292
CARD_MIN_HEIGHT = 250
GRID_SPACING = 16
MAX_COLUMNS = 3

# project_stats() 的键名契约只声明了 dict[str, int]，这里做多键兜底，
# 以便和 storage 层的实现保持解耦。
_WORD_KEYS = ("words", "word_count", "total_words", "字数", "total_word_count")
_CHAPTER_KEYS = ("chapters", "chapter_count", "章节", "chapter_total")
_CHARACTER_KEYS = ("characters", "character_count", "角色")

_GENRE_FALLBACK = "未设置类型"


def _format_words(value: int) -> str:
    """把字数格式化成中文习惯的展示文本。"""

    value = max(0, int(value or 0))
    if value < 10_000:
        return f"{value:,} 字"
    return f"{value / 10_000:.1f} 万字"


def _format_time(value: str | None) -> str:
    """把 ISO 时间字符串转成 ``YYYY-MM-DD HH:MM``，解析失败时原样返回。"""

    if not value:
        return ""
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value)[:16]


def _stat_value(stats: dict[str, Any] | None, keys: tuple[str, ...]) -> int:
    if not isinstance(stats, dict):
        return 0
    for key in keys:
        value = stats.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


class DashboardPage(BasePage):
    """项目工坊：项目列表 + 新建项目 + 统计概览。"""

    page_key = "dashboard"
    page_title = "项目工坊"
    page_subtitle = "管理你的小说项目，继续未完成的灵感。"

    def __init__(self, ctx: Any, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self._projects: list[Project] = []
        self._cards: list[QWidget] = []
        self._empty_state: EmptyState | None = None
        self._last_project_id: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.scroll = ScrollPage(self)
        root.addWidget(self.scroll)

        # ---------------------------------------------------------- 页头
        self.header = PageHeader(self.page_title, self.page_subtitle)
        refresh_button = tool_button("刷新", "refresh", variant="secondary", tooltip="重新读取项目列表")
        refresh_button.clicked.connect(self.refresh)
        self.new_button = QPushButton("新建项目")
        self.new_button.setProperty("variant", "primary")
        self.new_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_button.clicked.connect(self._create_project)
        self.header.add_action(refresh_button)
        self.header.add_action(self.new_button)
        self.scroll.add(self.header)

        # ---------------------------------------------------------- 统计行
        stats_row = QHBoxLayout()
        stats_row.setSpacing(14)
        self.project_stat = StatCard("项目总数", "0", "本地作品")
        self.chapter_stat = StatCard("累计章节", "0", "所有项目的章节")
        self.word_stat = StatCard("累计字数", "0 字", "正文总字数")
        for card in (self.project_stat, self.chapter_stat, self.word_stat):
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            stats_row.addWidget(card, 1)
        self.scroll.add_layout(stats_row)

        # ---------------------------------------------------------- 列表标题
        section_row = QHBoxLayout()
        section_row.setSpacing(10)
        section_title = QLabel("我的项目")
        section_title.setObjectName("SectionTitle")
        section_row.addWidget(section_title)
        self.project_count_label = QLabel("")
        self.project_count_label.setProperty("role", "caption")
        section_row.addWidget(self.project_count_label)
        section_row.addStretch(1)
        self.scroll.add_layout(section_row)

        # ---------------------------------------------------------- 项目卡片网格
        # 用 QGridLayout 而不是自定义 FlowLayout：QGridLayout 会正确按列宽计算
        # 含 wordWrap 的卡片高度，避免卡片高度错乱 / 互相重叠。
        self._cards_host = QWidget()
        self._grid = QGridLayout(self._cards_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(GRID_SPACING)
        self._grid.setVerticalSpacing(GRID_SPACING)
        self._grid_columns = 0
        self._cards_host.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        self.scroll.add(self._cards_host)

        # ---------------------------------------------------------- 空状态容器
        self._empty_holder = QWidget()
        empty_layout = QVBoxLayout(self._empty_holder)
        empty_layout.setContentsMargins(0, 8, 0, 8)
        empty_layout.setSpacing(0)
        self._empty_layout = empty_layout
        self._empty_holder.setVisible(False)
        self.scroll.add(self._empty_holder)

        self.scroll.body_layout.addStretch(1)

        self.refresh()

    # ------------------------------------------------------------------ 生命周期
    def on_project_changed(self, project: Project | None) -> None:
        # 当前项目变化只影响卡片上的高亮，重新读列表即可（失败也不会抛异常）。
        self.refresh()

    def on_navigated(self, payload: object = None) -> None:
        self.refresh()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._relayout_cards()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        self._relayout_cards()

    def refresh(self) -> None:
        """重新从 ProjectService 读取项目与统计信息，并重建卡片流。"""

        settings = getattr(self.ctx, "settings", None)
        self._last_project_id = getattr(settings, "last_project_id", None)

        service = getattr(self.ctx, "project_service", None)
        projects: list[Project] = []
        stats_by_id: dict[str, dict[str, Any]] = {}
        error: str | None = None

        if service is None:
            error = "项目服务尚未就绪，请稍后重试。"
        else:
            try:
                projects = list(service.list_projects() or [])
            except Exception as exc:  # 数据库异常不应让页面崩溃
                logger.exception("读取项目列表失败")
                error = f"读取项目列表失败：{exc}"
            else:
                for project in projects:
                    try:
                        stats_by_id[project.id] = dict(service.project_stats(project.id) or {})
                    except Exception as exc:
                        logger.exception("读取项目统计失败: %s", project.id)
                        stats_by_id[project.id] = {"error": str(exc)}
                projects.sort(
                    key=lambda p: (getattr(p, "updated_at", "") or "", getattr(p, "created_at", "") or ""),
                    reverse=True,
                )

        self._projects = projects
        self._update_stats(projects, stats_by_id)
        self._rebuild_cards(projects, stats_by_id, error)

    # ------------------------------------------------------------------ 视图构建
    def _update_stats(
        self,
        projects: list[Project],
        stats_by_id: dict[str, dict[str, Any]],
    ) -> None:
        total_chapters = 0
        total_words = 0
        for stats in stats_by_id.values():
            total_chapters += _stat_value(stats, _CHAPTER_KEYS)
            total_words += _stat_value(stats, _WORD_KEYS)
        self.project_stat.set_value(str(len(projects)), "本地作品")
        self.chapter_stat.set_value(str(total_chapters), "所有项目的章节")
        self.word_stat.set_value(_format_words(total_words), "正文总字数")
        self.project_count_label.setText(f"共 {len(projects)} 个项目")

    def _rebuild_cards(
        self,
        projects: list[Project],
        stats_by_id: dict[str, dict[str, Any]],
        error: str | None,
    ) -> None:
        self._clear_cards()

        if error:
            self._show_empty(
                "无法读取项目列表",
                error,
                icon_name="warning",
                action_text="重新加载",
                action=self.refresh,
            )
            return
        if not projects:
            self._show_empty(
                "还没有任何项目",
                "从一个书名开始，创建你的第一部小说。项目数据保存在本地，随时可以备份。",
                icon_name="sparkles",
                action_text="新建项目",
                action=self._create_project,
            )
            return

        self._hide_empty()
        current_id = getattr(getattr(self.ctx, "current_project", None), "id", None)
        for project in projects:
            card = self._build_project_card(
                project, stats_by_id.get(project.id, {}), current_id
            )
            self._cards.append(card)
        self._relayout_cards(force=True)

    def _clear_cards(self) -> None:
        self._cards = []
        self._grid_columns = 0
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _column_count(self) -> int:
        """根据滚动区可用宽度计算列数（1~3 列），保证卡片不小于最小宽度。"""

        margins = self.scroll.body_layout.contentsMargins()
        viewport = self.scroll.viewport().width()
        available = viewport - margins.left() - margins.right()
        if available <= 0:
            available = self._cards_host.width()
        if available <= 0:
            return MAX_COLUMNS
        columns = (available + GRID_SPACING) // (CARD_MIN_WIDTH + GRID_SPACING)
        return max(1, min(MAX_COLUMNS, int(columns)))

    def _relayout_cards(self, force: bool = False) -> None:
        """把已有卡片按当前列数放进网格；窗口缩放时只重排，不重建。"""

        if not self._cards:
            return
        columns = self._column_count()
        if not force and columns == self._grid_columns:
            return
        self._grid_columns = columns
        for card in self._cards:
            self._grid.removeWidget(card)
        for index, card in enumerate(self._cards):
            row, column = divmod(index, columns)
            self._grid.addWidget(card, row, column)
        for column in range(MAX_COLUMNS):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)

    def _build_project_card(
        self,
        project: Project,
        stats: dict[str, Any],
        current_id: str | None,
    ) -> QWidget:
        card = Card(padding=16, spacing=8)
        card.setMinimumWidth(CARD_MIN_WIDTH)
        card.setMinimumHeight(CARD_MIN_HEIGHT)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        full_title = project.title.strip() or "未命名项目"
        full_logline = project.logline.strip() or project.synopsis.strip()
        card.setToolTip(
            f"{full_title}\n{full_logline}" if full_logline else full_title
        )

        # 1) 标题 + 状态徽章
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_label = QLabel(full_title)
        title_label.setObjectName("CardTitle")
        title_label.setWordWrap(True)
        title_label.setMaximumHeight(46)
        title_row.addWidget(title_label, 1)
        if project.id == current_id:
            title_row.addWidget(Badge("当前", "success"), 0, Qt.AlignmentFlag.AlignTop)
        elif project.id == self._last_project_id:
            title_row.addWidget(Badge("最近打开", "accent"), 0, Qt.AlignmentFlag.AlignTop)
        card.content_layout.addLayout(title_row)

        # 2) 类型标签
        badge_row = QHBoxLayout()
        badge_row.setSpacing(6)
        badge_row.addWidget(Badge(project.genre.strip() or _GENRE_FALLBACK, "neutral"))
        badge_row.addStretch(1)
        card.content_layout.addLayout(badge_row)

        # 3) 章节 / 字数
        chapters = _stat_value(stats, _CHAPTER_KEYS)
        words = _stat_value(stats, _WORD_KEYS)
        meta = QLabel(f"{chapters} 章 · {_format_words(words)}")
        meta.setProperty("role", "muted")
        card.content_layout.addWidget(meta)

        # 4) 简介（最多两行，完整内容在 tooltip）
        logline_text = full_logline or "还没有简介，打开项目设置补充书籍档案。"
        logline = QLabel(logline_text)
        logline.setProperty("role", "caption")
        logline.setWordWrap(True)
        logline.setMaximumHeight(38)
        card.content_layout.addWidget(logline)

        # 5) 更新时间
        updated = _format_time(project.updated_at)
        if updated:
            updated_label = QLabel(f"更新于 {updated}")
            updated_label.setProperty("role", "hint")
            card.content_layout.addWidget(updated_label)
        else:
            card.content_layout.addWidget(QLabel(""))

        # 6) 操作按钮
        actions = QHBoxLayout()
        actions.setSpacing(6)
        open_button = QPushButton("打开")
        open_button.setProperty("variant", "primary")
        open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        open_button.setToolTip("打开项目并进入写作编辑器")
        open_button.clicked.connect(lambda _=False, p=project: self._open_project(p, "editor"))
        actions.addWidget(open_button)

        settings_button = tool_button("设置", "settings", variant="ghost", tooltip="打开项目设置")
        settings_button.clicked.connect(lambda _=False, p=project: self._open_project(p, "settings"))
        actions.addWidget(settings_button)
        actions.addStretch(1)

        delete_button = tool_button("", "trash", variant="ghost", tooltip="删除项目")
        delete_button.setFixedSize(30, 30)
        delete_button.clicked.connect(lambda _=False, p=project: self._delete_project(p))
        actions.addWidget(delete_button)
        card.content_layout.addLayout(actions)

        return card

    def _show_empty(
        self,
        title: str,
        description: str,
        *,
        icon_name: str = "book",
        action_text: str = "",
        action: Any = None,
    ) -> None:
        while self._empty_layout.count():
            item = self._empty_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._empty_state = EmptyState(
            title,
            description,
            icon_name=icon_name,
            action_text=action_text,
        )
        if action is not None and self._empty_state.action_button is not None:
            self._empty_state.actionClicked.connect(action)
        self._empty_layout.addWidget(self._empty_state)
        self._empty_holder.setVisible(True)
        self._cards_host.setVisible(False)

    def _hide_empty(self) -> None:
        self._empty_holder.setVisible(False)
        self._cards_host.setVisible(True)

    # ------------------------------------------------------------------ 动作
    def _ask_project_title(self) -> str | None:
        """弹出书名输入框；取消时返回 ``None``。"""

        title, ok = QInputDialog.getText(
            self,
            "新建项目",
            "书名：",
            QLineEdit.EchoMode.Normal,
            "",
        )
        if not ok:
            return None
        return title.strip()

    def _create_project(self) -> None:
        """弹出书名对话框，创建项目后直接进入项目设置完善资料。"""

        title = self._ask_project_title()
        if title is None:
            return
        if not title:
            self._notify("书名不能为空。", "warning")
            return

        service = getattr(self.ctx, "project_service", None)
        if service is None:
            self._notify("项目服务尚未就绪，无法创建项目。", "error")
            return
        try:
            project = service.create_project(title)
        except Exception as exc:
            logger.exception("创建项目失败")
            self._notify(f"创建项目失败：{exc}", "error")
            return
        if project is None:
            self._notify("创建项目失败：服务未返回项目。", "error")
            return

        try:
            self.ctx.open_project(project.id)
        except Exception as exc:
            logger.exception("打开新项目失败")
            self._notify(f"项目已创建，但打开失败：{exc}", "warning")

        self._notify(f"已创建《{project.title}》，先完善项目设置吧。", "success")
        self._emit_data_changed("project")
        self.ctx.navigate("settings")
        self.refresh()

    def _open_project(self, project: Project, page_key: str) -> None:
        if project is None:
            return
        try:
            opened = self.ctx.open_project(project.id)
        except Exception as exc:
            logger.exception("打开项目失败")
            self._notify(f"打开项目失败：{exc}", "error")
            return
        if opened is None:
            # AppContext 内部已经提示过原因，这里只需要刷新列表。
            self.refresh()
            return
        self.ctx.navigate(page_key)
        self.refresh()

    def _delete_project(self, project: Project) -> None:
        if project is None:
            return
        confirmed = ConfirmDialog.ask(
            self,
            "删除项目",
            f"确定删除《{project.title}》吗？\n"
            "该项目的章节、角色、大纲、版本与记忆将一并删除，且无法恢复。",
            confirm_text="删除项目",
            danger=True,
        )
        if not confirmed:
            return

        service = getattr(self.ctx, "project_service", None)
        if service is None:
            self._notify("项目服务尚未就绪，无法删除项目。", "error")
            return
        try:
            service.delete_project(project.id)
        except Exception as exc:
            logger.exception("删除项目失败")
            self._notify(f"删除项目失败：{exc}", "error")
            return

        current = getattr(self.ctx, "current_project", None)
        if current is not None and current.id == project.id:
            try:
                self.ctx.close_project()
            except Exception:
                logger.exception("关闭已删除项目失败")
        self._notify(f"已删除《{project.title}》。", "success")
        self._emit_data_changed("project")
        self.refresh()

    # ------------------------------------------------------------------ 工具
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
