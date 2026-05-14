import re

from PyQt6.QtCore import QProcess, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QTextCursor
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

try:
    from .models import DeviceInfo
except ImportError:
    from models import DeviceInfo  # type: ignore[no-redef]


class SpeedBadge(QWidget):
    """CPU speed, displayed as a mini fill-bar pill.  ≥360 MHz = full, <40 MHz = no fill."""

    _MIN_MHZ       = 40
    _MAX_MHZ       = 360
    _H             = 22
    _COLOR_BG      = QColor("#ede9fe")
    _COLOR_FILL    = QColor("#8b5cf6")
    _COLOR_BORDER  = QColor("#c4b5fd")
    _COLOR_LIGHT   = QColor("#ffffff")
    _COLOR_DARK    = QColor("#5b21b6")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ratio = 0.0
        self._text  = ""
        self.setFixedHeight(self._H)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set_speed(self, mhz: int) -> None:
        self._text = f"{mhz}MHz"
        if mhz < self._MIN_MHZ:
            self._ratio = 0.0
        elif mhz >= self._MAX_MHZ:
            self._ratio = 1.0
        else:
            self._ratio = (mhz - self._MIN_MHZ) / (self._MAX_MHZ - self._MIN_MHZ)
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        w = self.fontMetrics().horizontalAdvance(self._text) + 24
        return QSize(max(66, w), self._H)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect   = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 4.0

        badge_path = QPainterPath()
        badge_path.addRoundedRect(rect, radius, radius)

        # background
        painter.setClipPath(badge_path)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._COLOR_BG)
        painter.drawRect(self.rect())

        # fill
        if self._ratio > 0.0:
            fill_rect = QRectF(rect.x(), rect.y(), rect.width() * self._ratio, rect.height())
            fill_path = QPainterPath()
            fill_path.addRect(fill_rect)
            painter.setClipPath(badge_path.intersected(fill_path))
            painter.setBrush(self._COLOR_FILL)
            painter.drawRect(self.rect())

        # text: white on filled part, dark on empty part
        tr = self.rect()
        if self._ratio > 0.0:
            fill_clip = QPainterPath()
            fill_clip.addRect(QRectF(rect.x(), rect.y(), rect.width() * self._ratio, rect.height()))
            painter.setClipPath(badge_path.intersected(fill_clip))
            painter.setPen(self._COLOR_LIGHT)
            painter.drawText(tr, Qt.AlignmentFlag.AlignCenter, self._text)
        if self._ratio < 1.0:
            no_fill_clip = QPainterPath()
            no_fill_clip.addRect(QRectF(
                rect.x() + rect.width() * self._ratio, rect.y(),
                rect.width() * (1.0 - self._ratio), rect.height(),
            ))
            painter.setClipPath(badge_path.intersected(no_fill_clip))
            painter.setPen(self._COLOR_DARK)
            painter.drawText(tr, Qt.AlignmentFlag.AlignCenter, self._text)

        # border
        painter.setClipping(False)
        painter.setPen(QPen(self._COLOR_BORDER, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

        painter.end()


class ElidedLabel(QLabel):
    """QLabel that truncates overflow with '…' and shows a tooltip with the full text."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._full_text = ""

    def setText(self, text: str) -> None:
        self._full_text = text or ""
        self._refresh_elided()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_elided()

    def _refresh_elided(self) -> None:
        w = self.width()
        if w <= 0:
            super().setText(self._full_text)
            self.setToolTip("")
            return
        metrics = self.fontMetrics()
        elided = metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, w)
        super().setText(elided)
        # 仅在文字确实被截断时才显示 tooltip
        self.setToolTip(
            self._full_text
            if elided != self._full_text and self._full_text not in ("", "-")
            else ""
        )


class SplitTextProgressBar(QProgressBar):
    """进度文字按填充边界自动分色，避免蓝色进度块盖住深色文字。"""

    _BG = QColor("#eef0f5")
    _FILL = QColor("#2560e0")
    _TEXT_ON_FILL = QColor("#ffffff")
    _TEXT_ON_BG = QColor("#2560e0")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setTextVisible(True)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = min(5.0, rect.height() / 2.0)
        bar_path = QPainterPath()
        bar_path.addRoundedRect(rect, radius, radius)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._BG)
        painter.drawPath(bar_path)

        minimum = self.minimum()
        maximum = self.maximum()
        if maximum > minimum:
            ratio = (self.value() - minimum) / (maximum - minimum)
        else:
            ratio = 0.0
        ratio = max(0.0, min(1.0, ratio))
        fill_width = rect.width() * ratio

        if fill_width > 0.0:
            fill_clip = QPainterPath()
            fill_clip.addRect(QRectF(rect.x(), rect.y(), fill_width, rect.height()))
            painter.setClipPath(bar_path.intersected(fill_clip))
            painter.setBrush(self._FILL)
            painter.drawPath(bar_path)

        text = self.text() if self.isTextVisible() else ""
        if text:
            text_rect = self.rect()
            if fill_width > 0.0:
                fill_clip = QPainterPath()
                fill_clip.addRect(QRectF(rect.x(), rect.y(), fill_width, rect.height()))
                painter.setClipPath(bar_path.intersected(fill_clip))
                painter.setPen(self._TEXT_ON_FILL)
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)
            if fill_width < rect.width():
                bg_clip = QPainterPath()
                bg_clip.addRect(QRectF(
                    rect.x() + fill_width,
                    rect.y(),
                    rect.width() - fill_width,
                    rect.height(),
                ))
                painter.setClipPath(bar_path.intersected(bg_clip))
                painter.setPen(self._TEXT_ON_BG)
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, text)

        painter.end()


class DeviceCard(QFrame):
    _BASE_WIDTH = 380
    _BASE_LOG_HEIGHT = 200

    def __init__(self, device: DeviceInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.process: QProcess | None = None
        self._process_output_buffer = ""
        self._last_stage_log = ""
        self._last_command: list = []
        self._last_command_meta: dict = {}
        self._auto_retry_count: int = 0
        self.setObjectName("deviceCard")
        self.setProperty("cardState", "idle")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        self.setFixedWidth(380)
        self._init_ui()
        self.update_device(device)

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        title_layout = QHBoxLayout()
        title_layout.setSpacing(6)

        self.status_dot = QLabel()
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setFixedSize(10, 10)
        self.new_badge = QLabel("NEW")
        self.new_badge.setObjectName("newBadge")
        self.new_badge.hide()
        self.port_badge = QLabel()
        self.port_badge.setObjectName("portBadge")
        self.title_label = ElidedLabel()
        self.title_label.setObjectName("deviceTitle")
        self.summary_label = QLabel()
        self.summary_label.setObjectName("deviceSummary")
        self.summary_label.setWordWrap(False)

        title_layout.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        title_layout.addWidget(self.title_label, 1, Qt.AlignmentFlag.AlignVCenter)
        title_layout.addWidget(self.new_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        title_layout.addWidget(self.port_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        # ── 芯片能力辽标行 ────────────────────────────────────────
        cap_row = QHBoxLayout()
        cap_row.setSpacing(4)
        cap_row.setContentsMargins(0, 0, 0, 0)

        self.cap_cores_badge = QLabel()
        self.cap_cores_badge.setObjectName("capBadgeCpu")
        self.cap_wifi_badge = QLabel()
        self.cap_wifi_badge.setObjectName("capBadgeWifi")
        self.cap_bt_badge = QLabel()
        self.cap_bt_badge.setObjectName("capBadgeBt")
        self.cap_ieee_badge = QLabel()
        self.cap_ieee_badge.setObjectName("capBadgeIeee")
        self.cap_speed_badge = SpeedBadge()

        for _b in (self.cap_cores_badge, self.cap_wifi_badge, self.cap_bt_badge,
                   self.cap_ieee_badge, self.cap_speed_badge):
            _b.hide()
            cap_row.addWidget(_b)
        cap_row.addStretch(1)

        cap_row_container = QWidget()
        cap_row_container.setFixedHeight(26)
        cap_row_container.setLayout(cap_row)

        meta_grid = QGridLayout()
        meta_grid.setHorizontalSpacing(12)
        meta_grid.setVerticalSpacing(5)

        self.chip_value = QLabel()
        self.feature_value = ElidedLabel()
        self.mac_value = QLabel()
        self.flash_value = QLabel()
        self.crystal_value = QLabel()
        self.state_value = QLabel("空闲")
        self.state_value.setObjectName("stateIdle")

        entries = [
            ("芯片", self.chip_value),
            ("特性", self.feature_value),
            ("MAC", self.mac_value),
            ("Flash", self.flash_value),
            ("晶振", self.crystal_value),
            ("状态", self.state_value),
        ]
        for index, (label, widget) in enumerate(entries):
            row = index // 2
            col = (index % 2) * 2
            key = QLabel(label)
            key.setObjectName("metaKey")
            if not isinstance(widget, ElidedLabel) and widget is not self.chip_value:
                widget.setWordWrap(True)
            meta_grid.addWidget(key, row, col)
            meta_grid.addWidget(widget, row, col + 1)

        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)

        self.erase_button = QPushButton("擦除")
        self.erase_button.setObjectName("primaryButton")
        self.flash_button = QPushButton("烧录")
        self.flash_button.setObjectName("primaryButton")
        self.reset_button = QPushButton("复位")
        self.reset_button.setObjectName("primaryButton")
        self.export_button = QPushButton("导出")
        self.export_button.setObjectName("primaryButton")
        self.efuse_button = QPushButton("eFuse")
        self.efuse_button.setObjectName("efuseButton")
        self._action_buttons = [
            ("erase", self.erase_button, "擦除"),
            ("flash", self.flash_button, "烧录"),
            ("reset", self.reset_button, "复位"),
            ("export", self.export_button, "导出"),
        ]

        action_layout.addWidget(self.erase_button)
        action_layout.addWidget(self.flash_button)
        action_layout.addWidget(self.reset_button)
        action_layout.addWidget(self.export_button)
        action_layout.addStretch(1)
        action_layout.addWidget(self.efuse_button)

        self.progress_bar = SplitTextProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("准备就绪")

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(400)
        self.log_edit.setFixedHeight(200)
        self.log_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        _log_font = QFont()
        _log_font.setFamilies(["Consolas", "Menlo", "DejaVu Sans Mono", "monospace"])
        _log_font.setStyleHint(QFont.StyleHint.TypeWriter)
        _log_font.setPointSize(10)
        self.log_edit.setFont(_log_font)

        layout.addLayout(title_layout)
        layout.addWidget(self.summary_label)
        layout.addWidget(cap_row_container)
        layout.addLayout(meta_grid)
        layout.addLayout(action_layout)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.log_edit)

    @staticmethod
    def _short_chip_name(full: str) -> str:
        """\u4ece\u5b8c\u6574\u82af\u7247\u540d\u63d0\u53d6\u6807\u51c6\u989c\u578b\uff0c\u9002\u914d\u4efb\u610f ESP \u5c04\u9891\u3002
        \u4f8b\uff1a 'ESP32-C6FH8 (QFN32) (revision v0.2)' \u2192 'ESP32-C6'
             'ESP32-S3R8 (revision v0.2)' \u2192 'ESP32-S3'
             'ESP32 (revision v0.2)' \u2192 'ESP32'
        """
        m = re.match(r'^(ESP\d+(?:-[A-Z]\d+)?)(?=[A-Z\s(]|$)', full)
        return m.group(1) if m else full

    def update_device(self, device: DeviceInfo) -> None:
        self.device = device
        self.port_badge.setText(device.port)
        self.title_label.setText(device.label)
        summary_parts = []
        if device.chip_name and device.chip_name != "未识别":
            summary_parts.append(device.chip_name)
        if device.features:
            summary_parts.append(device.features)
        self.summary_label.setText(
            " | ".join(summary_parts) if summary_parts else "等待识别设备信息"
        )
        full_chip = device.chip_name or "未识别"
        short_chip = self._short_chip_name(full_chip)
        self.chip_value.setText(short_chip)
        self.chip_value.setToolTip(full_chip if short_chip != full_chip else "")
        self.feature_value.setText(device.features or "-")
        self.mac_value.setText(device.mac or "-")
        self.flash_value.setText(device.flash_size or "-")
        self.crystal_value.setText(device.crystal or "-")
        self._update_cap_badges(device.features or "")

    # ── 能力辽标解析 ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_capabilities(features: str) -> dict:
        text = features.lower()

        # 核心数
        cores = 0
        if "dual core" in text:
            cores = 2
        elif "single core" in text:
            cores = 1
        elif "triple core" in text:
            cores = 3
        lp_core = "lp core" in text

        # WiFi
        wifi_m = re.search(r"wi-?fi\s*(\d+)?", features, re.IGNORECASE)
        wifi: str | None = None
        if wifi_m:
            gen = wifi_m.group(1)
            wifi = f"Wi-Fi {gen}" if gen else "Wi-Fi"

        # Bluetooth
        bt = bool(re.search(r"\bbt\b|\bbluetooth\b|\bble\b", text))
        ble = bool(re.search(r"\bble\b|\(le\)", text))

        # IEEE 802.15.4 / Zigbee / Thread
        ieee154 = bool(re.search(r"802\.15\.4|zigbee|thread", text))

        # 主频
        speed_m = re.search(r"(\d+)\s*MHz", features, re.IGNORECASE)
        speed: str | None = f"{speed_m.group(1)}MHz" if speed_m else None

        return {
            "cores": cores,
            "lp_core": lp_core,
            "wifi": wifi,
            "bt": bt,
            "ble": ble,
            "ieee154": ieee154,
            "speed": speed,
        }

    def _update_cap_badges(self, features: str) -> None:
        caps = self._parse_capabilities(features)

        if caps["cores"] > 0:
            core_txt = {1: "单核", 2: "双核", 3: "三核"}.get(caps["cores"], f"{caps['cores']}核")
            if caps["lp_core"]:
                core_txt += "+LP"
            self.cap_cores_badge.setText(core_txt)
            self.cap_cores_badge.show()
        else:
            self.cap_cores_badge.hide()

        if caps["wifi"]:
            self.cap_wifi_badge.setText(caps["wifi"])
            self.cap_wifi_badge.show()
        else:
            self.cap_wifi_badge.hide()

        if caps["bt"]:
            self.cap_bt_badge.setText("BT LE" if caps["ble"] else "BT")
            self.cap_bt_badge.show()
        else:
            self.cap_bt_badge.hide()

        if caps["ieee154"]:
            self.cap_ieee_badge.setText("802.15.4")
            self.cap_ieee_badge.show()
        else:
            self.cap_ieee_badge.hide()

        if caps["speed"]:
            self.cap_speed_badge.set_speed(int(caps["speed"][:-3]))
            self.cap_speed_badge.show()
        else:
            self.cap_speed_badge.hide()

    def apply_scale(self, factor: float) -> None:
        self.setFixedWidth(int(self._BASE_WIDTH * factor))
        # log height grows faster than width so the card stays tall/rectangular
        height_factor = factor ** 2.0
        self.log_edit.setFixedHeight(int(self._BASE_LOG_HEIGHT * height_factor))

    def append_log(self, message: str) -> None:
        sb = self.log_edit.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        self.log_edit.appendPlainText(message.rstrip())
        if at_bottom:
            sb.setValue(sb.maximum())

    def _set_action_button_mode(
        self,
        stop_mode: bool,
        enabled: bool = True,
        active_action: str | None = None,
    ) -> None:
        """切换擦除/烧录/复位三颗主操作按钮的空闲态与停止态。"""
        for action, button, idle_text in self._action_buttons:
            is_active = stop_mode and action == active_action
            target_name = "dangerButton" if is_active else "primaryButton"
            button.setText("停止" if is_active else idle_text)
            if button.objectName() != target_name:
                button.setObjectName(target_name)
                button.style().unpolish(button)
                button.style().polish(button)
            button.setEnabled(enabled if not stop_mode else is_active)

    def set_running_state(
        self,
        text: str,
        running: bool,
        active_action: str | None = None,
    ) -> None:
        self.state_value.setText(text)
        self.state_value.setObjectName("stateBusy" if running else "stateIdle")
        self.state_value.style().unpolish(self.state_value)
        self.state_value.style().polish(self.state_value)
        self.setProperty("cardState", "busy" if running else "idle")
        self.style().unpolish(self)
        self.style().polish(self)
        self._set_action_button_mode(
            stop_mode=running,
            enabled=True,
            active_action=active_action,
        )
        if running:
            self._process_output_buffer = ""
            self._last_stage_log = ""
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat(f"{text} 0%")

    def set_result_state(self, text: str, state: str) -> None:
        name_map = {
            "idle": "stateIdle",
            "success": "stateSuccess",
            "error": "stateError",
            "disconnected": "stateDisconnected",
        }
        self.state_value.setText(text)
        self.state_value.setObjectName(name_map.get(state, "stateIdle"))
        self.state_value.style().unpolish(self.state_value)
        self.state_value.style().polish(self.state_value)
        self.setProperty("cardState", state)
        self.style().unpolish(self)
        self.style().polish(self)
        is_disconnected = state == "disconnected"
        self._set_action_button_mode(stop_mode=False, enabled=not is_disconnected)
        if state == "success":
            self.progress_bar.setValue(100)
            self.progress_bar.setFormat("完成 100%")
        elif state == "disconnected":
            self.progress_bar.setFormat("设备已断开")
        elif state == "error":
            self.progress_bar.setFormat("执行失败")

    def set_progress(self, percent: int, text: str | None = None) -> None:
        clamped = max(0, min(100, percent))
        self.progress_bar.setValue(clamped)
        self.progress_bar.setFormat(text or f"执行中 {clamped}%")

    def set_stage_text(self, text: str) -> None:
        self.progress_bar.setFormat(text)

    def log_stage(self, text: str) -> None:
        if text and text != self._last_stage_log:
            self._last_stage_log = text
            self.append_log(f"[阶段] {text}")

    def set_newly_detected(self, is_new: bool) -> None:
        self.new_badge.setVisible(is_new)
        if is_new:
            self.setProperty("cardState", "new")
        elif self.property("cardState") == "new":
            self.setProperty("cardState", "idle")
        self.style().unpolish(self)
        self.style().polish(self)
