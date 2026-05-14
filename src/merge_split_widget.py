"""分合台 — 固件合成（merge-bin）与固件分解（分区表解析拆分）。

上半部分：分解区 — 选择已合成的 .bin 文件 → 分析分区表 → 提取各子文件
下半部分：合成区 — 添加地址+固件条目 → 调用 esptool merge-bin → 输出合成文件
"""

from __future__ import annotations

import json
import os
import re
import struct
import subprocess
from pathlib import Path

from PyQt6.QtCore import QProcess, QProcessEnvironment, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .constants import (
    TOOL_DIR,
    _build_tool_command,
    _inject_local_esptool_pythonpath,
    _tool_backend_available,
)
from .dialog_memory import get_existing_directory, get_open_file_name, get_save_file_name

# ── 动态芯片列表 ─────────────────────────────────────────────────────────────
try:
    from esptool.targets import CHIP_DEFS as _ESPTOOL_CHIP_DEFS
    _CHIP_OPTIONS: list[str] = sorted(_ESPTOOL_CHIP_DEFS.keys())
except Exception:
    _CHIP_OPTIONS = [
        "esp32", "esp32c2", "esp32c3", "esp32c5", "esp32c6",
        "esp32h2", "esp32p4", "esp32s2", "esp32s3",
    ]

# ── 分区表常量 ───────────────────────────────────────────────────────────────
ESP_IMAGE_MAGIC = 0xE9
PT_MAGIC = 0x50AA
PT_MD5_MAGIC = 0xEBEB
PT_ENTRY_SIZE = 32
PT_OFFSET_DEFAULT = 0x8000
PT_OFFSET_CANDIDATES = [0x8000, 0x9000, 0xA000, 0xB000, 0xC000, 0xD000, 0xE000, 0xF000]
PT_MAX_ENTRIES = 95

PARTITION_TYPES = {0: "app", 1: "data"}
APP_SUBTYPES = {
    0x00: "factory", 0x10: "ota_0", 0x11: "ota_1", 0x12: "ota_2",
    0x13: "ota_3", 0x14: "ota_4", 0x15: "ota_5", 0x16: "ota_6",
    0x17: "ota_7", 0x18: "ota_8", 0x19: "ota_9", 0x1A: "ota_10",
    0x1B: "ota_11", 0x1C: "ota_12", 0x1D: "ota_13", 0x1E: "ota_14",
    0x1F: "ota_15", 0x20: "test",
}
DATA_SUBTYPES = {
    0x00: "ota", 0x01: "phy", 0x02: "nvs", 0x03: "coredump",
    0x04: "nvs_keys", 0x05: "efuse_em", 0x80: "esphttpd",
    0x81: "fat", 0x82: "spiffs", 0x83: "littlefs",
}
CHIPS_ZERO_BL_OFFSET = {
    "esp32c2", "esp32c3", "esp32c5", "esp32c6",
    "esp32h2", "esp32h4", "esp32p4", "esp32s3",
}
_SIZE_SUFFIXES = ["B", "KB", "MB"]


def _human_size(n: int) -> str:
    val = float(n)
    for s in _SIZE_SUFFIXES:
        if val < 1024:
            return f"{val:g}{s}"
        val /= 1024
    return f"{val:g}GB"


# ═══════════════════════════════════════════════════════════════════════════════
#  分区表解析工具
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_partition_table(blob: bytes) -> list[dict]:
    entries: list[dict] = []
    for i in range(min(len(blob) // PT_ENTRY_SIZE, PT_MAX_ENTRIES)):
        rec = blob[i * PT_ENTRY_SIZE: (i + 1) * PT_ENTRY_SIZE]
        if len(rec) < PT_ENTRY_SIZE:
            break
        magic = struct.unpack_from("<H", rec, 0)[0]
        if magic == 0xFFFF:
            break
        if magic == PT_MD5_MAGIC:
            continue
        if magic != PT_MAGIC:
            continue
        p_type = rec[2]
        subtype = rec[3]
        offset, size = struct.unpack_from("<II", rec, 4)
        name = rec[12:28].split(b"\x00")[0].decode("utf-8", errors="replace")
        if offset == 0 or size == 0:
            continue
        type_str = PARTITION_TYPES.get(p_type, f"type_{p_type:#x}")
        if p_type == 0:
            subtype_str = APP_SUBTYPES.get(subtype, f"sub_{subtype:#x}")
        else:
            subtype_str = DATA_SUBTYPES.get(subtype, f"sub_{subtype:#x}")
        flags = struct.unpack_from("<H", rec, 28)[0]
        encrypted = bool(flags & 0x1)
        entries.append({
            "name": name, "type_str": type_str, "subtype_str": subtype_str,
            "offset": offset, "size": size, "encrypted": encrypted,
        })
    return entries


def _detect_bootloader_offset(data: bytes, chip: str) -> int | None:
    if chip == "esp32p4":
        candidates = [0x2000, 0x0, 0x1000]
    elif chip in CHIPS_ZERO_BL_OFFSET:
        candidates = [0x0, 0x1000]
    elif chip == "esp32":
        candidates = [0x1000, 0x0]
    else:
        candidates = [0x0, 0x1000, 0x2000]
    for off in candidates:
        if off + 8 <= len(data) and data[off] == ESP_IMAGE_MAGIC:
            return off
    return None


def _detect_partition_table_offset(data: bytes) -> int:
    """在候选偏移处探测分区表魔数 0x50AA，返回第一个匹配的偏移。"""
    for off in PT_OFFSET_CANDIDATES:
        if off + PT_ENTRY_SIZE <= len(data):
            magic = struct.unpack_from("<H", data, off)[0]
            if magic == PT_MAGIC:
                return off
    return PT_OFFSET_DEFAULT


def analyze_merged_bin(data: bytes, chip: str = "auto") -> dict:
    result: dict = {
        "bootloader": None, "partition_table": None,
        "partitions": [], "warnings": [],
    }
    pt_offset = _detect_partition_table_offset(data)
    bl_off = _detect_bootloader_offset(data, chip)
    if bl_off is not None:
        bl_raw = data[bl_off:pt_offset].rstrip(b"\xff")
        result["bootloader"] = {
            "name": "bootloader", "offset": bl_off,
            "size": len(bl_raw), "type_str": "bootloader",
            "subtype_str": "-", "encrypted": False,
        }
    if pt_offset != PT_OFFSET_DEFAULT:
        result["warnings"].append(
            f"分区表偏移非默认值: {pt_offset:#x}（默认 {PT_OFFSET_DEFAULT:#x}）"
        )
    pt_raw_max = 0xC00
    pt_blob = data[pt_offset: pt_offset + pt_raw_max]
    pt_trimmed = pt_blob.rstrip(b"\xff")
    pt_size = min(len(pt_trimmed) + PT_ENTRY_SIZE, pt_raw_max)
    result["partition_table"] = {
        "name": "partition-table", "offset": pt_offset,
        "size": pt_size, "type_str": "partition-table",
        "subtype_str": "-", "encrypted": False,
    }
    entries = _parse_partition_table(pt_blob)
    if not entries:
        result["warnings"].append("未能解析有效分区表，可能是加密固件或非标准格式。")
        return result
    for e in entries:
        off, sz = e["offset"], e["size"]
        if off + sz > len(data):
            result["warnings"].append(
                f"分区 '{e['name']}' 超出文件边界 (offset={off:#x}, "
                f"size={sz:#x}, file={len(data):#x})，已截断。"
            )
            sz = max(0, len(data) - off)
        chunk = data[off: off + sz]
        is_empty = chunk.count(b"\xff") >= len(chunk)
        result["partitions"].append({
            "name": e["name"], "offset": off, "size": sz,
            "type_str": e["type_str"], "subtype_str": e["subtype_str"],
            "empty": is_empty,
        })
    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  文件系统浏览对话框
# ═══════════════════════════════════════════════════════════════════════════════

_FS_SUBTYPES = frozenset({"spiffs", "littlefs", "fat"})


class _FsViewerDialog(QDialog):
    """浏览嵌入式文件系统（LittleFS / FAT）的内容。"""

    def __init__(self, part_name: str, subtype: str, data: bytes,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"文件浏览 — {part_name}（{subtype}）")
        self.setMinimumSize(640, 440)
        self.resize(740, 520)
        self._fs = None
        self._buf: bytearray | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        self._status_lbl = QLabel("正在挂载…")
        self._status_lbl.setObjectName("configLabel")
        lay.addWidget(self._status_lbl)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["名称", "大小", "类型"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        lay.addWidget(self._tree, 1)

        btn_row = QHBoxLayout()
        self._extract_sel_btn = QPushButton("提取选中")
        self._extract_sel_btn.clicked.connect(self._extract_selected)
        self._extract_all_btn = QPushButton("全部提取")
        self._extract_all_btn.setObjectName("primaryButton")
        self._extract_all_btn.clicked.connect(self._extract_all)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._extract_sel_btn)
        btn_row.addWidget(self._extract_all_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._try_mount(data)

    # ── 挂载 ──────────────────────────────────────────────────────────────────

    def _try_mount(self, data: bytes) -> None:
        try:
            import littlefs as _lfs
        except ImportError:
            self._status_lbl.setText(
                "⚠ 未安装 littlefs-python，请运行: pip install littlefs-python"
            )
            return

        for block_size in (4096, 8192, 2048, 16384):
            block_count = len(data) // block_size
            if block_count < 2:
                continue
            try:
                self._buf = bytearray(data)
                ctx = _lfs.UserContext(buffsize=len(self._buf), buffer=self._buf)
                fs = _lfs.LittleFS(
                    context=ctx, block_size=block_size,
                    block_count=block_count, mount=True,
                )
                self._fs = fs
                self._status_lbl.setText(
                    f"✓ LittleFS 已挂载  "
                    f"block_size={block_size} B  "
                    f"blocks={block_count}  "
                    f"大小={_human_size(len(data))}"
                )
                self._populate_tree(fs)
                return
            except Exception:
                self._buf = None
                continue

        self._status_lbl.setText(
            "⚠ 无法解析此文件系统（可能是 SPIFFS 格式、数据为空或块参数不匹配）"
        )

    # ── 构建文件树 ─────────────────────────────────────────────────────────────

    def _populate_tree(self, fs) -> None:
        self._tree.clear()
        root = self._tree.invisibleRootItem()
        dir_map: dict[str, QTreeWidgetItem] = {"/": root}
        try:
            for dirpath, dirs, files in fs.walk("/"):
                base = "/" + dirpath.strip("/")  # normalize to "/" or "/sub"
                parent_item = dir_map.get(base, root)
                for dname in sorted(dirs):
                    child_path = base.rstrip("/") + "/" + dname
                    d_item = QTreeWidgetItem(parent_item, [dname, "—", "📁 目录"])
                    d_item.setData(0, Qt.ItemDataRole.UserRole, None)
                    dir_map[child_path] = d_item
                for fname in sorted(files):
                    fpath = base.rstrip("/") + "/" + fname
                    size_str = "?"
                    try:
                        size_str = _human_size(fs.stat(fpath).size)
                    except Exception:
                        pass
                    f_item = QTreeWidgetItem(parent_item, [fname, size_str, "📄 文件"])
                    f_item.setData(0, Qt.ItemDataRole.UserRole, fpath)
        except Exception as exc:
            self._status_lbl.setText(f"⚠ 遍历文件树失败: {exc}")
            return
        self._tree.expandAll()

    # ── 文件读取 ───────────────────────────────────────────────────────────────

    def _read_file(self, fpath: str) -> bytes | None:
        if self._fs is None:
            return None
        try:
            with self._fs.open(fpath, "rb") as fh:
                raw = fh.read()
                return bytes(raw) if raw is not None else b""
        except Exception:
            return None

    # ── 提取 ──────────────────────────────────────────────────────────────────

    def _extract_selected(self) -> None:
        items = [
            it for it in self._tree.selectedItems()
            if it.data(0, Qt.ItemDataRole.UserRole) is not None
        ]
        if not items:
            QMessageBox.information(self, "提示", "请先选择要提取的文件（非目录）。")
            return
        out_dir = get_existing_directory(self, "选择输出目录")
        if not out_dir:
            return
        out = Path(out_dir)
        count = 0
        for item in items:
            fpath = item.data(0, Qt.ItemDataRole.UserRole)
            raw = self._read_file(fpath)
            if raw is not None:
                (out / Path(fpath).name).write_bytes(raw)
                count += 1
        QMessageBox.information(self, "完成", f"已提取 {count} 个文件到\n{out_dir}")

    def _extract_all(self) -> None:
        if self._fs is None:
            return
        out_dir = get_existing_directory(self, "选择输出目录")
        if not out_dir:
            return
        out = Path(out_dir)
        count, errors = 0, []
        try:
            for dirpath, _, files in self._fs.walk("/"):
                rel = dirpath.strip("/")
                sub = out / rel if rel else out
                sub.mkdir(parents=True, exist_ok=True)
                base = "/" + rel
                for fname in files:
                    fpath = base.rstrip("/") + "/" + fname
                    raw = self._read_file(fpath)
                    if raw is not None:
                        (sub / fname).write_bytes(raw)
                        count += 1
                    else:
                        errors.append(fpath)
        except Exception as exc:
            QMessageBox.critical(self, "错误", str(exc))
            return
        msg = f"已提取 {count} 个文件到\n{out_dir}"
        if errors:
            msg += f"\n⚠ {len(errors)} 个文件读取失败"
        QMessageBox.information(self, "完成", msg)


# ═══════════════════════════════════════════════════════════════════════════════
#  条目行 Widget
# ═══════════════════════════════════════════════════════════════════════════════

class _EntryRow(QWidget):
    """地址 + 固件路径 + 浏览 + 移除。"""

    remove_requested = pyqtSignal()

    def __init__(self, addr: str = "0x0", path: str = "", parent: QWidget | None = None):
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
        self.path_edit.setPlaceholderText("选择 .bin 固件文件")

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
        fp, _ = get_open_file_name(
            self, "选择固件", "Binary Files (*.bin);;All Files (*.*)",
        )
        if fp:
            self.path_edit.setText(fp)
            self._auto_addr(fp)

    def _auto_addr(self, path: str) -> None:
        stem = Path(path).stem
        m = re.search(r"_(0[xX][0-9a-fA-F]+)$", stem)
        if m:
            self.addr_edit.setText(m.group(1).lower())


# ═══════════════════════════════════════════════════════════════════════════════
#  MergeSplitWidget — 分合台主 Widget
# ═══════════════════════════════════════════════════════════════════════════════

class MergeSplitWidget(QWidget):
    """分合台：上半部分=分解，下半部分=合成。"""

    send_to_flash_station = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._merge_process: QProcess | None = None
        self._merge_rows: list[_EntryRow] = []
        self._split_data: bytes = b""
        self._split_result: dict | None = None
        self._init_ui()

    # ── UI 构建 ───────────────────────────────────────────────────────────────

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        # ── 上：分解区 ───────────────────────────────────────────
        split_frame = QFrame()
        split_frame.setObjectName("mergeSplitFrame")
        sl = QVBoxLayout(split_frame)
        sl.setContentsMargins(14, 10, 14, 10)
        sl.setSpacing(6)

        split_title = QLabel("固件分解")
        split_title.setObjectName("sectionTitle")
        sl.addWidget(split_title)

        # 输入行
        r1 = QHBoxLayout()
        r1.setSpacing(6)
        lbl_in = QLabel("输入文件")
        lbl_in.setObjectName("configLabel")
        self._split_input_edit = QLineEdit()
        self._split_input_edit.setPlaceholderText("选择已合成的 .bin 固件文件")
        split_browse = QPushButton("选择")
        split_browse.setFixedWidth(52)
        split_browse.clicked.connect(self._split_browse_input)
        r1.addWidget(lbl_in)
        r1.addWidget(self._split_input_edit, 1)
        r1.addWidget(split_browse)
        sl.addLayout(r1)

        # 芯片 + 输出目录
        r2 = QHBoxLayout()
        r2.setSpacing(6)
        lbl_chip = QLabel("芯片型号")
        lbl_chip.setObjectName("configLabel")
        self._split_chip_combo = QComboBox()
        self._split_chip_combo.addItem("auto")
        self._split_chip_combo.addItems(_CHIP_OPTIONS)
        self._split_chip_combo.setFixedWidth(120)
        lbl_out = QLabel("输出目录")
        lbl_out.setObjectName("configLabel")
        self._split_output_edit = QLineEdit()
        self._split_output_edit.setPlaceholderText("默认与输入文件同目录")
        split_out_browse = QPushButton("选择")
        split_out_browse.setFixedWidth(52)
        split_out_browse.clicked.connect(self._split_browse_output)
        r2.addWidget(lbl_chip)
        r2.addWidget(self._split_chip_combo)
        r2.addSpacing(8)
        r2.addWidget(lbl_out)
        r2.addWidget(self._split_output_edit, 1)
        r2.addWidget(split_out_browse)
        sl.addLayout(r2)

        # 按钮行
        r3 = QHBoxLayout()
        r3.setSpacing(8)
        self._split_analyze_btn = QPushButton("分析")
        self._split_analyze_btn.setObjectName("primaryButton")
        self._split_analyze_btn.clicked.connect(self._split_analyze)
        self._split_from_device_btn = QPushButton("从设备分析")
        self._split_from_device_btn.clicked.connect(self._split_analyze_from_device)
        self._split_extract_btn = QPushButton("全部提取")
        self._split_extract_btn.clicked.connect(self._split_extract_all)
        self._split_extract_btn.setEnabled(False)
        r3.addWidget(self._split_analyze_btn)
        r3.addWidget(self._split_from_device_btn)
        r3.addWidget(self._split_extract_btn)
        r3.addStretch(1)
        sl.addLayout(r3)

        # 结果表
        self._split_table = QTableWidget(0, 7)
        self._split_table.setHorizontalHeaderLabels(
            ["名称", "偏移", "大小", "类型", "子类型", "加密", "操作"]
        )
        hdr = self._split_table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4, 5):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self._split_table.setColumnWidth(6, 124)  # 查看(52)+间距(6)+提取(52)+边距(8)*2 ≈ 124
        self._split_table.setAlternatingRowColors(True)
        self._split_table.setShowGrid(False)
        self._split_table.verticalHeader().setVisible(False)
        self._split_table.verticalHeader().setDefaultSectionSize(32)
        self._split_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._split_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._split_table.setMinimumHeight(100)
        self._split_table.cellDoubleClicked.connect(self._split_double_click)
        sl.addWidget(self._split_table, 1)

        # ── 下：合成区 ───────────────────────────────────────────
        merge_frame = QFrame()
        merge_frame.setObjectName("mergeSplitFrame")
        ml = QVBoxLayout(merge_frame)
        ml.setContentsMargins(14, 10, 14, 10)
        ml.setSpacing(6)

        merge_title = QLabel("固件合成")
        merge_title.setObjectName("sectionTitle")
        merge_title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        ml.addWidget(merge_title)

        # 芯片 + 格式
        m1 = QHBoxLayout()
        m1.setSpacing(6)
        lbl_mc = QLabel("芯片")
        lbl_mc.setObjectName("configLabel")
        self._merge_chip_combo = QComboBox()
        self._merge_chip_combo.addItems(_CHIP_OPTIONS)
        self._merge_chip_combo.setCurrentText("esp32s3")
        self._merge_chip_combo.setFixedWidth(120)
        lbl_fmt = QLabel("格式")
        lbl_fmt.setObjectName("configLabel")
        self._merge_fmt_combo = QComboBox()
        self._merge_fmt_combo.addItems(["raw", "uf2", "hex"])
        self._merge_fmt_combo.setFixedWidth(80)
        lbl_fm = QLabel("Flash模式")
        lbl_fm.setObjectName("configLabel")
        self._merge_flash_mode = QComboBox()
        self._merge_flash_mode.addItems(["keep", "qio", "qout", "dio", "dout"])
        self._merge_flash_mode.setFixedWidth(80)
        lbl_ff = QLabel("Flash频率")
        lbl_ff.setObjectName("configLabel")
        self._merge_flash_freq = QComboBox()
        self._merge_flash_freq.addItems(["keep", "80m", "60m", "48m", "40m", "30m", "26m", "24m", "20m"])
        self._merge_flash_freq.setFixedWidth(80)
        lbl_fs = QLabel("Flash大小")
        lbl_fs.setObjectName("configLabel")
        self._merge_flash_size = QComboBox()
        self._merge_flash_size.addItems([
            "keep", "256KB", "512KB", "1MB", "2MB", "4MB",
            "8MB", "16MB", "32MB", "64MB", "128MB",
        ])
        self._merge_flash_size.setFixedWidth(90)

        m1.addWidget(lbl_mc)
        m1.addWidget(self._merge_chip_combo)
        m1.addSpacing(4)
        m1.addWidget(lbl_fmt)
        m1.addWidget(self._merge_fmt_combo)
        m1.addSpacing(4)
        m1.addWidget(lbl_fm)
        m1.addWidget(self._merge_flash_mode)
        m1.addSpacing(4)
        m1.addWidget(lbl_ff)
        m1.addWidget(self._merge_flash_freq)
        m1.addSpacing(4)
        m1.addWidget(lbl_fs)
        m1.addWidget(self._merge_flash_size)
        m1.addStretch(1)
        ml.addLayout(m1)

        # 输出路径
        m2 = QHBoxLayout()
        m2.setSpacing(6)
        lbl_mo = QLabel("输出文件")
        lbl_mo.setObjectName("configLabel")
        self._merge_output_edit = QLineEdit()
        self._merge_output_edit.setPlaceholderText("输出合成文件路径 (.bin / .uf2 / .hex)")
        merge_out_browse = QPushButton("选择")
        merge_out_browse.setFixedWidth(52)
        merge_out_browse.clicked.connect(self._merge_browse_output)
        m2.addWidget(lbl_mo)
        m2.addWidget(self._merge_output_edit, 1)
        m2.addWidget(merge_out_browse)
        ml.addLayout(m2)

        # 条目区
        m3_header = QHBoxLayout()
        m3_header.setSpacing(8)
        lbl_entries = QLabel("合成条目")
        lbl_entries.setObjectName("configLabel")
        flash_args_btn = QPushButton("读取 flash_args")
        flash_args_btn.setObjectName("addEntryButton")
        flash_args_btn.clicked.connect(self._merge_load_flash_args)
        add_btn = QPushButton("＋ 添加条目")
        add_btn.setObjectName("addEntryButton")
        add_btn.clicked.connect(lambda: self._merge_add_entry())
        m3_header.addWidget(lbl_entries)
        m3_header.addStretch(1)
        m3_header.addWidget(flash_args_btn)
        m3_header.addWidget(add_btn)
        ml.addLayout(m3_header)

        self._merge_entries_inner = QWidget()
        self._merge_entries_inner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._merge_entries_vbox = QVBoxLayout(self._merge_entries_inner)
        self._merge_entries_vbox.setContentsMargins(0, 0, 0, 0)
        self._merge_entries_vbox.setSpacing(2)
        ml.addWidget(self._merge_entries_inner)

        # 按钮行
        m4 = QHBoxLayout()
        m4.setSpacing(8)
        self._merge_run_btn = QPushButton("合成")
        self._merge_run_btn.setObjectName("primaryButton")
        self._merge_run_btn.clicked.connect(self._merge_run)
        self._merge_send_btn = QPushButton("发送到烧录台")
        self._merge_send_btn.setEnabled(False)
        self._merge_send_btn.clicked.connect(self._merge_send_to_flash)
        m4.addWidget(self._merge_run_btn)
        m4.addWidget(self._merge_send_btn)
        m4.addStretch(1)
        ml.addLayout(m4)

        # 进度条
        self._merge_progress = QProgressBar()
        self._merge_progress.setRange(0, 100)
        self._merge_progress.setValue(0)
        self._merge_progress.setTextVisible(True)
        self._merge_progress.setFormat("准备就绪")
        ml.addWidget(self._merge_progress)
        ml.addStretch(1)

        # ── 共享日志 ─────────────────────────────────────────────
        log_frame = QFrame()
        log_frame.setObjectName("mergeSplitFrame")
        ll = QVBoxLayout(log_frame)
        ll.setContentsMargins(14, 6, 14, 6)
        ll.setSpacing(4)
        log_title = QLabel("日志")
        log_title.setObjectName("configLabel")
        ll.addWidget(log_title)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(600)
        self._log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._log.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        _font = QFont()
        _font.setFamilies(["Consolas", "Menlo", "DejaVu Sans Mono", "monospace"])
        _font.setStyleHint(QFont.StyleHint.TypeWriter)
        _font.setPointSize(10)
        self._log.setFont(_font)
        ll.addWidget(self._log)

        splitter.addWidget(split_frame)
        splitter.addWidget(merge_frame)
        splitter.addWidget(log_frame)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)
        root.addWidget(splitter, 1)

        # 初始化第一条合成条目
        self._merge_add_entry()

    # ── 日志 ──────────────────────────────────────────────────────────────────

    def _append_log(self, msg: str) -> None:
        sb = self._log.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4
        self._log.appendPlainText(msg.rstrip())
        if at_bottom:
            sb.setValue(sb.maximum())

    # ═════════════════════════════════════════════════════════════════════════
    #  分解区
    # ═════════════════════════════════════════════════════════════════════════

    def _split_browse_input(self) -> None:
        fp, _ = get_open_file_name(
            self, "选择合并固件", "Binary Files (*.bin);;All Files (*.*)",
        )
        if fp:
            self._split_input_edit.setText(fp)

    def _split_browse_output(self) -> None:
        d = get_existing_directory(self, "选择输出目录")
        if d:
            self._split_output_edit.setText(d)

    def _split_analyze_from_device(self) -> None:
        """扫描串口，弹窗选择设备，通过 esptool read_flash 读取分区表区域后分析。"""
        try:
            from serial.tools import list_ports as _list_ports
        except ImportError:
            QMessageBox.critical(self, "错误", "未找到 pyserial，请安装：pip install pyserial")
            return

        ports = [
            p for p in _list_ports.comports()
            if not (p.description and __import__("re").search(
                r"通信端口|Communications Port", p.description))
        ]
        if not ports:
            QMessageBox.information(self, "提示", "未检测到串口设备，请连接设备后重试。")
            return

        # 弹出设备选择对话框
        dlg = QDialog(self)
        dlg.setWindowTitle("选择串口设备")
        dlg.setMinimumWidth(420)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(8)
        lay.addWidget(QLabel("请选择要读取的设备："))
        lst = QListWidget()
        for p in ports:
            desc = p.description if p.description and p.description != "n/a" else "串口设备"
            lst.addItem(f"{p.device}  —  {desc}")
        lst.setCurrentRow(0)
        lay.addWidget(lst)
        chip_row = QHBoxLayout()
        chip_row.addWidget(QLabel("芯片"))
        chip_sel = QComboBox()
        chip_sel.addItem("auto")
        chip_sel.addItems(_CHIP_OPTIONS)
        # 同步分解区的芯片选择
        chip_sel.setCurrentText(self._split_chip_combo.currentText())
        chip_sel.setFixedWidth(120)
        chip_row.addWidget(chip_sel)
        chip_row.addStretch(1)
        lay.addLayout(chip_row)
        btns = QHBoxLayout()
        ok_btn = QPushButton("读取")
        ok_btn.setObjectName("primaryButton")
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(dlg.reject)
        btns.addStretch(1)
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        lay.addLayout(btns)
        lst.itemDoubleClicked.connect(lambda _: dlg.accept())

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        idx = lst.currentRow()
        if idx < 0 or idx >= len(ports):
            return
        port = ports[idx].device
        chip = chip_sel.currentText()
        # 同步芯片到分解区
        self._split_chip_combo.setCurrentText(chip)

        # 用 esptool 读取 Flash 到临时文件
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".bin", delete=False)
        tmp_path = tmp.name
        tmp.close()

        # 读取 4MB（足够包含分区表+常见分区头）
        read_size = "0x400000"
        chip_arg = chip if chip != "auto" else "auto"
        cmd = list(_build_tool_command(
            "esptool", "--chip", chip_arg, "--port", port,
            "read_flash", "0x0", read_size, tmp_path,
        ))
        self._append_log(f"═══ 从设备读取 Flash: {port} ═══")
        self._append_log("命令: " + subprocess.list2cmdline(cmd))
        self._split_analyze_btn.setEnabled(False)
        self._split_from_device_btn.setEnabled(False)

        proc = QProcess(self)
        proc.setProgram(cmd[0])
        proc.setArguments(cmd[1:])
        proc.setWorkingDirectory(str(TOOL_DIR))
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        _inject_local_esptool_pythonpath(env)
        env.insert("PYTHONUTF8", "1")
        proc.setProcessEnvironment(env)
        proc.readyRead.connect(lambda: self._append_log(
            bytes(proc.readAll()).decode("utf-8", errors="replace").rstrip()
        ))

        def _on_finished(exit_code, _):
            self._split_analyze_btn.setEnabled(True)
            self._split_from_device_btn.setEnabled(True)
            if exit_code != 0:
                self._append_log(f"✕ 读取失败 (exit {exit_code})")
                return
            self._append_log("✓ 读取完成，开始分析…")
            self._split_input_edit.setText(tmp_path)
            self._split_do_analyze(tmp_path)

        proc.finished.connect(_on_finished)
        proc.start()

    def _split_analyze(self) -> None:
        path = self._split_input_edit.text().strip()
        if not path or not Path(path).is_file():
            QMessageBox.warning(self, "提示", "请先选择有效的输入文件。")
            return
        self._split_do_analyze(path)

    def _split_do_analyze(self, path: str) -> None:
        try:
            data = Path(path).read_bytes()
        except Exception as exc:
            QMessageBox.critical(self, "错误", f"读取文件失败：{exc}")
            return
        if len(data) < PT_OFFSET_DEFAULT + PT_ENTRY_SIZE:
            QMessageBox.warning(
                self, "提示",
                f"文件过小（{_human_size(len(data))}），无法包含有效分区表。",
            )
            return
        self._split_data = data
        chip = self._split_chip_combo.currentText()
        result = analyze_merged_bin(data, chip)
        self._split_result = result
        self._append_log(f"═══ 分析 {Path(path).name} ({_human_size(len(data))}) ═══")
        for w in result["warnings"]:
            self._append_log(f"⚠ {w}")

        # 构建表格行
        rows: list[dict] = []
        if result["bootloader"]:
            rows.append(result["bootloader"])
        if result["partition_table"]:
            rows.append(result["partition_table"])
        rows.extend(result["partitions"])

        self._split_table.setRowCount(len(rows))
        self._split_rows_data = rows
        for i, r in enumerate(rows):
            self._split_table.setItem(i, 0, QTableWidgetItem(r["name"]))
            self._split_table.setItem(i, 1, QTableWidgetItem(f"{r['offset']:#010x}"))
            self._split_table.setItem(i, 2, QTableWidgetItem(_human_size(r["size"])))
            self._split_table.setItem(i, 3, QTableWidgetItem(r["type_str"]))
            self._split_table.setItem(i, 4, QTableWidgetItem(r.get("subtype_str", "-")))
            enc_item = QTableWidgetItem("🔒" if r.get("encrypted") else "")
            enc_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._split_table.setItem(i, 5, enc_item)
            empty = r.get("empty", False)
            subtype = r.get("subtype_str", "")
            # 操作列：左侧"查看"（仅文件系统分区），右侧"提取"
            op_w = QWidget()
            op_l = QHBoxLayout(op_w)
            op_l.setContentsMargins(4, 2, 4, 2)
            op_l.setSpacing(6)
            if subtype in _FS_SUBTYPES and not empty:
                view_btn = QPushButton("查看")
                view_btn.setFixedSize(52, 28)
                view_btn.clicked.connect(lambda _c=False, idx=i: self._split_view_fs(idx))
                op_l.addWidget(view_btn)
                op_l.addStretch(1)
            ext_btn = QPushButton("提取")
            ext_btn.setFixedSize(52, 28)
            ext_btn.clicked.connect(lambda _c=False, idx=i: self._split_extract_single(idx))
            op_l.addWidget(ext_btn)
            self._split_table.setCellWidget(i, 6, op_w)
            if empty:
                for c in range(6):
                    item = self._split_table.item(i, c)
                    if item:
                        item.setForeground(Qt.GlobalColor.gray)
                        item.setToolTip("此分区在合并固件中为空（全 0xFF 填充）")
            elif subtype in _FS_SUBTYPES:
                name_item = self._split_table.item(i, 0)
                if name_item:
                    name_item.setToolTip("双击以浏览内部文件（LittleFS / FAT）")
            self._append_log(
                f"  {r['offset']:#010x}  {_human_size(r['size']):>8s}  "
                f"{r['type_str']:<16s} {r['name']}"
            )

        self._split_extract_btn.setEnabled(len(rows) > 0)
        if not rows and not result["warnings"]:
            self._append_log("未找到有效分区。")
        self._append_log(f"分析完成，共 {len(rows)} 个区域。")

    def _split_double_click(self, row: int, _col: int) -> None:
        if row >= len(self._split_rows_data):
            return
        r = self._split_rows_data[row]
        subtype = r.get("subtype_str", "")
        if subtype not in _FS_SUBTYPES:
            return
        if r.get("empty", False):
            QMessageBox.information(
                self, "提示", f"分区 '{r['name']}' 为空（全 0xFF），无内容可浏览。"
            )
            return
        if not self._split_data:
            return
        off, sz = r["offset"], r["size"]
        chunk = self._split_data[off: off + sz]
        dlg = _FsViewerDialog(r["name"], subtype, chunk, parent=self)
        dlg.exec()

    def _split_view_fs(self, idx: int) -> None:
        """查看按键：直接打开文件系统浏览器（不经双击判断）。"""
        if not self._split_data or idx >= len(self._split_rows_data):
            return
        r = self._split_rows_data[idx]
        off, sz = r["offset"], r["size"]
        chunk = self._split_data[off: off + sz]
        dlg = _FsViewerDialog(r["name"], r.get("subtype_str", ""), chunk, parent=self)
        dlg.exec()

    def _split_resolve_output_dir(self) -> Path:
        out = self._split_output_edit.text().strip()
        if out:
            return Path(out)
        inp = self._split_input_edit.text().strip()
        if inp:
            return Path(inp).parent / (Path(inp).stem + "_split")
        return Path.cwd() / "split_output"

    def _split_extract_single(self, idx: int) -> None:
        if not self._split_data or idx >= len(self._split_rows_data):
            return
        row = self._split_rows_data[idx]
        off, sz = row["offset"], row["size"]
        default_name = f"{row['name']}_{off:#010x}.bin"
        # 总是弹出另存为对话框，让用户自己决定保存位置
        save_path, _ = get_save_file_name(
            self, "保存提取文件",
            default_name,
            "Binary Files (*.bin);;All Files (*.*)",
        )
        if not save_path:
            return
        chunk = self._split_data[off: off + sz]
        try:
            Path(save_path).write_bytes(chunk)
            self._append_log(f"✓ 已保存 {Path(save_path).name} ({_human_size(sz)})")
        except Exception as exc:
            self._append_log(f"✕ 保存失败: {exc}")

    def _split_extract_all(self) -> None:
        if not self._split_data or not self._split_rows_data:
            return
        # 若未设置输出目录，弹窗选择并写回字段
        out_str = self._split_output_edit.text().strip()
        if not out_str:
            chosen = get_existing_directory(self, "选择输出目录")
            if not chosen:
                return
            self._split_output_edit.setText(chosen)
            out_str = chosen
        out_dir = Path(out_str)
        out_dir.mkdir(parents=True, exist_ok=True)
        self._append_log(f"═══ 全部提取到 {out_dir} ═══")
        ok_count = 0
        for row in self._split_rows_data:
            off, sz = row["offset"], row["size"]
            chunk = self._split_data[off: off + sz]
            fname = f"{row['name']}_{off:#010x}.bin"
            try:
                (out_dir / fname).write_bytes(chunk)
                self._append_log(f"  ✓ {fname} ({_human_size(sz)})")
                ok_count += 1
            except Exception as exc:
                self._append_log(f"  ✕ {fname}: {exc}")
        self._append_log(f"提取完成 {ok_count}/{len(self._split_rows_data)}")

    # ═════════════════════════════════════════════════════════════════════════
    #  合成区
    # ═════════════════════════════════════════════════════════════════════════

    def _merge_add_entry(self, addr: str = "0x0", path: str = "") -> _EntryRow:
        row = _EntryRow(addr, path, parent=self._merge_entries_inner)
        row.remove_requested.connect(lambda r=row: self._merge_remove_entry(r))
        self._merge_rows.append(row)
        self._merge_entries_vbox.addWidget(row)
        self._merge_update_remove_btns()
        return row

    def _merge_remove_entry(self, row: _EntryRow) -> None:
        if len(self._merge_rows) <= 1:
            return
        self._merge_rows.remove(row)
        self._merge_entries_vbox.removeWidget(row)
        row.deleteLater()
        self._merge_update_remove_btns()

    def _merge_update_remove_btns(self) -> None:
        can = len(self._merge_rows) > 1
        for r in self._merge_rows:
            r.remove_btn.setEnabled(can)

    def _merge_load_flash_args(self) -> None:
        """读取 ESP-IDF build 目录中的 flash_args 或 flasher_args.json，填充条目和参数。"""
        chosen = get_existing_directory(self, "选择 ESP-IDF build 目录")
        if not chosen:
            return
        build_dir = Path(chosen)

        # 优先读取 flasher_args.json（信息最完整）
        json_path = build_dir / "flasher_args.json"
        txt_path = build_dir / "flash_args"
        entries: list[tuple[str, str]] = []
        chip = ""
        flash_mode = ""
        flash_freq = ""
        flash_size = ""

        if json_path.is_file():
            try:
                obj = json.loads(json_path.read_text(encoding="utf-8"))
                # flash_files: {"0x0": "bootloader/bootloader.bin", ...}
                for addr, rel in obj.get("flash_files", {}).items():
                    abs_path = build_dir / rel
                    entries.append((addr, str(abs_path)))
                settings = obj.get("flash_settings", {})
                flash_mode = settings.get("flash_mode", "")
                flash_freq = settings.get("flash_freq", "")
                flash_size = settings.get("flash_size", "")
                extra = obj.get("extra_esptool_args", {})
                chip = extra.get("chip", "")
            except Exception as exc:
                self._append_log(f"⚠ 读取 flasher_args.json 失败: {exc}")
        elif txt_path.is_file():
            try:
                lines = txt_path.read_text(encoding="utf-8").strip().splitlines()
                # 第一行：--flash-mode dio --flash-freq 80m --flash-size 8MB
                if lines:
                    first = lines[0]
                    m = re.search(r"--flash-mode\s+(\S+)", first)
                    if m:
                        flash_mode = m.group(1)
                    m = re.search(r"--flash-freq\s+(\S+)", first)
                    if m:
                        flash_freq = m.group(1)
                    m = re.search(r"--flash-size\s+(\S+)", first)
                    if m:
                        flash_size = m.group(1)
                # 后续行：0x0 bootloader/bootloader.bin
                for line in lines[1:]:
                    parts = line.strip().split(None, 1)
                    if len(parts) == 2 and parts[0].startswith("0x"):
                        abs_path = build_dir / parts[1]
                        entries.append((parts[0], str(abs_path)))
            except Exception as exc:
                self._append_log(f"⚠ 读取 flash_args 失败: {exc}")
        else:
            QMessageBox.warning(
                self, "提示",
                f"所选目录中未找到 flasher_args.json 或 flash_args 文件。\n"
                f"请选择 ESP-IDF 工程的 build 目录。",
            )
            return

        if not entries:
            QMessageBox.warning(self, "提示", "未从文件中解析到有效的烧录条目。")
            return

        # 清空现有条目
        while self._merge_rows:
            row = self._merge_rows.pop()
            self._merge_entries_vbox.removeWidget(row)
            row.deleteLater()

        # 填入条目
        for addr, path in entries:
            self._merge_add_entry(addr, path)

        # 设置芯片和 Flash 参数
        if chip:
            idx = self._merge_chip_combo.findText(chip, Qt.MatchFlag.MatchFixedString)
            if idx >= 0:
                self._merge_chip_combo.setCurrentIndex(idx)
        if flash_mode:
            idx = self._merge_flash_mode.findText(flash_mode, Qt.MatchFlag.MatchFixedString)
            if idx >= 0:
                self._merge_flash_mode.setCurrentIndex(idx)
        if flash_freq:
            idx = self._merge_flash_freq.findText(flash_freq, Qt.MatchFlag.MatchFixedString)
            if idx >= 0:
                self._merge_flash_freq.setCurrentIndex(idx)
        if flash_size:
            idx = self._merge_flash_size.findText(flash_size, Qt.MatchFlag.MatchFixedString)
            if idx >= 0:
                self._merge_flash_size.setCurrentIndex(idx)

        self._append_log(f"═══ 已加载 flash_args ({build_dir.name}) ═══")
        if chip:
            self._append_log(f"  芯片: {chip}")
        if flash_mode or flash_freq or flash_size:
            self._append_log(f"  Flash: mode={flash_mode} freq={flash_freq} size={flash_size}")
        for addr, path in entries:
            self._append_log(f"  {addr} {Path(path).name}")
        self._append_log(f"共 {len(entries)} 个条目。")

    def _merge_browse_output(self) -> None:
        fmt = self._merge_fmt_combo.currentText()
        ext_map = {"raw": "Binary Files (*.bin)", "uf2": "UF2 Files (*.uf2)", "hex": "Hex Files (*.hex)"}
        fp, _ = get_save_file_name(
            self, "保存合成文件", "",
            f"{ext_map.get(fmt, 'All Files (*.*)')};;All Files (*.*)",
        )
        if fp:
            self._merge_output_edit.setText(fp)

    def _merge_validate(self) -> list[tuple[str, str]] | None:
        entries: list[tuple[str, str]] = []
        for row in self._merge_rows:
            addr = row.addr_edit.text().strip()
            path = row.path_edit.text().strip()
            if not addr and not path:
                continue
            if not addr or not re.match(r"^0[xX][0-9a-fA-F]+$", addr):
                QMessageBox.warning(self, "提示", f"地址格式不正确：{addr!r}")
                return None
            if not path or not Path(path).is_file():
                QMessageBox.warning(self, "提示", f"固件文件无效：{path!r}")
                return None
            entries.append((addr, path))
        if not entries:
            QMessageBox.warning(self, "提示", "请至少填写一个有效的合成条目。")
            return None
        return entries

    def _merge_run(self) -> None:
        if self._merge_process is not None:
            QMessageBox.information(self, "提示", "合成任务正在运行。")
            return
        if not _tool_backend_available("esptool"):
            QMessageBox.critical(self, "错误", "未找到可用 esptool。")
            return
        entries = self._merge_validate()
        if entries is None:
            return
        chip = self._merge_chip_combo.currentText()
        output = self._merge_output_edit.text().strip()
        if not output:
            # 无输出路径时弹窗让用户选择
            fmt = self._merge_fmt_combo.currentText()
            ext_map = {"raw": ".bin", "uf2": ".uf2", "hex": ".hex"}
            ext = ext_map.get(fmt, ".bin")
            save_path, _ = get_save_file_name(
                self, "选择输出文件", f"merged{ext}",
                f"{fmt.upper()} Files (*{ext});;All Files (*.*)",
            )
            if not save_path:
                return
            self._merge_output_edit.setText(save_path)
            output = save_path
        out_dir = Path(output).parent
        if not out_dir.exists():
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                QMessageBox.critical(self, "错误", f"无法创建输出目录：{exc}")
                return

        fmt = self._merge_fmt_combo.currentText()
        cmd = list(_build_tool_command(
            "esptool", "--chip", chip, "merge-bin",
            "--output", output,
            "--format", fmt,
            "--flash_mode", self._merge_flash_mode.currentText(),
            "--flash_freq", self._merge_flash_freq.currentText(),
            "--flash_size", self._merge_flash_size.currentText(),
        ))
        for addr, path in entries:
            cmd.append(addr)
            cmd.append(path)

        self._append_log("═══ 开始合成 ═══")
        self._append_log("命令: " + subprocess.list2cmdline(cmd))
        self._merge_run_btn.setEnabled(False)
        self._merge_send_btn.setEnabled(False)
        self._merge_progress.setValue(0)
        self._merge_progress.setFormat("合成中…")

        process = QProcess(self)
        self._merge_process = process
        process.setProgram(cmd[0])
        process.setArguments(cmd[1:])
        process.setWorkingDirectory(str(TOOL_DIR))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        _inject_local_esptool_pythonpath(env)
        env.insert("PYTHONUTF8", "1")
        process.setProcessEnvironment(env)
        process.readyRead.connect(self._merge_read_output)
        process.finished.connect(self._merge_finished)
        process.errorOccurred.connect(self._merge_error)
        process.start()

    def _merge_read_output(self) -> None:
        if self._merge_process is None:
            return
        raw = self._merge_process.readAll()
        if raw:
            text = bytes(raw).decode("utf-8", errors="replace")
            for line in text.splitlines():
                self._append_log(line)

    def _merge_finished(self, exit_code: int, _exit_status) -> None:
        self._merge_process = None
        self._merge_run_btn.setEnabled(True)
        if exit_code == 0:
            output = self._merge_output_edit.text().strip()
            try:
                sz = Path(output).stat().st_size
                sz_str = _human_size(sz)
            except Exception:
                sz_str = "?"
            self._append_log(f"✓ 合成成功 → {output} ({sz_str})")
            self._merge_progress.setValue(100)
            self._merge_progress.setFormat("完成 100%")
            self._merge_send_btn.setEnabled(True)
        else:
            self._append_log(f"✕ 合成失败 (exit code {exit_code})")
            self._merge_progress.setFormat("合成失败")

    def _merge_error(self, error) -> None:
        self._merge_process = None
        self._merge_run_btn.setEnabled(True)
        self._append_log(f"✕ 进程错误: {error}")
        self._merge_progress.setFormat("进程错误")

    def _merge_stop(self) -> None:
        if self._merge_process is not None:
            self._merge_process.kill()
            self._append_log("已停止合成。")

    def _merge_send_to_flash(self) -> None:
        output = self._merge_output_edit.text().strip()
        if output and Path(output).is_file():
            self.send_to_flash_station.emit(output)
        else:
            QMessageBox.warning(self, "提示", "合成文件不存在，请先执行合成。")
