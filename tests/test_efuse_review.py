from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from src.efuse_batch_dialog import BurnEfuseBatchWidget, EfuseFieldConfig
from src.efuse_batch_safety import (
    EfuseTarget,
    evaluate_efuse_precheck,
    evaluate_efuse_verification,
    extract_stable_device_identity,
)
from src.efuse_dialog import EFuseDialog
from src.models import DeviceInfo


class EfuseValueReviewTests(unittest.TestCase):
    def test_missing_and_null_values_never_prove_erased_or_satisfied(self) -> None:
        cases = ({}, {"value": None}, {"value": ""}, {"value": "none"},
                 {"value": "0", "raw_value": None}, {"value": "0", "raw_value": ""})
        for info in cases:
            for target in ("0", "1"):
                with self.subTest(info=info, target=target):
                    fields = (EfuseTarget("FIELD", target),)
                    result = evaluate_efuse_precheck(fields, {"FIELD": info})
                    self.assertEqual(result.conflicts, ("FIELD",))
                    self.assertEqual(result.to_burn, ())
                    self.assertEqual(evaluate_efuse_verification(fields, {"FIELD": info}), ("FIELD",))

    def test_out_of_range_bits_cannot_pass_via_equal_display_text(self) -> None:
        fields = (EfuseTarget("FIELD", "2"),)
        data = {"FIELD": {"value": "2", "raw_value": "0x2", "bit_len": 1}}
        self.assertEqual(evaluate_efuse_precheck(fields, data).conflicts, ("FIELD",))
        self.assertEqual(evaluate_efuse_verification(fields, data), ("FIELD",))

    def test_string_permission_flags_do_not_authorize_writes(self) -> None:
        fields = (EfuseTarget("FIELD", "1"),)
        for permission in ("readable", "writeable"):
            with self.subTest(permission=permission):
                data = {"FIELD": {"value": 0, permission: "false"}}
                self.assertEqual(evaluate_efuse_precheck(fields, data).conflicts, ("FIELD",))

    def test_unreadable_identity_is_not_used(self) -> None:
        self.assertEqual(extract_stable_device_identity({
            "MAC_FACTORY": {"value": "AA:BB:CC:01:02:03", "readable": False}
        }), "")

    def test_false_boolean_remains_a_valid_erased_value(self) -> None:
        fields = (EfuseTarget("FIELD", "1"),)
        self.assertTrue(evaluate_efuse_precheck(fields, {"FIELD": {"value": False}}).can_burn)


class EfuseDialogReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.dialog = EFuseDialog(DeviceInfo("test", "COM_TEST", "Test", "esp32s3"), "115200")

    def tearDown(self) -> None:
        self.dialog.process = None
        self.dialog.close()
        self.dialog.deleteLater()
        self.app.processEvents()

    def test_escape_cannot_close_active_burn(self) -> None:
        process = MagicMock()
        self.dialog.process = process
        self.dialog._is_reading = False
        self.dialog.show()
        with patch.object(QMessageBox, "warning") as warning:
            QTest.keyClick(self.dialog, Qt.Key.Key_Escape)
        self.assertTrue(self.dialog.isVisible())
        self.assertIs(self.dialog.process, process)
        process.kill.assert_not_called()
        warning.assert_called_once()

    def test_stale_read_callback_cannot_dispose_new_process(self) -> None:
        old_process, new_process = MagicMock(), MagicMock()
        self.dialog.process = new_process
        self.dialog._is_reading = True
        self.dialog._read_buf = ["current"]
        self.dialog._set_busy(True)
        self.dialog._on_output(self.dialog.burn_log, old_process)
        self.dialog._on_finished(1, None, old_process)
        self.assertIs(self.dialog.process, new_process)
        self.assertEqual(self.dialog._read_buf, ["current"])
        self.assertFalse(self.dialog.read_btn.isEnabled())
        old_process.readAllStandardOutput.assert_not_called()
        new_process.deleteLater.assert_not_called()

    def test_stop_read_invalidates_and_disconnects_process(self) -> None:
        process = MagicMock()
        self.dialog.process = process
        self.dialog._is_reading = True
        self.dialog._stop_process()
        self.assertIsNone(self.dialog.process)
        process.blockSignals.assert_called_once_with(True)
        process.kill.assert_called_once()
        process.waitForFinished.assert_called_once()
        self.assertIn("已停止", self.dialog.row_count_lbl.text())

    def test_failed_read_does_not_publish_partial_results(self) -> None:
        process = MagicMock()
        process.readAllStandardOutput.return_value = b""
        self.dialog.process = process
        self.dialog._is_reading = True
        self.dialog._read_buf = [json.dumps({"FIELD": {"value": 1}})]
        self.dialog._on_finished(1, None, process)
        self.assertEqual(self.dialog.table.rowCount(), 0)
        self.assertIn("读取失败", self.dialog.row_count_lbl.text())

    def test_last_buffered_output_is_drained_before_parsing(self) -> None:
        process = MagicMock()
        process.readAllStandardOutput.return_value = b'{"FIELD":{"value":1}}'
        self.dialog.process = process
        self.dialog._is_reading = True
        self.dialog._on_finished(0, None, process)
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertEqual(self.dialog.table.item(0, 2).text(), "FIELD")

    def test_failed_start_disposes_process_and_restores_controls(self) -> None:
        process = MagicMock()
        process.waitForStarted.return_value = False
        self.dialog._is_reading = True
        with patch("src.efuse_dialog.QProcess", return_value=process):
            self.dialog._run_cmd(["fake-espefuse", "summary"], self.dialog.burn_log)
        self.assertIsNone(self.dialog.process)
        process.deleteLater.assert_called_once()
        self.assertTrue(self.dialog.read_btn.isEnabled())

    def test_null_and_unreadable_values_display_unknown(self) -> None:
        rows = self.dialog._parse_summary_json(json.dumps({
            "MISSING": {"value": None},
            "HIDDEN": {"value": "??", "readable": False},
        }))
        self.dialog._populate_table(rows)
        self.assertEqual(self.dialog.table.item(0, 3).text(), "未知")
        for row in (0, 1):
            badge = self.dialog.table.cellWidget(row, 0).findChild(QLabel)
            self.assertIn("未知", badge.text())
        self.dialog.only_burned_btn.setChecked(True)
        self.assertEqual(self.dialog.table.rowCount(), 0)

    def test_zero_byte_formats_are_not_shown_as_burned(self) -> None:
        for value in ("00:00:00:00:00:00", "00-00-00", "00 00 00", "00.00"):
            with self.subTest(value=value):
                self.assertFalse(self.dialog._is_burned(value))
        self.assertTrue(self.dialog._is_burned("00:00:00:00:00:01"))

    def test_active_read_cannot_be_reclassified_as_burn(self) -> None:
        process = MagicMock()
        self.dialog.process = process
        self.dialog._is_reading = True
        self.dialog.efuse_name_edit.setText("FIELD")
        self.dialog.efuse_value_edit.setText("1")
        with patch.object(QMessageBox, "warning") as warning:
            self.dialog._burn_efuse()
        warning.assert_not_called()
        self.assertTrue(self.dialog._is_reading)


class EfuseBatchConfigReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.widget = BurnEfuseBatchWidget()
        self.widget._field_configs = [EfuseFieldConfig("FIELD", "1")]

    def tearDown(self) -> None:
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def test_enabled_incomplete_rows_block_whole_config(self) -> None:
        self.widget._field_configs.append(EfuseFieldConfig("OTHER", " "))
        with patch.object(QMessageBox, "warning") as warning:
            self.assertIsNone(self.widget._capture_run_config())
        self.assertIn("2", warning.call_args.args[2])

    def test_disabled_incomplete_row_is_ignored(self) -> None:
        self.widget._field_configs.append(EfuseFieldConfig("", "", enabled=False))
        self.assertEqual(self.widget._capture_run_config().fields, (EfuseTarget("FIELD", "1"),))

    def test_baud_must_be_positive_ascii_integer(self) -> None:
        self.widget._baud_combo.setEditable(True)
        for baud in ("0", "-1", "１２３", "²", "1.5"):
            with self.subTest(baud=baud), patch.object(QMessageBox, "warning"):
                self.widget._baud_combo.setCurrentText(baud)
                self.assertIsNone(self.widget._capture_run_config())

    def test_snapshot_normalizes_field_names_and_whitespace(self) -> None:
        self.widget._field_configs = [EfuseFieldConfig(" field ", " 0x1 ")]
        self.assertEqual(self.widget._capture_run_config().fields, (EfuseTarget("FIELD", "0x1"),))


if __name__ == "__main__":
    unittest.main()
