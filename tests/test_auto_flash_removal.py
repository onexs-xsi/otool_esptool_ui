from __future__ import annotations

import os
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, QProcess, pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from src.device_policy import DevicePolicyStore
from src.main_window import OtoolEsptoolUI


def port(name="COM7", serial="BOARD-A"):
    return SimpleNamespace(device=name, serial_number=serial, location="1-2", vid=0x10C4,
                           pid=0xEA60, hwid="USB", description=f"USB Serial ({name})")


class AutomaticRemovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        avatar = patch("src.main_window._fetch_remote_avatar_bytes")
        avatar.start()
        self.addCleanup(avatar.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="otool-removal-")
        self.addCleanup(self.temporary.cleanup)
        enumeration = patch("serial.tools.list_ports.comports", return_value=[port()])
        self.ports = enumeration.start()
        self.addCleanup(enumeration.stop)
        self.probe_result = {"status": "present", "mac": "02:00:00:00:00:01", "chip": "ESP32-P4"}
        testcase = self
        class FakeProbe(QObject):
            finished = pyqtSignal(dict)
            def __init__(self, name, registry, parent=None):
                super().__init__(parent)
                self.port = name
            def start(self):
                self.finished.emit({**testcase.probe_result, "port": self.port})
                self.deleteLater()
            def cancel(self):
                return True
        probe = patch("src.main_window.ChipInfoProbe", FakeProbe)
        probe.start()
        self.addCleanup(probe.stop)
        self.window = OtoolEsptoolUI(DevicePolicyStore(Path(self.temporary.name) / "policy.json"))
        self.app.processEvents()
        self.did, self.card = next(iter(self.window.device_cards.items()))
        self.window.auto_flash_button.setChecked(True)

    def tearDown(self):
        self.window.stop_all_devices()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def finish(self, automatic=True, exit_code=0):
        self.card.device.mac = "02:00:00:00:00:01"
        process = Mock()
        process.readAll.return_value = b""
        self.card.process = process
        self.card._last_command = ["worker", "write-flash"]
        self.card._last_command_meta = {
            "active_action": "flash", "automatic_flash": automatic,
            "auto_generation": self.window._auto_flash_generation, "port_generation": 0,
        }
        self.window._process_finished(self.did, exit_code, QProcess.ExitStatus.NormalExit, process)

    def test_success_enters_three_second_monitor_and_logs_each_online_check(self):
        self.finish()
        timer = self.window._auto_flash_removal_timers["com7"]
        self.assertEqual(timer.interval(), 3000)
        self.assertEqual(self.card.property("cardState"), "success")
        self.assertIn("请拔出当前设备", self.card.log_edit.toPlainText())
        with patch.object(self.window, "flash_firmware") as flash:
            for _ in range(3):
                timer.timeout.emit()
                self.window.refresh_ports()
                self.window._auto_flash_new_device(self.did)
        flash.assert_not_called()
        log = self.card.log_edit.toPlainText()
        self.assertIn("第 1 次：芯片仍在线", log)
        self.assertIn("第 3 次：芯片仍在线", log)
        self.assertTrue(self.window._auto_flash_cycles["com7"].waiting_for_removal)

    def test_manual_task_cancels_background_chip_probe_before_claiming_port(self):
        self.finish()
        probe=Mock()
        self.window._chip_probes["com7"]=probe
        self.window._start_process(self.card,[sys.executable,"-c","pass"],"读取信息",active_action="read")
        probe.cancel.assert_called_once()
        for _ in range(50):
            QTest.qWait(20)
            if self.card.process is None: break
        self.assertIsNone(self.card.process)

    def test_disappearance_is_confirmed_by_monitor_and_ready_log_survives_refresh(self):
        self.finish()
        self.ports.return_value = []
        self.window.refresh_ports()
        self.assertIs(self.window.device_cards[self.did], self.card)
        self.assertTrue(self.window._auto_flash_cycles["com7"].waiting_for_removal)
        self.window._poll_auto_flash_removal("com7")
        self.assertFalse(self.window._auto_flash_cycles["com7"].waiting_for_removal)
        self.assertTrue(self.window._auto_flash_removal_timers)
        self.window.refresh_ports()
        self.assertIs(self.window.device_cards[self.did], self.card)
        self.assertIn("已确认设备移除", self.card.log_edit.toPlainText())
        self.assertIn("已就绪，等待下一台设备接入", self.card.log_edit.toPlainText())
        self.assertFalse(self.card.flash_button.isEnabled())

    def test_reconnection_reuses_result_and_history_then_starts_exactly_once(self):
        self.finish()
        self.ports.return_value = []
        self.window._poll_auto_flash_removal("com7")
        self.ports.return_value = [port(serial="BOARD-B")]
        with patch.object(self.window, "_validate_common_inputs", return_value=True), patch.object(self.window, "flash_firmware") as flash:
            self.window.refresh_ports()
            self.window._poll_auto_flash_removal("com7")
            new_did, new_card = next(iter(self.window.device_cards.items()))
            self.assertIs(new_card, self.card)
            self.assertNotEqual(new_did, self.did)
            QTest.qWait(220)
            self.window.refresh_ports()
        self.assertEqual(flash.call_count, 1)
        self.assertIn("已就绪，等待下一台设备接入", self.card.log_edit.toPlainText())
        self.assertIn("开始下一轮烧录准备", self.card.log_edit.toPlainText())
        with patch.object(self.window, "_handle_device_action") as action:
            self.card.flash_button.click()
        action.assert_called_once_with(new_did, "flash")

    def test_same_device_changing_com_port_stays_in_wait_state(self):
        self.finish()
        self.ports.return_value = [port("COM99")]
        self.window.refresh_ports()
        with patch.object(self.window, "flash_firmware") as flash:
            self.window._poll_auto_flash_removal("com7")
            for did in self.window.device_cards:
                self.window._auto_flash_new_device(did)
        flash.assert_not_called()
        self.assertTrue(self.window._auto_flash_cycles["com7"].waiting_for_removal)
        self.assertIn("COM99", self.card.log_edit.toPlainText())

    def test_replacement_before_poll_is_not_started_until_original_removal_is_confirmed(self):
        self.finish()
        self.ports.return_value = [port(serial="BOARD-B")]
        self.probe_result["mac"] = "02:00:00:00:00:02"
        with patch.object(self.window, "_validate_common_inputs", return_value=True), patch.object(self.window, "flash_firmware") as flash:
            self.window.refresh_ports()
            QTest.qWait(150)
            flash.assert_not_called()
            self.window._poll_auto_flash_removal("com7")
            QTest.qWait(220)
        self.assertEqual(flash.call_count, 1)
        self.assertEqual(len(self.window.device_cards), 1)
        self.assertIn("已确认设备移除", next(iter(self.window.device_cards.values())).log_edit.toPlainText())

    def test_scan_error_does_not_treat_device_as_removed(self):
        self.finish()
        self.ports.side_effect = OSError("enumeration failed")
        self.window._poll_auto_flash_removal("com7")
        self.assertTrue(self.window._auto_flash_cycles["com7"].waiting_for_removal)
        self.assertIn("尚未确认设备移除", self.card.log_edit.toPlainText())

    def test_adapter_remains_connected_but_chip_is_missing(self):
        self.finish()
        self.probe_result = {"status": "absent", "mac": "", "message": "未读取到芯片信息"}
        self.window._poll_auto_flash_removal("com7")
        self.assertTrue(self.window._auto_flash_cycles["com7"].waiting_for_removal)
        self.window._poll_auto_flash_removal("com7")
        self.assertFalse(self.window._auto_flash_cycles["com7"].waiting_for_removal)
        with patch.object(self.window, "flash_firmware") as flash:
            self.window.refresh_ports()
            self.window._poll_auto_flash_removal("com7")
            QTest.qWait(160)
        flash.assert_not_called()
        self.assertIn("已确认设备移除", self.card.log_edit.toPlainText())

    def test_busy_port_is_not_treated_as_chip_disconnection(self):
        self.finish()
        self.probe_result = {"status": "busy", "mac": "", "message": "串口被占用"}
        self.window._poll_auto_flash_removal("com7")
        self.window._poll_auto_flash_removal("com7")
        self.assertTrue(self.window._auto_flash_cycles["com7"].waiting_for_removal)

    def test_disarm_rearm_and_clear_do_not_bypass_wait_for_removal(self):
        self.finish()
        self.window.auto_flash_button.setChecked(False)
        self.assertFalse(self.window._auto_flash_removal_timers)
        self.window.auto_flash_button.setChecked(True)
        self.assertIn("com7", self.window._auto_flash_removal_timers)
        self.window.clear_devices()
        with patch.object(self.window, "flash_firmware") as flash:
            self.window.refresh_ports()
            QTest.qWait(220)
            self.window._auto_flash_new_device(self.did)
        flash.assert_not_called()
        self.assertTrue(self.window._auto_flash_cycles["com7"].waiting_for_removal)

    def test_port_auto_is_independent_and_stop_cancels_monitor(self):
        self.window._port_auto_flash.add("com7")
        self.window.auto_flash_button.setChecked(False)
        self.finish()
        self.assertIn("com7", self.window._auto_flash_removal_timers)
        self.window.stop_all_devices()
        self.assertFalse(self.window._auto_flash_removal_timers)
        self.assertTrue(self.window._auto_flash_cycles["com7"].stopped)

    def test_manual_success_and_automatic_failure_do_not_start_removal_monitor(self):
        self.finish(automatic=False)
        self.assertFalse(self.window._auto_flash_cycles)
        self.finish(exit_code=1)
        self.assertFalse(self.window._auto_flash_cycles)
        self.assertTrue(self.window._pending_retries)


if __name__ == "__main__":
    unittest.main()
