"""Dependency-free SVG line icons tinted for the current theme."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from novel_ai.ui.theme import current_palette

_PATHS: dict[str, str] = {
    "home": '<path d="M3.5 11.2 12 4l8.5 7.2"/><path d="M5.8 9.8V20h12.4V9.8"/><path d="M9.5 20v-5.2h5V20"/>',
    "book": '<path d="M5 5.2A2.2 2.2 0 0 1 7.2 3H19v18H7.2A2.2 2.2 0 0 0 5 23.2z"/><path d="M5 20.8A2.2 2.2 0 0 1 7.2 18.6H19"/><path d="M9 7.5h6"/>',
    "users": '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 20c.3-3.4 2.6-5.4 5.5-5.4s5.2 2 5.5 5.4"/><path d="M16 5.2a3.1 3.1 0 0 1 0 6"/><path d="M17.5 14.8c2.1.4 3.5 2.2 3.7 4.8"/>',
    "outline": '<path d="M5 5h4M5 12h4M5 19h4"/><path d="M12 5h7M12 12h7M12 19h7"/><circle cx="9" cy="5" r=".8"/><circle cx="9" cy="12" r=".8"/><circle cx="9" cy="19" r=".8"/>',
    "editor": '<path d="M4 20l4.2-.9L20 7.3 16.7 4 4.9 15.8z"/><path d="M14.5 6.2l3.3 3.3"/>',
    "key": '<circle cx="8" cy="8" r="4.2"/><path d="M11 11l8.5 8.5"/><path d="M16.2 16.2l2-2M18.5 18.5l2-2"/>',
    "prompt": '<path d="M4.2 6.2h9.6v9.2H9l-3.4 3v-3H4.2z"/><path d="M16 10.5h3.8v9.1l-2.7-2.3H16z"/><path d="M7 9.2h4M7 11.8h6"/>',
    "memory": '<ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v5.2c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V6"/><path d="M4.5 11.2v5.2c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-5.2"/>',
    "logs": '<path d="M3.5 12h4l2.2-6 4.2 12 2.3-6h4.3"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "trash": '<path d="M4.5 6.5h15"/><path d="M9 6.5V4.8h6v1.7"/><path d="M6.8 6.5l.9 13h8.6l.9-13"/><path d="M10 10.5v5M14 10.5v5"/>',
    "save": '<path d="M4.5 4.5h12l3 3v12h-15z"/><path d="M8 4.5v5h7v-5"/><path d="M8 19.5v-6h8v6"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2 5.4"/><path d="M20 4.5V11h-6.2"/>',
    "download": '<path d="M12 4v11"/><path d="M7.5 11.5 12 16l4.5-4.5"/><path d="M4.5 19.5h15"/>',
    "stop": '<rect x="6.2" y="6.2" width="11.6" height="11.6" rx="2"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2.8v2M12 19.2v2M2.8 12h2M19.2 12h2M5.4 5.4 6.8 6.8M17.2 17.2l1.4 1.4M18.6 5.4l-1.4 1.4M6.8 17.2l-1.4 1.4"/>',
    "moon": '<path d="M20 14.5A8.3 8.3 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 3.4v2.2M12 18.4v2.2M3.4 12h2.2M18.4 12h2.2M6 6l1.5 1.5M16.5 16.5 18 18M18 6l-1.5 1.5M7.5 16.5 6 18"/>',
    "close": '<path d="M6 6l12 12M18 6 6 18"/>',
    "check": '<path d="M4.5 12.5 9.5 17.5 19.5 6.5"/>',
    "warning": '<path d="M12 4 21 20H3z"/><path d="M12 9.5v4.8M12 17.4v.2"/>',
    "info": '<circle cx="12" cy="12" r="8.5"/><path d="M12 10.6v5.2M12 7.9v.2"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.2"/><path d="m15.2 15.2 4.8 4.8"/>',
    "arrow-up": '<path d="M12 19V5M6.5 10.5 12 5l5.5 5.5"/>',
    "arrow-down": '<path d="M12 5v14M6.5 13.5 12 19l5.5-5.5"/>',
    "play": '<path d="M8 5.5 19 12 8 18.5z"/>',
    "history": '<path d="M4 5v5h5"/><path d="M4.8 10A8 8 0 1 1 4 14"/><path d="M12 8v4.6l3 1.8"/>',
    "folder": '<path d="M3.5 6.5h6l2 2h9v10.5h-17z"/>',
    "sparkles": '<path d="M12 3.5 13.8 9l5.5 1.8-5.5 1.8L12 18l-1.8-5.4L4.7 10.8 10.2 9z"/><path d="M19 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/>',
    "flow": '<rect x="3.5" y="4" width="6" height="5" rx="1.2"/><rect x="14.5" y="4" width="6" height="5" rx="1.2"/><rect x="9" y="15" width="6" height="5" rx="1.2"/><path d="M9.5 9 12 15M14.5 9 12 15"/>',
    "chevron-down": '<path d="m6 9.5 6 6 6-6"/>',
    "external": '<path d="M14 4.5h5.5V10"/><path d="M19.5 4.5 11 13"/><path d="M18 14.5v5h-14v-14h5"/>',
    "undo": '<path d="M4.5 9.5h9a5 5 0 0 1 0 10H9"/><path d="M8.5 5.5 4.5 9.5l4 4"/>',
}


@lru_cache(maxsize=256)
def _render(name: str, color: str, size: int) -> QPixmap:
    path = _PATHS.get(name, '<circle cx="12" cy="12" r="3.5"/>')
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
        'viewBox="0 0 24 24" fill="none" stroke="'
        + color
        + '" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
        + path
        + "</svg>"
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return pixmap


def icon(name: str, color: str | None = None, size: int = 20, mode: str = "dark") -> QIcon:
    """Return a tinted icon.

    ``mode`` is only used when an explicit color is not supplied.
    """

    tint = color or current_palette(mode)["text_muted"]
    result = QIcon()
    # Render at 2x for crisp output on high-DPI displays.
    pixmap = _render(name, tint, max(16, size * 2))
    pixmap.setDevicePixelRatio(2.0)
    result.addPixmap(pixmap)
    return result


def available_icons() -> list[str]:
    return sorted(_PATHS)
