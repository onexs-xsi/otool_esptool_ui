from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

import serial
from PyQt6.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from .constants import TOOL_DIR, _build_process_env_dict, _build_tool_command
from .styles import BASE_STYLESHEET
from .verify_plan import (
    DEFAULT_VERIFY_PROFILE_NAME,
    VerifyPattern,
    VerifyProfile,
    VerifyStep,
    build_default_profile,
    describe_step,
    evaluate_match,
    load_single_profile_from_text,
    load_verify_profiles_from_text,
    profile_to_dict,
    profile_to_yaml_text,
    render_template_text,
)
from .verify_visual_editor import VerifyVisualEditor


class YamlSyntaxHighlighter(QSyntaxHighlighter):
    """YAML 代码语法高亮。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        def _fmt(color: str, bold: bool = False) -> QTextCharFormat:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(700)
            return fmt

        # (pattern, group_index, format)  group_index=0 表示整个匹配
        self._rules: list[tuple[re.Pattern, int, QTextCharFormat]] = [
            # YAML key（高亮键名部分）
            (re.compile(r"^\s*([\w_\-]+)\s*:"), 1, _fmt("#1d4ed8", bold=True)),
            # 注释
            (re.compile(r"#[^\n]*"), 0, _fmt("#8b9db7")),
            # 双引号字符串
            (re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"'), 0, _fmt("#059669")),
            # 单引号字符串
            (re.compile(r"'[^']*'"), 0, _fmt("#059669")),
            # 特殊值
            (re.compile(r"\b(true|false|null|~)\b", re.IGNORECASE), 0, _fmt("#7c3aed", bold=True)),
            # 数字
            (re.compile(r"\b-?\d+(\.\d+)?\b"), 0, _fmt("#c2410c")),
            # 列表符号
            (re.compile(r"^\s*-\s"), 0, _fmt("#0369a1")),
            # action 关键字
            (
                re.compile(
                    r"\b(reset|wait|wait_silence|send_text|expect|capture"
                    r"|set_result|clear_buffer|pass|fail)\b"
                ),
                0,
                _fmt("#7c3aed"),
            ),
        ]

    def highlightBlock(self, text: str) -> None:  # type: ignore[override]
        for pattern, group_idx, fmt in self._rules:
            for match in pattern.finditer(text):
                if group_idx == 0:
                    start = match.start()
                    length = match.end() - match.start()
                else:
                    start = match.start(group_idx)
                    length = match.end(group_idx) - match.start(group_idx)
                self.setFormat(start, length, fmt)


class _VerificationStopped(RuntimeError):
    """内部中断异常，用于快速结束线程。"""


class _VerificationPassed(RuntimeError):
    """内部提前通过异常，用于结束后续步骤。"""

    def __init__(self, summary: str) -> None:
        super().__init__(summary)
        self.summary = summary


class VerifyTaskState(Enum):
    WAITING = "等待"
    RUNNING = "执行中"
    PASSED = "通过"
    FAILED = "失败"
    STOPPED = "已停止"


@dataclass
class VerifyTaskItem:
    device_id: str
    port: str
    label: str = "串口设备"
    selected: bool = True
    state: VerifyTaskState = VerifyTaskState.WAITING
    current_step: str = "-"
    result_summary: str = ""
    error_message: str = ""
    captured_values: dict[str, str] = field(default_factory=dict)
    thread: QThread | None = None
    worker: "VerifyWorker | None" = None


class VerifyWorker(QObject):
    log_message = pyqtSignal(str)
    step_changed = pyqtSignal(str)
    result_changed = pyqtSignal(str, object)
    finished = pyqtSignal(str, str)

    def __init__(self, port: str, profile: VerifyProfile) -> None:
        super().__init__()
        self._port = port
        self._profile = profile
        self._serial: serial.Serial | None = None
        self._stop_requested = False
        self._buffer = ""
        self._line_buffer = ""
        self._captured_values: dict[str, str] = {}
        self._result_text = ""

    def request_stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        try:
            self._open_serial()
            self._emit_log(
                f"已打开串口 {self._port} @ {self._profile.serial.baudrate} bps"
            )
            total = len(self._profile.steps)
            for index, step in enumerate(self._profile.steps, start=1):
                self._ensure_not_stopped()
                step_title = f"{index}/{total} {describe_step(step)}"
                self.step_changed.emit(step_title)
                self._emit_log(f"开始步骤：{step_title}")
                self._run_step(step)
            self._pump_serial(120)
            self._flush_line_buffer()
            self._emit_result_preview()
            self.finished.emit("passed", "全部步骤通过")
        except _VerificationPassed as exc:
            if exc.summary:
                self._result_text = exc.summary
            self._emit_result_preview()
            self._emit_log(f"任务提前通过：{exc.summary or '脚本主动通过'}")
            self.finished.emit("passed", exc.summary or "脚本主动通过")
        except _VerificationStopped:
            self._emit_result_preview()
            self._emit_log("任务已停止")
            self.finished.emit("stopped", "用户停止")
        except Exception as exc:  # noqa: BLE001
            self._emit_result_preview()
            self._emit_log(f"任务失败：{exc}")
            self.finished.emit("failed", str(exc))
        finally:
            self._close_serial()

    def _run_step(self, step: VerifyStep) -> None:
        max_attempts = max(1, step.retry_count + 1)
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            self._ensure_not_stopped()
            try:
                if attempt > 1:
                    self._emit_log(f"重试步骤：第 {attempt}/{max_attempts} 次")
                self._run_step_once(step)
                return
            except (_VerificationStopped, _VerificationPassed):
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= max_attempts:
                    raise
                delay_ms = max(0, step.retry_delay_ms)
                self._emit_log(
                    f"步骤执行失败：{exc}；{delay_ms} ms 后重试"
                    if delay_ms > 0
                    else f"步骤执行失败：{exc}；立即重试"
                )
                if delay_ms > 0:
                    self._pump_serial(delay_ms)
        if last_error is not None:
            raise last_error

    def _run_step_once(self, step: VerifyStep) -> None:
        if step.action == "reset":
            self._run_reset(step)
            return
        if step.action == "wait":
            self._emit_log(f"等待 {step.duration_ms} ms")
            self._pump_serial(step.duration_ms)
            return
        if step.action == "wait_silence":
            self._run_wait_silence(step)
            return
        if step.action == "send_text":
            self._run_send_text(step)
            return
        if step.action == "expect":
            self._run_expect(step)
            return
        if step.action == "capture":
            self._run_capture(step)
            return
        if step.action == "set_result":
            self._run_set_result(step)
            return
        if step.action == "pass":
            self._run_pass(step)
            return
        if step.action == "fail":
            self._run_fail(step)
            return
        if step.action == "clear_buffer":
            self._clear_buffer(log_message="已清空匹配缓冲区")
            return
        raise RuntimeError(f"不支持的步骤类型：{step.action}")

    def _run_wait_silence(self, step: VerifyStep) -> None:
        self._emit_log(
            f"等待串口静默 {step.duration_ms} ms（总超时 {step.timeout_ms} ms）"
        )
        deadline = time.monotonic() + step.timeout_ms / 1000.0
        silence_start = time.monotonic()
        while time.monotonic() < deadline:
            self._ensure_not_stopped()
            had_data = self._read_once()
            now = time.monotonic()
            if had_data:
                silence_start = now
            elif (now - silence_start) * 1000 >= step.duration_ms:
                self._emit_log("串口已进入静默状态")
                return
            remaining = deadline - now
            if remaining <= 0:
                break
            time.sleep(min(0.02, remaining))
        raise RuntimeError(f"等待静默超时：{step.timeout_ms} ms 内未达到 {step.duration_ms} ms 静默")

    def _run_reset(self, step: VerifyStep) -> None:
        method = step.method or "serial_toggle"
        self._emit_log(f"执行复位：{method}")
        if method == "none":
            return
        if method == "serial_toggle":
            if self._serial is None:
                self._open_serial()
            if self._serial is None:
                raise RuntimeError("串口尚未打开，无法执行复位")
            self._clear_buffer(log_message=None)
            try:
                self._serial.setDTR(False)
                self._serial.setRTS(True)
                self._pump_serial(step.hold_ms)
                self._serial.setRTS(False)
                self._pump_serial(step.release_wait_ms)
            except serial.SerialException as exc:
                raise RuntimeError(f"串口复位失败：{exc}") from exc
            self._emit_log("串口复位已完成，等待设备启动输出")
            return
        if method == "esptool_run":
            self._close_serial()
            cmd = _build_tool_command(
                "esptool",
                "--chip",
                "auto",
                "--port",
                self._port,
                "--baud",
                str(self._profile.serial.baudrate),
                "run",
            )
            self._emit_log("执行命令：" + subprocess.list2cmdline(cmd))
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=str(TOOL_DIR),
                    env=_build_process_env_dict(),
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("esptool run 超时") from exc
            output = ((result.stdout or "") + (result.stderr or "")).replace("\r\n", "\n").replace("\r", "\n")
            for line in output.split("\n"):
                if line:
                    self.log_message.emit(f"[复位] {line}")
            if result.returncode != 0:
                raise RuntimeError(f"esptool run 失败，退出码 {result.returncode}")
            self._reopen_serial_with_retry(2500)
            self._clear_buffer(log_message=None)
            self._pump_serial(step.release_wait_ms)
            self._emit_log("esptool 复位完成，串口已重新打开")
            return
        raise RuntimeError(f"不支持的复位方式：{method}")

    def _run_send_text(self, step: VerifyStep) -> None:
        if self._serial is None:
            self._open_serial()
        if self._serial is None:
            raise RuntimeError("串口尚未打开，无法发送文本")

        payload = step.text + (self._profile.serial.newline if step.append_newline else "")
        preview = payload.replace("\r", "\\r").replace("\n", "\\n") or "<空>"
        self._emit_log(f"发送串口输入：{preview}")
        try:
            encoded = payload.encode(self._profile.serial.encoding)
        except LookupError as exc:
            raise RuntimeError(f"编码不可用：{self._profile.serial.encoding}") from exc

        try:
            if step.char_delay_ms > 0:
                for byte in encoded:
                    self._ensure_not_stopped()
                    self._serial.write(bytes([byte]))
                    self._serial.flush()
                    self._pump_serial(step.char_delay_ms)
            else:
                self._serial.write(encoded)
                self._serial.flush()
                self._pump_serial(80)
        except serial.SerialException as exc:
            raise RuntimeError(f"发送串口输入失败：{exc}") from exc

    def _run_expect(self, step: VerifyStep) -> None:
        patterns_text = ", ".join(pattern.pattern for pattern in step.patterns)
        self._emit_log(
            f"开始等待匹配（{step.match_mode}/{step.match_type}, {step.timeout_ms} ms）：{patterns_text}"
        )
        deadline = time.monotonic() + step.timeout_ms / 1000.0
        while time.monotonic() < deadline:
            self._ensure_not_stopped()
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            self._pump_serial(min(120, remaining_ms))
            result = evaluate_match(self._buffer, step)
            if step.match_mode == "none":
                if result.matched_patterns:
                    raise RuntimeError(
                        "命中禁止字段：" + ", ".join(result.matched_patterns)
                    )
            elif result.satisfied:
                self._emit_log("匹配成功：" + ", ".join(result.matched_patterns))
                return

        result = evaluate_match(self._buffer, step)
        if step.match_mode == "none":
            self._emit_log("检查通过：未命中禁止字段")
            return
        pending = result.pending_patterns or [pattern.pattern for pattern in step.patterns]
        raise RuntimeError("超时未匹配字段：" + ", ".join(pending))

    def _run_capture(self, step: VerifyStep) -> None:
        names = [pattern.name or pattern.pattern for pattern in step.patterns]
        self._emit_log(
            f"开始读取参数（{step.match_mode}/{step.match_type}, {step.timeout_ms} ms）：{', '.join(names)}"
        )
        deadline = time.monotonic() + step.timeout_ms / 1000.0
        while time.monotonic() < deadline:
            self._ensure_not_stopped()
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            self._pump_serial(min(120, remaining_ms))

            captured: dict[str, str] = {}
            missing: list[str] = []
            for pattern in step.patterns:
                value = self._extract_pattern_value(step, pattern)
                if value is None:
                    missing.append(pattern.name or pattern.pattern)
                else:
                    captured[pattern.name or pattern.pattern] = value

            if step.match_mode == "all" and len(captured) == len(step.patterns):
                self._store_captured_values(captured)
                return
            if step.match_mode == "any" and captured:
                self._store_captured_values(captured)
                return

        if step.match_mode == "any":
            raise RuntimeError("超时未读取到任何参数")
        raise RuntimeError("超时未读取参数：" + ", ".join(missing or names))

    def _run_set_result(self, step: VerifyStep) -> None:
        rendered = self._render_step_text(step, default_text="")
        if not rendered:
            raise RuntimeError("结果模板为空")
        self._result_text = rendered
        self._emit_log(f"设置结果展示：{rendered}")
        self._emit_result_preview()

    def _run_pass(self, step: VerifyStep) -> None:
        rendered = self._render_step_text(step, default_text="脚本主动通过")
        self._emit_log(f"脚本主动判定通过：{rendered}")
        raise _VerificationPassed(rendered)

    def _run_fail(self, step: VerifyStep) -> None:
        rendered = self._render_step_text(step, default_text="脚本主动判定失败")
        raise RuntimeError(rendered)

    def _render_step_text(self, step: VerifyStep, default_text: str) -> str:
        template = step.text.strip()
        if not template:
            return default_text
        try:
            return render_template_text(
                template,
                {"port": self._port, **self._captured_values},
            )
        except KeyError as exc:
            raise RuntimeError(f"模板引用了未读取参数：{exc.args[0]}") from exc

    def _extract_pattern_value(self, step: VerifyStep, pattern: VerifyPattern) -> str | None:
        if step.match_type == "regex":
            flags = 0 if step.case_sensitive else re.IGNORECASE
            matches = list(re.finditer(pattern.pattern, self._buffer, flags))
            if not matches:
                return None
            match = matches[-1]
            if pattern.name and pattern.name in match.groupdict():
                named_value = match.groupdict().get(pattern.name)
                if named_value is not None:
                    return str(named_value)
            try:
                if pattern.group == 0:
                    return str(match.group(0))
                return str(match.group(pattern.group))
            except (IndexError, KeyError):
                if match.lastindex:
                    return str(match.group(1))
                return str(match.group(0))

        haystack = self._buffer if step.case_sensitive else self._buffer.lower()
        needle = pattern.pattern if step.case_sensitive else pattern.pattern.lower()
        idx = haystack.rfind(needle)
        if idx < 0:
            return None
        return self._buffer[idx : idx + len(pattern.pattern)]

    def _store_captured_values(self, values: dict[str, str]) -> None:
        for key, value in values.items():
            cleaned = value.strip()
            self._captured_values[key] = cleaned
            self._emit_log(f"读取参数：{key}={cleaned}")
        self._emit_result_preview()

    def _build_result_text(self) -> str:
        if self._result_text.strip():
            return self._result_text.strip()
        if not self._captured_values:
            return ""
        items = list(self._captured_values.items())
        parts = [f"{key}={value}" for key, value in items[:4]]
        if len(items) > 4:
            parts.append("…")
        return " / ".join(parts)

    def _emit_result_preview(self) -> None:
        self.result_changed.emit(self._build_result_text(), dict(self._captured_values))

    def _open_serial(self, announce: bool = False) -> None:
        if self._serial is not None and self._serial.is_open:
            return
        serial_config = self._profile.serial
        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=serial_config.baudrate,
                bytesize=serial_config.bytesize,
                parity=serial_config.parity,
                stopbits=serial_config.stopbits,
                timeout=serial_config.timeout_ms / 1000.0,
                write_timeout=1,
            )
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        except serial.SerialException as exc:
            raise RuntimeError(f"打开串口失败：{exc}") from exc
        if announce:
            self._emit_log(f"串口已打开：{self._port}")

    def _reopen_serial_with_retry(self, timeout_ms: int) -> None:
        deadline = time.monotonic() + timeout_ms / 1000.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            self._ensure_not_stopped()
            try:
                self._open_serial()
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.1)
        raise RuntimeError(f"复位后重新打开串口失败：{last_error or '未知错误'}")

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

    def _pump_serial(self, duration_ms: int) -> None:
        end_time = time.monotonic() + max(0, duration_ms) / 1000.0
        while time.monotonic() < end_time:
            self._ensure_not_stopped()
            had_data = self._read_once()
            if not had_data:
                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.02, remaining))

    def _read_once(self) -> bool:
        if self._serial is None:
            return False
        try:
            waiting = max(1, self._serial.in_waiting)
            raw = self._serial.read(waiting)
        except serial.SerialException as exc:
            raise RuntimeError(f"读取串口失败：{exc}") from exc
        if not raw:
            return False
        text = raw.decode(self._profile.serial.encoding, errors="replace")
        self._append_serial_text(text)
        return True

    def _append_serial_text(self, text: str) -> None:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        self._buffer += normalized
        max_chars = self._profile.serial.max_buffer_chars
        if len(self._buffer) > max_chars:
            self._buffer = self._buffer[-max_chars:]

        self._line_buffer += normalized
        lines = self._line_buffer.split("\n")
        self._line_buffer = lines.pop()
        for line in lines:
            if line:
                self.log_message.emit(f"[串口] {line}")

    def _flush_line_buffer(self) -> None:
        if self._line_buffer:
            self.log_message.emit(f"[串口] {self._line_buffer}")
            self._line_buffer = ""

    def _clear_buffer(self, log_message: str | None) -> None:
        self._buffer = ""
        self._line_buffer = ""
        if self._serial is not None:
            try:
                self._serial.reset_input_buffer()
            except serial.SerialException:
                pass
        if log_message:
            self._emit_log(log_message)

    def _emit_log(self, message: str) -> None:
        self.log_message.emit(message)

    def _ensure_not_stopped(self) -> None:
        if self._stop_requested:
            raise _VerificationStopped()


class VerifyWidget(QWidget):
    """批量串口启动日志检验台。"""

    MAX_CONCURRENT = 4

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._profiles: dict[str, VerifyProfile] = {}
        self._tasks: list[VerifyTaskItem] = []
        self._auto_mode = False
        self._run_all_requested = False
        self._batch_profile: VerifyProfile | None = None
        self._batch_ports: set[str] = set()
        self._updating_editor_views = False
        self._table_refreshing = False
        self._splitter_sizes_initialized = False
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1500)
        self._poll_timer.timeout.connect(self._poll_ports)
        self._init_ui()
        self._apply_style()
        self._load_profiles_from_default_sources()

    def _make_summary_pill(self, text: str, accent: str = "default") -> QLabel:
        label = QLabel(text)
        label.setObjectName("summaryPill")
        label.setProperty("accent", accent)
        return label

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.setHandleWidth(10)

        # ── 左侧：脚本编辑区 ──────────────────────────────────────────────
        plan_frame = QFrame()
        plan_frame.setObjectName("sectionFrame")
        plan_frame.setMinimumWidth(660)
        plan_layout = QVBoxLayout(plan_frame)
        plan_layout.setContentsMargins(14, 10, 14, 10)
        plan_layout.setSpacing(8)

        plan_header = QHBoxLayout()
        plan_header.setSpacing(8)
        plan_title = QLabel("检验脚本")
        plan_title.setObjectName("sectionTitle")
        profile_label = QLabel("配置")
        profile_label.setObjectName("configLabel")
        self._profile_combo = QComboBox()
        self._profile_combo.setEditable(True)
        self._profile_combo.setMinimumWidth(200)
        self._load_profile_btn = QPushButton("载入")
        self._load_profile_btn.clicked.connect(self._load_selected_profile)
        self._import_btn = QPushButton("导入 YAML")
        self._import_btn.clicked.connect(self._import_profiles)
        self._export_btn = QPushButton("导出 YAML")
        self._export_btn.clicked.connect(self._export_current_profile)
        self._new_profile_btn = QPushButton("新建")
        self._new_profile_btn.clicked.connect(self._new_profile)
        self._validate_btn = QPushButton("检查脚本")
        self._validate_btn.clicked.connect(self._validate_current_profile_with_feedback)

        plan_header.addWidget(plan_title)
        plan_header.addStretch(1)
        plan_header.addWidget(profile_label)
        plan_header.addWidget(self._profile_combo)
        plan_header.addWidget(self._load_profile_btn)
        plan_header.addWidget(self._new_profile_btn)
        plan_header.addWidget(self._import_btn)
        plan_header.addWidget(self._export_btn)
        plan_header.addWidget(self._validate_btn)

        help_label = QLabel(
            "默认推荐使用“可视化编排”积木配置；高级用户可切到 YAML。\n"
            "当前支持步骤：reset / wait / wait_silence / send_text / expect / capture / set_result / pass / fail / clear_buffer。\n"
            "expect/capture 支持失败重试；wait_silence 适合等日志收尾；pass/fail 适合脚本分段收口。"
        )
        help_label.setWordWrap(True)
        help_label.setObjectName("statusLabel")

        plan_meta_bar = QHBoxLayout()
        plan_meta_bar.setSpacing(8)
        self._profile_name_pill = self._make_summary_pill("配置：-", "primary")
        self._profile_steps_pill = self._make_summary_pill("步骤 0", "default")
        self._profile_capture_pill = self._make_summary_pill("读取 0", "success")
        self._profile_advanced_pill = self._make_summary_pill("高级 0", "warning")
        plan_meta_bar.addWidget(self._profile_name_pill)
        plan_meta_bar.addWidget(self._profile_steps_pill)
        plan_meta_bar.addWidget(self._profile_capture_pill)
        plan_meta_bar.addWidget(self._profile_advanced_pill)
        plan_meta_bar.addStretch(1)

        self._visual_editor = VerifyVisualEditor()
        self._plan_edit = QPlainTextEdit()
        self._plan_edit.setObjectName("verifyPlanEdit")
        self._plan_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        _mono_font = QFont()
        _mono_font.setFamilies(["Consolas", "Menlo", "DejaVu Sans Mono", "monospace"])
        _mono_font.setStyleHint(QFont.StyleHint.TypeWriter)
        _mono_font.setPointSize(10)
        self._plan_edit.setFont(_mono_font)
        self._yaml_highlighter = YamlSyntaxHighlighter(self._plan_edit.document())

        # YAML tab 包一层，顶部加格式化工具栏
        yaml_tab = QWidget()
        yaml_tab_layout = QVBoxLayout(yaml_tab)
        yaml_tab_layout.setContentsMargins(0, 4, 0, 0)
        yaml_tab_layout.setSpacing(4)
        yaml_toolbar = QHBoxLayout()
        yaml_toolbar.setContentsMargins(8, 0, 8, 0)
        yaml_toolbar.setSpacing(6)
        self._format_yaml_btn = QPushButton("格式化")
        self._format_yaml_btn.setToolTip("整理 YAML 缩进与格式")
        self._format_yaml_btn.clicked.connect(self._format_yaml)
        yaml_toolbar.addStretch(1)
        yaml_toolbar.addWidget(self._format_yaml_btn)
        yaml_tab_layout.addLayout(yaml_toolbar)
        yaml_tab_layout.addWidget(self._plan_edit, 1)

        self._editor_tabs = QTabWidget()
        self._editor_tabs.addTab(self._visual_editor, "可视化编排")
        self._editor_tabs.addTab(yaml_tab, "YAML")
        self._editor_tabs.setCurrentIndex(0)
        self._editor_tabs.currentChanged.connect(self._on_editor_tab_changed)

        plan_layout.addLayout(plan_header)
        plan_layout.addWidget(help_label)
        plan_layout.addLayout(plan_meta_bar)
        plan_layout.addWidget(self._editor_tabs, 1)

        # ── 右侧：设备队列 + 日志 ───────────────────────────────────────
        self._right_splitter = QSplitter(Qt.Orientation.Vertical)
        self._right_splitter.setChildrenCollapsible(False)
        self._right_splitter.setHandleWidth(10)
        self._right_splitter.setMinimumWidth(540)

        dev_frame = QFrame()
        dev_frame.setObjectName("sectionFrame")
        dev_layout = QVBoxLayout(dev_frame)
        dev_layout.setContentsMargins(12, 8, 12, 8)
        dev_layout.setSpacing(8)

        dev_header = QHBoxLayout()
        dev_header.setSpacing(8)
        dev_title = QLabel("设备队列")
        dev_title.setObjectName("sectionTitle")
        self._dev_count_label = QLabel("0 台")
        self._dev_count_label.setObjectName("countBadge")
        dev_hint = QLabel("双击设备行即可开始/停止；“全部开始”仅对勾选设备生效。")
        dev_hint.setObjectName("statusLabel")
        dev_hint.setWordWrap(True)
        self._select_all_btn = QPushButton("全选")
        self._select_all_btn.clicked.connect(lambda: self._set_all_selected(True))
        self._clear_selection_btn = QPushButton("全不选")
        self._clear_selection_btn.clicked.connect(lambda: self._set_all_selected(False))
        self._auto_verify_btn = QPushButton("自动校验：关")
        self._auto_verify_btn.setCheckable(True)
        self._auto_verify_btn.toggled.connect(self._toggle_auto_mode)
        dev_header.addWidget(dev_title)
        dev_header.addWidget(self._dev_count_label)
        dev_header.addWidget(dev_hint, 1)
        dev_header.addWidget(self._select_all_btn)
        dev_header.addWidget(self._clear_selection_btn)
        dev_header.addWidget(self._auto_verify_btn)

        dev_stats = QHBoxLayout()
        dev_stats.setSpacing(8)
        self._task_total_pill = self._make_summary_pill("总数 0", "primary")
        self._task_selected_pill = self._make_summary_pill("勾选 0", "default")
        self._task_running_pill = self._make_summary_pill("执行中 0", "warning")
        self._task_passed_pill = self._make_summary_pill("通过 0", "success")
        self._task_failed_pill = self._make_summary_pill("失败 0", "danger")
        dev_stats.addWidget(self._task_total_pill)
        dev_stats.addWidget(self._task_selected_pill)
        dev_stats.addWidget(self._task_running_pill)
        dev_stats.addWidget(self._task_passed_pill)
        dev_stats.addWidget(self._task_failed_pill)
        dev_stats.addStretch(1)

        self._dev_table = QTableWidget()
        self._dev_table.setColumnCount(6)
        self._dev_table.setHorizontalHeaderLabels(["选", "串口", "描述", "状态", "当前步骤", "结果"])
        dev_header_view = self._dev_table.horizontalHeader()
        dev_header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        dev_header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        dev_header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        dev_header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        dev_header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        dev_header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._dev_table.setColumnWidth(0, 46)
        self._dev_table.setColumnWidth(1, 78)
        self._dev_table.setColumnWidth(2, 140)
        self._dev_table.setColumnWidth(3, 88)
        self._dev_table.setColumnWidth(4, 160)
        self._dev_table.verticalHeader().hide()
        self._dev_table.verticalHeader().setDefaultSectionSize(38)
        self._dev_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._dev_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._dev_table.setAlternatingRowColors(True)
        self._dev_table.itemChanged.connect(self._on_device_item_changed)
        self._dev_table.cellDoubleClicked.connect(self._on_device_double_clicked)

        dev_buttons = QHBoxLayout()
        dev_buttons.setSpacing(8)
        self._start_all_btn = QPushButton("全部开始")
        self._start_all_btn.setObjectName("primaryButton")
        self._start_all_btn.clicked.connect(self._start_all)
        self._stop_selected_btn = QPushButton("停止勾选")
        self._stop_selected_btn.setObjectName("dangerButton")
        self._stop_selected_btn.clicked.connect(self._stop_selected_tasks)
        self._clear_done_btn = QPushButton("清空已完成")
        self._clear_done_btn.clicked.connect(self._clear_done)
        self._refresh_btn = QPushButton("手动扫描")
        self._refresh_btn.clicked.connect(self._poll_ports)
        dev_buttons.addWidget(self._start_all_btn)
        dev_buttons.addWidget(self._stop_selected_btn)
        dev_buttons.addWidget(self._clear_done_btn)
        dev_buttons.addStretch(1)
        dev_buttons.addWidget(self._refresh_btn)

        dev_layout.addLayout(dev_header)
        dev_layout.addLayout(dev_stats)
        dev_layout.addWidget(self._dev_table, 1)
        dev_layout.addLayout(dev_buttons)

        log_frame = QFrame()
        log_frame.setObjectName("sectionFrame")
        log_layout = QVBoxLayout(log_frame)
        log_layout.setContentsMargins(12, 8, 12, 8)
        log_layout.setSpacing(4)

        log_header = QHBoxLayout()
        log_header.setSpacing(8)
        log_title = QLabel("检验日志")
        log_title.setObjectName("sectionTitle")
        log_hint = QLabel("日志下移到设备队列下方，为左侧检验脚本留出更多空间。")
        log_hint.setObjectName("statusLabel")
        self._clear_log_btn = QPushButton("清空")
        self._clear_log_btn.clicked.connect(lambda: self._log.clear())
        log_header.addWidget(log_title)
        log_header.addWidget(log_hint, 1)
        log_header.addWidget(self._clear_log_btn)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(4000)
        self._log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._log.setFont(_mono_font)
        self._log.setMinimumHeight(220)

        log_layout.addLayout(log_header)
        log_layout.addWidget(self._log, 1)

        self._right_splitter.addWidget(dev_frame)
        self._right_splitter.addWidget(log_frame)
        self._right_splitter.setStretchFactor(0, 3)
        self._right_splitter.setStretchFactor(1, 2)

        self._main_splitter.addWidget(plan_frame)
        self._main_splitter.addWidget(self._right_splitter)
        self._main_splitter.setStretchFactor(0, 7)
        self._main_splitter.setStretchFactor(1, 5)

        root.addWidget(self._main_splitter, 1)

    def _apply_style(self) -> None:
        _assets = Path(__file__).parent / "assets"
        _up_svg = (_assets / "spin_up.svg").as_posix()
        _down_svg = (_assets / "spin_down.svg").as_posix()
        _spinbox_arrow_css = (
            "QSpinBox::up-arrow { image: url(" + _up_svg + "); width: 8px; height: 6px; }\n"
            "QSpinBox::down-arrow { image: url(" + _down_svg + "); width: 8px; height: 6px; }\n"
        )
        self.setStyleSheet(
            BASE_STYLESHEET
            + """
            QFrame#sectionFrame {
                background: #ffffff;
                border: 1px solid #e0e4ea;
                border-radius: 12px;
            }
            QLabel#sectionTitle { font-size: 13px; font-weight: 700; }
            QLabel#countBadge {
                background: #e8edf7;
                border: 1px solid #c5cfe8;
                border-radius: 4px;
                padding: 3px 10px;
                color: #2560e0;
                font-weight: 600;
                font-size: 12px;
            }
            QLabel#summaryPill {
                border-radius: 999px;
                padding: 4px 12px;
                border: 1px solid #d9e2ec;
                background: #f8fafc;
                color: #334155;
                font-size: 11px;
                font-weight: 600;
            }
            QLabel#summaryPill[accent="primary"] {
                background: #eff6ff;
                border-color: #bfdbfe;
                color: #1d4ed8;
            }
            QLabel#summaryPill[accent="success"] {
                background: #ecfdf5;
                border-color: #a7f3d0;
                color: #047857;
            }
            QLabel#summaryPill[accent="warning"] {
                background: #fff7ed;
                border-color: #fdba74;
                color: #c2410c;
            }
            QLabel#summaryPill[accent="danger"] {
                background: #fff1f2;
                border-color: #fda4af;
                color: #be123c;
            }
            QTableWidget {
                background: #ffffff;
                border: 1px solid #e8edf3;
                border-radius: 8px;
                gridline-color: #eef2f7;
                font-size: 12px;
                alternate-background-color: #f8f9fb;
                selection-background-color: #dbeafe;
                selection-color: #1d4ed8;
            }
            QTableWidget::item { padding: 4px 6px; }
            QHeaderView::section {
                background: #f0f3f9;
                border: none;
                border-bottom: 1px solid #dde1ea;
                font-weight: 700;
                font-size: 11px;
                color: #6b7a94;
                padding: 6px 8px;
            }
            QPlainTextEdit, QLineEdit, QComboBox {
                background: #ffffff;
                border: 1px solid #d8e0ea;
                border-radius: 8px;
                color: #1e293b;
                min-height: 24px;
            }
            QSpinBox {
                background: #ffffff;
                border: 1px solid #d8e0ea;
                border-radius: 4px;
                color: #1e293b;
                min-height: 24px;
                padding: 2px 22px 2px 6px;
            }
            QSpinBox::up-button {
                subcontrol-origin: border;
                subcontrol-position: top right;
                width: 20px;
                border-left: 1px solid #d8e0ea;
                border-top-right-radius: 4px;
                background: #f0f3f9;
            }
            QSpinBox::up-button:hover { background: #dbeafe; }
            QSpinBox::up-button:pressed { background: #bfdbfe; }
            QSpinBox::down-button {
                subcontrol-origin: border;
                subcontrol-position: bottom right;
                width: 20px;
                border-left: 1px solid #d8e0ea;
                border-top: 1px solid #d8e0ea;
                border-bottom-right-radius: 4px;
                background: #f0f3f9;
            }
            QSpinBox::down-button:hover { background: #dbeafe; }
            QSpinBox::down-button:pressed { background: #bfdbfe; }
            """
            + _spinbox_arrow_css
            + """
            QPlainTextEdit {
                padding: 8px 10px;
                font-size: 11px;
            }
            QPlainTextEdit#verifyPlanEdit {
                background: #fbfcfe;
            }
            QLineEdit, QComboBox {
                padding: 2px 6px;
            }
            QLineEdit#stepInlineEdit {
                min-height: 20px;
                padding: 3px 8px;
            }
            QComboBox::drop-down {
                border: none;
                width: 22px;
            }
            QSplitter::handle {
                background: #eef2f7;
                border-radius: 4px;
                margin: 2px;
            }
            QTabWidget::pane {
                border: 1px solid #dde1ea;
                border-radius: 10px;
                background: #ffffff;
                top: -1px;
            }
            QTabBar::tab {
                background: #eef2f7;
                border: 1px solid #dbe2ea;
                border-bottom: none;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                padding: 7px 16px;
                margin-right: 4px;
                color: #5b677a;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                color: #1d4ed8;
            }
            QFrame#verifyEditorFrame {
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 12px;
            }
            QFrame#verifyStepBlock {
                border-radius: 14px;
                border: 1px solid #dbe2ea;
                background: #ffffff;
            }
            QFrame#verifyStepBlock[blockType="reset"] {
                background: #eff6ff;
                border-color: #93c5fd;
            }
            QFrame#verifyStepBlock[blockType="wait"] {
                background: #fff7ed;
                border-color: #fdba74;
            }
            QFrame#verifyStepBlock[blockType="send_text"] {
                background: #f5f3ff;
                border-color: #c4b5fd;
            }
            QFrame#verifyStepBlock[blockType="expect"] {
                background: #ecfeff;
                border-color: #67e8f9;
            }
            QFrame#verifyStepBlock[blockType="capture"] {
                background: #ecfdf5;
                border-color: #86efac;
            }
            QFrame#verifyStepBlock[blockType="set_result"] {
                background: #fff1f2;
                border-color: #fda4af;
            }
            QFrame#verifyStepBlock[blockType="clear_buffer"] {
                background: #f8fafc;
                border-color: #cbd5e1;
            }
            QLabel#stepBadge {
                background: #1d4ed8;
                border-radius: 6px;
                color: #ffffff;
                font-size: 10px;
                font-weight: 700;
                padding: 3px 8px;
            }
            QPushButton#stepMiniButton,
            QPushButton#stepAddButton,
            QPushButton#stepRemoveButton,
            QPushButton#stepFoldButton {
                border-radius: 8px;
                padding: 4px 10px;
                font-size: 11px;
                min-height: 24px;
            }
            QPushButton#stepFoldButton {
                background: rgba(255,255,255,0.92);
                border: 1px solid #cbd5e1;
                color: #334155;
                padding: 0;
                font-size: 14px;
                font-weight: 700;
            }
            QPushButton#stepAddButton {
                background: #eff6ff;
                border: 1px solid #bfdbfe;
                color: #1d4ed8;
            }
            QPushButton#stepAddButton:hover { background: #dbeafe; }
            QPushButton#stepMiniButton {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                color: #334155;
            }
            QPushButton#stepMiniButton:hover,
            QPushButton#stepFoldButton:hover { background: #f8fafc; }
            QPushButton#stepRemoveButton {
                background: #fff1f2;
                border: 1px solid #fecdd3;
                color: #be123c;
            }
            QPushButton#stepRemoveButton:hover { background: #ffe4e6; }
            QScrollArea {
                border: none;
                background: transparent;
            }
        """
        )

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._splitter_sizes_initialized:
            return
        total_width = max(self.width(), 1320)
        total_height = max(self.height(), 760)
        self._main_splitter.setSizes([int(total_width * 0.58), int(total_width * 0.42)])
        self._right_splitter.setSizes([int(total_height * 0.56), int(total_height * 0.44)])
        self._splitter_sizes_initialized = True

    def _load_profiles_from_default_sources(self) -> None:
        config_path = TOOL_DIR / "config.yaml"
        if config_path.is_file():
            try:
                self._profiles = load_verify_profiles_from_text(
                    config_path.read_text(encoding="utf-8")
                )
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"读取默认检验配置失败，已回退示例：{exc}")
        if not self._profiles:
            default_profile = build_default_profile()
            self._profiles = {default_profile.name: default_profile}
        self._refresh_profile_combo(select_name=next(iter(self._profiles)))
        self._load_selected_profile()

    def _update_profile_summary(self, profile: VerifyProfile | None) -> None:
        if profile is None:
            self._profile_name_pill.setText("配置：-")
            self._profile_steps_pill.setText("步骤 0")
            self._profile_capture_pill.setText("读取 0")
            self._profile_advanced_pill.setText("高级 0")
            return
        capture_count = sum(step.action == "capture" for step in profile.steps)
        advanced_count = sum(
            step.action in {"wait_silence", "pass", "fail"} or step.retry_count > 0
            for step in profile.steps
        )
        self._profile_name_pill.setText(f"配置：{profile.name}")
        self._profile_steps_pill.setText(f"步骤 {len(profile.steps)}")
        self._profile_capture_pill.setText(f"读取 {capture_count}")
        self._profile_advanced_pill.setText(f"高级 {advanced_count}")

    def _update_task_summary(self) -> None:
        total = len(self._tasks)
        selected = sum(task.selected for task in self._tasks)
        running = sum(task.state == VerifyTaskState.RUNNING for task in self._tasks)
        passed = sum(task.state == VerifyTaskState.PASSED for task in self._tasks)
        failed = sum(task.state == VerifyTaskState.FAILED for task in self._tasks)
        self._task_total_pill.setText(f"总数 {total}")
        self._task_selected_pill.setText(f"勾选 {selected}")
        self._task_running_pill.setText(f"执行中 {running}")
        self._task_passed_pill.setText(f"通过 {passed}")
        self._task_failed_pill.setText(f"失败 {failed}")

    def _refresh_profile_combo(self, select_name: str | None = None) -> None:
        current_text = select_name or self._profile_combo.currentText().strip()
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for name in self._profiles:
            self._profile_combo.addItem(name)
        self._profile_combo.blockSignals(False)
        if current_text:
            index = self._profile_combo.findText(current_text)
            if index >= 0:
                self._profile_combo.setCurrentIndex(index)
            else:
                self._profile_combo.setEditText(current_text)

    def _load_selected_profile(self) -> None:
        profile_name = self._profile_combo.currentText().strip() or DEFAULT_VERIFY_PROFILE_NAME
        profile = self._profiles.get(profile_name)
        if profile is None:
            QMessageBox.warning(self, "提示", f"未找到配置：{profile_name}")
            return
        self._set_profile_to_editors(profile)

    def _import_profiles(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择检验脚本 YAML",
            str(TOOL_DIR),
            "YAML 文件 (*.yaml *.yml);;All Files (*)",
        )
        if not path:
            return
        try:
            profiles = load_verify_profiles_from_text(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        self._profiles.update(profiles)
        first_name = next(iter(profiles))
        self._refresh_profile_combo(select_name=first_name)
        self._load_selected_profile()
        self._append_log(f"已导入 {len(profiles)} 个检验配置：{path}")

    def _export_current_profile(self) -> None:
        profile = self._validate_current_profile()
        if profile is None:
            return
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出检验脚本",
            str(TOOL_DIR / f"{profile.name}.yaml"),
            "YAML 文件 (*.yaml *.yml)",
        )
        if not save_path:
            return
        import yaml

        data = {"verify_profiles": {profile.name: profile_to_dict(profile)}}
        Path(save_path).write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        self._append_log(f"已导出检验配置：{save_path}")

    def _new_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "新建检验配置", "请输入配置名称：", text="新配置")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self._profiles:
            QMessageBox.warning(self, "提示", f'配置\u201c{name}\u201d已存在，请使用其他名称。')
            return
        profile = VerifyProfile(
            name=name,
            steps=[VerifyStep(action="reset", label="复位设备")],
        )
        self._profiles[name] = profile
        self._refresh_profile_combo(select_name=name)
        self._set_profile_to_editors(profile)
        self._append_log(f"已新建检验配置：{name}")

    def _format_yaml(self) -> None:
        import yaml

        text = self._plan_edit.toPlainText().strip()
        if not text:
            return
        try:
            data = yaml.safe_load(text)
            formatted = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
            self._plan_edit.setPlainText(formatted)
            self._append_log("YAML 已格式化")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "格式化失败", f"YAML 解析错误：\n{exc}")

    def _validate_current_profile_with_feedback(self) -> None:
        profile = self._validate_current_profile(show_message=True)
        if profile is None:
            return
        self._append_log(
            f"脚本校验通过：{profile.name}，共 {len(profile.steps)} 个步骤"
        )

    def _validate_current_profile(self, show_message: bool = False) -> VerifyProfile | None:
        profile_name = self._profile_combo.currentText().strip() or DEFAULT_VERIFY_PROFILE_NAME
        try:
            if self._editor_tabs.currentIndex() == 0:
                profile = self._visual_editor.build_profile(profile_name)
                self._set_yaml_from_profile(profile)
            else:
                text = self._plan_edit.toPlainText().strip()
                if not text:
                    QMessageBox.warning(self, "提示", "当前检验脚本为空。")
                    return None
                profile = load_single_profile_from_text(profile_name, text)
                self._visual_editor.load_profile(profile)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"脚本校验失败：{exc}")
            if show_message:
                QMessageBox.warning(self, "脚本错误", str(exc))
            return None
        self._profiles[profile.name] = profile
        self._refresh_profile_combo(select_name=profile.name)
        self._update_profile_summary(profile)
        if show_message:
            QMessageBox.information(
                self,
                "脚本通过",
                f"配置“{profile.name}”校验通过，共 {len(profile.steps)} 个步骤。",
            )
        return profile

    def _set_profile_to_editors(self, profile: VerifyProfile) -> None:
        self._updating_editor_views = True
        try:
            self._visual_editor.load_profile(profile)
            self._plan_edit.setPlainText(profile_to_yaml_text(profile))
        finally:
            self._updating_editor_views = False
        self._update_profile_summary(profile)

    def _set_yaml_from_profile(self, profile: VerifyProfile) -> None:
        self._updating_editor_views = True
        try:
            self._plan_edit.setPlainText(profile_to_yaml_text(profile))
        finally:
            self._updating_editor_views = False

    def _on_editor_tab_changed(self, index: int) -> None:
        if self._updating_editor_views:
            return
        if index == 1:
            try:
                profile = self._visual_editor.build_profile(
                    self._profile_combo.currentText().strip() or DEFAULT_VERIFY_PROFILE_NAME
                )
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"切换到 YAML 前校验失败：{exc}")
                QMessageBox.warning(self, "步骤配置有误", str(exc))
                self._editor_tabs.blockSignals(True)
                self._editor_tabs.setCurrentIndex(0)
                self._editor_tabs.blockSignals(False)
                return
            self._set_yaml_from_profile(profile)
            return

        text = self._plan_edit.toPlainText().strip()
        if not text:
            return
        try:
            profile = load_single_profile_from_text(
                self._profile_combo.currentText().strip() or DEFAULT_VERIFY_PROFILE_NAME,
                text,
            )
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"YAML 同步到可视化失败：{exc}")
            QMessageBox.warning(self, "YAML 有误", f"无法同步到可视化界面：\n{exc}")
            self._editor_tabs.blockSignals(True)
            self._editor_tabs.setCurrentIndex(1)
            self._editor_tabs.blockSignals(False)
            return
        self._updating_editor_views = True
        try:
            self._visual_editor.load_profile(profile)
        finally:
            self._updating_editor_views = False

    def _toggle_auto_mode(self, enabled: bool) -> None:
        self._auto_mode = enabled
        self._auto_verify_btn.setText(f"自动校验：{'开' if enabled else '关'}")
        if enabled:
            self._poll_timer.start()
            self._poll_ports()
        else:
            self._poll_timer.stop()
            self._run_all_requested = False
            self._batch_profile = None
            self._batch_ports.clear()

    def _poll_ports(self) -> None:
        current_ports: dict[str, str] = {}
        for port_info in list_ports.comports():
            if port_info.description and re.search(
                r"通信端口|Communications Port", port_info.description
            ):
                continue
            current_ports[port_info.device] = (
                port_info.description if port_info.description and port_info.description != "n/a" else "串口设备"
            )

        known_ports = {task.port for task in self._tasks}
        for port in sorted(current_ports.keys() - known_ports, key=self._port_sort_key):
            task = VerifyTaskItem(device_id=port, port=port, label=current_ports[port], selected=True)
            self._tasks.append(task)
            self._append_log(f"{port} 接入（{task.label}）")

        for task in self._tasks:
            if task.port in current_ports:
                task.label = current_ports[task.port]

        removed_ports = known_ports - current_ports.keys()
        for port in sorted(removed_ports, key=self._port_sort_key):
            task = self._find_task(port)
            if task is None:
                continue
            if task.state == VerifyTaskState.RUNNING and task.worker is not None:
                task.error_message = "设备已断开，等待任务退出"
                task.worker.request_stop()
                self._append_log(f"{port} 已断开（任务中）")
            else:
                self._tasks.remove(task)
                self._append_log(f"{port} 已断开，已移除")
                self._batch_ports.discard(port)

        self._refresh_dev_table()
        if self._auto_mode:
            self._schedule_next()

    def _find_task(self, port: str) -> VerifyTaskItem | None:
        for task in self._tasks:
            if task.port == port:
                return task
        return None

    def _refresh_dev_table(self) -> None:
        self._tasks.sort(key=lambda item: self._port_sort_key(item.port))
        table = self._dev_table
        self._table_refreshing = True
        table.blockSignals(True)
        table.setRowCount(len(self._tasks))
        state_colors = {
            VerifyTaskState.WAITING: "#6b7a94",
            VerifyTaskState.RUNNING: "#b45309",
            VerifyTaskState.PASSED: "#065f46",
            VerifyTaskState.FAILED: "#991b1b",
            VerifyTaskState.STOPPED: "#6b7a94",
        }
        state_backgrounds = {
            VerifyTaskState.WAITING: QColor("#f8fafc"),
            VerifyTaskState.RUNNING: QColor("#fff7ed"),
            VerifyTaskState.PASSED: QColor("#f0fdf4"),
            VerifyTaskState.FAILED: QColor("#fff1f2"),
            VerifyTaskState.STOPPED: QColor("#f8fafc"),
        }
        state_icons = {
            VerifyTaskState.WAITING: "○",
            VerifyTaskState.RUNNING: "⏳",
            VerifyTaskState.PASSED: "✓",
            VerifyTaskState.FAILED: "✗",
            VerifyTaskState.STOPPED: "■",
        }
        selected_count = 0
        for row, task in enumerate(self._tasks):
            if task.selected:
                selected_count += 1
            row_items: list[QTableWidgetItem] = []

            select_item = QTableWidgetItem()
            select_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            select_item.setCheckState(Qt.CheckState.Checked if task.selected else Qt.CheckState.Unchecked)
            select_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            table.setItem(row, 0, select_item)
            row_items.append(select_item)

            port_item = QTableWidgetItem(task.port)
            port_item.setToolTip(task.port)
            table.setItem(row, 1, port_item)
            row_items.append(port_item)

            label_item = QTableWidgetItem(task.label)
            label_item.setToolTip(task.label)
            table.setItem(row, 2, label_item)
            row_items.append(label_item)

            state_item = QTableWidgetItem(
                f"{state_icons[task.state]} {task.state.value}"
            )
            state_item.setForeground(QColor(state_colors[task.state]))
            bold_font = QFont()
            bold_font.setBold(True)
            state_item.setFont(bold_font)
            table.setItem(row, 3, state_item)
            row_items.append(state_item)

            step_item = QTableWidgetItem(task.current_step or "-")
            step_item.setToolTip(task.current_step or "-")
            table.setItem(row, 4, step_item)
            row_items.append(step_item)

            if task.state == VerifyTaskState.FAILED:
                result_text = task.error_message or task.result_summary or self._format_captured_values(task.captured_values)
            else:
                result_text = task.result_summary or task.error_message or self._format_captured_values(task.captured_values)
            result_item = QTableWidgetItem(result_text)
            result_item.setToolTip(result_text)
            table.setItem(row, 5, result_item)
            row_items.append(result_item)

            row_background = state_backgrounds[task.state]
            for item in row_items:
                item.setBackground(row_background)

        table.blockSignals(False)
        self._table_refreshing = False
        self._dev_count_label.setText(f"{len(self._tasks)} 台 / 勾选 {selected_count} 台")
        self._update_task_summary()

    def _set_all_selected(self, selected: bool) -> None:
        for task in self._tasks:
            task.selected = selected
        self._refresh_dev_table()

    def _on_device_item_changed(self, item: QTableWidgetItem) -> None:
        if self._table_refreshing or item.column() != 0:
            return
        row = item.row()
        if row < 0 or row >= len(self._tasks):
            return
        self._tasks[row].selected = item.checkState() == Qt.CheckState.Checked
        self._refresh_dev_table()

    def _on_device_double_clicked(self, row: int, column: int) -> None:
        if column == 0 or row < 0 or row >= len(self._tasks):
            return
        task = self._tasks[row]
        if task.state == VerifyTaskState.RUNNING:
            self._abort_task(task)
        else:
            self._start_task(task)

    def _format_captured_values(self, values: dict[str, str]) -> str:
        if not values:
            return ""
        items = list(values.items())
        parts = [f"{key}={value}" for key, value in items[:4]]
        if len(items) > 4:
            parts.append("…")
        return " / ".join(parts)

    def _start_task(self, task: VerifyTaskItem, profile: VerifyProfile | None = None) -> None:
        if task.worker is not None:
            return
        active_profile = profile or self._validate_current_profile(show_message=False)
        if active_profile is None:
            return
        if task.state == VerifyTaskState.RUNNING:
            return
        task.state = VerifyTaskState.RUNNING
        task.current_step = "准备启动"
        task.result_summary = ""
        task.error_message = ""
        task.captured_values.clear()
        worker = VerifyWorker(task.port, active_profile)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.log_message.connect(lambda msg, port=task.port: self._append_log(f"{port} {msg}"))
        worker.step_changed.connect(lambda step, t=task: self._on_task_step_changed(t, step))
        worker.result_changed.connect(lambda text, values, t=task: self._on_task_result_changed(t, text, values))
        worker.finished.connect(lambda outcome, summary, t=task: self._on_task_finished(t, outcome, summary))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        task.worker = worker
        task.thread = thread
        self._refresh_dev_table()
        thread.start()

    def _start_all(self) -> None:
        profile = self._validate_current_profile(show_message=False)
        if profile is None:
            return
        if not self._tasks:
            QMessageBox.information(self, "提示", "设备队列为空，请先扫描或接入设备。")
            return
        selected_ports = {task.port for task in self._tasks if task.selected}
        if not selected_ports:
            QMessageBox.information(self, "提示", "请先勾选至少一台设备。")
            return
        for task in self._tasks:
            if task.selected and task.worker is None and task.state != VerifyTaskState.RUNNING:
                task.state = VerifyTaskState.WAITING
                task.current_step = "-"
        self._run_all_requested = True
        self._batch_profile = profile
        self._batch_ports = selected_ports
        self._refresh_dev_table()
        self._schedule_next()

    def _stop_selected_tasks(self) -> None:
        selected_ports = {task.port for task in self._tasks if task.selected}
        if not selected_ports:
            return
        self._batch_ports -= selected_ports
        if not self._batch_ports:
            self._run_all_requested = False
            self._batch_profile = None
        for task in self._tasks:
            if task.selected and task.worker is not None:
                self._abort_task(task)

    def _schedule_next(self) -> None:
        running = sum(task.state == VerifyTaskState.RUNNING for task in self._tasks)
        if running >= self.MAX_CONCURRENT:
            return

        if self._run_all_requested and self._batch_profile is not None:
            profile = self._batch_profile
            for task in self._tasks:
                if running >= self.MAX_CONCURRENT:
                    break
                if (
                    task.port in self._batch_ports
                    and task.state == VerifyTaskState.WAITING
                    and task.worker is None
                ):
                    self._start_task(task, profile=profile)
                    running += 1
            if not any(
                task.port in self._batch_ports and task.state == VerifyTaskState.WAITING
                for task in self._tasks
            ):
                self._run_all_requested = False
                self._batch_profile = None
                self._batch_ports.clear()
            return

        if self._auto_mode:
            profile = self._validate_current_profile(show_message=False)
            if profile is None:
                return
            for task in self._tasks:
                if running >= self.MAX_CONCURRENT:
                    break
                if task.selected and task.state == VerifyTaskState.WAITING and task.worker is None:
                    self._start_task(task, profile=profile)
                    running += 1

    def _on_task_step_changed(self, task: VerifyTaskItem, step_text: str) -> None:
        task.current_step = step_text
        self._refresh_dev_table()

    def _on_task_result_changed(
        self,
        task: VerifyTaskItem,
        display_text: str,
        captured_values: object,
    ) -> None:
        task.result_summary = display_text
        task.captured_values = dict(captured_values or {})
        self._refresh_dev_table()

    def _on_task_finished(self, task: VerifyTaskItem, outcome: str, summary: str) -> None:
        task.worker = None
        task.thread = None
        task.current_step = "-"
        if outcome == "passed":
            task.state = VerifyTaskState.PASSED
            if not task.result_summary:
                task.result_summary = summary or self._format_captured_values(task.captured_values)
            task.error_message = ""
            self._append_log(f"{task.port} 校验通过 ✓")
        elif outcome == "stopped":
            task.state = VerifyTaskState.STOPPED
            task.error_message = ""
            if not task.result_summary:
                task.result_summary = summary
            self._append_log(f"{task.port} 已停止")
        else:
            task.state = VerifyTaskState.FAILED
            task.error_message = summary
            self._append_log(f"{task.port} 校验失败：{summary}")
        self._refresh_dev_table()
        if self._auto_mode or self._run_all_requested:
            QTimer.singleShot(0, self._schedule_next)

    def _abort_task(self, task: VerifyTaskItem) -> None:
        if task.worker is None:
            return
        task.current_step = "停止中"
        task.worker.request_stop()
        self._append_log(f"{task.port} 收到停止请求")
        self._refresh_dev_table()

    def _clear_done(self) -> None:
        self._tasks = [
            task for task in self._tasks if task.state in {VerifyTaskState.WAITING, VerifyTaskState.RUNNING}
        ]
        self._refresh_dev_table()

    def stop_all_tasks(self) -> None:
        self._run_all_requested = False
        self._batch_profile = None
        self._batch_ports.clear()
        self._auto_mode = False
        self._poll_timer.stop()
        self._auto_verify_btn.blockSignals(True)
        self._auto_verify_btn.setChecked(False)
        self._auto_verify_btn.setText("自动校验：关")
        self._auto_verify_btn.blockSignals(False)
        for task in self._tasks:
            if task.worker is not None:
                task.current_step = "停止中"
                task.worker.request_stop()
        self._refresh_dev_table()

    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._log.appendPlainText(f"[{timestamp}] {message}")
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._log.setTextCursor(cursor)

    @staticmethod
    def _port_sort_key(port: str) -> tuple[int, str]:
        match = re.search(r"(\d+)$", port)
        if match:
            return (int(match.group(1)), port)
        return (10**9, port)
