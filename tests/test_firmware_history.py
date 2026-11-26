from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QPushButton, QToolButton

from src.firmware_history import FirmwareHistoryStore, FirmwareSource, shorten_source_path, HISTORY_LABEL_MAX_WIDTH
from src.main_window import FlashEntryRow, OtoolEsptoolUI
from src.device_policy import DevicePolicyStore


class FirmwareHistoryTests(unittest.TestCase):
    def test_persistent_mru_is_shared_by_bins_and_builds_limited_to_sixteen(self):
        with tempfile.TemporaryDirectory(prefix="otool-history-") as directory:
            root = Path(directory)
            history = FirmwareHistoryStore(root / "history.json")
            for i in range(20):
                path = root / f"firmware-{i}.bin"
                path.write_bytes(b"firmware")
                history.remember("bin", path)
            build = root / "project" / "build"
            build.mkdir(parents=True)
            (build / "flasher_args.json").write_text("{}")
            history.remember("build", build)
            history.remember("bin", root / "firmware-10.bin")
            self.assertEqual(len(history.entries), 16)
            self.assertEqual(history.entries[0].path, str((root / "firmware-10.bin").resolve()))
            self.assertEqual(history.entries[1].kind, "build")
            self.assertEqual(len({(item.kind, item.path) for item in history.entries}), 16)
            self.assertEqual(FirmwareHistoryStore(history.path).entries, history.entries)

    def test_save_error_does_not_reorder_or_leave_temporary_files(self):
        with tempfile.TemporaryDirectory(prefix="otool-history-") as directory:
            root = Path(directory)
            firmware = root / "firmware.bin"
            firmware.write_bytes(b"firmware")
            history = FirmwareHistoryStore(root / "history.json")
            with patch("src.firmware_history.os.replace", side_effect=PermissionError("read only")):
                with self.assertRaises(OSError):
                    history.remember("bin", firmware)
            self.assertFalse(history.entries)
            self.assertEqual(list(root.glob("firmware_history-*.tmp")), [])

    def test_long_path_retains_tail_and_uses_six_stars(self):
        for kind, path in (("bin", Path("D:/") / ("project-" * 15) / "output" / "firmware_0x10000.bin"),
                           ("build", Path("D:/") / ("project-" * 15) / "esp32s3_project" / "build")):
            text = shorten_source_path(FirmwareSource(kind, str(path)))
            self.assertLessEqual(len(text), 56)
            self.assertIn("******", text)
            self.assertTrue(text.endswith(path.name))


class FirmwareHistoryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_menu_separator_types_full_tooltips_missing_path_and_restore(self):
        with tempfile.TemporaryDirectory(prefix="otool-history-") as directory:
            root = Path(directory)
            firmware = root / "firmware_0x10000.bin"
            firmware.write_bytes(b"firmware")
            build = root / "project" / "build"
            build.mkdir(parents=True)
            (build / "flasher_args.json").write_text("{}")
            history = FirmwareHistoryStore(root / "history.json")
            history.remember("build", build)
            history.remember("bin", firmware)
            row = FlashEntryRow(history=history)
            try:
                actions = row.browse_menu.actions()
                self.assertTrue(actions[2].isSeparator())
                self.assertTrue(actions[3].text().startswith("bin · "))
                self.assertTrue(actions[4].text().startswith("build · "))
                self.assertEqual(actions[3].toolTip(), str(firmware))
                actions[3].trigger()
                self.assertEqual(row.path_edit.text(), str(firmware))
                self.assertEqual(row.addr_edit.text(), "0x10000")
                received = []
                row.build_import_requested.connect(received.append)
                actions[4].trigger()
                self.assertEqual(received, [str(build)])
                firmware.unlink()
                row._populate_browse_menu()
                missing = row.browse_menu.actions()[3].defaultWidget()
                self.assertFalse(missing.findChild(QPushButton).isEnabled())
                self.assertTrue(missing.findChild(QToolButton).isEnabled())
                self.assertIn("路径已不存在", row.browse_menu.actions()[3].toolTip())
            finally:
                row.deleteLater()

    def test_recent_source_buttons_forget_records_without_selecting_or_deleting_files(self):
        with tempfile.TemporaryDirectory(prefix="otool-history-") as directory:
            root = Path(directory)
            firmware = root / "firmware.bin"
            firmware.write_bytes(b"firmware")
            build = root / "build"
            build.mkdir()
            manifest = build / "flasher_args.json"
            manifest.write_text("{}")
            history = FirmwareHistoryStore(root / "history.json")
            history.remember("bin", firmware)
            history.remember("build", build)
            manifest.unlink()
            row = FlashEntryRow("0x1000", str(firmware), history=history)
            menu = row.browse_menu
            activations = []
            row.firmware_used.connect(activations.append)
            row.build_import_requested.connect(activations.append)
            try:
                menu.popup(QPoint(5, 5))
                self.app.processEvents()
                missing = menu.actions()[3].defaultWidget()
                remove = missing.findChild(QToolButton)
                self.assertLess(remove.geometry().right(), missing.findChild(QPushButton).geometry().left())
                with (patch("src.firmware_history.os.replace", side_effect=OSError("read only")),
                      patch("src.main_window.QMessageBox.warning") as warning):
                    QTest.mouseClick(remove, Qt.MouseButton.LeftButton)
                    warning.assert_called_once()
                self.assertEqual(len(FirmwareHistoryStore(history.path).entries), 2)
                self.assertEqual(len(history.entries), 2)
                QTest.mouseClick(remove, Qt.MouseButton.LeftButton)
                self.assertTrue(menu.isVisible())
                self.assertEqual(history.entries, [FirmwareSource("bin", str(firmware))])
                menu.setActiveAction(menu.actions()[3])
                QTest.keyClick(menu, Qt.Key.Key_Delete)
                self.assertEqual(menu.actions()[-1].text(), "暂无使用记录")
                self.assertFalse(FirmwareHistoryStore(history.path).entries)
                self.assertTrue(build.is_dir())
                self.assertEqual(firmware.read_bytes(), b"firmware")
                self.assertEqual((row.addr_edit.text(), row.path_edit.text()), ("0x1000", str(firmware)))
                self.assertEqual(activations, [])
            finally:
                menu.close()
                row.deleteLater()

    def test_build_history_reuses_import_and_bin_selection_updates_mru_across_rows(self):
        with (tempfile.TemporaryDirectory(prefix="otool-history-") as directory,
              patch("serial.tools.list_ports.comports", return_value=[]),
              patch("src.main_window._fetch_remote_avatar_bytes")):
            root = Path(directory)
            firmware = root / "firmware_0x10000.bin"
            firmware.write_bytes(b"firmware")
            build = root / "project" / "build"
            build.mkdir(parents=True)
            (build / "app.bin").write_bytes(b"app")
            (build / "flasher_args.json").write_text(json.dumps({"flash_files": {"0x10000": "app.bin"},
                                                               "extra_esptool_args": {"chip": "esp32s3"}}))
            window = OtoolEsptoolUI(DevicePolicyStore(root / "policy.json"))
            self.app.processEvents()
            try:
                self.assertTrue(window._import_idf_build(str(build)))
                self.assertEqual(window._firmware_history.entries[0].kind, "build")
                row = window._flash_rows[0]
                with patch("src.main_window.get_open_file_name", return_value=(str(firmware), "")):
                    row._browse()
                self.assertEqual(window._firmware_history.entries[0].kind, "bin")
                second = window._add_flash_entry()
                second._populate_browse_menu()
                second.browse_menu.actions()[4].trigger()
                self.assertEqual(window._build_flash_config.directory, build.resolve())
                self.assertEqual(window._firmware_history.entries[0].kind, "build")
                self.assertEqual(FirmwareHistoryStore(root / "firmware_history.json").entries,
                                 window._firmware_history.entries)
            finally:
                window.close()
                window.deleteLater()
                self.app.processEvents()

    def test_history_menu_label_width_is_bounded(self):
        history = FirmwareHistoryStore()
        history.entries = [FirmwareSource("bin", "D:/" + "very-long-project/" * 15 + "version_0x10000.bin")]
        row = FlashEntryRow(history=history)
        try:
            text = row.browse_menu.actions()[3].text()
            self.assertLessEqual(row.browse_menu.fontMetrics().horizontalAdvance(text), HISTORY_LABEL_MAX_WIDTH)
            self.assertIn("******", text)
            self.assertTrue(text.endswith("0x10000.bin"))
        finally:
            row.deleteLater()


if __name__ == "__main__":
    unittest.main()
