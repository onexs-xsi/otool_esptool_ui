"""Compact device history and proportional phase-duration bars."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QEvent, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QPushButton, QScrollArea,
    QSizePolicy, QToolTip, QVBoxLayout, QWidget,
)

from .flash_timing import PHASES, normalize_mac

STATUS = {"success": ("成功", "#047857", "#ecfdf5"), "failed": ("失败", "#b91c1c", "#fef2f2"),
          "stopped": ("已停止", "#64748b", "#f1f5f9"), "interrupted": ("已中断", "#b45309", "#fffbeb"),
          "running": ("进行中", "#2560e0", "#eff6ff")}


def seconds(milliseconds) -> str:
    try:
        return f"{max(0, float(milliseconds)) / 1000:.2f} s"
    except (TypeError, ValueError):
        return "—"


def start_text(record, full=False) -> str:
    try:
        stamp = datetime.fromisoformat(record["started_at"])
        return stamp.strftime("%Y-%m-%d %H:%M:%S" if full else "%m-%d %H:%M:%S")
    except (KeyError, ValueError, TypeError):
        return "开始时间未知"


def is_identified(record: dict) -> bool:
    chip = record.get("chip", "").strip().casefold()
    return bool(normalize_mac(record.get("mac"))) and chip not in {"", "未识别", "未知设备", "未知", "unknown", "-", "—"}


class PhaseDurationBar(QWidget):
    def __init__(self, record: dict, parent=None):
        super().__init__(parent)
        self.record = record
        self.parts: list[tuple[str, float]] = []
        totals = defaultdict(float)
        for span in record.get("phases", []):
            if span.get("phase") in PHASES:
                totals[span["phase"]] += max(0.0, float(span.get("duration_ms", 0)))
        self.parts = [(phase, totals[phase]) for phase in PHASES if totals[phase] > 0]
        self.setFixedHeight(10)
        self.setMinimumWidth(80)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet("QToolTip { background:#172b4d; color:#ffffff; border:1px solid #344766; border-radius:5px; padding:8px; font-size:12px; }")
        self.setAccessibleName("阶段耗时：" + "，".join(f"{PHASES[p][0]} {seconds(ms)}" for p, ms in self.parts))
        self.setToolTip(self.summary())

    def summary(self) -> str:
        return "总耗时 " + seconds(self.record.get("elapsed_ms")) + "\n" + "\n".join(
            f"{PHASES[phase][0]}：{seconds(ms)}" for phase, ms in self.parts)

    def phase_at(self, x: float):
        total = sum(ms for _, ms in self.parts)
        position = 0.0
        for phase, ms in self.parts:
            width = self.width() * ms / total if total else 0
            if position <= x < position + width:
                return phase, ms, ms / total * 100
            position += width
        return None

    def event(self, event):
        if event.type() == QEvent.Type.ToolTip:
            part = self.phase_at(event.pos().x())
            if part:
                phase, ms, ratio = part
                QToolTip.showText(event.globalPos(), f"{PHASES[phase][0]}  ·  {seconds(ms)}  ·  {ratio:.1f}%\n"
                                  f"总耗时 {seconds(self.record.get('elapsed_ms'))}\n按实际工具调用阶段统计，不含等待拔出。", self)
            else:
                QToolTip.showText(event.globalPos(), self.summary(), self)
            return True
        return super().event(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 4, 4)
        painter.setClipPath(path)
        painter.fillRect(self.rect(), QColor("#e9eef5"))
        total, x = sum(ms for _, ms in self.parts), 0.0
        for phase, ms in self.parts:
            width = self.width() * ms / total if total else 0
            painter.fillRect(QRectF(x, 0, width, self.height()), QColor(PHASES[phase][1]))
            x += width
        if self.hasFocus():
            painter.setClipping(False)
            painter.setPen(QColor("#1d4ed8"))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 4, 4)
        painter.end()


class HistoryDeviceCard(QFrame):
    def __init__(self, records: list[dict], expanded=False, on_expand=None, parent=None):
        super().__init__(parent)
        self.setObjectName("historyRecord")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        latest = records[0]
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(10, 8, 10, 7)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(4)
        chip = QLabel(latest.get("chip") or "未知设备")
        chip.setObjectName("historyDeviceTitle")
        chip.setTextFormat(Qt.TextFormat.PlainText)
        chip.setToolTip(chip.text())
        chip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        header.addWidget(chip)
        mac_address = normalize_mac(latest.get("mac"))
        mac = QLabel("- " + (mac_address or "MAC 未读取"))
        mac.setObjectName("historyMac")
        mac.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mac.setToolTip(mac_address or "尚未读取到 MAC，独立保留本次记录")
        mac.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        header.addWidget(mac)
        header.addStretch(1)
        name, fg, bg = STATUS.get(latest.get("status"), STATUS["interrupted"])
        badge = QLabel(name)
        badge.setStyleSheet(f"color:{fg}; background:{bg}; border-radius:4px; padding:2px 6px; font-weight:600; font-size:11px;")
        header.addWidget(badge)
        layout.addLayout(header)
        meta = QHBoxLayout()
        meta.setSpacing(4)
        stamp = QLabel("开始 " + start_text(latest) + " · " + latest.get("port", "—") + f" · {len(records)} 次")
        stamp.setObjectName("historyMuted")
        stamp.setToolTip("烧录开始：" + start_text(latest, True))
        meta.addWidget(stamp, 1)
        elapsed = QLabel(seconds(latest.get("elapsed_ms")))
        elapsed.setObjectName("historyElapsed")
        elapsed.setToolTip("本次执行总耗时；不含等待设备拔出和失败后的重试等待。")
        meta.addWidget(elapsed)
        layout.addLayout(meta)
        layout.addWidget(PhaseDurationBar(latest))
        footer = QHBoxLayout()
        paths = latest.get("firmware", [])
        firmware = QLabel(Path(paths[0]).name if paths else "未记录固件")
        firmware.setTextFormat(Qt.TextFormat.PlainText)
        firmware.setObjectName("historyFirmware")
        firmware.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        firmware.setToolTip("\n".join(paths))
        footer.addWidget(firmware, 1)
        button = QPushButton("收起明细" if expanded else "阶段明细")
        button.setObjectName("historyDetailsButton")
        button.setCheckable(True)
        button.setChecked(expanded)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda checked: on_expand(checked) if on_expand else None)
        footer.addWidget(button)
        layout.addLayout(footer)
        if expanded:
            details = QWidget()
            content = QVBoxLayout(details)
            content.setContentsMargins(0, 3, 0, 0)
            content.setSpacing(4)
            for record in records[:20]:
                caption = QLabel(start_text(record, True) + "  ·  " + STATUS.get(record.get("status"), STATUS["interrupted"])[0])
                caption.setObjectName("historyMuted")
                content.addWidget(caption)
                content.addWidget(PhaseDurationBar(record))
                totals = defaultdict(float)
                for span in record.get("phases", []):
                    totals[span.get("phase")] += float(span.get("duration_ms", 0))
                legend = QGridLayout()
                legend.setHorizontalSpacing(8)
                legend.setVerticalSpacing(4)
                for index, (phase, ms) in enumerate((p, totals[p]) for p in PHASES if totals[p] > 0):
                    label, color = PHASES[phase]
                    item = QLabel(f'<span style="color:{color}">●</span> {label} {seconds(ms)}')
                    item.setObjectName("historyPhaseLegend")
                    legend.addWidget(item, index // 3, index % 3)
                content.addLayout(legend)
            if len(records) > 20:
                content.addWidget(QLabel(f"显示最近 20 次，另有 {len(records)-20} 次保存在本地历史文件中。"))
            layout.addWidget(details)


class FlashHistoryPanel(QFrame):
    close_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records: list[dict] = []
        self.expanded: set[str] = set()
        self.visible_limit = 40
        self.setObjectName("flashHistoryPanel")
        self.setMinimumWidth(350)
        self.setMaximumWidth(420)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 13, 14, 12)
        layout.setSpacing(9)
        header = QHBoxLayout()
        title = QLabel("烧录历史")
        title.setObjectName("historyPanelTitle")
        header.addWidget(title, 1)
        self.count = QLabel("0 台 / 0 次")
        self.count.setObjectName("historyMuted")
        header.addWidget(self.count)
        close = QPushButton("×")
        close.setObjectName("historyCloseButton")
        close.setFixedSize(24, 24)
        close.setAccessibleName("关闭设备历史")
        close.setToolTip("关闭设备历史")
        close.clicked.connect(self.close_requested)
        header.addWidget(close)
        layout.addLayout(header)
        self.caption = QLabel("按 MAC 汇总 · 最近烧录优先")
        self.caption.setObjectName("historyMuted")
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.error = QLabel()
        self.error.setObjectName("historyError")
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.hide()
        layout.addWidget(self.error)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索 MAC、芯片或固件")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("搜索设备烧录历史")
        self.search.textChanged.connect(self._search_changed)
        layout.addWidget(self.search)
        self.summary = QLabel()
        self.summary.setObjectName("historySummary")
        layout.addWidget(self.summary)
        legend = QLabel(" &nbsp; ".join(f'<span style="color:{color}">●</span> {label}' for label, color in PHASES.values()))
        legend.setObjectName("historyPhaseLegend")
        legend.setWordWrap(True)
        layout.addWidget(legend)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.items = QWidget()
        self.items.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.list_layout = QVBoxLayout(self.items)
        self.list_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.items)
        layout.addWidget(self.scroll, 1)
        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.hint = QLabel("悬停查看阶段耗时")
        self.hint.setObjectName("historyMuted")
        self.hint.setToolTip("分段宽度代表阶段耗时，悬停查看具体数值。")
        footer.addWidget(self.hint, 1)
        self.show_all_button = QPushButton("显示全部")
        self.show_all_button.setObjectName("historyShowAll")
        self.show_all_button.setCheckable(True)
        self.show_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_all_button.setToolTip("包含未识别芯片型号或未读取到 MAC 的记录")
        self.show_all_button.toggled.connect(self._search_changed)
        footer.addWidget(self.show_all_button)
        layout.addLayout(footer)
        self.setStyleSheet("""
          QFrame#flashHistoryPanel { background:#ffffff; border:1px solid #dde4ee; border-radius:12px; }
          QFrame#flashHistoryPanel QWidget { background:transparent; }
          QFrame#historyRecord { background:#ffffff; border:1px solid #e2e8f0; border-radius:9px; }
          QLabel#historyPanelTitle { color:#172b4d; font-size:14px; font-weight:700; }
          QLabel#historyDeviceTitle { color:#26374f; font-size:13px; font-weight:700; }
          QLabel#historyMac { color:#475569; font-family:Consolas; font-size:11px; }
          QLabel#historyMuted, QLabel#historyFirmware { color:#718096; font-size:11px; }
          QLabel#historyElapsed { color:#203651; font-family:Consolas; font-size:14px; font-weight:700; }
          QLabel#historySummary { background:#f6f8fc; color:#52647b; border-radius:6px; padding:8px; font-size:11px; }
          QLabel#historyError { color:#b45309; background:#fffbeb; border-radius:6px; padding:6px; font-size:11px; }
          QLabel#historyPhaseLegend { color:#52647b; font-size:11px; }
          QPushButton#historyDetailsButton { border:none; padding:0 2px; color:#3b6bc9; font-size:11px; background:transparent; }
          QPushButton#historyDetailsButton:hover, QPushButton#historyDetailsButton:focus { color:#174cad; background:#edf3ff; }
          QPushButton#historyCloseButton { border:none; padding:0; color:#738399; font-size:18px; }
          QPushButton#historyCloseButton:hover { background:#edf3ff; color:#2560e0; }
          QPushButton#historyShowAll { border:1px solid #dce4ee; border-radius:5px; padding:3px 8px; font-size:11px; color:#52647b; background:#f8fafc; }
          QPushButton#historyShowAll:hover, QPushButton#historyShowAll:focus { border-color:#7fa4ee; background:#edf3ff; }
          QPushButton#historyShowAll:checked { border-color:#2560e0; background:#2560e0; color:#ffffff; }
        """)

    def set_records(self, records: list[dict], error="") -> None:
        self.records = records
        self.error.setText(error)
        self.error.setVisible(bool(error))
        self.rebuild()

    def _search_changed(self):
        self.visible_limit = 40
        self.rebuild()

    def rebuild(self):
        scroll_position = self.scroll.verticalScrollBar().value()
        while self.list_layout.count():
            widget = self.list_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        groups = defaultdict(list)
        query = self.search.text().strip().casefold()
        show_all = self.show_all_button.isChecked()
        hidden_count = sum(not is_identified(record) for record in self.records) if not show_all else 0
        self.hint.setText(f"已隐藏 {hidden_count} 条未识别记录" if hidden_count else "悬停查看阶段耗时")
        for record in sorted(self.records, key=lambda r: r.get("started_at", ""), reverse=True):
            if not show_all and not is_identified(record):
                continue
            if query and query not in " ".join((record.get("mac", ""), record.get("chip", ""), record.get("port", ""), *record.get("firmware", []))).casefold():
                continue
            groups[record.get("mac") or record["id"]].append(record)
        self.count.setText(f"{len(groups)} 台 / {sum(map(len, groups.values()))} 次")
        matched_records = [r for records in groups.values() for r in records]
        completed = [r for r in matched_records if r.get("status") == "success"]
        failed = sum(r.get("status") in ("failed", "stopped", "interrupted") for r in matched_records)
        avg = sum(r.get("elapsed_ms", 0) for r in completed) / len(completed) if completed else 0
        self.summary.setText(f"成功 {len(completed)}    ·    失败 {failed}    ·    平均 {seconds(avg)}")
        if not groups:
            message = "未找到匹配的设备" if query else (
                "暂无已识别设备记录\n勾选右下角“显示全部”查看未识别记录。" if hidden_count else
                "暂无烧录历史\n完成一次烧录后，将在这里记录设备与阶段耗时。")
            label = QLabel(message)
            label.setObjectName("historyMuted")
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumHeight(120)
            self.list_layout.addWidget(label)
        for key, records in list(groups.items())[:self.visible_limit]:
            def expanded(checked, identity=key):
                self.expanded.add(identity) if checked else self.expanded.discard(identity)
                self.rebuild()
            card = HistoryDeviceCard(records, key in self.expanded, expanded, parent=self.items)
            self.list_layout.addWidget(card)
        if len(groups) > self.visible_limit:
            more = QPushButton(f"加载更多（剩余 {len(groups)-self.visible_limit} 台）")
            more.clicked.connect(self._load_more)
            self.list_layout.addWidget(more)
        self.list_layout.activate()
        self.scroll.verticalScrollBar().setValue(scroll_position)

    def _load_more(self):
        self.visible_limit += 40
        self.rebuild()
