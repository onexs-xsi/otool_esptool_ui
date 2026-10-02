"""Render the real widgets without hardware; run once per QT_SCALE_FACTOR.

Usage: python tests/ui_matrix.py OUTPUT_DIRECTORY
This is a visual QA helper, not an application startup mode.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtGui import QFontDatabase, QFont
from PyQt6.QtWidgets import QApplication
from src.main_window import OtoolEsptoolUI

app = QApplication([])
app.setStyle("Fusion")
for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
    path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
    if path.is_file():
        QFontDatabase.addApplicationFont(str(path))
app.setFont(QFont("Microsoft YaHei UI", 9))
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
scale = float(os.environ.get("QT_SCALE_FACTOR", "1"))
records = []
with patch("serial.tools.list_ports.comports", return_value=[]):
    window = OtoolEsptoolUI()
    window.show()
    app.processEvents()
    for physical in ((1366, 768), (1920, 1080)):
        available = (int(physical[0] / scale) - 24, int(physical[1] / scale) - 60)
        for tab in range(5):
            window.tab_switcher.set_current(tab, animated=False)
            window.resize(*available)
            app.processEvents()
            name = f"{physical[0]}x{physical[1]}-{scale:g}-tab{tab}"
            window.grab().save(str(out / (name + ".png")))
            records.append({"name": name, "requested": available,
                            "actual": [window.width(), window.height()],
                            "footer_overlap": window.floating_info_panel.geometry().intersects(window.floating_tab_panel.geometry())})
    window.close()
    app.processEvents()
(out / f"matrix-{scale:g}.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
print(json.dumps(records))
