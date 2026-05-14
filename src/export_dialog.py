import re
from dataclasses import dataclass
from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .models import DeviceInfo
from .styles import BASE_STYLESHEET


@dataclass(frozen=True)
class ExportFlashConfig:
    address: str
    size: str
    project_name: str
    filename: str


class ExportFlashDialog(QDialog):
    """配置单设备 Flash 导出参数。"""

    _INVALID_FILENAME_CHARS = r'<>:"/\\|?*'

    def __init__(
        self,
        device: DeviceInfo,
        baud: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.device = device
        self.baud = baud
        self._timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.setWindowTitle(f"导出固件 — {device.port}")
        self.setModal(True)
        self.setMinimumWidth(620)
        self._init_ui()
        self._apply_style()
        self._refresh_auto_filename()

    def export_config(self) -> ExportFlashConfig:
        return ExportFlashConfig(
            address=self.address_edit.text().strip(),
            size=self.size_combo.currentText().strip(),
            project_name=self.project_name_edit.text().strip(),
            filename=self.filename_edit.text().strip(),
        )

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("固件导出配置")
        title.setObjectName("sectionTitle")
        port_badge = QLabel(self.device.port)
        port_badge.setObjectName("portBadge")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(port_badge)
        root.addLayout(header)

        hint = QLabel("默认读取整片 Flash（自动检测容量）。确认后只需选择一个目录即可开始导出。")
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)

        info_frame = QFrame()
        info_frame.setObjectName("sectionFrame")
        info_grid = QGridLayout(info_frame)
        info_grid.setContentsMargins(12, 10, 12, 10)
        info_grid.setHorizontalSpacing(12)
        info_grid.setVerticalSpacing(6)
        info_rows = [
            ("芯片", self.device.chip_name or "未识别"),
            ("Flash", self.device.flash_size or "自动检测"),
            ("MAC", self.device.mac or "-"),
            ("波特率", self.baud or "115200"),
        ]
        for row, (label, value) in enumerate(info_rows):
            key = QLabel(label)
            key.setObjectName("configLabel")
            val = QLabel(value)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            info_grid.addWidget(key, row // 2, (row % 2) * 2)
            info_grid.addWidget(val, row // 2, (row % 2) * 2 + 1)
        root.addWidget(info_frame)

        form_frame = QFrame()
        form_frame.setObjectName("sectionFrame")
        form = QGridLayout(form_frame)
        form.setContentsMargins(12, 10, 12, 10)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        addr_lbl = QLabel("起始地址")
        addr_lbl.setObjectName("configLabel")
        self.address_edit = QLineEdit("0x0")
        self.address_edit.setPlaceholderText("例如 0x0")

        size_lbl = QLabel("读取大小")
        size_lbl.setObjectName("configLabel")
        self.size_combo = QComboBox()
        self.size_combo.setEditable(True)
        self.size_combo.addItems(self._build_size_options())
        self.size_combo.setCurrentText("ALL")
        self.size_combo.lineEdit().setPlaceholderText("ALL / 16M / 0x1000000")

        project_lbl = QLabel("项目名称")
        project_lbl.setObjectName("configLabel")
        self.project_name_edit = QLineEdit()
        self.project_name_edit.setPlaceholderText("可选；填写后自动命名会追加到 FW 后")

        filename_lbl = QLabel("文件名")
        filename_lbl.setObjectName("configLabel")
        self.filename_edit = QLineEdit()
        self.filename_edit.setPlaceholderText("导出的 .bin 文件名")
        self.auto_name_check = QCheckBox("自动命名")
        self.auto_name_check.setChecked(True)
        self.filename_edit.setReadOnly(True)

        form.addWidget(addr_lbl, 0, 0)
        form.addWidget(self.address_edit, 0, 1)
        form.addWidget(size_lbl, 1, 0)
        form.addWidget(self.size_combo, 1, 1)
        form.addWidget(project_lbl, 2, 0)
        form.addWidget(self.project_name_edit, 2, 1)
        form.addWidget(filename_lbl, 3, 0)
        filename_row = QHBoxLayout()
        filename_row.setSpacing(8)
        filename_row.addWidget(self.filename_edit, 1)
        filename_row.addWidget(self.auto_name_check)
        form.addLayout(filename_row, 3, 1)

        note = QLabel("提示：ALL 会让 esptool 自动检测 Flash 容量并导出整片；自定义大小可输入 4M、0x400000 等。")
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        form.addWidget(note, 4, 0, 1, 2)
        root.addWidget(form_frame)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        self.continue_btn = QPushButton("继续导出")
        self.continue_btn.setObjectName("primaryButton")
        self.continue_btn.clicked.connect(self._accept_if_valid)
        actions.addWidget(cancel_btn)
        actions.addWidget(self.continue_btn)
        root.addLayout(actions)

        self.address_edit.textChanged.connect(self._refresh_auto_filename)
        self.size_combo.currentTextChanged.connect(self._refresh_auto_filename)
        self.project_name_edit.textChanged.connect(self._refresh_auto_filename)
        self.auto_name_check.toggled.connect(self._on_auto_name_toggled)

    def _build_size_options(self) -> list[str]:
        options = ["ALL"]
        flash_size = (self.device.flash_size or "").strip().upper()
        detected = self._normalize_flash_size(flash_size)
        if detected and detected not in options:
            options.append(detected)
        for value in ("4M", "8M", "16M", "32M", "64M", "128M"):
            if value not in options:
                options.append(value)
        return options

    @staticmethod
    def _normalize_flash_size(text: str) -> str:
        match = re.search(r"(\d+)\s*MB", text, re.IGNORECASE)
        if match:
            return f"{match.group(1)}M"
        match = re.search(r"(\d+)\s*M\b", text, re.IGNORECASE)
        if match:
            return f"{match.group(1)}M"
        return ""

    def _on_auto_name_toggled(self, checked: bool) -> None:
        self.filename_edit.setReadOnly(checked)
        if checked:
            self._refresh_auto_filename()
        else:
            self.filename_edit.setFocus()
            self.filename_edit.selectAll()

    def _refresh_auto_filename(self) -> None:
        if not self.auto_name_check.isChecked():
            return
        self.filename_edit.setText(self._build_auto_filename())

    def _build_auto_filename(self) -> str:
        chip = self._safe_token(self._short_chip_name(self.device.chip_name or "auto"))
        flash = self._safe_token(self.device.flash_size or "flash-auto")
        mac = self._safe_token((self.device.mac or "").replace(":", ""))
        address = self._safe_token(self.address_edit.text().strip() or "0x0")
        size_text = self.size_combo.currentText().strip()
        size = self._safe_token(size_text) if size_text.lower() != "all" else ""
        project = self._safe_token(self.project_name_edit.text().strip())
        parts = ["FW"]
        if project:
            parts.append(project)
        parts.append(chip)
        if mac and mac != "-":
            parts.append(mac[-6:])
        parts.extend([flash, self._timestamp])
        if size:
            parts.append(size)
        parts.append(address)
        return "_".join(part for part in parts if part) + ".bin"

    @staticmethod
    def _short_chip_name(full: str) -> str:
        match = re.match(r"^(ESP\d+(?:-[A-Z]\d+)?)(?=[A-Z\s(]|$)", full, re.IGNORECASE)
        return match.group(1).upper() if match else full

    @classmethod
    def _safe_token(cls, value: str) -> str:
        cleaned = value.strip().replace(" ", "-")
        cleaned = re.sub(rf"[{re.escape(cls._INVALID_FILENAME_CHARS)}]+", "-", cleaned)
        cleaned = re.sub(r"[^\w.-]+", "-", cleaned, flags=re.UNICODE)
        cleaned = re.sub(r"-+", "-", cleaned).strip(".-_")
        return cleaned

    def _accept_if_valid(self) -> None:
        address = self.address_edit.text().strip()
        size = self.size_combo.currentText().strip()
        filename = self.filename_edit.text().strip()
        if not self._valid_address(address):
            QMessageBox.warning(self, "提示", "起始地址格式不正确，应为 0x0、0x10000 等十六进制地址。")
            self.address_edit.setFocus()
            return
        if not self._valid_size(size):
            QMessageBox.warning(self, "提示", "读取大小格式不正确，请输入 ALL、16M、512k 或 0x100000 等。")
            self.size_combo.setFocus()
            return
        normalized_size = self._normalize_size_for_esptool(size)
        if normalized_size != size:
            self.size_combo.setCurrentText(normalized_size)
        if not filename:
            QMessageBox.warning(self, "提示", "文件名不能为空。")
            self.filename_edit.setFocus()
            return
        if any(ch in filename for ch in self._INVALID_FILENAME_CHARS):
            QMessageBox.warning(self, "提示", "文件名包含 Windows 不支持的字符。")
            self.filename_edit.setFocus()
            return
        if not filename.lower().endswith(".bin"):
            self.filename_edit.setText(filename + ".bin")
        self.accept()

    @staticmethod
    def _valid_address(value: str) -> bool:
        return bool(re.fullmatch(r"0[xX][0-9a-fA-F]+", value))

    @staticmethod
    def _valid_size(value: str) -> bool:
        if value.lower() == "all":
            return True
        if re.fullmatch(r"0[xX][0-9a-fA-F]+", value):
            return True
        if re.fullmatch(r"\d+", value):
            return True
        return bool(re.fullmatch(r"\d+[kKmM]", value))

    @staticmethod
    def _normalize_size_for_esptool(value: str) -> str:
        if value.lower() == "all":
            return "ALL"
        if value.endswith("m"):
            return value[:-1] + "M"
        if value.endswith("K"):
            return value[:-1] + "k"
        return value

    def _apply_style(self) -> None:
        self.setStyleSheet(
            BASE_STYLESHEET
            + """
            QDialog {
                background: #f0f2f5;
            }
            QFrame#sectionFrame {
                background: #ffffff;
                border: 1px solid #e0e4ea;
                border-radius: 10px;
            }
            QLabel#hintLabel {
                color: #6b7a94;
                font-size: 12px;
            }
            QLineEdit[readOnly="true"] {
                background: #eef2f7;
                color: #475569;
            }
            QCheckBox {
                color: #475569;
                font-weight: 600;
            }
            """
        )