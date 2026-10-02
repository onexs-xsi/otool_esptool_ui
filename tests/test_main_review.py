from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QProcess, QRect, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from src.esptool_commands import validate_flash_entries
from src.flow_layout import FlowLayout
from src.main_window import OtoolEsptoolUI


def port(name="COM7", serial="UNIT-A", hwid="USB VID:PID=10C4:EA60"):
    return SimpleNamespace(device=name, serial_number=serial, location="1-2",
                           vid=0x10C4, pid=0xEA60, hwid=hwid, description="USB Serial")


class FlashEntryReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.firmware = Path(self.temp.name) / "固件.bin"
        self.firmware.write_bytes(b"firmware")

    def test_rejects_overlapping_erase_sectors_even_when_bytes_do_not_overlap(self):
        with self.assertRaisesRegex(ValueError, "重叠"):
            validate_flash_entries([("0x100", str(self.firmware)), ("0x200", str(self.firmware))])

    def test_accepts_adjacent_sectors_in_input_order_with_absolute_paths(self):
        entries = [("0x1000", str(self.firmware)), ("0x0", str(self.firmware))]
        self.assertEqual(validate_flash_entries(entries), entries)

    def test_rejects_empty_file_and_address_overflow(self):
        with self.assertRaisesRegex(ValueError, "地址范围"):
            validate_flash_entries([("0xffffffff", str(self.firmware))])
        self.firmware.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "空文件"):
            validate_flash_entries([("0x0", str(self.firmware))])

    def test_suspicious_path_is_repaired_only_when_literal_is_missing(self):
        literal_dir = Path(self.temp.name) / "firmware"
        literal_dir.mkdir()
        literal = literal_dir / "_app.bin"
        literal.write_bytes(b"literal")
        candidate = Path(self.temp.name) / "firmware_app.bin"
        candidate.write_bytes(b"candidate")
        self.assertEqual(validate_flash_entries([("0x0", str(literal))])[0][1], str(literal))
        literal.unlink()
        self.assertEqual(validate_flash_entries([("0x0", str(literal))])[0][1], str(candidate))


class MainWindowReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.ports = patch("src.main_window.list_ports.comports", return_value=[port()])
        self.comports = self.ports.start()
        self.addCleanup(self.ports.stop)
        self.window = OtoolEsptoolUI()
        self.app.processEvents()
        self.did, self.card = next(iter(self.window.device_cards.items()))

    def tearDown(self):
        for did, card in list(self.window.device_cards.items()):
            if card.process is not None:
                if isinstance(card.process, QProcess):
                    self.window.stop_process(did)
                else:
                    self.window._cleanup_process(did)
        self.window.stop_all_devices()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_refresh_preserves_success_failure_and_new_badge_does_not_override(self):
        for state, count_label in (("success", self.window.success_stat), ("error", self.window.failed_stat)):
            self.card.set_result_state("本次结果", state)
            self.window.refresh_ports()
            self.assertEqual(self.card.property("cardState"), state)
            self.assertEqual(self.card.state_value.text(), "本次结果")
            self.assertTrue(count_label.text().endswith("1"))

    def test_unchanged_scan_does_not_reparent_or_rebuild_cards(self):
        with patch.object(self.window.device_layout, "takeAt") as remove:
            self.window.refresh_ports()
        remove.assert_not_called()

    def test_duplicate_usb_serial_numbers_remain_separate_devices(self):
        self.comports.return_value = [port("COM7"), port("COM8")]
        self.window.refresh_ports()
        self.assertEqual({card.device.port for card in self.window.device_cards.values()}, {"COM7", "COM8"})

    def test_scan_failure_preserves_existing_device(self):
        self.comports.side_effect = OSError("enumeration failed")
        self.window.refresh_ports()
        self.assertIs(self.window.device_cards[self.did], self.card)
        self.assertIn("扫描失败", self.window.status_label.text())

    def test_auto_flash_callback_respects_disarm_stop_and_replaced_card(self):
        self.window.auto_flash_button.setChecked(True)
        generation = self.window._auto_flash_generation
        with patch.object(self.window, "flash_firmware") as flash:
            self.window.auto_flash_button.setChecked(False)
            self.window._auto_flash_new_device(self.did, self.card, generation)
            self.window.auto_flash_button.setChecked(True)
            self.window._auto_flash_new_device(self.did, self.card, generation)
            self.window._auto_flash_new_device(self.did, object(), self.window._auto_flash_generation)
            self.window.stop_all_devices()
            self.window._auto_flash_new_device(self.did, self.card, self.window._auto_flash_generation)
        flash.assert_not_called()

    def test_invalid_baud_is_rejected_before_starting_worker(self):
        with patch("src.main_window.QMessageBox.warning"), patch.object(self.window, "_start_process") as start:
            for baud in ("0", "²", "-1", "2147483648"):
                self.window.baud_edit.setCurrentText(baud)
                self.window.reset_device(self.did)
        start.assert_not_called()

    def fake_process(self):
        process = Mock()
        process.readAll.return_value = b""
        self.card.process = process
        lease = self.window._port_registry.acquire(self.card.device.port, "test", "test")
        self.window._device_port_leases[self.did] = lease
        return process

    def test_stale_output_error_and_finished_callbacks_cannot_touch_new_process(self):
        process = self.fake_process()
        stale = Mock()
        self.window._read_process_output(self.did, stale)
        self.window._process_error(self.did, QProcess.ProcessError.FailedToStart, stale)
        self.window._process_finished(self.did, 0, QProcess.ExitStatus.NormalExit, stale)
        self.assertIs(self.card.process, process)
        process.readAll.assert_not_called()
        self.assertFalse(self.window._port_registry.is_available(self.card.device.port))

    def test_nonterminal_process_error_keeps_serial_lease(self):
        process = self.fake_process()
        self.window._process_error(self.did, QProcess.ProcessError.ReadError, process)
        self.assertIs(self.card.process, process)
        self.assertFalse(self.window._port_registry.is_available(self.card.device.port))

    def test_crash_exit_zero_is_failure(self):
        process = self.fake_process()
        self.window._process_finished(self.did, 0, QProcess.ExitStatus.CrashExit, process)
        self.assertEqual(self.card.property("cardState"), "error")
        self.assertTrue(self.window._port_registry.is_available(self.card.device.port))

    def test_stopping_task_cannot_trigger_chip_mismatch_retry(self):
        process = self.fake_process()
        self.card._operation_output = "This chip is ESP32-C6, not ESP32."
        self.card._last_command = ["worker", "--chip", "esp32"]
        self.card._stop_requested = True
        self.window._process_finished(self.did, 1, QProcess.ExitStatus.CrashExit, process)
        self.assertFalse(self.window._pending_retries)
        self.assertEqual(self.card.state_value.text(), "已停止")

    def test_cancel_retry_prevents_later_start(self):
        self.fake_process()
        self.card._operation_output = "This chip is ESP32-C6, not ESP32."
        self.card._last_command = ["worker", "--chip", "esp32"]
        self.assertTrue(self.window._try_auto_retry(self.card, self.did))
        self.assertEqual(self.window.running_stat.text(), "运行中 1")
        with patch.object(self.window, "_start_process") as start:
            self.window.stop_process(self.did)
            QTest.qWait(350)
        start.assert_not_called()
        self.assertFalse(self.window._pending_retries)

    def test_retry_port_contention_restores_action_buttons(self):
        self.card.set_running_state("等待重试", True, "reset")
        lease = self.window._port_registry.acquire(self.card.device.port, "terminal", "终端")
        self.addCleanup(lease.release)
        with patch("src.main_window.QMessageBox.information"):
            self.window._start_process(self.card, ["worker"], "重试", "reset", _is_retry=True)
        self.assertEqual(self.card.property("cardState"), "error")
        self.assertEqual(self.card.reset_button.text(), "复位")
        self.assertTrue(self.card.flash_button.isEnabled())

    def test_efuse_dialog_rejects_invalid_baud(self):
        self.window.baud_edit.setCurrentText("0")
        with patch("src.main_window.QMessageBox.warning"), patch("src.main_window.EFuseDialog") as dialog:
            self.window._open_efuse_dialog(self.did)
        dialog.assert_not_called()

    def test_previous_operation_logs_cannot_trigger_retry_or_replace_metadata(self):
        self.card.append_log("This chip is ESP32-C6, not ESP32.\nChip type: ESP32-C6")
        self.card._last_command = ["worker", "--chip", "esp32"]
        self.card._operation_output = "Chip type: ESP32-S3\n"
        self.assertFalse(self.window._try_auto_retry(self.card, self.did))
        self.window._update_device_info_from_log(self.card, self.did)
        self.assertEqual(self.card.device.chip_name, "ESP32-S3")

    def test_worker_stream_preserves_split_chinese_and_ignores_malformed_events(self):
        process = self.fake_process()
        for chunk in [b"path: \xe4", b"\xb8", b"\xad\n"]:
            process.readAll.return_value = chunk
            self.window._read_process_output(self.did, process)
        self.window._handle_process_output_line(self.card, '[[OTOOL_EVENT]][]')
        self.window._handle_process_output_line(self.card, '[[OTOOL_EVENT]]{"type":"progress","percent":"bad"}')
        self.assertIn("path: 中", self.card.log_edit.toPlainText())

    def test_efuse_shortcut_prefills_protected_dialog(self):
        with patch("src.main_window.EFuseDialog") as dialog, patch.object(self.window, "_start_process") as start:
            self.window._burn_efuse_preset(self.did, "FIELD", "1", "preset")
        dialog.return_value._apply_preset.assert_called_once_with("FIELD", "1")
        dialog.return_value.exec.assert_called_once()
        start.assert_not_called()
        self.assertTrue(self.window._port_registry.is_available(self.card.device.port))

    def test_real_worker_success_updates_status_and_releases_lease(self):
        self.window._start_process(self.card, [sys.executable, "-c", "print('Chip type: ESP32-S3')"], "测试", "reset")
        for _ in range(100):
            self.app.processEvents()
            if self.card.process is None:
                break
            QTest.qWait(20)
        self.assertIsNone(self.card.process)
        self.assertEqual(self.card.property("cardState"), "success")
        self.assertEqual(self.card.device.chip_name, "ESP32-S3")
        self.assertTrue(self.window._port_registry.is_available(self.card.device.port))

    def test_failed_to_start_is_terminal_and_releases_lease(self):
        self.window._start_process(self.card, ["__missing_otool_test_executable__"], "测试", "reset")
        for _ in range(50):
            self.app.processEvents()
            if self.card.process is None:
                break
            QTest.qWait(20)
        self.assertIsNone(self.card.process)
        self.assertEqual(self.card.property("cardState"), "error")
        self.assertTrue(self.window._port_registry.is_available(self.card.device.port))

    def test_flow_layout_exact_fit_does_not_wrap(self):
        parent = QWidget()
        layout = FlowLayout(parent, h_spacing=12, v_spacing=12)
        layout.setContentsMargins(0, 0, 0, 0)
        children = [QWidget(), QWidget()]
        for child in children:
            child.setFixedSize(100, 30)
            layout.addWidget(child)
        parent.show()
        layout.setGeometry(QRect(0, 0, 212, 100))
        self.assertEqual(children[1].geometry().y(), 0)
        layout.setGeometry(QRect(0, 0, 211, 100))
        self.assertEqual(children[1].geometry().y(), 42)
        parent.close()


if __name__ == "__main__":
    unittest.main()
