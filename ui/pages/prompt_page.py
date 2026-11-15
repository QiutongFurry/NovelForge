"""Prompt template editor page.

Templates use ``{{placeholder}}`` double-brace markers.  The page explains which
placeholders the context builder can fill and warns that the "only the current
chapter" hard guard is appended by the generation pipeline itself and therefore
cannot be removed by editing this template.
"""

from __future__ import annotations

import logging
import re
from dataclasses import is_dataclass, replace
from types import SimpleNamespace
from typing import Any

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from novel_ai.ui.icons import icon
from novel_ai.ui.pages.base import BasePage
from novel_ai.ui.widgets import (
    Badge,
    Card,
    ConfirmDialog,
    FlowLayout,
    MutedLabel,
    PageHeader,
    ScrollPage,
    SectionTitle,
)

logger = logging.getLogger(__name__)

_DEFAULT_PLACEHOLDERS: list[str] = [
    "project_title",
    "genre",
    "chapter_number",
    "chapter_title",
    "worldbuilding",
    "style",
    "taboos",
    "global_summary",
    "volume_summary",
    "chapter_summaries",
    "characters",
    "memories",
    "outline",
    "recent_chapters",
    "retrieval",
    "requirements",
    "target_words",
    "pov",
    "tone",
    "pacing",
    "dialogue_ratio",
    "output_format",
]

_PLACEHOLDER_HINTS: dict[str, str] = {
    "project_title": "作品标题",
    "genre": "题材 / 类型",
    "chapter_number": "当前章节序号",
    "chapter_title": "当前章节标题",
    "worldbuilding": "世界观设定",
    "style": "文风与语言要求",
    "taboos": "写作禁忌 / 雷点",
    "global_summary": "全书摘要",
    "volume_summary": "本卷摘要",
    "chapter_summaries": "已写章节摘要合集",
    "characters": "角色当前状态卡片",
    "memories": "长期记忆锚点（伏笔/规则/道具/时间线）",
    "outline": "本章大纲与目标",
    "recent_chapters": "最近章节原文节选",
    "retrieval": "本地检索出的相关片段",
    "requirements": "本章额外要求",
    "target_words": "目标字数",
    "pov": "叙事视角",
    "tone": "语气",
    "pacing": "节奏",
    "dialogue_ratio": "对话比例",
    "output_format": "输出格式要求",
}

_FALLBACK_SYSTEM = (
    "你是「墨言」的小说写作引擎，一位经验丰富的中文长篇小说作家。\n"
    "请严格服从设定与本章大纲，只输出当前这一章的标题与正文。"
)
_FALLBACK_USER = (
    "【本章大纲】\n{{outline}}\n\n"
    "【角色当前状态】\n{{characters}}\n\n"
    "现在开始写作：只输出本章的章节标题与完整正文。"
)

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


def load_placeholders() -> list[str]:
    """Return the placeholders the context builder fills."""

    try:
        from novel_ai.prompts.templates import PLACEHOLDERS

        placeholders = list(PLACEHOLDERS)
    except Exception:  # pragma: no cover - prompts module unavailable
        placeholders = []
    return placeholders or list(_DEFAULT_PLACEHOLDERS)


class PromptPage(BasePage):
    page_key = "prompt"
    page_title = "Prompt 模板"
    page_subtitle = "编辑生成时使用的 System / User 模板；占位符将由上下文构建器安全替换。"

    def __init__(self, ctx, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self._loading = False
        self._dirty = False
        self._template: Any = None
        self._last_editor: QPlainTextEdit | None = None
        self.placeholders: list[str] = load_placeholders()
        self.placeholder_buttons: dict[str, QPushButton] = {}
        self._build_ui()
        self._load_template()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.scroll = ScrollPage(self, margins=(24, 20, 24, 28), spacing=16)
        root.addWidget(self.scroll)

        self.header = PageHeader(self.page_title, self.page_subtitle)
        self.status_badge = Badge("载入中", "neutral")
        self.reload_button = self._button("重新载入", "refresh", self._reload)
        self.reset_button = self._button("恢复默认", "undo", self._reset)
        self.save_button = self._button(
            "保存模板", "save", self._save, variant="primary"
        )
        self.header.add_action(self.status_badge)
        self.header.add_action(self.reload_button)
        self.header.add_action(self.reset_button)
        self.header.add_action(self.save_button)
        self.scroll.add(self.header)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #FF8296;")
        self.error_label.setVisible(False)
        self.scroll.add(self.error_label)

        warning_card = Card(padding=16, spacing=8)
        warning_header = QHBoxLayout()
        warning_header.setSpacing(8)
        warning_header.addWidget(SectionTitle("当前章节硬性保护"))
        warning_header.addStretch(1)
        warning_header.addWidget(Badge("始终追加", "warning"))
        warning_card.content_layout.addLayout(warning_header)
        warning_card.content_layout.addWidget(
            MutedLabel(
                "生成时系统会在 System 消息末尾自动追加「只输出当前这一章完整正文」的硬性规则。"
                "该规则不来自本模板，无法在这里删除或覆盖；请不要在模板中要求模型续写多章、"
                "输出解释或预告下一章。"
            )
        )
        self.scroll.add(warning_card)

        info_card = Card(padding=16, spacing=10)
        info_card.content_layout.addWidget(SectionTitle("模板信息"))
        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("模板名称")
        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("模板说明（可选）")
        form.addRow("名称", self.name_edit)
        form.addRow("说明", self.description_edit)
        info_card.content_layout.addLayout(form)
        self.scroll.add(info_card)

        placeholder_card = Card(padding=16, spacing=10)
        placeholder_header = QHBoxLayout()
        placeholder_header.setSpacing(8)
        placeholder_header.addWidget(SectionTitle("可用占位符"))
        placeholder_header.addStretch(1)
        placeholder_header.addWidget(
            Badge(f"{len(self.placeholders)} 个", "neutral")
        )
        placeholder_card.content_layout.addLayout(placeholder_header)
        placeholder_card.content_layout.addWidget(
            MutedLabel(
                "点击占位符可插入到最近编辑的模板文本框。使用 {{name}} 双花括号；"
                "未列出的占位符会原样保留，不会被替换。"
            )
        )
        placeholder_host = QWidget()
        self.placeholder_flow = FlowLayout(placeholder_host, margin=0, h_spacing=8, v_spacing=8)
        for name in self.placeholders:
            button = QPushButton("{{" + name + "}}")
            button.setProperty("variant", "ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(
                f"{name}：{_PLACEHOLDER_HINTS.get(name, '模板变量')}"
            )
            button.clicked.connect(
                lambda _checked=False, placeholder=name: self._insert_placeholder(
                    placeholder
                )
            )
            self.placeholder_flow.addWidget(button)
            self.placeholder_buttons[name] = button
        placeholder_card.content_layout.addWidget(placeholder_host)
        self.scroll.add(placeholder_card)

        self.system_edit = self._template_editor(
            "System 模板",
            "设定角色、创作铁律与输出边界，末尾会自动追加当前章节硬性规则。",
            280,
        )
        self.user_edit = self._template_editor(
            "User 模板",
            "组织上下文与本章写作任务；用占位符引用设定、摘要、大纲与检索片段。",
            240,
        )

        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.status_label = MutedLabel("正在读取本地模板…")
        footer.addWidget(self.status_label, 1)
        self.footer_save_button = self._button(
            "保存模板", "save", self._save, variant="primary"
        )
        footer.addWidget(self.footer_save_button)
        self.scroll.add_layout(footer)
        self.scroll.body_layout.addStretch(1)

        for editor in (self.system_edit, self.user_edit):
            editor.installEventFilter(self)

    def _template_editor(self, title: str, hint: str, min_height: int) -> QPlainTextEdit:
        card = Card(padding=16, spacing=8)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(SectionTitle(title))
        head.addStretch(1)
        counter = QLabel("0 字符 · 约 0 tokens")
        counter.setProperty("role", "hint")
        head.addWidget(counter)
        card.content_layout.addLayout(head)
        card.content_layout.addWidget(MutedLabel(hint))
        editor = QPlainTextEdit()
        editor.setMinimumHeight(min_height)
        editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        editor.setTabStopDistance(28)
        editor.setPlaceholderText("在此编辑模板，支持 {{placeholder}} 占位符…")
        card.content_layout.addWidget(editor)
        self.scroll.add(card)
        if title.startswith("System"):
            self.system_counter = counter
        else:
            self.user_counter = counter
        editor.textChanged.connect(self._on_editor_changed)
        return editor

    def _button(
        self,
        text: str,
        icon_name: str,
        handler,
        *,
        variant: str = "secondary",
    ) -> QPushButton:
        button = QPushButton(text)
        if icon_name:
            button.setIcon(icon(icon_name))
        button.setProperty("variant", variant)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(handler)
        return button

    # --------------------------------------------------------------- loading
    def _load_template(self) -> None:
        template: Any = None
        error = ""
        try:
            template = self.ctx.prompt_store.load()
        except Exception as exc:
            logger.exception("Could not load prompt template")
            error = str(exc)
        if template is None:
            template = self._fallback_template()
            if not error:
                error = "本地模板文件不可用"
        self.error_label.setText(
            f"读取模板失败：{error}。当前显示内置默认模板，保存可尝试修复。" if error else ""
        )
        self.error_label.setVisible(bool(error))
        self._apply_template(template)

    def _fallback_template(self) -> Any:
        template = _build_default_template()
        if template is not None:
            return template
        return SimpleNamespace(
            name="内置默认模板",
            description="（本地默认模板不可用时的内置回退）",
            system_template=_FALLBACK_SYSTEM,
            user_template=_FALLBACK_USER,
        )

    def _apply_template(self, template: Any) -> None:
        self._loading = True
        try:
            self._template = template
            self.name_edit.setText(str(getattr(template, "name", "") or ""))
            self.description_edit.setText(
                str(getattr(template, "description", "") or "")
            )
            self.system_edit.setPlainText(
                str(getattr(template, "system_template", "") or "")
            )
            self.user_edit.setPlainText(
                str(getattr(template, "user_template", "") or "")
            )
        finally:
            self._loading = False
        self._dirty = False
        self._update_counts()
        self._update_status()

    def _on_editor_changed(self) -> None:
        if self._loading:
            return
        self._dirty = True
        self._update_counts()
        self._update_status()

    def _update_counts(self) -> None:
        system_text = self.system_edit.toPlainText()
        user_text = self.user_edit.toPlainText()
        self.system_counter.setText(
            f"{len(system_text)} 字符 · 约 {_estimate_tokens(system_text)} tokens"
        )
        self.user_counter.setText(
            f"{len(user_text)} 字符 · 约 {_estimate_tokens(user_text)} tokens"
        )

    def _update_status(self) -> None:
        if self._template is None:
            self.status_badge.setText("未载入")
            self.status_badge.set_variant("warning")
            self.status_label.setText("模板未载入，请检查本地模板文件后重新载入。")
        elif self._dirty:
            self.status_badge.setText("有未保存修改")
            self.status_badge.set_variant("warning")
            self.status_label.setText(
                "修改尚未保存；保存后立即对下一次生成生效，无需重启。"
            )
        else:
            self.status_badge.setText("已同步")
            self.status_badge.set_variant("success")
            self.status_label.setText(
                "模板与本地文件一致；保存后立即对下一次生成生效，无需重启。"
            )

    # ------------------------------------------------------------- actions
    def _save(self) -> None:
        if self._template is None:
            self.ctx.notify("模板尚未载入，暂时无法保存。", "warning")
            return
        system_text = self.system_edit.toPlainText()
        user_text = self.user_edit.toPlainText()
        if not system_text.strip() or not user_text.strip():
            self.ctx.notify("System 模板与 User 模板都不能为空。", "warning")
            return
        values = {
            "name": self.name_edit.text().strip()
            or str(getattr(self._template, "name", "") or "自定义模板"),
            "description": self.description_edit.text().strip(),
            "system_template": system_text,
            "user_template": user_text,
        }
        template = self._build_template(values)
        try:
            self.ctx.prompt_store.save(template)
        except Exception as exc:
            logger.exception("Could not save prompt template")
            self.ctx.notify(f"保存模板失败：{exc}", "error")
            return
        self._template = template
        self._dirty = False
        self._update_status()
        self.ctx.notify("Prompt 模板已保存，将在下一次生成时生效。", "success")

    def _build_template(self, values: dict[str, str]) -> Any:
        base = self._template
        if base is not None and is_dataclass(base):
            try:
                return replace(base, **values)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Could not replace template dataclass: %s", exc)
        merged = {
            "name": values["name"],
            "description": values["description"],
            "system_template": values["system_template"],
            "user_template": values["user_template"],
        }
        return SimpleNamespace(**merged)

    def _reset(self) -> None:
        confirmed = ConfirmDialog.ask(
            self,
            "恢复默认模板",
            "当前编辑的 System / User 模板将被内置默认模板覆盖，且无法撤销。"
            "确定要恢复默认吗？",
            confirm_text="恢复默认",
            danger=True,
        )
        if not confirmed:
            return
        try:
            template = self.ctx.prompt_store.reset()
        except Exception as exc:
            logger.exception("Could not reset prompt template")
            self.ctx.notify(f"恢复默认模板失败：{exc}", "error")
            return
        self.error_label.setVisible(False)
        self._apply_template(template)
        self.ctx.notify("已恢复内置默认模板。", "success")

    def _reload(self) -> None:
        if self._dirty:
            confirmed = ConfirmDialog.ask(
                self,
                "重新载入模板",
                "重新载入会丢弃当前未保存的修改，确定继续吗？",
                confirm_text="重新载入",
                danger=True,
            )
            if not confirmed:
                return
        self._load_template()

    def _insert_placeholder(self, name: str) -> None:
        editor = self._last_editor or self.system_edit
        editor.insertPlainText("{{" + name + "}}")
        editor.setFocus()

    # ------------------------------------------------------------ lifecycle
    def eventFilter(self, watched, event) -> bool:  # noqa: ANN001 - Qt override
        if event.type() == QEvent.Type.FocusIn and watched in (
            self.system_edit,
            self.user_edit,
        ):
            self._last_editor = watched
        return super().eventFilter(watched, event)

    def refresh(self) -> None:
        if self._dirty:
            return
        self._load_template()

    def on_project_changed(self, project) -> None:  # noqa: ANN001 - BasePage hook
        # Prompt templates are global and independent from the current project.
        pass

    def on_navigated(self, payload: object = None) -> None:
        self.refresh()


def _build_default_template() -> Any:
    try:
        from novel_ai.prompts.templates import default_template

        return default_template()
    except Exception:  # pragma: no cover - prompts module unavailable
        return None


__all__ = ["PromptPage", "load_placeholders"]
