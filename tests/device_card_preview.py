"""Render/inspect device-card states with simulated ports and no hardware access.

python tests/device_card_preview.py OUTPUT_DIRECTORY [--live]
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

live = "--live" in sys.argv
if not live:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication

from src.device_policy import DeviceIdentity, DevicePolicyStore
from src.main_window import OtoolEsptoolUI


def port(number, model):
    return SimpleNamespace(device=f"COM{number}", description=f"USB-Enhanced-SERIAL {model} (COM{number})",
                           serial_number=f"PREVIEW-{number}", vid=0x1A86, pid=0x55D4, location=f"1-{number}",
                           hwid=f"USB VID:PID=1A86:55D4 SER=PREVIEW-{number}")


app = QApplication([])
app.setStyle("Fusion")
for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
    if font_path.is_file():
        QFontDatabase.addApplicationFont(str(font_path))
app.setFont(QFont("Microsoft YaHei UI", 9))
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
ports = [port(15, "CH343"), port(16, "CH9102"), port(34, "CH340")]
policy = DevicePolicyStore()
policy.set_blocked(DeviceIdentity.from_device(ports[2]), "port", True)

with (patch("serial.tools.list_ports.comports", return_value=ports),
      patch.object(OtoolEsptoolUI, "_start_process"),
      patch.object(OtoolEsptoolUI, "_validate_common_inputs", return_value=True),
      patch.object(OtoolEsptoolUI, "flash_firmware")):
    window = OtoolEsptoolUI(policy)
    window.setWindowTitle("OTool 0.6.19 — 设备卡片预览（模拟串口）")
    window._port_auto_flash.add("com16")
    window.show()
    app.processEvents()
    for did, card in window.device_cards.items():
        window._sync_device_policy(did)
        card.append_log("界面预览：模拟串口，不执行芯片操作。")
    app.processEvents()
    window.grab().save(str(out / "cards-desktop.png"))
    for card in window.device_cards.values():
        card.grab().save(str(out / f"card-{card.device.port.lower()}.png"))
    first_did, first_card = next(iter(window.device_cards.items()))
    menu = window._create_device_menu(first_did)
    menu.ensurePolished()
    menu.resize(menu.sizeHint())
    menu.grab().save(str(out / "device-menu.png"))
    submenu = menu.actions()[0].menu()
    submenu.ensurePolished()
    submenu.resize(submenu.sizeHint())
    submenu.grab().save(str(out / "block-submenu.png"))
    menu.deleteLater()
    window.resize(840, 700)
    app.processEvents()
    window.grab().save(str(out / "cards-narrow.png"))
    window.resize(1320, 816)
    app.processEvents()
    if live:
        sys.exit(app.exec())
    window.close()
    app.processEvents()
print(out.resolve())
