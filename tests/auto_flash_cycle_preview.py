"""Software-only UI preview for removal waiting and recent source menus.

python tests/auto_flash_cycle_preview.py OUTPUT_DIRECTORY [--live]
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

live = "--live" in sys.argv
if not live:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication
from src.constants import APP_VERSION
from src.device_policy import DevicePolicyStore
from src.main_window import OtoolEsptoolUI


def port(number):
    return SimpleNamespace(device=f"COM{number}", description=f"USB-Enhanced-SERIAL CH343 (COM{number})",
                           serial_number=f"PREVIEW-{number}", vid=0x1A86, pid=0x55D4,
                           location=f"1-{number}", hwid=f"USB SER=PREVIEW-{number}")


app = QApplication([])
app.setStyle("Fusion")
for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
app.setFont(QFont("Microsoft YaHei UI", 9))
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
ports = [port(15), port(16)]

with (tempfile.TemporaryDirectory(prefix="otool-source-preview-") as temporary,
      patch("serial.tools.list_ports.comports", side_effect=lambda: list(ports)),
      patch.object(OtoolEsptoolUI, "_start_process"),
      patch("src.main_window.ChipInfoProbe"),
      patch.object(OtoolEsptoolUI, "_validate_common_inputs", return_value=True),
      patch.object(OtoolEsptoolUI, "flash_firmware")):
    window = OtoolEsptoolUI(DevicePolicyStore())
    window.setWindowTitle(f"OTool {APP_VERSION} — 自动烧录流程预览（模拟设备）")
    window.show()
    app.processEvents()
    window._port_auto_flash.update(("com15", "com16"))
    for did, card in window.device_cards.items():
        window._sync_device_policy(did)
        card.append_log("[界面预览] 模拟流程状态，不执行实际设备烧录。")
        window._begin_auto_flash_removal_wait(card)
    window._poll_auto_flash_removal("com15")
    window._poll_auto_flash_removal("com15")
    ports[:] = [ports[0]]
    window._poll_auto_flash_removal("com16")
    root = Path(temporary)
    firmware = root / "OTool_ESP32S3_Controller_Release" / "output" / "controller_20261111_0x10000.bin"
    firmware.parent.mkdir(parents=True)
    firmware.write_bytes(b"preview")
    window._firmware_history.remember("bin", firmware)
    build = root / "environment_monitor_esp32s3_project" / "build"
    build.mkdir(parents=True)
    (build / "app.bin").write_bytes(b"preview")
    (build / "flasher_args.json").write_text(json.dumps({"flash_files": {"0x10000": "app.bin"}}))
    window._firmware_history.remember("build", build)
    row = window._flash_rows[0]
    row._populate_browse_menu()
    menu = row.browse_menu
    menu.ensurePolished()
    menu.resize(menu.sizeHint())
    menu.grab().save(str(out / "recent-source-menu.png"))
    app.processEvents()
    window.grab().save(str(out / "auto-flash-cycle.png"))
    window.resize(840, 700)
    app.processEvents()
    window.grab().save(str(out / "auto-flash-cycle-narrow.png"))
    window.resize(1320, 816)
    app.processEvents()
    if live:
        app.exec()
    window.close()
    app.processEvents()
print(out.resolve())
