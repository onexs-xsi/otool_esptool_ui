from __future__ import annotations

import gc
import subprocess
import sys
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from src import bootstrap


ROOT = Path(__file__).resolve().parents[1]


class BootstrapImportOrderTests(unittest.TestCase):
    def test_importing_bootstrap_does_not_import_main_window(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import src.bootstrap; "
                    "raise SystemExit('src.main_window' in sys.modules)"
                ),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_qt_dll_directory_handle_survives_configuration(self) -> None:
        created_handles: list[weakref.ReferenceType[object]] = []

        class FakeDllDirectoryHandle:
            pass

        def fake_add_dll_directory(_path: str) -> FakeDllDirectoryHandle:
            handle = FakeDllDirectoryHandle()
            created_handles.append(weakref.ref(handle))
            return handle

        try:
            with patch.object(
                bootstrap.os,
                "add_dll_directory",
                side_effect=fake_add_dll_directory,
            ):
                bootstrap.configure_qt_dll_path()
            gc.collect()

            self.assertEqual(len(created_handles), 1)
            self.assertIsNotNone(created_handles[0]())
        finally:
            handles = getattr(bootstrap, "_QT_DLL_DIRECTORY_HANDLES", None)
            if handles is not None:
                handles.clear()


if __name__ == "__main__":
    unittest.main()
