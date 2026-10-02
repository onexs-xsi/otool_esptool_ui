from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from src.port_ownership import PortOwnershipRegistry
from src.verify_plan import VerifyPattern, VerifyProfile, VerifyStep, parse_verify_profile
from src.verify_visual_editor import _escape_control_text, _unescape_control_text
from src.verify_widget import VerifyTaskItem, VerifyTaskState, VerifyWidget, VerifyWorker


class VerifyPlanReviewTests(unittest.TestCase):
    def test_quoted_false_does_not_enable_serial_newline_or_case_sensitive_match(self) -> None:
        profile = parse_verify_profile("test", {
            "steps": [{"action": "send_text", "text": "status", "append_newline": "false"},
                      {"action": "expect", "patterns": ["ready"], "case_sensitive": "false"}],
        })
        self.assertFalse(profile.steps[0].append_newline)
        self.assertFalse(profile.steps[1].case_sensitive)

    def test_invalid_profile_fails_before_opening_hardware(self) -> None:
        cases = [
            {"steps": 3},
            {"steps": [{"action": "wait", "duration_ms": 2.5}]},
            {"steps": [{"action": "wait", "duration_ms": True}]},
            {"steps": [{"action": "expect", "patterns": ["("], "match_type": "regex"}]},
            {"serial": {"encoding": "not-an-encoding"}, "steps": [{"action": "pass"}]},
            {"steps": [{"action": "send_text", "text": "status", "append_newline": "typo"}]},
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_verify_profile("test", raw)

    def test_control_text_round_trip_preserves_literal_backslashes(self) -> None:
        for text in [r"\n", r"C:\new\test", "\\\r\n", "\r\n\t", r"\\n", "中文\\n"]:
            with self.subTest(text=text):
                self.assertEqual(_unescape_control_text(_escape_control_text(text)), text)


class VerifyWorkerReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.worker = VerifyWorker("TEST-PORT", VerifyProfile("test", steps=[VerifyStep("pass")]))

    def test_serial_decode_handles_split_unicode_and_split_crlf(self) -> None:
        port = Mock()
        self.worker._serial = port
        for chunk in [b"A\xe4", b"\xb8", b"\xad\r", b"\nB"]:
            port.in_waiting = len(chunk)
            port.read.return_value = chunk
            self.assertTrue(self.worker._read_once())
        self.assertEqual(self.worker._buffer, "A中\nB")

    def test_idle_serial_does_not_block_on_configured_read_timeout(self) -> None:
        port = Mock(in_waiting=0)
        self.worker._serial = port
        self.assertFalse(self.worker._read_once())
        port.read.assert_not_called()

    def test_clear_buffer_discards_partial_decoder_sequence(self) -> None:
        port = Mock(in_waiting=1)
        self.worker._serial = port
        port.read.return_value = b"\xe4"
        self.worker._read_once()
        self.worker._clear_buffer(None)
        port.in_waiting = 2
        port.read.return_value = b"OK"
        self.worker._read_once()
        self.assertEqual(self.worker._buffer, "OK")

    def test_unterminated_serial_line_stays_bounded(self) -> None:
        self.worker._profile.serial.max_buffer_chars = 256
        self.worker._append_serial_text("x" * 600)
        self.assertEqual(len(self.worker._buffer), 256)
        self.assertLessEqual(len(self.worker._line_buffer), 256)

    def test_forbidden_buffer_is_checked_even_when_deadline_has_elapsed(self) -> None:
        self.worker._buffer = "panic"
        step = VerifyStep("expect", match_mode="none", timeout_ms=1, patterns=[VerifyPattern("panic")])
        with patch("src.verify_widget.time.monotonic", side_effect=[0, 1]), self.assertRaisesRegex(RuntimeError, "命中禁止字段"):
            self.worker._run_expect(step)

    def test_existing_positive_match_does_not_wait_for_more_serial_data(self) -> None:
        self.worker._buffer = "ready"
        step = VerifyStep("expect", timeout_ms=1, patterns=[VerifyPattern("ready")])
        with patch.object(self.worker, "_pump_serial") as pump:
            self.worker._run_expect(step)
        pump.assert_not_called()

    def test_capture_timeout_reports_missing_name_when_no_time_remains(self) -> None:
        step = VerifyStep("capture", timeout_ms=1, patterns=[VerifyPattern("ready", name="status")])
        with patch("src.verify_widget.time.monotonic", side_effect=[0, 1]), self.assertRaisesRegex(RuntimeError, "status"):
            self.worker._run_capture(step)

    def test_unmatched_optional_capture_is_missing_instead_of_string_none(self) -> None:
        self.worker._buffer = "OK"
        step = VerifyStep("capture", match_type="regex")
        for pattern in [VerifyPattern(r"OK(?:=(\w+))?", name="status"),
                        VerifyPattern(r"OK(?:=(?P<status>\w+))?", name="status")]:
            with self.subTest(pattern=pattern):
                self.assertIsNone(self.worker._extract_pattern_value(step, pattern))

    def test_capture_all_counts_patterns_when_parameter_names_are_shared(self) -> None:
        self.worker._buffer = "ready1 ready2"
        step = VerifyStep("capture", patterns=[
            VerifyPattern("ready1", name="status"), VerifyPattern("ready2", name="status"),
        ])
        with patch.object(self.worker, "_pump_serial") as pump:
            self.worker._run_capture(step)
        pump.assert_not_called()
        self.assertEqual(self.worker._captured_values, {"status": "ready2"})

    def test_wait_silence_succeeds_when_duration_equals_timeout(self) -> None:
        step = VerifyStep("wait_silence", duration_ms=1000, timeout_ms=1000)
        with patch("src.verify_widget.time.monotonic", side_effect=[0, 0, 1]):
            self.worker._run_wait_silence(step)

    def test_finish_signal_is_sent_after_serial_is_closed(self) -> None:
        closed = []
        observations = []
        self.worker.finished.connect(lambda outcome, _: observations.append((outcome, bool(closed))))
        with patch.object(self.worker, "_open_serial"), patch.object(self.worker, "_close_serial", side_effect=lambda: closed.append(True)):
            self.worker.run()
        self.assertEqual(observations, [("passed", True)])


class VerifyWidgetReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.widget = VerifyWidget(port_registry=PortOwnershipRegistry())

    def tearDown(self) -> None:
        self.widget._tasks.clear()
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def test_stop_selected_cancels_waiting_task_in_auto_mode(self) -> None:
        task = VerifyTaskItem("TEST", "TEST", selected=True)
        self.widget._tasks = [task]
        self.widget._auto_mode = True
        self.widget._stop_selected_tasks()
        with patch.object(self.widget, "_start_task") as start:
            self.widget._schedule_next()
        self.assertEqual(task.state, VerifyTaskState.STOPPED)
        start.assert_not_called()

    def test_auto_poll_without_pending_work_does_not_rewrite_editors(self) -> None:
        self.widget._auto_mode = True
        with patch.object(self.widget, "_validate_current_profile") as validate:
            self.widget._schedule_next()
        validate.assert_not_called()

    def test_task_cannot_restart_while_previous_thread_is_cleaning_up(self) -> None:
        task = VerifyTaskItem("TEST", "TEST", state=VerifyTaskState.PASSED, thread=Mock())
        with patch.object(self.widget, "_validate_current_profile") as validate:
            self.widget._start_task(task)
        validate.assert_not_called()

    def test_clear_done_retains_threads_until_cleanup(self) -> None:
        task = VerifyTaskItem("TEST", "TEST", state=VerifyTaskState.PASSED, thread=Mock())
        self.widget._tasks = [task]
        self.widget._clear_done()
        self.assertEqual(self.widget._tasks, [task])

    def test_background_validation_of_empty_yaml_does_not_open_modal(self) -> None:
        self.widget._editor_tabs.setCurrentIndex(1)
        self.widget._plan_edit.clear()
        with patch("src.verify_widget.QMessageBox.warning") as warning:
            self.assertIsNone(self.widget._validate_current_profile(show_message=False))
        warning.assert_not_called()

    def test_worker_thread_can_finish_while_gui_is_waiting(self) -> None:
        task = VerifyTaskItem("TEST", "TEST")
        self.widget._tasks = [task]
        profile = VerifyProfile("test", steps=[VerifyStep("pass")])
        with patch.object(VerifyWorker, "_open_serial"), patch.object(VerifyWorker, "_close_serial"):
            self.widget._start_task(task, profile)
            thread = task.thread
            try:
                self.assertTrue(thread.wait(1500), "worker quit must not depend on the blocked GUI event loop")
            finally:
                thread.quit()
                thread.wait(1500)
                self.app.processEvents()
        self.assertTrue(self.widget._port_registry.is_available("TEST"))


if __name__ == "__main__":
    unittest.main()
