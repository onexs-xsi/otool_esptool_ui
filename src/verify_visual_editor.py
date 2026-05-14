from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .verify_plan import (
    VerifyPattern,
    VerifyProfile,
    VerifySerialConfig,
    VerifyStep,
    describe_step,
    parse_verify_profile,
    profile_to_dict,
)


class _DownComboBox(QComboBox):
    """始终向下弹出的 QComboBox，解决嵌套 ScrollArea 内弹出位置漂移问题。"""

    def showPopup(self) -> None:
        super().showPopup()
        container = self.view().parentWidget()
        if container is not None:
            pos = self.mapToGlobal(self.rect().bottomLeft())
            container.move(pos)


_BAUDRATE_PRESETS = [
    "9600", "19200", "38400", "57600", "115200",
    "230400", "460800", "921600", "1500000", "2000000",
]


_ACTION_LABELS = {
    "reset": "复位",
    "wait": "等待",
    "wait_silence": "等静默",
    "send_text": "发送文本",
    "expect": "判断匹配",
    "capture": "读取参数",
    "set_result": "设置结果",
    "pass": "主动通过",
    "fail": "主动失败",
    "clear_buffer": "清空缓冲",
}

_RESET_METHOD_OPTIONS = [
    ("串口 RTS/DTR 复位", "serial_toggle"),
    ("esptool run 复位", "esptool_run"),
    ("跳过复位", "none"),
]

_MATCH_MODE_OPTIONS = [
    ("全部匹配", "all"),
    ("任意一个", "any"),
    ("都不能出现", "none"),
]

_MATCH_TYPE_OPTIONS = [
    ("包含文本", "contains"),
    ("正则表达式", "regex"),
]

_STOP_BITS_OPTIONS = ["1", "1.5", "2"]
_PARITY_OPTIONS = ["N", "E", "O", "M", "S"]
_BYTESIZE_OPTIONS = ["5", "6", "7", "8"]


def _escape_control_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def _unescape_control_text(text: str) -> str:
    result = text.replace("\\r", "\r").replace("\\n", "\n").replace("\\t", "\t")
    return result.replace("\\\\", "\\")


def _find_combo_index_by_value(combo: QComboBox, value: str) -> int:
    for idx in range(combo.count()):
        if combo.itemData(idx) == value:
            return idx
    return -1


def _default_step_for_action(action: str) -> VerifyStep:
    if action == "reset":
        return VerifyStep(action="reset", method="serial_toggle")
    if action == "wait":
        return VerifyStep(action="wait", duration_ms=300)
    if action == "wait_silence":
        return VerifyStep(action="wait_silence", duration_ms=400, timeout_ms=3000)
    if action == "send_text":
        return VerifyStep(action="send_text", append_newline=True)
    if action == "capture":
        return VerifyStep(
            action="capture",
            timeout_ms=3000,
            match_mode="all",
            match_type="regex",
            patterns=[
                VerifyPattern(
                    pattern=r"version[:= ]+([^\r\n]+)",
                    name="version",
                    description="读取版本号",
                )
            ],
        )
    if action == "set_result":
        return VerifyStep(action="set_result", text="版本={{version}}")
    if action == "pass":
        return VerifyStep(action="pass", text="脚本主动通过")
    if action == "fail":
        return VerifyStep(action="fail", text="脚本主动判定失败")
    if action == "clear_buffer":
        return VerifyStep(action="clear_buffer")
    return VerifyStep(
        action="expect",
        timeout_ms=3000,
        match_mode="all",
        match_type="contains",
        patterns=[VerifyPattern("boot:")],
    )


class PatternRowWidget(QWidget):
    remove_requested = pyqtSignal(object)

    def __init__(
        self,
        pattern: VerifyPattern | None = None,
        mode: str = "expect",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._mode = mode
        pattern = pattern or VerifyPattern(pattern="")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.name_edit = QLineEdit(pattern.name)
        self.name_edit.setPlaceholderText("参数名")
        self.name_edit.setObjectName("stepInlineEdit")

        self.pattern_edit = QLineEdit(pattern.pattern)
        self.pattern_edit.setPlaceholderText("匹配字段或正则")
        self.pattern_edit.setObjectName("stepInlineEdit")

        self.group_spin = QSpinBox()
        self.group_spin.setRange(0, 20)
        self.group_spin.setValue(pattern.group)
        self.group_spin.setToolTip("仅 regex 模式下使用：0=整体匹配，1..n=捕获组")

        self.description_edit = QLineEdit(pattern.description)
        self.description_edit.setPlaceholderText("说明（可选）")
        self.description_edit.setObjectName("stepInlineEdit")

        self.remove_btn = QPushButton("删除")
        self.remove_btn.setObjectName("stepMiniButton")
        self.remove_btn.clicked.connect(lambda: self.remove_requested.emit(self))

        layout.addWidget(self.name_edit, 1)
        layout.addWidget(self.pattern_edit, 3)
        layout.addWidget(self.group_spin)
        layout.addWidget(self.description_edit, 2)
        layout.addWidget(self.remove_btn)

        self.set_mode(mode)

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        is_capture = mode == "capture"
        self.name_edit.setVisible(is_capture)
        self.group_spin.setVisible(is_capture)

    def to_pattern(self) -> VerifyPattern:
        return VerifyPattern(
            pattern=self.pattern_edit.text().strip(),
            description=self.description_edit.text().strip(),
            name=self.name_edit.text().strip(),
            group=self.group_spin.value(),
        )


class PatternListEditor(QWidget):
    def __init__(self, mode: str = "expect", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[PatternRowWidget] = []
        self._mode = mode

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self._title = QLabel()
        self._title.setObjectName("metaKey")
        self._hint = QLabel()
        self._hint.setObjectName("statusLabel")
        self._hint.setWordWrap(True)
        self.add_btn = QPushButton("＋ 添加字段")
        self.add_btn.setObjectName("stepMiniButton")
        self.add_btn.clicked.connect(lambda: self.add_pattern())
        header.addWidget(self._title)
        header.addWidget(self._hint, 1)
        header.addWidget(self.add_btn)

        self._rows_container = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        self._rows_layout.addStretch(1)

        self._empty_hint = QLabel()
        self._empty_hint.setObjectName("statusLabel")
        self._empty_hint.setWordWrap(True)

        root.addLayout(header)
        root.addWidget(self._empty_hint)
        root.addWidget(self._rows_container)

        self.set_mode(mode)
        self._sync_empty_state()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        capture_mode = mode == "capture"
        self._title.setText("读取规则" if capture_mode else "匹配字段")
        self._hint.setText(
            "为每条规则填写参数名，命中后会写入设备结果上下文。"
            if capture_mode
            else "可配置多个判断字段，支持 contains / regex。"
        )
        self._empty_hint.setText(
            "暂无读取规则，建议至少读取一个版本号 / 序列号 / 结果字段。"
            if capture_mode
            else "暂无匹配字段，建议至少添加一个判断项。"
        )
        for row in self._rows:
            row.set_mode(mode)

    def clear(self) -> None:
        for row in list(self._rows):
            self._remove_row(row)

    def set_patterns(self, patterns: list[VerifyPattern]) -> None:
        self.clear()
        for pattern in patterns:
            self.add_pattern(pattern)
        self._sync_empty_state()

    def add_pattern(self, pattern: VerifyPattern | None = None) -> None:
        row = PatternRowWidget(pattern=pattern, mode=self._mode, parent=self._rows_container)
        row.remove_requested.connect(self._remove_row)
        self._rows.append(row)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        self._sync_empty_state()

    def patterns(self) -> list[VerifyPattern]:
        return [row.to_pattern() for row in self._rows]

    def _remove_row(self, row: object) -> None:
        if not isinstance(row, PatternRowWidget):
            return
        if row not in self._rows:
            return
        self._rows.remove(row)
        self._rows_layout.removeWidget(row)
        row.deleteLater()
        self._sync_empty_state()

    def _sync_empty_state(self) -> None:
        empty = not self._rows
        self._empty_hint.setVisible(empty)
        self._rows_container.setVisible(not empty)


class VerifyStepBlock(QFrame):
    remove_requested = pyqtSignal(object)
    move_up_requested = pyqtSignal(object)
    move_down_requested = pyqtSignal(object)

    def __init__(self, step: VerifyStep | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("verifyStepBlock")
        self._collapsed = False
        self._step = step or _default_step_for_action("expect")

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        self._index_badge = QLabel("STEP 1")
        self._index_badge.setObjectName("stepBadge")

        self._collapse_btn = QPushButton("▾")
        self._collapse_btn.setObjectName("stepFoldButton")
        self._collapse_btn.setFixedWidth(30)
        self._collapse_btn.clicked.connect(self.toggle_collapsed)

        title_box = QVBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(2)
        self._title_label = QLabel()
        self._title_label.setObjectName("deviceTitle")
        self._summary_label = QLabel()
        self._summary_label.setObjectName("statusLabel")
        self._summary_label.setWordWrap(True)
        title_box.addWidget(self._title_label)
        title_box.addWidget(self._summary_label)

        header.addWidget(self._index_badge, 0, Qt.AlignmentFlag.AlignTop)
        header.addWidget(self._collapse_btn, 0, Qt.AlignmentFlag.AlignTop)
        header.addLayout(title_box, 1)

        self._move_up_btn = QPushButton("上移")
        self._move_up_btn.setObjectName("stepMiniButton")
        self._move_down_btn = QPushButton("下移")
        self._move_down_btn.setObjectName("stepMiniButton")
        self._remove_btn = QPushButton("删除")
        self._remove_btn.setObjectName("stepRemoveButton")
        self._move_up_btn.clicked.connect(lambda: self.move_up_requested.emit(self))
        self._move_down_btn.clicked.connect(lambda: self.move_down_requested.emit(self))
        self._remove_btn.clicked.connect(lambda: self.remove_requested.emit(self))
        header.addWidget(self._move_up_btn)
        header.addWidget(self._move_down_btn)
        header.addWidget(self._remove_btn)

        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)

        general_grid = QGridLayout()
        general_grid.setHorizontalSpacing(10)
        general_grid.setVerticalSpacing(8)

        action_label = QLabel("步骤类型")
        action_label.setObjectName("metaKey")
        self._action_combo = _DownComboBox()
        for action, label in _ACTION_LABELS.items():
            self._action_combo.addItem(label, action)

        title_label = QLabel("步骤标题")
        title_label.setObjectName("metaKey")
        self._label_edit = QLineEdit()
        self._label_edit.setPlaceholderText("例如：检查启动日志 / 发送版本命令")
        self._label_edit.setObjectName("stepInlineEdit")

        retry_label = QLabel("失败重试")
        retry_label.setObjectName("metaKey")
        self._retry_count_spin = QSpinBox()
        self._retry_count_spin.setRange(0, 20)
        self._retry_count_spin.setToolTip("0 表示不重试；1 表示失败后再试 1 次")

        retry_delay_label = QLabel("重试间隔(ms)")
        retry_delay_label.setObjectName("metaKey")
        self._retry_delay_spin = QSpinBox()
        self._retry_delay_spin.setRange(0, 60000)
        self._retry_delay_spin.setToolTip("步骤失败后等待多久再重试")

        general_grid.addWidget(action_label, 0, 0)
        general_grid.addWidget(self._action_combo, 0, 1)
        general_grid.addWidget(title_label, 0, 2)
        general_grid.addWidget(self._label_edit, 0, 3)
        general_grid.addWidget(retry_label, 1, 0)
        general_grid.addWidget(self._retry_count_spin, 1, 1)
        general_grid.addWidget(retry_delay_label, 1, 2)
        general_grid.addWidget(self._retry_delay_spin, 1, 3)

        self._detail_stack = QStackedWidgetCompat()
        self._reset_panel = self._build_reset_panel()
        self._wait_panel = self._build_wait_panel()
        self._wait_silence_panel = self._build_wait_silence_panel()
        self._send_panel = self._build_send_panel()
        self._expect_panel = self._build_expect_panel()
        self._capture_panel = self._build_capture_panel()
        self._result_panel = self._build_result_panel()
        self._pass_panel = self._build_message_panel(
            placeholder="例如：读取到版本={{version}}，设备可直接判定通过",
            hint="命中这里会立即结束后续步骤并标记通过；支持引用 capture 的参数。",
        )
        self._fail_panel = self._build_message_panel(
            placeholder="例如：设备输出自检失败，终止量产流程",
            hint="命中这里会立即终止脚本并标记失败；支持引用 capture 的参数。",
        )
        self._clear_panel = self._build_clear_panel()

        self._detail_stack.add_named_widget("reset", self._reset_panel)
        self._detail_stack.add_named_widget("wait", self._wait_panel)
        self._detail_stack.add_named_widget("wait_silence", self._wait_silence_panel)
        self._detail_stack.add_named_widget("send_text", self._send_panel)
        self._detail_stack.add_named_widget("expect", self._expect_panel)
        self._detail_stack.add_named_widget("capture", self._capture_panel)
        self._detail_stack.add_named_widget("set_result", self._result_panel)
        self._detail_stack.add_named_widget("pass", self._pass_panel)
        self._detail_stack.add_named_widget("fail", self._fail_panel)
        self._detail_stack.add_named_widget("clear_buffer", self._clear_panel)

        body_layout.addLayout(general_grid)
        body_layout.addWidget(self._detail_stack)

        root.addLayout(header)
        root.addWidget(self._body)

        self._action_combo.currentIndexChanged.connect(self._on_action_changed)
        self._label_edit.textChanged.connect(self._update_header_texts)
        self._connect_update_signals()

        self.load_step(self._step)

    def _connect_update_signals(self) -> None:
        self._reset_method_combo.currentIndexChanged.connect(self._update_header_texts)
        self._hold_spin.valueChanged.connect(self._update_header_texts)
        self._release_wait_spin.valueChanged.connect(self._update_header_texts)
        self._wait_duration_spin.valueChanged.connect(self._update_header_texts)
        self._retry_count_spin.valueChanged.connect(self._update_header_texts)
        self._retry_delay_spin.valueChanged.connect(self._update_header_texts)
        self._wait_silence_duration_spin.valueChanged.connect(self._update_header_texts)
        self._wait_silence_timeout_spin.valueChanged.connect(self._update_header_texts)
        self._send_text_edit.textChanged.connect(self._update_header_texts)
        self._append_newline_check.toggled.connect(self._update_header_texts)
        self._char_delay_spin.valueChanged.connect(self._update_header_texts)
        self._expect_timeout_spin.valueChanged.connect(self._update_header_texts)
        self._match_mode_combo.currentIndexChanged.connect(self._update_header_texts)
        self._match_type_combo.currentIndexChanged.connect(self._update_header_texts)
        self._case_sensitive_check.toggled.connect(self._update_header_texts)
        self._capture_timeout_spin.valueChanged.connect(self._update_header_texts)
        self._capture_match_mode_combo.currentIndexChanged.connect(self._update_header_texts)
        self._capture_match_type_combo.currentIndexChanged.connect(self._update_header_texts)
        self._capture_case_sensitive_check.toggled.connect(self._update_header_texts)
        self._result_template_edit.textChanged.connect(self._update_header_texts)
        self._pass_text_edit.textChanged.connect(self._update_header_texts)
        self._fail_text_edit.textChanged.connect(self._update_header_texts)

    def _build_reset_panel(self) -> QWidget:
        panel = QWidget()
        grid = QGridLayout(panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        method_label = QLabel("复位方式")
        method_label.setObjectName("metaKey")
        self._reset_method_combo = _DownComboBox()
        for text, value in _RESET_METHOD_OPTIONS:
            self._reset_method_combo.addItem(text, value)

        hold_label = QLabel("保持时长(ms)")
        hold_label.setObjectName("metaKey")
        self._hold_spin = QSpinBox()
        self._hold_spin.setRange(0, 60000)
        self._hold_spin.setValue(120)

        release_label = QLabel("释放后等待(ms)")
        release_label.setObjectName("metaKey")
        self._release_wait_spin = QSpinBox()
        self._release_wait_spin.setRange(0, 60000)
        self._release_wait_spin.setValue(200)

        grid.addWidget(method_label, 0, 0)
        grid.addWidget(self._reset_method_combo, 0, 1)
        grid.addWidget(hold_label, 0, 2)
        grid.addWidget(self._hold_spin, 0, 3)
        grid.addWidget(release_label, 1, 0)
        grid.addWidget(self._release_wait_spin, 1, 1)
        return panel

    def _build_wait_panel(self) -> QWidget:
        panel = QWidget()
        layout = QGridLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)
        duration_label = QLabel("等待时长(ms)")
        duration_label.setObjectName("metaKey")
        self._wait_duration_spin = QSpinBox()
        self._wait_duration_spin.setRange(1, 3600000)
        self._wait_duration_spin.setValue(300)
        layout.addWidget(duration_label, 0, 0)
        layout.addWidget(self._wait_duration_spin, 0, 1)
        return panel

    def _build_wait_silence_panel(self) -> QWidget:
        panel = QWidget()
        layout = QGridLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        duration_label = QLabel("静默时长(ms)")
        duration_label.setObjectName("metaKey")
        self._wait_silence_duration_spin = QSpinBox()
        self._wait_silence_duration_spin.setRange(1, 3600000)
        self._wait_silence_duration_spin.setValue(400)

        timeout_label = QLabel("最大等待(ms)")
        timeout_label.setObjectName("metaKey")
        self._wait_silence_timeout_spin = QSpinBox()
        self._wait_silence_timeout_spin.setRange(1, 3600000)
        self._wait_silence_timeout_spin.setValue(3000)

        hint = QLabel("适合等待设备日志喷完后再继续，例如等待 boot log 完整结束。")
        hint.setObjectName("statusLabel")
        hint.setWordWrap(True)

        layout.addWidget(duration_label, 0, 0)
        layout.addWidget(self._wait_silence_duration_spin, 0, 1)
        layout.addWidget(timeout_label, 0, 2)
        layout.addWidget(self._wait_silence_timeout_spin, 0, 3)
        layout.addWidget(hint, 1, 0, 1, 4)
        return panel

    def _build_send_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        head = QGridLayout()
        head.setHorizontalSpacing(10)
        head.setVerticalSpacing(8)
        char_delay_label = QLabel("逐字延时(ms)")
        char_delay_label.setObjectName("metaKey")
        self._char_delay_spin = QSpinBox()
        self._char_delay_spin.setRange(0, 5000)
        self._char_delay_spin.setValue(0)
        self._append_newline_check = QCheckBox("自动追加换行")
        self._append_newline_check.setChecked(True)
        head.addWidget(char_delay_label, 0, 0)
        head.addWidget(self._char_delay_spin, 0, 1)
        head.addWidget(self._append_newline_check, 0, 2, 1, 2)

        text_label = QLabel("发送内容")
        text_label.setObjectName("metaKey")
        self._send_text_edit = QPlainTextEdit()
        self._send_text_edit.setPlaceholderText("例如：version / help / AT+GMR")
        self._send_text_edit.setFixedHeight(76)

        layout.addLayout(head)
        layout.addWidget(text_label)
        layout.addWidget(self._send_text_edit)
        return panel

    def _build_expect_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QGridLayout()
        header.setHorizontalSpacing(10)
        header.setVerticalSpacing(8)

        timeout_label = QLabel("超时(ms)")
        timeout_label.setObjectName("metaKey")
        self._expect_timeout_spin = QSpinBox()
        self._expect_timeout_spin.setRange(1, 3600000)
        self._expect_timeout_spin.setValue(3000)

        match_mode_label = QLabel("判定模式")
        match_mode_label.setObjectName("metaKey")
        self._match_mode_combo = _DownComboBox()
        for text, value in _MATCH_MODE_OPTIONS:
            self._match_mode_combo.addItem(text, value)

        match_type_label = QLabel("匹配方式")
        match_type_label.setObjectName("metaKey")
        self._match_type_combo = _DownComboBox()
        for text, value in _MATCH_TYPE_OPTIONS:
            self._match_type_combo.addItem(text, value)

        self._case_sensitive_check = QCheckBox("区分大小写")

        header.addWidget(timeout_label, 0, 0)
        header.addWidget(self._expect_timeout_spin, 0, 1)
        header.addWidget(match_mode_label, 0, 2)
        header.addWidget(self._match_mode_combo, 0, 3)
        header.addWidget(match_type_label, 1, 0)
        header.addWidget(self._match_type_combo, 1, 1)
        header.addWidget(self._case_sensitive_check, 1, 2, 1, 2)

        self._pattern_editor = PatternListEditor(mode="expect")

        layout.addLayout(header)
        layout.addWidget(self._pattern_editor)
        return panel

    def _build_capture_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QGridLayout()
        header.setHorizontalSpacing(10)
        header.setVerticalSpacing(8)

        timeout_label = QLabel("超时(ms)")
        timeout_label.setObjectName("metaKey")
        self._capture_timeout_spin = QSpinBox()
        self._capture_timeout_spin.setRange(1, 3600000)
        self._capture_timeout_spin.setValue(3000)

        match_mode_label = QLabel("读取条件")
        match_mode_label.setObjectName("metaKey")
        self._capture_match_mode_combo = _DownComboBox()
        for text, value in _MATCH_MODE_OPTIONS[:2]:
            self._capture_match_mode_combo.addItem(text, value)

        match_type_label = QLabel("读取方式")
        match_type_label.setObjectName("metaKey")
        self._capture_match_type_combo = _DownComboBox()
        for text, value in _MATCH_TYPE_OPTIONS:
            self._capture_match_type_combo.addItem(text, value)

        self._capture_case_sensitive_check = QCheckBox("区分大小写")

        header.addWidget(timeout_label, 0, 0)
        header.addWidget(self._capture_timeout_spin, 0, 1)
        header.addWidget(match_mode_label, 0, 2)
        header.addWidget(self._capture_match_mode_combo, 0, 3)
        header.addWidget(match_type_label, 1, 0)
        header.addWidget(self._capture_match_type_combo, 1, 1)
        header.addWidget(self._capture_case_sensitive_check, 1, 2, 1, 2)

        hint = QLabel("适合读取版本号、SN、MAC、测量值或设备返回结果；建议优先使用 regex。")
        hint.setObjectName("statusLabel")
        hint.setWordWrap(True)

        self._capture_editor = PatternListEditor(mode="capture")

        layout.addLayout(header)
        layout.addWidget(hint)
        layout.addWidget(self._capture_editor)
        return panel

    def _build_result_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        hint = QLabel("支持使用 {{参数名}} 引用 capture 读取到的值，例如：版本={{version}} / SN={{sn}} / 结果=PASS")
        hint.setObjectName("statusLabel")
        hint.setWordWrap(True)

        self._result_template_edit = QPlainTextEdit()
        self._result_template_edit.setPlaceholderText("例如：版本={{version}} / SN={{sn}} / 测试结果=PASS")
        self._result_template_edit.setFixedHeight(76)

        layout.addWidget(hint)
        layout.addWidget(self._result_template_edit)
        return panel

    def _build_message_panel(self, *, placeholder: str, hint: str) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        hint_label = QLabel(hint)
        hint_label.setObjectName("statusLabel")
        hint_label.setWordWrap(True)

        edit = QPlainTextEdit()
        edit.setPlaceholderText(placeholder)
        edit.setFixedHeight(76)

        layout.addWidget(hint_label)
        layout.addWidget(edit)

        if "通过" in hint:
            self._pass_text_edit = edit
        else:
            self._fail_text_edit = edit
        return panel

    def _build_clear_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("执行到这里会清空当前已收到的串口缓冲，便于后续步骤只判断新的输出。")
        hint.setWordWrap(True)
        hint.setObjectName("statusLabel")
        layout.addWidget(hint)
        return panel

    def load_step(self, step: VerifyStep) -> None:
        self._step = step
        action_idx = _find_combo_index_by_value(self._action_combo, step.action)
        if action_idx >= 0:
            self._action_combo.setCurrentIndex(action_idx)
        self._label_edit.setText(step.label)

        reset_idx = _find_combo_index_by_value(self._reset_method_combo, step.method)
        if reset_idx >= 0:
            self._reset_method_combo.setCurrentIndex(reset_idx)
        self._hold_spin.setValue(step.hold_ms)
        self._release_wait_spin.setValue(step.release_wait_ms)

        self._wait_duration_spin.setValue(max(1, step.duration_ms or 1))
        self._retry_count_spin.setValue(step.retry_count)
        self._retry_delay_spin.setValue(step.retry_delay_ms)
        self._wait_silence_duration_spin.setValue(max(1, step.duration_ms or 1))
        self._wait_silence_timeout_spin.setValue(step.timeout_ms)

        self._send_text_edit.setPlainText(step.text)
        self._append_newline_check.setChecked(step.append_newline)
        self._char_delay_spin.setValue(step.char_delay_ms)

        self._expect_timeout_spin.setValue(step.timeout_ms)
        expect_mode_idx = _find_combo_index_by_value(self._match_mode_combo, step.match_mode)
        if expect_mode_idx >= 0:
            self._match_mode_combo.setCurrentIndex(expect_mode_idx)
        expect_type_idx = _find_combo_index_by_value(self._match_type_combo, step.match_type)
        if expect_type_idx >= 0:
            self._match_type_combo.setCurrentIndex(expect_type_idx)
        self._case_sensitive_check.setChecked(step.case_sensitive)
        self._pattern_editor.set_patterns(step.patterns)

        self._capture_timeout_spin.setValue(step.timeout_ms)
        capture_mode_idx = _find_combo_index_by_value(self._capture_match_mode_combo, step.match_mode)
        if capture_mode_idx >= 0:
            self._capture_match_mode_combo.setCurrentIndex(capture_mode_idx)
        capture_type_idx = _find_combo_index_by_value(self._capture_match_type_combo, step.match_type)
        if capture_type_idx >= 0:
            self._capture_match_type_combo.setCurrentIndex(capture_type_idx)
        self._capture_case_sensitive_check.setChecked(step.case_sensitive)
        self._capture_editor.set_patterns(step.patterns)

        self._result_template_edit.setPlainText(step.text)
        self._pass_text_edit.setPlainText(step.text if step.action == "pass" else "")
        self._fail_text_edit.setPlainText(step.text if step.action == "fail" else "")
        self._on_action_changed()
        self._update_header_texts()

    def to_step(self) -> VerifyStep:
        action = self.current_action()
        step = VerifyStep(
            action=action,
            label=self._label_edit.text().strip(),
            duration_ms=self._wait_duration_spin.value(),
            text="",
            append_newline=False,
            char_delay_ms=0,
            timeout_ms=3000,
            patterns=[],
            match_mode="all",
            match_type="contains",
            case_sensitive=False,
            method=str(self._reset_method_combo.currentData() or "serial_toggle"),
            hold_ms=self._hold_spin.value(),
            release_wait_ms=self._release_wait_spin.value(),
            retry_count=self._retry_count_spin.value(),
            retry_delay_ms=self._retry_delay_spin.value(),
        )

        if action == "send_text":
            step.text = self._send_text_edit.toPlainText()
            step.append_newline = self._append_newline_check.isChecked()
            step.char_delay_ms = self._char_delay_spin.value()
        elif action == "wait_silence":
            step.duration_ms = self._wait_silence_duration_spin.value()
            step.timeout_ms = self._wait_silence_timeout_spin.value()
        elif action == "expect":
            step.timeout_ms = self._expect_timeout_spin.value()
            step.match_mode = str(self._match_mode_combo.currentData() or "all")
            step.match_type = str(self._match_type_combo.currentData() or "contains")
            step.case_sensitive = self._case_sensitive_check.isChecked()
            step.patterns = self._pattern_editor.patterns()
        elif action == "capture":
            step.timeout_ms = self._capture_timeout_spin.value()
            step.match_mode = str(self._capture_match_mode_combo.currentData() or "all")
            step.match_type = str(self._capture_match_type_combo.currentData() or "regex")
            step.case_sensitive = self._capture_case_sensitive_check.isChecked()
            step.patterns = self._capture_editor.patterns()
        elif action == "set_result":
            step.text = self._result_template_edit.toPlainText().strip()
        elif action == "pass":
            step.text = self._pass_text_edit.toPlainText().strip()
        elif action == "fail":
            step.text = self._fail_text_edit.toPlainText().strip()
        elif action == "clear_buffer":
            step.patterns = []

        return step

    def current_action(self) -> str:
        return str(self._action_combo.currentData() or "expect")

    def set_step_number(self, number: int, total: int) -> None:
        self._index_badge.setText(f"STEP {number}")
        self._move_up_btn.setEnabled(number > 1)
        self._move_down_btn.setEnabled(number < total)

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self._body.setVisible(not collapsed)
        self._collapse_btn.setText("▸" if collapsed else "▾")
        self._collapse_btn.setToolTip("展开步骤" if collapsed else "折叠步骤")

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_collapsed()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _step_summary(self, step: VerifyStep) -> str:
        if step.action == "reset":
            summary = f"方式：{step.method} · 保持 {step.hold_ms} ms · 释放后等待 {step.release_wait_ms} ms"
            return self._append_retry_summary(summary, step)
        if step.action == "wait":
            return self._append_retry_summary(f"等待 {step.duration_ms} ms", step)
        if step.action == "wait_silence":
            return self._append_retry_summary(
                f"静默 {step.duration_ms} ms · 最大等待 {step.timeout_ms} ms",
                step,
            )
        if step.action == "send_text":
            suffix = " + 换行" if step.append_newline else ""
            summary = f"发送 {len(step.text)} 字符{suffix} · 逐字延时 {step.char_delay_ms} ms"
            return self._append_retry_summary(summary, step)
        if step.action == "expect":
            return self._append_retry_summary(
                f"{step.match_mode} / {step.match_type} · {len(step.patterns)} 个字段 · 超时 {step.timeout_ms} ms",
                step,
            )
        if step.action == "capture":
            return self._append_retry_summary(
                f"读取 {len(step.patterns)} 个参数 · {step.match_mode} / {step.match_type} · 超时 {step.timeout_ms} ms",
                step,
            )
        if step.action == "set_result":
            preview = step.text.replace("\n", " ").replace("\r", " ").strip()
            return preview[:80] or "从读取到的参数生成设备结果展示"
        if step.action == "pass":
            preview = step.text.replace("\n", " ").replace("\r", " ").strip()
            return preview[:80] or "立即结束并标记通过"
        if step.action == "fail":
            preview = step.text.replace("\n", " ").replace("\r", " ").strip()
            return preview[:80] or "立即结束并标记失败"
        return "清空当前缓冲，只保留后续新输出"

    def _append_retry_summary(self, summary: str, step: VerifyStep) -> str:
        if step.retry_count <= 0:
            return summary
        delay_text = f" / 间隔 {step.retry_delay_ms} ms" if step.retry_delay_ms > 0 else ""
        return f"{summary} · 失败重试 {step.retry_count} 次{delay_text}"

    def _on_action_changed(self) -> None:
        action = self.current_action()
        self._detail_stack.set_current_name(action)
        self.setProperty("blockType", action)
        self.style().unpolish(self)
        self.style().polish(self)
        self._update_header_texts()

    def _update_header_texts(self) -> None:
        step = self.to_step()
        self._title_label.setText(describe_step(step))
        self._summary_label.setText(self._step_summary(step))


class VerifyVisualEditor(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._blocks: list[VerifyStepBlock] = []
        self._init_ui()
        self.load_profile(VerifyProfile(name="临时", steps=[_default_step_for_action("reset")]))

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        profile_frame = QFrame()
        profile_frame.setObjectName("verifyEditorFrame")
        profile_layout = QVBoxLayout(profile_frame)
        profile_layout.setContentsMargins(12, 10, 12, 10)
        profile_layout.setSpacing(8)

        desc_label = QLabel("脚本说明")
        desc_label.setObjectName("metaKey")
        self._description_edit = QPlainTextEdit()
        self._description_edit.setPlaceholderText("例如：设备上电后检查启动日志，再发送 version 指令并校验返回。")
        self._description_edit.setFixedHeight(64)

        serial_grid = QGridLayout()
        serial_grid.setHorizontalSpacing(10)
        serial_grid.setVerticalSpacing(8)

        self._baudrate_combo = _DownComboBox()
        self._baudrate_combo.setEditable(True)
        self._baudrate_combo.setMinimumWidth(90)
        for baud in _BAUDRATE_PRESETS:
            self._baudrate_combo.addItem(baud)
        self._baudrate_combo.setCurrentText("115200")
        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(1, 60000)
        self._timeout_spin.setValue(80)
        self._max_buffer_spin = QSpinBox()
        self._max_buffer_spin.setRange(256, 500000)
        self._max_buffer_spin.setValue(20000)
        self._bytesize_combo = _DownComboBox()
        for item in _BYTESIZE_OPTIONS:
            self._bytesize_combo.addItem(item)
        self._parity_combo = _DownComboBox()
        for item in _PARITY_OPTIONS:
            self._parity_combo.addItem(item)
        self._stopbits_combo = _DownComboBox()
        for item in _STOP_BITS_OPTIONS:
            self._stopbits_combo.addItem(item)
        self._encoding_edit = QLineEdit("utf-8")
        self._encoding_edit.setObjectName("stepInlineEdit")
        self._newline_edit = QLineEdit("\\r\\n")
        self._newline_edit.setObjectName("stepInlineEdit")
        self._newline_edit.setPlaceholderText(r"例如：\r\n / \n")

        serial_grid.addWidget(self._make_key_label("波特率"), 0, 0)
        serial_grid.addWidget(self._baudrate_combo, 0, 1)
        serial_grid.addWidget(self._make_key_label("字节位"), 0, 2)
        serial_grid.addWidget(self._bytesize_combo, 0, 3)
        serial_grid.addWidget(self._make_key_label("校验位"), 0, 4)
        serial_grid.addWidget(self._parity_combo, 0, 5)
        serial_grid.addWidget(self._make_key_label("停止位"), 0, 6)
        serial_grid.addWidget(self._stopbits_combo, 0, 7)
        serial_grid.addWidget(self._make_key_label("读超时(ms)"), 1, 0)
        serial_grid.addWidget(self._timeout_spin, 1, 1)
        serial_grid.addWidget(self._make_key_label("编码"), 1, 2)
        serial_grid.addWidget(self._encoding_edit, 1, 3)
        serial_grid.addWidget(self._make_key_label("换行"), 1, 4)
        serial_grid.addWidget(self._newline_edit, 1, 5)
        serial_grid.addWidget(self._make_key_label("最大缓冲"), 1, 6)
        serial_grid.addWidget(self._max_buffer_spin, 1, 7)

        profile_layout.addWidget(desc_label)
        profile_layout.addWidget(self._description_edit)
        profile_layout.addLayout(serial_grid)

        steps_frame = QFrame()
        steps_frame.setObjectName("verifyEditorFrame")
        steps_layout = QVBoxLayout(steps_frame)
        steps_layout.setContentsMargins(12, 10, 12, 10)
        steps_layout.setSpacing(8)

        steps_header = QHBoxLayout()
        steps_header.setContentsMargins(0, 0, 0, 0)
        steps_header.setSpacing(8)
        steps_title = QLabel("步骤积木")
        steps_title.setObjectName("sectionTitle")
        steps_hint = QLabel("双击步骤头部可折叠/展开；支持重试、等静默、主动通过/失败。")
        steps_hint.setObjectName("statusLabel")
        steps_header.addWidget(steps_title)
        steps_header.addWidget(steps_hint)
        steps_header.addStretch(1)
        self._collapse_all_btn = QPushButton("全部折叠")
        self._collapse_all_btn.setObjectName("stepMiniButton")
        self._expand_all_btn = QPushButton("全部展开")
        self._expand_all_btn.setObjectName("stepMiniButton")
        self._collapse_all_btn.clicked.connect(self.collapse_all)
        self._expand_all_btn.clicked.connect(self.expand_all)
        steps_header.addWidget(self._collapse_all_btn)
        steps_header.addWidget(self._expand_all_btn)

        add_bar = QHBoxLayout()
        add_bar.setContentsMargins(0, 0, 0, 0)
        add_bar.setSpacing(6)
        for action, label in _ACTION_LABELS.items():
            button = QPushButton(f"＋ {label}")
            button.setObjectName("stepAddButton")
            button.clicked.connect(lambda _=False, a=action: self.add_step_block(action=a))
            add_bar.addWidget(button)
        add_bar.addStretch(1)

        self._blocks_area = QScrollArea()
        self._blocks_area.setWidgetResizable(True)
        self._blocks_area.setFrameShape(QFrame.Shape.NoFrame)

        self._blocks_container = QWidget()
        self._blocks_layout = QVBoxLayout(self._blocks_container)
        self._blocks_layout.setContentsMargins(0, 0, 0, 0)
        self._blocks_layout.setSpacing(10)
        self._blocks_layout.addStretch(1)
        self._blocks_area.setWidget(self._blocks_container)

        self._empty_hint = QLabel("暂无步骤，点击上方按钮添加第一块。")
        self._empty_hint.setObjectName("emptyHint")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        steps_layout.addLayout(steps_header)
        steps_layout.addLayout(add_bar)
        steps_layout.addWidget(self._empty_hint)
        steps_layout.addWidget(self._blocks_area, 1)

        root.addWidget(profile_frame)
        root.addWidget(steps_frame, 1)

        self._sync_empty_state()

    def _make_key_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("metaKey")
        return label

    def load_profile(self, profile: VerifyProfile) -> None:
        self._description_edit.setPlainText(profile.description)
        self._baudrate_combo.setCurrentText(str(profile.serial.baudrate))
        self._bytesize_combo.setCurrentText(str(profile.serial.bytesize))
        self._parity_combo.setCurrentText(profile.serial.parity)
        self._stopbits_combo.setCurrentText(str(profile.serial.stopbits).rstrip("0").rstrip("."))
        self._timeout_spin.setValue(profile.serial.timeout_ms)
        self._encoding_edit.setText(profile.serial.encoding)
        self._newline_edit.setText(_escape_control_text(profile.serial.newline))
        self._max_buffer_spin.setValue(profile.serial.max_buffer_chars)

        self.clear_steps()
        for step in profile.steps:
            self.add_step_block(step=step)
        self._sync_empty_state()
        self._reindex_blocks()
        self.expand_all()

    def clear_steps(self) -> None:
        for block in list(self._blocks):
            self._remove_block(block)

    def add_step_block(self, action: str | None = None, step: VerifyStep | None = None) -> None:
        block = VerifyStepBlock(step=step or _default_step_for_action(action or "expect"), parent=self._blocks_container)
        block.remove_requested.connect(self._remove_block)
        block.move_up_requested.connect(self._move_block_up)
        block.move_down_requested.connect(self._move_block_down)
        self._blocks.append(block)
        self._blocks_layout.insertWidget(self._blocks_layout.count() - 1, block)
        self._reindex_blocks()
        self._sync_empty_state()

    def build_profile(self, name: str) -> VerifyProfile:
        temp_profile = VerifyProfile(
            name=name,
            description=self._description_edit.toPlainText().strip(),
            serial=VerifySerialConfig(
                baudrate=int(self._baudrate_combo.currentText().strip() or "115200"),
                bytesize=int(self._bytesize_combo.currentText()),
                parity=self._parity_combo.currentText(),
                stopbits=float(self._stopbits_combo.currentText()),
                timeout_ms=self._timeout_spin.value(),
                encoding=self._encoding_edit.text().strip() or "utf-8",
                newline=_unescape_control_text(self._newline_edit.text()),
                max_buffer_chars=self._max_buffer_spin.value(),
            ),
            steps=[block.to_step() for block in self._blocks],
        )
        if not temp_profile.steps:
            raise ValueError("请至少添加一个步骤。")
        return parse_verify_profile(name, profile_to_dict(temp_profile))

    def collapse_all(self) -> None:
        for block in self._blocks:
            block.set_collapsed(True)

    def expand_all(self) -> None:
        for block in self._blocks:
            block.set_collapsed(False)

    def _move_block_up(self, block_obj: object) -> None:
        if not isinstance(block_obj, VerifyStepBlock) or block_obj not in self._blocks:
            return
        index = self._blocks.index(block_obj)
        if index <= 0:
            return
        self._blocks[index - 1], self._blocks[index] = self._blocks[index], self._blocks[index - 1]
        self._rebuild_block_layout()

    def _move_block_down(self, block_obj: object) -> None:
        if not isinstance(block_obj, VerifyStepBlock) or block_obj not in self._blocks:
            return
        index = self._blocks.index(block_obj)
        if index >= len(self._blocks) - 1:
            return
        self._blocks[index + 1], self._blocks[index] = self._blocks[index], self._blocks[index + 1]
        self._rebuild_block_layout()

    def _remove_block(self, block_obj: object) -> None:
        if not isinstance(block_obj, VerifyStepBlock) or block_obj not in self._blocks:
            return
        self._blocks.remove(block_obj)
        self._blocks_layout.removeWidget(block_obj)
        block_obj.deleteLater()
        self._reindex_blocks()
        self._sync_empty_state()

    def _rebuild_block_layout(self) -> None:
        for block in self._blocks:
            self._blocks_layout.removeWidget(block)
        for index, block in enumerate(self._blocks):
            self._blocks_layout.insertWidget(index, block)
        self._reindex_blocks()
        self._sync_empty_state()

    def _reindex_blocks(self) -> None:
        total = len(self._blocks)
        for index, block in enumerate(self._blocks, start=1):
            block.set_step_number(index, total)

    def _sync_empty_state(self) -> None:
        empty = not self._blocks
        self._empty_hint.setVisible(empty)
        self._blocks_area.setVisible(not empty)


class QStackedWidgetCompat(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._widgets: dict[str, QWidget] = {}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

    def add_named_widget(self, name: str, widget: QWidget) -> None:
        widget.setVisible(False)
        self._widgets[name] = widget
        self._layout.addWidget(widget)

    def set_current_name(self, name: str) -> None:
        for key, widget in self._widgets.items():
            widget.setVisible(key == name)
