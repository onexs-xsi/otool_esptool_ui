from __future__ import annotations

import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import serial
from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import QApplication

from src.export_dialog import ExportFlashDialog
from src.merge_split_widget import (
    PT_MAGIC,
    MergeSplitWidget,
    _FsViewerDialog,
    _contained_output_path,
    _parse_partition_table,
    analyze_merged_bin,
)
from src.models import DeviceInfo
from src.port_ownership import PortOwnershipRegistry
from src.terminal_widget import TerminalSerialConfig, TerminalSerialThread, TerminalWidget


def partition_record(name: bytes = b"factory") -> bytes:
    return struct.pack("<HBBII16sI", PT_MAGIC, 0, 0, 0x10000, 0x1000, name, 1)


def firmware(name: bytes = b"factory") -> bytes:
    data = bytearray(b"\xff" * 0x11000)
    data[0x1000:0x1008] = b"\xe9\x01\x00\x00\x00\x00\x00\x00"
    data[0x8000:0x8020] = partition_record(name)
    data[0x10000:0x10004] = b"test"
    return bytes(data)


class PartitionReviewTests(unittest.TestCase):
    def test_missing_partition_table_never_creates_extractable_regions(self) -> None:
        for data in (b"", b"\xff" * 0x10000, b"\x00" * 0x10000):
            with self.subTest(length=len(data)):
                result = analyze_merged_bin(data)
                self.assertIsNone(result["partition_table"])
                self.assertIsNone(result["bootloader"])
                self.assertEqual(result["partitions"], [])
                self.assertTrue(result["warnings"])

    def test_bootloader_region_preserves_trailing_ff_bytes(self) -> None:
        data = firmware()
        result = analyze_merged_bin(data, "esp32")
        bootloader = result["bootloader"]
        self.assertEqual(bootloader["offset"], 0x1000)
        self.assertEqual(bootloader["size"], 0x7000)
        self.assertEqual(data[0x1000:0x1000 + bootloader["size"]], data[0x1000:0x8000])
        self.assertEqual(result["partition_table"]["size"], 0xC00)
        self.assertTrue(result["partitions"][0]["encrypted"])

    def test_corrupt_partition_record_does_not_resynchronize_inside_random_data(self) -> None:
        self.assertEqual(_parse_partition_table(b"\x00" * 32 + partition_record()), [])


class TerminalSerialReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.thread = TerminalSerialThread(
            TerminalSerialConfig("COM_FAKE", 115200, 8, "N", 1, "none")
        )

    def test_partial_serial_writes_deliver_each_byte_exactly_once(self) -> None:
        delivered = bytearray()
        sink = MagicMock()

        def write(data: bytes) -> int:
            count = min(2, len(data))
            delivered.extend(data[:count])
            return count

        sink.write.side_effect = write
        self.thread._serial = sink
        self.thread.send(b"abcdefg")
        self.thread._drain_send_queue()
        self.assertEqual(delivered, b"abcdefg")
        sink.flush.assert_called_once()

    def test_zero_progress_raises_instead_of_silently_losing_payload(self) -> None:
        sink = MagicMock()
        sink.write.return_value = 0
        self.thread._serial = sink
        self.thread.send(b"data")
        with self.assertRaises(serial.SerialTimeoutException):
            self.thread._drain_send_queue()

    def test_stop_request_does_not_drain_pending_transmissions(self) -> None:
        sink = MagicMock()
        self.thread._serial = sink
        self.thread.send(b"data")
        self.thread.request_stop()
        self.thread._drain_send_queue()
        sink.write.assert_not_called()

    def test_continuous_transmissions_leave_time_for_receive_loop(self) -> None:
        sink = MagicMock()
        sink.write.side_effect = len
        self.thread._serial = sink
        for _ in range(1000):
            self.thread.send(b"a")
        self.thread._drain_send_queue()
        self.assertGreater(sink.write.call_count, 0)
        self.assertLess(sink.write.call_count, 1000)
        self.assertFalse(self.thread._send_queue.empty())


class WidgetReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.widget = MergeSplitWidget(port_registry=PortOwnershipRegistry())

    def tearDown(self) -> None:
        self.widget.shutdown()
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_partition_labels_cannot_escape_output_directory(self) -> None:
        for label in (b"../escape", b"..\\escape", b"C:\\escape", b"CON"):
            data = firmware(label)
            self.widget._split_data = data
            self.widget._split_rows_data = analyze_merged_bin(data)["partitions"]
            output = self.root / label.hex()
            self.widget._split_output_edit.setText(str(output))
            self.widget._split_extract_all()
            files = list(output.iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].parent, output)
            self.assertEqual(files[0].read_bytes(), data[0x10000:0x11000])
        self.assertTrue(all(path.is_dir() for path in self.root.iterdir()))

    def test_changing_analyzed_input_invalidates_extract_actions(self) -> None:
        image = self.root / "firmware.bin"
        image.write_bytes(firmware())
        self.widget._split_input_edit.setText(str(image))
        self.widget._split_analyze()
        self.assertTrue(self.widget._split_extract_btn.isEnabled())
        self.widget._split_input_edit.setText(str(self.root / "missing.bin"))
        self.assertFalse(self.widget._split_extract_btn.isEnabled())
        self.assertEqual(self.widget._split_data, b"")
        self.assertEqual(self.widget._split_table.rowCount(), 0)

    def test_extraction_directory_failure_is_reported_without_exception(self) -> None:
        output = self.root / "a_file"
        output.write_bytes(b"existing")
        data = firmware()
        self.widget._split_data = data
        self.widget._split_rows_data = analyze_merged_bin(data)["partitions"]
        self.widget._split_output_edit.setText(str(output))
        self.widget._split_extract_all()
        self.assertEqual(output.read_bytes(), b"existing")
        self.assertIn("无法创建输出目录", self.widget._log.toPlainText())

    def test_embedded_file_paths_cannot_escape_output_directory(self) -> None:
        for path in ("../escape", "..\\escape", "C:\\escape", "/escape"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                _contained_output_path(self.root, path)

    def test_selected_files_with_same_basename_keep_their_directories(self) -> None:
        with patch.object(_FsViewerDialog, "_try_mount"):
            dialog = _FsViewerDialog("fs", "littlefs", b"")
        first, second = MagicMock(), MagicMock()
        first.data.return_value = "/first/config.txt"
        second.data.return_value = "/second/config.txt"
        try:
            with (
                patch.object(dialog._tree, "selectedItems", return_value=[first, second]),
                patch.object(dialog, "_read_file", side_effect=[b"first", b"second"]),
                patch("src.merge_split_widget.get_existing_directory", return_value=str(self.root)),
                patch("src.merge_split_widget.QMessageBox.information"),
            ):
                dialog._extract_selected()
            self.assertEqual((self.root / "first/config.txt").read_bytes(), b"first")
            self.assertEqual((self.root / "second/config.txt").read_bytes(), b"second")
        finally:
            dialog.close()
            dialog.deleteLater()

    def _input_entry(self, address: str, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        if self.widget._merge_rows[0].path_edit.text():
            row = self.widget._merge_add_entry()
        else:
            row = self.widget._merge_rows[0]
        row.addr_edit.setText(address)
        row.path_edit.setText(str(path))
        return path

    def test_overlapping_merge_ranges_are_rejected_before_launch(self) -> None:
        self._input_entry("0x1000", "a.bin", b"a" * 32)
        self._input_entry("0x1010", "b.bin", b"b" * 32)
        with patch("src.merge_split_widget.QMessageBox.warning") as warning:
            self.assertIsNone(self.widget._merge_validate())
        self.assertIn("重叠", warning.call_args.args[2])

    def test_output_cannot_overwrite_input_firmware(self) -> None:
        path = self._input_entry("0x0", "a.bin", b"original")
        self.widget._merge_output_edit.setText(str(path))
        with (
            patch("src.merge_split_widget._tool_backend_available", return_value=True),
            patch("src.merge_split_widget.QMessageBox.warning") as warning,
            patch("src.merge_split_widget.QProcess") as process,
        ):
            self.widget._merge_run()
        process.assert_not_called()
        self.assertIn("输入固件相同", warning.call_args.args[2])
        self.assertEqual(path.read_bytes(), b"original")

    def _finish_merge(self, output: Path, fmt: str = "raw") -> None:
        self.widget._merge_process = QProcess(self.widget)
        self.widget._merge_run_output = str(output)
        self.widget._merge_run_format = fmt
        self.widget._merge_finished(0, QProcess.ExitStatus.NormalExit)

    def test_successful_merge_uses_snapshot_even_if_output_field_changes(self) -> None:
        actual = self.root / "actual.bin"
        actual.write_bytes(b"merged")
        unrelated = self.root / "unrelated.bin"
        unrelated.write_bytes(b"do not flash")
        self.widget._merge_output_edit.setText(str(unrelated))
        emitted: list[str] = []
        self.widget.send_to_flash_station.connect(emitted.append)
        self._finish_merge(actual)
        self.widget._merge_send_to_flash()
        self.assertEqual(emitted, [str(actual)])
        self.assertIn(str(actual), self.widget._log.toPlainText())

    def test_hex_and_uf2_outputs_cannot_be_sent_as_raw_firmware(self) -> None:
        for fmt in ("hex", "uf2"):
            with self.subTest(fmt=fmt):
                output = self.root / ("merged." + fmt)
                output.write_bytes(b"formatted output")
                self._finish_merge(output, fmt)
                self.assertFalse(self.widget._merge_send_btn.isEnabled())
                with patch("src.merge_split_widget.QMessageBox.warning"):
                    emitted: list[str] = []
                    self.widget.send_to_flash_station.connect(emitted.append)
                    self.widget._merge_send_to_flash()
                    self.assertEqual(emitted, [])

    def test_missing_output_is_not_reported_as_merge_success(self) -> None:
        self._finish_merge(self.root / "missing.bin")
        self.assertFalse(self.widget._merge_send_btn.isEnabled())
        self.assertEqual(self.widget._merge_progress.format(), "合成失败")

    def test_background_terminal_reconnect_keeps_active_session(self) -> None:
        with patch("src.terminal_widget.list_ports.comports", return_value=[]):
            terminal = TerminalWidget(port_registry=PortOwnershipRegistry())
            try:
                first = terminal._active_session()
                terminal._new_session()
                second = terminal._active_session()
                with patch("src.terminal_widget.TerminalSerialThread") as thread:
                    terminal._start_serial_thread(
                        TerminalSerialConfig("COM_FAKE", 115200, 8, "N", 1, "none"),
                        session_id=first.session_id,
                    )
                    self.assertEqual(terminal._active_session_id, second.session_id)
                    self.assertIs(terminal._records, second.records)
                    self.assertIsNone(second.serial_thread)
                    thread.return_value.start.assert_called_once()
            finally:
                terminal.shutdown()
                terminal.close()
                terminal.deleteLater()


class ExportReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_zero_sizes_and_address_overflow_are_invalid(self) -> None:
        for size in ("0", "0x0", "0k", "0M", "0x100000001"):
            self.assertFalse(ExportFlashDialog._valid_size(size), size)
        for size in ("ALL", "1", "512k", "4M", "0x100000"):
            self.assertTrue(ExportFlashDialog._valid_size(size), size)
        self.assertFalse(ExportFlashDialog._valid_address("0x100000000"))

    def test_full_flash_export_requires_zero_start_address(self) -> None:
        dialog = ExportFlashDialog(DeviceInfo("id", "COM_FAKE", "test"), "115200")
        try:
            dialog.address_edit.setText("0x1000")
            with (
                patch("src.export_dialog.QMessageBox.warning") as warning,
                patch.object(dialog, "accept") as accept,
            ):
                dialog._accept_if_valid()
            accept.assert_not_called()
            self.assertIn("起始地址必须", warning.call_args.args[2])
        finally:
            dialog.close()
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
