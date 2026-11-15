"""项目设置页面。

用于编辑 :class:`novel_ai.domain.models.Project` 的全部可编辑字段：书名、类型、
一句话简介、故事简介、世界观、风格、禁忌、叙事视角、基调、节奏、对话比例、
每章目标字数与默认供应商。

设计要点：
- 页面滚动承载多个卡片分组，长文本使用 ``QPlainTextEdit``；
- 表单填充期间使用 ``_loading`` 闸门，避免 setText / setCurrentIndex 触发脏标记；
- 保存调用 ``ProjectService.update_project`` + ``AppContext.update_current_project``，
  随后 ``notify`` 并 ``dataChanged.emit``；
- 没有打开项目或供应商服务不可用时只显示空状态 / 提示，不抛异常。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from novel_ai.domain.models import Project
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

GENRE_PRESETS = [
    "都市",
    "玄幻",
    "仙侠",
    "科幻",
    "悬疑",
    "历史",
    "言情",
    "奇幻",
    "武侠",
    "恐怖",
    "现实",
    "游戏竞技",
]

POV_PRESETS = ["第三人称有限视角", "第一人称", "第三人称全知视角", "多视角切换"]
TONE_PRESETS = ["克制而有张力", "轻松幽默", "冷峻硬朗", "温情细腻", "史诗恢弘", "暗黑压抑"]
PACING_PRESETS = ["张弛有度", "快节奏", "慢热铺垫", "高潮迭起", "舒缓抒情"]


def _format_time(value: str | None) -> str:
    if not value:
        return "未知"
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value)[:16]


def _section_card(title: str, subtitle: str = "") -> tuple[Card, QGridLayout]:
    """创建一个带标题与两列表单网格的卡片。"""

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
    grid.setColumnMinimumWidth(0, 86)
    card.content_layout.addLayout(grid)
    return card, grid


def _add_row(
    grid: QGridLayout,
    row: int,
    label: str,
    field: QWidget,
    hint: str = "",
) -> int:
    """向表单网格追加一行：左列标签、右列控件（可带提示文案）。"""

    caption = QLabel(label)
    caption.setProperty("role", "caption")
    caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
    caption.setMinimumWidth(80)
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


class ProjectSettingsPage(BasePage):
    """项目设置表单页。"""

    page_key = "settings"
    page_title = "项目设置"
    page_subtitle = "定义这本书的类型、世界观与写作风格，AI 会据此保持设定一致。"

    def __init__(self, ctx: Any, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self._project: Project | None = None
        self._loading = False
        self._dirty = False
        self._provider_count = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack = QStackedWidget()
        root.addWidget(self._stack)

        self._empty_page = self._build_empty_page()
        self._stack.addWidget(self._empty_page)

        self.scroll = ScrollPage()
        self._stack.addWidget(self.scroll)
        self._build_form()

        self._connect_dirty_signals()
        self._sync_from_context(force=True)

    # ------------------------------------------------------------------ 构建
    def _build_empty_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 28)
        layout.setSpacing(18)
        header = PageHeader(self.page_title, self.page_subtitle)
        layout.addWidget(header)
        empty = EmptyState(
            "尚未打开项目",
            "项目设置需要先打开一个项目。去项目工坊打开已有项目，或创建一个新项目。",
            icon_name="settings",
            action_text="去项目工坊",
        )
        empty.actionClicked.connect(lambda: self.ctx.navigate("dashboard"))
        layout.addWidget(empty, 1)
        return page

    def _build_form(self) -> None:
        self.header = PageHeader(self.page_title, self.page_subtitle)
        self.reload_button = tool_button(
            "恢复", "undo", variant="secondary", tooltip="放弃未保存修改并重新载入"
        )
        self.reload_button.clicked.connect(self._reload_from_context)
        self.save_button = QPushButton("保存设置")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save)
        self.header.add_action(self.reload_button)
        self.header.add_action(self.save_button)
        self.scroll.add(self.header)

        self.meta_label = QLabel("")
        self.meta_label.setProperty("role", "hint")
        self.meta_label.setWordWrap(True)
        self.scroll.add(self.meta_label)

        # ---------------------------------------------------------- 基本信息
        basic_card, basic_grid = _section_card(
            "基本信息", "书名与类型会出现在导出文件、章节标题和 AI 提示词中。"
        )
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("例如：星轨尽头")
        self.title_edit.setClearButtonEnabled(True)
        self.genre_combo = QComboBox()
        self.genre_combo.setEditable(True)
        self.genre_combo.addItems(GENRE_PRESETS)
        self.genre_combo.setCurrentText("")
        self.logline_edit = QLineEdit()
        self.logline_edit.setPlaceholderText("一句话讲清故事核心，例如：失忆的星图师在帝国边境寻找自己的身世。")
        self.synopsis_edit = QPlainTextEdit()
        self.synopsis_edit.setPlaceholderText("更完整的故事梗概：主角是谁、想要什么、遇到什么阻碍、结局走向。")
        self.synopsis_edit.setMinimumHeight(96)

        row = 0
        row = _add_row(basic_grid, row, "书名", self.title_edit, "保存前不能为空。")
        row = _add_row(basic_grid, row, "类型", self.genre_combo, "可以直接输入自定义类型。")
        row = _add_row(basic_grid, row, "一句话简介", self.logline_edit)
        _add_row(basic_grid, row, "故事简介", self.synopsis_edit)
        self.scroll.add(basic_card)

        # ---------------------------------------------------------- 世界观与风格
        world_card, world_grid = _section_card(
            "世界观与风格",
            "硬设定会作为 AI 上下文的高优先级内容，请只写稳定、确定的规则。",
        )
        self.world_edit = QPlainTextEdit()
        self.world_edit.setPlaceholderText(
            "世界背景、力量体系、地理与阵营、时代科技水平、核心规则与代价……"
        )
        self.world_edit.setMinimumHeight(130)
        self.style_edit = QPlainTextEdit()
        self.style_edit.setPlaceholderText("叙事风格、语言质感、参考作者或作品、常用修辞。")
        self.style_edit.setMinimumHeight(88)
        self.taboos_edit = QPlainTextEdit()
        self.taboos_edit.setPlaceholderText("禁止出现的内容：雷点、敏感设定、OOC 行为、不能写的剧情走向……")
        self.taboos_edit.setMinimumHeight(88)

        row = 0
        row = _add_row(world_grid, row, "世界观", self.world_edit)
        row = _add_row(world_grid, row, "写作风格", self.style_edit)
        _add_row(world_grid, row, "禁忌", self.taboos_edit)
        self.scroll.add(world_card)

        # ---------------------------------------------------------- 写作偏好
        pref_card, pref_grid = _section_card(
            "写作偏好", "这些参数会作为生成默认值，可在每次生成时临时覆盖。"
        )
        self.pov_combo = self._make_editable_combo(POV_PRESETS)
        self.tone_combo = self._make_editable_combo(TONE_PRESETS)
        self.pacing_combo = self._make_editable_combo(PACING_PRESETS)

        dialogue_box = QWidget()
        dialogue_layout = QHBoxLayout(dialogue_box)
        dialogue_layout.setContentsMargins(0, 0, 0, 0)
        dialogue_layout.setSpacing(10)
        self.dialogue_slider = QSlider(Qt.Orientation.Horizontal)
        self.dialogue_slider.setRange(0, 100)
        self.dialogue_slider.setSingleStep(5)
        self.dialogue_slider.setPageStep(5)
        self.dialogue_spin = QDoubleSpinBox()
        self.dialogue_spin.setRange(0.0, 1.0)
        self.dialogue_spin.setSingleStep(0.05)
        self.dialogue_spin.setDecimals(2)
        self.dialogue_spin.setFixedWidth(86)
        dialogue_layout.addWidget(self.dialogue_slider, 1)
        dialogue_layout.addWidget(self.dialogue_spin)
        self.dialogue_slider.valueChanged.connect(self._on_dialogue_slider)
        self.dialogue_spin.valueChanged.connect(self._on_dialogue_spin)

        self.target_words_spin = QSpinBox()
        self.target_words_spin.setRange(300, 20_000)
        self.target_words_spin.setSingleStep(100)
        self.target_words_spin.setSuffix(" 字")
        self.target_words_spin.setFixedWidth(140)

        provider_box = QWidget()
        provider_layout = QHBoxLayout(provider_box)
        provider_layout.setContentsMargins(0, 0, 0, 0)
        provider_layout.setSpacing(8)
        self.provider_combo = QComboBox()
        self.provider_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.manage_provider_button = tool_button(
            "管理", "key", variant="ghost", tooltip="前往 AI 配置页管理供应商"
        )
        self.manage_provider_button.clicked.connect(lambda: self.ctx.navigate("api"))
        provider_layout.addWidget(self.provider_combo, 1)
        provider_layout.addWidget(self.manage_provider_button)

        row = 0
        row = _add_row(pref_grid, row, "叙事视角", self.pov_combo)
        row = _add_row(pref_grid, row, "基调", self.tone_combo)
        row = _add_row(pref_grid, row, "节奏", self.pacing_combo)
        row = _add_row(pref_grid, row, "对话比例", dialogue_box, "对话占正文的大致比例。")
        row = _add_row(pref_grid, row, "每章目标", self.target_words_spin, "生成章节时的默认目标字数。")
        _add_row(
            pref_grid,
            row,
            "默认供应商",
            provider_box,
            "留空时跟随系统全局默认供应商。",
        )
        self.provider_hint = QLabel("")
        self.provider_hint.setProperty("role", "hint")
        self.provider_hint.setWordWrap(True)
        pref_grid.addWidget(self.provider_hint, row + 1, 1)
        self.scroll.add(pref_card)

        self.scroll.body_layout.addStretch(1)
        self._update_dirty_ui()

    @staticmethod
    def _make_editable_combo(items: list[str]) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(items)
        combo.setCurrentText("")
        return combo

    # ------------------------------------------------------------------ 生命周期
    def on_project_changed(self, project: Project | None) -> None:
        if project is None:
            self._project = None
            self._dirty = False
            self._update_dirty_ui()
            self._stack.setCurrentWidget(self._empty_page)
            return

        if self._project is not None and self._project.id == project.id and self._dirty:
            # 同一项目的外部刷新：保留用户正在编辑的内容，只更新底稿对象。
            self._project = project
            self._populate_providers()
            return

        self._load_project(project)

    def on_navigated(self, payload: object = None) -> None:
        if self._dirty:
            return
        self._sync_from_context(force=True)

    def refresh(self) -> None:
        if self._dirty:
            # 避免刷新覆盖未保存的表单内容。
            self._populate_providers()
            return
        self._sync_from_context(force=True)

    # ------------------------------------------------------------------ 数据装载
    def _sync_from_context(self, force: bool = False) -> None:
        project = getattr(self.ctx, "current_project", None)
        if project is None:
            self._project = None
            self._stack.setCurrentWidget(self._empty_page)
            self._update_dirty_ui()
            return
        if not force and self._dirty and self._project is not None and self._project.id == project.id:
            return
        self._load_project(project)

    def _load_project(self, project: Project) -> None:
        self._loading = True
        try:
            self._project = project
            self.title_edit.setText(project.title or "")
            self.genre_combo.setCurrentText(project.genre or "")
            self.logline_edit.setText(project.logline or "")
            self.synopsis_edit.setPlainText(project.synopsis or "")
            self.world_edit.setPlainText(project.worldbuilding or "")
            self.style_edit.setPlainText(project.style or "")
            self.taboos_edit.setPlainText(project.taboos or "")
            self.pov_combo.setCurrentText(project.pov or "")
            self.tone_combo.setCurrentText(project.tone or "")
            self.pacing_combo.setCurrentText(project.pacing or "")

            ratio = float(getattr(project, "dialogue_ratio", 0.35) or 0.0)
            ratio = min(max(ratio, 0.0), 1.0)
            self.dialogue_spin.setValue(ratio)
            self.dialogue_slider.setValue(int(round(ratio * 100)))
            self.target_words_spin.setValue(
                int(getattr(project, "target_words", 2500) or 2500)
            )
            self._populate_providers()
        finally:
            self._loading = False
        self._dirty = False
        self._update_dirty_ui()
        self._stack.setCurrentWidget(self.scroll)

    def _populate_providers(self) -> None:
        """填充默认供应商下拉框；不修改脏状态（调用方负责闸门）。"""

        configs: list[Any] = []
        error = ""
        manager = getattr(self.ctx, "provider_manager", None)
        if manager is not None:
            try:
                configs = list(manager.list_configs() or [])
            except Exception as exc:  # 供应商服务可能尚未就绪
                logger.exception("读取供应商配置失败")
                error = str(exc)
        self._provider_count = len(configs)

        combo = self.provider_combo
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("跟随系统全局默认", None)
            for config in configs:
                name = getattr(config, "name", "供应商") or "供应商"
                model = getattr(config, "model", "") or ""
                label = f"{name} · {model}" if model else str(name)
                combo.addItem(label, getattr(config, "id", None))

            target_id = getattr(self._project, "default_provider_id", None) if self._project else None
            if target_id:
                index = combo.findData(target_id)
                if index < 0:
                    combo.addItem(f"未知供应商（{target_id}）", target_id)
                    index = combo.count() - 1
                combo.setCurrentIndex(index)
            else:
                combo.setCurrentIndex(0)
        finally:
            combo.blockSignals(False)

        if error:
            self.provider_hint.setText(f"读取供应商列表失败：{error}")
        elif not configs:
            self.provider_hint.setText("尚未配置供应商，请先在「AI 配置」页添加一个供应商。")
        else:
            self.provider_hint.setText(
                f"共 {len(configs)} 个可用供应商；选择「跟随系统全局默认」时使用系统的默认配置。"
            )

    # ------------------------------------------------------------------ 交互
    def _on_dialogue_slider(self, value: int) -> None:
        self.dialogue_spin.setValue(value / 100.0)

    def _on_dialogue_spin(self, value: float) -> None:
        self.dialogue_slider.setValue(int(round(float(value) * 100)))

    def _connect_dirty_signals(self) -> None:
        for line_edit in (self.title_edit, self.logline_edit):
            line_edit.textChanged.connect(self._mark_dirty)
        for plain in (
            self.synopsis_edit,
            self.world_edit,
            self.style_edit,
            self.taboos_edit,
        ):
            plain.textChanged.connect(self._mark_dirty)
        for combo in (
            self.genre_combo,
            self.pov_combo,
            self.tone_combo,
            self.pacing_combo,
        ):
            combo.currentTextChanged.connect(self._mark_dirty)
        self.dialogue_slider.valueChanged.connect(self._mark_dirty)
        self.dialogue_spin.valueChanged.connect(self._mark_dirty)
        self.target_words_spin.valueChanged.connect(self._mark_dirty)
        self.provider_combo.currentIndexChanged.connect(self._mark_dirty)

    def _mark_dirty(self, *_args: object) -> None:
        if self._loading or self._project is None:
            return
        if self._dirty:
            return
        self._dirty = True
        self._update_dirty_ui()

    def _update_dirty_ui(self) -> None:
        has_project = self._project is not None
        self.save_button.setEnabled(has_project and self._dirty)
        subtitle = self.page_subtitle
        if self._dirty:
            subtitle = f"● 有未保存的修改 · {subtitle}"
        self.header.set_subtitle(subtitle)
        if self._project is not None:
            self.meta_label.setText(
                f"创建于 {_format_time(self._project.created_at)} · "
                f"最后更新 {_format_time(self._project.updated_at)}"
            )
            self.meta_label.setVisible(True)
        else:
            self.meta_label.setVisible(False)

    # ------------------------------------------------------------------ 保存
    def save(self) -> None:
        """公开保存入口（供 MainWindow 的 Ctrl+S 等全局快捷键调用）。"""

        self._save()

    def _apply_form(self, project: Project) -> bool:
        title = self.title_edit.text().strip()
        if not title:
            self._notify("书名不能为空。", "warning")
            self.title_edit.setFocus()
            return False

        project.title = title
        project.genre = self.genre_combo.currentText().strip()
        project.logline = self.logline_edit.text().strip()
        project.synopsis = self.synopsis_edit.toPlainText().strip()
        project.worldbuilding = self.world_edit.toPlainText().strip()
        project.style = self.style_edit.toPlainText().strip()
        project.taboos = self.taboos_edit.toPlainText().strip()
        project.pov = self.pov_combo.currentText().strip()
        project.tone = self.tone_combo.currentText().strip()
        project.pacing = self.pacing_combo.currentText().strip()
        project.dialogue_ratio = round(float(self.dialogue_spin.value()), 2)
        project.target_words = int(self.target_words_spin.value())
        provider_id = self.provider_combo.currentData()
        project.default_provider_id = str(provider_id) if provider_id else None
        return True

    def _save(self) -> None:
        project = self._project or getattr(self.ctx, "current_project", None)
        service = getattr(self.ctx, "project_service", None)
        if project is None:
            self._notify("请先打开一个项目。", "warning")
            return
        if service is None:
            self._notify("项目服务尚未就绪，无法保存。", "error")
            return
        if not self._apply_form(project):
            return

        try:
            project.touch()
            saved = service.update_project(project)
        except Exception as exc:
            logger.exception("保存项目设置失败")
            self._notify(f"保存失败：{exc}", "error")
            return
        saved = saved or project
        self._project = saved
        self._dirty = False

        try:
            self.ctx.update_current_project(saved)
        except Exception:
            logger.exception("同步当前项目失败")

        self._update_dirty_ui()
        self._notify("项目设置已保存。", "success")
        self._emit_data_changed("project")

    def _reload_from_context(self) -> None:
        if self._dirty:
            confirmed = ConfirmDialog.ask(
                self,
                "放弃修改",
                "当前表单有未保存的修改，确定放弃并重新载入吗？",
                confirm_text="放弃修改",
                danger=True,
            )
            if not confirmed:
                return
        self._dirty = False
        self._sync_from_context(force=True)

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
