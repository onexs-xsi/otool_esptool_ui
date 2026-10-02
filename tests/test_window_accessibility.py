from __future__ import annotations

import os
import unittest
import threading
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QStyleOptionFrame, QStyle
from src.main_window import OtoolEsptoolUI, ReferenceNoticeDialog
from src.verify_visual_editor import _DownComboBox
from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
from PyQt6.QtGui import QImage, QColor
from PyQt6.QtTest import QTest


class SmallWindowAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_stylesheet_assets_exist_outside_the_source_directory(self):
        with tempfile.TemporaryDirectory(prefix="独立 目录 ") as directory, patch(
            "src.main_window.TOOL_DIR", Path(directory)
        ), patch("serial.tools.list_ports.comports", return_value=[]):
            window = OtoolEsptoolUI()
            try:
                resources = re.findall(r"url\(([^)]+)\)", window.styleSheet())
                self.assertTrue(resources)
                for resource in resources:
                    self.assertTrue(Path(resource.strip("\"'")).is_file(), resource)
            finally:
                window.close()
                window.deleteLater()
                self.app.processEvents()

    def test_verify_uses_bundled_config_when_external_file_is_absent(self):
        self._check_verify_config_source(external=False)

    def test_verify_keeps_external_config_precedence(self):
        self._check_verify_config_source(external=True)

    def _check_verify_config_source(self, external):
        from src.verify_widget import VerifyWidget
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            executable = root / "exe"
            bundle.mkdir()
            executable.mkdir()
            for folder, name in [(bundle, "内置配置"), (executable, "外部配置")]:
                if folder == executable and not external:
                    continue
                (folder / "config.yaml").write_text(
                    f"verify_profiles:\n  {name}:\n    steps:\n      - action: pass\n", encoding="utf-8"
                )
            with patch("src.verify_widget.TOOL_DIR", executable), patch(
                "src.verify_widget._resource_root", return_value=bundle
            ), patch("serial.tools.list_ports.comports", return_value=[]):
                widget = VerifyWidget()
                try:
                    self.assertEqual(list(widget._profiles), ["外部配置" if external else "内置配置"])
                finally:
                    widget.close()
                    widget.deleteLater()
                    self.app.processEvents()

    def test_all_workbenches_fit_small_desktop_with_reachable_content(self):
        with patch("serial.tools.list_ports.comports", return_value=[]):
            window = OtoolEsptoolUI()
            try:
                window.show()
                for tab in range(5):
                    with self.subTest(tab=tab):
                        window.tab_switcher.set_current(tab, animated=False)
                        window.resize(886, 452)
                        self.app.processEvents()
                        self.assertLessEqual(window.width(), 886)
                        self.assertLessEqual(window.height(), 452)
                        self.assertFalse(window.floating_info_panel.geometry().intersects(window.floating_tab_panel.geometry()))
                        viewport = window._content_scroll.viewport()
                        content = window._content_scroll.widget()
                        self.assertGreater(content.width(), viewport.width())
                        self.assertGreater(window._content_scroll.horizontalScrollBar().maximum(), 0)
                        self.assertTrue(window.floating_tab_panel.isVisible())
            finally:
                window.close()
                window.deleteLater()
                self.app.processEvents()

    def test_serial_encoding_text_fits_at_minimum_editor_width(self):
        with patch("serial.tools.list_ports.comports", return_value=[]):
            window = OtoolEsptoolUI()
            try:
                window.tab_switcher.set_current(2, animated=False)
                window.resize(1256, 660)
                window.show()
                self.app.processEvents()

                edit = window._verify_widget._visual_editor._encoding_edit
                opt = QStyleOptionFrame()
                edit.initStyleOption(opt)
                content = edit.style().subElementRect(QStyle.SubElement.SE_LineEditContents, opt, edit)
                self.assertGreaterEqual(content.width() - 4, edit.fontMetrics().horizontalAdvance("utf-8"))
            finally:
                window.close()
                window.deleteLater()
                self.app.processEvents()

    def test_reference_notice_can_shrink_to_small_desktop(self):
        dialog = ReferenceNoticeDialog()
        try:
            dialog.resize(886, 452)
            dialog.show()
            self.app.processEvents()
            self.assertLessEqual(dialog.width(), 886)
            self.assertLessEqual(dialog.height(), 452)
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_downloaded_avatar_is_applied_from_worker_thread(self):
        callbacks = []
        with patch("serial.tools.list_ports.comports", return_value=[]), patch(
            "src.main_window._fetch_remote_avatar_bytes", side_effect=callbacks.append
        ):
            window = OtoolEsptoolUI()
            try:
                previous = window.github_button.icon().cacheKey()
                picture = QImage(20, 20, QImage.Format.Format_RGB32)
                picture.fill(QColor("#456789"))
                data = QByteArray()
                buffer = QBuffer(data)
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                picture.save(buffer, "PNG")
                thread = threading.Thread(target=callbacks[0], args=(bytes(data),))
                thread.start()
                thread.join()
                QTest.qWait(30)
                self.assertNotEqual(previous, window.github_button.icon().cacheKey())
            finally:
                window.close()
                window.deleteLater()
                self.app.processEvents()

    def test_dropdown_near_screen_bottom_stays_on_screen(self):
        combo = _DownComboBox()
        combo.addItems([str(n) for n in range(15)])
        available = self.app.primaryScreen().availableGeometry()
        combo.move(available.left() + 10, available.bottom() - 32)
        combo.resize(120, 28)
        try:
            combo.show()
            self.app.processEvents()
            combo.showPopup()
            self.app.processEvents()
            self.assertTrue(available.contains(combo.view().parentWidget().geometry()))
        finally:
            combo.hidePopup()
            combo.close()
            combo.deleteLater()
            self.app.processEvents()


    def test_efuse_dialog_remains_scrollable_on_small_desktop(self):
        from src.efuse_dialog import EFuseDialog
        from src.models import DeviceInfo
        dialog = EFuseDialog(DeviceInfo("mock", "COM0", "测试"), "115200")
        try:
            dialog.resize(886, 452)
            dialog.show()
            self.app.processEvents()
            self.assertLessEqual(dialog.height(), 452)
            self.assertGreater(dialog._content_scroll.verticalScrollBar().maximum(), 0)
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
