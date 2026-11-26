from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

import serial
from PyQt6.QtCore import QEvent, QObject, QThread, QThreadPool, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetricsF, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
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

from .flow_layout import FlowLayout
from .port_ownership import GLOBAL_PORT_OWNERSHIP, PortLease, PortOwnershipRegistry
from .styles import BASE_STYLESHEET
from .terminal_decode import decode_payload_chunks
from .unix_terminal import UnixTerminalEmulator, encode_unix_key, normalize_pasted_text


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

_SHELL_TERM_TYPES = ("xterm-256color", "xterm", "vt100", "vt102", "linux")
# Run the probe in a subshell so its helper and variables stay out of the
# operator's shell. Keep the command ASCII for devices using legacy locales.
_SHELL_TERM_PROBE = (
    '(echo "TERM=$TERM"; '
    'if command -v infocmp >/dev/null; then check() { infocmp "$1"; }; '
    'elif command -v tput >/dev/null; then check() { tput -T "$1" cup 0 0; }; '
    'else echo "infocmp / tput unavailable"; exit 1; fi; '
    f'for t in {" ".join(_SHELL_TERM_TYPES)}; do '
    'if check "$t" >/dev/null 2>&1; then echo "+ $t"; else echo "- $t"; fi; done)'
)


@dataclass(frozen=True)
class TerminalSerialConfig:
    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    flow_control: str

    def serial_settings(self) -> dict:
        return dict(
            baudrate=self.baudrate, bytesize=self.bytesize, parity=self.parity,
            stopbits=self.stopbits, xonxoff=self.flow_control == "xonxoff",
            rtscts=self.flow_control == "rtscts", dsrdtr=self.flow_control == "dsrdtr",
        )


@dataclass
class TerminalLogRecord:
    record_id: int
    direction: str
    timestamp: datetime
    payload: bytes = b""
    message: str = ""
    clears_unix_display: bool = False


@dataclass
class TerminalSession:
    session_id: int
    port: str
    description: str
    status: str = "未连接"
    config: TerminalSerialConfig | None = None
    pending_config: TerminalSerialConfig | None = None
    mode: str = "terminal"
    encoding: str = "utf-8"
    last_sent_term: str = ""
    newline_by_mode: dict[str, str] = field(
        default_factory=lambda: {"terminal": "\r\n", "plain": "\r\n", "unix": "auto"}
    )
    timestamp_enabled: bool = False
    hex_enabled: bool = False
    auto_follow: bool = True
    unix_terminal: UnixTerminalEmulator = field(default_factory=UnixTerminalEmulator)
    records: list[TerminalLogRecord] = field(default_factory=list)
    record_hex_overrides: dict[int, bool] = field(default_factory=dict)
    send_history: list[str] = field(default_factory=list)
    serial_thread: "TerminalSerialThread | None" = None
    port_lease: PortLease | None = None
    listening: bool = False
    closing_requested: bool = False


def _port_sort_key(port: str) -> tuple[int, str]:
    match = re.search(r"(\d+)$", port)
    if match:
        return (int(match.group(1)), port)
    return (10**9, port)


def _format_hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


class _TerminalPortScan(QObject):
    finished = pyqtSignal(object, str)

    def run(self) -> None:
        try:
            ports = list(list_ports.comports())
        except Exception as exc:
            self.finished.emit([], str(exc))
        else:
            self.finished.emit(ports, "")


class _TerminalPortCombo(QComboBox):
    opening = pyqtSignal()

    def showPopup(self) -> None:
        self.opening.emit()
        super().showPopup()


class TerminalSerialThread(QThread):
    opened = pyqtSignal(str)
    data_received = pyqtSignal(bytes)
    error = pyqtSignal(str)
    closed = pyqtSignal()
    settings_applied = pyqtSignal(object, object, str)

    def __init__(self, config: TerminalSerialConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._stop_event = threading.Event()
        self._reset_requested = threading.Event()
        self._send_queue: queue.Queue[bytes | TerminalSerialConfig] = queue.Queue()
        self._serial: serial.Serial | None = None

    def request_stop(self) -> None:
        self._stop_event.set()

    def request_reset(self) -> None:
        self._reset_requested.set()

    def send(self, data: bytes) -> None:
        if data:
            self._send_queue.put(data)

    def request_settings(self, config: TerminalSerialConfig) -> None:
        self._send_queue.put(config)

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
        return serial.Serial(
            port=self._config.port,
            **self._config.serial_settings(),
            timeout=0.03,
            write_timeout=1,
        )

    def _apply_settings(self, requested: TerminalSerialConfig) -> None:
        assert self._serial is not None
        try:
            self._serial.apply_settings(requested.serial_settings())
        except (OSError, ValueError, OverflowError) as exc:
            # pySerial may have applied earlier fields before a driver rejects one.
            try:
                self._serial.apply_settings(self._config.serial_settings())
            except (OSError, ValueError, OverflowError) as rollback_error:
                raise serial.SerialException(f"参数更改失败且无法恢复：{rollback_error}") from exc
            self.settings_applied.emit(requested, self._config, str(exc))
        else:
            self._config = requested
            self.settings_applied.emit(requested, requested, "")

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
        # Bound each pass so continuous TX cannot starve RX or disconnect.
        for _ in range(64):
            if self._stop_event.is_set():
                break
            try:
                data = self._send_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(data, TerminalSerialConfig):
                self._apply_settings(data)
                continue
            written = 0
            while written < len(data) and not self._stop_event.is_set():
                count = self._serial.write(data[written:])
                if count is None or count <= 0:
                    raise serial.SerialTimeoutException("串口写入未取得进展")
                written += count
            if self._stop_event.is_set():
                break
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

    compactModeChanged = pyqtSignal(bool)

    _MAX_RECORDS = 4000

    def __init__(
        self,
        parent: QWidget | None = None,
        port_registry: PortOwnershipRegistry | None = None,
    ) -> None:
        super().__init__(parent)
        self._port_registry = port_registry or GLOBAL_PORT_OWNERSHIP
        self._records: list[TerminalLogRecord] = []
        self._next_record_id = 1
        self._record_hex_overrides: dict[int, bool] = {}
        self._sessions: dict[int, TerminalSession] = {}
        self._session_order: list[int] = []
        self._next_session_id = 1
        self._active_session_id: int | None = None
        self._port_descriptions: dict[str, str] = {}
        self._port_snapshot: list[tuple[str, str]] | None = None
        self._latest_ports: list[tuple[str, str]] = []
        self._enumerated_ports: set[str] = set()
        self._port_scan = _TerminalPortScan()
        self._port_scan.finished.connect(self._port_scan_finished)
        self._scan_running = False
        self._announce_scan = False
        self._port_timer = QTimer(self)
        self._port_timer.setInterval(5000)
        self._port_timer.timeout.connect(self._request_port_scan)
        self._reconnect_timers: dict[int, QTimer] = {}
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(35)
        self._render_timer.timeout.connect(self._render_log)
        self._send_history: list[str] = []
        self._history_index: int | None = None
        self._history_draft = ""
        self._updating_log_scroll = False
        self._unix_html_lines: list[str] | None = None
        self._updating_session_controls = False
        self._fullscreen_restore_geometry = None
        self._fullscreen_restore_state = None
        self._init_ui()
        self._apply_style()
        self._new_session()
        self._port_timer.start()
        QTimer.singleShot(0, self._request_port_scan)

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

        self._serial_config_widget = QWidget()
        config_rows = QVBoxLayout(self._serial_config_widget)
        config_rows.setContentsMargins(0, 0, 0, 0)
        config_rows.setSpacing(6)
        port_row = FlowLayout(h_spacing=12, v_spacing=6)
        settings_row = FlowLayout(h_spacing=12, v_spacing=6)
        for row in (port_row, settings_row):
            row.setContentsMargins(0, 0, 0, 0)
            config_rows.addLayout(row)

        self._mode_combo = QComboBox()
        self._mode_combo.addItem("标准终端", "terminal")
        self._mode_combo.addItem("Unix 终端", "unix")
        self._mode_combo.addItem("文本终端", "plain")
        self._mode_combo.setItemData(
            self._mode_combo.findData("unix"),
            "ANSI/VT100 屏幕仿真；直接输入并支持终端控制键",
            Qt.ItemDataRole.ToolTipRole,
        )
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self._port_combo = _TerminalPortCombo()
        self._port_combo.opening.connect(self._apply_port_list)
        self._port_combo.setEditable(True)
        self._port_combo.setMinimumWidth(180)
        self._port_combo.setMaximumWidth(360)
        self._port_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._port_combo.addItem(CUSTOM_PORT_LABEL, CUSTOM_PORT_DATA)
        self._port_combo.setEditText("")
        self._port_combo.currentIndexChanged.connect(self._on_port_index_changed)
        self._port_combo.currentTextChanged.connect(self._port_combo.setToolTip)
        self._port_combo.currentTextChanged.connect(self._save_active_session_properties)
        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self.refresh_ports)

        self._baud_combo = QComboBox()
        self._baud_combo.setEditable(True)
        for baud in TERMINAL_BAUD_OPTIONS:
            self._baud_combo.addItem(baud)
        self._baud_combo.setCurrentText("115200")
        self._baud_combo.activated.connect(self._commit_serial_settings)
        self._baud_combo.lineEdit().editingFinished.connect(self._commit_serial_settings)

        self._bytesize_combo = QComboBox()
        for value in (8, 7, 6, 5):
            self._bytesize_combo.addItem(str(value), value)
        self._bytesize_combo.currentIndexChanged.connect(self._commit_serial_settings)

        self._parity_combo = QComboBox()
        self._parity_combo.addItem("无", serial.PARITY_NONE)
        self._parity_combo.addItem("偶校验", serial.PARITY_EVEN)
        self._parity_combo.addItem("奇校验", serial.PARITY_ODD)
        self._parity_combo.addItem("Mark", serial.PARITY_MARK)
        self._parity_combo.addItem("Space", serial.PARITY_SPACE)
        self._parity_combo.currentIndexChanged.connect(self._commit_serial_settings)

        self._stopbits_combo = QComboBox()
        self._stopbits_combo.addItem("1", serial.STOPBITS_ONE)
        self._stopbits_combo.addItem("1.5", serial.STOPBITS_ONE_POINT_FIVE)
        self._stopbits_combo.addItem("2", serial.STOPBITS_TWO)
        self._stopbits_combo.currentIndexChanged.connect(self._commit_serial_settings)

        self._flow_combo = QComboBox()
        self._flow_combo.addItem("无", "none")
        self._flow_combo.addItem("XON/XOFF", "xonxoff")
        self._flow_combo.addItem("RTS/CTS", "rtscts")
        self._flow_combo.addItem("DSR/DTR", "dsrdtr")
        self._flow_combo.currentIndexChanged.connect(self._commit_serial_settings)

        self._term_type_combo = QComboBox()
        self._term_type_combo.addItems(_SHELL_TERM_TYPES)
        self._term_type_combo.setPlaceholderText("选择 TERM")
        self._term_type_combo.setCurrentIndex(-1)
        self._term_type_combo.setToolTip(
            "在已连接设备的空白 Shell 提示符下选择，将发送 export TERM 命令。\n"
            "现代彩色终端常用 xterm-256color，精简串口设备可先试 vt100。\n"
            "显示本次连接最后发送的类型。右键终端可检测设备支持的类型。"
        )
        self._term_type_combo.textActivated.connect(self._send_shell_term)

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

        for row, label, editor, *buttons in (
            (port_row, "模式", self._mode_combo),
            (port_row, "串口", self._port_combo, self._refresh_btn),
            (port_row, "波特率", self._baud_combo),
            (settings_row, "数据位", self._bytesize_combo),
            (settings_row, "校验", self._parity_combo),
            (settings_row, "停止位", self._stopbits_combo),
            (settings_row, "流控", self._flow_combo),
            (settings_row, "终端类型", self._term_type_combo),
        ):
            caption = self._make_label(label)
            caption.setBuddy(editor)
            editor.setAccessibleName(label)
            row.addWidget(self._config_group(caption, editor, *buttons))

        self._compact_btn = QPushButton("精简")
        self._compact_btn.setObjectName("terminalViewToggle")
        self._compact_btn.setCheckable(True)
        self._compact_btn.setFixedWidth(90)
        self._compact_btn.setToolTip("隐藏其他工作台和应用信息，仅保留串口终端")
        self._compact_btn.toggled.connect(self._on_compact_toggled)
        self._fullscreen_btn = QPushButton("全屏")
        self._fullscreen_btn.setObjectName("terminalViewToggle")
        self._fullscreen_btn.setCheckable(True)
        self._fullscreen_btn.setFixedWidth(90)
        self._fullscreen_btn.setToolTip("切换应用全屏显示；可与精简模式同时启用")
        self._fullscreen_btn.toggled.connect(self._on_fullscreen_toggled)

        actions = self._config_group(
            self._connect_btn, self._listen_btn, self._disconnect_btn,
            self._reset_btn, self._compact_btn, self._fullscreen_btn,
        )
        settings_row.addWidget(actions)
        settings_row.setAlignment(actions, Qt.AlignmentFlag.AlignRight)

        control_layout.addLayout(header)
        control_layout.addWidget(self._serial_config_widget)

        self._log = QTextBrowser()
        self._log.setObjectName("terminalLog")
        self._log.setReadOnly(True)
        self._log.setOpenExternalLinks(False)
        self._log.setOpenLinks(False)
        self._log.anchorClicked.connect(self._handle_log_link)
        self._log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._log.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._log.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._log.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self._log.installEventFilter(self)
        self._log.viewport().installEventFilter(self)
        self._log.verticalScrollBar().valueChanged.connect(self._on_log_scroll_changed)
        mono = QFont()
        mono.setFamilies(["Consolas", "Menlo", "DejaVu Sans Mono", "monospace"])
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        mono.setPointSize(10)
        self._log.setFont(mono)

        self._input_frame = QFrame()
        self._input_frame.setObjectName("sectionFrame")
        input_layout = QVBoxLayout(self._input_frame)
        input_layout.setContentsMargins(12, 8, 12, 8)
        input_layout.setSpacing(6)

        input_header = QHBoxLayout()
        input_header.setSpacing(8)
        self._input_title = QLabel("发送")
        self._input_title.setObjectName("sectionTitle")
        self._unix_input_hint = QLabel("直接输入 · 右键检测 TERM")
        self._unix_input_hint.setToolTip("在设备的 Shell 提示符下，右键终端可检查或设置当前 Shell 的 TERM。")
        self._unix_input_hint.setObjectName("terminalSessionHint")
        self._unix_input_hint.hide()
        self._unix_quick_buttons: dict[str, QPushButton] = {}
        for label, payload, tooltip in (
            ("Ctrl+V", None, "粘贴剪贴板内容"),
            ("Tab", b"\t", "命令或路径补全"),
            ("↑", b"\x1b[A", "上一条历史命令 / 向上移动"),
            ("↓", b"\x1b[B", "下一条历史命令 / 向下移动"),
            ("Ctrl+L", b"\x0c", "清除当前 Unix 屏幕和滚动历史，并请求设备重绘"),
            ("Esc", b"\x1b", "退出当前菜单或操作"),
        ):
            button = QPushButton(label)
            button.setObjectName("terminalQuickKeyButton")
            button.setToolTip(tooltip)
            button.setFixedHeight(30)
            button.setMinimumWidth(36 if label in {"↑", "↓"} else 48)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if payload is None:
                button.clicked.connect(self._paste_unix_terminal)
            else:
                button.clicked.connect(
                    lambda _checked=False, data=payload: self._send_unix_key_bytes(data)
                )
            button.hide()
            self._unix_quick_buttons[label] = button
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
        self._newline_combo.addItem("自动", "auto")
        self._newline_combo.setCurrentIndex(1)
        self._newline_combo.setToolTip(
            "自动：Unix 终端的 Enter 和粘贴换行使用单个 CR；其他模式使用 CRLF。\n"
            "各会话分别记住每种模式的手动选择。"
        )
        self._newline_combo.currentIndexChanged.connect(self._save_active_session_properties)

        input_header.addWidget(self._input_title)
        input_header.addWidget(self._unix_input_hint)
        for button in self._unix_quick_buttons.values():
            input_header.addWidget(button)
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
        for view in (self._log, self._terminal_input, self._plain_input):
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(
                lambda point, view=view: self._show_terminal_menu(view, point)
            )

        input_layout.addLayout(input_header)
        input_layout.addWidget(self._input_stack)

        root.addWidget(control_frame)
        root.addWidget(self._log, 1)
        root.addWidget(self._input_frame)

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

    def _config_group(self, *widgets: QWidget) -> QWidget:
        group = QWidget()
        row = QHBoxLayout(group)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        for widget in widgets:
            row.addWidget(widget)
        return group

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
            QPushButton#terminalQuickKeyButton {
                background: #eef2ff;
                border: 1px solid #c7d2fe;
                border-radius: 6px;
                color: #334155;
                font-weight: 700;
                padding: 3px 8px;
            }
            QPushButton#terminalQuickKeyButton:hover {
                background: #dbeafe;
                border-color: #93c5fd;
                color: #1d4ed8;
            }
            QPushButton#terminalQuickKeyButton:pressed {
                background: #bfdbfe;
            }
            QPushButton#terminalViewToggle {
                background: #f8fafc;
                border: 1px solid #cbd5e1;
                border-radius: 7px;
                color: #334155;
                font-weight: 700;
                padding: 5px 14px;
            }
            QPushButton#terminalViewToggle:hover {
                background: #eef2ff;
                border-color: #a5b4fc;
            }
            QPushButton#terminalViewToggle:checked {
                background: #2560e0;
                border-color: #1a4db5;
                color: #ffffff;
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
        self._request_port_scan(announce=True)

    def _request_port_scan(self, announce: bool = False) -> None:
        if not self._port_timer.isActive():
            return
        self._announce_scan |= announce
        if not self._scan_running:
            self._scan_running = True
            QThreadPool.globalInstance().start(self._port_scan.run)

    def _port_scan_finished(self, devices: list, error: str) -> None:
        self._scan_running = False
        announce, self._announce_scan = self._announce_scan, False
        if not self._port_timer.isActive():
            return
        if error:
            if announce:
                self._append_sys(f"刷新串口失败：{error}")
            return
        self._update_port_list(devices, announce)
        for session in self._sessions.values():
            if session.listening and session.serial_thread is None:
                self._try_listener_reconnect(session.session_id)

    def _update_port_list(self, devices: list, announce: bool) -> None:
        self._enumerated_ports = {device.device.lower() for device in devices}
        ports: list[tuple[str, str]] = []
        for port_info in devices:
            if self._port_registry.is_blocked(port_info):
                continue
            device = port_info.device
            desc = port_info.description if port_info.description and port_info.description != "n/a" else "串口设备"
            ports.append((device, desc))
        ports.sort(key=lambda item: _port_sort_key(item[0]))
        self._latest_ports = ports
        self._port_descriptions = {device.lower(): desc for device, desc in ports}
        if announce:
            self._append_sys(f"已刷新串口：{len(ports)} 个")
        if self._port_combo.view().isVisible() or (not announce and self._port_combo.hasFocus()):
            return
        self._apply_port_list(select_first=self._port_snapshot is None or announce)

    def _apply_port_list(self, select_first: bool = False) -> None:
        ports = self._latest_ports
        if ports == self._port_snapshot:
            return
        current_port = self._selected_port()
        self._port_snapshot = ports

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
        elif ports and select_first:
            self._port_combo.setCurrentIndex(0)
        else:
            self._port_combo.setCurrentIndex(self._port_combo.count() - 1)
            self._port_combo.setEditText("")
        self._port_combo.blockSignals(False)
        self._port_combo.setToolTip(self._port_combo.currentText())
        if self._port_combo.lineEdit() is not None:
            self._port_combo.lineEdit().setPlaceholderText("选择串口或输入自定义串口")
            if self._port_combo.currentData() != CUSTOM_PORT_DATA:
                self._port_combo.lineEdit().setCursorPosition(0)

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
        elif self._port_combo.lineEdit() is not None:
            self._port_combo.lineEdit().setCursorPosition(0)
        self._save_active_session_properties()

    def _collect_config(self, show_errors: bool = True) -> TerminalSerialConfig | None:
        port = self._selected_port()
        if not port:
            if show_errors:
                QMessageBox.warning(self, "提示", "请先选择或输入串口号。")
            return None
        try:
            baudrate = int(self._baud_combo.currentText().strip())
            if baudrate <= 0:
                raise ValueError
        except ValueError:
            if show_errors:
                QMessageBox.warning(self, "提示", "波特率必须是正整数。")
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
            if session_id == self._active_session_id:
                self._activate_session(session_id)
        else:
            session_id = self._ensure_session_for_config(config)
            session = self._sessions[session_id]
        self._timer_for_session(session_id).stop()
        session.closing_requested = False
        if self._port_registry.is_blocked(config.port):
            self._set_session_status(session_id, "已屏蔽")
            self._append_sys(f"{config.port} 已屏蔽，请从烧录台设备菜单解除屏蔽", session_id=session_id)
            return
        lease = self._port_registry.acquire(
            config.port,
            owner=f"terminal:{session_id}",
            purpose="串口终端",
        )
        if lease is None:
            claim = self._port_registry.claim_for(config.port)
            detail = f"（{claim.purpose}）" if claim is not None else ""
            self._set_session_status(session_id, "串口忙")
            self._append_sys(
                f"{config.port} 正由其他工作台占用{detail}",
                session_id=session_id,
            )
            return
        session.port_lease = lease
        self._set_session_status(session_id, "连接中")
        self._append_sys(
            f"正在连接 {config.port} @ {config.baudrate}, "
            f"{config.bytesize}{config.parity}{config.stopbits:g}, 流控 {config.flow_control}",
            session_id=session_id,
        )
        thread = TerminalSerialThread(config, self)
        thread.opened.connect(lambda message, sid=session_id: self._serial_opened(sid, message))
        thread.settings_applied.connect(
            lambda requested, applied, error, sid=session_id:
                self._serial_settings_applied(sid, thread, requested, applied, error)
        )
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
        self._update_active_connection_controls()
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
            self._request_port_scan()
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
        if not self._port_registry.is_available(config.port):
            self._set_session_status(session_id, "已屏蔽" if self._port_registry.is_blocked(config.port) else "串口忙")
            return
        self._append_sys(f"监听检测到 {config.port}，正在自动重连", session_id=session_id)
        self._start_serial_thread(config, session_id=session_id)

    def _port_is_available(self, port: str) -> bool:
        return port.lower() in self._enumerated_ports

    def _schedule_listener_reconnect(self, session_id: int) -> None:
        session = self._sessions.get(session_id)
        if session is None or not session.listening or session.config is None:
            return
        self._set_session_status(session_id, "监听中")
        timer = self._timer_for_session(session_id)
        if not timer.isActive():
            timer.start()

    def shutdown(self, wait_ms: int = 3000) -> bool:
        self._port_timer.stop()
        for timer in self._reconnect_timers.values():
            timer.stop()
        threads: list[tuple[TerminalSession, TerminalSerialThread]] = []
        for session in self._sessions.values():
            session.listening = False
            thread = session.serial_thread
            if thread is not None:
                thread.request_stop()
                threads.append((session, thread))
        deadline = time.monotonic() + max(0, wait_ms) / 1000
        all_stopped = True
        for session, thread in threads:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            stopped = thread.wait(remaining)
            all_stopped = all_stopped and stopped
            if stopped:
                session.serial_thread = None
                if session.port_lease is not None:
                    session.port_lease.release()
                    session.port_lease = None
        return all_stopped

    def active_connection_count(self) -> int:
        return sum(
            1 for session in self._sessions.values() if session.serial_thread is not None
        )

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
        session.last_sent_term = ""
        if session.pending_config and session.config and session is self._active_session():
            self._show_serial_settings(session.config)
        session.pending_config = None
        if session.closing_requested:
            self._append_sys("串口已断开", session_id=session_id)
            self._set_session_status(session_id, "已断开")
        elif session.status != "错误":
            self._append_sys("串口连接已关闭", session_id=session_id)
            self._set_session_status(session_id, "已关闭")
        session.serial_thread = None
        if session.port_lease is not None:
            session.port_lease.release()
            session.port_lease = None
        session.closing_requested = False
        if session.listening:
            self._schedule_listener_reconnect(session_id)
        elif session.status != "错误":
            self._set_session_status(session_id, "未连接")
        self._update_active_connection_controls()

    def _set_connected_controls(self, connected: bool) -> None:
        session = self._active_session()
        can_edit = not connected or (session is not None and session.status == "已连接" and not session.closing_requested)
        self._port_combo.setEnabled(not connected)
        for widget in (
            self._baud_combo,
            self._bytesize_combo,
            self._parity_combo,
            self._stopbits_combo,
            self._flow_combo,
        ):
            widget.setEnabled(can_edit)
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
            timer.timeout.connect(self._request_port_scan)
            self._reconnect_timers[session_id] = timer
        return timer

    def _update_active_connection_controls(self) -> None:
        session = self._active_session()
        connected = session is not None and session.serial_thread is not None
        self._set_connected_controls(connected)
        self._update_term_type_combo()
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
        # Store the selection, including 'auto', rather than its resolved bytes.
        session.newline_by_mode[session.mode] = self._selected_newline()
        session.timestamp_enabled = self._timestamp_check.isChecked()
        session.hex_enabled = self._hex_check.isChecked()
        session.auto_follow = self._auto_follow_btn.isChecked()
        session.listening = self._listen_btn.isChecked()
        session.send_history = self._send_history
        if session.serial_thread is None:
            config = self._collect_config(show_errors=False)
            if config is not None:
                self._update_session_config(session, config)
        self._refresh_session_table()

    def _commit_serial_settings(self, *_args) -> None:
        session = self._active_session()
        if self._updating_session_controls or session is None:
            return
        if session.serial_thread is None:
            self._save_active_session_properties()
            return
        if session.status != "已连接" or session.closing_requested or session.config is None:
            return
        displayed = session.pending_config or session.config
        config = self._collect_config(show_errors=False)
        if config is None:
            self._show_serial_settings(displayed)
            QMessageBox.warning(self, "串口参数", "请输入有效的正整数波特率。")
        elif config != displayed:
            session.pending_config = config
            session.serial_thread.request_settings(config)

    def _serial_settings_applied(
        self, session_id: int, thread: TerminalSerialThread, requested: TerminalSerialConfig,
        applied: TerminalSerialConfig, error: str,
    ) -> None:
        session = self._sessions.get(session_id)
        if session is None or session.serial_thread is not thread:
            return
        self._update_session_config(session, applied)
        latest = session.pending_config is requested
        if latest:
            session.pending_config = None
        self._refresh_session_table()
        if error:
            message = f"串口参数未生效，已恢复上次设置。\n{error}"
            self._append_sys(message, session_id=session_id)
            if latest and session is self._active_session():
                self._show_serial_settings(applied)
                QMessageBox.warning(self, "串口参数", message)

    def _show_serial_settings(self, config: TerminalSerialConfig) -> None:
        previous = self._updating_session_controls
        self._updating_session_controls = True
        try:
            self._baud_combo.setCurrentText(str(config.baudrate))
            self._set_combo_data(self._bytesize_combo, config.bytesize)
            self._set_combo_data(self._parity_combo, config.parity)
            self._set_combo_data(self._stopbits_combo, config.stopbits)
            self._set_combo_data(self._flow_combo, config.flow_control)
        finally:
            self._updating_session_controls = previous

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
        if session_id != self._active_session_id:
            self._log.moveCursor(QTextCursor.MoveOperation.Start)
        self._active_session_id = session_id
        self._records = session.records
        self._record_hex_overrides = session.record_hex_overrides
        self._send_history = session.send_history
        self._history_index = None
        self._history_draft = ""
        self._updating_session_controls = True
        self._set_combo_data(self._mode_combo, session.mode)
        self._timestamp_check.setChecked(session.timestamp_enabled)
        self._hex_check.setChecked(session.hex_enabled)
        self._auto_follow_btn.setChecked(session.auto_follow)
        self._encoding_combo.setCurrentText(session.encoding)
        self._set_combo_data(self._newline_combo, session.newline_by_mode[session.mode])
        if session.config is not None:
            self._select_port_text(session.config.port)
            self._show_serial_settings(session.pending_config or session.config)
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
        self._update_mode_input_ui(session.mode)
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
        if session is None or session.status == status:
            return
        session.status = status
        if session_id == self._active_session_id:
            self._set_status(status, self._state_for_session_status(status))
            self._update_active_connection_controls()
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
        self._focus_mode_input()

    def _on_mode_changed(self) -> None:
        mode = self._current_mode()
        session = self._active_session()
        if session is not None and not self._updating_session_controls:
            # Block the intermediate signal so a mode switch cannot save the
            # previous mode's selection under the newly selected mode.
            self._newline_combo.blockSignals(True)
            self._set_combo_data(self._newline_combo, session.newline_by_mode[mode])
            self._newline_combo.blockSignals(False)
        self._update_mode_input_ui(mode)
        self._save_active_session_properties()
        self._schedule_render()
        if not self._updating_session_controls:
            self._focus_mode_input()

    def set_mode(self, mode: str) -> bool:
        """Select a terminal type, including requests from Windows Jump List."""

        index = self._mode_combo.findData(mode)
        if index < 0:
            return False
        self._mode_combo.setCurrentIndex(index)
        return True

    def _on_compact_toggled(self, checked: bool) -> None:
        keep_fullscreen = self._fullscreen_btn.isChecked()
        top_level = self.window()
        self._compact_btn.setToolTip(
            "恢复完整应用界面" if checked else "隐藏其他工作台和应用信息，仅保留串口终端"
        )
        self.compactModeChanged.emit(checked)
        if keep_fullscreen:
            top_level.showFullScreen()
        self._log.setFocus()

    def _on_fullscreen_toggled(self, checked: bool) -> None:
        top_level = self.window()
        if checked:
            self._fullscreen_restore_geometry = top_level.saveGeometry()
            self._fullscreen_restore_state = top_level.windowState()
            top_level.showFullScreen()
            self._fullscreen_btn.setToolTip("退出全屏显示")
        else:
            restore_geometry = self._fullscreen_restore_geometry
            restore_state = self._fullscreen_restore_state
            top_level.showNormal()
            if restore_geometry is not None:
                top_level.restoreGeometry(restore_geometry)
            if restore_state is not None and (
                restore_state & Qt.WindowState.WindowMaximized
            ):
                top_level.showMaximized()
            self._fullscreen_btn.setToolTip("切换应用全屏显示；可与精简模式同时启用")
        self._log.setFocus()

    def _update_mode_input_ui(self, mode: str) -> None:
        unix_terminal = mode == "unix"
        self._term_type_combo.parentWidget().setVisible(unix_terminal)
        self._update_term_type_combo()
        # Unix line wrapping follows the viewport width. Reserve the scrollbar's
        # space so replacing the document cannot trigger another width change.
        self._log.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn if unix_terminal else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._input_stack.setVisible(not unix_terminal)
        self._input_stack.setCurrentIndex(1 if mode == "plain" else 0)
        self._input_title.setText("输入设置" if unix_terminal else "发送")
        self._unix_input_hint.setVisible(unix_terminal)
        for button in self._unix_quick_buttons.values():
            button.setVisible(unix_terminal)
        self._timestamp_check.setEnabled(not unix_terminal)
        self._hex_check.setEnabled(not unix_terminal)

    def _focus_mode_input(self) -> None:
        mode = self._current_mode()
        if mode == "unix":
            self._log.setFocus()
        elif mode == "plain":
            self._plain_input.setFocus()
        else:
            self._terminal_input.setFocus()

    def _current_mode(self) -> str:
        return str(self._mode_combo.currentData() or "terminal")

    def _current_encoding(self) -> str:
        return self._encoding_combo.currentText().strip() or "utf-8"

    def _selected_newline(self) -> str:
        value = self._newline_combo.currentData()
        return value if isinstance(value, str) else "\r\n"

    def _current_newline(self) -> str:
        """Resolve the input convention; RX line endings do not identify it."""
        value = self._selected_newline()
        if value == "auto":
            return "\r" if self._current_mode() == "unix" else "\r\n"
        return value

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
        encoding = self._current_encoding()
        try:
            data = text.encode(encoding)
        except LookupError:
            self._append_sys(f"发送失败：未知编码 {encoding}")
            return False
        except UnicodeEncodeError as exc:
            self._append_sys(f"发送失败：文本无法用 {encoding} 编码：{exc}")
            return False
        return self._send_bytes(data)

    def _send_bytes(self, data: bytes, *, clears_unix_display: bool = False) -> bool:
        if not data:
            return True
        session = self._active_session()
        if session is None or session.serial_thread is None:
            self._append_record("SYS", b"", "发送失败：串口未连接", clears_unix_display=clears_unix_display)
            return False
        session.serial_thread.send(data)
        self._append_record("TX", data, clears_unix_display=clears_unix_display)
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
        if source is self._log.viewport() and event.type() == QEvent.Type.Resize:
            if event.size().width() != event.oldSize().width() and self._current_mode() == "unix":
                self._schedule_render()
        if source is self._log and event.type() == QEvent.Type.KeyPress:
            if self._current_mode() == "unix":
                return self._handle_unix_terminal_key(event)
            return self._handle_log_clipboard_key(event)
        if (
            source is self._log
            and event.type() == QEvent.Type.InputMethod
            and self._current_mode() == "unix"
        ):
            commit_text = event.commitString()
            if commit_text:
                self._send_text(commit_text)
            return True

        if event.type() == QEvent.Type.KeyPress and source in (
            self._terminal_input,
            self._plain_input,
        ):
            key = event.key()
            if event.modifiers() == (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
                if key == Qt.Key.Key_V:
                    source.paste()
                    return True
                if key == Qt.Key.Key_C:
                    source.copy()
                    return True
            if source is self._terminal_input and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._send_terminal_line()
                return True
            if key == Qt.Key.Key_Up and self._can_use_history(source, previous=True):
                return self._navigate_send_history(source, -1)
            if key == Qt.Key.Key_Down and self._can_use_history(source, previous=False):
                return self._navigate_send_history(source, 1)
        return super().eventFilter(source, event)

    def _handle_log_clipboard_key(self, event) -> bool:
        ctrl = Qt.KeyboardModifier.ControlModifier
        shift = Qt.KeyboardModifier.ShiftModifier
        key, modifiers = event.key(), event.modifiers()
        if (key == Qt.Key.Key_V and modifiers in (ctrl, ctrl | shift)) or (
            key == Qt.Key.Key_Insert and modifiers == shift
        ):
            self._paste_from_log()
            return True
        if (
            key == Qt.Key.Key_C
            and (modifiers == (ctrl | shift) or (modifiers == ctrl and self._current_mode() != "unix"))
        ) or (key == Qt.Key.Key_Insert and modifiers == ctrl):
            self._copy_log_selection()
            return True
        return False

    def _handle_unix_terminal_key(self, event) -> bool:
        if self._current_mode() != "unix":
            return False
        if self._handle_log_clipboard_key(event):
            return True
        key = event.key()
        modifiers = event.modifiers()
        encoding = self._current_encoding()
        try:
            data = encode_unix_key(
                key,
                event.text(),
                modifiers,
                encoding,
                self._current_newline(),
            )
        except LookupError:
            self._append_sys(f"发送失败：未知编码 {encoding}")
            return True
        except UnicodeEncodeError as exc:
            self._append_sys(f"发送失败：文本无法用 {encoding} 编码：{exc}")
            return True
        if data is None:
            return False
        self._send_unix_key_bytes(data)
        return True

    def _paste_unix_terminal(self) -> None:
        text = QApplication.clipboard().text()
        if not text:
            return
        self._send_text(normalize_pasted_text(text, self._current_newline()))
        self._log.setFocus()

    def _paste_from_log(self) -> None:
        mode = self._current_mode()
        if mode == "unix":
            self._paste_unix_terminal()
            return
        editor = self._plain_input if mode == "plain" else self._terminal_input
        editor.setFocus()
        editor.paste()

    def _copy_log_selection(self) -> None:
        cursor = self._log.textCursor()
        if cursor.hasSelection():
            QApplication.clipboard().setText(cursor.selection().toPlainText())

    def _show_terminal_menu(self, view, point) -> None:
        menu = self._terminal_menu(view)
        try:
            menu.exec(view.viewport().mapToGlobal(point))
        finally:
            menu.deleteLater()

    def _terminal_menu(self, view) -> QMenu:
        menu = QMenu(self)
        selected = view.textCursor().hasSelection()
        has_text = not view.document().isEmpty()
        if view is self._log:
            unix = self._current_mode() == "unix"
            copy_key = "Ctrl+Shift+C" if unix else "Ctrl+C"
            menu.addAction(f"复制选中内容\t{copy_key}", self._copy_log_selection).setEnabled(selected)
            menu.addAction("复制全部日志", self._copy_all).setEnabled(has_text)
            menu.addAction("全选日志", view.selectAll).setEnabled(has_text)
            menu.addSeparator()
            session = self._active_session()
            can_paste = bool(QApplication.clipboard().text()) and (
                not unix or (session is not None and session.serial_thread is not None)
            )
            label = "粘贴并发送到串口" if unix else "粘贴到发送框"
            menu.addAction(f"{label}\tCtrl+V", self._paste_from_log).setEnabled(can_paste)
            if unix:
                self._add_shell_term_menu(menu, session)
            menu.addSeparator()
            menu.addAction("清空日志", self._clear_log).setEnabled(has_text)
        else:
            menu.addAction("撤销\tCtrl+Z", view.undo).setEnabled(view.document().isUndoAvailable())
            menu.addAction("重做\tCtrl+Y", view.redo).setEnabled(view.document().isRedoAvailable())
            menu.addSeparator()
            menu.addAction("剪切\tCtrl+X", view.cut).setEnabled(selected)
            menu.addAction("复制\tCtrl+C", view.copy).setEnabled(selected)
            menu.addAction("粘贴\tCtrl+V", view.paste).setEnabled(view.canPaste())
            menu.addAction("全选\tCtrl+A", view.selectAll).setEnabled(has_text)
        return menu

    def _can_send_shell_term(self, session: TerminalSession | None) -> bool:
        return (
            session is not None
            and self._current_mode() == "unix"
            and session.serial_thread is not None
            and session.status == "已连接"
            and not session.closing_requested
        )

    def _update_term_type_combo(self) -> None:
        session = self._active_session()
        combo = self._term_type_combo
        combo.hidePopup()
        combo.setEnabled(self._can_send_shell_term(session))
        combo.setCurrentIndex(combo.findText(session.last_sent_term) if session else -1)

    def _send_shell_term(self, term: str | None) -> None:
        session = self._active_session()
        if session is None or not self._can_send_shell_term(session):
            return
        command = _SHELL_TERM_PROBE if term is None else f"export TERM={term}"
        if self._send_text(command + self._current_newline()) and term is not None:
            session.last_sent_term = term
        self._update_term_type_combo()
        self._log.setFocus()

    def _add_shell_term_menu(self, menu: QMenu, session: TerminalSession | None) -> None:
        term_menu = menu.addMenu("设备终端类型（TERM）")
        thread = session.serial_thread if session is not None else None

        def send(term: str | None) -> None:
            # A context menu can stay open across a reconnect.
            if session is not None and self._active_session() is session and session.serial_thread is thread:
                self._send_shell_term(term)

        term_menu.setEnabled(self._can_send_shell_term(session))
        term_menu.addAction("检测可用类型（+ 表示可用）", lambda: send(None))
        term_menu.addSeparator()
        for term in _SHELL_TERM_TYPES:
            term_menu.addAction(f"export TERM={term}", lambda checked=False, value=term: send(value))

    def _send_unix_key_bytes(self, data: bytes) -> None:
        self._send_bytes(data, clears_unix_display=data == b"\x0c")
        self._log.setFocus()

    def _can_use_history(self, source: QWidget, previous: bool) -> bool:
        if source is self._terminal_input:
            return True
        if source is self._log:
            return False
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
        *,
        clears_unix_display: bool = False,
    ) -> None:
        records, overrides = self._records_for_session(session_id)
        records.append(
            TerminalLogRecord(
                record_id=self._next_record_id,
                direction=direction,
                timestamp=datetime.now(),
                payload=payload,
                message=message,
                clears_unix_display=clears_unix_display,
            )
        )
        self._next_record_id += 1
        unix_screen_changed = direction == "RX" or clears_unix_display
        if len(records) > self._MAX_RECORDS:
            removed = records[: len(records) - self._MAX_RECORDS]
            del records[: len(records) - self._MAX_RECORDS]
            for record in removed:
                overrides.pop(record.record_id, None)
                if record.direction == "RX" or record.clears_unix_display:
                    unix_screen_changed = True
        if session_id is None or session_id == self._active_session_id:
            if self._current_mode() != "unix" or unix_screen_changed:
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
        unix = self._current_mode() == "unix"
        scrollbar = self._log.verticalScrollBar()
        previous_value = scrollbar.value()
        previous_max = scrollbar.maximum()
        should_follow = self._auto_follow_btn.isChecked() or self._is_log_at_bottom()
        cursor = self._log.textCursor()
        anchor, position = cursor.anchor(), cursor.position()
        selection = cursor.selectedText()
        self._updating_log_scroll = True
        if unix:
            if not self._update_unix_document():
                self._updating_log_scroll = False
                return
        else:
            self._unix_html_lines = None
            text, is_html = self._build_display_text()
            if is_html:
                self._log.setHtml(text)
            else:
                self._log.setPlainText(text)
        cursor = self._log.textCursor()
        if (selection and (cursor.anchor(), cursor.position()) != (anchor, position)
                and max(anchor, position) < self._log.document().characterCount()):
            cursor.setPosition(anchor)
            cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
            if cursor.selectedText() == selection:
                self._log.setTextCursor(cursor)
        if should_follow:
            self._scroll_log_to_bottom()
        else:
            self._set_log_scroll_value(min(previous_value, scrollbar.maximum()))
        self._updating_log_scroll = False
        if unix:
            return

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
        self._set_log_scroll_value(self._log.verticalScrollBar().maximum())

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

    def _update_unix_document(self) -> bool:
        session = self._active_session()
        lines = [""]
        if session is not None:
            viewport = self._log.viewport()
            text_width = viewport.width() - 2 * self._log.document().documentMargin()
            cell_width = QFontMetricsF(self._log.font(), viewport).horizontalAdvance("M")
            session.unix_terminal.sync(
                (
                    (record.record_id, None if record.clears_unix_display else record.payload)
                    for record in self._records
                    if record.direction == "RX" or record.clears_unix_display
                ),
                self._current_encoding(),
                columns=max(1, int(text_width / cell_width)),
            )
            lines = session.unix_terminal.render_html_lines()

        previous = self._unix_html_lines
        if lines == previous:
            return False
        document = self._log.document()
        if previous is None or document.blockCount() != len(previous):
            cursor = QTextCursor(document)
            cursor.select(QTextCursor.SelectionType.Document)
            html_lines = lines
        else:
            # Keep unchanged QTextBlocks in place, including the scrollback.
            # Include one common row when appending or removing whole lines so
            # the edit also inserts or removes the separating paragraph break.
            first = 0
            while first < min(len(lines), len(previous)) - 1 and lines[first] == previous[first]:
                first += 1
            old_end, new_end = len(previous), len(lines)
            while old_end > first + 1 and new_end > first + 1 and previous[old_end - 1] == lines[new_end - 1]:
                old_end -= 1
                new_end -= 1
            cursor = QTextCursor(document.findBlockByNumber(first))
            last = document.findBlockByNumber(old_end - 1)
            cursor.setPosition(last.position() + last.length() - 1, QTextCursor.MoveMode.KeepAnchor)
            html_lines = lines[first:new_end]
        cursor.insertHtml(self._build_html_page("\n".join(html_lines), unix=True))
        self._unix_html_lines = lines
        return True

    def _build_display_text(self) -> tuple[str, bool]:
        mode = self._current_mode()

        timestamp_enabled = self._timestamp_check.isChecked()
        hex_enabled = self._hex_check.isChecked()
        encoding = self._current_encoding()
        decoded_payloads = self._decode_record_streams(encoding)
        terminal_simple = mode == "terminal" and not timestamp_enabled and not hex_enabled

        if terminal_simple:
            parts: list[str] = []
            for record in self._records:
                if record.direction == "RX":
                    parts.append(decoded_payloads.get(record.record_id, ""))
                elif record.direction == "SYS":
                    parts.append(f"\n[SYS] {record.message}\n")
            return self._build_html_page(self._colorize_html_text("".join(parts))), True

        if timestamp_enabled:
            lines: list[str] = []
            for record in self._records:
                formatted = self._format_record_html(
                    record,
                    encoding,
                    hex_enabled,
                    decoded_payloads.get(record.record_id),
                )
                if formatted:
                    lines.append(formatted)
            return self._build_html_page("\n".join(lines)), True

        lines: list[str] = []
        for record in self._records:
            formatted = self._format_record_plain(
                record,
                encoding,
                hex_enabled,
                decoded_payloads.get(record.record_id),
            )
            if formatted:
                lines.append(formatted)
        return self._build_html_page(self._colorize_html_text("\n".join(lines))), True

    def _format_record_plain(
        self,
        record: TerminalLogRecord,
        encoding: str,
        hex_enabled: bool,
        decoded_text: str | None = None,
    ) -> str:
        if record.direction == "SYS":
            return f"[SYS] {record.message}"

        if hex_enabled:
            body = _format_hex(record.payload)
        else:
            body = (
                decoded_text
                if decoded_text is not None
                else self._decode_payload(record.payload, encoding)
            )
            body = self._normalize_display_text(body)

        return f"[{record.direction}] {body}"

    def _format_record_html(
        self,
        record: TerminalLogRecord,
        encoding: str,
        global_hex_enabled: bool,
        decoded_text: str | None = None,
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
                decoded_text
                if decoded_text is not None
                else self._decode_payload(record.payload, encoding)
            )
            toggle_text = "HEX"
        label = f"[{time_prefix} {record.direction} {len(record.payload)}B]"
        label_html = self._format_html_label(label, self._direction_color(record.direction))
        link = (
            f"<a style=\"color:#93c5fd;text-decoration:none;\" "
            f"href=\"toggle:{record.record_id}\">{toggle_text}</a>"
        )
        return f"{label_html} {link} {self._colorize_html_text(body)}"

    def _build_html_page(self, body: str, *, unix: bool = False) -> str:
        margin = "margin:0;" if unix else ""
        return (
            "<html><body>"
            "<pre style=\"font-family: Consolas, Menlo, 'DejaVu Sans Mono', monospace; "
            f"font-size: 10pt; color: #d1d5db; white-space: pre;{margin}\">"
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

    def _decode_record_streams(self, encoding: str) -> dict[int, str]:
        """Decode RX and TX as independent streams across serial chunk boundaries."""
        decoded: dict[int, str] = {}
        for direction in ("RX", "TX"):
            records = [item for item in self._records if item.direction == direction]
            texts = decode_payload_chunks((item.payload for item in records), encoding)
            decoded.update(
                (item.record_id, text) for item, text in zip(records, texts, strict=True)
            )
        return decoded

    def _normalize_display_text(self, text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")

    def _copy_all(self) -> None:
        QApplication.clipboard().setText(self._log.toPlainText())

    def _clear_log(self) -> None:
        self._records.clear()
        self._record_hex_overrides.clear()
        self._unix_html_lines = None
        self._log.clear()

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)
