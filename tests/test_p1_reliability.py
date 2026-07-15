from __future__ import annotations

import inspect
import os
import struct
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from src.efuse_batch_dialog import BurnEfuseBatchWidget, BurnTaskItem, BurnTaskState
from src.flash_size import parse_detected_flash_size
from src.main_window import OtoolEsptoolUI
from src.merge_split_widget import PT_MAGIC, analyze_merged_bin
from src.port_ownership import PortOwnershipRegistry
from src.terminal_decode import decode_payload_chunks


ROOT = Path(__file__).resolve().parents[1]


class P1ReliabilityPureTests(unittest.TestCase):
    def test_port_leases_are_exclusive_case_insensitively(self) -> None:
        registry = PortOwnershipRegistry()
        first = registry.acquire("COM7", "flash", "烧录")
        self.assertIsNotNone(first)
        self.assertIsNone(registry.acquire("com7", "terminal", "终端"))
        self.assertEqual(registry.claim_for("COM7").purpose, "烧录")

        first.release()
        second = registry.acquire("com7", "terminal", "终端")
        self.assertIsNotNone(second)
        # Releasing an old lease again must not clear the new claim.
        first.release()
        self.assertEqual(registry.claim_for("COM7").owner, "terminal")
        second.release()
        self.assertTrue(registry.is_available("COM7"))

    def test_flash_size_parser_supports_esptool_units_and_rejects_bad_output(self) -> None:
        self.assertEqual(
            parse_detected_flash_size("Detected flash size: 16MB"),
            16 * 1024 * 1024,
        )
        self.assertEqual(
            parse_detected_flash_size("Detected flash size: 512KB"),
            512 * 1024,
        )
        self.assertIsNone(parse_detected_flash_size("flash size unknown"))
        self.assertIsNone(parse_detected_flash_size("Detected flash size: 999GB"))

    def test_incremental_decoder_preserves_split_utf8_character(self) -> None:
        chunks = [b"A\xe4", b"\xb8", b"\xadB"]
        self.assertEqual(decode_payload_chunks(chunks, "utf-8"), ["A", "", "中B"])

    def test_partition_encrypted_flag_survives_analysis(self) -> None:
        data = bytearray(b"\xff" * 0x12000)
        record = struct.pack(
            "<HBBII16sI",
            PT_MAGIC,
            0,
            0,
            0x10000,
            0x1000,
            b"factory\0".ljust(16, b"\0"),
            1,
        )
        data[0x8000:0x8020] = record
        result = analyze_merged_bin(bytes(data), "esp32")
        self.assertTrue(result["partitions"][0]["encrypted"])

    def test_periodic_main_refresh_is_passive_enumeration(self) -> None:
        source = inspect.getsource(OtoolEsptoolUI.refresh_ports)
        self.assertNotIn("subprocess.run", source)
        self.assertNotIn("_get_chip_info", source)

    def test_main_station_acquires_port_before_starting_process(self) -> None:
        start_source = inspect.getsource(OtoolEsptoolUI._start_process)
        self.assertLess(
            start_source.index("self._port_registry.acquire"),
            start_source.index("QProcess(self)"),
        )
        self.assertNotIn(
            "self._port_registry.acquire",
            inspect.getsource(OtoolEsptoolUI._read_process_output),
        )

    def test_wheel_declares_runtime_resources(self) -> None:
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        package_data = config["tool"]["setuptools"]["package-data"]
        root_files = package_data["otool_esptool_ui"]
        self.assertIn("config.yaml", root_files)
        self.assertIn("THIRD_PARTY_NOTICES.md", root_files)
        self.assertIn("logo_all_size.ico", root_files)
        self.assertIn("assets/*.png", root_files)
        self.assertIn("assets/*.svg", package_data["otool_esptool_ui.src"])


class P1ReliabilityWidgetTests(unittest.TestCase):
    _app: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.widget = BurnEfuseBatchWidget(port_registry=PortOwnershipRegistry())

    def tearDown(self) -> None:
        self.widget.close()
        self.widget.deleteLater()
        self._app.processEvents()

    def test_efuse_configuration_is_visibly_locked_for_active_snapshot(self) -> None:
        self.widget._tasks.append(
            BurnTaskItem(
                device_id="device-1",
                port="COM7",
                transport_id="usb-1",
                state=BurnTaskState.READ_OK,
            )
        )
        self.widget._refresh_dev_table()
        self.assertFalse(self.widget._chip_combo.isEnabled())
        self.assertFalse(self.widget._baud_combo.isEnabled())
        self.assertFalse(self.widget._field_table.isEnabled())

        self.widget._tasks[0].state = BurnTaskState.FAILED
        self.widget._refresh_dev_table()
        self.assertTrue(self.widget._chip_combo.isEnabled())
        self.assertTrue(self.widget._field_table.isEnabled())

    def test_efuse_hotplug_poll_does_not_probe_ports_synchronously(self) -> None:
        port = SimpleNamespace(
            device="COM7",
            description="USB Serial Port",
            serial_number="UNIT-A",
            location="1-2",
            vid=0x10C4,
            pid=0xEA60,
            hwid="USB VID:PID=10C4:EA60 SER=UNIT-A",
        )
        with (
            patch("src.efuse_batch_dialog.list_ports.comports", return_value=[port]),
            patch.object(self.widget, "_schedule_next"),
        ):
            self.widget._poll_ports()
        source = inspect.getsource(self.widget._poll_ports)
        self.assertNotIn("subprocess.run", source)

    def test_main_window_injects_one_registry_into_every_workbench(self) -> None:
        with patch("src.main_window.list_ports.comports", return_value=[]):
            window = OtoolEsptoolUI()
            self._app.processEvents()
        registry = window._port_registry
        self.assertIs(window._efuse_batch_widget._port_registry, registry)
        self.assertIs(window._verify_widget._port_registry, registry)
        self.assertIs(window._merge_split_widget._port_registry, registry)
        self.assertIs(window._terminal_widget._port_registry, registry)
        window.close()
        window.deleteLater()
        self._app.processEvents()


if __name__ == "__main__":
    unittest.main()
