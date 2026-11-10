from __future__ import annotations

import copy
import json
import tempfile
import unittest
import sys
from pathlib import Path

from src.idf_build import load_idf_build


class IdfBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="中文 构建 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "_产物"
        self.root.mkdir()
        self.data = {
            "flash_files": {
                "0x20000": "应用.bin",
                "0x0": "bootloader/bootloader.bin",
                "0x9000": "partition_table/partition-table.bin",
                "0x30000": "数据 文件.bin",
            },
            "flash_settings": {
                "flash_mode": "dio", "flash_freq": "80m", "flash_size": "16MB",
            },
            "write_flash_args": [
                "--flash_mode", "dio", "--flash_size", "16MB", "--flash_freq", "80m",
            ],
            "extra_esptool_args": {
                "chip": "esp32s3", "before": "default_reset",
                "after": "hard_reset", "stub": False,
            },
            "app": {"offset": "0x20000", "file": "应用.bin", "encrypted": "false"},
        }
        for filename in self.data["flash_files"].values():
            path = self.root / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"firmware")

    def write_manifest(self, data=None):
        (self.root / "flasher_args.json").write_text(
            json.dumps(self.data if data is None else data, ensure_ascii=False), encoding="utf-8"
        )

    def test_loads_complete_project_sorted_and_normalizes_legacy_parameters(self):
        self.write_manifest()
        config = load_idf_build(self.root)
        self.assertEqual(config.entries, [
            ("0x0", str(self.root / "bootloader/bootloader.bin")),
            ("0x9000", str(self.root / "partition_table/partition-table.bin")),
            ("0x20000", str(self.root / "应用.bin")),
            ("0x30000", str(self.root / "数据 文件.bin")),
        ])
        self.assertEqual(config.chip, "esp32s3")
        self.assertEqual(config.global_args, [
            "--chip", "esp32s3", "--before", "default-reset",
            "--after", "hard-reset", "--no-stub",
        ])
        self.assertEqual(config.write_flash_args, [
            "--flash-mode", "dio", "--flash-size", "16MB", "--flash-freq", "80m",
        ])

    def test_absolute_windows_paths_and_leading_underscore_are_preserved(self):
        literal = self.root / "_app.bin"
        literal.write_bytes(b"literal")
        candidate = self.root.parent / f"{self.root.name}_app.bin"
        candidate.write_bytes(b"candidate")
        self.data["flash_files"]["0x20000"] = str(literal)
        self.write_manifest()
        config = load_idf_build(self.root)
        self.assertEqual(config.entries[2][1], str(literal))
        self.assertEqual(Path(config.entries[2][1]).read_bytes(), b"literal")

    def test_windows_relative_backslashes_and_manifest_bom_are_supported(self):
        self.data["flash_files"]["0x0"] = r"bootloader\bootloader.bin"
        self.write_manifest()
        manifest = self.root / "flasher_args.json"
        manifest.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8-sig")
        self.assertEqual(load_idf_build(self.root).entries[0][1],
                         str(self.root / "bootloader/bootloader.bin"))

    def test_missing_manifest_does_not_guess_bin_addresses(self):
        with self.assertRaisesRegex(ValueError, "flasher_args.json"):
            load_idf_build(self.root)

    def test_malformed_json_and_duplicate_offsets_are_rejected(self):
        manifest = self.root / "flasher_args.json"
        for text in ["{", '{"flash_files": {"0x0": "a.bin", "0x0": "b.bin"}}']:
            with self.subTest(text=text):
                manifest.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_idf_build(self.root)

    def test_bad_manifest_shapes_are_rejected_with_value_error(self):
        for data in [[], {}, {"flash_files": {}}, {"flash_files": []},
                     {"flash_files": {"0x0": 1}}, {"flash_files": {"bad": "应用.bin"}}]:
            with self.subTest(data=data):
                self.write_manifest(data)
                with self.assertRaises(ValueError):
                    load_idf_build(self.root)

    def test_unreadable_missing_empty_and_overflow_images_are_rejected(self):
        from unittest.mock import patch
        self.write_manifest()
        with patch("src.esptool_commands.Path.open", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(ValueError, "读取"):
                load_idf_build(self.root)
        image = self.root / "应用.bin"
        image.unlink()
        with self.assertRaisesRegex(ValueError, "固件"):
            load_idf_build(self.root)
        image.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "空文件"):
            load_idf_build(self.root)
        image.write_bytes(b"firmware")
        self.data["flash_files"]["0xffffffff"] = self.data["flash_files"].pop("0x20000")
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "地址范围"):
            load_idf_build(self.root)

    def test_duplicate_numeric_offsets_and_erase_sector_overlap_are_rejected(self):
        for address in ["0x00000", "0x100"]:
            with self.subTest(address=address):
                data = copy.deepcopy(self.data)
                data["flash_files"][address] = data["flash_files"].pop("0x20000")
                self.write_manifest(data)
                with self.assertRaisesRegex(ValueError, "重叠"):
                    load_idf_build(self.root)

    def test_optional_parameters_can_be_absent(self):
        self.write_manifest({"flash_files": self.data["flash_files"]})
        config = load_idf_build(self.root)
        self.assertIsNone(config.chip)
        self.assertEqual(config.global_args, [])
        self.assertEqual(config.write_flash_args, [])

    def test_missing_backend_is_reported_as_an_import_error_to_the_user(self):
        from unittest.mock import patch
        self.write_manifest()
        with patch.dict(sys.modules, {"esptool": None}):
            with self.assertRaisesRegex(ValueError, "参数"):
                load_idf_build(self.root)

    def test_explorer_quoted_directory_can_be_pasted(self):
        self.write_manifest()
        self.assertEqual(load_idf_build(Path(f'"{self.root}"')).directory, self.root)

    def test_write_args_supply_settings_when_flash_settings_are_absent(self):
        del self.data["flash_settings"]
        self.write_manifest()
        self.assertEqual(load_idf_build(self.root).write_flash_args, [
            "--flash-mode", "dio", "--flash-size", "16MB", "--flash-freq", "80m",
        ])

    def test_conflicting_and_unsupported_parameters_are_rejected(self):
        mutations = [
            ("flash_settings", {"flash_mode": "invalid"}),
            ("flash_settings", {"flash_freq": "invalid"}),
            ("flash_settings", {"flash_size": "invalid"}),
            ("flash_settings", {"flash_mode": "qio"}),
            ("flash_settings", []),
            ("extra_esptool_args", {"chip": "not-a-chip"}),
            ("extra_esptool_args", {"before": "invalid"}),
            ("extra_esptool_args", {"stub": "false"}),
            ("extra_esptool_args", {"port": "COM99"}),
            ("write_flash_args", ["--force"]),
            ("write_flash_args", ["--flash-mode"]),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                data = copy.deepcopy(self.data)
                data[key] = value
                self.write_manifest(data)
                with self.assertRaisesRegex(ValueError, "参数"):
                    load_idf_build(self.root)


if __name__ == "__main__":
    unittest.main()
