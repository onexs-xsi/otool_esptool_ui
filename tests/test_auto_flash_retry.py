from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QProcess
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from src.device_policy import DevicePolicyStore
from src.main_window import OtoolEsptoolUI

ROOT = Path(__file__).resolve().parents[1]


class AutomaticRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        port = SimpleNamespace(device="COM7", serial_number="TEST", location="1-2",
                               vid=0x10C4, pid=0xEA60, hwid="USB", description="USB Serial")
        self.enumeration = patch("serial.tools.list_ports.comports", return_value=[port])
        self.ports = self.enumeration.start()
        self.addCleanup(self.enumeration.stop)
        self.window = OtoolEsptoolUI(DevicePolicyStore(Path(self.temp.name) / "policy.json"))
        self.app.processEvents()
        self.did, self.card = next(iter(self.window.device_cards.items()))
        self.window.auto_flash_button.setChecked(True)
        self.command = [sys.executable, "-c", "raise SystemExit(1)"]

    def tearDown(self):
        self.window.stop_all_devices()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def finish_attempt(self, *, automatic=True, text="Connecting...", exit_code=1):
        process = Mock()
        process.readAll.return_value = b""
        self.card.process = process
        self.card._operation_output = text
        self.card.append_log(text)
        self.card._last_command = self.command.copy()
        self.card._last_command_meta = {
            "active_action": "flash", "automatic_flash": automatic,
            "auto_generation": self.window._auto_flash_generation,
            "port_generation": self.window._port_auto_generations.get("com7", 0),
        }
        self.window._process_finished(self.did, exit_code, QProcess.ExitStatus.NormalExit, process)

    def test_connection_failure_keeps_retrying_every_ten_seconds_until_success(self):
        with patch.object(self.window, "_start_process") as start:
            for _ in range(3):
                self.finish_attempt()
                self.assertIn(self.did, self.window._pending_retries)
                timer = self.window._pending_retries[self.did]
                self.assertEqual(timer.interval(), 10000)
                self.assertEqual(self.card.flash_button.text(), "停止")
                self.assertIn("10 秒", self.card.state_value.text())
                self.assertIn("Connecting", self.card.log_edit.toPlainText() + self.card._operation_output)
                timer.timeout.emit()
            self.assertEqual(start.call_count, 3)
        self.finish_attempt(exit_code=0)
        self.assertFalse(self.window._pending_retries)
        self.assertEqual(self.card.property("cardState"), "success")

    def test_manual_failure_does_not_enter_automatic_loop(self):
        self.finish_attempt(automatic=False)
        self.assertFalse(self.window._pending_retries)

    def test_disarming_cancels_pending_retry_and_restores_actions(self):
        self.finish_attempt()
        self.assertIn(self.did, self.window._pending_retries)
        timer = self.window._pending_retries[self.did]
        with patch.object(self.window, "_start_process") as start:
            self.window.auto_flash_button.setChecked(False)
            timer.timeout.emit()
        start.assert_not_called()
        self.assertFalse(self.window._pending_retries)
        self.assertEqual(self.card.flash_button.text(), "烧录")

    def test_stop_block_and_disconnect_cancel_waiting_retry(self):
        for action in ("stop", "block", "disconnect"):
            with self.subTest(action=action):
                self.card._stop_requested = False
                self.finish_attempt()
                timer = self.window._pending_retries[self.did]
                with patch.object(self.window, "_start_process") as start:
                    if action == "stop":
                        self.window.stop_all_devices()
                    elif action == "block":
                        self.window._set_device_blocked(self.did, "port", True)
                    else:
                        self.ports.return_value = []
                        self.window.refresh_ports()
                    timer.timeout.emit()
                start.assert_not_called()
                self.assertFalse(self.window._pending_retries)
                if action == "block":
                    self.window._set_device_blocked(self.did, "port", False)

    def test_inflight_disarm_and_rearm_does_not_revive_old_attempt(self):
        self.finish_attempt()
        self.window.auto_flash_button.setChecked(False)
        self.window.auto_flash_button.setChecked(True)
        self.assertFalse(self.window._try_auto_retry(self.card, self.did))

    def test_locked_chip_mismatch_is_not_retried_or_rewritten(self):
        self.card._last_command = ["worker", "--chip", "esp32s3", "write-flash"]
        self.card._last_command_meta = {"automatic_flash": True, "expected_chip": "esp32s3"}
        self.card._operation_output = "This chip is ESP32-C6, not ESP32-S3."
        self.assertFalse(self.window._try_auto_retry(self.card, self.did))
        self.assertFalse(self.window._pending_retries)

    def test_port_only_auto_retries_and_global_disarm_keeps_it(self):
        self.window._port_auto_flash.add("com7")
        self.window.auto_flash_button.setChecked(False)
        self.finish_attempt()
        self.assertIn(self.did, self.window._pending_retries)
        self.window.auto_flash_button.setChecked(True)
        self.window.auto_flash_button.setChecked(False)
        self.assertIn(self.did, self.window._pending_retries)
        self.window._set_port_auto_flash(self.did, False)
        self.assertFalse(self.window._pending_retries)
        self.assertEqual(self.card.flash_button.text(), "烧录")

    def test_automatic_contention_retries_without_blocking_dialog(self):
        lease = self.window._port_registry.acquire("COM7", "terminal", "test")
        self.addCleanup(lease.release)
        with patch("src.main_window.QMessageBox.information") as dialog:
            self.window._start_process(self.card, self.command, "烧录", "flash", automatic_flash=True)
        dialog.assert_not_called()
        self.assertIn(self.did, self.window._pending_retries)
        self.assertEqual(self.window._pending_retries[self.did].interval(), 10000)

    def test_real_workers_fail_twice_then_succeed_with_same_configuration(self):
        attempt = Path(self.temp.name) / "attempt.txt"
        code = (
            "import os; from pathlib import Path; "
            f"p=Path({str(attempt)!r}); n=int(p.read_text())+1 if p.exists() else 1; "
            "p.write_text(str(n)); print('DEADLINE='+os.environ.get('OTOOL_AUTO_DETECT_TIMEOUT_SECONDS', 'missing')); "
            "raise SystemExit(1 if n<3 else 0)"
        )
        with patch("src.main_window.AUTO_RETRY_DELAY_MS", 30):
            self.window._start_process(self.card, [sys.executable, "-c", code], "烧录", "flash", automatic_flash=True)
            for _ in range(250):
                self.app.processEvents()
                if self.card.property("cardState") == "success":
                    break
                QTest.qWait(20)
        self.assertEqual(attempt.read_text(), "3")
        self.assertEqual(self.card.property("cardState"), "success")
        self.assertEqual(self.card.log_edit.toPlainText().count("DEADLINE=3"), 3)
        self.assertFalse(self.window._pending_retries)
        self.assertTrue(self.window._port_registry.is_available("COM7"))

    def test_automatic_entrypoint_marks_worker_and_failed_start_is_retried(self):
        firmware = Path(self.temp.name) / "firmware.bin"
        firmware.write_bytes(b"firmware")
        self.window._flash_rows[0].set_firmware_path(str(firmware))
        with patch("src.main_window._build_tool_worker_command", return_value=["__missing_otool_retry_test__"]):
            self.window._auto_flash_new_device(self.did)
        for _ in range(100):
            self.app.processEvents()
            if self.did in self.window._pending_retries:
                break
            QTest.qWait(20)
        self.assertTrue(self.card._last_command_meta["automatic_flash"])
        self.assertIn(self.did, self.window._pending_retries)

    def test_imported_chip_lock_survives_ordinary_connection_retry(self):
        self.finish_attempt()
        self.card._last_command_meta["expected_chip"] = "esp32s3"
        self.window._cancel_retry(self.did)
        self.assertTrue(self.window._try_auto_retry(self.card, self.did))
        with patch.object(self.window, "_start_process") as start:
            self.window._pending_retries[self.did].timeout.emit()
        self.assertEqual(start.call_args.kwargs["expected_chip"], "esp32s3")


class DetectionDeadlineTests(unittest.TestCase):
    def run_worker(self, connect, after="", timeout=0.2):
        code = f'''
import time
from types import SimpleNamespace
from src.auto_flash import detection_deadline
def connect():
    {connect}
module = SimpleNamespace(connect_esp=connect)
with detection_deadline(module, {timeout}, lambda e: print(e, flush=True)):
    module.connect_esp()
    {after or "pass"}
print("DONE", flush=True)
'''
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                              capture_output=True, encoding="utf-8", timeout=8,
                              env={**os.environ, "PYTHONUTF8": "1"})

    def test_stuck_detection_exits_before_any_write(self):
        result = self.run_worker("time.sleep(5)", "print('WRITE', flush=True)")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("设备检测超时", result.stdout)
        self.assertNotIn("WRITE", result.stdout)

    def test_write_after_success_can_exceed_detection_deadline(self):
        result = self.run_worker("return object()", "time.sleep(0.4)")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DONE", result.stdout)

    def test_actual_worker_dispatch_bounds_only_automatic_detection_at_three_seconds(self):
        # Exercise the real worker dispatch and real esptool module; replace only
        # its hardware connection/action, with no serial handles opened.
        code = '''
import sys, time
from src import bootstrap
import esptool
def connect(*args, **kwargs):
    print('CONNECT_START', time.monotonic(), flush=True)
    time.sleep(6)
def main(args):
    esptool.connect_esp()
    print('WRITE', flush=True)
esptool.connect_esp = connect
esptool.main = main
sys.argv = ['worker', '__otool_worker__', 'esptool', 'write-flash']
bootstrap.dispatch_internal_worker()
'''
        import time
        process = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8",
                                   env={**os.environ, "PYTHONUTF8": "1", "OTOOL_AUTO_DETECT_TIMEOUT_SECONDS": "3"})
        stdout, stderr = process.communicate(timeout=12)
        finished = time.monotonic()
        start = float(next(line.split()[1] for line in stdout.splitlines() if line.startswith("CONNECT_START")))
        self.assertEqual(process.returncode, 1, stdout + stderr)
        self.assertGreaterEqual(finished - start, 2.95)
        self.assertLess(finished - start, 5)
        self.assertNotIn("WRITE", stdout)
        self.assertIn("设备检测超时（3 秒）", stdout)


if __name__ == "__main__":
    unittest.main()
