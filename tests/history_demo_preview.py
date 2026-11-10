"""Render the history prototype using labeled samples, with no serial operations."""
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

from PyQt6.QtCore import QEvent, QPoint
from PyQt6.QtGui import QFont, QFontDatabase, QHelpEvent
from PyQt6.QtWidgets import QApplication, QLabel
from src.device_policy import DevicePolicyStore
from src.flash_history_widget import PhaseDurationBar
from src.main_window import OtoolEsptoolUI
from tests.history_samples import sample_history

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
app = QApplication([])
app.setStyle("Fusion")
for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "consola.ttf"):
    path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
    if path.is_file():
        QFontDatabase.addApplicationFont(str(path))
app.setFont(QFont("Microsoft YaHei UI", 9))
ports = [SimpleNamespace(device=f"COM{n}", serial_number=f"DEMO-{n}", vid=0x1A86, pid=0x55D4,
                         location=f"1-{n}", hwid="USB", description=f"USB-SERIAL CH340 (COM{n})") for n in (15,16,34)]
with (patch("serial.tools.list_ports.comports", return_value=ports),
      patch.object(OtoolEsptoolUI, "_start_process"),
      patch("src.main_window.ChipInfoProbe"),
      patch("src.main_window._fetch_remote_avatar_bytes")):
    window = OtoolEsptoolUI(DevicePolicyStore())
    window.setWindowTitle("OTool 历史布局测试 · 模拟设备与示例数据")
    for record in sample_history():
        window._flash_history.upsert(record)
    window.show()
    app.processEvents()
    window.show_history_button.setChecked(True)
    window._history_panel.caption.setText("界面测试样例 · 不访问真实设备、不保存历史")
    card = next(c for c in window.device_cards.values() if c.device.port == "COM34")
    card.device.chip_name = "ESP32-P4 (revision v3.2)"
    card.device.mac = "02:00:00:ed:64:26"
    card.device.features = "Dual Core + LP Core, 400MHz"
    card.device.flash_size = "16MB"
    card.device.crystal = "40MHz"
    card.update_device(card.device)
    card.set_result_state("成功，待移除", "success")
    card.set_stage_text("烧录完成，请拔出设备")
    card.set_policy_state(False, True)
    card.append_log("[演示] 示例状态，未执行实际烧录。")
    card.append_log("[自动烧录] 本次烧录已完成，请拔出当前设备。")
    card.append_log("[芯片检测] 每 3 秒读取芯片信息与 MAC。")
    card.append_log("[芯片检测] USB 串口仍在不代表芯片在线。")
    for width, height, name in ((1920,1000,"history-demo-wide"),(1380,860,"history-demo-desktop"),(1024,760,"history-demo-narrow")):
        window.resize(width, height)
        app.processEvents()
        window.grab().save(str(out / f"{name}.png"))
    window.resize(1800,960)
    app.processEvents()
    window._history_panel.expanded.add("02:00:00:ed:64:26")
    window._history_panel.rebuild()
    app.processEvents()
    window._history_panel.grab().save(str(out / "history-detail.png"))
    bar = window._history_panel.findChildren(PhaseDurationBar)[0]
    point = QPoint(int(bar.width()*.55),6)
    QApplication.sendEvent(bar, QHelpEvent(QEvent.Type.ToolTip, point, bar.mapToGlobal(point)))
    app.processEvents()
    for widget in app.topLevelWidgets():
        if isinstance(widget,QLabel) and widget.objectName()=="qtooltip_label":
            widget.grab().save(str(out / "phase-tooltip.png"))
    window._history_panel.expanded.clear()
    window._history_panel.rebuild()
    app.processEvents()
    if live:
        app.exec()
    window.close()
    app.processEvents()
print(out.resolve())
