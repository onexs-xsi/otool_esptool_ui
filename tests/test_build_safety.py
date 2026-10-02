from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from src.build_safety import partition_root_icu_binaries


ROOT = Path(__file__).resolve().parents[1]


class FrozenBuildSafetyTests(unittest.TestCase):
    def test_release_validation_rejects_stale_metadata_and_backend(self) -> None:
        from src.build_safety import validate_release_inputs

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "otool_esptool_ui.py").write_text('__version__ = "0.6.14"')
            (root / "src" / "constants.py").write_text('_DEFAULT_VERSION = "0.6.14"')
            (root / "pyproject.toml").write_text('[project]\nversion = "0.6.14"\ndependencies = ["esptool==5.4.0"]')
            (root / "README.md").write_text('> ESP tool · v0.6.14', encoding="utf-8")
            (root / "requirements-build.txt").write_text('esptool==5.4.0\n')
            self.assertEqual(validate_release_inputs(root, lambda _name: "5.4.0"), "0.6.14")
            (root / "src" / "constants.py").write_text('_DEFAULT_VERSION = "0.6.13"')
            with self.assertRaisesRegex(RuntimeError, "version"):
                validate_release_inputs(root, lambda _name: "5.4.0")
            (root / "src" / "constants.py").write_text('_DEFAULT_VERSION = "0.6.14"')
            with self.assertRaisesRegex(RuntimeError, "esptool"):
                validate_release_inputs(root, lambda _name: "5.3.1")
            for name in ("esptool", "espefuse", "espsecure"):
                path = root / "esptool" / name
                path.mkdir(parents=True)
                (path / "__init__.py").write_text('__version__ = "5.3.1"')
            with self.assertRaisesRegex(RuntimeError, "submodule"):
                validate_release_inputs(root, lambda _name: "5.4.0")

    def test_external_root_icu_dlls_are_excluded(self) -> None:
        binaries = [
            ("icuuc.dll", r"C:\tools\poppler\icuuc.dll", "BINARY"),
            ("ICUDT78.DLL", r"C:\tools\poppler\icudt78.dll", "BINARY"),
            ("icu.dll", r"C:\Windows\System32\icu.dll", "BINARY"),
            (
                r"PyQt6\Qt6\bin\icuuc.dll",
                r"C:\site-packages\PyQt6\Qt6\bin\icuuc.dll",
                "BINARY",
            ),
            (
                r"PyQt6\Qt6\bin\Qt6Core.dll",
                r"C:\site-packages\PyQt6\Qt6\bin\Qt6Core.dll",
                "BINARY",
            ),
        ]

        kept, excluded = partition_root_icu_binaries(binaries)

        self.assertEqual([entry[0] for entry in excluded], [
            "icuuc.dll",
            "ICUDT78.DLL",
            "icu.dll",
        ])
        self.assertEqual([entry[0] for entry in kept], [
            r"PyQt6\Qt6\bin\icuuc.dll",
            r"PyQt6\Qt6\bin\Qt6Core.dll",
        ])

    def test_spec_applies_root_icu_filter(self) -> None:
        spec_text = (ROOT / "otool_esptool_ui.spec").read_text(encoding="utf-8")

        self.assertIn("partition_root_icu_binaries(a.binaries)", spec_text)


if __name__ == "__main__":
    unittest.main()
