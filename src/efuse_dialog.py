import json
import re

from PyQt6.QtCore import QProcess, QProcessEnvironment, Qt
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .styles import BASE_STYLESHEET

from .constants import (
    TOOL_DIR,
    EFUSE_CHIP_PRESETS,
    _build_tool_command,
    _inject_local_esptool_pythonpath,
    resolve_chip_arg,
)
from .models import DeviceInfo


class EFuseDialog(QDialog):
    """Per-device eFuse read (table) / burn dialog."""

    # 按芯片型号分组的快捷预设；从 config.yaml 加载，格式：{chip_key: [(label, name, value), ...]}
    CHIP_PRESETS: dict[str, list[tuple[str, str, str]]] = EFUSE_CHIP_PRESETS

    def __init__(
        self,
        device: DeviceInfo,
        baud: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.device = device
        self.baud = baud
        self.process: QProcess | None = None
        self._read_buf: list[str] = []
        self._is_reading = False
        self._all_rows: list[tuple[str, str, str, bool, bool]] = []
        self.setWindowTitle(f"eFuse — {device.port}")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self.resize(920, 680)
        self._init_ui()
        self._apply_style()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # 标题行
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        port_lbl = QLabel(self.device.port)
        port_lbl.setObjectName("portBadge")
        chip_lbl = QLabel(self.device.chip_name or "未识别")
        chip_lbl.setObjectName("deviceSummary")
        self.read_btn = QPushButton("读取 eFuse")
        self.read_btn.setObjectName("primaryButton")
        self.stop_read_btn = QPushButton("停止读取")
        self.stop_read_btn.setObjectName("dangerButton")
        self.stop_read_btn.setEnabled(False)
        hdr.addWidget(port_lbl)
        hdr.addWidget(chip_lbl)
        hdr.addStretch(1)
        hdr.addWidget(self.read_btn)
        hdr.addWidget(self.stop_read_btn)
        root.addLayout(hdr)

        # ── 读取区 — 表格 ──────────────────────────────────────────────────────
        read_frame = QFrame()
        read_frame.setObjectName("sectionFrame")
        read_vbox = QVBoxLayout(read_frame)
        read_vbox.setContentsMargins(12, 10, 12, 10)
        read_vbox.setSpacing(8)

        tbl_hdr = QHBoxLayout()
        tbl_hdr.setSpacing(8)
        tbl_ttl = QLabel("eFuse 字段一览")
        tbl_ttl.setObjectName("sectionTitle")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索字段名 / 描述…")
        self.search_edit.setFixedWidth(200)
        self.only_burned_btn = QPushButton("仅已熔丝")
        self.only_burned_btn.setCheckable(True)
        self.only_burned_btn.setObjectName("filterButton")
        self.row_count_lbl = QLabel('点击"读取 eFuse"加载')
        self.row_count_lbl.setObjectName("hintLabel")
        tbl_hdr.addWidget(tbl_ttl)
        tbl_hdr.addWidget(self.row_count_lbl)
        tbl_hdr.addStretch(1)
        tbl_hdr.addWidget(self.search_edit)
        tbl_hdr.addWidget(self.only_burned_btn)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["状态", "读写", "字段名", "当前值", "描述"])
        hv = self.table.horizontalHeader()
        hv.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hv.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hv.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hv.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 80)
        self.table.setColumnWidth(1, 66)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        vh = self.table.verticalHeader()
        vh.setDefaultSectionSize(26)
        vh.hide()
        self.table.setMinimumHeight(220)

        read_vbox.addLayout(tbl_hdr)
        read_vbox.addWidget(self.table)

        # ── 烧写区 ─────────────────────────────────────────────────────────────
        burn_frame = QFrame()
        burn_frame.setObjectName("sectionFrame")
        burn_vbox = QVBoxLayout(burn_frame)
        burn_vbox.setContentsMargins(12, 10, 12, 10)
        burn_vbox.setSpacing(8)

        burn_ttl = QLabel("烧写 eFuse")
        burn_ttl.setObjectName("sectionTitle")

        preset_row = QHBoxLayout()
        preset_row.setSpacing(6)
        preset_lbl = QLabel("快捷预设")
        preset_lbl.setObjectName("configLabel")
        # 按 chip_name 匹配预设列表
        chip_key = (self.device.chip_name or "").lower()
        active_presets = next(
            (v for k, v in self.CHIP_PRESETS.items() if k in chip_key), []
        )
        if active_presets:
            preset_row.addWidget(preset_lbl)
            for lbl, name, val, *_ in active_presets:
                b = QPushButton(lbl)
                b.setObjectName("presetButton")
                b.clicked.connect(lambda _=False, n=name, v=val: self._apply_preset(n, v))
                preset_row.addWidget(b)
            preset_row.addStretch(1)

        fields_row = QHBoxLayout()
        fields_row.setSpacing(8)
        n_lbl = QLabel("字段名")
        n_lbl.setObjectName("configLabel")
        self.efuse_name_edit = QLineEdit()
        self.efuse_name_edit.setPlaceholderText("例如 USB_EXCHG_PINS  — 双击表格行自动填入")
        v_lbl = QLabel("值")
        v_lbl.setObjectName("configLabel")
        self.efuse_value_edit = QLineEdit()
        self.efuse_value_edit.setFixedWidth(80)
        self.efuse_value_edit.setPlaceholderText("例如 1")
        fields_row.addWidget(n_lbl)
        fields_row.addWidget(self.efuse_name_edit, 1)
        fields_row.addWidget(v_lbl)
        fields_row.addWidget(self.efuse_value_edit)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        warn_lbl = QLabel("⚠ eFuse 一经烧写不可撤销，请三思")
        warn_lbl.setObjectName("warnLabel")
        self.burn_btn = QPushButton("执行烧写")
        self.burn_btn.setObjectName("primaryButton")
        self.stop_burn_btn = QPushButton("停止")
        self.stop_burn_btn.setObjectName("dangerButton")
        self.stop_burn_btn.setEnabled(False)
        action_row.addWidget(warn_lbl)
        action_row.addStretch(1)
        action_row.addWidget(self.burn_btn)
        action_row.addWidget(self.stop_burn_btn)

        self.burn_log = QPlainTextEdit()
        self.burn_log.setReadOnly(True)
        self.burn_log.setMaximumBlockCount(400)
        self.burn_log.setFixedHeight(90)
        self.burn_log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        burn_vbox.addWidget(burn_ttl)
        burn_vbox.addLayout(preset_row)
        burn_vbox.addLayout(fields_row)
        burn_vbox.addLayout(action_row)
        burn_vbox.addWidget(self.burn_log)

        root.addWidget(read_frame, 1)
        root.addWidget(burn_frame)

        # 信号
        self.read_btn.clicked.connect(self._start_read_summary)
        self.stop_read_btn.clicked.connect(self._stop_process)
        self.burn_btn.clicked.connect(self._burn_efuse)
        self.stop_burn_btn.clicked.connect(self._stop_process)
        self.search_edit.textChanged.connect(self._filter_table)
        self.only_burned_btn.toggled.connect(self._filter_table)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)

    # ── 表格工具 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _is_burned(value: str) -> bool:
        v = value.strip().lower()
        if v in ("false", "none", "disabled", "disable", "user", "", "0"):
            return False
        if re.match(r"^0x0+$", v):
            return False
        if re.match(r"^0+$", v):
            return False
        if v.startswith("0b") and set(v[2:]) <= {"0"}:
            return False
        # all-zero byte arrays like "00 00 00 ..."
        if re.match(r"^(00 )*00$", v):
            return False
        return True

    @staticmethod
    def _parse_summary_json(text: str) -> list[tuple[str, str, str, bool, bool]]:
        """Parse espefuse summary --format json output.

        Locates the outermost JSON object in mixed stdout/stderr output and
        returns a list of (name, display_value, description, readable, writeable).
        """
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            return []
        try:
            data = json.loads(text[start:end])
        except json.JSONDecodeError:
            return []
        results: list[tuple[str, str, str, bool, bool]] = []
        for name, info in data.items():
            if not isinstance(info, dict):
                continue
            raw = info.get("value")
            display = str(raw) if raw is not None else "0"
            desc = str(info.get("description", ""))
            readable = bool(info.get("readable", True))
            writeable = bool(info.get("writeable", True))
            results.append((name, display, desc, readable, writeable))
        return results

    @staticmethod
    def _parse_summary_text(text: str) -> list[tuple[str, str, str, bool, bool]]:
        """Fallback: parse plain-text espefuse summary output via regex.

        Returns list of (name, value, description, readable, writeable).
        """
        results: list[tuple[str, str, str, bool, bool]] = []
        # Format: NAME (BLOCKX)   description...   = value  R/W (optional_canonical)
        field_re = re.compile(
            r"^\s*([A-Z][A-Z0-9_]{2,})\s+\(([^)]*)\)(.*?)\s+=\s*(.+?)\s+(R/W|R/-|-/W|-/-|R-)"
        )
        for line in text.splitlines():
            if "read_regs" in line:
                continue
            m = field_re.match(line)
            if not m:
                continue
            name = m.group(1).strip()
            desc = m.group(3).strip()
            value = m.group(4).strip()
            perms = m.group(5).strip()   # "R/W", "R/-", "-/W", "-/-"
            readable = perms[0] == "R"
            writeable = len(perms) >= 3 and perms[2] == "W"
            results.append((name, value, desc, readable, writeable))
        return results

    def _populate_table(self, rows: list[tuple[str, str, str, bool, bool]]) -> None:
        self._all_rows = rows
        self._filter_table()

    @staticmethod
    def _make_badge_cell(label: str, obj_name: str) -> QWidget:
        lbl = QLabel(label)
        lbl.setObjectName(obj_name)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(2, 1, 2, 1)
        lay.addWidget(lbl)
        return w

    def _filter_table(self) -> None:
        rows = self._all_rows
        query = self.search_edit.text().strip().lower()
        only_burned = self.only_burned_btn.isChecked()

        filtered: list[tuple[str, str, str, bool, bool, bool]] = []
        for name, value, desc, readable, writeable in rows:
            burned = self._is_burned(value)
            if only_burned and not burned:
                continue
            if query and query not in name.lower() and query not in desc.lower():
                continue
            filtered.append((name, value, desc, readable, writeable, burned))

        self.table.setRowCount(len(filtered))
        for row, (name, value, desc, readable, writeable, burned) in enumerate(filtered):
            # col 0 — 状态 badge
            self.table.setCellWidget(
                row, 0,
                self._make_badge_cell(
                    "● 已熔丝" if burned else "○ 未熔丝",
                    "burnedBadge" if burned else "unburnedBadge",
                )
            )

            # col 1 — 读写 badge
            if readable and writeable:
                rw_text, rw_obj = "R/W", "rwBadgeRW"
            elif readable:
                rw_text, rw_obj = "R/-", "rwBadgeR"
            elif writeable:
                rw_text, rw_obj = "-/W", "rwBadgeW"
            else:
                rw_text, rw_obj = "-/-", "rwBadgeNo"
            self.table.setCellWidget(row, 1, self._make_badge_cell(rw_text, rw_obj))

            # col 2 — 字段名
            name_item = QTableWidgetItem(name)
            bold_font = QFont()
            bold_font.setBold(burned)
            name_item.setFont(bold_font)
            self.table.setItem(row, 2, name_item)

            # col 3 — 当前值
            value_item = QTableWidgetItem(value)
            value_item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
            self.table.setItem(row, 3, value_item)

            # col 4 — 描述
            self.table.setItem(row, 4, QTableWidgetItem(desc))

        total = len(rows)
        shown = len(filtered)
        self.row_count_lbl.setText(
            f"{total} 个字段，显示 {shown} 个" if rows else '点击"读取 eFuse"加载'
        )

    def _on_row_double_clicked(self, row: int, _col: int) -> None:
        item = self.table.item(row, 2)   # col 2 = 字段名
        if item:
            self.efuse_name_edit.setText(item.text())
            self.efuse_value_edit.setText("1")
            self.efuse_value_edit.setFocus()
            self.efuse_value_edit.selectAll()

    def _apply_preset(self, name: str, value: str) -> None:
        self.efuse_name_edit.setText(name)
        self.efuse_value_edit.setText(value)

    # ── 进程管理 ─────────────────────────────────────────────────────────────

    def _chip_arg(self) -> str:
        """Map DeviceInfo.chip_name to the espefuse --chip argument."""
        return resolve_chip_arg(self.device.chip_name)

    def _build_base_cmd(self) -> list[str]:
        return _build_tool_command(
            "espefuse",
            "--chip", self._chip_arg(),
            "--port", self.device.port,
            "--baud", self.baud,
        )

    def _start_read_summary(self) -> None:
        if self.process is not None:
            return
        self._read_buf.clear()
        self._is_reading = True
        self.row_count_lbl.setText("正在读取 eFuse…")
        self.burn_log.clear()
        cmd = self._build_base_cmd() + ["summary", "--format", "json"]
        self._run_cmd(cmd, log=self.burn_log)

    def _run_cmd(self, cmd: list[str], log: QPlainTextEdit | None) -> None:
        if self.process is not None:
            return
        process = QProcess(self)
        self.process = process
        process.setProgram(cmd[0])
        process.setArguments(cmd[1:])
        process.setWorkingDirectory(str(TOOL_DIR))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        _inject_local_esptool_pythonpath(env)
        process.setProcessEnvironment(env)
        process.readyReadStandardOutput.connect(lambda l=log: self._on_output(l))
        process.finished.connect(self._on_finished)
        self._set_busy(True)
        process.start()
        if not process.waitForStarted(3000):
            msg = "进程启动失败，请检查 espefuse 调用环境。"
            self.burn_log.appendPlainText(msg)
            self.row_count_lbl.setText("启动失败")
            self.process = None
            self._is_reading = False
            self._set_busy(False)

    def _on_output(self, log: QPlainTextEdit | None) -> None:
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if self._is_reading:
            self._read_buf.append(data)
        if log is not None:
            log.appendPlainText(data.rstrip())
            c = log.textCursor()
            c.movePosition(QTextCursor.MoveOperation.End)
            log.setTextCursor(c)

    def _on_finished(self, exit_code: int, _) -> None:
        if self._is_reading:
            full_text = "".join(self._read_buf)
            rows = self._parse_summary_json(full_text)
            if not rows:
                rows = self._parse_summary_text(full_text)
            self._populate_table(rows)
            self._is_reading = False
            if exit_code != 0:
                self.burn_log.appendPlainText(f"[读取失败，退出码: {exit_code}]")
                if not rows:
                    self.row_count_lbl.setText(f"读取失败（退出码 {exit_code}），请检查串口连接")
        else:
            self.burn_log.appendPlainText(f"[完成，退出码: {exit_code}]")
        if self.process:
            self.process.deleteLater()
            self.process = None
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self.read_btn.setEnabled(not busy)
        self.stop_read_btn.setEnabled(busy and self._is_reading)
        self.burn_btn.setEnabled(not busy)
        self.stop_burn_btn.setEnabled(busy and not self._is_reading)

    def _burn_efuse(self) -> None:
        name = self.efuse_name_edit.text().strip()
        value = self.efuse_value_edit.text().strip()
        if not name or not value:
            QMessageBox.warning(self, "提示", "请填写字段名和值。")
            return
        reply = QMessageBox.warning(
            self,
            "确认烧写",
            f"即将对 {self.device.port} 执行：\n\n  {name} = {value}\n\n此操作不可逆，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._is_reading = False
        self.burn_log.clear()
        self.burn_log.appendPlainText(f"[烧写] {name} = {value}")
        cmd = self._build_base_cmd() + ["--do-not-confirm", "burn-efuse", name, value]
        self._run_cmd(cmd, log=self.burn_log)

    def _stop_process(self) -> None:
        if self.process:
            self.process.kill()
            self.process.deleteLater()
            self.process = None
        self._is_reading = False
        self._set_busy(False)

    # ── 样式 ─────────────────────────────────────────────────────────────────

    def _apply_style(self) -> None:
        self.setStyleSheet(
            BASE_STYLESHEET
            + """
            QDialog { background: #f0f2f5; }
            QFrame#sectionFrame {
                background: #ffffff;
                border: 1px solid #e0e4ea;
                border-radius: 10px;
            }
            QLabel#sectionTitle { font-size: 13px; }
            QLabel#hintLabel { color: #9aa5bc; font-size: 12px; }
            QLabel#warnLabel { color: #b45309; font-size: 12px; font-weight: 600; }
            QLabel#portBadge { border-radius: 999px; }
            QLabel#burnedBadge {
                background: #fef3c7; border: 1px solid #f6c843;
                border-radius: 999px; padding: 1px 9px;
                color: #92400e; font-size: 11px; font-weight: 700;
            }
            QLabel#unburnedBadge {
                background: #f0f2f5; border: 1px solid #d0d5df;
                border-radius: 999px; padding: 1px 9px;
                color: #9aa5bc; font-size: 11px;
            }
            QLabel#rwBadgeRW {
                background: #e6f4ea; border: 1px solid #86c98a;
                border-radius: 999px; padding: 1px 9px;
                color: #1a6b28; font-size: 11px; font-weight: 600;
            }
            QLabel#rwBadgeR {
                background: #e8f0fe; border: 1px solid #93b4f8;
                border-radius: 999px; padding: 1px 9px;
                color: #1a4ea6; font-size: 11px; font-weight: 600;
            }
            QLabel#rwBadgeW {
                background: #fff8e1; border: 1px solid #f6c843;
                border-radius: 999px; padding: 1px 9px;
                color: #92400e; font-size: 11px; font-weight: 600;
            }
            QLabel#rwBadgeNo {
                background: #fce8e8; border: 1px solid #f09090;
                border-radius: 999px; padding: 1px 9px;
                color: #991b1b; font-size: 11px; font-weight: 600;
            }
            QTableWidget {
                background: #ffffff;
                border: none;
                gridline-color: #f0f2f5;
                font-size: 12px;
                outline: none;
            }
            QTableWidget::item { padding: 3px 8px; border: none; }
            QTableWidget::item:selected { background: #e8edf7; color: #1a2333; }
            QTableWidget::item:alternate { background: #fafbfc; }
            QHeaderView::section {
                background: #f5f7fa;
                border: none;
                border-bottom: 1px solid #e0e4ea;
                border-right: 1px solid #e0e4ea;
                padding: 5px 8px;
                font-weight: 700;
                font-size: 12px;
                color: #6b7a94;
            }
            QPlainTextEdit {
                background: #f8f9fb;
                color: #374151;
                font-size: 12px;
            }
            QPushButton#presetButton {
                background: #f0f5ff; border: 1px solid #c3d4f8;
                color: #2560e0; font-size: 12px; padding: 4px 10px;
            }
            QPushButton#presetButton:hover { background: #dbeafe; }
            QPushButton#filterButton {
                background: #f0f2f5; border: 1px solid #d0d5df;
                color: #6b7a94; font-size: 12px; padding: 4px 10px;
            }
            QPushButton#filterButton:checked {
                background: #fef3c7; border: 1px solid #f6c843; color: #92400e;
            }
            """
        )

    def closeEvent(self, event) -> None:
        if self.process is not None and not self._is_reading:
            reply = QMessageBox.warning(
                self,
                "烧写进行中",
                "eFuse 烧写操作不可逆，中断可能损坏芯片。\n确定要关闭吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._stop_process()
        super().closeEvent(event)
