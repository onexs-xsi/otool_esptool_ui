from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint
from PyQt6.QtGui import QHelpEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QToolTip

from src.chip_probe import classify_probe
from src.device_policy import DevicePolicyStore
from src.flash_history import FlashHistoryStore, HistoryPreferences
from tests.history_samples import sample_history
from src.flash_history_widget import FlashHistoryPanel, PhaseDurationBar
from src.flash_timing import FlashRunTimer, instrument_esptool_phases
from src.main_window import OtoolEsptoolUI
from src.constants import APP_TITLE, APP_VERSION


class FlashTimingTests(unittest.TestCase):
    def test_monotonic_stage_totals_and_finish_are_stable(self):
        now = [10.0]
        timer = FlashRunTimer("COM34", ["firmware.bin"], clock=lambda: now[0])
        now[0] = 10.5
        timer.switch("connect")
        now[0] = 12
        timer.switch("write")
        now[0] = 15
        record = timer.finish("success", mac="02:00:00:00:00:01")
        self.assertEqual(record["elapsed_ms"], 5000)
        self.assertEqual(sum(p["duration_ms"] for p in record["phases"]), 5000)
        self.assertEqual([p["phase"] for p in record["phases"]], ["prepare","connect","write"])
        now[0] = 100
        self.assertEqual(timer.snapshot()["elapsed_ms"], 5000)

    def test_instrumentation_uses_actual_calls_and_restores_inheritance(self):
        class Loader:
            def flash_begin(self): return 1
            def flash_block(self): return 2
            def flash_md5sum(self): return 3
        class Child(Loader):
            def hard_reset(self): return 4
        module = SimpleNamespace(ESPLoader=Loader, connect_esp=lambda: Child())
        events=[]
        original=Loader.flash_begin
        with instrument_esptool_phases(module,events.append):
            loader=module.connect_esp()
            self.assertEqual(loader.flash_begin(),1)
            loader.flash_block();loader.flash_block();loader.flash_md5sum();loader.hard_reset()
        self.assertEqual([e["phase"] for e in events],["connect","erase","write","verify","reset","finish"])
        self.assertIs(Loader.flash_begin,original)
        self.assertNotIn("flash_begin",Child.__dict__)
        self.assertTrue(all(a["elapsed_ms"]<=b["elapsed_ms"] for a,b in zip(events,events[1:])))

    def test_usb_port_alone_is_not_evidence_of_a_chip_and_busy_is_not_absence(self):
        absent=classify_probe("A fatal error occurred: Failed to connect: No serial data received",1)
        self.assertEqual(absent["status"],"absent")
        self.assertEqual(classify_probe("PermissionError: Access is denied",1)["status"],"busy")
        self.assertEqual(classify_probe("Traceback: internal tool error",1)["status"],"error")
        result=classify_probe('[[OTOOL_EVENT]]'+json.dumps({"type":"log","text":"MAC: E8:F6:0A:ED:64:26"}),0)
        self.assertEqual(result["mac"],"e8:f6:0a:ed:64:26")
        self.assertEqual(result["status"],"present")

    def test_local_history_preserves_separate_attempts_and_recovers_interruption(self):
        with tempfile.TemporaryDirectory(prefix="otool-phase-history-") as directory:
            path=Path(directory)/"history.json"
            store=FlashHistoryStore(path,limit=2)
            samples=sample_history()
            store.upsert(samples[0]);store.upsert(samples[3]);store.upsert(samples[-1])
            self.assertEqual(len(store.records),2)
            self.assertEqual(FlashHistoryStore(path).records[0]["status"],"interrupted")

    def test_invalid_history_is_visible_and_never_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="otool-invalid-history-") as directory:
            path=Path(directory)/"history.json"
            record=sample_history()[0]
            record["phases"][0]["duration_ms"]="invalid"
            original=json.dumps({"version":1,"records":[record]})
            path.write_text(original,encoding="utf-8")
            store=FlashHistoryStore(path)
            self.assertTrue(store.load_error)
            with self.assertRaisesRegex(ValueError,"已保留原文件"):
                store.upsert(sample_history()[1])
            self.assertEqual(path.read_text(encoding="utf-8"),original)
            self.assertEqual(len(store.records),1)


class FlashHistoryUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def test_bar_proportions_and_hover_show_duration(self):
        record=sample_history()[0]
        bar=PhaseDurationBar(record)
        try:
            bar.resize(400,12)
            phase, ms, ratio=bar.phase_at(220)
            self.assertEqual(phase,"write")
            self.assertGreater(ratio,50)
            point=QPoint(220,6)
            QApplication.sendEvent(bar,QHelpEvent(QEvent.Type.ToolTip,point,point))
            self.assertIn("写入",QToolTip.text())
            self.assertIn("8.24 s",QToolTip.text())
        finally:
            QToolTip.hideText();bar.deleteLater()

    def test_mac_grouping_search_and_expansion(self):
        panel=FlashHistoryPanel()
        try:
            panel.set_records(sample_history())
            self.assertEqual(panel.count.text(),"6 台 / 7 次")
            panel.search.setText("ed:64:26")
            self.assertEqual(panel.count.text(),"1 台 / 2 次")
            panel.expanded.add("02:00:00:ed:64:26")
            panel.rebuild()
            self.assertGreaterEqual(len(panel.findChildren(PhaseDurationBar)),3)
            panel.search.setText("unmatched")
            self.assertEqual(panel.count.text(),"0 台 / 0 次")
        finally:
            panel.deleteLater()

    def test_unidentified_history_is_hidden_until_show_all_without_removing_records(self):
        panel=FlashHistoryPanel()
        records=sample_history()
        records.extend([
            {**records[0],"id":"no-mac","mac":""},
            {**records[0],"id":"no-chip","mac":"02:00:00:00:00:03","chip":"未识别"},
        ])
        try:
            panel.set_records(records)
            self.assertFalse(panel.show_all_button.isChecked())
            self.assertEqual(panel.count.text(),"6 台 / 7 次")
            self.assertIn("已隐藏 2 条",panel.hint.text())
            panel.show_all_button.setChecked(True)
            self.assertEqual(panel.count.text(),"8 台 / 9 次")
            panel.search.setText("未识别")
            self.assertEqual(panel.count.text(),"1 台 / 1 次")
            panel.show_all_button.setChecked(False)
            self.assertEqual(panel.count.text(),"0 台 / 0 次")
            panel.search.clear()
            panel.set_records(records)
            self.assertEqual(len(panel.records),9)
            self.assertEqual(panel.count.text(),"6 台 / 7 次")
        finally:
            panel.deleteLater()

    def test_history_is_hidden_by_default_and_user_choice_survives_restart(self):
        with (tempfile.TemporaryDirectory(prefix="otool-history-pref-") as directory,
              patch("serial.tools.list_ports.comports",return_value=[]),
              patch("src.main_window._fetch_remote_avatar_bytes")):
            policy=DevicePolicyStore(Path(directory)/"policy.json")
            first=OtoolEsptoolUI(policy)
            self.app.processEvents()
            self.assertFalse(first.show_history_button.isChecked())
            first.show_history_button.setChecked(True)
            self.assertTrue(HistoryPreferences(Path(directory)/"ui_preferences.json").show_history)
            first.close();first.deleteLater();self.app.processEvents()
            second=OtoolEsptoolUI(policy)
            self.app.processEvents()
            self.assertTrue(second.show_history_button.isChecked())
            second.close();second.deleteLater();self.app.processEvents()

    def test_full_application_starts_with_real_empty_history_and_five_workbenches(self):
        with (patch("serial.tools.list_ports.comports",return_value=[]),
              patch("src.main_window._fetch_remote_avatar_bytes"),
              patch.object(sys,"argv",["otool_esptool_ui.py","--history-demo"])):
            window=OtoolEsptoolUI(DevicePolicyStore())
            try:
                self.app.processEvents()
                window.show_history_button.setChecked(True)
                self.assertEqual(window.windowTitle(),f"{APP_TITLE} v{APP_VERSION}")
                self.assertEqual(window.page_stack.count(),5)
                self.assertEqual(window._flash_history.records,[])
                self.assertEqual(window._history_panel.count.text(),"0 台 / 0 次")
                self.assertEqual(window._history_panel.caption.text(),"按 MAC 汇总 · 最近烧录优先")
            finally:
                window.close();window.deleteLater();self.app.processEvents()

    def test_expanded_cards_fit_content_without_overlap_at_sidebar_widths(self):
        panel=FlashHistoryPanel()
        try:
            panel.show()
            panel.expanded.add("02:00:00:ed:64:26")
            for width in (350,390,420):
                panel.resize(width,720)
                panel.set_records(sample_history())
                QTest.qWait(30)
                cards=[panel.list_layout.itemAt(i).widget() for i in range(panel.list_layout.count())]
                for previous,current in zip(cards,cards[1:]):
                    self.assertLess(previous.geometry().bottom(),current.geometry().top())
                for card in cards:
                    title=card.findChild(QLabel,"historyDeviceTitle")
                    mac=card.findChild(QLabel,"historyMac")
                    self.assertEqual(title.geometry().center().y(),mac.geometry().center().y())
                    self.assertGreater(mac.x(),title.x())
                    self.assertGreaterEqual(mac.width(),mac.sizeHint().width())
                    for label in card.findChildren(QLabel):
                        point=label.mapTo(card,QPoint(0,label.height()))
                        self.assertLessEqual(point.y(),card.height())
                        self.assertGreaterEqual(label.height(),label.fontMetrics().height())
        finally:
            panel.close();panel.deleteLater();self.app.processEvents()

    def test_worker_events_persist_new_identity_start_time_and_actual_phases(self):
        fake_port=SimpleNamespace(device="COM7",serial_number="TEST",location="1-2",vid=0x10c4,
                                  pid=0xea60,hwid="USB",description="USB Serial")
        with (tempfile.TemporaryDirectory(prefix="otool-worker-history-") as directory,
              patch("serial.tools.list_ports.comports",return_value=[fake_port]),
              patch("src.main_window._fetch_remote_avatar_bytes")):
            window=OtoolEsptoolUI(DevicePolicyStore(Path(directory)/"policy.json"))
            try:
                self.app.processEvents()
                card=next(iter(window.device_cards.values()))
                card.device.mac="02:00:00:00:00:99"
                script="""import json,time
def event(data): print('[[OTOOL_EVENT]]'+json.dumps(data),flush=True)
event({'type':'phase','phase':'connect','elapsed_ms':0})
event({'type':'log','text':'Chip type: ESP32-S3\\nMAC: 02:00:00:00:00:22'})
time.sleep(.02)
event({'type':'phase','phase':'write','elapsed_ms':20})
time.sleep(.02)
event({'type':'phase','phase':'verify','elapsed_ms':40})
"""
                window._start_process(card,[sys.executable,"-u","-c",script,"write-flash","0x0","new.bin"],
                                      "测试",active_action="flash")
                for _ in range(100):
                    QTest.qWait(20)
                    if card.process is None: break
                self.assertIsNone(card.process)
                records=FlashHistoryStore(Path(directory)/"flash_history.json").records
                self.assertEqual(len(records),1)
                record=records[0]
                self.assertEqual(record["mac"],"02:00:00:00:00:22")
                self.assertEqual(record["chip"],"ESP32-S3")
                self.assertEqual(record["status"],"success")
                self.assertEqual(record["firmware"],["new.bin"])
                self.assertIn("started_at",record)
                self.assertEqual([p["phase"] for p in record["phases"]],["prepare","connect","write","verify"])
                self.assertAlmostEqual(sum(p["duration_ms"] for p in record["phases"]),record["elapsed_ms"])
            finally:
                window.close();window.deleteLater();self.app.processEvents()


if __name__=="__main__":
    unittest.main()
