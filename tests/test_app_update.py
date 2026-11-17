from __future__ import annotations

from hashlib import sha256
import io
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QAbstractAnimation
from PyQt6.QtWidgets import QApplication

from src.app_update import EXECUTABLE_NAME, RELEASE_ROOT, UpdateError, download_release, parse_release
from src.device_policy import DevicePolicyStore
from src.main_window import OtoolEsptoolUI
from src.update_dialog import ReleaseUpdateDialog


PACKAGE = b"a test executable"


def release_payload(tag: str = "v0.6.100") -> dict:
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "body": "## 更新\n\n- 修复设备列表",
        "assets": [{
            "name": EXECUTABLE_NAME,
            "browser_download_url": f"{RELEASE_ROOT}/download/{tag}/{EXECUTABLE_NAME}",
            "state": "uploaded",
            "digest": f"sha256:{sha256(PACKAGE).hexdigest()}",
            "size": len(PACKAGE),
        }],
    }


class ReleaseDownloadTests(unittest.TestCase):
    def test_stable_versions_are_compared_numerically(self):
        self.assertEqual(parse_release(release_payload(), "0.6.99").version, "0.6.100")
        for tag in ("v0.6.9", "v0.6.99", "v0.7.0-rc.1", "v0.7.0/other"):
            with self.subTest(tag=tag):
                self.assertIsNone(parse_release(release_payload(tag), "0.6.99"))
        for field in ("draft", "prerelease"):
            payload = release_payload()
            payload[field] = True
            self.assertIsNone(parse_release(payload, "0.6.99"))

    def test_unsafe_or_incomplete_assets_still_allow_reading_notes(self):
        for overrides in ({"digest": None}, {"browser_download_url": "https://other.example/binary.exe"}):
            payload = release_payload()
            payload["assets"][0].update(overrides)
            release = parse_release(payload, "0.6.23")
            self.assertIn("修复设备列表", release.notes)
            self.assertIsNone(release.asset)

    def test_download_checks_digest_size_and_cleans_failed_downloads(self):
        asset = parse_release(release_payload(), "0.6.23").asset
        for data, cancelled, expected in (
            (PACKAGE, False, True), (b"0" * len(PACKAGE), False, False),
            (PACKAGE[:-1], False, False), (PACKAGE + b"1", False, False), (PACKAGE, True, False),
        ):
            with self.subTest(data=data, cancelled=cancelled), tempfile.TemporaryDirectory() as folder:
                stage = Path(folder) / "staging"
                stage.mkdir()
                cancel = Event()
                if cancelled:
                    cancel.set()
                with patch("src.app_update.tempfile.mkdtemp", return_value=str(stage)), patch(
                    "src.app_update._github_request", return_value=io.BytesIO(data),
                ):
                    progress = Mock()
                    if expected:
                        self.assertEqual(download_release(asset, cancel, progress), stage)
                        self.assertEqual((stage / EXECUTABLE_NAME).read_bytes(), PACKAGE)
                        progress.assert_called_with(100)
                    else:
                        with self.assertRaises(UpdateError):
                            download_release(asset, cancel, progress)
                        self.assertFalse(stage.exists())


class ReleaseWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_button_reveal_keeps_badges_in_place_and_respects_reduced_motion(self):
        with patch("serial.tools.list_ports.comports", return_value=[]), patch(
            "src.main_window._fetch_remote_avatar_bytes"
        ), patch("src.update_dialog._animations_enabled", return_value=False):
            window = OtoolEsptoolUI(DevicePolicyStore())
            try:
                window.resize(790, 530)
                window.show()
                self.app.processEvents()
                original_version = window.version_badge.mapTo(window, window.version_badge.rect().topLeft())
                original_footer_height = window._footer.height()
                button = window.release_update_button
                self.assertTrue(button.isHidden())
                window._show_release_update(parse_release(release_payload(), "0.6.23"))
                self.app.processEvents()
                self.assertTrue(button.isVisible())
                self.assertEqual(button.parentWidget(), window.floating_info_panel)
                self.assertLess(button.geometry().right(), window.version_badge.x())
                self.assertEqual(window._footer.height(), original_footer_height)
                self.assertEqual(window.version_badge.mapTo(window, window.version_badge.rect().topLeft()), original_version)
                self.assertEqual(button._animation.state(), QAbstractAnimation.State.Stopped)
            finally:
                window.close()
                window.deleteLater()
                self.app.processEvents()

    def test_irreversible_work_does_not_allow_replacement(self):
        with patch("serial.tools.list_ports.comports", return_value=[]), patch(
            "src.main_window._fetch_remote_avatar_bytes"
        ):
            window = OtoolEsptoolUI(DevicePolicyStore())
            update = Mock()
            try:
                with patch.object(window._efuse_batch_widget, "has_irreversible_operation", return_value=True):
                    window.show()
                    self.assertIn("eFuse", window._close_for_release_update(update))
                    self.assertTrue(window.isVisible())
                    update.commit.assert_not_called()
            finally:
                window.close()
                window.deleteLater()
                self.app.processEvents()

    def test_missing_checksum_disables_the_install_action(self):
        payload = release_payload()
        payload["assets"][0].pop("digest")
        with patch("src.update_dialog.can_install_release", return_value=True):
            dialog = ReleaseUpdateDialog(parse_release(payload, "0.6.23"), Mock(), Mock(), [], None)
        self.assertFalse(dialog._start.isEnabled())
        self.assertTrue(dialog._github.isEnabled())
        self.assertIn("可校验", dialog._status.text())
        dialog.close()
