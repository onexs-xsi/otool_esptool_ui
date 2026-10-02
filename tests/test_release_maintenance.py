from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from littlefs import LittleFS, UserContext

from src import constants
from src.merge_split_widget import _FsViewerDialog

ROOT = Path(__file__).resolve().parents[1]


class LocalBackendTests(unittest.TestCase):
    def test_empty_or_incomplete_directory_is_not_a_local_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(constants, "LOCAL_ESPTOOL_DIR", root):
                self.assertFalse(constants._local_esptool_available())
                (root / "esptool").mkdir()
                (root / "esptool" / "__init__.py").write_text("")
                self.assertFalse(constants._local_esptool_available())

    def test_complete_backend_is_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("esptool", "espefuse", "espsecure"):
                (root / name).mkdir()
                (root / name / "__init__.py").write_text("")
            with patch.object(constants, "LOCAL_ESPTOOL_DIR", root):
                self.assertTrue(constants._local_esptool_available())


class LittleFSReadOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def open_dialog(self, data, subtype="littlefs"):
        dialog = _FsViewerDialog("sample", subtype, data)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        return dialog

    def test_invalid_image_is_not_formatted_into_successful_empty_filesystem(self):
        data = (b"not a filesystem" * 600)[:8192].ljust(8192, b"\xff")
        dialog = self.open_dialog(data)
        self.assertIsNone(dialog._fs)
        self.assertNotIn("已挂载", dialog._status_lbl.text())
        self.assertFalse(dialog._extract_all_btn.isEnabled())

    def test_fat_and_spiffs_are_not_reported_as_littlefs(self):
        for subtype in ("fat", "spiffs"):
            with self.subTest(subtype=subtype):
                dialog = self.open_dialog(b"\xff" * 8192, subtype)
                self.assertIsNone(dialog._fs)
                self.assertIn("不支持", dialog._status_lbl.text())

    def test_valid_image_reads_nested_unicode_files_without_mutation(self):
        for block_size in (4096, 8192):
            with self.subTest(block_size=block_size):
                context = UserContext(buffsize=block_size * 16)
                fs = LittleFS(context=context, block_size=block_size, block_count=16)
                fs.mkdir("/资料")
                with fs.open("/资料/版本.txt", "wb") as stream:
                    stream.write("版本 α\n".encode())
                fs.unmount()
                original = bytes(context.buffer)
                dialog = self.open_dialog(original)
                self.assertIsNotNone(dialog._fs, dialog._status_lbl.text())
                self.assertEqual(dialog._read_file("/资料/版本.txt"), "版本 α\n".encode())
                self.assertEqual(bytes(dialog._buf), original)


class WorkerIntegrationTests(unittest.TestCase):
    def test_real_logger_adapter_preserves_progress_warning_and_unicode(self):
        # Only the hardware/tool action is replaced; upstream esptool and its
        # esp-pylib adapter are real so a logger API migration is exercised.
        code = '''
import sys
from types import SimpleNamespace
from unittest.mock import patch
from src import bootstrap
from esptool.logger import log
def run(args):
    log.print("分片", "中文", end="")
    log.warning("警告")
    log.error("错误")
    log.stage()
    log.progress_bar(1, 4, prefix="写入", suffix="字节")
    log.stage(finish=True)
sys.argv = ["worker", "__otool_worker__", "synthetic"]
with patch.object(bootstrap.importlib, "import_module", return_value=SimpleNamespace(main=run)):
    bootstrap.dispatch_internal_worker()
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = [json.loads(line.removeprefix("[[OTOOL_EVENT]]"))
                  for line in result.stdout.splitlines() if line.startswith("[[OTOOL_EVENT]]")]
        self.assertIn({"type": "log", "level": "info", "text": "分片 中文"}, events)
        self.assertIn({"type": "log", "level": "warning", "text": "Warning: 警告"}, events)
        self.assertIn({"type": "log", "level": "error", "text": "错误"}, events)
        self.assertIn({"type": "stage", "finish": True}, events)
        progress = next(event for event in events if event["type"] == "progress")
        self.assertEqual((progress["current"], progress["total"], progress["percent"]), (1, 4, 25))
        self.assertEqual(progress["prefix"], "写入")

    def test_real_backend_merge_reports_events_and_correct_bytes(self):
        with tempfile.TemporaryDirectory(prefix="固件 merge ") as directory:
            root = Path(directory)
            first, second, output = (root / name for name in ("a.bin", "b.bin", "merged.bin"))
            first.write_bytes(b"OTool")
            second.write_bytes(b"second")
            result = subprocess.run(
                [sys.executable, str(ROOT / "otool_esptool_ui.py"),
                 "__otool_worker__", "esptool", "--chip", "esp32",
                 "merge-bin", "-o", str(output), "0x0", str(first), "0x1000", str(second)],
                cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace", timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            events = [json.loads(line.removeprefix("[[OTOOL_EVENT]]"))
                      for line in result.stdout.splitlines() if line.startswith("[[OTOOL_EVENT]]")]
            self.assertTrue(any(event.get("type") == "log" and "Wrote" in event.get("text", "") for event in events), events)
            self.assertFalse(any(event.get("type") == "fatal" for event in events))
            self.assertEqual(events[0]["text"], "[Worker] 工作进程已启动")
            self.assertEqual(output.read_bytes(), b"OTool" + b"\xff" * 4091 + b"second")


if __name__ == "__main__":
    unittest.main()
