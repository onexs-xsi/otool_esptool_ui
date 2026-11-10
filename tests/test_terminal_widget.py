from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent, QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from src.port_ownership import PortOwnershipRegistry
from src.terminal_widget import TerminalWidget


class _SerialSink:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.payloads.append(data)

    def request_stop(self) -> None:
        pass

    def wait(self, _wait_ms: int) -> bool:
        return True


class TerminalWidgetTests(unittest.TestCase):
    _app: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        with patch("src.terminal_widget.list_ports.comports", return_value=[]):
            self.widget = TerminalWidget(port_registry=PortOwnershipRegistry())
            self._app.processEvents()

    def tearDown(self) -> None:
        self.widget.close()
        self.widget.deleteLater()
        self._app.processEvents()

    def _select_mode(self, mode: str) -> None:
        index = self.widget._mode_combo.findData(mode)
        self.assertGreaterEqual(index, 0)
        self.widget._mode_combo.setCurrentIndex(index)
        self._app.processEvents()

    def _connect_sink(self) -> _SerialSink:
        sink = _SerialSink()
        session = self.widget._active_session()
        self.assertIsNotNone(session)
        session.serial_thread = sink  # type: ignore[assignment]
        return sink

    def _press_enter(self, key=Qt.Key.Key_Return) -> None:
        QApplication.sendEvent(
            self.widget._log,
            QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier),
        )

    def test_unix_default_enter_sends_one_cr_for_return_and_keypad_enter(self) -> None:
        self._select_mode("unix")
        sink = self._connect_sink()
        self._press_enter()
        self._press_enter(Qt.Key.Key_Enter)
        self.assertEqual(sink.payloads, [b"\r", b"\r"])
        self.assertEqual(self.widget._newline_combo.currentText(), "自动")

    def test_unix_viewport_resize_reflows_existing_log_without_new_input(self) -> None:
        self.widget.resize(1200, 700)
        self.widget.show()
        self._select_mode("unix")
        terminal = self.widget._active_session().unix_terminal
        self.widget._render_log()
        narrow = terminal.columns
        message = "x" * (narrow + 5)
        self.widget._append_record("RX", message.encode() + b"\r\nEND")
        QTest.qWait(120)
        self.assertNotIn(message, self.widget._log.toPlainText())

        self.widget.resize(2400, 700)
        QTest.qWait(120)
        self.assertGreater(terminal.columns, max(80, len(message)))
        self.assertIn(message + "\nEND", self.widget._log.toPlainText())
        log = self.widget._log
        self.assertEqual(log.horizontalScrollBar().maximum(), 0)

        self.widget._fullscreen_btn.setChecked(True)
        QTest.qWait(120)
        self.assertTrue(self.widget.isFullScreen())
        self.assertEqual(log.horizontalScrollBar().maximum(), 0)
        self.widget._fullscreen_btn.setChecked(False)
        self.widget.resize(1200, 700)
        QTest.qWait(120)
        self.assertEqual(terminal.columns, narrow)
        self.assertIn("xxxxx\nEND", log.toPlainText())

    def test_log_menu_and_shortcuts_paste_to_the_active_destination(self) -> None:
        for mode in ("terminal", "plain", "unix"):
            with self.subTest(mode=mode):
                self._select_mode(mode)
                clipboard = QApplication.clipboard()
                clipboard.setText("first\nsecond")
                menu = self.widget._terminal_menu(self.widget._log)
                paste = next(action for action in menu.actions() if action.text().startswith("粘贴"))
                if mode == "unix":
                    self.assertFalse(paste.isEnabled())
                    sink = self._connect_sink()
                    menu = self.widget._terminal_menu(self.widget._log)
                    paste = next(action for action in menu.actions() if action.text().startswith("粘贴"))
                    self.assertTrue(paste.isEnabled())
                    paste.trigger()
                    self.assertEqual(sink.payloads, [b"first\rsecond"])
                else:
                    self.assertTrue(paste.isEnabled())
                    paste.trigger()
                    editor = self.widget._terminal_input if mode == "terminal" else self.widget._plain_input
                    self.assertEqual(editor.toPlainText(), "first\nsecond")
                    editor.selectAll()
                    actions = self.widget._terminal_menu(editor).actions()
                    next(action for action in actions if action.text().startswith("剪切")).trigger()
                    self.assertEqual(editor.toPlainText(), "")
                    QTest.keyClick(self.widget._log, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
                    self.assertEqual(editor.toPlainText(), "first\nsecond")

    def test_live_output_keeps_selection_copyable_in_all_modes(self) -> None:
        for mode in ("terminal", "plain", "unix"):
            with self.subTest(mode=mode):
                self._select_mode(mode)
                self.widget._clear_log()
                self.widget._append_record("RX", b"selected\r\n")
                self.widget._render_log()
                log = self.widget._log
                cursor = log.document().find("selected")
                self.assertTrue(cursor.hasSelection())
                log.setTextCursor(cursor)
                self.widget._append_record("RX", b"new output\r\n")
                self.widget._render_log()
                self._app.processEvents()
                copy = next(a for a in self.widget._terminal_menu(log).actions() if a.text().startswith("复制选中"))
                self.assertTrue(copy.isEnabled())
                copy.trigger()
                self.assertEqual(QApplication.clipboard().text(), "selected")
                log.moveCursor(QTextCursor.MoveOperation.Start)

    def test_standard_and_text_modes_default_to_crlf(self) -> None:
        sink = self._connect_sink()
        self.widget._terminal_input.setPlainText("status")
        self.widget._send_terminal_line()
        self._select_mode("plain")
        self.widget._plain_input.setPlainText("version")
        self.widget._send_plain_text()
        self.assertEqual(sink.payloads, [b"status\r\n", b"version\r\n"])
        self.assertEqual(self.widget._newline_combo.currentText(), "CRLF")

    def test_manual_newlines_are_remembered_separately_for_each_mode(self) -> None:
        sink = self._connect_sink()
        self.widget._newline_combo.setCurrentText("LF")
        self._select_mode("unix")
        self.widget._newline_combo.setCurrentText("CRLF")
        self._select_mode("terminal")
        self.widget._terminal_input.setPlainText("status")
        self.widget._send_terminal_line()
        self._select_mode("unix")
        self._press_enter()
        self.assertEqual(sink.payloads, [b"status\n", b"\r\n"])

    def test_auto_selection_survives_mode_round_trip(self) -> None:
        self._select_mode("unix")
        sink = self._connect_sink()
        self.widget._newline_combo.setCurrentText("自动")
        self._select_mode("terminal")
        self.widget._terminal_input.setPlainText("status")
        self.widget._send_terminal_line()
        self._select_mode("unix")
        self._press_enter()
        self.assertEqual(sink.payloads, [b"status\r\n", b"\r"])
        self.assertEqual(self.widget._newline_combo.currentText(), "自动")

    def test_new_sessions_keep_independent_unix_newline_preferences(self) -> None:
        self._select_mode("unix")
        first_id = self.widget._active_session_id
        first_sink = self._connect_sink()
        self.widget._newline_combo.setCurrentText("LF")
        self.widget._new_session()
        self._select_mode("unix")
        second_sink = self._connect_sink()
        self._press_enter()
        self.widget._switch_session(first_id)
        self._press_enter()
        self.assertEqual(first_sink.payloads, [b"\n"])
        self.assertEqual(second_sink.payloads, [b"\r"])

    def test_unix_explicit_newline_choices_override_auto(self) -> None:
        self._select_mode("unix")
        sink = self._connect_sink()
        for label in ("LF", "CRLF", "CR", "不追加"):
            self.widget._newline_combo.setCurrentText(label)
            self._press_enter()
        self.assertEqual(sink.payloads, [b"\n", b"\r\n", b"\r"])

    def test_unix_mode_hides_editor_but_keeps_send_settings(self) -> None:
        self._select_mode("unix")

        self.assertEqual(self.widget._mode_combo.currentText(), "Unix 终端")
        self.assertTrue(self.widget._input_stack.isHidden())
        self.assertFalse(self.widget._encoding_combo.isHidden())
        self.assertFalse(self.widget._newline_combo.isHidden())
        self.assertFalse(self.widget._unix_input_hint.isHidden())
        self.assertEqual(self.widget._input_title.text(), "输入设置")
        self.assertFalse(self.widget._timestamp_check.isEnabled())
        self.assertFalse(self.widget._hex_check.isEnabled())
        self.assertEqual(
            list(self.widget._unix_quick_buttons),
            ["Ctrl+V", "Tab", "↑", "↓", "Ctrl+L", "Esc"],
        )
        self.assertNotIn("Ctrl+C", self.widget._unix_quick_buttons)
        self.assertTrue(
            all(not button.isHidden() for button in self.widget._unix_quick_buttons.values())
        )

        self._select_mode("terminal")
        self.assertFalse(self.widget._input_stack.isHidden())
        self.assertTrue(self.widget._unix_input_hint.isHidden())
        self.assertEqual(self.widget._input_title.text(), "发送")
        self.assertTrue(self.widget._timestamp_check.isEnabled())
        self.assertTrue(self.widget._hex_check.isEnabled())
        self.assertTrue(
            all(button.isHidden() for button in self.widget._unix_quick_buttons.values())
        )

    def test_unix_input_sends_each_key_with_selected_encoding_and_newline(self) -> None:
        self._select_mode("unix")
        self.widget._encoding_combo.setCurrentText("latin-1")
        newline_index = self.widget._newline_combo.findData("\n")
        self.widget._newline_combo.setCurrentIndex(newline_index)

        sink = self._connect_sink()

        text_event = QKeyEvent(
            QEvent.Type.KeyPress,
            0,
            Qt.KeyboardModifier.NoModifier,
            "é",
        )
        enter_event = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Return,
            Qt.KeyboardModifier.NoModifier,
        )

        self.assertTrue(self.widget._handle_unix_terminal_key(text_event))
        self.assertTrue(self.widget._handle_unix_terminal_key(enter_event))
        self.assertEqual(sink.payloads, [b"\xe9", b"\n"])
        self.assertEqual(self.widget._records[-1].direction, "TX")
        self.assertEqual(self.widget._records[-1].payload, b"\n")

    def test_unix_quick_buttons_send_expected_sequences_without_ctrl_c(self) -> None:
        self._select_mode("unix")
        sink = self._connect_sink()
        QApplication.clipboard().setText("first\r\nsecond\nthird\rfourth")

        for label in ("Ctrl+V", "Tab", "↑", "↓", "Ctrl+L", "Esc"):
            self.widget._unix_quick_buttons[label].click()

        self.assertEqual(
            sink.payloads,
            [
                b"first\rsecond\rthird\rfourth",
                b"\t",
                b"\x1b[A",
                b"\x1b[B",
                b"\x0c",
                b"\x1b",
            ],
        )

    def test_keyboard_ctrl_v_pastes_and_ctrl_c_sends_interrupt(self) -> None:
        self._select_mode("unix")
        sink = self._connect_sink()
        QApplication.clipboard().setText("echo 1\necho 2")
        control = Qt.KeyboardModifier.ControlModifier
        paste_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V, control, "\x16")
        interrupt_event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C, control, "\x03")

        self.assertTrue(self.widget._handle_unix_terminal_key(paste_event))
        self.assertTrue(self.widget._handle_unix_terminal_key(interrupt_event))

        self.assertEqual(sink.payloads, [b"echo 1\recho 2", b"\x03"])

    def test_standard_terminal_does_not_capture_unix_keys(self) -> None:
        self._select_mode("terminal")
        text_event = QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_A,
            Qt.KeyboardModifier.NoModifier,
            "a",
        )

        self.assertFalse(self.widget._handle_unix_terminal_key(text_event))

    def test_compact_and_fullscreen_modes_are_independent(self) -> None:
        self.widget.show()
        self._app.processEvents()
        compact_states: list[bool] = []
        self.widget.compactModeChanged.connect(compact_states.append)

        self.widget._compact_btn.setChecked(True)
        self.assertFalse(self.widget._serial_config_widget.isHidden())
        self.assertFalse(self.widget._input_frame.isHidden())
        self.assertEqual(compact_states, [True])

        self.widget._fullscreen_btn.setChecked(True)
        self._app.processEvents()
        self.assertTrue(self.widget.window().isFullScreen())
        self.assertTrue(self.widget._compact_btn.isChecked())

        self.widget._compact_btn.setChecked(False)
        self.assertFalse(self.widget._serial_config_widget.isHidden())
        self.assertFalse(self.widget._input_frame.isHidden())
        self.assertTrue(self.widget.window().isFullScreen())
        self.assertEqual(compact_states, [True, False])

        self.widget._fullscreen_btn.setChecked(False)
        self._app.processEvents()
        self.assertFalse(self.widget.window().isFullScreen())

    def test_terminal_mode_can_be_selected_from_launch_request(self) -> None:
        self.assertTrue(self.widget.set_mode("unix"))
        self.assertEqual(self.widget._mode_combo.currentData(), "unix")
        self.assertTrue(self.widget.set_mode("plain"))
        self.assertEqual(self.widget._mode_combo.currentData(), "plain")
        self.assertFalse(self.widget.set_mode("unsupported"))


if __name__ == "__main__":
    unittest.main()
