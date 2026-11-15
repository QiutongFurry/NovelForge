"""Dialogs for context inspection and chapter versions."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from novel_ai.context.models import ContextPlan
from novel_ai.domain.models import ChapterVersion
from novel_ai.ui.theme import current_palette
from novel_ai.ui.widgets import ConfirmDialog


class ContextPreviewDialog(QDialog):
    """Show exactly what will be sent to the model and how it was compressed."""

    def __init__(
        self,
        plan: ContextPlan,
        parent: QWidget | None = None,
        *,
        provider_label: str = "",
    ) -> None:
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("上下文预览")
        self.setModal(True)
        self.resize(1080, 780)
        self.setMinimumSize(860, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title = QLabel("上下文预览")
        title.setStyleSheet("font-size: 21px; font-weight: 700;")
        subtitle = QLabel(
            f"{provider_label or '当前模型'} · 只生成本章 · 展示压缩后的最终 Prompt"
        )
        subtitle.setProperty("role", "muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        root.addLayout(header)

        stats = QHBoxLayout()
        stats.setSpacing(10)
        stats.addWidget(self._stat("模型上下文窗口", f"{plan.context_window:,}", "tokens"))
        stats.addWidget(self._stat("输入预算", f"{plan.input_budget:,}", "tokens"))
        stats.addWidget(
            self._stat("预计输入", f"{plan.input_tokens:,}", f"{plan.usage_ratio * 100:.0f}% 已使用")
        )
        stats.addWidget(self._stat("预留输出", f"{plan.reserved_output:,}", "tokens"))
        stats.addWidget(
            self._stat(
                "压缩提示", f"{len(plan.truncated_sections)}", "个分节被裁剪/压缩"
            )
        )
        root.addLayout(stats)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(8)
        left_label = QLabel("上下文分节")
        left_label.setStyleSheet("font-weight: 600;")
        left_layout.addWidget(left_label)
        self.section_list = QListWidget()
        self.section_list.setMinimumWidth(270)
        for section in plan.sections:
            marker = "●" if section.included else "○"
            state = "已压缩" if section.truncated else ("已包含" if section.included else "已省略")
            item = QListWidgetItem(
                f"{marker} {section.title}   ·   {section.tokens:,} tokens   [{state}]"
            )
            item.setData(Qt.ItemDataRole.UserRole, section.key)
            if section.truncated:
                item.setForeground(
                    QBrush(QColor(current_palette("dark")["warning"]))
                )
            elif not section.included:
                item.setForeground(
                    QBrush(QColor(current_palette("dark")["text_muted"]))
                )
            self.section_list.addItem(item)
        self.section_list.currentRowChanged.connect(self._show_section)
        left_layout.addWidget(self.section_list, 1)
        note = QLabel(
            "生成时会使用这里展示的 System / User Prompt；系统消息末尾会追加不可删除的"
            "“只输出当前这一章”硬性规则。"
        )
        note.setProperty("role", "hint")
        note.setWordWrap(True)
        left_layout.addWidget(note)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(8)
        self.section_meta = QLabel("选择一个分节查看最终文本")
        self.section_meta.setProperty("role", "muted")
        right_layout.addWidget(self.section_meta)
        self.section_preview = QPlainTextEdit()
        self.section_preview.setReadOnly(True)
        self.section_preview.setObjectName("PromptPreview")
        right_layout.addWidget(self.section_preview, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([320, 720])
        root.addWidget(splitter, 1)

        tabs = QTabWidget()
        self.system_view = self._prompt_view(plan.system_prompt)
        self.user_view = self._prompt_view(plan.user_prompt)
        tabs.addTab(self.system_view, "最终 System Prompt")
        tabs.addTab(self.user_view, "最终 User Prompt")

        notes_page = QWidget()
        notes_layout = QVBoxLayout(notes_page)
        notes_layout.setContentsMargins(12, 10, 12, 10)
        notes_layout.setSpacing(8)
        notes_label = QLabel("压缩说明")
        notes_label.setStyleSheet("font-weight: 600;")
        notes_layout.addWidget(notes_label)
        if plan.compression_notes:
            for note_text in plan.compression_notes:
                label = QLabel(f"• {note_text}")
                label.setProperty("role", "muted")
                label.setWordWrap(True)
                notes_layout.addWidget(label)
        else:
            empty = QLabel("未触发压缩，全部分节均在预算内。")
            empty.setProperty("role", "muted")
            notes_layout.addWidget(empty)
        if plan.retrieval_hits:
            retrieval_label = QLabel("检索命中")
            retrieval_label.setStyleSheet("font-weight: 600; margin-top: 6px;")
            notes_layout.addWidget(retrieval_label)
            for hit in plan.retrieval_hits:
                label = QLabel(
                    f"• [{hit.source_type}] {hit.title} · 相似度 {hit.score:.3f}\n  {hit.text[:160]}"
                )
                label.setProperty("role", "muted")
                label.setWordWrap(True)
                notes_layout.addWidget(label)
        else:
            no_retrieval = QLabel("本次未启用检索或没有命中相关片段。")
            no_retrieval.setProperty("role", "muted")
            notes_layout.addWidget(no_retrieval)
        notes_layout.addStretch(1)
        tabs.addTab(notes_page, "压缩与检索")
        root.addWidget(tabs, 1)

        actions = QHBoxLayout()
        used = f"输入 {plan.input_tokens:,} / {plan.input_budget:,} tokens · 共 {len(plan.sections)} 个分节"
        used_label = QLabel(used)
        used_label.setProperty("role", "hint")
        actions.addWidget(used_label)
        actions.addStretch(1)
        copy_button = QPushButton("复制完整 Prompt")
        copy_button.clicked.connect(self._copy_prompt)
        actions.addWidget(copy_button)
        close_button = QPushButton("关闭")
        close_button.setProperty("variant", "primary")
        close_button.clicked.connect(self.accept)
        actions.addWidget(close_button)
        root.addLayout(actions)

        if plan.sections:
            self.section_list.setCurrentRow(0)

    @staticmethod
    def _stat(label: str, value: str, hint: str) -> QFrame:
        card = QFrame()
        card.setObjectName("StatCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        title = QLabel(label)
        title.setProperty("role", "caption")
        val = QLabel(value)
        val.setStyleSheet("font-size: 19px; font-weight: 700;")
        sub = QLabel(hint)
        sub.setProperty("role", "hint")
        layout.addWidget(title)
        layout.addWidget(val)
        layout.addWidget(sub)
        return card

    @staticmethod
    def _prompt_view(text: str) -> QPlainTextEdit:
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setObjectName("PromptPreview")
        view.setPlainText(text)
        return view

    def _show_section(self, row: int) -> None:
        if row < 0 or row >= len(self.plan.sections):
            return
        section = self.plan.sections[row]
        state = "已压缩" if section.truncated else ("已包含" if section.included else "已省略")
        self.section_meta.setText(
            f"{section.title} · {section.tokens:,} tokens · 优先级 {section.priority} · {state}"
            + (f" · {section.note}" if section.note else "")
        )
        self.section_preview.setPlainText(section.content or "（无内容）")

    def _copy_prompt(self) -> None:
        text = (
            "===== SYSTEM =====\n"
            + self.plan.system_prompt
            + "\n\n===== USER =====\n"
            + self.plan.user_prompt
        )
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        try:
            self.sender().setText("已复制")
        except Exception:  # pragma: no cover - cosmetic only
            pass


class VersionHistoryDialog(QDialog):
    """Browse versions, compare them with the current draft, and restore one."""

    def __init__(
        self,
        versions: list[ChapterVersion],
        current_title: str,
        current_content: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.versions = versions
        self.current_title = current_title
        self.current_content = current_content
        self.selected_version: ChapterVersion | None = None
        self.setWindowTitle("版本历史与对比")
        self.setModal(True)
        self.resize(1000, 700)
        self.setMinimumSize(760, 540)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(12)
        title = QLabel("版本历史")
        title.setStyleSheet("font-size: 21px; font-weight: 700;")
        subtitle = QLabel(
            f"当前草稿：{current_title or '未命名'} · 共 {len(versions)} 个历史版本。"
            "恢复前会先为当前草稿创建快照。"
        )
        subtitle.setProperty("role", "muted")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(6)
        self.version_list = QListWidget()
        for version in reversed(versions):
            item = QListWidgetItem(
                f"v{version.version_no} · {version.created_at[:16].replace('T', ' ')}\n"
                f"{version.note or '无备注'} · {version.word_count} 字"
            )
            item.setData(Qt.ItemDataRole.UserRole, version.id)
            self.version_list.addItem(item)
        self.version_list.currentRowChanged.connect(self._show_version)
        left_layout.addWidget(self.version_list, 1)
        splitter.addWidget(left)

        right = QTabWidget()
        preview_page = QWidget()
        preview_layout = QVBoxLayout(preview_page)
        preview_layout.setContentsMargins(8, 8, 8, 8)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setObjectName("PromptPreview")
        self.preview.setPlaceholderText("选择一个版本查看正文")
        preview_layout.addWidget(self.preview)
        right.addTab(preview_page, "版本预览")

        compare_page = QWidget()
        compare_layout = QHBoxLayout(compare_page)
        compare_layout.setContentsMargins(8, 8, 8, 8)
        compare_layout.setSpacing(8)
        current_box = QVBoxLayout()
        current_label = QLabel("当前草稿")
        current_label.setStyleSheet("font-weight: 600;")
        current_box.addWidget(current_label)
        current_view = QPlainTextEdit()
        current_view.setReadOnly(True)
        current_view.setPlainText(current_content)
        current_box.addWidget(current_view, 1)
        version_box = QVBoxLayout()
        self.version_label = QLabel("历史版本")
        self.version_label.setStyleSheet("font-weight: 600;")
        version_box.addWidget(self.version_label)
        self.compare_view = QPlainTextEdit()
        self.compare_view.setReadOnly(True)
        version_box.addWidget(self.compare_view, 1)
        compare_layout.addLayout(current_box, 1)
        compare_layout.addLayout(version_box, 1)
        right.addTab(compare_page, "并排对比")
        splitter.addWidget(right)
        splitter.setSizes([310, 690])
        right.setCurrentIndex(0)
        root.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.info_label = QLabel("")
        self.info_label.setProperty("role", "hint")
        actions.addWidget(self.info_label)
        actions.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.reject)
        actions.addWidget(close_button)
        self.restore_button = QPushButton("恢复此版本")
        self.restore_button.setProperty("variant", "primary")
        self.restore_button.setEnabled(False)
        self.restore_button.clicked.connect(self._restore)
        actions.addWidget(self.restore_button)
        root.addLayout(actions)

        if self.version_list.count():
            self.version_list.setCurrentRow(0)

    def _current_version(self) -> ChapterVersion | None:
        item = self.version_list.currentItem()
        if item is None:
            return None
        version_id = item.data(Qt.ItemDataRole.UserRole)
        return next((v for v in self.versions if v.id == version_id), None)

    def _show_version(self, row: int) -> None:
        version = self._current_version()
        if version is None:
            return
        content = version.content or "（空版本）"
        title = version.title or "未命名"
        self.preview.setPlainText(f"{title}\n\n{content}")
        self.compare_view.setPlainText(content)
        self.version_label.setText(
            f"v{version.version_no} · {version.created_at[:16].replace('T', ' ')} · {version.word_count} 字"
        )
        self.info_label.setText(version.note or "")
        self.restore_button.setEnabled(True)

    def _restore(self) -> None:
        version = self._current_version()
        if version is None:
            return
        if not ConfirmDialog.ask(
            self,
            "恢复历史版本",
            f"确定恢复 v{version.version_no}？当前草稿会先保存为一个新版本，恢复后可再次回滚。",
            confirm_text="恢复",
        ):
            return
        self.selected_version = version
        self.accept()
