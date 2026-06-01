from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

import serial
from PyQt6.QtCore import QEvent, QThread, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from .styles import BASE_STYLESHEET


TERMINAL_BAUD_OPTIONS = [
    "9600",
    "19200",
    "38400",
    "57600",
    "74880",
    "115200",
    "230400",
    "460800",
    "921600",
    "1500000",
    "2000000",
]

ENCODING_OPTIONS = [
    "utf-8",
    "gbk",
    "gb18030",
    "big5",
    "shift_jis",
    "latin-1",
    "ascii",
]

CUSTOM_PORT_DATA = "__custom_port__"
CUSTOM_PORT_LABEL = "自定义串口..."


@dataclass(frozen=True)
class TerminalSerialConfig:
    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    flow_control: str


@dataclass
class TerminalLogRecord:
    record_id: int
    direction: str
    timestamp: datetime
    payload: bytes = b""
    message: str = ""


@dataclass
class TerminalSession:
    session_id: int
    port: str
    description: str
    status: str = "未连接"
    config: TerminalSerialConfig | None = None
    mode: str = "terminal"
    encoding: str = "utf-8"
    newline: str = "\r\n"
    timestamp_enabled: bool = False
    hex_enabled: bool = False
    auto_follow: bool = True
    inline_input: str = ""
    records: list[TerminalLogRecord] = field(default_factory=list)
    record_hex_overrides: dict[int, bool] = field(default_factory=dict)
    send_history: list[str] = field(default_factory=list)
    serial_thread: "TerminalSerialThread | None" = None
    listening: bool = False
    closing_requested: bool = False


def _port_sort_key(port: str) -> tuple[int, str]:
    match = re.search(r"(\d+)$", port)
    if match:
        return (int(match.group(1)), port)
    return (10**9, port)


def _format_hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


class TerminalSerialThread(QThread):
    opened = pyqtSignal(str)
    data_received = pyqtSignal(bytes)
    error = pyqtSignal(str)
    closed = pyqtSignal()

    def __init__(self, config: TerminalSerialConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._stop_event = threading.Event()
        self._reset_requested = threading.Event()
        self._send_queue: queue.Queue[bytes] = queue.Queue()
        self._serial: serial.Serial | None = None

    def request_stop(self) -> None:
        self._stop_event.set()

    def request_reset(self) -> None:
        self._reset_requested.set()

    def send(self, data: bytes) -> None:
        if data:
            self._send_queue.put(data)

    def run(self) -> None:
        try:
            self._serial = self._open_serial()
            self.opened.emit(
                f"{self._config.port} 已连接 @ {self._config.baudrate} bps"
            )
            self._read_write_loop()
        except serial.SerialException as exc:
            self.error.emit(f"串口错误：{exc}")
        except OSError as exc:
            self.error.emit(f"串口系统错误：{exc}")
        finally:
            self._close_serial()
            self.closed.emit()

    def _open_serial(self) -> serial.Serial:
        xonxoff = self._config.flow_control == "xonxoff"
        rtscts = self._config.flow_control == "rtscts"
        dsrdtr = self._config.flow_control == "dsrdtr"
        return serial.Serial(
            port=self._config.port,
            baudrate=self._config.baudrate,
            bytesize=self._config.bytesize,
            parity=self._config.parity,
            stopbits=self._config.stopbits,
            timeout=0.03,
            write_timeout=1,
            xonxoff=xonxoff,
            rtscts=rtscts,
            dsrdtr=dsrdtr,
        )

    def _read_write_loop(self) -> None:
        assert self._serial is not None
        while not self._stop_event.is_set():
            if self._reset_requested.is_set():
                self._perform_reset()
                self._reset_requested.clear()
            self._drain_send_queue()
            waiting = self._serial.in_waiting
            raw = self._serial.read(waiting or 1)
            if raw:
                self.data_received.emit(bytes(raw))

    def _perform_reset(self) -> None:
        if self._serial and self._serial.is_open:
            try:
                # 硬件复位序列：拉低 DTR (pyserial setDTR(False) 为高电平)，拉高 RTS (setRTS(True) 为低电平使 EN 拉低复位)
                # 保持 100ms 后释放 RTS (setRTS(False) 使 EN 拉高启动)
                self._serial.setDTR(False)
                self._serial.setRTS(True)
                time.sleep(0.1)
                self._serial.setRTS(False)
            except serial.SerialException as exc:
                self.error.emit(f"串口复位错误：{exc}")

    def _drain_send_queue(self) -> None:
        assert self._serial is not None
        while True:
            try:
                data = self._send_queue.get_nowait()
            except queue.Empty:
                break
            self._serial.write(data)
            self._serial.flush()

    def _close_serial(self) -> None:
        if self._serial is None:
            return
        try:
            if self._serial.is_open:
                self._serial.close()
        except serial.SerialException:
            pass
        finally:
            self._serial = None


class TerminalWidget(QWidget):
    """串口终端台。"""

    _MAX_RECORDS = 4000

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._records: list[TerminalLogRecord] = []
        self._next_record_id = 1
        self._record_hex_overrides: dict[int, bool] = {}
        self._sessions: dict[int, TerminalSession] = {}
        self._session_order: list[int] = []
        self._next_session_id = 1
        self._active_session_id: int | None = None
        self._port_descriptions: dict[str, str] = {}
        self._reconnect_timers: dict[int, QTimer] = {}
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(35)
        self._render_timer.timeout.connect(self._render_log)
        self._send_history: list[str] = []
        self._history_index: int | None = None
        self._history_draft = ""
        self._updating_log_scroll = False
        self._updating_session_controls = False
        self._terminal_inline_input = ""
        self._init_ui()
        self._apply_style()
        self._new_session()
        QTimer.singleShot(0, self.refresh_ports)

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        control_frame = QFrame()
        control_frame.setObjectName("sectionFrame")
        control_layout = QVBoxLayout(control_frame)
        control_layout.setContentsMargins(12, 10, 12, 10)
        control_layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("终端会话")
        title.setObjectName("sectionTitle")
        self._session_count_label = QLabel("0 个")
        self._session_count_label.setObjectName("terminalSessionCount")
        self._session_tabs_widget = QWidget()
        self._session_tabs_widget.setObjectName("terminalSessionTabs")
        self._session_tabs_layout = QHBoxLayout(self._session_tabs_widget)
        self._session_tabs_layout.setContentsMargins(0, 0, 0, 0)
        self._session_tabs_layout.setSpacing(4)
        self._new_session_btn = QPushButton("新建会话")
        self._new_session_btn.setObjectName("sessionActionButton")
        self._new_session_btn.clicked.connect(self._new_session)
        self._status_label = QLabel("未连接")
        self._status_label.setObjectName("terminalStatus")
        self._status_label.setProperty("state", "idle")

        self._timestamp_check = QCheckBox("收发时间")
        self._timestamp_check.toggled.connect(self._on_session_display_option_changed)
        self._hex_check = QCheckBox("HEX 显示")
        self._hex_check.toggled.connect(self._on_session_display_option_changed)
        self._auto_follow_btn = QPushButton("自动跟随")
        self._auto_follow_btn.setCheckable(True)
        self._auto_follow_btn.setChecked(True)
        self._auto_follow_btn.setObjectName("autoFollowButton")
        self._auto_follow_btn.toggled.connect(self._on_auto_follow_toggled)
        self._copy_btn = QPushButton("复制全部")
        self._copy_btn.clicked.connect(self._copy_all)
        self._clear_btn = QPushButton("清空")
        self._clear_btn.clicked.connect(self._clear_log)

        header.addWidget(title)
        header.addWidget(self._session_count_label)
        header.addWidget(self._session_tabs_widget)
        header.addWidget(self._new_session_btn)
        header.addWidget(self._status_label)
        header.addStretch(1)
        header.addWidget(self._timestamp_check)
        header.addWidget(self._hex_check)
        header.addWidget(self._auto_follow_btn)
        header.addWidget(self._copy_btn)
        header.addWidget(self._clear_btn)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self._mode_combo = QComboBox()
        self._mode_combo.addItem("标准终端", "terminal")
        self._mode_combo.addItem("文本终端", "plain")
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self._port_combo = QComboBox()
        self._port_combo.setEditable(True)
        self._port_combo.setMinimumWidth(180)
        self._port_combo.currentIndexChanged.connect(self._on_port_index_changed)
        self._port_combo.currentTextChanged.connect(self._save_active_session_properties)
        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self.refresh_ports)

        self._baud_combo = QComboBox()
        self._baud_combo.setEditable(True)
        for baud in TERMINAL_BAUD_OPTIONS:
            self._baud_combo.addItem(baud)
        self._baud_combo.setCurrentText("115200")
        self._baud_combo.currentTextChanged.connect(self._save_active_session_properties)

        self._bytesize_combo = QComboBox()
        for value in (8, 7, 6, 5):
            self._bytesize_combo.addItem(str(value), value)
        self._bytesize_combo.currentIndexChanged.connect(self._save_active_session_properties)

        self._parity_combo = QComboBox()
        self._parity_combo.addItem("无", serial.PARITY_NONE)
        self._parity_combo.addItem("偶校验", serial.PARITY_EVEN)
        self._parity_combo.addItem("奇校验", serial.PARITY_ODD)
        self._parity_combo.addItem("Mark", serial.PARITY_MARK)
        self._parity_combo.addItem("Space", serial.PARITY_SPACE)
        self._parity_combo.currentIndexChanged.connect(self._save_active_session_properties)

        self._stopbits_combo = QComboBox()
        self._stopbits_combo.addItem("1", serial.STOPBITS_ONE)
        self._stopbits_combo.addItem("1.5", serial.STOPBITS_ONE_POINT_FIVE)
        self._stopbits_combo.addItem("2", serial.STOPBITS_TWO)
        self._stopbits_combo.currentIndexChanged.connect(self._save_active_session_properties)

        self._flow_combo = QComboBox()
        self._flow_combo.addItem("无", "none")
        self._flow_combo.addItem("XON/XOFF", "xonxoff")
        self._flow_combo.addItem("RTS/CTS", "rtscts")
        self._flow_combo.addItem("DSR/DTR", "dsrdtr")
        self._flow_combo.currentIndexChanged.connect(self._save_active_session_properties)

        self._connect_btn = QPushButton("连接")
        self._connect_btn.setObjectName("primaryButton")
        self._connect_btn.clicked.connect(self._connect_serial)
        self._listen_btn = QPushButton("监听")
        self._listen_btn.setCheckable(True)
        self._listen_btn.setObjectName("listenButton")
        self._listen_btn.toggled.connect(self._on_listen_toggled)
        self._disconnect_btn = QPushButton("断开")
        self._disconnect_btn.setObjectName("dangerButton")
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.clicked.connect(self.close_serial)
        self._reset_btn = QPushButton("复位重启")
        self._reset_btn.setObjectName("warningButton")
        self._reset_btn.setToolTip("拉低 EN (RTS/DTR) 引脚以硬件复位重启设备")
        self._reset_btn.setEnabled(False)
        self._reset_btn.clicked.connect(self._trigger_reset)

        self._add_labeled_control(grid, 0, 0, "模式", self._mode_combo)
        self._add_labeled_control(grid, 0, 2, "串口", self._port_combo)
        grid.addWidget(self._refresh_btn, 0, 4)
        self._add_labeled_control(grid, 0, 5, "波特率", self._baud_combo)
        self._add_labeled_control(grid, 0, 7, "数据位", self._bytesize_combo)
        self._add_labeled_control(grid, 1, 0, "校验", self._parity_combo)
        self._add_labeled_control(grid, 1, 2, "停止位", self._stopbits_combo)
        self._add_labeled_control(grid, 1, 4, "流控", self._flow_combo)
        grid.addWidget(self._connect_btn, 1, 6)
        grid.addWidget(self._listen_btn, 1, 7)
        grid.addWidget(self._disconnect_btn, 1, 8)
        grid.addWidget(self._reset_btn, 1, 9)
        grid.setColumnStretch(10, 1)

        control_layout.addLayout(header)
        control_layout.addLayout(grid)

        self._log = QTextBrowser()
        self._log.setObjectName("terminalLog")
        self._log.setReadOnly(True)
        self._log.setOpenExternalLinks(False)
        self._log.setOpenLinks(False)
        self._log.anchorClicked.connect(self._handle_log_link)
        self._log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._log.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._log.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._log.installEventFilter(self)
        self._log.verticalScrollBar().valueChanged.connect(self._on_log_scroll_changed)
        mono = QFont()
        mono.setFamilies(["Consolas", "Menlo", "DejaVu Sans Mono", "monospace"])
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        mono.setPointSize(10)
        self._log.setFont(mono)

        input_frame = QFrame()
        input_frame.setObjectName("sectionFrame")
        input_layout = QVBoxLayout(input_frame)
        input_layout.setContentsMargins(12, 8, 12, 8)
        input_layout.setSpacing(6)

        input_header = QHBoxLayout()
        input_header.setSpacing(8)
        input_title = QLabel("发送")
        input_title.setObjectName("sectionTitle")
        self._encoding_combo = QComboBox()
        self._encoding_combo.setEditable(True)
        for encoding in ENCODING_OPTIONS:
            self._encoding_combo.addItem(encoding)
        self._encoding_combo.setCurrentText("utf-8")
        self._encoding_combo.currentTextChanged.connect(self._on_encoding_changed)

        self._newline_combo = QComboBox()
        self._newline_combo.addItem("LF", "\n")
        self._newline_combo.addItem("CRLF", "\r\n")
        self._newline_combo.addItem("CR", "\r")
        self._newline_combo.addItem("不追加", "")
        self._newline_combo.setCurrentIndex(1)
        self._newline_combo.currentIndexChanged.connect(self._save_active_session_properties)

        input_header.addWidget(input_title)
        input_header.addStretch(1)
        input_header.addWidget(self._make_label("编码"))
        input_header.addWidget(self._encoding_combo)
        input_header.addWidget(self._make_label("换行"))
        input_header.addWidget(self._newline_combo)

        self._input_stack = QStackedWidget()

        self._terminal_input = QPlainTextEdit()
        self._terminal_input.setObjectName("terminalSendEdit")
        self._terminal_input.setPlaceholderText("输入命令，按 Enter 发送")
        self._terminal_input.setMinimumHeight(94)
        self._terminal_input.setMaximumHeight(118)
        self._terminal_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._terminal_input.setViewportMargins(0, 0, 96, 40)
        self._terminal_input.setFont(mono)
        self._terminal_input.installEventFilter(self)
        self._terminal_send_btn = QPushButton("发送")
        self._terminal_send_btn.setObjectName("terminalInlineSendButton")
        self._terminal_send_btn.setFixedHeight(34)
        self._terminal_send_btn.setMinimumWidth(74)
        self._terminal_send_btn.clicked.connect(self._send_terminal_line)
        terminal_input_page = self._build_send_input_page(
            self._terminal_input,
            self._terminal_send_btn,
        )

        self._plain_input = QPlainTextEdit()
        self._plain_input.setObjectName("terminalSendEdit")
        self._plain_input.setPlaceholderText("输入要发送的文本")
        self._plain_input.setMinimumHeight(94)
        self._plain_input.setMaximumHeight(118)
        self._plain_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._plain_input.setViewportMargins(0, 0, 106, 40)
        self._plain_input.setFont(mono)
        self._plain_input.installEventFilter(self)
        self._plain_send_btn = QPushButton("发送文本")
        self._plain_send_btn.setObjectName("terminalInlineSendButton")
        self._plain_send_btn.setFixedHeight(34)
        self._plain_send_btn.setMinimumWidth(86)
        self._plain_send_btn.clicked.connect(self._send_plain_text)
        plain_input_page = self._build_send_input_page(
            self._plain_input,
            self._plain_send_btn,
        )

        self._input_stack.addWidget(terminal_input_page)
        self._input_stack.addWidget(plain_input_page)

        input_layout.addLayout(input_header)
        input_layout.addWidget(self._input_stack)

        root.addWidget(control_frame)
        root.addWidget(self._log, 1)
        root.addWidget(input_frame)

    def _build_send_input_page(self, input_widget: QWidget, send_button: QPushButton) -> QWidget:
        page = QWidget()
        page.setObjectName("terminalSendPage")
        page.setMinimumHeight(94)
        page.setMaximumHeight(118)
        layout = QGridLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(input_widget, 0, 0)
        layout.addWidget(
            send_button,
            0,
            0,
            alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
        )
        layout.setRowStretch(0, 1)
        layout.setColumnStretch(0, 1)
        return page

    def _add_labeled_control(
        self, grid: QGridLayout, row: int, col: int, label: str, widget: QWidget
    ) -> None:
        grid.addWidget(self._make_label(label), row, col)
        grid.addWidget(widget, row, col + 1)

    def _make_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("configLabel")
        return label

    def _apply_style(self) -> None:
        self.setStyleSheet(
            BASE_STYLESHEET
            + """
            QFrame#sectionFrame {
                background: #ffffff;
                border: 1px solid #e0e4ea;
                border-radius: 10px;
            }
            QLabel#sectionTitle {
                font-size: 13px;
                font-weight: 700;
            }
            QLabel#terminalSessionCount {
                background: #e8edf7;
                border: 1px solid #c5cfe8;
                border-radius: 4px;
                padding: 2px 8px;
                color: #2560e0;
                font-weight: 600;
                font-size: 12px;
            }
            QLabel#terminalSessionHint {
                color: #6b7a94;
                font-size: 12px;
            }
            QPushButton#sessionTabButton {
                background: #f8fafc;
                border: 1px solid #cbd5e1;
                border-radius: 7px;
                color: #334155;
                padding: 4px 10px;
                font-size: 12px;
                font-weight: 700;
            }
            QPushButton#sessionTabButton:hover {
                background: #eef2ff;
                border-color: #c7d2fe;
            }
            QPushButton#sessionTabButton:checked {
                background: #2560e0;
                border-color: #1a4db5;
                color: #ffffff;
            }
            QLabel#terminalStatus {
                border-radius: 999px;
                padding: 3px 10px;
                font-size: 12px;
                font-weight: 700;
                background: #eef2f7;
                border: 1px solid #d8dee9;
                color: #64748b;
            }
            QLabel#terminalStatus[state="connected"] {
                background: #ecfdf5;
                border-color: #bbf7d0;
                color: #047857;
            }
            QLabel#terminalStatus[state="error"] {
                background: #fff1f2;
                border-color: #fecdd3;
                color: #be123c;
            }
            QLabel#terminalStatus[state="listening"] {
                background: #fffbeb;
                border-color: #fde68a;
                color: #b45309;
            }
            QTextBrowser#terminalLog {
                background: #111827;
                border: 1px solid #0f172a;
                border-radius: 10px;
                color: #d1d5db;
                selection-background-color: #2560e0;
                selection-color: #ffffff;
                padding: 10px;
            }
            QLineEdit#terminalSendEdit,
            QPlainTextEdit#terminalSendEdit {
                background: #f8f9fb;
                border: 1px solid #dde1ea;
                border-radius: 7px;
                padding: 9px 10px;
                color: #1a2333;
            }
            QLineEdit#terminalSendEdit:focus,
            QPlainTextEdit#terminalSendEdit:focus {
                background: #ffffff;
                border: 1.5px solid #2560e0;
            }
            QWidget#terminalSendPage {
                background: transparent;
            }
            QPushButton#terminalInlineSendButton {
                background: #2560e0;
                border: 1px solid #1a4db5;
                border-radius: 7px;
                color: #ffffff;
                font-weight: 700;
                margin: 0 10px 10px 0;
                padding: 5px 14px;
            }
            QPushButton#terminalInlineSendButton:hover {
                background: #1a4db5;
            }
            QPushButton#terminalInlineSendButton:pressed {
                background: #153d91;
            }
            QCheckBox {
                color: #374151;
                font-weight: 600;
            }
            QPushButton#listenButton:checked {
                background: #0f766e;
                border-color: #0f766e;
                color: #ffffff;
            }
            QPushButton#listenButton:checked:hover {
                background: #115e59;
            }
            QPushButton#autoFollowButton:checked {
                background: #2560e0;
                border-color: #1a4db5;
                color: #ffffff;
            }
            QPushButton#warningButton {
                background: #fffbeb;
                border: 1px solid #d97706;
                color: #b45309;
            }
            QPushButton#warningButton:hover {
                background: #fef3c7;
            }
            QPushButton#warningButton:pressed {
                background: #fde68a;
            }
            QPushButton#warningButton:disabled {
                background: #f5f6f8;
                color: #b0b8cd;
                border-color: #e8eaef;
            }
            """
        )

    def refresh_ports(self) -> None:
        current_port = self._selected_port()
        ports: list[tuple[str, str]] = []
        self._port_descriptions.clear()
        for port_info in list_ports.comports():
            device = port_info.device
            desc = port_info.description if port_info.description and port_info.description != "n/a" else "串口设备"
            self._port_descriptions[device.lower()] = desc
            ports.append((device, desc))
        ports.sort(key=lambda item: _port_sort_key(item[0]))

        self._port_combo.blockSignals(True)
        self._port_combo.clear()
        for device, desc in ports:
            self._port_combo.addItem(f"{device} - {desc}", device)
        self._port_combo.addItem(CUSTOM_PORT_LABEL, CUSTOM_PORT_DATA)
        if current_port:
            idx = self._find_port_index(current_port)
            if idx >= 0:
                self._port_combo.setCurrentIndex(idx)
            else:
                self._port_combo.setCurrentIndex(self._port_combo.count() - 1)
                self._port_combo.setEditText(current_port)
        elif ports:
            self._port_combo.setCurrentIndex(0)
        else:
            self._port_combo.setCurrentIndex(0)
            self._port_combo.setEditText("")
        self._port_combo.blockSignals(False)
        if self._port_combo.lineEdit() is not None:
            self._port_combo.lineEdit().setPlaceholderText("选择串口或输入自定义串口")

        self._append_sys(f"已刷新串口：{len(ports)} 个")

    def _find_port_index(self, port: str) -> int:
        for idx in range(self._port_combo.count()):
            if self._port_combo.itemData(idx) == port:
                return idx
        return -1

    def _selected_port(self) -> str:
        data = self._port_combo.currentData()
        text = self._port_combo.currentText().strip()
        if data == CUSTOM_PORT_DATA:
            return "" if text == CUSTOM_PORT_LABEL else text
        item_text = self._port_combo.itemText(self._port_combo.currentIndex()).strip()
        if text and text != item_text:
            return text.split(" - ", 1)[0].strip()
        if isinstance(data, str) and data:
            return data
        if " - " in text:
            return text.split(" - ", 1)[0].strip()
        return text

    def _on_port_index_changed(self) -> None:
        if self._port_combo.currentData() == CUSTOM_PORT_DATA:
            current_text = self._port_combo.currentText().strip()
            if current_text == CUSTOM_PORT_LABEL:
                self._port_combo.setEditText("")
            if self._port_combo.lineEdit() is not None:
                self._port_combo.lineEdit().setPlaceholderText("输入串口，如 COM9")
        self._save_active_session_properties()

    def _collect_config(self, show_errors: bool = True) -> TerminalSerialConfig | None:
        port = self._selected_port()
        if not port:
            if show_errors:
                QMessageBox.warning(self, "提示", "请先选择或输入串口号。")
            return None
        try:
            baudrate = int(self._baud_combo.currentText().strip())
        except ValueError:
            if show_errors:
                QMessageBox.warning(self, "提示", "波特率必须是整数。")
            return None
        return TerminalSerialConfig(
            port=port,
            baudrate=baudrate,
            bytesize=int(self._bytesize_combo.currentData()),
            parity=str(self._parity_combo.currentData()),
            stopbits=float(self._stopbits_combo.currentData()),
            flow_control=str(self._flow_combo.currentData() or "none"),
        )

    def _connect_serial(self) -> None:
        session = self._active_session()
        if session is None or session.serial_thread is not None:
            return
        self._save_active_session_properties()
        config = self._collect_config()
        if config is None:
            return
        self._start_serial_thread(config, session_id=self._active_session_id)
        if not self._listen_btn.isChecked():
            self._listen_btn.setChecked(True)

    def _start_serial_thread(
        self,
        config: TerminalSerialConfig,
        session_id: int | None = None,
    ) -> None:
        if session_id is not None and session_id in self._sessions:
            session = self._sessions[session_id]
            if session.serial_thread is not None:
                return
            self._update_session_config(session, config)
            self._activate_session(session_id)
        else:
            session_id = self._ensure_session_for_config(config)
            session = self._sessions[session_id]
        self._timer_for_session(session_id).stop()
        session.closing_requested = False
        self._set_status("连接中", "idle")
        self._set_session_status(session_id, "连接中")
        self._append_sys(
            f"正在连接 {config.port} @ {config.baudrate}, "
            f"{config.bytesize}{config.parity}{config.stopbits:g}, 流控 {config.flow_control}",
            session_id=session_id,
        )
        thread = TerminalSerialThread(config, self)
        thread.opened.connect(lambda message, sid=session_id: self._serial_opened(sid, message))
        thread.data_received.connect(lambda data, sid=session_id: self._serial_data_received(sid, data))
        thread.error.connect(lambda message, sid=session_id: self._serial_error(sid, message))
        thread.closed.connect(lambda sid=session_id: self._serial_closed(sid))
        session.serial_thread = thread
        self._update_active_connection_controls()
        thread.start()

    def close_serial(self) -> None:
        session = self._active_session()
        if session is None:
            return
        if self._listen_btn.isChecked():
            self._listen_btn.setChecked(False)
        if session.serial_thread is None:
            self._set_session_status(session.session_id, "已断开")
            return
        session.closing_requested = True
        self._append_sys("正在断开串口", session_id=session.session_id)
        session.serial_thread.request_stop()
        self._disconnect_btn.setEnabled(False)

    def _trigger_reset(self) -> None:
        session = self._active_session()
        if session is not None and session.serial_thread is not None:
            self._append_sys("正在拉低 RTS/DTR 触发设备复位重启...", session_id=session.session_id)
            session.serial_thread.request_reset()

    def _on_listen_toggled(self, checked: bool) -> None:
        session = self._active_session()
        if session is None:
            return
        self._listen_btn.setText("监听中" if checked else "监听")
        session.listening = checked
        if not checked:
            self._timer_for_session(session.session_id).stop()
            if session.serial_thread is None and self._status_label.property("state") == "listening":
                self._set_status("未连接", "idle")
            return

        self._save_active_session_properties()
        config = session.config
        if config is None:
            config = self._collect_config(show_errors=True)
        if config is None:
            self._listen_btn.setChecked(False)
            return
        if session.serial_thread is None:
            self._set_status("监听中", "listening")
            self._try_listener_reconnect(session.session_id)
            timer = self._timer_for_session(session.session_id)
            if session.serial_thread is None and not timer.isActive():
                timer.start()

    def _try_listener_reconnect(self, session_id: int) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        timer = self._timer_for_session(session_id)
        if not session.listening:
            timer.stop()
            return
        if session.serial_thread is not None:
            timer.stop()
            return
        config = session.config
        if config is None:
            return
        if not self._port_is_available(config.port):
            self._set_session_status(session_id, "监听中")
            return
        self._append_sys(f"监听检测到 {config.port}，正在自动重连", session_id=session_id)
        self._start_serial_thread(config, session_id=session_id)

    def _port_is_available(self, port: str) -> bool:
        target = port.lower()
        for port_info in list_ports.comports():
            if str(port_info.device).lower() == target:
                return True
        return False

    def _schedule_listener_reconnect(self, session_id: int) -> None:
        session = self._sessions.get(session_id)
        if session is None or not session.listening or session.config is None:
            return
        self._set_session_status(session_id, "监听中")
        timer = self._timer_for_session(session_id)
        if not timer.isActive():
            timer.start()

    def shutdown(self) -> None:
        for timer in self._reconnect_timers.values():
            timer.stop()
        for session in self._sessions.values():
            thread = session.serial_thread
            if thread is not None:
                thread.request_stop()
                thread.wait(1200)

    def _serial_opened(self, session_id: int, message: str) -> None:
        self._set_session_status(session_id, "已连接")
        self._append_sys(message, session_id=session_id)

    def _serial_data_received(self, session_id: int, data: bytes) -> None:
        self._append_record("RX", data, session_id=session_id)

    def _serial_error(self, session_id: int, message: str) -> None:
        self._set_session_status(session_id, "错误")
        self._append_sys(message, session_id=session_id)

    def _serial_closed(self, session_id: int) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        if session.closing_requested:
            self._append_sys("串口已断开", session_id=session_id)
            self._set_session_status(session_id, "已断开")
        elif session.status != "错误":
            self._append_sys("串口连接已关闭", session_id=session_id)
            self._set_session_status(session_id, "已关闭")
        session.serial_thread = None
        session.closing_requested = False
        if session.listening:
            self._schedule_listener_reconnect(session_id)
        elif session.status != "错误":
            self._set_session_status(session_id, "未连接")
        self._update_active_connection_controls()

    def _set_connected_controls(self, connected: bool) -> None:
        for widget in (
            self._port_combo,
            self._refresh_btn,
            self._baud_combo,
            self._bytesize_combo,
            self._parity_combo,
            self._stopbits_combo,
            self._flow_combo,
        ):
            widget.setEnabled(not connected)
        self._connect_btn.setEnabled(not connected)
        self._disconnect_btn.setEnabled(connected)
        self._reset_btn.setEnabled(connected)

    def _active_session(self) -> TerminalSession | None:
        return self._sessions.get(self._active_session_id or -1)

    def _timer_for_session(self, session_id: int) -> QTimer:
        timer = self._reconnect_timers.get(session_id)
        if timer is None:
            timer = QTimer(self)
            timer.setInterval(1000)
            timer.timeout.connect(lambda sid=session_id: self._try_listener_reconnect(sid))
            self._reconnect_timers[session_id] = timer
        return timer

    def _update_active_connection_controls(self) -> None:
        session = self._active_session()
        connected = session is not None and session.serial_thread is not None
        self._set_connected_controls(connected)
        if session is not None:
            self._listen_btn.blockSignals(True)
            self._listen_btn.setChecked(session.listening)
            self._listen_btn.setText("监听中" if session.listening else "监听")
            self._listen_btn.blockSignals(False)

    def _set_status(self, text: str, state: str) -> None:
        self._status_label.setText(text)
        self._status_label.setProperty("state", state)
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)

    def _new_session(self) -> None:
        self._save_active_session_properties()
        session_id = self._next_session_id
        self._next_session_id += 1
        session = TerminalSession(
            session_id=session_id,
            port=f"会话 {session_id}",
            description="未配置串口",
            status="未连接",
        )
        self._sessions[session_id] = session
        self._session_order.append(session_id)
        self._activate_session(session_id)
        self._refresh_session_table()

    def _save_active_session_properties(self, *_args) -> None:
        if self._updating_session_controls:
            return
        session = self._sessions.get(self._active_session_id or -1)
        if session is None:
            return
        session.mode = self._current_mode()
        session.encoding = self._current_encoding()
        session.newline = self._current_newline()
        session.timestamp_enabled = self._timestamp_check.isChecked()
        session.hex_enabled = self._hex_check.isChecked()
        session.auto_follow = self._auto_follow_btn.isChecked()
        session.listening = self._listen_btn.isChecked()
        session.send_history = self._send_history
        session.inline_input = self._terminal_inline_input
        config = self._collect_config(show_errors=False)
        if config is not None:
            self._update_session_config(session, config)
        self._refresh_session_table()

    def _on_session_display_option_changed(self, *_args) -> None:
        self._save_active_session_properties()
        self._schedule_render()

    def _on_encoding_changed(self, *_args) -> None:
        self._save_active_session_properties()
        self._schedule_render()

    def _ensure_session_for_config(self, config: TerminalSerialConfig) -> int:
        active = self._sessions.get(self._active_session_id or -1)
        if active is not None:
            self._update_session_config(active, config)
            self._refresh_session_table()
            return active.session_id

        existing = self._session_for_config(config)
        if existing is not None:
            self._update_session_config(existing, config)
            self._activate_session(existing.session_id)
            self._refresh_session_table()
            return existing.session_id

        session_id = self._next_session_id
        self._next_session_id += 1
        session = TerminalSession(
            session_id=session_id,
            port=config.port,
            description=self._build_session_description(config),
            status="未连接",
            config=config,
        )
        self._sessions[session_id] = session
        self._session_order.append(session_id)
        self._activate_session(session_id)
        self._refresh_session_table()
        return session_id

    def _update_session_config(
        self, session: TerminalSession, config: TerminalSerialConfig
    ) -> None:
        session.config = config
        session.port = config.port
        session.description = self._build_session_description(config)

    def _session_for_config(self, config: TerminalSerialConfig) -> TerminalSession | None:
        target = config.port.lower()
        for session_id in self._session_order:
            session = self._sessions.get(session_id)
            if session is not None and session.port.lower() == target:
                return session
        return None

    def _build_session_description(self, config: TerminalSerialConfig) -> str:
        device_desc = self._port_descriptions.get(config.port.lower(), "自定义串口")
        flow_text = {
            "none": "无流控",
            "xonxoff": "XON/XOFF",
            "rtscts": "RTS/CTS",
            "dsrdtr": "DSR/DTR",
        }.get(config.flow_control, config.flow_control)
        return (
            f"{device_desc} · {config.baudrate} bps · "
            f"{config.bytesize}{config.parity}{config.stopbits:g} · {flow_text}"
        )

    def _activate_session(self, session_id: int) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        self._active_session_id = session_id
        self._records = session.records
        self._record_hex_overrides = session.record_hex_overrides
        self._send_history = session.send_history
        self._terminal_inline_input = session.inline_input
        self._history_index = None
        self._history_draft = ""
        self._updating_session_controls = True
        self._set_combo_data(self._mode_combo, session.mode)
        self._timestamp_check.setChecked(session.timestamp_enabled)
        self._hex_check.setChecked(session.hex_enabled)
        self._auto_follow_btn.setChecked(session.auto_follow)
        self._encoding_combo.setCurrentText(session.encoding)
        self._set_combo_data(self._newline_combo, session.newline)
        if session.config is not None:
            self._select_port_text(session.config.port)
            self._baud_combo.setCurrentText(str(session.config.baudrate))
            self._set_combo_data(self._bytesize_combo, session.config.bytesize)
            self._set_combo_data(self._parity_combo, session.config.parity)
            self._set_combo_data(self._stopbits_combo, session.config.stopbits)
            self._set_combo_data(self._flow_combo, session.config.flow_control)
        else:
            custom_idx = self._find_custom_port_index()
            if custom_idx >= 0:
                self._port_combo.setCurrentIndex(custom_idx)
                self._port_combo.setEditText("")
            self._baud_combo.setCurrentText("115200")
            self._set_combo_data(self._bytesize_combo, 8)
            self._set_combo_data(self._parity_combo, serial.PARITY_NONE)
            self._set_combo_data(self._stopbits_combo, serial.STOPBITS_ONE)
            self._set_combo_data(self._flow_combo, "none")
        self._updating_session_controls = False
        self._input_stack.setCurrentIndex(0 if session.mode == "terminal" else 1)
        self._set_status(session.status, self._state_for_session_status(session.status))
        self._update_active_connection_controls()
        self._refresh_session_table()
        self._render_log()

    def _set_combo_data(self, combo: QComboBox, value: object) -> None:
        for idx in range(combo.count()):
            if combo.itemData(idx) == value:
                combo.setCurrentIndex(idx)
                return

    def _select_port_text(self, port: str) -> None:
        idx = self._find_port_index(port)
        if idx >= 0:
            self._port_combo.setCurrentIndex(idx)
            return
        custom_idx = self._find_custom_port_index()
        if custom_idx >= 0:
            self._port_combo.setCurrentIndex(custom_idx)
            self._port_combo.setEditText(port)

    def _find_custom_port_index(self) -> int:
        for idx in range(self._port_combo.count()):
            if self._port_combo.itemData(idx) == CUSTOM_PORT_DATA:
                return idx
        return -1

    def _set_session_status(self, session_id: int, status: str) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.status = status
        if session_id == self._active_session_id:
            self._set_status(status, self._state_for_session_status(status))
        self._refresh_session_table()

    def _state_for_session_status(self, status: str) -> str:
        if "错误" in status:
            return "error"
        if "监听" in status:
            return "listening"
        if "连接" in status and "断开" not in status:
            return "connected"
        return "idle"

    def _refresh_session_table(self) -> None:
        if not hasattr(self, "_session_tabs_layout"):
            return
        while self._session_tabs_layout.count():
            item = self._session_tabs_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for session_id in self._session_order:
            session = self._sessions[session_id]
            btn = QPushButton(self._session_label(session))
            btn.setObjectName("sessionTabButton")
            btn.setCheckable(True)
            btn.setChecked(session_id == self._active_session_id)
            btn.setToolTip(f"{session.description}\n状态：{session.status}")
            btn.clicked.connect(lambda _checked=False, sid=session_id: self._switch_session(sid))
            self._session_tabs_layout.addWidget(btn)
        self._session_tabs_layout.addStretch(1)
        self._session_count_label.setText(f"{len(self._session_order)} 个")

    def _session_label(self, session: TerminalSession) -> str:
        if session.config is not None:
            return session.config.port
        return session.port

    def _switch_session(self, session_id: int) -> None:
        self._save_active_session_properties()
        self._activate_session(session_id)
        self._terminal_input.setFocus()

    def _on_mode_changed(self) -> None:
        mode = self._current_mode()
        self._input_stack.setCurrentIndex(0 if mode == "terminal" else 1)
        self._save_active_session_properties()
        self._schedule_render()

    def _current_mode(self) -> str:
        return str(self._mode_combo.currentData() or "terminal")

    def _current_encoding(self) -> str:
        return self._encoding_combo.currentText().strip() or "utf-8"

    def _current_newline(self) -> str:
        value = self._newline_combo.currentData()
        return value if isinstance(value, str) else "\n"

    def _send_terminal_line(self) -> None:
        text = self._terminal_input.toPlainText()
        if self._send_text(text + self._current_newline()):
            self._remember_send_history(text)
            self._terminal_input.clear()

    def _send_plain_text(self) -> None:
        text = self._plain_input.toPlainText()
        if self._send_text(text + self._current_newline()):
            self._remember_send_history(text)

    def _send_text(self, text: str) -> bool:
        session = self._active_session()
        if session is None or session.serial_thread is None:
            self._append_sys("发送失败：串口未连接")
            return False
        encoding = self._current_encoding()
        try:
            data = text.encode(encoding)
        except LookupError:
            self._append_sys(f"发送失败：未知编码 {encoding}")
            return False
        except UnicodeEncodeError as exc:
            self._append_sys(f"发送失败：文本无法用 {encoding} 编码：{exc}")
            return False
        session.serial_thread.send(data)
        self._append_record("TX", data)
        return True

    def _remember_send_history(self, text: str) -> None:
        if not text:
            return
        if not self._send_history or self._send_history[-1] != text:
            self._send_history.append(text)
        if len(self._send_history) > 100:
            self._send_history = self._send_history[-100:]
        session = self._sessions.get(self._active_session_id or -1)
        if session is not None:
            session.send_history = self._send_history
        self._history_index = None
        self._history_draft = ""

    def eventFilter(self, source, event) -> bool:
        if source is self._log and event.type() == QEvent.Type.KeyPress:
            return self._handle_terminal_log_key(event)

        if event.type() == QEvent.Type.KeyPress and source in (
            self._terminal_input,
            self._plain_input,
        ):
            key = event.key()
            if source is self._terminal_input and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._send_terminal_line()
                return True
            if key == Qt.Key.Key_Up and self._can_use_history(source, previous=True):
                return self._navigate_send_history(source, -1)
            if key == Qt.Key.Key_Down and self._can_use_history(source, previous=False):
                return self._navigate_send_history(source, 1)
        return super().eventFilter(source, event)

    def _handle_terminal_log_key(self, event) -> bool:
        if self._current_mode() != "terminal":
            return False
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            return False

        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            text = self._terminal_inline_input
            if self._send_text(text + self._current_newline()):
                self._remember_send_history(text)
                self._terminal_inline_input = ""
                self._save_active_session_properties()
                self._schedule_render()
            return True
        if key == Qt.Key.Key_Backspace:
            self._terminal_inline_input = self._terminal_inline_input[:-1]
            self._save_active_session_properties()
            self._schedule_render()
            return True
        if key == Qt.Key.Key_Escape:
            self._terminal_inline_input = ""
            self._save_active_session_properties()
            self._schedule_render()
            return True
        if key == Qt.Key.Key_Up and self._can_use_history(self._log, previous=True):
            return self._navigate_send_history(self._log, -1)
        if key == Qt.Key.Key_Down and self._can_use_history(self._log, previous=False):
            return self._navigate_send_history(self._log, 1)

        text = event.text()
        if text and text >= " ":
            self._terminal_inline_input += text
            self._save_active_session_properties()
            self._schedule_render()
            return True
        return False

    def _can_use_history(self, source: QWidget, previous: bool) -> bool:
        if source is self._terminal_input:
            return True
        if source is self._log:
            return self._current_mode() == "terminal"
        if source is self._plain_input:
            cursor = self._plain_input.textCursor()
            block = cursor.blockNumber()
            if previous:
                return block == 0
            return block == self._plain_input.document().blockCount() - 1
        return False

    def _navigate_send_history(self, source: QWidget, step: int) -> bool:
        if not self._send_history:
            return False
        if self._history_index is None:
            self._history_draft = self._input_text(source)
            self._history_index = len(self._send_history) - 1 if step < 0 else None
        elif step < 0:
            self._history_index = max(0, self._history_index - 1)
        else:
            if self._history_index >= len(self._send_history) - 1:
                self._history_index = None
                self._set_input_text(source, self._history_draft)
                return True
            self._history_index += 1

        if self._history_index is not None:
            self._set_input_text(source, self._send_history[self._history_index])
            return True
        return False

    def _input_text(self, source: QWidget) -> str:
        if source is self._terminal_input:
            return self._terminal_input.toPlainText()
        if source is self._log:
            return self._terminal_inline_input
        if source is self._plain_input:
            return self._plain_input.toPlainText()
        return ""

    def _set_input_text(self, source: QWidget, text: str) -> None:
        if source is self._terminal_input:
            self._terminal_input.setPlainText(text)
            cursor = self._terminal_input.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self._terminal_input.setTextCursor(cursor)
            return
        if source is self._log:
            self._terminal_inline_input = text
            self._save_active_session_properties()
            self._schedule_render()
            return
        if source is self._plain_input:
            self._plain_input.setPlainText(text)
            cursor = self._plain_input.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self._plain_input.setTextCursor(cursor)

    def _append_sys(self, message: str, session_id: int | None = None) -> None:
        self._append_record("SYS", b"", message, session_id=session_id)

    def _append_record(
        self,
        direction: str,
        payload: bytes,
        message: str = "",
        session_id: int | None = None,
    ) -> None:
        records, overrides = self._records_for_session(session_id)
        records.append(
            TerminalLogRecord(
                record_id=self._next_record_id,
                direction=direction,
                timestamp=datetime.now(),
                payload=payload,
                message=message,
            )
        )
        self._next_record_id += 1
        if len(records) > self._MAX_RECORDS:
            removed = records[: len(records) - self._MAX_RECORDS]
            del records[: len(records) - self._MAX_RECORDS]
            for record in removed:
                overrides.pop(record.record_id, None)
        if session_id is None or session_id == self._active_session_id:
            self._schedule_render()

    def _records_for_session(
        self, session_id: int | None
    ) -> tuple[list[TerminalLogRecord], dict[int, bool]]:
        if session_id is None:
            return self._records, self._record_hex_overrides
        session = self._sessions.get(session_id)
        if session is None:
            return self._records, self._record_hex_overrides
        return session.records, session.record_hex_overrides

    def _schedule_render(self) -> None:
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _render_log(self) -> None:
        text, is_html = self._build_display_text()
        scrollbar = self._log.verticalScrollBar()
        previous_value = scrollbar.value()
        previous_max = scrollbar.maximum()
        should_follow = self._auto_follow_btn.isChecked() or self._is_log_at_bottom()
        self._updating_log_scroll = True
        if is_html:
            self._log.setHtml(text)
        else:
            self._log.setPlainText(text)
        if should_follow:
            self._scroll_log_to_bottom()
        else:
            self._set_log_scroll_value(min(previous_value, scrollbar.maximum()))
        self._updating_log_scroll = False

        # QTextBrowser may finish layout after setHtml returns. Apply the same
        # scroll rule once more after the event loop so manual scroll position
        # does not snap to the top while new serial data is arriving.
        if should_follow:
            QTimer.singleShot(0, self._scroll_log_to_bottom)
        else:
            QTimer.singleShot(0, lambda value=previous_value, max_value=previous_max: self._restore_log_scroll(value, max_value))

    def _restore_log_scroll(self, previous_value: int, _previous_max: int) -> None:
        if self._auto_follow_btn.isChecked():
            self._scroll_log_to_bottom()
            return
        self._set_log_scroll_value(min(previous_value, self._log.verticalScrollBar().maximum()))

    def _scroll_log_to_bottom(self) -> None:
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._log.setTextCursor(cursor)
        self._set_log_scroll_value(self._log.verticalScrollBar().maximum())
        self._log.ensureCursorVisible()

    def _set_log_scroll_value(self, value: int) -> None:
        scrollbar = self._log.verticalScrollBar()
        self._updating_log_scroll = True
        scrollbar.setValue(value)
        self._updating_log_scroll = False

    def _is_log_at_bottom(self) -> bool:
        scrollbar = self._log.verticalScrollBar()
        return scrollbar.value() >= scrollbar.maximum() - 4

    def _on_log_scroll_changed(self, value: int) -> None:
        if self._updating_log_scroll:
            return
        scrollbar = self._log.verticalScrollBar()
        at_bottom = value >= scrollbar.maximum() - 4
        if at_bottom:
            if not self._auto_follow_btn.isChecked():
                self._auto_follow_btn.blockSignals(True)
                self._auto_follow_btn.setChecked(True)
                self._auto_follow_btn.blockSignals(False)
            return
        if self._auto_follow_btn.isChecked():
            self._auto_follow_btn.blockSignals(True)
            self._auto_follow_btn.setChecked(False)
            self._auto_follow_btn.blockSignals(False)

    def _on_auto_follow_toggled(self, checked: bool) -> None:
        self._save_active_session_properties()
        if checked:
            QTimer.singleShot(0, self._scroll_log_to_bottom)

    def _build_display_text(self) -> tuple[str, bool]:
        mode = self._current_mode()
        timestamp_enabled = self._timestamp_check.isChecked()
        hex_enabled = self._hex_check.isChecked()
        encoding = self._current_encoding()
        terminal_simple = mode == "terminal" and not timestamp_enabled and not hex_enabled

        if terminal_simple:
            parts: list[str] = []
            for record in self._records:
                if record.direction == "RX":
                    parts.append(self._decode_payload(record.payload, encoding))
                elif record.direction == "SYS":
                    parts.append(f"\n[SYS] {record.message}\n")
            if self._terminal_inline_input:
                parts.append(self._terminal_inline_input)
            return self._build_html_page(self._colorize_html_text("".join(parts))), True

        if timestamp_enabled:
            lines: list[str] = []
            for record in self._records:
                formatted = self._format_record_html(record, encoding, hex_enabled)
                if formatted:
                    lines.append(formatted)
            if mode == "terminal" and self._terminal_inline_input:
                label = self._format_html_label("[INPUT]", "#93c5fd")
                lines.append(f"{label} {self._colorize_html_text(self._terminal_inline_input)}")
            return self._build_html_page("\n".join(lines)), True

        lines: list[str] = []
        for record in self._records:
            formatted = self._format_record_plain(record, encoding, hex_enabled)
            if formatted:
                lines.append(formatted)
        if mode == "terminal" and self._terminal_inline_input:
            lines.append(f"[INPUT] {self._terminal_inline_input}")
        return self._build_html_page(self._colorize_html_text("\n".join(lines))), True

    def _format_record_plain(
        self,
        record: TerminalLogRecord,
        encoding: str,
        hex_enabled: bool,
    ) -> str:
        if record.direction == "SYS":
            return f"[SYS] {record.message}"

        if hex_enabled:
            body = _format_hex(record.payload)
        else:
            body = self._decode_payload(record.payload, encoding)
            body = self._normalize_display_text(body)

        return f"[{record.direction}] {body}"

    def _format_record_html(
        self,
        record: TerminalLogRecord,
        encoding: str,
        global_hex_enabled: bool,
    ) -> str:
        time_prefix = record.timestamp.strftime("%H:%M:%S.%f")[:-3]
        if record.direction == "SYS":
            label = self._format_html_label(f"[{time_prefix} SYS]", "#9ca3af")
            return f"{label} {self._colorize_html_text(record.message)}"

        hex_enabled = self._hex_for_record(record, global_hex_enabled)
        if hex_enabled:
            body = _format_hex(record.payload)
            toggle_text = "文本"
        else:
            body = self._normalize_display_text(
                self._decode_payload(record.payload, encoding)
            )
            toggle_text = "HEX"
        label = f"[{time_prefix} {record.direction} {len(record.payload)}B]"
        label_html = self._format_html_label(label, self._direction_color(record.direction))
        link = (
            f"<a style=\"color:#93c5fd;text-decoration:none;\" "
            f"href=\"toggle:{record.record_id}\">{toggle_text}</a>"
        )
        return f"{label_html} {link} {self._colorize_html_text(body)}"

    def _build_html_page(self, body: str) -> str:
        return (
            "<html><body>"
            "<pre style=\"font-family: Consolas, Menlo, monospace; "
            "font-size: 10pt; color: #d1d5db; white-space: pre;\">"
            f"{body}"
            "</pre></body></html>"
        )

    def _format_html_label(self, label: str, color: str) -> str:
        return f"<span style=\"color:{color};font-weight:700;\">{escape(label)}</span>"

    def _direction_color(self, direction: str) -> str:
        if direction == "RX":
            return "#67e8f9"
        if direction == "TX":
            return "#86efac"
        return "#9ca3af"

    def _colorize_html_text(self, text: str) -> str:
        html = escape(text)
        rules = [
            (
                r"\b(error|fail(?:ed)?|fatal|exception|panic|traceback|denied|invalid)\b|错误|失败|异常",
                "#fca5a5",
                "700",
            ),
            (
                r"\b(warn(?:ing)?|timeout|retry|busy)\b|警告|超时|重试",
                "#fcd34d",
                "700",
            ),
            (
                r"\b(ok|ready|success|succeeded|pass(?:ed)?|done|connected)\b|成功|通过|完成|就绪|已连接",
                "#86efac",
                "700",
            ),
            (
                r"\b(boot|reset|reboot|rst|start(?:ed)?|waiting|listen(?:ing)?)\b|启动|复位|监听|等待",
                "#93c5fd",
                "600",
            ),
            (
                r"\b0x[0-9a-fA-F]+\b",
                "#c4b5fd",
                "600",
            ),
        ]
        for pattern, color, weight in rules:
            html = re.sub(
                pattern,
                lambda match, c=color, w=weight: (
                    f"<span style=\"color:{c};font-weight:{w};\">"
                    f"{match.group(0)}</span>"
                ),
                html,
                flags=re.IGNORECASE,
            )
        return html

    def _hex_for_record(self, record: TerminalLogRecord, global_hex_enabled: bool) -> bool:
        return self._record_hex_overrides.get(record.record_id, global_hex_enabled)

    def _handle_log_link(self, url: QUrl) -> None:
        raw = url.toString()
        if not raw.startswith("toggle:"):
            return
        try:
            record_id = int(raw.split(":", 1)[1])
        except ValueError:
            return
        record = next((item for item in self._records if item.record_id == record_id), None)
        if record is None or record.direction == "SYS":
            return
        current = self._hex_for_record(record, self._hex_check.isChecked())
        self._record_hex_overrides[record_id] = not current
        self._schedule_render()

    def _decode_payload(self, payload: bytes, encoding: str) -> str:
        try:
            return payload.decode(encoding, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")

    def _normalize_display_text(self, text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")

    def _copy_all(self) -> None:
        QApplication.clipboard().setText(self._log.toPlainText())

    def _clear_log(self) -> None:
        self._records.clear()
        self._record_hex_overrides.clear()
        self._log.clear()

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)
