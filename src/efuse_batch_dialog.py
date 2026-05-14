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
    _inject_local_esptool_pythonpath,
    _tool_backend_available,
    resolve_chip_arg,
)
from .dialog_memory import get_open_file_name
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
    BURNING = "烧录中"
    VERIFYING = "验证中"
    DONE_OK = "完成"
    SKIPPED = "已跳过"
    FAILED = "失败"


@dataclass
class BurnTaskItem:
    device_id: str
    port: str
    chip_name: str = "未识别"
    state: BurnTaskState = BurnTaskState.WAITING
    read_result: dict[str, dict] = field(default_factory=dict)
    fields_to_burn: list[EfuseFieldConfig] = field(default_factory=list)
    fields_skipped: list[str] = field(default_factory=list)
    fields_conflict: list[str] = field(default_factory=list)
    error_message: str = ""
    force_burn: bool = False
    process: QProcess | None = None


# ── 辅助 ─────────────────────────────────────────────────────────────────────

_STATE_COLORS: dict[BurnTaskState, str] = {
    BurnTaskState.WAITING: "#6b7a94",
    BurnTaskState.READING: "#b45309",
    BurnTaskState.READ_OK: "#2560e0",
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
    BurnTaskState.BURNING: "⏳",
    BurnTaskState.VERIFYING: "⏳",
    BurnTaskState.DONE_OK: "✓",
    BurnTaskState.SKIPPED: "~",
    BurnTaskState.FAILED: "✗",
}


def _normalize_efuse_value(v: str) -> str:
    """Normalize an eFuse value string for comparison."""
    v = v.strip().lower()
    if v in ("true", "enable", "enabled"):
        return "1"
    if v in ("false", "disable", "disabled", "none", ""):
        return "0"
    if v.startswith("0x"):
        try:
            return str(int(v, 16))
        except ValueError:
            pass
    return v


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
        self._auto_mode = False
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

    # ── 热插拔轮询 ───────────────────────────────────────────────────────────

    def _toggle_auto_detect(self, enabled: bool) -> None:
        self._auto_burn_btn.setText(f"自动熔丝：{'开' if enabled else '关'}")
        self._auto_mode = enabled
        if enabled:
            self._poll_timer.start()
            self._poll_ports()
        else:
            self._poll_timer.stop()

    def _poll_ports(self) -> None:
        current_ports: dict[str, str] = {}
        for pi in list_ports.comports():
            if pi.description and re.search(r"通信端口|Communications Port", pi.description):
                continue
            current_ports[pi.device] = (pi.description or "串口设备")

        known_ports = {t.port for t in self._tasks}

        # 新接入
        for port in sorted(current_ports.keys() - known_ports):
            chip = _identify_chip(port)
            task = BurnTaskItem(
                device_id=port,
                port=port,
                chip_name=chip or "识别中…",
            )
            self._tasks.append(task)
            self._append_log(f"{port} 接入 ({task.chip_name})")
            if self._auto_mode and chip:
                QTimer.singleShot(200, lambda t=task: self._run_task(t))

        # 断开
        for port in known_ports - current_ports.keys():
            task = self._find_task(port)
            if task is None:
                continue
            if task.state in (BurnTaskState.WAITING, BurnTaskState.DONE_OK, BurnTaskState.SKIPPED):
                self._tasks.remove(task)
                self._append_log(f"{port} 已断开，移除")
            elif task.process is not None:
                self._kill_task(task)
                task.state = BurnTaskState.FAILED
                task.error_message = "设备已断开"
                self._append_log(f"{port} 已断开（任务中），标记失败")

        self._refresh_dev_table()

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
            if task.state in (BurnTaskState.READ_OK, BurnTaskState.BURNING, BurnTaskState.VERIFYING,
                              BurnTaskState.DONE_OK, BurnTaskState.SKIPPED):
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
            detail = task.error_message
            if not detail and task.state == BurnTaskState.SKIPPED:
                if task.fields_conflict:
                    detail = "写保护冲突: " + ", ".join(task.fields_conflict)
                else:
                    detail = "全部字段已满足"
            elif not detail and task.fields_conflict:
                detail = "写保护冲突: " + ", ".join(task.fields_conflict)
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
            elif task.state in (BurnTaskState.READING, BurnTaskState.BURNING, BurnTaskState.VERIFYING):
                stop_b = QPushButton("停止")
                stop_b.setObjectName("dangerButton")
                stop_b.setStyleSheet(_btn_ss)
                stop_b.clicked.connect(lambda _=False, t=task: self._abort_task(t))
                btn_lay.addWidget(stop_b)
            elif task.state == BurnTaskState.READ_OK:
                burn_b = QPushButton("烧录")
                burn_b.setObjectName("primaryButton")
                burn_b.setStyleSheet(_btn_ss)
                burn_b.clicked.connect(lambda _=False, t=task: self._do_burn(t))
                btn_lay.addWidget(burn_b)
            elif task.state == BurnTaskState.SKIPPED:
                re_b = QPushButton("开始")
                re_b.setStyleSheet(
                    _btn_ss + "background:#fef3c7; color:#92400e; border:1px solid #f59e0b;"
                )
                re_b.clicked.connect(lambda _=False, t=task: self._force_run_task(t))
                btn_lay.addWidget(re_b)
            tbl.setCellWidget(row, 5, btn_w)

        self._dev_count_lbl.setText(f"{len(self._tasks)} 台")

    # ── 全局按钮 ─────────────────────────────────────────────────────────────

    def _start_all(self) -> None:
        fields = self._get_enabled_fields()
        if not fields:
            QMessageBox.warning(self, "提示", "请至少配置一个已启用的 eFuse 字段。")
            return
        if not self._tasks:
            QMessageBox.information(self, "提示", "设备队列为空，请先扫描或接入设备。")
            return
        reply = QMessageBox.warning(
            self, "确认批量烧录",
            f"即将对 {len(self._tasks)} 台设备执行 eFuse 烧录。\n"
            f"启用字段: {', '.join(f.name for f in fields)}\n\n"
            "eFuse 一经烧写不可撤销，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._auto_mode = True
        for task in self._tasks:
            if task.state == BurnTaskState.WAITING:
                self._schedule_next()
                break

    def _schedule_next(self) -> None:
        running = sum(1 for t in self._tasks if t.state in (
            BurnTaskState.READING, BurnTaskState.BURNING, BurnTaskState.VERIFYING))
        for task in self._tasks:
            if running >= self.MAX_CONCURRENT:
                break
            if task.state == BurnTaskState.WAITING:
                self._run_task(task)
                running += 1

    def _clear_done(self) -> None:
        self._tasks = [t for t in self._tasks if t.state not in (
            BurnTaskState.DONE_OK, BurnTaskState.SKIPPED)]
        self._refresh_dev_table()

    def _clear_all_devices(self) -> None:
        """清空所有设备（停止运行中的任务）。"""
        for task in self._tasks:
            if task.process is not None:
                self._kill_task(task)
        self._tasks.clear()
        self._refresh_dev_table()
        self._append_log("已清空所有设备")

    def _stop_all(self) -> None:
        for task in self._tasks:
            if task.process is not None:
                self._kill_task(task)
                task.state = BurnTaskState.FAILED
                task.error_message = "用户停止"
        self._auto_mode = False
        self._poll_timer.stop()
        self._auto_burn_btn.blockSignals(True)
        self._auto_burn_btn.setChecked(False)
        self._auto_burn_btn.setText("自动熔丝：关")
        self._auto_burn_btn.blockSignals(False)
        self._refresh_dev_table()

    # ── 状态机 ───────────────────────────────────────────────────────────────

    def _find_task(self, port: str) -> BurnTaskItem | None:
        for t in self._tasks:
            if t.port == port:
                return t
        return None

    def _get_selected_chip(self) -> str:
        """返回右上角芯片型号选择值。"""
        return self._chip_combo.currentText().strip()

    def _check_chip_match(self, task: BurnTaskItem) -> bool:
        """校验设备芯片是否匹配右上角选择的型号。"""
        selected = self._get_selected_chip().lower()
        detected = (task.chip_name or "").lower().replace(" ", "").replace("-", "")
        if not detected or detected in ("未识别", "识别中…"):
            return True  # 未识别的不拦截，由 espefuse --chip 参数控制
        # 只要 detected 包含 selected（如 "esp32-p4" contains "esp32p4"）
        norm_selected = selected.replace(" ", "").replace("-", "")
        return norm_selected in detected or detected in norm_selected

    def _run_task(self, task: BurnTaskItem) -> None:
        """启动任务：先 READ，再根据预检结果决定 BURN/SKIP。"""
        fields = self._get_enabled_fields()
        if not fields:
            self._append_log(f"{task.port} 没有已启用的字段，跳过")
            return
        # 芯片型号校验
        if not self._check_chip_match(task):
            selected = self._get_selected_chip()
            task.state = BurnTaskState.SKIPPED
            task.error_message = f"芯片不匹配（检测={task.chip_name}，需要={selected}）"
            self._append_log(f"{task.port} 跳过：{task.error_message}")
            self._refresh_dev_table()
            return
        if not _tool_backend_available("espefuse"):
            self._append_log(f"{task.port} 未找到 espefuse 后端")
            task.state = BurnTaskState.FAILED
            task.error_message = "未找到 espefuse"
            self._refresh_dev_table()
            return
        task.state = BurnTaskState.READING
        task.error_message = ""
        task.fields_to_burn.clear()
        task.fields_skipped.clear()
        task.fields_conflict.clear()
        task.read_result.clear()
        self._refresh_dev_table()
        self._append_log(f"{task.port} 开始读取 eFuse ({task.chip_name})")
        cmd = self._build_espefuse_cmd(task, ["summary", "--format", "json"])
        self._start_process(task, cmd, self._on_read_finished)

    def _retry_task(self, task: BurnTaskItem) -> None:
        task.state = BurnTaskState.WAITING
        task.error_message = ""
        task.force_burn = False
        self._run_task(task)

    def _force_run_task(self, task: BurnTaskItem) -> None:
        """强制执行：即使字段已经写入也再次烧录。"""
        task.state = BurnTaskState.WAITING
        task.error_message = ""
        task.force_burn = True
        self._run_task(task)

    def _abort_task(self, task: BurnTaskItem) -> None:
        self._kill_task(task)
        task.state = BurnTaskState.FAILED
        task.error_message = "用户中断"
        self._refresh_dev_table()

    def _do_burn(self, task: BurnTaskItem) -> None:
        """执行烧录步骤。"""
        if not task.fields_to_burn:
            task.state = BurnTaskState.SKIPPED
            self._refresh_dev_table()
            self._on_task_done(task)
            return
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
        task.state = BurnTaskState.VERIFYING
        self._refresh_dev_table()
        self._append_log(f"{task.port} 开始验证")
        cmd = self._build_espefuse_cmd(task, ["summary", "--format", "json"])
        self._start_process(task, cmd, self._on_verify_finished)

    # ── 进程回调 ─────────────────────────────────────────────────────────────

    def _on_read_finished(self, task: BurnTaskItem, exit_code: int, output: str) -> None:
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

        # 预检
        fields = self._get_enabled_fields()
        for cfg in fields:
            info = task.read_result.get(cfg.name)
            if info is None:
                task.fields_conflict.append(cfg.name)
                continue
            if not info.get("writeable", True):
                # 已写保护
                current = _normalize_efuse_value(str(info.get("value", "0")))
                target = _normalize_efuse_value(cfg.value)
                if current == target:
                    task.fields_skipped.append(cfg.name)
                else:
                    task.fields_conflict.append(cfg.name)
                continue
            current = _normalize_efuse_value(str(info.get("value", "0")))
            target = _normalize_efuse_value(cfg.value)
            if current == target and not task.force_burn:
                task.fields_skipped.append(cfg.name)
            else:
                task.fields_to_burn.append(cfg)

        burn_n = len(task.fields_to_burn)
        skip_n = len(task.fields_skipped)
        conflict_n = len(task.fields_conflict)
        self._append_log(
            f"{task.port} 预检：待烧 {burn_n} / 跳过 {skip_n} / 冲突 {conflict_n}"
        )

        if conflict_n > 0 and burn_n == 0:
            task.state = BurnTaskState.FAILED
            task.error_message = "存在写保护冲突"
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

        # 自动模式直接烧录
        if self._auto_mode:
            self._do_burn(task)

    def _on_burn_finished(self, task: BurnTaskItem, exit_code: int, output: str) -> None:
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
        if exit_code != 0:
            task.state = BurnTaskState.FAILED
            task.error_message = f"验证读取失败（退出码 {exit_code}）"
            self._append_log(f"{task.port} 验证读取失败")
            self._refresh_dev_table()
            self._on_task_done(task)
            return

        verify_data = self._parse_json_output(output)
        mismatches: list[str] = []
        for cfg in task.fields_to_burn:
            info = verify_data.get(cfg.name)
            if info is None:
                mismatches.append(f"{cfg.name}: 字段缺失")
                continue
            current = _normalize_efuse_value(str(info.get("value", "0")))
            target = _normalize_efuse_value(cfg.value)
            if current != target:
                mismatches.append(f"{cfg.name}: 期望={cfg.value} 实际={info.get('value')}")

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
        self._schedule_next()

    # ── QProcess 管理 ────────────────────────────────────────────────────────

    def _build_espefuse_cmd(self, task: BurnTaskItem, extra_args: list[str]) -> list[str]:
        chip_arg = self._chip_combo.currentText().strip() or "auto"
        baud = self._baud_combo.currentText().strip() or "115200"
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
    ) -> None:
        if task.process is not None:
            return
        process = QProcess(self)
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
            bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        ))
        process.finished.connect(
            lambda code, _status: self._on_process_finished(task, code, "".join(buf), on_finished)
        )
        process.start()
        if not process.waitForStarted(5000):
            task.state = BurnTaskState.FAILED
            task.error_message = "进程启动失败"
            task.process = None
            self._append_log(f"{task.port} 进程启动失败")
            self._refresh_dev_table()

    def _on_process_finished(
        self,
        task: BurnTaskItem,
        exit_code: int,
        output: str,
        callback: callable,
    ) -> None:
        if task.process:
            task.process.deleteLater()
            task.process = None
        callback(task, exit_code, output)

    def _kill_task(self, task: BurnTaskItem) -> None:
        if task.process:
            task.process.kill()
            task.process.deleteLater()
            task.process = None

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
