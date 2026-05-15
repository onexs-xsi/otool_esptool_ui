import json
import os
import re
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QProcess,
    QProcessEnvironment,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QTextCursor,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from .constants import (
    APP_AUTHOR,
    APP_GITHUB_URL,
    APP_TITLE,
    APP_VERSION,
    _INTERNAL_WORKER_EVENT_PREFIX,
    DEFAULT_FIRMWARE_DIR,
    DETECT_BAUD,
    EFUSE_CHIP_PRESETS,
    FLASH_BAUD_DEFAULT,
    FLASH_BAUD_OPTIONS,
    LOCAL_ESPTOOL_DIR,
    TOOL_DIR,
    _FROZEN,
    _build_process_env_dict,
    _build_reference_notice,
    _build_tool_command,
    _build_tool_worker_command,
    _inject_local_esptool_pythonpath,
    _local_esptool_available,
    _resolve_build_timestamp_text,
    _tool_backend_available,
    resolve_chip_arg,
)
from .dialog_memory import get_existing_directory, get_open_file_name
from .device_card import DeviceCard
from .efuse_batch_dialog import BurnEfuseBatchWidget
from .jump_list import setup_jump_list
from .efuse_dialog import EFuseDialog
from .export_dialog import ExportFlashDialog
from .flow_layout import FlowLayout
from .helpers import _build_avatar_icon, _build_github_icon, _fetch_remote_avatar_bytes
from .merge_split_widget import MergeSplitWidget
from .models import DeviceInfo
from .styles import BASE_STYLESHEET
from .verify_widget import VerifyWidget


class TabSwitcher(QWidget):
    """左下角页面切换滑块控件：可点击切换，也可鼠标拖动。"""

    currentChanged = pyqtSignal(int)

    _PILL_COLOR   = QColor("#2560e0")
    _TXT_ACTIVE   = QColor("#ffffff")
    _TXT_INACTIVE = QColor("#6b7a94")
    _HOVER_TINT   = QColor(37, 96, 224, 18)   # 微弱蓝色悬停高亮

    def __init__(self, tabs: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tabs     = tabs
        self._n        = len(tabs)
        self._current  = 0
        self._hover    = -1
        self._slide    = 0.0          # float index：0 .. n-1
        self._drag_on  = False
        self._is_drag  = False
        self._drag_x0  = 0.0
        self._drag_s0  = 0.0

        self._anim = QPropertyAnimation(self, b"slidePos", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(self._n * 90)
        self.setFixedHeight(36)

    # ── property ──────────────────────────────────────────────────
    @pyqtProperty(float)
    def slidePos(self) -> float:          # type: ignore[override]
        return self._slide

    @slidePos.setter
    def slidePos(self, val: float) -> None:
        self._slide = float(val)
        self.update()

    # ── geometry helpers ──────────────────────────────────────────
    def _tw(self) -> float:
        return self.width() / self._n

    def _idx_at(self, x: float) -> int:
        return max(0, min(self._n - 1, int(x / self._tw())))

    # ── public API ────────────────────────────────────────────────
    def current(self) -> int:
        return self._current

    def set_current(self, idx: int, animated: bool = True) -> None:
        idx = max(0, min(self._n - 1, idx))
        prev = self._current
        self._current = idx
        if animated:
            self._anim.stop()
            self._anim.setStartValue(self._slide)
            self._anim.setEndValue(float(idx))
            self._anim.start()
        else:
            self._slide = float(idx)
            self.update()
        if idx != prev:
            self.currentChanged.emit(idx)

    # ── mouse ─────────────────────────────────────────────────────
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_on = True
            self._is_drag = False
            self._drag_x0 = e.position().x()
            self._drag_s0 = self._slide
            self._anim.stop()

    def mouseMoveEvent(self, e) -> None:
        x = e.position().x()
        if self._drag_on:
            dx = x - self._drag_x0
            if abs(dx) > 5:
                self._is_drag = True
            if self._is_drag:
                raw = self._drag_s0 + dx / self._tw()
                self._slide = max(0.0, min(float(self._n - 1), raw))
                self.update()
        else:
            new_h = self._idx_at(x)
            if new_h != self._hover:
                self._hover = new_h
                self.update()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self._drag_on:
            self._drag_on = False
            if self._is_drag:
                nearest = max(0, min(self._n - 1, round(self._slide)))
                if nearest != self._current:
                    self._current = nearest
                    self.currentChanged.emit(nearest)
                self.set_current(self._current)   # snap+animate
            else:
                clicked = self._idx_at(e.position().x())
                if clicked != self._current:
                    self.set_current(clicked)

    def leaveEvent(self, e) -> None:
        self._hover = -1
        self.update()

    # ── paint ─────────────────────────────────────────────────────
    def paintEvent(self, _) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        tw = w / self._n
        pad = 3
        ph = h - 2 * pad
        pr = ph / 2

        # hover tint
        if not self._drag_on and self._hover >= 0:
            hx = self._hover * tw
            path = QPainterPath()
            path.addRoundedRect(hx + 2, pad, tw - 4, ph, pr, pr)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._HOVER_TINT)
            p.drawPath(path)

        # sliding pill
        pill_x = self._slide * tw + pad
        pill_w = tw - 2 * pad
        path2 = QPainterPath()
        path2.addRoundedRect(pill_x, pad, pill_w, ph, pr - 1, pr - 1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._PILL_COLOR)
        p.drawPath(path2)

        # labels — color interpolated by proximity to slider
        ar, ag, ab = 255, 255, 255
        ir, ig, ib = 107, 122, 148
        font = QFont()
        font.setPointSize(10)
        for i, name in enumerate(self._tabs):
            t = max(0.0, 1.0 - abs(i - self._slide))
            r = int(ar * t + ir * (1 - t))
            g = int(ag * t + ig * (1 - t))
            bv = int(ab * t + ib * (1 - t))
            font.setBold(t > 0.5)
            p.setFont(font)
            p.setPen(QColor(r, g, bv))
            p.drawText(QRectF(i * tw, 0, tw, h), Qt.AlignmentFlag.AlignCenter, name)

        p.end()


class FlashEntryRow(QWidget):
    """单条烧录条目：地址 + 固件路径 + 浏览 + 移除。"""

    remove_requested = pyqtSignal()

    def __init__(
        self, addr: str = "0x0", path: str = "", parent: "QWidget | None" = None
    ) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 1, 0, 1)
        lay.setSpacing(6)

        addr_lbl = QLabel("地址")
        addr_lbl.setObjectName("configLabel")
        self.addr_edit = QLineEdit(addr)
        self.addr_edit.setPlaceholderText("0x0")
        self.addr_edit.setFixedWidth(90)

        path_lbl = QLabel("固件")
        path_lbl.setObjectName("configLabel")
        self.path_edit = QLineEdit(path)
        self.path_edit.setPlaceholderText("选择待烧录的 .bin 固件文件")

        browse_btn = QPushButton("选择")
        browse_btn.setFixedWidth(52)
        browse_btn.clicked.connect(self._browse)

        self.remove_btn = QPushButton("✕")
        self.remove_btn.setObjectName("entryRemoveButton")
        self.remove_btn.setFixedSize(26, 26)
        self.remove_btn.clicked.connect(self.remove_requested)

        lay.addWidget(addr_lbl)
        lay.addWidget(self.addr_edit)
        lay.addSpacing(4)
        lay.addWidget(path_lbl)
        lay.addWidget(self.path_edit, 1)
        lay.addWidget(browse_btn)
        lay.addWidget(self.remove_btn)

    def _browse(self) -> None:
        file_path, _ = get_open_file_name(
            self,
            "选择固件",
            "Binary Files (*.bin);;All Files (*.*)",
        )
        if file_path:
            self.path_edit.setText(file_path)
            self._auto_set_addr(file_path)

    def _auto_set_addr(self, path: str) -> None:
        """从文件名中提取地址标记（如 _0x10000），无则置 0x0。"""
        stem = Path(path).stem
        match = re.search(r'_(0[xX][0-9a-fA-F]+)$', stem)
        self.addr_edit.setText(match.group(1).lower() if match else "0x0")


class OtoolEsptoolUI(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.device_cards: dict[str, DeviceCard] = {}
        self.device_infos: dict[str, DeviceInfo] = {}
        self.new_device_ids: set[str] = set()
        self.acknowledged_device_ids: set[str] = set()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2500)
        self.refresh_timer.timeout.connect(self.refresh_ports)
        self.setWindowTitle(f"{APP_TITLE} v{APP_VERSION}")
        self.resize(1320, 816)
        self._init_ui()
        self._apply_style()
        self._card_scale: float = 1.0
        QApplication.instance().installEventFilter(self)
        self.auto_pick_firmware()
        QTimer.singleShot(0, self.refresh_ports)

    def _init_ui(self) -> None:
        central = QWidget(self)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(16, 12, 16, 12)
        root_layout.setSpacing(10)

        # 页 1 — 熔丝台（提前创建，供 toolbar 引用其控件）
        self._efuse_batch_widget = BurnEfuseBatchWidget()
        self._verify_widget = VerifyWidget()

        # ── 顶部工具栏 ─────────────────────────────────────
        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        toolbar_layout = QVBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(16, 10, 16, 10)
        toolbar_layout.setSpacing(8)

        # 第一行：标题 + 统计 badge + 操作按钮
        row1 = QHBoxLayout()
        row1.setSpacing(8)

        title = QLabel("烧录台")
        title.setObjectName("heroTitle")
        self.hero_title = title

        self.total_stat = QLabel("总数 0")
        self.total_stat.setObjectName("statBadge")
        self.running_stat = QLabel("运行中 0")
        self.running_stat.setObjectName("statBadgeBusy")
        self.success_stat = QLabel("成功 0")
        self.success_stat.setObjectName("statBadgeSuccess")
        self.failed_stat = QLabel("失败 0")
        self.failed_stat.setObjectName("statBadgeError")

        self.refresh_ports_button = QPushButton("刷新设备")
        self.refresh_ports_button.clicked.connect(self.refresh_ports)
        self.clear_devices_button = QPushButton("清空设备")
        self.clear_devices_button.clicked.connect(self.clear_devices)
        self.auto_refresh_button = QPushButton("自动刷新: 关")
        self.auto_refresh_button.setCheckable(True)
        self.auto_refresh_button.toggled.connect(self.toggle_auto_refresh)
        self.auto_flash_button = QPushButton("自动烧录: 关")
        self.auto_flash_button.setCheckable(True)
        self.auto_flash_button.toggled.connect(self.toggle_auto_flash)
        self.flash_all_button = QPushButton("全部烧录")
        self.flash_all_button.clicked.connect(self.flash_all_devices)
        self.erase_all_button = QPushButton("全部擦除")
        self.erase_all_button.clicked.connect(self.erase_all_devices)
        self.stop_all_button = QPushButton("全部停止")
        self.stop_all_button.setObjectName("dangerButton")
        self.stop_all_button.clicked.connect(self.stop_all_devices)
        self.stop_all_button.clicked.connect(self._efuse_batch_widget._stop_all)
        self.stop_all_button.clicked.connect(self._verify_widget.stop_all_tasks)

        row1.addWidget(title)
        row1.addSpacing(12)
        row1.addWidget(self.total_stat)
        row1.addWidget(self.running_stat)
        row1.addWidget(self.success_stat)
        row1.addWidget(self.failed_stat)
        row1.addStretch(1)
        self._flash_btns_group = QWidget()
        _fbg = QHBoxLayout(self._flash_btns_group)
        _fbg.setContentsMargins(0, 0, 0, 0)
        _fbg.setSpacing(8)
        _fbg.addWidget(self.auto_refresh_button)
        _fbg.addWidget(self.auto_flash_button)
        _fbg.addWidget(self.flash_all_button)
        _fbg.addWidget(self.erase_all_button)
        # 熔丝台控件组（初始隐藏）
        self._efuse_btns_group = QWidget()
        _ebg = QHBoxLayout(self._efuse_btns_group)
        _ebg.setContentsMargins(0, 0, 0, 0)
        _ebg.setSpacing(8)
        _chip_lbl2 = QLabel("芯片型号")
        _chip_lbl2.setObjectName("configLabel")
        _ebg.addWidget(_chip_lbl2)
        _ebg.addWidget(self._efuse_batch_widget._chip_combo)
        _ebg.addWidget(self._efuse_batch_widget._auto_burn_btn)
        self._efuse_btns_group.setVisible(False)
        row1.addWidget(self._flash_btns_group)
        row1.addWidget(self._efuse_btns_group)
        row1.addWidget(self.stop_all_button)

        # 第二行：烧录台 row2
        self._flash_row2 = QWidget()
        _fr2 = QHBoxLayout(self._flash_row2)
        _fr2.setContentsMargins(0, 0, 0, 0)
        _fr2.setSpacing(8)

        baud_label = QLabel("波特率")
        baud_label.setObjectName("configLabel")
        self.baud_edit = QComboBox()
        self.baud_edit.setEditable(True)
        for _baud in FLASH_BAUD_OPTIONS:
            self.baud_edit.addItem(_baud)
        self.baud_edit.setCurrentText(FLASH_BAUD_DEFAULT)
        self.baud_edit.setFixedWidth(110)
        self.baud_edit.lineEdit().setPlaceholderText("波特率")

        self.status_label = QLabel("状态: 正在等待设备")
        self.status_label.setObjectName("statusLabel")

        _fr2.addWidget(baud_label)
        _fr2.addWidget(self.baud_edit)
        _fr2.addSpacing(4)
        _fr2.addWidget(self.refresh_ports_button)
        _fr2.addWidget(self.clear_devices_button)
        _fr2.addStretch(1)
        _fr2.addWidget(self.status_label)

        # 第二行：熔丝台 row2
        self._efuse_row2 = QWidget()
        _er2 = QHBoxLayout(self._efuse_row2)
        _er2.setContentsMargins(0, 0, 0, 0)
        _er2.setSpacing(8)
        self._efuse_refresh_btn = QPushButton("刷新设备")
        self._efuse_refresh_btn.clicked.connect(self._efuse_batch_widget._poll_ports)
        self._efuse_clear_btn = QPushButton("清空设备")
        self._efuse_clear_btn.clicked.connect(self._efuse_batch_widget._clear_all_devices)
        _er2.addWidget(self._efuse_refresh_btn)
        _er2.addWidget(self._efuse_clear_btn)
        _er2.addStretch(1)
        self._efuse_row2.setVisible(False)

        row2 = QHBoxLayout()
        row2.setSpacing(0)
        row2.addWidget(self._flash_row2)
        row2.addWidget(self._efuse_row2)

        # 烧录条目区（整体作为 _flash_cfg_group，便于 tab 切换时统一隐藏）
        entries_outer = QWidget()
        entries_layout = QVBoxLayout(entries_outer)
        entries_layout.setContentsMargins(0, 2, 0, 2)
        entries_layout.setSpacing(2)

        entries_header = QHBoxLayout()
        entries_header.setSpacing(8)
        entries_lbl = QLabel("烧录条目")
        entries_lbl.setObjectName("configLabel")
        self._add_entry_btn = QPushButton("＋ 添加条目")
        self._add_entry_btn.setObjectName("addEntryButton")
        self._add_entry_btn.clicked.connect(lambda: self._add_flash_entry())
        entries_header.addWidget(entries_lbl)
        entries_header.addStretch(1)
        entries_header.addWidget(self._add_entry_btn)

        self._entries_inner = QWidget()
        self._entries_vbox = QVBoxLayout(self._entries_inner)
        self._entries_vbox.setContentsMargins(0, 0, 0, 0)
        self._entries_vbox.setSpacing(2)

        entries_layout.addLayout(entries_header)
        entries_layout.addWidget(self._entries_inner)

        self._flash_cfg_group = entries_outer
        self._flash_rows: list[FlashEntryRow] = []

        toolbar_layout.addLayout(row1)
        toolbar_layout.addLayout(row2)
        toolbar_layout.addWidget(entries_outer)

        device_header = QHBoxLayout()
        device_header.setSpacing(8)
        device_title = QLabel("已识别设备")
        device_title.setObjectName("sectionTitle")
        self.device_count_label = QLabel("0 台")
        self.device_count_label.setObjectName("countBadge")
        device_header.addWidget(device_title)
        device_header.addWidget(self.device_count_label, 0, Qt.AlignmentFlag.AlignVCenter)
        device_header.addStretch(1)

        self.device_container = QWidget()
        self.device_layout = FlowLayout(self.device_container, h_spacing=12, v_spacing=12)
        self.device_layout.setContentsMargins(0, 0, 0, 0)
        self.empty_hint = QLabel(
            "暂无设备，点击\u201c刷新设备\u201d或开启自动刷新后等待设备接入。"
        )
        self.empty_hint.setObjectName("emptyHint")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.device_layout.addWidget(self.empty_hint)

        scroll = QScrollArea()
        self.scroll_area = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.device_container)

        # ── 页面容器 ────────────────────────────────────────────
        self.page_stack = QStackedWidget()

        # 页 0 — 烧录台
        page_flash = QWidget()
        pg0_layout = QVBoxLayout(page_flash)
        pg0_layout.setContentsMargins(0, 0, 0, 0)
        pg0_layout.setSpacing(8)
        pg0_layout.addLayout(device_header)
        pg0_layout.addWidget(scroll, 1)

        # 页 1 — 熔丝台
        page_efuse = QWidget()
        pg1_layout = QVBoxLayout(page_efuse)
        pg1_layout.setContentsMargins(0, 0, 0, 70)  # 底部留出浮动 tab 栏空间
        pg1_layout.addWidget(self._efuse_batch_widget)

        # 页 2 — 校验台（暂空白）
        page_verify = QWidget()
        pg2_layout = QVBoxLayout(page_verify)
        pg2_layout.setContentsMargins(0, 0, 0, 70)
        pg2_layout.addWidget(self._verify_widget)

        # 页 3 — 分合台
        page_merge_split = QWidget()
        pg3_layout = QVBoxLayout(page_merge_split)
        pg3_layout.setContentsMargins(0, 0, 0, 70)
        self._merge_split_widget = MergeSplitWidget()
        self._merge_split_widget.send_to_flash_station.connect(
            self._receive_merged_firmware
        )
        pg3_layout.addWidget(self._merge_split_widget)

        # 页 4 — 终端台（占位界面，功能待开发）
        page_terminal = QWidget()
        pg4_layout = QVBoxLayout(page_terminal)
        pg4_layout.setContentsMargins(0, 0, 0, 70)
        _term_frame = QFrame()
        _term_frame.setObjectName("mergeSplitFrame")
        _term_fl = QVBoxLayout(_term_frame)
        _term_fl.setContentsMargins(40, 60, 40, 60)
        _term_icon = QLabel(">_")
        _term_icon.setObjectName("terminalPlaceholderIcon")
        _term_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _term_title = QLabel("串口终端台")
        _term_title.setObjectName("terminalPlaceholderTitle")
        _term_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _term_hint = QLabel("串口终端监视与交互功能，即将推出")
        _term_hint.setObjectName("emptyHint")
        _term_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _term_fl.addStretch(1)
        _term_fl.addWidget(_term_icon)
        _term_fl.addSpacing(12)
        _term_fl.addWidget(_term_title)
        _term_fl.addSpacing(6)
        _term_fl.addWidget(_term_hint)
        _term_fl.addStretch(1)
        pg4_layout.addWidget(_term_frame)

        self.page_stack.addWidget(page_flash)
        self.page_stack.addWidget(page_efuse)
        self.page_stack.addWidget(page_verify)
        self.page_stack.addWidget(page_merge_split)
        self.page_stack.addWidget(page_terminal)

        root_layout.addWidget(toolbar)
        root_layout.addWidget(self.page_stack, 1)
        self.setCentralWidget(central)
        self._init_floating_info_panel(central)
        self._init_floating_tab_panel(central)
        self._position_floating_panels()
        self._add_flash_entry()  # 初始化第一条烧录条目

    def _init_floating_info_panel(self, parent: QWidget) -> None:
        self.floating_info_panel = QFrame(parent)
        self.floating_info_panel.setObjectName("floatingInfoPanel")

        panel_layout = QHBoxLayout(self.floating_info_panel)
        panel_layout.setContentsMargins(8, 6, 8, 6)
        panel_layout.setSpacing(6)

        self.version_badge = QLabel(f"v{APP_VERSION} · {_resolve_build_timestamp_text()}")
        self.version_badge.setObjectName("floatingMetaPrimary")
        self.version_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.author_badge = QLabel(f"Coder {APP_AUTHOR}")
        self.author_badge.setObjectName("floatingMetaSecondary")
        self.author_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.floating_info_divider = QFrame()
        self.floating_info_divider.setObjectName("floatingInfoDivider")
        self.floating_info_divider.setFixedWidth(1)

        self.references_button = QPushButton("引用说明")
        self.references_button.setObjectName("floatingActionButton")
        self.references_button.setMinimumWidth(74)
        self.references_button.clicked.connect(self.show_reference_notice)
        self.github_button = QPushButton("GitHub")
        self.github_button.setObjectName("floatingActionButton")
        self.github_button.setIcon(_build_github_icon())
        self.github_button.setIconSize(QSize(20, 20))
        self.github_button.setMinimumWidth(74)
        self.github_button.clicked.connect(self.open_github_url)
        # 异步下载网络头像，下载成功后替换按钮图标
        _fetch_remote_avatar_bytes(
            lambda data: QTimer.singleShot(0, lambda: self._update_github_icon(data))
        )

        panel_layout.addWidget(self.version_badge)
        panel_layout.addWidget(self.author_badge)
        panel_layout.addWidget(self.floating_info_divider)
        panel_layout.addWidget(self.references_button)
        panel_layout.addWidget(self.github_button)

        self.floating_info_panel.setFixedHeight(44)
        self.floating_info_panel.adjustSize()
        self.floating_info_panel.raise_()

    def _init_floating_tab_panel(self, parent: QWidget) -> None:
        panel = QFrame(parent)
        panel.setObjectName("floatingInfoPanel")
        panel.setFixedHeight(44)
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(0)
        self.tab_switcher = TabSwitcher(["烧录台", "熔丝台", "校验台", "分合台", "终端台"], panel)
        self.tab_switcher.currentChanged.connect(self.page_stack.setCurrentIndex)
        self.tab_switcher.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tab_switcher)
        panel.adjustSize()
        self.floating_tab_panel = panel
        panel.raise_()

    def _position_floating_panels(self) -> None:
        central = self.centralWidget()
        if central is None:
            return
        margin = 18
        panel_h = 44
        y = max(margin, central.height() - panel_h - margin)

        if hasattr(self, "floating_info_panel"):
            ps = self.floating_info_panel.sizeHint()
            ps.setHeight(panel_h)
            self.floating_info_panel.resize(ps)
            x = max(margin, central.width() - ps.width() - margin)
            self.floating_info_panel.move(x, y)
            self.floating_info_panel.raise_()

        if hasattr(self, "floating_tab_panel"):
            self.floating_tab_panel.adjustSize()
            self.floating_tab_panel.setFixedHeight(panel_h)
            self.floating_tab_panel.move(margin, y)
            self.floating_tab_panel.raise_()

    def _position_floating_info_panel(self) -> None:
        self._position_floating_panels()

    _TAB_TITLES = ["烧录台", "熔丝台", "校验台", "分合台", "终端台"]

    def _on_tab_changed(self, idx: int) -> None:
        self.hero_title.setText(self._TAB_TITLES[idx])
        is_flash = idx == 0
        is_efuse = idx == 1
        self._flash_btns_group.setVisible(is_flash)
        self._flash_cfg_group.setVisible(is_flash)
        self._flash_row2.setVisible(is_flash)
        self._efuse_btns_group.setVisible(is_efuse)
        self._efuse_row2.setVisible(is_efuse)
        for widget in (
            self.total_stat,
            self.running_stat,
            self.success_stat,
            self.failed_stat,
        ):
            widget.setVisible(is_flash)

    def _receive_merged_firmware(self, output_path: str) -> None:
        """从分合台接收合成文件，跳转到烧录台并将其填入烧录条目。"""
        if self.tab_switcher.current() != 0:
            self.tab_switcher.set_current(0)
        if self._flash_rows:
            row = self._flash_rows[0]
            row.path_edit.setText(output_path)
            row.addr_edit.setText("0x0")

    def _apply_style(self) -> None:
        _arrow_path = str(TOOL_DIR / "src" / "assets" / "chevron_down.svg").replace("\\", "/")
        self.setStyleSheet(
            BASE_STYLESHEET
            + """
            QMainWindow {
                background: #f0f2f5;
            }
            QFrame#toolbar {
                background: #ffffff;
                border: 1px solid #e0e4ea;
                border-radius: 10px;
            }
            QFrame#deviceCard {
                background: #ffffff;
                border: 1px solid #e0e4ea;
                border-radius: 12px;
            }
            QFrame#deviceCard[cardState="busy"] {
                background: #fffbf0;
                border: 1px solid #f59e0b;
            }
            QFrame#deviceCard[cardState="success"] {
                background: #f0faf5;
                border: 1px solid #14a05c;
            }
            QFrame#deviceCard[cardState="new"] {
                background: #f0f5ff;
                border: 1px solid #2560e0;
            }
            QFrame#deviceCard[cardState="error"] {
                background: #fff5f5;
                border: 1px solid #e53935;
            }
            QFrame#deviceCard[cardState="disconnected"] {
                background: #f7f8fa;
                border: 1px solid #c5cad4;
            }
            QLabel#heroTitle {
                font-size: 18px;
                font-weight: 700;
                color: #1a2333;
            }
            QLabel#sectionTitle {
                font-size: 14px;
            }
            QFrame#floatingInfoPanel {
                background: rgba(255, 255, 255, 248);
                border: 1px solid #dbe2ea;
                border-radius: 12px;
            }
            QLabel#floatingMetaPrimary, QLabel#floatingMetaSecondary {
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 10px;
                font-weight: 600;
            }
            QLabel#floatingMetaPrimary {
                background: #eff6ff;
                border: 1px solid #bfdbfe;
                color: #1d4ed8;
            }
            QLabel#floatingMetaSecondary {
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                color: #475569;
            }
            QFrame#floatingInfoDivider {
                background: #e2e8f0;
                min-width: 1px;
                max-width: 1px;
                margin-top: 1px;
                margin-bottom: 1px;
            }
            QLabel#infoBadge, QLabel#authorBadge {
                border-radius: 4px;
                padding: 3px 10px;
                font-weight: 700;
                font-size: 12px;
            }
            QLabel#infoBadge {
                background: #eef2ff;
                border: 1px solid #c7d2fe;
                color: #3730a3;
            }
            QLabel#authorBadge {
                background: #ecfdf5;
                border: 1px solid #a7f3d0;
                color: #047857;
            }
            QLabel#countBadge {
                background: #e8edf7;
                border: 1px solid #c5cfe8;
                border-radius: 4px;
                padding: 3px 10px;
                color: #2560e0;
                font-weight: 600;
                font-size: 12px;
            }
            QLabel#newBadge {
                background: #dbeafe;
                border: 1px solid #93c5fd;
                border-radius: 4px;
                padding: 2px 8px;
                color: #1d4ed8;
                font-weight: 700;
                font-size: 11px;
            }
            QLabel#capBadgeCpu, QLabel#capBadgeWifi, QLabel#capBadgeBt, QLabel#capBadgeIeee {
                border-radius: 4px;
                padding: 1px 7px;
                font-size: 11px;
                font-weight: 700;
            }
            QLabel#capBadgeCpu   { background: #fef3c7; border: 1px solid #f59e0b; color: #92400e; }
            QLabel#capBadgeWifi  { background: #eff6ff; border: 1px solid #93c5fd; color: #1d4ed8; }
            QLabel#capBadgeBt    { background: #ede9fe; border: 1px solid #c4b5fd; color: #5b21b6; }
            QLabel#capBadgeIeee  { background: #d1fae5; border: 1px solid #6ee7b7; color: #065f46; }
            QLabel#statBadge, QLabel#statBadgeBusy, QLabel#statBadgeSuccess, QLabel#statBadgeError {
                border-radius: 4px;
                padding: 3px 10px;
                font-weight: 700;
                font-size: 12px;
            }
            QLabel#statBadge {
                background: #e8edf7;
                border: 1px solid #c5cfe8;
                color: #2560e0;
            }
            QLabel#statBadgeBusy {
                background: #fef3c7;
                border: 1px solid #f59e0b;
                color: #b45309;
            }
            QLabel#statBadgeSuccess {
                background: #d1fae5;
                border: 1px solid #14a05c;
                color: #065f46;
            }
            QLabel#statBadgeError {
                background: #fee2e2;
                border: 1px solid #e53935;
                color: #991b1b;
            }
            QLabel#deviceTitle {
                font-size: 14px;
                font-weight: 700;
                color: #1a2333;
            }
            QLabel#statusLabel {
                color: #9aa5bc;
                font-size: 12px;
            }
            QLabel#metaKey {
                color: #9aa5bc;
                font-weight: 600;
                font-size: 12px;
            }
            QLabel#statusDot {
                background: #14a05c;
                border-radius: 5px;
                min-width: 10px;
                min-height: 10px;
                max-width: 10px;
                max-height: 10px;
            }
            QFrame#deviceCard[cardState="busy"] QLabel#statusDot {
                background: #f59e0b;
            }
            QFrame#deviceCard[cardState="success"] QLabel#statusDot {
                background: #14a05c;
            }
            QFrame#deviceCard[cardState="new"] QLabel#statusDot {
                background: #2560e0;
            }
            QFrame#deviceCard[cardState="error"] QLabel#statusDot {
                background: #e53935;
            }
            QFrame#deviceCard[cardState="disconnected"] QLabel#statusDot {
                background: #9aa5bc;
            }
            QLabel#stateIdle {
                color: #14a05c;
                font-weight: 700;
                font-size: 12px;
            }
            QLabel#stateBusy {
                color: #b45309;
                font-weight: 700;
                font-size: 12px;
            }
            QLabel#stateSuccess {
                color: #065f46;
                font-weight: 700;
                font-size: 12px;
            }
            QLabel#stateError {
                color: #991b1b;
                font-weight: 700;
                font-size: 12px;
            }
            QLabel#stateDisconnected {
                color: #9aa5bc;
                font-weight: 700;
                font-size: 12px;
            }
            QLineEdit {
                selection-background-color: #c3d4f8;
            }
            QPlainTextEdit {
                background: #ffffff;
                border: 1px solid #dde1ea;
                border-radius: 7px;
                padding: 6px 8px;
                color: #1e2a3a;
                font-size: 11px;
                selection-background-color: #c3d4f8;
                selection-color: #1a2333;
            }
            QPlainTextEdit QScrollBar:vertical {
                background: transparent;
                width: 5px;
                border-radius: 2px;
                margin: 2px 0;
            }
            QPlainTextEdit QScrollBar::handle:vertical {
                background: rgba(180, 190, 210, 200);
                border-radius: 2px;
                min-height: 24px;
            }
            QPlainTextEdit QScrollBar::handle:vertical:hover {
                background: rgba(140, 155, 180, 240);
            }
            QPlainTextEdit QScrollBar::add-line:vertical,
            QPlainTextEdit QScrollBar::sub-line:vertical {
                height: 0;
            }
            QPlainTextEdit QScrollBar:horizontal {
                height: 0;
            }
            QProgressBar {
                background: #eef0f5;
                border: none;
                border-radius: 5px;
                text-align: center;
                color: #2560e0;
                font-size: 11px;
                font-weight: 600;
                min-height: 14px;
                max-height: 14px;
            }
            QProgressBar::chunk {
                border-radius: 5px;
                background: #2560e0;
            }
            QPushButton:hover {
                border-color: #b0b8cd;
            }
            QPushButton:pressed {
                background: #d8dde8;
            }
            QPushButton#primaryButton:pressed {
                background: #153d91;
            }
            QPushButton#resetButton {
                background: #fff7ed;
                border: 1px solid #fb923c;
                color: #c2410c;
            }
            QPushButton#resetButton:hover {
                background: #ffedd5;
            }
            QPushButton#resetButton:disabled {
                background: #f5f6f8;
                color: #b0b8cd;
                border-color: #e8eaef;
            }
            QPushButton#ghostButton {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                color: #334155;
            }
            QPushButton#ghostButton:hover {
                background: #f8fafc;
            }
            QPushButton#linkButton {
                background: #111827;
                border: 1px solid #0f172a;
                color: #ffffff;
                padding-right: 14px;
            }
            QPushButton#linkButton:hover {
                background: #1f2937;
            }
            QPushButton#floatingActionButton {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 7px;
                min-height: 26px;
                padding: 0 10px;
                color: #334155;
                font-size: 10px;
                font-weight: 600;
                text-align: center;
            }
            QPushButton#floatingActionButton:hover {
                background: #f8fafc;
            }
            QPushButton:checked {
                background: #2560e0;
                border-color: #1a4db5;
                color: #ffffff;
            }
            QPushButton#entryRemoveButton {
                background: #fff0f0;
                border: 1px solid #f5c0c0;
                border-radius: 4px;
                color: #991b1b;
                font-size: 12px;
                padding: 0px;
                min-width: 26px;
                max-width: 26px;
            }
            QPushButton#entryRemoveButton:hover {
                background: #fee2e2;
            }
            QPushButton#entryRemoveButton:disabled {
                background: #f5f5f5;
                border-color: #e0e0e0;
                color: #c0c0c0;
            }
            QPushButton#addEntryButton {
                background: #f0f5ff;
                border: 1px solid #c3d4f8;
                border-radius: 6px;
                color: #2560e0;
                font-size: 12px;
                padding: 3px 10px;
            }
            QPushButton#addEntryButton:hover {
                background: #dce8ff;
            }
            QScrollArea {
                border: none;
                background: transparent;
            }
            QScrollBar:vertical {
                background: #f0f2f5;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #c5cad4;
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QWidget#qt_scrollarea_viewport {
                background: transparent;
            }
            QLabel#emptyHint {
                color: #9aa5bc;
                font-size: 14px;
                padding: 40px 12px;
            }
            QFrame#mergeSplitFrame {
                background: #ffffff;
                border: 1px solid #e0e4ea;
                border-radius: 10px;
            }
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #f6f8fb;
                border: 1px solid #e0e4ea;
                border-radius: 8px;
                gridline-color: transparent;
                outline: 0;
                font-size: 13px;
            }
            QTableWidget::item {
                padding: 5px 8px;
                border: none;
            }
            QTableWidget::item:selected {
                background: #dbeafe;
                color: #1d4ed8;
            }
            QHeaderView::section {
                background: #f0f3f9;
                color: #6b7a94;
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 0.5px;
                padding: 4px 8px;
                border: none;
                border-bottom: 1px solid #e0e4ea;
            }
            QHeaderView::section:first {
                border-top-left-radius: 8px;
            }
            QHeaderView::section:last {
                border-top-right-radius: 8px;
            }
            QTableWidget QPushButton {
                font-size: 13px;
                padding: 0px 6px;
                border-radius: 5px;
                min-height: 28px;
                max-height: 28px;
            }
            QComboBox {
                background: #f8f9fb;
                border: 1px solid #dde1ea;
                border-radius: 7px;
                padding: 4px 8px;
                color: #1a2333;
                selection-background-color: #c3d4f8;
                min-height: 20px;
            }
            QComboBox:focus {
                border: 1.5px solid #2560e0;
                background: #ffffff;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 22px;
                border-left: 1px solid #dde1ea;
                border-top-right-radius: 7px;
                border-bottom-right-radius: 7px;
                background: #eef0f5;
            }
            QComboBox::down-arrow {
                image: url("__ARROW_PATH__");
                width: 10px;
                height: 10px;
            }
            QComboBox QAbstractItemView {
                background: #ffffff;
                border: 1px solid #dde1ea;
                border-radius: 4px;
                selection-background-color: #dbeafe;
                selection-color: #1d4ed8;
                padding: 2px;
                outline: 0;
            }
            QComboBox QAbstractItemView::item {
                min-height: 24px;
                padding: 2px 8px;
                border-radius: 3px;
            }
            QLabel#terminalPlaceholderIcon {
                font-size: 52px;
                font-weight: 700;
                color: #c5cad4;
                letter-spacing: 4px;
            }
            QLabel#terminalPlaceholderTitle {
                font-size: 20px;
                font-weight: 700;
                color: #9aa5bc;
            }
            """.replace("__ARROW_PATH__", _arrow_path)
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_floating_panels()

    def open_github_url(self) -> None:
        if not QDesktopServices.openUrl(QUrl(APP_GITHUB_URL)):
            QMessageBox.warning(self, "提示", f"无法打开链接：\n{APP_GITHUB_URL}")

    def _update_github_icon(self, avatar_bytes: bytes) -> None:
        icon = _build_avatar_icon(avatar_bytes)
        if icon is not None:
            self.github_button.setIcon(icon)

    def show_reference_notice(self) -> None:
        QMessageBox.information(
            self,
            f"引用说明 - {APP_TITLE}",
            _build_reference_notice(),
        )

    def auto_pick_firmware(self) -> None:
        if not self._flash_rows or self._flash_rows[0].path_edit.text().strip():
            return
        if not DEFAULT_FIRMWARE_DIR.exists():
            return
        # 优先选有十六进制地址标记的文件，其次所有 .bin，均按修改时间降序
        hex_stamped = sorted(
            DEFAULT_FIRMWARE_DIR.glob("*_0x*.bin"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        all_bins = sorted(
            DEFAULT_FIRMWARE_DIR.glob("*.bin"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        candidates = hex_stamped or all_bins
        if candidates:
            chosen = str(candidates[0])
            first = self._flash_rows[0]
            first.path_edit.setText(chosen)
            first._auto_set_addr(chosen)

    def _sync_new_badge(self, device_id: str) -> None:
        card = self.device_cards.get(device_id)
        if card is None:
            return
        info = self.device_infos.get(device_id)
        card.set_newly_detected(
            device_id in self.new_device_ids
            and device_id not in self.acknowledged_device_ids
            and (info is None or info.connected)
        )

    def _acknowledge_device(self, device_id: str) -> None:
        self.acknowledged_device_ids.add(device_id)
        self.new_device_ids.discard(device_id)
        self._sync_new_badge(device_id)

    def refresh_ports(self) -> None:
        previous_ids = set(self.device_cards)
        ports = []
        for port_info in list_ports.comports():
            if port_info.description and re.search(
                r"通信端口|Communications Port", port_info.description
            ):
                continue
            label = (
                port_info.description
                if port_info.description and port_info.description != "n/a"
                else "串口设备"
            )
            device_id = self._make_device_id(port_info)
            device = self._get_chip_info(port_info.device, label, device_id)
            device.serial_number = getattr(port_info, "serial_number", "") or ""
            device.hwid = getattr(port_info, "hwid", "") or ""
            ports.append(device)

        active_ids = {device.device_id for device in ports}
        removed_ids = [
            device_id for device_id in self.device_cards if device_id not in active_ids
        ]
        for device_id in removed_ids:
            card = self.device_cards.get(device_id)
            if card is None:
                continue
            if card.process is not None:
                disconnected = self.device_infos.get(
                    device_id,
                    DeviceInfo(
                        device_id=device_id, port=card.device.port, label="串口设备"
                    ),
                )
                disconnected.connected = False
                self.device_infos[device_id] = disconnected
                card.update_device(disconnected)
                card.set_result_state("设备已断开", "disconnected")
            else:
                self.device_cards.pop(device_id, None)
                self.device_infos.pop(device_id, None)
                card.setParent(None)
                card.deleteLater()

        for device in ports:
            self.device_infos[device.device_id] = device
            if device.device_id in self.device_cards:
                self.device_cards[device.device_id].update_device(device)
                self._sync_new_badge(device.device_id)
                if self.device_cards[device.device_id].process is None:
                    self.device_cards[device.device_id].set_result_state("空闲", "idle")
            else:
                card = DeviceCard(device)
                card.erase_button.clicked.connect(
                    lambda _checked=False,
                    device_id=device.device_id: self._handle_device_action(
                        device_id, "erase"
                    )
                )
                card.flash_button.clicked.connect(
                    lambda _checked=False,
                    device_id=device.device_id: self._handle_device_action(
                        device_id, "flash"
                    )
                )
                card.reset_button.clicked.connect(
                    lambda _checked=False,
                    device_id=device.device_id: self._handle_device_action(
                        device_id, "reset"
                    )
                )
                card.export_button.clicked.connect(
                    lambda _checked=False,
                    device_id=device.device_id: self._handle_device_action(
                        device_id, "export"
                    )
                )
                card.efuse_button.clicked.connect(
                    lambda _checked=False,
                    device_id=device.device_id: self._open_efuse_dialog(device_id)
                )
                card.efuse_button.setContextMenuPolicy(
                    Qt.ContextMenuPolicy.CustomContextMenu
                )
                card.efuse_button.customContextMenuRequested.connect(
                    lambda pos, device_id=device.device_id: self._show_efuse_context_menu(
                        device_id, pos
                    )
                )
                self.device_cards[device.device_id] = card
                if device.device_id not in self.acknowledged_device_ids:
                    self.new_device_ids.add(device.device_id)
                self._sync_new_badge(device.device_id)
                if self._card_scale != 1.0:
                    card.apply_scale(self._card_scale)
                if self.auto_flash_button.isChecked():
                    QTimer.singleShot(
                        100,
                        lambda device_id=device.device_id: self._auto_flash_new_device(
                            device_id
                        ),
                    )

        self._rebuild_device_grid()
        self._update_stats()
        self.device_count_label.setText(f"{len(self.device_cards)} 台")
        self.status_label.setText(
            "状态: 未发现可用设备"
            if not self.device_cards
            else f"状态: 已识别 {len(self.device_cards)} 台设备，新增 {len(set(self.device_cards) - previous_ids)} 台"
        )

    def _update_stats(self) -> None:
        total = len(self.device_cards)
        running = sum(
            1 for card in self.device_cards.values() if card.process is not None
        )
        success = sum(
            1
            for card in self.device_cards.values()
            if card.property("cardState") == "success"
        )
        failed = sum(
            1
            for card in self.device_cards.values()
            if card.property("cardState") in {"error", "disconnected"}
        )
        self.total_stat.setText(f"总数 {total}")
        self.running_stat.setText(f"运行中 {running}")
        self.success_stat.setText(f"成功 {success}")
        self.failed_stat.setText(f"失败 {failed}")

    def _rebuild_device_grid(self) -> None:
        cards = sorted(
            self.device_cards.values(),
            key=lambda card: self._port_sort_key(card.device.port),
        )
        while self.device_layout.count() > 0:
            item = self.device_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        if not cards:
            self.device_layout.addWidget(self.empty_hint)
            return
        for card in cards:
            self.device_layout.addWidget(card)

    def _port_sort_key(self, port: str) -> tuple[int, str]:
        match = re.search(r"(\d+)$", port)
        if match:
            return (int(match.group(1)), port)
        return (10**9, port)

    def _make_device_id(self, port_info) -> str:
        serial_number = getattr(port_info, "serial_number", "") or ""
        location = getattr(port_info, "location", "") or ""
        vid = getattr(port_info, "vid", None)
        pid = getattr(port_info, "pid", None)
        hwid = getattr(port_info, "hwid", "") or ""
        description = getattr(port_info, "description", "") or ""
        if serial_number:
            return f"sn:{serial_number}"
        if location and vid is not None and pid is not None:
            return f"loc:{vid:04x}:{pid:04x}:{location}"
        if hwid:
            return f"hwid:{hwid}"
        return f"port:{port_info.device}:{description}"

    def _get_chip_info(self, port: str, label: str, device_id: str) -> DeviceInfo:
        info = DeviceInfo(device_id=device_id, port=port, label=label)
        if not _tool_backend_available("esptool"):
            return info
        try:
            result = subprocess.run(
                _build_tool_command(
                    "esptool",
                    "--port",
                    port,
                    "--baud",
                    DETECT_BAUD,
                    "flash-id",
                ),
                capture_output=True,
                text=True,
                timeout=2.5,
                cwd=str(TOOL_DIR),
                env=_build_process_env_dict(),
            )
            output = (result.stdout or "") + (result.stderr or "")
            chip_name = self._extract_with_patterns(
                output,
                [r"Chip is\s+(.+)", r"Detecting chip type\.\.\.\s*(.+)"],
            )
            if chip_name:
                info.chip_name = chip_name
            info.features = self._extract_with_patterns(output, [r"Features:\s*(.+)"])
            info.mac = self._extract_with_patterns(output, [r"MAC:\s*(.+)"])
            info.flash_size = self._extract_with_patterns(
                output, [r"Detected flash size:\s*(.+)"]
            )
            info.crystal = self._extract_with_patterns(
                output, [r"Crystal frequency:\s*(.+)", r"Crystal is\s*(.+)"]
            )
        except subprocess.TimeoutExpired:
            info.chip_name = "识别超时"
        except Exception:
            info.chip_name = "识别失败"
        return info

    def _extract_with_patterns(self, text: str, patterns: list[str]) -> str:
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip()
        return ""

    # ── 多条目烧录管理 ──────────────────────────────────────────

    def _add_flash_entry(self, addr: str = "0x0", path: str = "") -> "FlashEntryRow":
        row = FlashEntryRow(addr, path, parent=self._entries_inner)
        row.remove_requested.connect(lambda r=row: self._remove_flash_entry(r))
        self._flash_rows.append(row)
        self._entries_vbox.addWidget(row)
        self._update_remove_btns()
        return row

    def _remove_flash_entry(self, row: "FlashEntryRow") -> None:
        if len(self._flash_rows) <= 1:
            return
        self._flash_rows.remove(row)
        self._entries_vbox.removeWidget(row)
        row.deleteLater()
        self._update_remove_btns()

    def _update_remove_btns(self) -> None:
        can_remove = len(self._flash_rows) > 1
        for r in self._flash_rows:
            r.remove_btn.setEnabled(can_remove)

    def _get_valid_flash_entries(self) -> "list[tuple[str, str]] | None":
        """
        校验所有烧录条目并返回 [(addr, path), ...]；任一条目不合法则弹窗并返回 None。
        空行（地址和路径均为空）直接跳过。
        """
        entries: list[tuple[str, str]] = []
        for row in self._flash_rows:
            addr = row.addr_edit.text().strip()
            path = row.path_edit.text().strip()
            if not addr and not path:
                continue
            if not addr or not re.match(r'^0[xX][0-9a-fA-F]+$', addr):
                QMessageBox.warning(
                    self,
                    "提示",
                    f"烧录地址格式不正确：{addr!r}，应为十六进制如 0x0、0x10000。",
                )
                return None
            if not path or not Path(path).is_file():
                QMessageBox.warning(self, "提示", f"请选择有效的固件文件（当前：{path!r}）。")
                return None
            entries.append((addr, path))
        if not entries:
            QMessageBox.warning(self, "提示", "请至少填写一个有效的烧录条目。")
            return None
        return entries

    def _validate_common_inputs(self, require_bin: bool) -> bool:
        if not _tool_backend_available("esptool"):
            QMessageBox.critical(
                self,
                "错误",
                "未找到可用 esptool。请保留仓库内 esptool 目录，或先执行 pip install -r requirements.txt。",
            )
            return False
        baud_text = self.baud_edit.currentText().strip()
        if not baud_text.isdigit():
            QMessageBox.warning(self, "提示", "公共波特率必须是数字。")
            return False
        if require_bin:
            return self._get_valid_flash_entries() is not None
        return True

    def _build_esptool_base_args(self, device_id: str) -> list[str]:
        info = self.device_infos.get(device_id)
        port = info.port if info else ""
        chip_arg = resolve_chip_arg(info.chip_name if info else "")
        return [
            "--chip",
            chip_arg,
            "--port",
            port,
            "--baud",
            self.baud_edit.currentText().strip(),
        ]

    def _handle_device_action(self, device_id: str, action: str) -> None:
        card = self.device_cards.get(device_id)
        if card is not None and card.process is not None:
            self.stop_process(device_id)
            return
        if action == "erase":
            self.erase_flash(device_id)
        elif action == "flash":
            self.flash_firmware(device_id)
        elif action == "reset":
            self.reset_device(device_id)
        elif action == "export":
            self.export_firmware(device_id)

    @staticmethod
    def _normalize_read_flash_size(size: str) -> str:
        value = size.strip()
        if value.lower() == "all":
            return "ALL"
        if value.endswith("m"):
            return value[:-1] + "M"
        if value.endswith("K"):
            return value[:-1] + "k"
        return value

    def reset_device(self, device_id: str, acknowledge: bool = True) -> None:
        if not _tool_backend_available("esptool"):
            QMessageBox.critical(self, "错误", "未找到可用 esptool。")
            return
        baud_text = self.baud_edit.currentText().strip()
        if not baud_text.isdigit():
            QMessageBox.warning(self, "提示", "公共波特率必须是数字。")
            return
        if acknowledge:
            self._acknowledge_device(device_id)
        card = self.device_cards[device_id]
        esptool_args = self._build_esptool_base_args(device_id) + ["run"]
        self._start_process(
            card,
            _build_tool_worker_command("esptool", *esptool_args),
            "正在复位",
            active_action="reset",
            display_command=subprocess.list2cmdline(
                _build_tool_command("esptool", *esptool_args)
            ),
            backend_text="内置 esptool API worker",
        )

    def export_firmware(self, device_id: str, acknowledge: bool = True) -> None:
        if not _tool_backend_available("esptool"):
            QMessageBox.critical(self, "错误", "未找到可用 esptool。")
            return
        baud_text = self.baud_edit.currentText().strip()
        if not baud_text.isdigit():
            QMessageBox.warning(self, "提示", "公共波特率必须是数字。")
            return
        info = self.device_infos.get(device_id)
        card = self.device_cards.get(device_id)
        if info is None or card is None:
            return

        dlg = ExportFlashDialog(info, baud_text, parent=self)
        if not dlg.exec():
            return
        config = dlg.export_config()

        output_dir = get_existing_directory(
            self,
            "选择导出目录",
        )
        if not output_dir:
            return
        output_path = Path(output_dir) / config.filename
        if output_path.exists():
            reply = QMessageBox.question(
                self,
                "确认覆盖",
                f"文件已存在，是否覆盖？\n\n{output_path}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        if acknowledge:
            self._acknowledge_device(device_id)
        size_arg = self._normalize_read_flash_size(config.size)
        esptool_args = self._build_esptool_base_args(device_id) + [
            "read-flash",
            config.address,
            size_arg,
            str(output_path),
        ]
        self._start_process(
            card,
            _build_tool_worker_command("esptool", *esptool_args),
            "正在导出",
            active_action="export",
            display_command=subprocess.list2cmdline(
                _build_tool_command("esptool", *esptool_args)
            ),
            backend_text="内置 esptool API worker",
        )

    def erase_flash(self, device_id: str, acknowledge: bool = True) -> None:
        if not self._validate_common_inputs(require_bin=False):
            return
        if acknowledge:
            self._acknowledge_device(device_id)
        card = self.device_cards[device_id]
        esptool_args = self._build_esptool_base_args(device_id) + [
            "erase-region",
            "0x0",
            "ALL",
        ]
        self._start_process(
            card,
            _build_tool_worker_command("esptool", *esptool_args),
            "正在擦除",
            active_action="erase",
            display_command=subprocess.list2cmdline(
                _build_tool_command("esptool", *esptool_args)
            ),
            backend_text="内置 esptool API worker",
        )

    def flash_firmware(self, device_id: str, acknowledge: bool = True) -> None:
        entries = self._get_valid_flash_entries()
        if entries is None:
            return
        if not _tool_backend_available("esptool"):
            QMessageBox.critical(self, "错误", "未找到可用 esptool。")
            return
        baud_text = self.baud_edit.currentText().strip()
        if not baud_text.isdigit():
            QMessageBox.warning(self, "提示", "公共波特率必须是数字。")
            return
        if acknowledge:
            self._acknowledge_device(device_id)
        card = self.device_cards[device_id]
        flash_pairs: list[str] = []
        for addr, path in entries:
            flash_pairs += [addr, path]
        esptool_args = self._build_esptool_base_args(device_id) + ["write-flash"] + flash_pairs
        self._start_process(
            card,
            _build_tool_worker_command("esptool", *esptool_args),
            "正在烧录",
            active_action="flash",
            display_command=subprocess.list2cmdline(
                _build_tool_command("esptool", *esptool_args)
            ),
            backend_text="内置 esptool API worker",
        )

    def _auto_flash_new_device(self, device_id: str) -> None:
        card = self.device_cards.get(device_id)
        info = self.device_infos.get(device_id)
        if (
            card is None
            or info is None
            or not info.connected
            or card.process is not None
        ):
            return
        if not self._validate_common_inputs(require_bin=True):
            return
        self.flash_firmware(device_id, acknowledge=False)

    def erase_all_devices(self) -> None:
        if not self._validate_common_inputs(require_bin=False):
            return
        for port, card in list(self.device_cards.items()):
            info = self.device_infos.get(port)
            if card.process is None and (info is None or info.connected):
                self.erase_flash(port)

    def flash_all_devices(self) -> None:
        if not self._validate_common_inputs(require_bin=True):
            return
        for port, card in list(self.device_cards.items()):
            info = self.device_infos.get(port)
            if card.process is None and (info is None or info.connected):
                self.flash_firmware(port)

    def stop_all_devices(self) -> None:
        for port in list(self.device_cards):
            self.stop_process(port)

    def clear_devices(self) -> None:
        """移除所有设备卡片并重置 NEW 状态，下次刷新时所有设备重新判定为新设备。"""
        for card in list(self.device_cards.values()):
            if card.process is not None:
                card.process.kill()
                card.process = None
            card.setParent(None)
            card.deleteLater()
        self.device_cards.clear()
        self.device_infos.clear()
        self.new_device_ids.clear()
        self.acknowledged_device_ids.clear()
        self._rebuild_device_grid()
        self._update_stats()
        self.device_count_label.setText("0 台")
        self.status_label.setText("状态: 已清空，等待设备接入")

    def toggle_auto_refresh(self, enabled: bool) -> None:
        self.auto_refresh_button.setText(f"自动刷新: {'开' if enabled else '关'}")
        if enabled:
            self.refresh_timer.start()
            self.refresh_ports()
        else:
            self.refresh_timer.stop()

    def toggle_auto_flash(self, enabled: bool) -> None:
        self.auto_flash_button.setText(f"自动烧录: {'开' if enabled else '关'}")

    def _start_process(
        self,
        card: DeviceCard,
        command: list[str],
        busy_text: str,
        active_action: str | None = None,
        display_command: str | None = None,
        backend_text: str | None = None,
        _is_retry: bool = False,
    ) -> None:
        if card.process is not None:
            QMessageBox.information(
                self, "提示", f"{card.device.port} 当前已有任务在运行。"
            )
            return
        if not _is_retry:
            card._auto_retry_count = 0
        card._last_command = list(command)
        card._last_command_meta = {
            "busy_text": busy_text,
            "active_action": active_action,
            "display_command": display_command,
            "backend_text": backend_text,
        }
        card.append_log("=" * 72)
        card.append_log("命令: " + (display_command or subprocess.list2cmdline(command)))
        card.append_log(f"工作目录: {TOOL_DIR}")
        if backend_text is None:
            if _FROZEN:
                backend_text = "内置 EXE 分发入口"
            elif _local_esptool_available():
                backend_text = "本地 esptool 模块"
            else:
                backend_text = "当前 Python 环境模块"
        card.append_log(f"后端: {backend_text}")
        process = QProcess(self)
        card.process = process
        process.setProgram(command[0])
        process.setArguments(command[1:])
        process.setWorkingDirectory(str(TOOL_DIR))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        _inject_local_esptool_pythonpath(env)
        env.insert("PYTHONUTF8", "1")
        process.setProcessEnvironment(env)
        process.readyRead.connect(
            lambda did=card.device.device_id: self._read_process_output(did)
        )
        process.finished.connect(
            lambda exit_code,
            exit_status,
            did=card.device.device_id: self._process_finished(did, exit_code, exit_status)
        )
        process.errorOccurred.connect(
            lambda error, did=card.device.device_id: self._process_error(did, error)
        )
        card.set_running_state(busy_text, True, active_action=active_action)
        initial_stage_text = ""
        if "read-flash" in command or "read_flash" in command:
            initial_stage_text = "准备导出 Flash"
        elif "write-flash" in command:
            initial_stage_text = "准备连接设备"
        elif "erase-region" in command:
            initial_stage_text = "准备擦除 Flash"
        elif "reset-chip" in command or (len(command) > 0 and command[-1] == "run"):
            initial_stage_text = "准备复位设备"
        if initial_stage_text:
            card.set_stage_text(initial_stage_text)
            card.log_stage(initial_stage_text)
        self.status_label.setText(f"状态: {card.device.port} {busy_text}")
        card.append_log("[状态] 正在启动进程...")
        process.start()
        if not process.waitForStarted(3000):
            card.append_log("进程启动失败。")
            card.set_running_state("启动失败", False)
            self._cleanup_process(card.device.device_id)
            return
        card.append_log(f"[状态] 进程已启动，PID: {process.processId()}")
        card.append_log("[状态] 等待工具输出...")

    def _read_process_output(self, device_id: str) -> None:
        card = self.device_cards.get(device_id)
        if card is None or card.process is None:
            return
        data = bytes(card.process.readAll()).decode("utf-8", errors="replace")
        if data:
            self._handle_process_output_chunk(card, data)

    def _handle_process_output_chunk(self, card: DeviceCard, chunk: str) -> None:
        normalized = chunk.replace("\r\n", "\n").replace("\r", "\n")
        card._process_output_buffer += normalized
        lines = card._process_output_buffer.split("\n")
        card._process_output_buffer = lines.pop()
        for line in lines:
            self._handle_process_output_line(card, line)

    def _flush_process_output_buffer(self, card: DeviceCard) -> None:
        if card._process_output_buffer:
            remaining = card._process_output_buffer
            card._process_output_buffer = ""
            self._handle_process_output_line(card, remaining)

    def _handle_process_output_line(self, card: DeviceCard, line: str) -> None:
        if line.startswith(_INTERNAL_WORKER_EVENT_PREFIX):
            payload = line[len(_INTERNAL_WORKER_EVENT_PREFIX):]
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                card.append_log(line)
                self._update_progress_from_output(card, line)
                return
            self._handle_worker_event(card, event)
            return
        card.append_log(line)
        self._update_progress_from_output(card, line)

    def _handle_worker_event(self, card: DeviceCard, event: dict[str, object]) -> None:
        event_type = str(event.get("type", ""))
        if event_type == "log":
            text = str(event.get("text", ""))
            card.append_log(text)
            self._update_progress_from_output(card, text)
            return
        if event_type == "progress":
            total = int(event.get("total", 0) or 0)
            current = int(event.get("current", 0) or 0)
            percent = int(event.get("percent", 0) or 0)
            prefix = str(event.get("prefix", "") or "执行中")
            suffix = str(event.get("suffix", "") or "")
            if "reading from" in prefix.lower():
                prefix = "正在导出 Flash"
            if total > 0:
                percent = max(0, min(100, percent))
            progress_text = " ".join(part for part in [prefix.strip(), f"{percent}%", suffix.strip()] if part)
            card.set_progress(percent, progress_text)
            return
        if event_type == "fatal":
            text = str(event.get("text", ""))
            if text:
                card.append_log(text)
            return

    def _update_progress_from_output(self, card: DeviceCard, text: str) -> None:
        lower_text = text.lower()

        stage_patterns = [
            (r"serial port", "串口连接中"),
            (r"connecting", "正在连接设备"),
            (r"chip is", "已识别芯片"),
            (r"detecting chip type", "正在识别芯片"),
            (r"features:", "正在读取芯片特性"),
            (r"changing baud rate", "正在切换波特率"),
            (r"reading from", "正在导出 Flash"),
            (r"read \d+ bytes from", "导出完成"),
            (r"erasing flash", "正在擦除 Flash"),
            (r"erase completed", "擦除完成"),
            (r"flash will be erased", "准备擦除目标区域"),
            (r"writing at", "正在写入 Flash"),
            (r"wrote", "写入完成，等待校验"),
            (r"hash of data verified", "校验成功"),
            (r"hard resetting via rts pin", "正在复位设备"),
        ]

        stage_text = None
        for pattern, label in stage_patterns:
            if re.search(pattern, lower_text):
                stage_text = label

        matches = re.findall(r"(\d+)%", text)
        if matches:
            try:
                percent = int(matches[-1])
            except ValueError:
                percent = None
            if percent is not None:
                if stage_text:
                    card.log_stage(stage_text)
                prefix = stage_text or "执行中"
                card.set_progress(percent, f"{prefix} {percent}%")
                return

        if stage_text:
            card.log_stage(stage_text)
            current = card.progress_bar.value()
            if stage_text in {"正在连接设备", "串口连接中"}:
                card.set_progress(max(current, 3), stage_text)
            elif stage_text in {"正在识别芯片", "已识别芯片", "正在读取芯片特性"}:
                card.set_progress(max(current, 8), stage_text)
            elif stage_text in {"准备擦除 Flash", "准备擦除目标区域", "正在擦除 Flash"}:
                card.set_progress(max(current, 15), stage_text)
            elif stage_text == "正在导出 Flash":
                card.set_progress(max(current, 15), stage_text)
            elif stage_text in {"擦除完成", "正在写入 Flash"}:
                card.set_progress(max(current, 55), stage_text)
            elif stage_text in {"写入完成，等待校验", "校验成功", "导出完成"}:
                card.set_progress(max(current, 92), stage_text)
            elif stage_text == "正在复位设备":
                card.set_progress(max(current, 97), stage_text)
            else:
                card.set_stage_text(stage_text)

    def _process_finished(
        self, device_id: str, exit_code: int, _exit_status: QProcess.ExitStatus
    ) -> None:
        card = self.device_cards.get(device_id)
        if card is None:
            return
        self._read_process_output(device_id)
        self._flush_process_output_buffer(card)
        card.append_log(f"[状态] 进程已结束，退出码: {exit_code}")
        if exit_code == 0:
            card.append_log("任务完成。")
            success_text = (
                "导出成功"
                if card._last_command_meta.get("active_action") == "export"
                else "执行成功"
            )
            card.set_result_state(success_text, "success")
            self._update_device_info_from_log(card, device_id)
        else:
            if self._try_auto_retry(card, device_id):
                return
            card.append_log(f"任务失败，退出码: {exit_code}")
            card.set_result_state(f"执行失败({exit_code})", "error")
        self._update_stats()
        self.status_label.setText(f"状态: {card.device.port} 任务结束")
        self._cleanup_process(device_id)

    def _process_error(self, device_id: str, error: QProcess.ProcessError) -> None:
        card = self.device_cards.get(device_id)
        if card is None:
            return
        card.append_log(f"[状态] 进程错误事件: {error}")
        card.append_log(f"进程错误: {error}")
        card.set_result_state(f"进程错误({error})", "error")
        self._update_stats()
        self.status_label.setText(f"状态: {card.device.port} 发生进程错误")
        self._cleanup_process(device_id)

    def stop_process(self, device_id: str) -> None:
        card = self.device_cards.get(device_id)
        if card is None or card.process is None:
            return
        self._acknowledge_device(device_id)
        card.append_log("用户请求停止任务。")
        card.process.kill()
        card.set_result_state("已停止", "error")
        self._update_stats()
        self.status_label.setText(f"状态: {card.device.port} 已停止")
        self._cleanup_process(device_id)

    def _open_efuse_dialog(self, device_id: str) -> None:
        info = self.device_infos.get(device_id)
        if info is None:
            return
        self._acknowledge_device(device_id)
        baud = self.baud_edit.currentText().strip() or "115200"
        dlg = EFuseDialog(info, baud, parent=self)
        dlg.exec()

    def _show_efuse_context_menu(self, device_id: str, pos) -> None:
        info = self.device_infos.get(device_id)
        if info is None:
            return
        chip_lower = (info.chip_name or "").lower()
        # 查找匹配当前芯片的预设列表（键为子串匹配）
        active_presets = next(
            (v for k, v in EFUSE_CHIP_PRESETS.items() if k in chip_lower), []
        )
        if not active_presets:
            return
        menu = QMenu(self)
        menu.setObjectName("efuseContextMenu")
        actions = []
        for label, name, value, desc in active_presets:
            act = menu.addAction(f"烧录 {name}（{label}）")
            actions.append((act, label, name, value, desc))
        card = self.device_cards.get(device_id)
        sender_btn = card.efuse_button if card else None
        global_pos = sender_btn.mapToGlobal(pos) if sender_btn else self.mapToGlobal(pos)
        chosen = menu.exec(global_pos)
        for act, label, name, value, desc in actions:
            if chosen is act:
                self._burn_efuse_preset(device_id, efuse_name=name, value=value, label=label, description=desc)
                break

    def _burn_efuse_preset(
        self, device_id: str, efuse_name: str, value: str, label: str, description: str = ""
    ) -> None:
        info = self.device_infos.get(device_id)
        if info is None:
            return
        desc_html = f"<br><br>{description}" if description else ""
        reply = QMessageBox.warning(
            self,
            "确认烧录 eFuse",
            f"即将对设备 <b>{info.port}</b>（{info.chip_name}）烧录 eFuse："
            f"<br><br><b>{efuse_name} = {value}</b>（{label}）"
            f"{desc_html}"
            f"<br><br>⚠️ eFuse 烧录不可逆，请确认后再操作。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        baud = self.baud_edit.currentText().strip() or "115200"
        chip_arg = resolve_chip_arg(info.chip_name)
        espefuse_args = [
            "--chip", chip_arg,
            "--port", info.port,
            "--baud", baud,
            "--do-not-confirm",
            "burn-efuse",
            efuse_name, value,
        ]
        card = self.device_cards.get(device_id)
        if card is None:
            return
        self._start_process(
            card,
            _build_tool_worker_command("espefuse", *espefuse_args),
            f"烧录中 ({efuse_name})",
            active_action="flash",
            display_command=subprocess.list2cmdline(
                _build_tool_command("espefuse", *espefuse_args)
            ),
            backend_text="内置 espefuse API worker",
        )

    def _try_auto_retry(self, card: DeviceCard, device_id: str) -> bool:
        """检测芯片类型不匹配错误并自动重试，返回 True 表示已触发重试。"""
        if card._auto_retry_count >= 1:
            return False
        log_text = card.log_edit.toPlainText()
        chip_match = re.search(r"This chip is ([\w\-]+), not", log_text)
        if not chip_match:
            return False
        try:
            chip_idx = card._last_command.index("--chip")
        except ValueError:
            return False
        actual_chip = chip_match.group(1).lower().replace("-", "")
        new_cmd = list(card._last_command)
        new_cmd[chip_idx + 1] = actual_chip
        meta = card._last_command_meta
        new_display = meta.get("display_command") or ""
        if new_display:
            new_display = re.sub(r"--chip\s+\S+", f"--chip {actual_chip}", new_display)
        card._auto_retry_count += 1
        # 立即更新卡片显示的芯片名（使用原始大小写，如 "ESP32-C6")
        detected_chip_display = chip_match.group(1)  # e.g. "ESP32-C6"
        card.device.chip_name = detected_chip_display
        info = self.device_infos.get(device_id)
        if info is not None:
            info.chip_name = detected_chip_display
        card.update_device(card.device)
        card.append_log(
            f"[自动重试] 检测到实际芯片: {chip_match.group(1)} "
            f"→ 切换 --chip {actual_chip} 后重新执行..."
        )
        self._cleanup_process(device_id)
        QTimer.singleShot(
            300,
            lambda: self._start_process(
                card,
                new_cmd,
                meta.get("busy_text", "重试中"),
                active_action=meta.get("active_action"),
                display_command=new_display or None,
                backend_text=meta.get("backend_text"),
                _is_retry=True,
            ),
        )
        return True

    def _update_device_info_from_log(self, card: DeviceCard, device_id: str) -> None:
        """重试成功后从日志文本中解析完整芯片信息并刷新卡片。"""
        log_text = card.log_edit.toPlainText()
        info = self.device_infos.get(device_id)
        if info is None:
            return
        chip = self._extract_with_patterns(
            log_text,
            [
                r"Chip type:\s+(.+)",
                r"Chip is\s+(.+)",
                r"Connected to\s+(ESP[\w\-]+(?:\s+\([^)]+\))*)",
            ],
        )
        if chip:
            info.chip_name = chip.strip().rstrip(":")
        features = self._extract_with_patterns(log_text, [r"Features:\s*(.+)"])
        if features:
            info.features = features.strip()
        mac = self._extract_with_patterns(
            log_text, [r"BASE MAC:\s*([0-9a-fA-F:]+)", r"\bMAC:\s*([0-9a-fA-F:]+)"]
        )
        if mac:
            info.mac = mac.strip()
        # Flash: \u4f18\u5148\u4ece Features \u884c\u63d0\u53d6 Embedded Flash\uff0c\u5176\u6b21\u518d\u7528 Detected flash size
        flash = self._extract_with_patterns(
            log_text,
            [r"Detected flash size:\s*(.+)", r"Embedded Flash\s+([\w\-]+)"],
        )
        if flash:
            info.flash_size = flash.strip()
        crystal = self._extract_with_patterns(
            log_text, [r"Crystal frequency:\s*(.+)", r"Crystal is\s+(.+)"]
        )
        if crystal:
            info.crystal = crystal.strip()
        card.update_device(info)
        card.append_log(f"[设备信息] 已更新芯片信息: {info.chip_name}")

    def _cleanup_process(self, device_id: str) -> None:
        card = self.device_cards.get(device_id)
        if card is None or card.process is None:
            return
        card.process.deleteLater()
        card.process = None
        self._update_stats()

    def _adjust_card_scale(self, delta: float) -> None:
        new_scale = max(0.9, min(5.0, self._card_scale + delta))
        if new_scale != self._card_scale:
            self._card_scale = new_scale
            self._apply_card_scale()

    def _set_card_scale(self, scale: float) -> None:
        self._card_scale = scale
        self._apply_card_scale()

    def _apply_card_scale(self) -> None:
        for card in self.device_cards.values():
            card.apply_scale(self._card_scale)

    def eventFilter(self, source, event):
        if event.type() == QEvent.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y()
                self._adjust_card_scale(0.05 if delta > 0 else -0.05)
                return True
        return super().eventFilter(source, event)

    def closeEvent(self, event) -> None:
        running = [c for c in self.device_cards.values() if c.process is not None]
        if running:
            reply = QMessageBox.question(
                self,
                "确认退出",
                f"有 {len(running)} 个任务正在运行，确定退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.stop_all_devices()
        self._efuse_batch_widget._stop_all()
        self._verify_widget.stop_all_tasks()
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            key = event.key()
            if key in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
                self._adjust_card_scale(0.1)
                return
            elif key == Qt.Key.Key_Minus:
                self._adjust_card_scale(-0.1)
                return
            elif key == Qt.Key.Key_0:
                self._set_card_scale(1.0)
                return
        super().keyPressEvent(event)


def main() -> int:
    # Windows: 设置 AppUserModelID，使开始菜单/任务栏使用本应用图标
    # 必须在 QApplication 创建之前调用，否则 Windows 会将进程归入 Python 分组
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "onexs.otool_esptool_ui"
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    # PyInstaller onefile: 打包资源解压到 sys._MEIPASS，而非 TOOL_DIR（EXE 目录）
    _meipass = getattr(sys, "_MEIPASS", None)
    _ico_path = (Path(_meipass) if _meipass else TOOL_DIR) / "logo_all_size.ico"
    _qicon = QIcon(str(_ico_path)) if _ico_path.exists() else None
    if _qicon:
        app.setWindowIcon(_qicon)
    window = OtoolEsptoolUI()
    if _qicon:
        window.setWindowIcon(_qicon)

    # 处理 --tab N 参数（从 Jump List 任务启动时，在 show() 前设置，避免页面闪烁）
    if "--tab" in sys.argv:
        try:
            _tab_idx = int(sys.argv[sys.argv.index("--tab") + 1])
            window.tab_switcher.set_current(_tab_idx, animated=False)
        except (ValueError, IndexError):
            pass

    window.show()

    # Windows Jump List：向任务栏注册快捷任务（任务对应 --tab 参数切换页面）
    setup_jump_list(
        app_id="onexs.otool_esptool_ui",
        exe_path=sys.executable,
        tasks=[("烧录", ""), ("合成", "--tab 3"), ("终端", "--tab 4")],
        script_path=TOOL_DIR / "otool_esptool_ui.py",
        icon_path=None if _FROZEN else _ico_path,
    )

    # Windows 11: Qt 的 setWindowIcon 在某些情况下不发送 WM_SETICON。
    # 用 LoadImageW + SendMessageW 直接写入窗口句柄，确保任务栏/开始菜单显示正确图标。
    if sys.platform == "win32" and _ico_path.exists():
        try:
            import ctypes
            import ctypes.wintypes
            _WM_SETICON = 0x0080
            _ICON_SMALL = 0
            _ICON_BIG = 1
            _IMAGE_ICON = 1
            _LR_LOADFROMFILE = 0x0010
            _LR_DEFAULTSIZE = 0x0040
            _hwnd = int(window.winId())
            _hicon_big = ctypes.windll.user32.LoadImageW(
                None, str(_ico_path), _IMAGE_ICON, 0, 0,
                _LR_LOADFROMFILE | _LR_DEFAULTSIZE,
            )
            _hicon_small = ctypes.windll.user32.LoadImageW(
                None, str(_ico_path), _IMAGE_ICON, 16, 16,
                _LR_LOADFROMFILE,
            )
            if _hicon_big:
                ctypes.windll.user32.SendMessageW(_hwnd, _WM_SETICON, _ICON_BIG, _hicon_big)
            if _hicon_small:
                ctypes.windll.user32.SendMessageW(_hwnd, _WM_SETICON, _ICON_SMALL, _hicon_small)
        except Exception:
            pass

    return app.exec()
