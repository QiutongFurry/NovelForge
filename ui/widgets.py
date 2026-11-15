"""Reusable UI building blocks shared by every page.

The widgets deliberately use the QSS object names from :mod:`novel_ai.ui.theme`
so pages stay visually consistent without repeating styling code.
"""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from novel_ai.ui.icons import icon
from novel_ai.ui.theme import current_palette


class Card(QFrame):
    """Rounded surface container with an internal vertical layout."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        padding: int = 18,
        spacing: int = 12,
        object_name: str = "Card",
    ) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(padding, padding, padding, padding)
        self._layout.setSpacing(spacing)

    @property
    def content_layout(self) -> QVBoxLayout:
        return self._layout

    def add_widget(self, widget: QWidget) -> None:
        self._layout.addWidget(widget)


class SoftCard(Card):
    def __init__(self, parent: QWidget | None = None, *, padding: int = 14) -> None:
        super().__init__(parent, padding=padding, object_name="SoftCard")


class SectionCard(Card):
    """Card with a section title row and optional header actions."""

    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
        *,
        subtitle: str = "",
        padding: int = 18,
    ) -> None:
        super().__init__(parent, padding=padding)
        header = QHBoxLayout()
        header.setSpacing(10)
        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("SectionTitle")
        text_box.addWidget(self.title_label)
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setProperty("role", "caption")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(subtitle))
        text_box.addWidget(self.subtitle_label)
        header.addLayout(text_box)
        header.addStretch(1)
        self._action_row = QHBoxLayout()
        self._action_row.setSpacing(6)
        header.addLayout(self._action_row)
        self._layout.addLayout(header)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        self._layout.addLayout(self.body)

    def add_action(self, widget: QWidget) -> None:
        self._action_row.addWidget(widget)

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))


class PageHeader(QWidget):
    def __init__(
        self,
        title: str,
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 4)
        root.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(4)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("PageTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("PageSubtitle")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(subtitle))
        left.addWidget(self.title_label)
        left.addWidget(self.subtitle_label)
        root.addLayout(left, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        root.addLayout(self.actions)

    def add_action(self, widget: QWidget) -> None:
        self.actions.addWidget(widget)

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))


class SectionTitle(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("SectionTitle")


class MutedLabel(QLabel):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setProperty("role", "muted")
        self.setWordWrap(True)


class Badge(QLabel):
    def __init__(
        self,
        text: str = "",
        variant: str = "accent",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.setObjectName("Badge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_variant(variant)

    def set_variant(self, variant: str) -> None:
        self.setProperty("variant", variant)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_value(self, text: str) -> None:
        self.setText(text)


class StatCard(Card):
    def __init__(
        self,
        label: str,
        value: str = "0",
        hint: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, padding=16, spacing=4, object_name="StatCard")
        self.setMinimumWidth(120)
        top = QHBoxLayout()
        self.label = QLabel(label)
        self.label.setProperty("role", "caption")
        top.addWidget(self.label)
        top.addStretch(1)
        self._icon_label = QLabel()
        self._icon_label.setPixmap(icon("flow", mode="dark").pixmap(16, 16))
        top.addWidget(self._icon_label)
        self._layout.addLayout(top)
        self.value_label = QLabel(value)
        self.value_label.setProperty("role", "stat")
        self._layout.addWidget(self.value_label)
        self.hint_label = QLabel(hint)
        self.hint_label.setProperty("role", "hint")
        self._layout.addWidget(self.hint_label)

    def set_value(self, value: str, hint: str | None = None) -> None:
        self.value_label.setText(value)
        if hint is not None:
            self.hint_label.setText(hint)


class EmptyState(QWidget):
    actionClicked = Signal()

    def __init__(
        self,
        title: str = "暂无内容",
        description: str = "",
        *,
        action_text: str = "",
        icon_name: str = "book",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 36, 24, 36)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label = QLabel()
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setPixmap(icon(icon_name, mode="dark", size=40).pixmap(48, 48))
        layout.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)
        self.description_label = QLabel(description)
        self.description_label.setProperty("role", "muted")
        self.description_label.setWordWrap(True)
        self.description_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.description_label.setMaximumWidth(420)
        layout.addWidget(self.description_label, 0, Qt.AlignmentFlag.AlignHCenter)
        self.action_button: QPushButton | None = None
        if action_text:
            self.action_button = QPushButton(action_text)
            self.action_button.setProperty("variant", "primary")
            self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.action_button.clicked.connect(self.actionClicked)
            layout.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignHCenter)


class ScrollPage(QScrollArea):
    """Vertically scrolling page body with consistent margins."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        margins: tuple[int, int, int, int] = (28, 24, 28, 28),
        spacing: int = 18,
    ) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName("ScrollBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(*margins)
        self.body_layout.setSpacing(spacing)
        self.setWidget(self.body)

    def add(self, widget: QWidget) -> None:
        self.body_layout.addWidget(widget)

    def add_layout(self, layout: QLayout) -> None:
        self.body_layout.addLayout(layout)


class FlowLayout(QLayout):
    """Responsive wrapping layout used for project/character cards."""

    def __init__(
        self,
        parent: QWidget | None = None,
        margin: int = 0,
        h_spacing: int = 14,
        v_spacing: int = 14,
    ) -> None:
        super().__init__(parent)
        self._items: list[QWidgetItem] = []
        self._h_space = h_spacing
        self._v_space = v_spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item: QWidgetItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QWidgetItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QWidgetItem | None:  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def clear(self) -> None:
        while self._items:
            item = self._items.pop()
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        for item in self._items:
            widget = item.widget()
            if widget is not None and not widget.isVisible():
                continue
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_space
            if next_x - self._h_space > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + self._v_space
                next_x = x + hint.width() + self._h_space
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class Toast(QFrame):
    """Small non-blocking notification over the main window."""

    _ICONS = {
        "info": ("info", "accent"),
        "success": ("check", "success"),
        "warning": ("warning", "warning"),
        "error": ("warning", "danger"),
    }

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setVisible(False)
        self.setMaximumWidth(520)
        self.setMinimumWidth(260)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 12, 10)
        layout.setSpacing(10)
        self.icon_label = QLabel()
        layout.addWidget(self.icon_label)
        self.text_label = QLabel()
        self.text_label.setWordWrap(True)
        layout.addWidget(self.text_label, 1)
        close_button = QPushButton("✕")
        close_button.setObjectName("IconButton")
        close_button.setFixedSize(24, 24)
        close_button.clicked.connect(self.hide)
        layout.addWidget(close_button)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._anim = QPropertyAnimation(self._opacity, b"opacity", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def show_message(self, message: str, level: str = "info", duration: int = 3600) -> None:
        palette = current_palette(
            QApplication.instance().property("theme_mode") or "dark"
            if QApplication.instance()
            else "dark"
        )
        icon_name, color_key = self._ICONS.get(level, self._ICONS["info"])
        self.icon_label.setPixmap(icon(icon_name, palette[color_key], 18).pixmap(20, 20))
        self.text_label.setText(message)
        self.setStyleSheet(
            f"QFrame#Toast {{ background: {palette['surface']}; "
            f"border: 1px solid {palette['border']}; "
            f"border-left: 3px solid {palette[color_key]}; border-radius: 9px; }}"
            "QLabel { border: none; background: transparent; }"
        )
        self.adjustSize()
        self._reposition()
        self._opacity.setOpacity(0.0)
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer.start(duration)

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        margin = 28
        x = max(12, parent.width() - self.width() - margin)
        y = max(12, parent.height() - self.height() - margin - 26)
        self.move(x, y)

    def parent_resized(self) -> None:
        if self.isVisible():
            self._reposition()


class ConfirmDialog(QDialog):
    def __init__(
        self,
        title: str,
        message: str,
        parent: QWidget | None = None,
        *,
        confirm_text: str = "确认",
        danger: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setStyleSheet("font-size: 17px; font-weight: 600;")
        layout.addWidget(heading)
        body = QLabel(message)
        body.setProperty("role", "muted")
        body.setWordWrap(True)
        layout.addWidget(body)
        layout.addSpacing(6)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        confirm = QPushButton(confirm_text)
        confirm.setProperty("variant", "danger" if danger else "primary")
        confirm.clicked.connect(self.accept)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)

    @staticmethod
    def ask(
        parent: QWidget | None,
        title: str,
        message: str,
        *,
        confirm_text: str = "确认",
        danger: bool = False,
    ) -> bool:
        dialog = ConfirmDialog(
            title, message, parent, confirm_text=confirm_text, danger=danger
        )
        return dialog.exec() == QDialog.DialogCode.Accepted


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFrameShape(QFrame.Shape.HLine)
    return line


def apply_shadow(widget: QWidget, blur: int = 24, y_offset: int = 4) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, y_offset)
    effect.setColor(QColor(0, 0, 0, 90))
    widget.setGraphicsEffect(effect)


def set_button_variant(button: QPushButton, variant: str) -> None:
    button.setProperty("variant", variant)
    button.style().unpolish(button)
    button.style().polish(button)


def tool_button(
    text: str = "",
    icon_name: str = "",
    *,
    variant: str = "secondary",
    tooltip: str = "",
    mode: str = "dark",
) -> QPushButton:
    button = QPushButton(text)
    if icon_name:
        button.setIcon(icon(icon_name, mode=mode))
    button.setProperty("variant", variant)
    if tooltip:
        button.setToolTip(tooltip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def add_row(layout: QHBoxLayout, widgets: Iterable[QWidget], *, stretch_last: bool = True) -> None:
    for widget in widgets:
        layout.addWidget(widget)
    if stretch_last:
        layout.addStretch(1)
