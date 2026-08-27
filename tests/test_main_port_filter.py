from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from src.main_window import OtoolEsptoolUI, is_bluetooth_serial_port


def _port(device: str, description: str, hwid: str) -> SimpleNamespace:
    return SimpleNamespace(
        device=device,
        description=description,
        hwid=hwid,
        serial_number="",
        location="",
        vid=None,
        pid=None,
        interface=None,
        product=None,
    )


class BluetoothPortFilterTests(unittest.TestCase):
    _app: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_bluetooth_port_detection_supports_localized_names_and_hardware_ids(self) -> None:
        self.assertTrue(
            is_bluetooth_serial_port(
                _port("COM13", "蓝牙链接上的标准串行 (COM13)", "n/a")
            )
        )
        self.assertTrue(
            is_bluetooth_serial_port(
                _port("COM14", "Standard Serial over Bluetooth link", "n/a")
            )
        )
        self.assertTrue(
            is_bluetooth_serial_port(
                _port("COM15", "Serial Port", r"BTHENUM\{00001101-0000-1000-8000-00805F9B34FB}")
            )
        )
        self.assertFalse(
            is_bluetooth_serial_port(
                _port("COM7", "USB Serial Port", "USB VID:PID=10C4:EA60")
            )
        )

    def test_show_all_toggle_reveals_and_rehides_bluetooth_ports(self) -> None:
        usb = _port("COM7", "USB Serial Port", "USB VID:PID=10C4:EA60")
        bluetooth = _port("COM13", "蓝牙链接上的标准串行 (COM13)", "n/a")
        with patch("src.main_window.list_ports.comports", return_value=[usb, bluetooth]):
            window = OtoolEsptoolUI()
            self._app.processEvents()
            window.refresh_ports()

            self.assertEqual(
                {card.device.port for card in window.device_cards.values()}, {"COM7"}
            )
            self.assertEqual(window.device_count_label.text(), "1 台")
            self.assertIn("已屏蔽 1 个", window.status_label.text())

            window.show_all_devices_button.setChecked(True)
            self.assertEqual(
                {card.device.port for card in window.device_cards.values()},
                {"COM7", "COM13"},
            )
            self.assertEqual(window.device_count_label.text(), "2 台")

            window.show_all_devices_button.setChecked(False)
            self.assertEqual(
                {card.device.port for card in window.device_cards.values()}, {"COM7"}
            )

        window.close()
        window.deleteLater()
        self._app.processEvents()


if __name__ == "__main__":
    unittest.main()
