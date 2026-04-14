import threading
from functools import lru_cache
from typing import Callable
from urllib.request import Request, urlopen

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap

from .constants import (
    APP_GITHUB_AVATAR_URL,
    APP_TITLE,
    APP_VERSION,
    _resolve_packaged_avatar_path,
    _load_packaged_avatar,
    _download_github_avatar,
)


def _build_avatar_icon(avatar_bytes: bytes, icon_size: int = 20) -> QIcon | None:
    pixmap = QPixmap()
    if not pixmap.loadFromData(avatar_bytes):
        return None

    target_pixels = max(icon_size * 4, 96)
    source = pixmap.scaled(
        target_pixels,
        target_pixels,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )

    rounded = QPixmap(target_pixels, target_pixels)
    rounded.fill(Qt.GlobalColor.transparent)

    painter = QPainter(rounded)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip_path = QPainterPath()
    clip_path.addEllipse(0, 0, target_pixels, target_pixels)
    painter.setClipPath(clip_path)
    source_x = max(0, (source.width() - target_pixels) // 2)
    source_y = max(0, (source.height() - target_pixels) // 2)
    painter.drawPixmap(0, 0, source, source_x, source_y, target_pixels, target_pixels)
    painter.end()

    return QIcon(rounded)


def _build_fallback_github_icon() -> QIcon:
    icon_size = 20
    pixmap = QPixmap(icon_size, icon_size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#111827"))
    painter.drawRoundedRect(0, 0, icon_size - 1, icon_size - 1, 6, 6)

    font = QFont()
    font.setFamilies(["Segoe UI", "Helvetica Neue", "Arial", "sans-serif"])
    font.setPointSize(8)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#ffffff"))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "GH")
    painter.end()

    return QIcon(pixmap)


def _build_github_icon() -> QIcon:
    """Return local packaged avatar icon or fallback. No network I/O."""
    avatar_bytes = _load_packaged_avatar()
    if avatar_bytes:
        avatar_icon = _build_avatar_icon(avatar_bytes)
        if avatar_icon is not None:
            return avatar_icon
    return _build_fallback_github_icon()


def _fetch_remote_avatar_bytes(on_success: Callable[[bytes], None]) -> None:
    """Download avatar in a daemon thread; call *on_success(bytes)* on success.

    The callback is invoked from the background thread — callers must
    marshal to the main thread (e.g. via ``QTimer.singleShot``) before
    touching any Qt widgets.
    """

    def _worker() -> None:
        avatar_bytes = _download_github_avatar()
        if avatar_bytes:
            on_success(avatar_bytes)

    threading.Thread(target=_worker, daemon=True).start()
