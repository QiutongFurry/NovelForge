"""Modern light/dark themes for the application."""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

THEMES = {"dark": "深色", "light": "浅色"}

PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "window": "#0E1117",
        "sidebar": "#11151D",
        "surface": "#161B26",
        "surface_alt": "#1B2230",
        "surface_hover": "#232B3A",
        "border": "#2A3243",
        "border_strong": "#3A455C",
        "text": "#E9EDF7",
        "text_muted": "#8E99AF",
        "accent": "#7C9DFF",
        "accent_hover": "#96B0FF",
        "accent_soft": "rgba(124, 157, 255, 0.14)",
        "success": "#45D5A5",
        "warning": "#F1C46B",
        "danger": "#FF8296",
        "selection": "#26344F",
        "shadow": "rgba(0, 0, 0, 0.35)",
    },
    "light": {
        "window": "#F3F5FA",
        "sidebar": "#FFFFFF",
        "surface": "#FFFFFF",
        "surface_alt": "#F6F8FC",
        "surface_hover": "#EDF1F8",
        "border": "#E2E7F0",
        "border_strong": "#C9D1E0",
        "text": "#172033",
        "text_muted": "#68738A",
        "accent": "#4F6EF7",
        "accent_hover": "#3B5BEA",
        "accent_soft": "rgba(79, 110, 247, 0.10)",
        "success": "#0E9F6E",
        "warning": "#B7791F",
        "danger": "#DC3545",
        "selection": "#E7ECFF",
        "shadow": "rgba(20, 30, 60, 0.12)",
    },
}


def current_palette(mode: str = "dark") -> dict[str, str]:
    return PALETTES.get(mode, PALETTES["dark"])


_QSS = """
* {
    outline: none;
}
QWidget {
    color: @text;
    background: transparent;
    font-size: 13px;
}
QMainWindow, QDialog {
    background: @window;
}
QWidget#CentralHost, QWidget#PageRoot {
    background: @window;
}
QFrame#Sidebar {
    background: @sidebar;
    border-right: 1px solid @border;
}
QFrame#TopBar {
    background: @surface;
    border-bottom: 1px solid @border;
}
QFrame#Card, QFrame#Panel {
    background: @surface;
    border: 1px solid @border;
    border-radius: 12px;
}
QFrame#SoftCard {
    background: @surface_alt;
    border: 1px solid @border;
    border-radius: 10px;
}
QFrame#StatCard {
    background: @surface;
    border: 1px solid @border;
    border-radius: 12px;
}
QFrame#Divider {
    background: @border;
    max-height: 1px;
    min-height: 1px;
}
QLabel {
    background: transparent;
}
QLabel#PageTitle {
    font-size: 24px;
    font-weight: 700;
}
QLabel#PageSubtitle {
    font-size: 13px;
    color: @text_muted;
}
QLabel#SectionTitle {
    font-size: 15px;
    font-weight: 600;
}
QLabel#CardTitle {
    font-size: 14px;
    font-weight: 600;
}
QLabel#Muted, QLabel[role="muted"] {
    color: @text_muted;
}
QLabel[role="caption"] {
    color: @text_muted;
    font-size: 12px;
}
QLabel[role="stat"] {
    font-size: 24px;
    font-weight: 700;
    color: @text;
}
QLabel[role="hint"] {
    color: @text_muted;
    font-size: 12px;
}
QLabel#BrandTitle {
    font-size: 17px;
    font-weight: 700;
    letter-spacing: 1px;
}
QLabel#BrandSubtitle {
    color: @text_muted;
    font-size: 10px;
    letter-spacing: 1px;
}
QLabel#Badge {
    border-radius: 10px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
    background: @accent_soft;
    color: @accent;
}
QLabel#Badge[variant="success"] {
    background: rgba(69, 213, 165, 0.14);
    color: @success;
}
QLabel#Badge[variant="warning"] {
    background: rgba(241, 196, 107, 0.14);
    color: @warning;
}
QLabel#Badge[variant="danger"] {
    background: rgba(255, 130, 150, 0.14);
    color: @danger;
}
QLabel#Badge[variant="neutral"] {
    background: @surface_alt;
    color: @text_muted;
    border: 1px solid @border;
}
QPushButton {
    background: @surface_alt;
    color: @text;
    border: 1px solid @border;
    border-radius: 8px;
    padding: 7px 14px;
    font-weight: 500;
    min-height: 18px;
}
QPushButton:hover {
    background: @surface_hover;
    border-color: @border_strong;
}
QPushButton:pressed {
    background: @selection;
}
QPushButton:disabled {
    color: @text_muted;
    background: @surface_alt;
    border-color: @border;
}
QPushButton[variant="primary"] {
    background: @accent;
    color: #0C1220;
    border: 1px solid @accent;
    font-weight: 700;
}
QPushButton[variant="primary"]:hover {
    background: @accent_hover;
    border-color: @accent_hover;
}
QPushButton[variant="primary"]:disabled {
    background: @border_strong;
    border-color: @border_strong;
    color: @text_muted;
}
QPushButton[variant="danger"] {
    color: @danger;
    border-color: rgba(255, 130, 150, 0.35);
    background: rgba(255, 130, 150, 0.08);
}
QPushButton[variant="danger"]:hover {
    background: rgba(255, 130, 150, 0.16);
}
QPushButton[variant="ghost"] {
    background: transparent;
    border: 1px solid transparent;
    color: @text_muted;
}
QPushButton[variant="ghost"]:hover {
    background: @surface_hover;
    color: @text;
}
QPushButton[nav="true"] {
    text-align: left;
    padding: 9px 12px;
    border-radius: 8px;
    background: transparent;
    border: 1px solid transparent;
    color: @text_muted;
    font-weight: 500;
}
QPushButton[nav="true"]:hover {
    background: @surface_hover;
    color: @text;
}
QPushButton[nav="true"]:checked {
    background: @accent_soft;
    color: @accent;
    border-color: rgba(124, 157, 255, 0.25);
    font-weight: 600;
}
QPushButton#IconButton {
    padding: 5px;
    border-radius: 7px;
    background: transparent;
    border: 1px solid transparent;
}
QPushButton#IconButton:hover {
    background: @surface_hover;
}
QLineEdit, QPlainTextEdit, QTextEdit, QTextBrowser, QSpinBox, QDoubleSpinBox,
QComboBox, QDateEdit, QTimeEdit {
    background: @surface_alt;
    border: 1px solid @border;
    border-radius: 8px;
    padding: 7px 10px;
    color: @text;
    selection-background-color: @selection;
    selection-color: @text;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid @accent;
    background: @surface;
}
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {
    color: @text_muted;
    background: @surface_alt;
}
QLineEdit[error="true"] {
    border: 1px solid @danger;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background: @surface;
    border: 1px solid @border;
    border-radius: 8px;
    selection-background-color: @selection;
    outline: 0;
    padding: 4px;
}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 16px;
    border: none;
    background: transparent;
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: @border_strong;
    border-radius: 4px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover {
    background: @text_muted;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: @border_strong;
    border-radius: 4px;
    min-width: 32px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
QSplitter::handle {
    background: transparent;
}
QSplitter::handle:horizontal {
    width: 8px;
}
QSplitter::handle:vertical {
    height: 8px;
}
QListWidget, QTreeWidget, QTableWidget, QListView, QTreeView, QTableView {
    background: transparent;
    border: none;
    outline: 0;
}
QListWidget::item, QTreeWidget::item {
    padding: 7px 8px;
    border-radius: 7px;
    margin: 1px 2px;
}
QListWidget::item:hover, QTreeWidget::item:hover {
    background: @surface_hover;
}
QListWidget::item:selected, QTreeWidget::item:selected {
    background: @selection;
    color: @text;
}
QTableWidget {
    background: @surface;
    border: 1px solid @border;
    border-radius: 10px;
    gridline-color: @border;
    selection-background-color: @selection;
    selection-color: @text;
}
QHeaderView::section {
    background: @surface_alt;
    color: @text_muted;
    border: none;
    border-bottom: 1px solid @border;
    padding: 8px;
    font-weight: 600;
}
QTabWidget::pane {
    border: 1px solid @border;
    border-radius: 10px;
    background: @surface;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    color: @text_muted;
    padding: 8px 14px;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}
QTabBar::tab:hover {
    color: @text;
}
QTabBar::tab:selected {
    color: @accent;
    border-bottom: 2px solid @accent;
}
QGroupBox {
    border: 1px solid @border;
    border-radius: 10px;
    margin-top: 12px;
    padding-top: 12px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: @text_muted;
}
QCheckBox {
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid @border_strong;
    border-radius: 4px;
    background: @surface_alt;
}
QCheckBox::indicator:checked {
    background: @accent;
    border-color: @accent;
    image: none;
}
QSlider::groove:horizontal {
    height: 4px;
    background: @border;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 14px;
    margin: -5px 0;
    border-radius: 7px;
    background: @accent;
}
QProgressBar {
    background: @surface_alt;
    border: none;
    border-radius: 4px;
    text-align: center;
    color: @text_muted;
    font-size: 11px;
    min-height: 8px;
    max-height: 8px;
}
QProgressBar::chunk {
    background: @accent;
    border-radius: 4px;
}
QMenu {
    background: @surface;
    border: 1px solid @border;
    border-radius: 8px;
    padding: 5px;
}
QMenu::item {
    padding: 7px 24px 7px 12px;
    border-radius: 5px;
}
QMenu::item:selected {
    background: @selection;
}
QToolTip {
    background: @surface;
    color: @text;
    border: 1px solid @border;
    padding: 6px 8px;
    border-radius: 6px;
}
QStatusBar {
    background: @surface;
    border-top: 1px solid @border;
    color: @text_muted;
    font-size: 12px;
}
QStatusBar::item {
    border: none;
}
QToolBar {
    background: transparent;
    border: none;
    spacing: 6px;
}
QTextBrowser {
    background: @surface_alt;
}
QPlainTextEdit#ChapterEditor {
    font-size: 15px;
    line-height: 190%;
    padding: 20px 24px;
    background: @surface;
    border: 1px solid @border;
    border-radius: 10px;
    selection-background-color: @selection;
}
QPlainTextEdit#ChapterEditor:focus {
    border-color: @border_strong;
}
QListWidget#ChapterList {
    background: @surface;
    border: 1px solid @border;
    border-radius: 10px;
    padding: 6px;
}
QListWidget#ChapterList::item {
    padding: 10px;
}
QTextEdit#PromptPreview {
    font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI";
    font-size: 12px;
    line-height: 160%;
}
QWidget#SidebarFooter {
    border-top: 1px solid @border;
}
QPushButton#ThemeToggle {
    background: @surface_alt;
    border: 1px solid @border;
    border-radius: 16px;
    padding: 5px 11px;
    color: @text_muted;
}
QPushButton#ThemeToggle:hover {
    color: @text;
    border-color: @border_strong;
}
QWidget#ScrollBody {
    background: transparent;
}
"""


def apply_theme(app: QApplication, mode: str = "dark") -> None:
    """Apply a complete application-wide stylesheet and QPalette."""

    palette = current_palette(mode)
    qss = _QSS
    # Longer keys first: otherwise "@text" would corrupt "@text_muted".
    for key in sorted(palette, key=len, reverse=True):
        qss = qss.replace(f"@{key}", palette[key])
    app.setStyle("Fusion")
    app.setProperty("theme_mode", mode)
    qt_palette = QPalette()
    qt_palette.setColor(QPalette.ColorRole.Window, QColor(palette["window"]))
    qt_palette.setColor(QPalette.ColorRole.WindowText, QColor(palette["text"]))
    qt_palette.setColor(QPalette.ColorRole.Base, QColor(palette["surface"]))
    qt_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(palette["surface_alt"]))
    qt_palette.setColor(QPalette.ColorRole.Text, QColor(palette["text"]))
    qt_palette.setColor(QPalette.ColorRole.Button, QColor(palette["surface_alt"]))
    qt_palette.setColor(QPalette.ColorRole.ButtonText, QColor(palette["text"]))
    qt_palette.setColor(QPalette.ColorRole.Highlight, QColor(palette["accent"]))
    qt_palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#0C1220"))
    qt_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(palette["surface"]))
    qt_palette.setColor(QPalette.ColorRole.ToolTipText, QColor(palette["text"]))
    qt_palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette["text_muted"]))
    app.setPalette(qt_palette)

    font = QFont()
    font.setFamilies(
        ["Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", "Segoe UI", "Sans Serif"]
    )
    font.setPointSize(10)
    app.setFont(font)
    app.setStyleSheet(qss)


def palette_color(mode: str, key: str) -> str:
    return current_palette(mode).get(key, "#7C9DFF")


def build_palette(mode: str) -> dict[str, Any]:
    """Small helper kept for pages that need colors without importing QSS."""

    return dict(current_palette(mode))
