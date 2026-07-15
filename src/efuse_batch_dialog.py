"""批量 eFuse 烧录控件 — 嵌入主窗口熔丝台页。

流程：配置待烧 eFuse 字段列表 → 热插拔检测设备 → 对每台设备执行
READ → PRE-CHECK → BURN → VERIFY 状态机。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

from PyQt6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from serial.tools import list_ports

from .constants import (
    EFUSE_CHIP_PRESETS,
    FLASH_BAUD_DEFAULT,
    FLASH_BAUD_OPTIONS,
    TOOL_DIR,
    _build_tool_command,
    decode_process_output,
    _inject_local_esptool_pythonpath,
    _tool_backend_available,
    resolve_chip_arg,
)
from .dialog_memory import get_open_file_name
from .efuse_batch_safety import (
    EfuseRunConfig,
    EfuseTarget,
    build_transport_fingerprint,
    evaluate_efuse_precheck,
    evaluate_efuse_verification,
    extract_stable_device_identity,
)
from .styles import BASE_STYLESHEET

# ── 动态芯片列表（来自 esptool，不硬编码）───────────────────────────────────
try:
    from esptool.targets import CHIP_DEFS as _ESPTOOL_CHIP_DEFS
    _CHIP_OPTIONS: list[str] = sorted(_ESPTOOL_CHIP_DEFS.keys())
except Exception:
    _CHIP_OPTIONS = ["esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32h2", "esp32p4"]

# ── 数据模型 ─────────────────────────────────────────────────────────────────


@dataclass
class EfuseFieldConfig:
    name: str
    value: str
    description: str = ""
    enabled: bool = True


class BurnTaskState(Enum):
    WAITING = "等待"
    READING = "读取中"
    READ_OK = "预检完成"
    IDENTITY_CHECK = "身份确认中"
    BURNING = "烧录中"
    VERIFYING = "验证中"
    DONE_OK = "完成"
    SKIPPED = "已跳过"
    FAILED = "失败"


class BurnAuthorization(Enum):
    NONE = "none"
    MANUAL = "manual"
    BATCH = "batch"
    AUTO = "auto"


@dataclass
class BurnTaskItem:
    device_id: str
    port: str
    transport_id: str
    chip_name: str = "未识别"
    state: BurnTaskState = BurnTaskState.WAITING
    read_result: dict[str, dict] = field(default_factory=dict)
    fields_to_burn: list[EfuseTarget] = field(default_factory=list)
    fields_skipped: list[str] = field(default_factory=list)
    fields_conflict: list[str] = field(default_factory=list)
    error_message: str = ""
    force_burn: bool = False
    run_config: EfuseRunConfig | None = None
    authorization: BurnAuthorization = BurnAuthorization.NONE
    authorization_id: int | None = None
    batch_precheck_id: int | None = None
    precheck_identity: str = ""
    authorized_identity: str = ""
    transport_warning: str = ""
    process: QProcess | None = None
    process_token: int = 0


# ── 辅助 ─────────────────────────────────────────────────────────────────────

_STATE_COLORS: dict[BurnTaskState, str] = {
    BurnTaskState.WAITING: "#6b7a94",
    BurnTaskState.READING: "#b45309",
    BurnTaskState.READ_OK: "#2560e0",
    BurnTaskState.IDENTITY_CHECK: "#b45309",
    BurnTaskState.BURNING: "#b45309",
    BurnTaskState.VERIFYING: "#b45309",
    BurnTaskState.DONE_OK: "#065f46",
    BurnTaskState.SKIPPED: "#6b7a94",
    BurnTaskState.FAILED: "#991b1b",
}

_STATE_ICONS: dict[BurnTaskState, str] = {
    BurnTaskState.WAITING: "○",
    BurnTaskState.READING: "⏳",
    BurnTaskState.READ_OK: "ℹ",
    BurnTaskState.IDENTITY_CHECK: "🔐",
    BurnTaskState.BURNING: "⏳",
    BurnTaskState.VERIFYING: "⏳",
    BurnTaskState.DONE_OK: "✓",
    BurnTaskState.SKIPPED: "~",
    BurnTaskState.FAILED: "✗",
}


def _identify_chip(port: str) -> str:
    """Run esptool chip_id synchronously to identify the chip. Returns chip name or empty."""
    import subprocess
    from .constants import _build_process_env_dict

    cmd = _build_tool_command("esptool", "--port", port, "chip_id")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=8,
            cwd=str(TOOL_DIR),
            env=_build_process_env_dict(),
        )
        for pattern in [r"Chip is\s+(.+?)(?:\s+\(|$)", r"Detecting chip type\.*\s*(.+)"]:
            m = re.search(pattern, result.stdout)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return ""


# ── 主控件 ───────────────────────────────────────────────────────────────────


class BurnEfuseBatchWidget(QWidget):
    """批量 eFuse 烧录控件 — 直接嵌入 page_efuse 页。"""

    MAX_CONCURRENT = 4

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._field_configs: list[EfuseFieldConfig] = []
        self._tasks: list[BurnTaskItem] = []
        self._present_transports: dict[str, str] = {}
        self._connection_generation = 0
        self._next_authorization_id = 1
        self._auto_authorization_id: int | None = None
        self._auto_run_config: EfuseRunConfig | None = None
        self._pending_batch_id: int | None = None
        self._pending_batch_config: EfuseRunConfig | None = None
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1500)
        self._poll_timer.timeout.connect(self._poll_ports)
        self._init_ui()
        self._apply_style()

    # ── UI 构建 ──────────────────────────────────────────────────────────────

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # ─── 上半部：字段配置 + 设备队列 ──────────────────────
        top = QWidget()
        top_lay = QVBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(8)

        # 字段配置区
        field_frame = QFrame()
        field_frame.setObjectName("sectionFrame")
        field_vbox = QVBoxLayout(field_frame)
        field_vbox.setContentsMargins(12, 8, 12, 8)
        field_vbox.setSpacing(6)

        fhdr = QHBoxLayout()
        fhdr.setSpacing(8)
        fttl = QLabel("熔丝字段配置")
        fttl.setObjectName("sectionTitle")
        self._add_field_btn = QPushButton("＋ 添加字段")
        self._add_field_btn.setObjectName("addEntryButton")
        self._add_field_btn.clicked.connect(self._add_empty_field)
        self._import_preset_btn = QPushButton("从预设导入")
        self._import_preset_btn.clicked.connect(self._import_from_presets)
        # 这三个控件不加入本 layout，由主窗口 toolbar 接管显示
        self._baud_combo = QComboBox()
        self._baud_combo.setEditable(True)
        for b in FLASH_BAUD_OPTIONS:
            self._baud_combo.addItem(b)
        self._baud_combo.setCurrentText(FLASH_BAUD_DEFAULT)
        self._baud_combo.setFixedWidth(100)
        self._chip_combo = QComboBox()
        for _chip in _CHIP_OPTIONS:
            self._chip_combo.addItem(_chip)
        _default_chip = "esp32p4"
        _ci = self._chip_combo.findText(_default_chip)
        if _ci >= 0:
            self._chip_combo.setCurrentIndex(_ci)
        self._chip_combo.setFixedWidth(110)
        self._auto_burn_btn = QPushButton("自动熔丝：关")
        self._auto_burn_btn.setCheckable(True)
        self._auto_burn_btn.toggled.connect(self._toggle_auto_detect)
        fhdr.addWidget(fttl)
        fhdr.addStretch(1)
        fhdr.addWidget(self._add_field_btn)
        fhdr.addWidget(self._import_preset_btn)

        self._field_table = QTableWidget()
        self._field_table.setColumnCount(5)
        self._field_table.setHorizontalHeaderLabels(["启用", "字段名", "值", "说明", "操作"])
        fh = self._field_table.horizontalHeader()
        fh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        fh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        fh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        fh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        fh.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._field_table.setColumnWidth(0, 42)
        self._field_table.setColumnWidth(1, 180)
        self._field_table.setColumnWidth(2, 80)
        self._field_table.setColumnWidth(4, 72)
        vh = self._field_table.verticalHeader()
        vh.setDefaultSectionSize(30)
        vh.hide()
        self._field_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._field_table.setMinimumHeight(131)
        self._field_table.setMaximumHeight(131)

        field_vbox.addLayout(fhdr)
        field_vbox.addWidget(self._field_table)
        top_lay.addWidget(field_frame)

        # 设备队列区
        dev_frame = QFrame()
        dev_frame.setObjectName("sectionFrame")
        dev_vbox = QVBoxLayout(dev_frame)
        dev_vbox.setContentsMargins(12, 8, 12, 8)
        dev_vbox.setSpacing(6)

        dhdr = QHBoxLayout()
        dhdr.setSpacing(8)
        dttl = QLabel("设备队列")
        dttl.setObjectName("sectionTitle")
        self._dev_count_lbl = QLabel("0 台")
        self._dev_count_lbl.setObjectName("countBadge")
        dhdr.addWidget(dttl)
        dhdr.addWidget(self._dev_count_lbl)
        dhdr.addStretch(1)

        self._dev_table = QTableWidget()
        self._dev_table.setColumnCount(6)
        self._dev_table.setHorizontalHeaderLabels(["串口", "芯片", "状态", "预检", "详情", "操作"])
        dh = self._dev_table.horizontalHeader()
        dh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        dh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        dh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        dh.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        dh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        dh.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self._dev_table.setColumnWidth(0, 70)
        self._dev_table.setColumnWidth(1, 100)
        self._dev_table.setColumnWidth(2, 100)
        self._dev_table.setColumnWidth(3, 120)
        self._dev_table.setColumnWidth(5, 140)
        dvh = self._dev_table.verticalHeader()
        dvh.setDefaultSectionSize(36)
        dvh.hide()
        self._dev_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._dev_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._start_all_btn = QPushButton("全部开始")
        self._start_all_btn.setObjectName("primaryButton")
        self._start_all_btn.clicked.connect(self._start_all)
        self._clear_done_btn = QPushButton("清空已完成")
        self._clear_done_btn.clicked.connect(self._clear_done)
        self._refresh_btn = QPushButton("手动扫描")
        self._refresh_btn.clicked.connect(self._poll_ports)
        btn_row.addWidget(self._start_all_btn)
        btn_row.addWidget(self._clear_done_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._refresh_btn)

        dev_vbox.addLayout(dhdr)
        dev_vbox.addWidget(self._dev_table, 1)
        dev_vbox.addLayout(btn_row)
        top_lay.addWidget(dev_frame, 1)

        # ─── 下半部：日志 ─────────────────────────────────────
        log_frame = QFrame()
        log_frame.setObjectName("sectionFrame")
        log_vbox = QVBoxLayout(log_frame)
        log_vbox.setContentsMargins(12, 8, 12, 8)
        log_vbox.setSpacing(4)

        lhdr = QHBoxLayout()
        lhdr.setSpacing(8)
        lttl = QLabel("操作日志")
        lttl.setObjectName("sectionTitle")
        self._clear_log_btn = QPushButton("清空")
        self._clear_log_btn.clicked.connect(lambda: self._log.clear())
        lhdr.addWidget(lttl)
        lhdr.addStretch(1)
        lhdr.addWidget(self._clear_log_btn)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        self._log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        log_vbox.addLayout(lhdr)
        log_vbox.addWidget(self._log, 1)

        splitter.addWidget(top)
        splitter.addWidget(log_frame)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    # ── 样式 ─────────────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        self.setStyleSheet(
            BASE_STYLESHEET
            + """
            QFrame#sectionFrame {
                background: #ffffff;
                border: 1px solid #e0e4ea;
                border-radius: 10px;
            }
            QLabel#sectionTitle { font-size: 13px; }
            QLabel#countBadge {
                background: #e8edf7; border: 1px solid #c5cfe8;
                border-radius: 4px; padding: 3px 10px;
                color: #2560e0; font-weight: 600; font-size: 12px;
            }
            QTableWidget {
                background: #ffffff; border: none;
                gridline-color: #eef0f5; font-size: 12px;
                alternate-background-color: #f8f9fb;
            }
            QTableWidget::item { padding: 2px 6px; }
            QHeaderView::section {
                background: #f0f2f5; border: none;
                border-bottom: 1px solid #dde1ea;
                font-weight: 600; font-size: 11px;
                color: #6b7a94; padding: 4px 6px;
            }
            QPlainTextEdit {
                background: #ffffff; border: 1px solid #dde1ea;
                border-radius: 7px; padding: 6px 8px;
                color: #1e2a3a; font-size: 11px;
            }
            QPushButton#addEntryButton {
                background: #f0f5ff; border: 1px solid #c3d4f8;
                border-radius: 6px; color: #2560e0;
                font-size: 12px; padding: 3px 10px;
            }
            QPushButton#addEntryButton:hover { background: #dce8ff; }
            QPushButton#fieldRemoveBtn {
                background: #fff0f0; border: 1px solid #f5c0c0;
                border-radius: 4px; color: #991b1b;
                font-size: 11px; padding: 1px 6px;
                min-width: 40px; max-height: 24px;
            }
            QPushButton#fieldRemoveBtn:hover { background: #fee2e2; }
            QPushButton#devActionBtn {
                border-radius: 4px; padding: 2px 8px;
                font-size: 11px; min-width: 44px; max-height: 28px;
            }
            QComboBox {
                background: #f8f9fb; border: 1px solid #dde1ea;
                border-radius: 7px; padding: 4px 8px;
                color: #1a2333; min-height: 20px;
            }
            QComboBox:focus { border: 1.5px solid #2560e0; background: #ffffff; }
            QCheckBox::indicator { width: 16px; height: 16px; }
        """
        )

    # ── 字段配置管理 ─────────────────────────────────────────────────────────

    def _add_empty_field(self) -> None:
        self._field_configs.append(EfuseFieldConfig(name="", value="", enabled=True))
        self._refresh_field_table()

    def _add_field(self, cfg: EfuseFieldConfig) -> None:
        self._field_configs.append(cfg)
        self._refresh_field_table()

    def _remove_field(self, idx: int) -> None:
        if 0 <= idx < len(self._field_configs):
            self._field_configs.pop(idx)
            self._refresh_field_table()

    def _import_from_presets(self) -> None:
        """打开文件选择器，加载 YAML 文件中的 eFuse 字段配置。"""
        path, _ = get_open_file_name(
            self,
            "选择 eFuse 配置文件",
            "YAML 文件 (*.yaml *.yml);;All Files (*)",
        )
        if not path:
            return
        try:
            import yaml  # type: ignore
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as e:
            QMessageBox.critical(self, "读取失败", f"无法读取文件：{e}")
            return

        if not isinstance(data, dict):
            QMessageBox.warning(self, "格式错误", "文件不是有效的 YAML 字典。")
            return

        # 支持两种格式：
        # 1. burn_efuse_fields: [{enabled, name, value, description}, ...]
        # 2. efuse_presets: {chip: [{label, name, value, description}, ...]}
        entries: list[EfuseFieldConfig] = []
        if "burn_efuse_fields" in data:
            for item in (data["burn_efuse_fields"] or []):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                value = str(item.get("value", "")).strip()
                if not name or not value:
                    continue
                entries.append(EfuseFieldConfig(
                    name=name,
                    value=value,
                    description=str(item.get("description", "")).strip(),
                    enabled=bool(item.get("enabled", True)),
                ))
        elif "efuse_presets" in data:
            for _chip_key, chip_entries in (data["efuse_presets"] or {}).items():
                for item in (chip_entries or []):
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                    if not name or not value:
                        continue
                    entries.append(EfuseFieldConfig(
                        name=name,
                        value=value,
                        description=str(item.get("description") or item.get("label", "")).strip(),
                        enabled=True,
                    ))
        else:
            QMessageBox.warning(
                self, "格式不支持",
                "没有找到 burn_efuse_fields 或 efuse_presets 键。\n"
                "请参考 esp32p4_burn_efuse_config_example.yaml 格式。"
            )
            return

        if not entries:
            QMessageBox.information(self, "提示", "文件中没有有效的字段配置。")
            return

        existing_names = {c.name for c in self._field_configs}
        added = 0
        for cfg in entries:
            if cfg.name not in existing_names:
                self._field_configs.append(cfg)
                existing_names.add(cfg.name)
                added += 1
        self._refresh_field_table()
        self._append_log(f"从文件导入 {added} 个字段（{path}")

    def _refresh_field_table(self) -> None:
        tbl = self._field_table
        tbl.setRowCount(len(self._field_configs))
        for row, cfg in enumerate(self._field_configs):
            # col 0 — 启用
            cb = QCheckBox()
            cb.setChecked(cfg.enabled)
            cb.toggled.connect(lambda checked, r=row: self._on_field_enabled_changed(r, checked))
            w = QWidget()
            lay = QHBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(cb)
            tbl.setCellWidget(row, 0, w)

            # col 1 — 字段名
            name_item = QTableWidgetItem(cfg.name)
            tbl.setItem(row, 1, name_item)

            # col 2 — 值
            val_item = QTableWidgetItem(cfg.value)
            val_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            tbl.setItem(row, 2, val_item)

            # col 3 — 说明
            tbl.setItem(row, 3, QTableWidgetItem(cfg.description))

            # col 4 — 删除按钮
            rm_btn = QPushButton("删除")
            rm_btn.setObjectName("fieldRemoveBtn")
            rm_btn.clicked.connect(lambda _=False, r=row: self._remove_field(r))
            w2 = QWidget()
            lay2 = QHBoxLayout(w2)
            lay2.setContentsMargins(2, 2, 0, 2)
            lay2.addWidget(rm_btn)
            tbl.setCellWidget(row, 4, w2)

        # 监听编辑
        try:
            self._field_table.cellChanged.disconnect(self._on_field_cell_changed)
        except TypeError:
            pass
        self._field_table.cellChanged.connect(self._on_field_cell_changed)

    def _on_field_enabled_changed(self, row: int, checked: bool) -> None:
        if 0 <= row < len(self._field_configs):
            self._field_configs[row].enabled = checked

    def _on_field_cell_changed(self, row: int, col: int) -> None:
        if row < 0 or row >= len(self._field_configs):
            return
        item = self._field_table.item(row, col)
        if item is None:
            return
        text = item.text().strip()
        cfg = self._field_configs[row]
        if col == 1:
            cfg.name = text
        elif col == 2:
            cfg.value = text
        elif col == 3:
            cfg.description = text

    def _get_enabled_fields(self) -> list[EfuseFieldConfig]:
        return [c for c in self._field_configs if c.enabled and c.name and c.value]

    def _capture_run_config(self) -> EfuseRunConfig | None:
        fields = self._get_enabled_fields()
        if not fields:
            QMessageBox.warning(self, "提示", "请至少配置一个已启用的 eFuse 字段。")
            return None

        normalized_names = [field.name.strip().upper() for field in fields]
        duplicate_names = sorted(
            {name for name in normalized_names if normalized_names.count(name) > 1}
        )
        if duplicate_names:
            QMessageBox.warning(
                self,
                "字段配置错误",
                "存在重复的 eFuse 字段：" + ", ".join(duplicate_names),
            )
            return None

        baud = self._baud_combo.currentText().strip()
        if not baud.isdigit():
            QMessageBox.warning(self, "字段配置错误", "波特率必须是数字。")
            return None
        chip = self._chip_combo.currentText().strip() or "auto"
        targets = tuple(
            EfuseTarget(field.name, field.value, field.description) for field in fields
        )
        return EfuseRunConfig(fields=targets, chip=chip, baud=baud)

    def _new_authorization_id(self) -> int:
        authorization_id = self._next_authorization_id
        self._next_authorization_id += 1
        return authorization_id

    def _set_auto_button_state(self, enabled: bool) -> None:
        self._auto_burn_btn.blockSignals(True)
        self._auto_burn_btn.setChecked(enabled)
        self._auto_burn_btn.setText(
            "自动熔丝：已武装" if enabled else "自动熔丝：关"
        )
        self._auto_burn_btn.blockSignals(False)

    def _clear_authorization(self, task: BurnTaskItem) -> None:
        task.authorization = BurnAuthorization.NONE
        task.authorization_id = None
        task.authorized_identity = ""

    def _is_burn_authorized(self, task: BurnTaskItem) -> bool:
        if task.authorization in (BurnAuthorization.MANUAL, BurnAuthorization.BATCH):
            return task.authorization_id is not None
        if task.authorization == BurnAuthorization.AUTO:
            return (
                task.authorization_id is not None
                and task.authorization_id == self._auto_authorization_id
            )
        return False

    def _disarm_auto_burn(self, *, log: bool = True) -> None:
        previous_id = self._auto_authorization_id
        self._auto_authorization_id = None
        self._auto_run_config = None
        self._poll_timer.stop()
        self._set_auto_button_state(False)
        if previous_id is not None:
            for task in self._tasks:
                if (
                    task.authorization == BurnAuthorization.AUTO
                    and task.authorization_id == previous_id
                ):
                    self._clear_authorization(task)
            if log:
                self._append_log("自动熔丝已解除武装；未开始烧录的任务授权已撤销")

    # ── 热插拔轮询 ───────────────────────────────────────────────────────────

    def _toggle_auto_detect(self, enabled: bool) -> None:
        if not enabled:
            self._disarm_auto_burn()
            return

        if self._pending_batch_id is not None:
            QMessageBox.warning(
                self,
                "批量预检进行中",
                "请先完成或停止当前批量预检，再武装自动熔丝。",
            )
            self._set_auto_button_state(False)
            return

        run_config = self._capture_run_config()
        if run_config is None:
            self._set_auto_button_state(False)
            return
        reply = QMessageBox.warning(
            self,
            "武装自动熔丝",
            "开启后，新接入且通过身份复核的设备将自动执行不可逆 eFuse 烧录。\n\n"
            f"芯片：{run_config.chip}\n"
            f"波特率：{run_config.baud}\n"
            f"字段：{', '.join(field.name for field in run_config.fields)}\n\n"
            "是否确认武装？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._set_auto_button_state(False)
            return

        self._auto_authorization_id = self._new_authorization_id()
        self._auto_run_config = run_config
        self._set_auto_button_state(True)
        self._append_log(
            f"自动熔丝已武装（授权 {self._auto_authorization_id}，"
            f"字段 {', '.join(field.name for field in run_config.fields)}）"
        )
        self._poll_timer.start()
        self._poll_ports()

    def _poll_ports(self) -> None:
        current_ports: dict[str, tuple[str, str]] = {}
        for pi in list_ports.comports():
            if pi.description and re.search(r"通信端口|Communications Port", pi.description):
                continue
            description = pi.description or "串口设备"
            transport_id = build_transport_fingerprint(
                device=pi.device,
                serial_number=getattr(pi, "serial_number", "") or "",
                location=getattr(pi, "location", "") or "",
                vid=getattr(pi, "vid", None),
                pid=getattr(pi, "pid", None),
                hwid=getattr(pi, "hwid", "") or "",
                description=description,
            )
            current_ports[pi.device] = (description, transport_id)

        self._present_transports = {
            port: observation[1] for port, observation in current_ports.items()
        }

        # 任何断开或同 COM 传输身份变化都会使旧任务和旧预检立即失效。
        for task in list(self._tasks):
            observation = current_ports.get(task.port)
            if observation is None:
                if task.state in (BurnTaskState.BURNING, BurnTaskState.VERIFYING):
                    self._record_irreversible_transport_loss(task, "设备连接已丢失")
                else:
                    self._remove_task(task, "设备已断开")
            elif observation[1] != task.transport_id:
                if task.state in (BurnTaskState.BURNING, BurnTaskState.VERIFYING):
                    self._record_irreversible_transport_loss(
                        task,
                        "同一串口的 USB 传输身份已变化",
                    )
                else:
                    self._remove_task(task, "同一串口的 USB 传输身份已变化")

        # 新接入
        known_ports = {task.port for task in self._tasks}
        for port in sorted(current_ports.keys() - known_ports):
            _description, transport_id = current_ports[port]
            chip = _identify_chip(port)
            self._connection_generation += 1
            task = BurnTaskItem(
                device_id=f"{transport_id}#{self._connection_generation}",
                port=port,
                transport_id=transport_id,
                chip_name=chip or "识别中…",
            )
            if self._auto_authorization_id is not None and self._auto_run_config is not None:
                task.authorization = BurnAuthorization.AUTO
                task.authorization_id = self._auto_authorization_id
                task.run_config = self._auto_run_config
            self._tasks.append(task)
            self._append_log(f"{port} 接入 ({task.chip_name})")

        self._refresh_dev_table()
        self._schedule_next()
        QTimer.singleShot(0, self._maybe_confirm_pending_batch)

    # ── 设备队列 UI ──────────────────────────────────────────────────────────

    def _refresh_dev_table(self) -> None:
        tbl = self._dev_table
        tbl.setRowCount(len(self._tasks))
        for row, task in enumerate(self._tasks):
            # 串口
            tbl.setItem(row, 0, QTableWidgetItem(task.port))

            # 芯片
            tbl.setItem(row, 1, QTableWidgetItem(task.chip_name))

            # 状态
            icon = _STATE_ICONS.get(task.state, "")
            state_item = QTableWidgetItem(f"{icon} {task.state.value}")
            color = _STATE_COLORS.get(task.state, "#1a2333")
            state_item.setForeground(QColor(color))
            bold = QFont()
            bold.setBold(True)
            state_item.setFont(bold)
            tbl.setItem(row, 2, state_item)

            # 预检
            precheck = ""
            if task.state in (
                BurnTaskState.READ_OK,
                BurnTaskState.IDENTITY_CHECK,
                BurnTaskState.BURNING,
                BurnTaskState.VERIFYING,
                BurnTaskState.DONE_OK,
                BurnTaskState.SKIPPED,
            ):
                burn_n = len(task.fields_to_burn)
                skip_n = len(task.fields_skipped)
                conflict_n = len(task.fields_conflict)
                parts = []
                if burn_n:
                    parts.append(f"待烧 {burn_n}")
                if skip_n:
                    parts.append(f"跳过 {skip_n}")
                if conflict_n:
                    parts.append(f"冲突 {conflict_n}")
                precheck = " / ".join(parts) if parts else "全部已满足"
            tbl.setItem(row, 3, QTableWidgetItem(precheck))

            # 详情
            detail = task.error_message or task.transport_warning
            if not detail and task.state == BurnTaskState.SKIPPED:
                if task.fields_conflict:
                    detail = "预检冲突: " + ", ".join(task.fields_conflict)
                else:
                    detail = "全部字段已满足"
            elif not detail and task.fields_conflict:
                detail = "预检冲突: " + ", ".join(task.fields_conflict)
            tbl.setItem(row, 4, QTableWidgetItem(detail))

            # 操作按钮
            btn_w = QWidget()
            btn_lay = QHBoxLayout(btn_w)
            btn_lay.setContentsMargins(4, 4, 4, 4)
            btn_lay.setSpacing(4)
            _btn_ss = "font-size:11px; padding:2px 8px; min-width:44px; max-height:28px;"
            if task.state == BurnTaskState.WAITING:
                start_b = QPushButton("开始")
                start_b.setObjectName("primaryButton")
                start_b.setStyleSheet(_btn_ss)
                start_b.clicked.connect(lambda _=False, t=task: self._run_task(t))
                btn_lay.addWidget(start_b)
            elif task.state in (BurnTaskState.FAILED,):
                retry_b = QPushButton("重试")
                retry_b.setObjectName("primaryButton")
                retry_b.setStyleSheet(_btn_ss)
                retry_b.clicked.connect(lambda _=False, t=task: self._retry_task(t))
                btn_lay.addWidget(retry_b)
            elif task.state in (
                BurnTaskState.READING,
                BurnTaskState.IDENTITY_CHECK,
                BurnTaskState.BURNING,
                BurnTaskState.VERIFYING,
            ):
                stop_b = QPushButton("停止")
                stop_b.setObjectName("dangerButton")
                stop_b.setStyleSheet(_btn_ss)
                stop_b.clicked.connect(lambda _=False, t=task: self._abort_task(t))
                btn_lay.addWidget(stop_b)
            elif task.state == BurnTaskState.READ_OK:
                if task.batch_precheck_id is not None:
                    pending = QLabel("待批次确认")
                    pending.setStyleSheet("font-size:11px; color:#b45309;")
                    btn_lay.addWidget(pending)
                else:
                    burn_b = QPushButton("烧录")
                    burn_b.setObjectName("primaryButton")
                    burn_b.setStyleSheet(_btn_ss)
                    burn_b.clicked.connect(
                        lambda _=False, t=task: self._authorize_manual_burn(t)
                    )
                    btn_lay.addWidget(burn_b)
            elif task.state == BurnTaskState.SKIPPED:
                if task.batch_precheck_id is not None:
                    pending = QLabel("批次中已满足")
                    pending.setStyleSheet("font-size:11px; color:#6b7a94;")
                    btn_lay.addWidget(pending)
                else:
                    re_b = QPushButton("开始")
                    re_b.setStyleSheet(
                        _btn_ss
                        + "background:#fef3c7; color:#92400e; "
                        "border:1px solid #f59e0b;"
                    )
                    re_b.clicked.connect(
                        lambda _=False, t=task: self._force_run_task(t)
                    )
                    btn_lay.addWidget(re_b)
            tbl.setCellWidget(row, 5, btn_w)

        self._dev_count_lbl.setText(f"{len(self._tasks)} 台")

    # ── 全局按钮 ─────────────────────────────────────────────────────────────

    def _start_all(self) -> None:
        if self._pending_batch_id is not None:
            QMessageBox.information(
                self,
                "批量预检进行中",
                "当前批量仍在读取设备身份和 eFuse 状态。",
            )
            return
        run_config = self._capture_run_config()
        if run_config is None:
            return
        candidates = [
            task
            for task in self._tasks
            if task.state == BurnTaskState.WAITING and self._is_task_current(task)
        ]
        if not candidates:
            QMessageBox.information(self, "提示", "设备队列为空，请先扫描或接入设备。")
            return

        # 先做只读预检，再向用户展示每颗芯片的身份并请求不可逆授权。
        self._disarm_auto_burn(log=False)
        batch_id = self._new_authorization_id()
        self._pending_batch_id = batch_id
        self._pending_batch_config = run_config
        for task in candidates:
            self._clear_authorization(task)
            task.batch_precheck_id = batch_id
            task.run_config = run_config
            task.force_burn = False
        self._append_log(
            f"批量 {batch_id} 开始只读预检（设备 {len(candidates)} 台）；"
            "新接入设备不会加入本批次"
        )
        self._schedule_next()

    def _maybe_confirm_pending_batch(self) -> None:
        """Request irreversible authorization after every target MAC is known."""
        batch_id = self._pending_batch_id
        run_config = self._pending_batch_config
        if batch_id is None or run_config is None:
            return
        members = [
            task for task in self._tasks if task.batch_precheck_id == batch_id
        ]
        if any(
            task.state in (BurnTaskState.WAITING, BurnTaskState.READING)
            for task in members
        ):
            return

        invalid_config = [
            task
            for task in members
            if task.state == BurnTaskState.READ_OK and task.run_config != run_config
        ]
        for task in invalid_config:
            task.state = BurnTaskState.FAILED
            task.error_message = "任务配置与批量确认快照不一致，已阻止烧录"
            self._clear_authorization(task)
            self._append_log(f"{task.port} {task.error_message}")

        ready = [
            task
            for task in members
            if task.state == BurnTaskState.READ_OK
            and self._is_task_current(task)
            and task.precheck_identity
            and task.run_config == run_config
        ]
        self._pending_batch_id = None
        self._pending_batch_config = None
        for task in members:
            task.batch_precheck_id = None

        if not ready:
            self._append_log(
                f"批量 {batch_id} 预检结束：没有可进入烧录确认的设备"
            )
            self._refresh_dev_table()
            return

        identity_lines = "\n".join(
            f"  {task.port}: {task.precheck_identity}" for task in ready
        )
        reply = QMessageBox.warning(
            self,
            "确认批量 eFuse 烧录",
            f"以下 {len(ready)} 台设备已完成预检：\n{identity_lines}\n\n"
            f"芯片：{run_config.chip}\n"
            f"字段：{', '.join(field.name for field in run_config.fields)}\n\n"
            "授权仅绑定上列芯片身份；eFuse 一经烧写不可撤销。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._append_log(f"批量 {batch_id} 的不可逆烧录授权已取消")
            self._refresh_dev_table()
            return

        authorization_id = self._new_authorization_id()
        for task in ready:
            task.authorization = BurnAuthorization.BATCH
            task.authorization_id = authorization_id
            task.authorized_identity = task.precheck_identity
        self._append_log(
            f"批量烧录已授权（授权 {authorization_id}，设备 {len(ready)} 台）"
        )
        self._refresh_dev_table()
        self._schedule_next()

    def _schedule_next(self) -> None:
        active_states = (
            BurnTaskState.READING,
            BurnTaskState.IDENTITY_CHECK,
            BurnTaskState.BURNING,
            BurnTaskState.VERIFYING,
        )
        running = sum(1 for task in self._tasks if task.state in active_states)
        for task in self._tasks:
            if running >= self.MAX_CONCURRENT:
                break
            if (
                task.state == BurnTaskState.WAITING
                and self._is_task_current(task)
                and (
                    self._is_burn_authorized(task)
                    or task.batch_precheck_id is not None
                )
            ):
                self._run_task(task)
                if task.state in active_states:
                    running += 1
        for task in self._tasks:
            if running >= self.MAX_CONCURRENT:
                break
            if (
                task.state == BurnTaskState.READ_OK
                and self._is_task_current(task)
                and self._is_burn_authorized(task)
            ):
                self._do_burn(task)
                if task.state in active_states:
                    running += 1

    def _clear_done(self) -> None:
        self._tasks = [t for t in self._tasks if t.state not in (
            BurnTaskState.DONE_OK, BurnTaskState.SKIPPED)]
        self._refresh_dev_table()
        QTimer.singleShot(0, self._maybe_confirm_pending_batch)

    def _clear_all_devices(self) -> None:
        """Clear safe tasks without interrupting an irreversible operation."""
        self._disarm_auto_burn(log=False)
        self._pending_batch_id = None
        self._pending_batch_config = None
        protected: list[BurnTaskItem] = []
        for task in list(self._tasks):
            task.batch_precheck_id = None
            self._clear_authorization(task)
            if task.state in (BurnTaskState.BURNING, BurnTaskState.VERIFYING):
                protected.append(task)
                continue
            self._kill_task(task)
            task.run_config = None
        self._tasks = protected
        self._refresh_dev_table()
        if protected:
            self._append_log(
                f"已清空可安全停止的设备；{len(protected)} 个不可逆任务将继续完成并验证"
            )
        else:
            self._append_log("已清空所有设备")

    def _stop_all(self) -> None:
        self._disarm_auto_burn(log=False)
        self._pending_batch_id = None
        self._pending_batch_config = None
        for task in self._tasks:
            task.batch_precheck_id = None
            self._clear_authorization(task)
            if task.state in (BurnTaskState.BURNING, BurnTaskState.VERIFYING):
                self._append_log(
                    f"{task.port} 已进入不可逆阶段，停止请求不会中断；将继续验证"
                )
                continue
            if task.state in (
                BurnTaskState.READING,
                BurnTaskState.IDENTITY_CHECK,
            ):
                self._kill_task(task)
                task.state = BurnTaskState.FAILED
                task.error_message = "用户停止"
        self._refresh_dev_table()

    # ── 状态机 ───────────────────────────────────────────────────────────────

    def _find_task(self, port: str) -> BurnTaskItem | None:
        for t in self._tasks:
            if t.port == port:
                return t
        return None

    def _is_task_current(self, task: BurnTaskItem) -> bool:
        """Return whether *task* still represents the observed transport."""
        if task in self._tasks and task.state in (
            BurnTaskState.BURNING,
            BurnTaskState.VERIFYING,
        ):
            # Once OTP writing begins, transport loss must not invalidate the
            # process callback or trigger an active kill.  Verification will
            # still fail closed if the port now points at another chip.
            return True
        return (
            task in self._tasks
            and self._present_transports.get(task.port) == task.transport_id
        )

    def _record_irreversible_transport_loss(
        self,
        task: BurnTaskItem,
        reason: str,
    ) -> None:
        warning = f"{reason}；不可逆操作不会被强制中断，最终状态必须重新核验"
        if task.transport_warning == warning:
            return
        task.transport_warning = warning
        self._append_log(f"{task.port} {warning}")

    def _remove_task(self, task: BurnTaskItem, reason: str) -> None:
        """Invalidate every capability held by a disconnected/replaced task."""
        if task not in self._tasks:
            return
        was_active = task.state in (
            BurnTaskState.READING,
            BurnTaskState.IDENTITY_CHECK,
            BurnTaskState.BURNING,
            BurnTaskState.VERIFYING,
        )
        self._kill_task(task)
        self._clear_authorization(task)
        task.batch_precheck_id = None
        task.run_config = None
        task.precheck_identity = ""
        task.read_result.clear()
        task.fields_to_burn.clear()
        task.fields_skipped.clear()
        task.fields_conflict.clear()
        self._tasks.remove(task)
        suffix = "；运行中的操作已失效" if was_active else ""
        self._append_log(f"{task.port} {reason}{suffix}")

    def _get_selected_chip(self) -> str:
        """返回右上角芯片型号选择值。"""
        return self._chip_combo.currentText().strip()

    def _check_chip_match(self, task: BurnTaskItem) -> bool:
        """校验设备芯片是否匹配右上角选择的型号。"""
        selected = (
            task.run_config.chip if task.run_config is not None else self._get_selected_chip()
        ).lower()
        if selected in ("", "auto"):
            return True
        detected = (task.chip_name or "").lower().replace(" ", "").replace("-", "")
        if not detected or detected in ("未识别", "识别中…"):
            return True  # 未识别的不拦截，由 espefuse --chip 参数控制
        # 只要 detected 包含 selected（如 "esp32-p4" contains "esp32p4"）
        norm_selected = selected.replace(" ", "").replace("-", "")
        return norm_selected in detected or detected in norm_selected

    def _run_task(self, task: BurnTaskItem) -> bool:
        """启动任务：先 READ，再根据预检结果决定 BURN/SKIP。"""
        if not self._is_task_current(task) or task.state != BurnTaskState.WAITING:
            return False
        if task.run_config is None:
            task.run_config = self._capture_run_config()
        if task.run_config is None:
            return False
        # 芯片型号校验
        if not self._check_chip_match(task):
            selected = task.run_config.chip
            task.state = BurnTaskState.SKIPPED
            task.error_message = f"芯片不匹配（检测={task.chip_name}，需要={selected}）"
            self._append_log(f"{task.port} 跳过：{task.error_message}")
            self._refresh_dev_table()
            self._on_task_done(task)
            return False
        if not _tool_backend_available("espefuse"):
            self._append_log(f"{task.port} 未找到 espefuse 后端")
            task.state = BurnTaskState.FAILED
            task.error_message = "未找到 espefuse"
            self._refresh_dev_table()
            self._on_task_done(task)
            return False
        task.state = BurnTaskState.READING
        task.error_message = ""
        task.fields_to_burn.clear()
        task.fields_skipped.clear()
        task.fields_conflict.clear()
        task.read_result.clear()
        task.precheck_identity = ""
        self._refresh_dev_table()
        self._append_log(f"{task.port} 开始读取 eFuse ({task.chip_name})")
        cmd = self._build_espefuse_cmd(task, ["summary", "--format", "json"])
        return self._start_process(task, cmd, self._on_read_finished)

    def _retry_task(self, task: BurnTaskItem) -> None:
        if not self._is_task_current(task):
            return
        self._clear_authorization(task)
        if (
            task.batch_precheck_id is not None
            and task.batch_precheck_id == self._pending_batch_id
            and self._pending_batch_config is not None
        ):
            task.run_config = self._pending_batch_config
        else:
            task.batch_precheck_id = None
            task.run_config = None
        task.state = BurnTaskState.WAITING
        task.error_message = ""
        task.force_burn = False
        self._run_task(task)

    def _force_run_task(self, task: BurnTaskItem) -> None:
        """强制执行：即使字段已经写入也再次烧录。"""
        if not self._is_task_current(task):
            return
        if task.batch_precheck_id is not None:
            self._append_log(f"{task.port} 批量预检期间不能启用强制烧录")
            return
        self._clear_authorization(task)
        task.run_config = None
        task.state = BurnTaskState.WAITING
        task.error_message = ""
        task.force_burn = True
        self._run_task(task)

    def _abort_task(self, task: BurnTaskItem) -> None:
        if not self._is_task_current(task):
            return
        if task.state in (BurnTaskState.BURNING, BurnTaskState.VERIFYING):
            self._append_log(
                f"{task.port} 已进入不可逆阶段，不能普通中断；任务将继续并验证"
            )
            return
        self._kill_task(task)
        self._clear_authorization(task)
        task.state = BurnTaskState.FAILED
        task.error_message = "用户中断"
        self._refresh_dev_table()
        self._on_task_done(task)

    def _authorize_manual_burn(self, task: BurnTaskItem) -> None:
        """Create one explicit, task-scoped authorization from the burn button."""
        if (
            not self._is_task_current(task)
            or task.state != BurnTaskState.READ_OK
            or task.batch_precheck_id is not None
        ):
            return
        task.authorization = BurnAuthorization.MANUAL
        task.authorization_id = self._new_authorization_id()
        task.authorized_identity = task.precheck_identity
        self._do_burn(task)

    def has_irreversible_operation(self) -> bool:
        """Return whether closing the application could interrupt eFuse state."""
        return any(
            task.state in (BurnTaskState.BURNING, BurnTaskState.VERIFYING)
            for task in self._tasks
        )

    def _do_burn(self, task: BurnTaskItem) -> None:
        """Re-read the chip identity before entering the irreversible step."""
        if (
            not self._is_task_current(task)
            or task.state != BurnTaskState.READ_OK
            or not self._is_burn_authorized(task)
            or task.run_config is None
            or not task.precheck_identity
            or task.authorized_identity != task.precheck_identity
            or task.fields_conflict
        ):
            self._clear_authorization(task)
            if self._is_task_current(task):
                task.state = BurnTaskState.FAILED
                task.error_message = "烧录授权、身份或预检状态已失效"
                self._refresh_dev_table()
                self._on_task_done(task)
            return
        if not task.fields_to_burn:
            task.state = BurnTaskState.SKIPPED
            self._refresh_dev_table()
            self._on_task_done(task)
            return
        task.state = BurnTaskState.IDENTITY_CHECK
        self._refresh_dev_table()
        self._append_log(f"{task.port} 烧录前再次核对设备身份")
        cmd = self._build_espefuse_cmd(task, ["summary", "--format", "json"])
        self._start_process(task, cmd, self._on_identity_check_finished)

    def _execute_burn(self, task: BurnTaskItem) -> None:
        """Consume authorization and start the irreversible command exactly once."""
        if (
            not self._is_task_current(task)
            or task.state != BurnTaskState.IDENTITY_CHECK
            or not self._is_burn_authorized(task)
        ):
            return
        self._clear_authorization(task)
        task.state = BurnTaskState.BURNING
        self._refresh_dev_table()
        pairs: list[str] = []
        for f in task.fields_to_burn:
            pairs += [f.name, f.value]
        self._append_log(
            f"{task.port} 烧录 {', '.join(f'{f.name}={f.value}' for f in task.fields_to_burn)}"
        )
        cmd = self._build_espefuse_cmd(task, ["--do-not-confirm", "burn-efuse"] + pairs)
        self._start_process(task, cmd, self._on_burn_finished)

    def _do_verify(self, task: BurnTaskItem) -> None:
        """烧录后验证。"""
        if not self._is_task_current(task) or task.run_config is None:
            return
        task.state = BurnTaskState.VERIFYING
        self._refresh_dev_table()
        self._append_log(f"{task.port} 开始验证")
        cmd = self._build_espefuse_cmd(task, ["summary", "--format", "json"])
        self._start_process(task, cmd, self._on_verify_finished)

    # ── 进程回调 ─────────────────────────────────────────────────────────────

    def _on_read_finished(self, task: BurnTaskItem, exit_code: int, output: str) -> None:
        if not self._is_task_current(task) or task.state != BurnTaskState.READING:
            return
        if exit_code != 0:
            task.state = BurnTaskState.FAILED
            task.error_message = f"读取失败（退出码 {exit_code}）"
            self._append_log(f"{task.port} 读取失败，退出码 {exit_code}")
            self._refresh_dev_table()
            self._on_task_done(task)
            return

        # 解析 JSON
        task.read_result = self._parse_json_output(output)
        if not task.read_result:
            task.state = BurnTaskState.FAILED
            task.error_message = "无法解析 eFuse JSON"
            self._append_log(f"{task.port} JSON 解析失败")
            self._refresh_dev_table()
            self._on_task_done(task)
            return

        if task.run_config is None:
            task.state = BurnTaskState.FAILED
            task.error_message = "作业配置快照已失效"
            self._refresh_dev_table()
            self._on_task_done(task)
            return

        # COM/USB 标识只能发现重枚举；不可逆操作必须绑定芯片自身 MAC。
        task.precheck_identity = extract_stable_device_identity(task.read_result)
        if not task.precheck_identity:
            task.state = BurnTaskState.FAILED
            task.error_message = "无法读取有效的设备唯一标识（Factory MAC），禁止烧录"
            self._append_log(f"{task.port} 预检失败：{task.error_message}")
            self._refresh_dev_table()
            self._on_task_done(task)
            return

        result = evaluate_efuse_precheck(
            task.run_config.fields,
            task.read_result,
            force_burn=task.force_burn,
        )
        task.fields_to_burn = list(result.to_burn)
        task.fields_skipped = list(result.skipped)
        task.fields_conflict = list(result.conflicts)

        burn_n = len(task.fields_to_burn)
        skip_n = len(task.fields_skipped)
        conflict_n = len(task.fields_conflict)
        self._append_log(
            f"{task.port} 预检：待烧 {burn_n} / 跳过 {skip_n} / 冲突 {conflict_n}"
        )

        if conflict_n > 0:
            task.state = BurnTaskState.FAILED
            task.error_message = (
                "存在缺失、不可读、写保护或 OTP 目标冲突，已阻止整个任务"
            )
            self._append_log(
                f"{task.port} 整体阻断：{', '.join(task.fields_conflict)}"
            )
            self._refresh_dev_table()
            self._on_task_done(task)
            return

        if burn_n == 0:
            task.state = BurnTaskState.SKIPPED
            self._append_log(f"{task.port} 全部字段已满足，跳过")
            self._refresh_dev_table()
            self._on_task_done(task)
            return

        task.state = BurnTaskState.READ_OK
        self._refresh_dev_table()

        # 只有本任务仍持有显式授权时，才能进入烧录前身份复核。
        if (
            task.authorization == BurnAuthorization.AUTO
            and self._is_burn_authorized(task)
        ):
            task.authorized_identity = task.precheck_identity
        if self._is_burn_authorized(task):
            self._do_burn(task)
        elif task.batch_precheck_id == self._pending_batch_id:
            QTimer.singleShot(0, self._maybe_confirm_pending_batch)

    def _on_identity_check_finished(
        self,
        task: BurnTaskItem,
        exit_code: int,
        output: str,
    ) -> None:
        if (
            not self._is_task_current(task)
            or task.state != BurnTaskState.IDENTITY_CHECK
        ):
            return
        if exit_code != 0:
            task.state = BurnTaskState.FAILED
            task.error_message = f"烧录前身份读取失败（退出码 {exit_code}）"
            self._append_log(f"{task.port} 身份复核失败，禁止烧录")
            self._refresh_dev_table()
            self._on_task_done(task)
            return

        identity_data = self._parse_json_output(output)
        current_identity = extract_stable_device_identity(identity_data)
        if (
            not current_identity
            or current_identity != task.precheck_identity
            or current_identity != task.authorized_identity
        ):
            task.state = BurnTaskState.FAILED
            task.error_message = "设备身份已变化或无法确认，禁止烧录"
            self._append_log(
                f"{task.port} 身份复核不一致，已阻止不可逆命令"
            )
            self._refresh_dev_table()
            self._on_task_done(task)
            return
        if task.run_config is None or not self._is_burn_authorized(task):
            task.state = BurnTaskState.FAILED
            task.error_message = "烧录授权或作业配置已失效"
            self._refresh_dev_table()
            self._on_task_done(task)
            return

        # 身份复核同时重新做全字段预检，避免两次读取间状态变化后部分烧录。
        result = evaluate_efuse_precheck(
            task.run_config.fields,
            identity_data,
            force_burn=task.force_burn,
        )
        task.read_result = identity_data
        task.fields_to_burn = list(result.to_burn)
        task.fields_skipped = list(result.skipped)
        task.fields_conflict = list(result.conflicts)
        if result.conflicts:
            task.state = BurnTaskState.FAILED
            task.error_message = "身份复核时字段状态冲突，已阻止整个任务"
            self._append_log(
                f"{task.port} 二次预检冲突：{', '.join(result.conflicts)}"
            )
            self._refresh_dev_table()
            self._on_task_done(task)
            return
        if result.all_satisfied:
            task.state = BurnTaskState.SKIPPED
            self._append_log(f"{task.port} 二次预检确认全部字段已满足，跳过")
            self._refresh_dev_table()
            self._on_task_done(task)
            return

        self._append_log(f"{task.port} 设备身份复核通过")
        self._execute_burn(task)

    def _on_burn_finished(self, task: BurnTaskItem, exit_code: int, output: str) -> None:
        if not self._is_task_current(task) or task.state != BurnTaskState.BURNING:
            return
        if exit_code != 0:
            task.state = BurnTaskState.FAILED
            task.error_message = f"烧录失败（退出码 {exit_code}）"
            self._append_log(f"{task.port} 烧录失败，退出码 {exit_code}")
            self._refresh_dev_table()
            self._on_task_done(task)
            return
        self._append_log(f"{task.port} 烧录完成，开始验证")
        self._do_verify(task)

    def _on_verify_finished(self, task: BurnTaskItem, exit_code: int, output: str) -> None:
        if not self._is_task_current(task) or task.state != BurnTaskState.VERIFYING:
            return
        if exit_code != 0:
            task.state = BurnTaskState.FAILED
            task.error_message = f"验证读取失败（退出码 {exit_code}）"
            self._append_log(f"{task.port} 验证读取失败")
            self._refresh_dev_table()
            self._on_task_done(task)
            return

        verify_data = self._parse_json_output(output)
        if task.run_config is None:
            mismatches = ["作业配置快照已失效"]
        else:
            mismatches = list(
                evaluate_efuse_verification(task.run_config.fields, verify_data)
            )
        verify_identity = extract_stable_device_identity(verify_data)
        if not verify_identity or verify_identity != task.precheck_identity:
            mismatches.append("设备身份验证失败")

        if mismatches:
            task.state = BurnTaskState.FAILED
            task.error_message = "验证不通过: " + "; ".join(mismatches)
            self._append_log(f"{task.port} 验证失败: {'; '.join(mismatches)}")
        else:
            task.state = BurnTaskState.DONE_OK
            self._append_log(f"{task.port} 验证通过 ✓")
        self._refresh_dev_table()
        self._on_task_done(task)

    def _on_task_done(self, task: BurnTaskItem) -> None:
        """任务结束（成功/跳过/失败）后调度下一个。"""
        self._clear_authorization(task)
        QTimer.singleShot(0, self._schedule_next)
        QTimer.singleShot(0, self._maybe_confirm_pending_batch)

    # ── QProcess 管理 ────────────────────────────────────────────────────────

    def _build_espefuse_cmd(self, task: BurnTaskItem, extra_args: list[str]) -> list[str]:
        config = task.run_config
        chip_arg = config.chip if config is not None else "auto"
        baud = config.baud if config is not None else FLASH_BAUD_DEFAULT
        return _build_tool_command(
            "espefuse",
            "--chip", chip_arg,
            "--port", task.port,
            "--baud", baud,
            *extra_args,
        )

    def _start_process(
        self,
        task: BurnTaskItem,
        cmd: list[str],
        on_finished: callable,
    ) -> bool:
        if not self._is_task_current(task) or task.process is not None:
            return False
        process = QProcess(self)
        task.process_token += 1
        process_token = task.process_token
        task.process = process
        process.setProgram(cmd[0])
        process.setArguments(cmd[1:])
        process.setWorkingDirectory(str(TOOL_DIR))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        _inject_local_esptool_pythonpath(env)
        process.setProcessEnvironment(env)

        buf: list[str] = []
        process.readyReadStandardOutput.connect(lambda: buf.append(
            decode_process_output(bytes(process.readAllStandardOutput()))
        ))
        process.finished.connect(
            lambda code, _status: self._on_process_finished(
                task,
                process,
                process_token,
                code,
                "".join(buf),
                on_finished,
            )
        )
        process.start()
        if not process.waitForStarted(5000):
            task.process_token += 1
            task.process = None
            process.blockSignals(True)
            process.kill()
            process.deleteLater()
            task.state = BurnTaskState.FAILED
            task.error_message = "进程启动失败"
            self._append_log(f"{task.port} 进程启动失败")
            self._refresh_dev_table()
            self._on_task_done(task)
            return False
        return True

    def _on_process_finished(
        self,
        task: BurnTaskItem,
        process: QProcess,
        process_token: int,
        exit_code: int,
        output: str,
        callback: callable,
    ) -> None:
        if (
            task.process is not process
            or task.process_token != process_token
            or not self._is_task_current(task)
        ):
            process.deleteLater()
            return
        task.process = None
        process.deleteLater()
        callback(task, exit_code, output)

    def _kill_task(self, task: BurnTaskItem) -> None:
        process = task.process
        task.process_token += 1
        task.process = None
        if process is not None:
            process.blockSignals(True)
            process.kill()
            process.waitForFinished(500)
            process.deleteLater()

    # ── 解析 ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_json_output(text: str) -> dict[str, dict]:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            return {}
        try:
            data = json.loads(text[start:end])
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    # ── 日志 ─────────────────────────────────────────────────────────────────

    def _append_log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.appendPlainText(f"[{ts}] {msg}")
        c = self._log.textCursor()
        c.movePosition(QTextCursor.MoveOperation.End)
        self._log.setTextCursor(c)
