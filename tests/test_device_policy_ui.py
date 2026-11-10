from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from src.device_card import DeviceTitleLabel, device_display_name
from src.device_policy import DeviceIdentity, DevicePolicyStore
from src.main_window import OtoolEsptoolUI
from src.port_ownership import PortOwnershipRegistry


def port(name="COM7", serial="DEVICE-A", label="USB-Enhanced-SERIAL CH343"):
    return SimpleNamespace(device=name, serial_number=serial, vid=0x1A86, pid=0x55D4,
                           location="1-2", hwid="USB VID:PID=1A86:55D4", description=f"{label} ({name})")


class DevicePolicyTests(unittest.TestCase):
    def test_device_rule_survives_restart_and_com_change_but_not_adapter_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device_policies.json"
            policy = DevicePolicyStore(path)
            policy.set_blocked(DeviceIdentity.from_device(port()), "device", True)
            restored = DevicePolicyStore(path)
            self.assertTrue(restored.is_blocked(DeviceIdentity.from_device(port("COM99"))))
            self.assertFalse(restored.is_blocked(DeviceIdentity.from_device(port(serial="OTHER"))))
            restored.set_blocked(DeviceIdentity.from_device(port("COM99")), "device", False)
            self.assertFalse(DevicePolicyStore(path).is_blocked(DeviceIdentity.from_device(port())))

    def test_port_rule_matches_any_adapter_on_that_port_only(self):
        policy = DevicePolicyStore()
        policy.set_blocked(DeviceIdentity.from_device(port()), "port", True)
        self.assertTrue(policy.is_blocked(DeviceIdentity.from_device(port("com7", "OTHER"))))
        self.assertFalse(policy.is_blocked(DeviceIdentity.from_device(port("COM8"))))

    def test_generic_adapter_fallback_does_not_block_every_adapter_of_same_model(self):
        first, second = port(serial=""), port("COM8", "")
        first.location = second.location = ""
        identity = DeviceIdentity.from_device(first)
        self.assertFalse(identity.portable)
        policy = DevicePolicyStore()
        policy.set_blocked(identity, "device", True)
        self.assertFalse(policy.is_blocked(DeviceIdentity.from_device(second)))

    def test_failed_save_preserves_rules_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device_policies.json"
            policy = DevicePolicyStore(path)
            with patch("src.device_policy.os.replace", side_effect=PermissionError("read only")):
                with self.assertRaises(OSError):
                    policy.set_blocked(DeviceIdentity.from_device(port()), "port", True)
            self.assertFalse(policy.blocked_ports)
            self.assertFalse(list(Path(directory).iterdir()))

    def test_unreadable_rules_fail_closed_and_registry_enforces_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device_policies.json"
            path.write_text("broken", encoding="utf-8")
            registry = PortOwnershipRegistry(DevicePolicyStore(path))
            self.assertTrue(registry.is_blocked(port()))
            self.assertFalse(registry.is_available("COM7"))
            self.assertIsNone(registry.acquire("COM7", "flash", "test"))


class DevicePolicyUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.policy = DevicePolicyStore(self.root / "device_policies.json")
        self.ports_patch = patch("serial.tools.list_ports.comports", return_value=[port(), port("COM8", "DEVICE-B")])
        self.ports = self.ports_patch.start()
        self.addCleanup(self.ports_patch.stop)
        # No test in this class is allowed to start a hardware worker.
        self.start_patch = patch.object(OtoolEsptoolUI, "_start_process")
        self.start = self.start_patch.start()
        self.addCleanup(self.start_patch.stop)
        self.window = OtoolEsptoolUI(self.policy)
        self.app.processEvents()
        self.did = next(did for did, card in self.window.device_cards.items() if card.device.port == "COM7")
        self.card = self.window.device_cards[self.did]
        firmware = self.root / "firmware.bin"
        firmware.write_bytes(b"test firmware")
        self.window._flash_rows[0].set_firmware_path(str(firmware))

    def tearDown(self):
        self.window.refresh_timer.stop()
        self.window.stop_all_devices()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def wait_for_starts(self, count: int) -> None:
        for _ in range(50):
            if self.start.call_count >= count:
                break
            QTest.qWait(20)
        self.assertEqual(self.start.call_count, count, {
            "pending": list(self.window._pending_auto_flash),
            "stopped": self.card._stop_requested,
            "blocked": self.card.blocked,
            "connected": self.card.device.connected,
            "armed": self.window._port_auto_flash,
        })

    def test_title_full_name_tooltip_no_com_suffix_and_tail_preserved_at_multiple_widths(self):
        self.assertEqual(self.card.title_label.toolTip(), "USB-Enhanced-SERIAL CH343")
        self.assertEqual(self.card.port_badge.text(), "COM7")
        label = DeviceTitleLabel()
        self.addCleanup(label.deleteLater)
        text = "Silicon Labs CP210x USB to UART Bridge"
        label.setText(text)
        for width in (180, 240):
            label.resize(width, 24)
            label._refresh_elided()
            self.assertIn("******", label.text())
            self.assertTrue(label.text().endswith("Bridge"), label.text())
            self.assertLessEqual(label.fontMetrics().horizontalAdvance(label.text()), width)
            self.assertEqual(label.toolTip(), text)
        label.resize(900, 24)
        label._refresh_elided()
        self.assertEqual(label.text(), text)
        label.resize(240, 24)
        label.setText("Espressif USB Serial Debug Adapter ESP32-S3")
        self.assertTrue(label.text().endswith("ESP32-S3"))
        self.assertEqual(device_display_name("USB (COM15)", "COM15"), "USB")

    def test_menu_order_checks_and_click_or_right_click_targets(self):
        menu = self.window._create_device_menu(self.did)
        self.addCleanup(menu.deleteLater)
        self.assertEqual([a.text() for a in menu.actions()], ["屏蔽", "自动烧录"])
        self.assertEqual([a.text() for a in menu.actions()[0].menu().actions()], ["按端口屏蔽", "按设备屏蔽"])
        self.assertTrue(menu.actions()[1].isCheckable())
        with patch.object(self.window, "_show_device_menu") as show:
            QTest.mouseClick(self.card.menu_button, Qt.MouseButton.LeftButton)
            self.card.title_label.customContextMenuRequested.emit(QPoint(4, 4))
            self.card.menu_button.customContextMenuRequested.emit(QPoint(4, 4))
        self.assertEqual(show.call_count, 3)
        menu.actions()[0].menu().actions()[0].trigger()
        self.assertTrue(self.card.blocked)
        checked = self.window._create_device_menu(self.did)
        self.addCleanup(checked.deleteLater)
        self.assertTrue(checked.actions()[0].menu().actions()[0].isChecked())
        self.assertFalse(checked.actions()[1].isEnabled())

    def test_block_disables_all_buttons_and_excludes_manual_auto_and_bulk(self):
        self.window._set_device_blocked(self.did, "port", True)
        self.assertEqual(self.card.ribbon.text, "BLOCK")
        self.assertTrue(all(not button.isEnabled() for _, button, _ in self.card._action_buttons))
        self.assertFalse(self.card.efuse_button.isEnabled())
        self.window.auto_flash_button.setChecked(True)
        self.window._auto_flash_new_device(self.did)
        self.window.flash_firmware(self.did)
        self.window.erase_flash(self.did)
        self.window.reset_device(self.did)
        with patch("src.main_window.ExportFlashDialog") as export:
            self.window.export_firmware(self.did)
        export.assert_not_called()
        self.start.assert_not_called()
        self.window.flash_all_devices()
        self.window.erase_all_devices()
        self.assertEqual(self.start.call_count, 2)
        self.assertTrue(all(call.args[0].device.port == "COM8" for call in self.start.call_args_list))
        self.assertEqual({task.port for task in self.window._verify_widget._tasks}, {"COM8"})
        self.assertEqual({task.port for task in self.window._efuse_batch_widget._tasks}, {"COM8"})
        self.assertFalse(self.window._terminal_widget._port_is_available("COM90"))
        self.assertFalse(self.window._port_registry.is_available("COM7"))

    def test_persisted_device_rule_blocks_changed_com_and_unblock_restores_controls(self):
        self.window._set_device_blocked(self.did, "device", True)
        self.ports.return_value = [port("COM99")]
        self.window.refresh_ports()
        new_did, new_card = next(iter(self.window.device_cards.items()))
        self.assertTrue(new_card.blocked)
        self.assertEqual(new_card.ribbon.text, "BLOCK")
        self.assertTrue(DevicePolicyStore(self.policy.path).blocked_devices)
        self.window._set_device_blocked(new_did, "device", False)
        self.assertFalse(new_card.blocked)
        self.assertTrue(new_card.flash_button.isEnabled())
        self.assertEqual(new_card.ribbon.text, "")

    def test_single_port_auto_works_with_global_off_and_on_reconnect_of_another_device(self):
        self.window._set_port_auto_flash(self.did, True)
        self.assertFalse(self.window.auto_flash_button.isChecked())
        self.assertTrue(self.window.auto_refresh_button.isChecked())
        self.assertEqual(self.card.ribbon.text, "AUTO")
        self.wait_for_starts(1)
        self.assertEqual(self.start.call_args.args[0].device.port, "COM7")
        self.ports.return_value = []
        self.window.refresh_ports()
        self.ports.return_value = [port("COM7", "REPLACEMENT"), port("COM8", "DEVICE-B")]
        self.window.refresh_ports()
        self.wait_for_starts(2)
        self.assertTrue(all(c.args[0].device.port == "COM7" for c in self.start.call_args_list))

    def test_disarm_block_and_stop_cancel_pending_local_auto(self):
        for action in ("disarm", "block", "stop"):
            with self.subTest(action=action):
                self.window._set_port_auto_flash(self.did, True)
                if action == "disarm":
                    self.window._set_port_auto_flash(self.did, False)
                elif action == "block":
                    self.window._set_device_blocked(self.did, "port", True)
                else:
                    self.window.stop_all_devices()
                QTest.qWait(150)
                self.start.assert_not_called()
                if action == "block":
                    self.window._set_device_blocked(self.did, "port", False)

    def test_running_device_cannot_be_blocked_and_releasing_lease_allows_it(self):
        lease = self.window._port_registry.acquire("COM7", "efuse", "eFuse 烧录")
        self.assertIsNotNone(lease)
        self.window._set_device_blocked(self.did, "port", True)
        self.assertFalse(self.card.blocked)
        lease.release()
        self.window._set_device_blocked(self.did, "port", True)
        self.assertTrue(self.card.blocked)

    def test_global_disarm_does_not_disable_local_auto(self):
        self.window.auto_flash_button.setChecked(True)
        self.window._set_port_auto_flash(self.did, True)
        self.window.auto_flash_button.setChecked(False)
        QTest.qWait(150)
        self.assertEqual(self.start.call_count, 1)


if __name__ == "__main__":
    unittest.main()
