from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from src.main_window import OtoolEsptoolUI
from src.device_policy import DevicePolicyStore


class IdfBuildUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="烧录 项目 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.build = self.root / "_build-output"
        self.build.mkdir()
        for filename in ("bootloader.bin", "partitions.bin", "app.bin"):
            (self.build / filename).write_bytes(b"firmware")
        self.data = {
            "flash_files": {"0x20000": "app.bin", "0x0": "bootloader.bin", "0x9000": "partitions.bin"},
            "flash_settings": {"flash_mode": "dio", "flash_size": "16MB", "flash_freq": "80m"},
            "extra_esptool_args": {
                "chip": "esp32s3", "before": "usb_reset", "after": "no_reset", "stub": False,
            },
        }
        self.write_manifest()
        devices = [SimpleNamespace(device=name, serial_number=name, location=name,
                                   vid=0x10C4, pid=0xEA60, hwid="USB", description="USB Serial")
                   for name in ("COM7", "COM8")]
        self.ports = patch("serial.tools.list_ports.comports", return_value=devices)
        self.ports.start()
        self.addCleanup(self.ports.stop)
        self.window = OtoolEsptoolUI(DevicePolicyStore(self.root / "policy.json"))
        self.app.processEvents()
        self.did, self.card = next(iter(self.window.device_cards.items()))
        self.old_file = self.root / "old_0x40000.bin"
        self.old_file.write_bytes(b"old")
        row = self.window._flash_rows[0]
        row.path_edit.setText(str(self.old_file))
        row.addr_edit.setText("0x40000")
        row.path_edit.editingFinished.emit()

    def tearDown(self):
        self.window.stop_all_devices()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def write_manifest(self):
        (self.build / "flasher_args.json").write_text(json.dumps(self.data), encoding="utf-8")

    def rows(self):
        return [(row.addr_edit.text(), row.path_edit.text()) for row in self.window._flash_rows]

    def test_build_menu_import_replaces_entire_list_with_editable_rows(self):
        self.window._add_flash_entry("0x50000", str(self.old_file))
        row = self.window._flash_rows[0]
        actions = row.browse_btn.menu().actions()
        self.assertEqual([action.text() for action in actions[:2]], ["选择 bin 文件", "导入 build 目录"])
        self.assertTrue(actions[2].isSeparator())
        with patch("src.main_window.get_existing_directory", return_value=str(self.build)):
            actions[1].trigger()
        self.assertEqual(self.rows(), [
            ("0x0", str(self.build / "bootloader.bin")),
            ("0x9000", str(self.build / "partitions.bin")),
            ("0x20000", str(self.build / "app.bin")),
        ])
        self.assertTrue(all(row.addr_edit.isEnabled() and not row.path_edit.isReadOnly()
                            for row in self.window._flash_rows))
        self.assertIn("esp32s3", self.window._build_params_label.text())
        self.assertIn("80m", self.window._build_params_label.text())
        self.assertFalse(self.window._build_params_group.isHidden())

    def test_cancelled_folder_dialog_leaves_entries_and_parameters(self):
        self.window._import_idf_build(str(self.build))
        before = self.rows()
        config = self.window._build_flash_config
        with patch("src.main_window.get_existing_directory", return_value=""):
            self.window._flash_rows[0].browse_btn.menu().actions()[1].trigger()
        self.assertEqual(self.rows(), before)
        self.assertIs(self.window._build_flash_config, config)

    def test_pasted_directory_imports_and_failed_paste_restores_previous_path(self):
        row = self.window._flash_rows[0]
        row.path_edit.setText(str(self.build))
        row.path_edit.editingFinished.emit()
        self.assertEqual(len(self.window._flash_rows), 3)
        before = self.rows()
        config = self.window._build_flash_config
        row = self.window._flash_rows[0]
        invalid = self.root / "empty-build"
        invalid.mkdir()
        with patch("src.main_window.QMessageBox.warning"):
            row.path_edit.setText(str(invalid))
            row.path_edit.editingFinished.emit()
        self.assertEqual(self.rows(), before)
        self.assertIs(self.window._build_flash_config, config)

    def test_failed_import_is_atomic_and_reimport_does_not_duplicate_rows(self):
        self.window._import_idf_build(str(self.build))
        before = self.rows()
        config = self.window._build_flash_config
        self.data["flash_settings"]["flash_mode"] = "invalid"
        self.write_manifest()
        with patch("src.main_window.QMessageBox.warning"):
            self.assertFalse(self.window._import_idf_build(str(self.build)))
        self.assertEqual(self.rows(), before)
        self.assertIs(self.window._build_flash_config, config)
        self.data["flash_settings"]["flash_mode"] = "qio"
        self.data["flash_files"]["0x40000"] = self.data["flash_files"].pop("0x20000")
        self.write_manifest()
        self.assertTrue(self.window._import_idf_build(str(self.build)))
        self.assertEqual(len(self.window._flash_rows), 3)
        self.assertEqual(self.rows()[-1][0], "0x40000")
        self.assertIn("QIO", self.window._build_params_label.text())

    def test_edited_rows_and_interface_baud_are_used_by_every_flash_entrypoint(self):
        self.window._import_idf_build(str(self.build))
        app_row = self.window._flash_rows[-1]
        app_row.addr_edit.setText("0x40000")
        app_row.path_edit.setText(str(self.old_file))
        self.window.baud_edit.setCurrentText("460800")
        with patch.object(self.window, "_start_process") as start:
            self.window.auto_flash_button.setChecked(True)
            self.window._auto_flash_new_device(self.did)
            self.window.flash_firmware(self.did)
            self.window.flash_all_devices()
        self.assertEqual(start.call_count, 4)
        for call in start.call_args_list:
            args = call.args[1]
            self.assertIn("--chip", args)
            self.assertEqual(args[args.index("--chip") + 1], "esp32s3")
            self.assertEqual(args[args.index("--baud") + 1], "460800")
            self.assertEqual(args[args.index("--before") + 1], "usb-reset")
            self.assertEqual(args[args.index("--after") + 1], "no-reset")
            self.assertIn("--no-stub", args)
            write = args[args.index("write-flash") + 1:]
            self.assertEqual(write, [
                "--flash-mode", "dio", "--flash-size", "16MB", "--flash-freq", "80m",
                "0x0", str(self.build / "bootloader.bin"),
                "0x9000", str(self.build / "partitions.bin"),
                "0x40000", str(self.old_file),
            ])
            self.assertEqual(call.kwargs["expected_chip"], "esp32s3")

    def test_invalid_edited_entries_cannot_open_worker_on_any_entrypoint(self):
        self.window._import_idf_build(str(self.build))
        (self.build / "app.bin").unlink()
        with patch("src.main_window.QMessageBox.warning"), patch.object(self.window, "_start_process") as start:
            self.window.flash_firmware(self.did)
            self.window.flash_all_devices()
            self.window.auto_flash_button.setChecked(True)
            self.window._auto_flash_new_device(self.did)
        start.assert_not_called()

    def test_known_chip_mismatch_is_rejected_and_project_lock_survives_reset(self):
        self.window._import_idf_build(str(self.build))
        self.window.device_infos[self.did].chip_name = "ESP32-C6"
        with patch("src.main_window.QMessageBox.warning"), patch.object(self.window, "_start_process") as start:
            self.window.flash_firmware(self.did)
        start.assert_not_called()
        # The in-flight task keeps its lock even if the UI is reset or reimported.
        self.card._last_command = ["worker", "--chip", "esp32s3", "write-flash"]
        self.card._last_command_meta = {"expected_chip": "esp32s3"}
        self.card._operation_output = "This chip is ESP32-C6, not ESP32-S3."
        self.window._reset_build_params()
        self.assertFalse(self.window._try_auto_retry(self.card, self.did))
        self.assertFalse(self.window._pending_retries)

    def test_started_task_snapshots_chip_lock_before_the_ui_is_reset(self):
        self.window._import_idf_build(str(self.build))
        command = [sys.executable, "-c",
                   "print('This chip is ESP32-C6, not ESP32-S3.'); raise SystemExit(1)",
                   "--chip", "esp32s3"]
        self.window._start_process(self.card, command, "测试", "flash", expected_chip="esp32s3")
        self.window._reset_build_params()
        for _ in range(100):
            self.app.processEvents()
            if self.card.process is None:
                break
            QTest.qWait(20)
        self.assertIsNone(self.card.process)
        self.assertFalse(self.window._pending_retries)
        self.assertEqual(self.card._auto_retry_count, 0)
        self.assertEqual(self.card._last_command_meta["expected_chip"], "esp32s3")
        self.assertEqual(self.card.property("cardState"), "error")

    def test_parameter_reset_keeps_rows_and_restores_manual_flashing_defaults(self):
        self.window._import_idf_build(str(self.build))
        before = self.rows()
        self.window._reset_build_params_btn.click()
        self.assertEqual(self.rows(), before)
        self.assertIsNone(self.window._build_flash_config)
        self.assertTrue(self.window._build_params_group.isHidden())
        with patch.object(self.window, "_start_process") as start:
            self.window.flash_firmware(self.did)
        args = start.call_args.args[1]
        self.assertEqual(args[args.index("--chip") + 1], "auto")
        self.assertNotIn("--flash-mode", args)
        self.assertNotIn("--no-stub", args)

    def test_bin_selection_still_extracts_filename_address_and_cancellation_preserves_it(self):
        row = self.window._flash_rows[0]
        with patch("src.main_window.get_open_file_name", return_value=(str(self.old_file), "")):
            row._browse()
        self.assertEqual(row.addr_edit.text(), "0x40000")
        with patch("src.main_window.get_open_file_name", return_value=("", "")):
            row._browse()
        self.assertEqual(row.path_edit.text(), str(self.old_file))

    def test_non_flash_operations_do_not_inherit_build_parameters(self):
        self.window._import_idf_build(str(self.build))
        with patch.object(self.window, "_start_process") as start:
            self.window.erase_flash(self.did)
        args = start.call_args.args[1]
        self.assertEqual(args[args.index("--chip") + 1], "auto")
        self.assertNotIn("--before", args)
        self.assertNotIn("--no-stub", args)


if __name__ == "__main__":
    unittest.main()
