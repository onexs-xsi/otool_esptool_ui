"""Footer update entry and the modal release/install flow."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from http.client import HTTPException
from math import cos, sin, tau
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
from time import monotonic

from PyQt6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt, QTimer, QUrl, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QPainter, QPainterPath, QTextDocument
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QProgressBar, QPushButton, QStyle, QTextBrowser, QVBoxLayout, QWidget,
)

from .app_update import (
    PreparedUpdate, Release, UpdateError, can_install_release, discard_download, download_release, prepare_installation,
)
from .constants import APP_VERSION


def _animations_enabled(widget: QWidget) -> bool:
    if sys.platform == "win32":
        enabled = ctypes.c_int()
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0):
            return bool(enabled.value)
    return bool(widget.style().styleHint(QStyle.StyleHint.SH_Widget_Animate))


class NewReleaseButton(QPushButton):
    """Play a single, clipped celebration when the update becomes visible."""

    _BURSTS = ((0, 11, 10), (680, 51, 17), (1430, 10, 17))
    _COLORS = (QColor("#e46b9c"), QColor("#b94f80"), QColor("#b67938"))

    def __init__(self, parent: QWidget) -> None:
        super().__init__("NEW!", parent)
        self.setObjectName("releaseUpdateButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(62, 28)
        self.setAccessibleName("查看新版本")
        self._played = False
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(2400)
        self._animation.setStartValue(0)
        self._animation.setEndValue(2400)
        self._animation.valueChanged.connect(self.update)
        self._animation.finished.connect(self.update)
        self.hide()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._played:
            self._played = True
            if _animations_enabled(self):
                self._animation.start()

    def hideEvent(self, event) -> None:
        self._animation.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self._animation.state() != QVariantAnimation.State.Running:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()).adjusted(2, 2, -2, -2), 12, 12)
        painter.setClipPath(clip)
        painter.setPen(Qt.PenStyle.NoPen)
        elapsed = self._animation.currentTime()
        for start, x, y in self._BURSTS:
            age = (elapsed - start) / 970
            if not 0 <= age < 1:
                continue
            for ray in range(6):
                angle = ray * tau / 6 + start * 0.001
                distance = 2 + (8 + ray % 3) * (1 - (1 - age) ** 2)
                painter.setBrush(self._COLORS[ray % len(self._COLORS)])
                painter.setOpacity(0.72 * min(1.0, age * 10) * (1 - age))
                point = QPointF(x + cos(angle) * distance, y + sin(angle) * distance + 2 * age)
                painter.drawEllipse(point, 0.85, 0.85)
        painter.end()


class _ReleaseNotes(QTextBrowser):
    def loadResource(self, resource_type, name):
        # Markdown is remote text. Do not load images or local/remote resources.
        return QByteArray()


class ReleaseUpdateDialog(QDialog):
    _download_progress = pyqtSignal(int)
    _download_finished = pyqtSignal(object, str)

    def __init__(
        self,
        release: Release,
        blocked_reason: Callable[[], str],
        install: Callable[[PreparedUpdate], str],
        restart_arguments: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._release = release
        self._blocked_reason = blocked_reason
        self._install = install
        self._restart_arguments = restart_arguments
        self._download_cancel: Event | None = None
        self._prepared: PreparedUpdate | None = None
        self._prepare_deadline = 0.0
        self.setWindowTitle("版本更新")
        self.setModal(True)
        self.resize(QSize(620, 450).boundedTo(self.screen().availableGeometry().size() - QSize(24, 48)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)
        versions = QLabel(f"v{APP_VERSION}  →  {release.tag}")
        versions.setObjectName("releaseVersion")
        layout.addWidget(versions)

        notes = _ReleaseNotes(self)
        notes.setObjectName("releaseNotes")
        notes.setAccessibleName("版本更新内容")
        notes.setOpenLinks(False)
        notes.setOpenExternalLinks(False)
        notes.document().setMarkdown(
            release.notes.strip() or "这个版本没有附带更新说明。",
            QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML,
        )
        layout.addWidget(notes, 1)

        self._status = QLabel()
        self._status.setObjectName("releaseStatus")
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        self._status.setMinimumHeight(self._status.fontMetrics().lineSpacing() * 2)
        layout.addWidget(self._status)
        self._progress = QProgressBar()
        self._progress.setObjectName("releaseProgress")
        self._progress.setAccessibleName("更新包下载进度")
        policy = self._progress.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self._progress.setSizePolicy(policy)
        self._progress.hide()
        layout.addWidget(self._progress)

        buttons = QDialogButtonBox()
        self._github = buttons.addButton("查看 GitHub", QDialogButtonBox.ButtonRole.ActionRole)
        self._later = buttons.addButton("以后再说", QDialogButtonBox.ButtonRole.RejectRole)
        self._start = buttons.addButton("开始更新", QDialogButtonBox.ButtonRole.AcceptRole)
        self._start.setObjectName("primaryButton")
        self._later.setMinimumWidth(self._later.fontMetrics().horizontalAdvance("取消下载") + 28)
        self._github.clicked.connect(self._open_release)
        buttons.rejected.connect(self.reject)
        self._start.clicked.connect(self._start_download)
        layout.addWidget(buttons)
        self._later.setDefault(True)
        self._later.setFocus()

        supported = can_install_release()
        self._start.setEnabled(supported and release.asset is not None)
        if not supported:
            self._status.setText("请在 GitHub Releases 下载 Windows 发布包。")
        elif release.asset is None:
            self._status.setText("这个版本缺少可校验的 Windows 发布包。可以到 GitHub 查看发布详情。")
        else:
            size_mb = release.asset.size / (1024 * 1024)
            self._status.setText(f"下载 {size_mb:.1f} MB 后会自动重启。请先保存正在编辑的配置。")

        self._download_progress.connect(self._progress.setValue)
        self._download_finished.connect(self._on_download_finished)
        self._prepare_timer = QTimer(self)
        self._prepare_timer.setInterval(100)
        self._prepare_timer.timeout.connect(self._check_installer)

    def _open_release(self) -> None:
        if not QDesktopServices.openUrl(QUrl(self._release.url)):
            self._status.setText("无法打开 GitHub 发布页，请检查默认浏览器设置。")

    def _start_download(self) -> None:
        asset = self._release.asset
        if asset is None or not self._start.isEnabled():
            return
        error = self._blocked_reason()
        if error:
            self._status.setText(error)
            return
        cancel = self._download_cancel = Event()
        self._github.setEnabled(False)
        self._start.setEnabled(False)
        self._later.setText("取消下载")
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.show()
        self._status.setText("正在从 GitHub 下载更新包…")

        def run() -> None:
            try:
                directory = download_release(asset, cancel, self._download_progress.emit)
            except UpdateError as exc:
                self._download_finished.emit(None, str(exc))
            except (OSError, HTTPException, ValueError):
                self._download_finished.emit(None, "下载失败，请检查网络连接和磁盘空间后重试。")
            else:
                self._download_finished.emit(directory, "")

        Thread(target=run, daemon=True, name="release-download").start()

    def _on_download_finished(self, directory: Path | None, error: str) -> None:
        cancel = self._download_cancel
        self._download_cancel = None
        if cancel is not None and cancel.is_set():
            if directory is not None:
                discard_download(directory)
            super().reject()
            return
        if directory is None:
            self._show_error(error)
            return
        error = self._blocked_reason()
        if error:
            discard_download(directory)
            self._show_error(error)
            return
        try:
            self._prepared = prepare_installation(directory, self._release, self._restart_arguments)
        except (OSError, UpdateError) as exc:
            discard_download(directory)
            self._show_error(str(exc) if isinstance(exc, UpdateError) else "无法启动 Windows 更新程序，请重试。")
            return
        self._progress.setRange(0, 0)
        self._status.setText("更新包校验通过。受保护的目录会请求 Windows 管理员权限。")
        self._later.setText("以后再说")
        self._prepare_deadline = monotonic() + 15
        self._prepare_timer.start()

    def _check_installer(self) -> None:
        prepared = self._prepared
        if prepared is None:
            return
        error = ""
        if prepared.process.poll() is not None:
            try:
                error = (prepared.directory / "error.txt").read_text(encoding="utf-8")
            except OSError:
                error = "Windows 更新程序启动失败，请重试。"
        elif (prepared.directory / "ready").is_file():
            error = self._install(prepared)
            if not error:
                self._prepare_timer.stop()
                self._prepared = None
                self.accept()
                return
        elif monotonic() >= self._prepare_deadline:
            error = "等待 Windows 更新程序超时，请重试。"
        if error:
            self._cancel_preparation()
            self._show_error(error)

    def _cancel_preparation(self) -> None:
        self._prepare_timer.stop()
        if self._prepared is not None:
            try:
                self._prepared.cancel()
            except (OSError, subprocess.TimeoutExpired):
                pass
            self._prepared = None

    def _show_error(self, message: str) -> None:
        self._status.setText(message)
        self._progress.hide()
        self._later.setText("以后再说")
        self._github.setEnabled(True)
        self._start.setEnabled(True)

    def reject(self) -> None:
        if self._download_cancel is not None:
            self._download_cancel.set()
            self._status.setText("正在取消下载…")
            self._later.setEnabled(False)
            return
        self._cancel_preparation()
        super().reject()
