from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent, QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QTextBrowser
import serial
from serial.tools.list_ports_common import ListPortInfo

from src.port_ownership import PortOwnershipRegistry
from src.terminal_widget import TerminalSerialConfig, TerminalSerialThread, TerminalWidget


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
            self._wait_for(lambda: not self.widget._scan_running)

    def _wait_for(self, ready) -> None:
        deadline = time.monotonic() + 3
        while not ready() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(ready())

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

    def test_settings_apply_live_to_the_same_open_pyserial_handle(self) -> None:
        w = self.widget
        w._port_combo.setEditText("COM_TEST")
        handle = serial.serial_for_url("loop://", baudrate=115200, timeout=0.03)
        with patch.object(TerminalSerialThread, "_open_serial", return_value=handle) as opened:
            w._connect_serial()
            session = w._active_session()
            self._wait_for(lambda: session.status == "已连接")
            worker, lease = session.serial_thread, session.port_lease
            self.assertFalse(w._port_combo.isEnabled())
            self.assertTrue(all(c.isEnabled() for c in (
                w._baud_combo, w._bytesize_combo, w._parity_combo, w._stopbits_combo, w._flow_combo)))
            w._baud_combo.setEditText("57601")
            self.assertIsNone(session.pending_config)
            self.assertEqual(handle.baudrate, 115200)
            w._baud_combo.lineEdit().editingFinished.emit()
            w._bytesize_combo.setCurrentText("7")
            w._parity_combo.setCurrentText("偶校验")
            w._stopbits_combo.setCurrentText("2")
            w._flow_combo.setCurrentText("XON/XOFF")
            wanted = TerminalSerialConfig("COM_TEST", 57601, 7, "E", 2, "xonxoff")
            self._wait_for(lambda: session.config == wanted and session.pending_config is None)
            for key, value in wanted.serial_settings().items():
                self.assertEqual(getattr(handle, key), value)
            self.assertIs(session.serial_thread, worker)
            self.assertIs(session.port_lease, lease)
            self.assertTrue(w._send_bytes(b"ping"))
            self._wait_for(lambda: any(r.direction == "RX" and r.payload.endswith(b"g") for r in session.records))
            with patch.object(handle, "apply_settings", side_effect=[ValueError("unsupported"), None]), \
                    patch("src.terminal_widget.QMessageBox.warning") as warning:
                w._parity_combo.setCurrentText("奇校验")
                self._wait_for(lambda: session.pending_config is None)
                self.assertEqual(session.config, wanted)
                self.assertEqual(w._parity_combo.currentData(), "E")
                warning.assert_called_once()
            opened.assert_called_once()
            self.assertTrue(handle.is_open)
            w.close_serial()
            self._wait_for(lambda: session.serial_thread is None)

    def test_periodic_port_scan_runs_off_ui_and_preserves_user_input(self) -> None:
        w = self.widget
        self.assertEqual(w._port_timer.interval(), 5000)
        combo = w._port_combo
        added = ListPortInfo("COM7")
        entered, release = threading.Event(), threading.Event()
        scan_threads = []

        def enumerate_ports():
            scan_threads.append(threading.get_ident())
            entered.set()
            release.wait(2)
            return [added]

        records = list(w._records)
        with patch("src.terminal_widget.list_ports.comports", side_effect=enumerate_ports):
            w._port_timer.timeout.emit()
            try:
                self.assertTrue(entered.wait(1))
                w._port_timer.timeout.emit()
            finally:
                release.set()
            self._wait_for(lambda: not w._scan_running)
        self.assertEqual(len(scan_threads), 1)
        self.assertNotEqual(scan_threads[0], threading.get_ident())
        self.assertGreaterEqual(combo.findData("COM7"), 0)
        self.assertEqual(w._records, records)
        w.show()
        combo.setEditText("custom name")
        combo.lineEdit().setSelection(2, 4)
        combo.setFocus()
        self._app.processEvents()
        w._update_port_list([added], announce=False)
        w._update_port_list([], announce=False)
        self.assertEqual(combo.lineEdit().selectedText(), "stom")
        self.assertEqual(combo.currentText(), "custom name")
        self.assertGreaterEqual(combo.findData("COM7"), 0)
        w._mode_combo.setFocus()
        w._update_port_list([], announce=False)
        self.assertEqual(combo.findData("COM7"), -1)
        self.assertEqual(combo.currentText(), "custom name")
        self.assertEqual(w._records, records)

    def test_term_type_field_and_caption_are_visible_only_in_unix(self) -> None:
        field = self.widget._term_type_combo.parentWidget()
        for mode in ("terminal", "unix", "plain", "unix"):
            self._select_mode(mode)
            self.assertEqual(field.isHidden(), mode != "unix")
            self.assertEqual(field.layout().itemAt(0).widget().text(), "终端类型")

    def test_unix_scrollback_stops_rendering_when_input_stops(self) -> None:
        self.widget.resize(1400, 1050)
        self.widget.show()
        self._select_mode("unix")
        log = self.widget._log
        received = b"boot message\r\n" * 180 + b"~ # "
        self.widget._serial_data_received(self.widget._active_session().session_id, received)
        # Deliver the pending single-shot render synchronously. Layout events
        # from this render must not enqueue another one without new input.
        self.assertTrue(self.widget._render_timer.isActive())
        self.widget._render_timer.stop()
        self.widget._render_log()
        self._app.processEvents()
        self.assertIn("~ #", log.toPlainText())
        self.assertGreater(log.verticalScrollBar().maximum(), 0)
        self.assertEqual(log.verticalScrollBar().value(), log.verticalScrollBar().maximum())
        self.assertFalse(self.widget._render_timer.isActive())

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

    def test_unix_typing_changes_only_the_echoed_row_without_scrolling(self) -> None:
        self.widget.resize(1200, 700)
        self.widget.show()
        self._select_mode("unix")
        sink = self._connect_sink()
        sid = self.widget._active_session().session_id
        self.widget._serial_data_received(sid, b"boot log\r\n" * 180 + b"~ # ")
        self.widget._render_timer.stop()
        self.widget._render_log()
        self._app.processEvents()
        log = self.widget._log
        first_editable_position = log.document().lastBlock().position()
        changes = []
        scrolls = []
        log.document().contentsChange.connect(lambda pos, removed, added: changes.append(pos))
        bar = log.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum())
        self.assertGreater(bar.value(), 0)
        bar.valueChanged.connect(scrolls.append)
        for key in ("a", "b"):
            QTest.keyClicks(log, key)
            self.assertEqual(sink.payloads[-1], key.encode())
            self.assertFalse(self.widget._render_timer.isActive())
            self.assertEqual(changes, [])
            self.widget._serial_data_received(sid, key.encode())
            self.widget._render_timer.stop()
            self.widget._render_log()
            self._app.processEvents()
            self.assertTrue(changes)
            self.assertGreaterEqual(min(changes), first_editable_position)
            self.assertEqual(scrolls, [])
            changes.clear()
        self.assertIn("~ # ab", log.toPlainText())

    def test_unix_incremental_edits_match_full_terminal_layout(self) -> None:
        self._select_mode("unix")
        session = self.widget._active_session()
        reference = QTextBrowser()
        reference.setFont(self.widget._log.font())
        for data in (b"boot\r\n" * 100, b"\x1b[31mword\x1b[0m", b"\b \b", b"\r\nnew",
                     b"\x1b[H\x1b[2J", "中文".encode(), b"\x1b[?25l", b"\x1b[?25h"):
            self.widget._serial_data_received(session.session_id, data)
            self.widget._render_log()
            reference.setHtml(self.widget._build_html_page(session.unix_terminal.render_html(), unix=True))
            self.assertEqual(self.widget._log.toHtml(), reference.toHtml())

    def test_ctrl_l_clears_stale_menu_and_scrollback_on_keyboard_and_button(self) -> None:
        self.widget.resize(1240, 420)
        self.widget.show()
        self._select_mode("unix")
        session = self.widget._active_session()
        sink = self._connect_sink()
        log = self.widget._log
        for use_button in (False, True):
            with self.subTest(use_button=use_button):
                self.widget._serial_data_received(session.session_id, b"old WiFi menu\r\n" * 65)
                self.widget._render_log()
                self.assertIn("old WiFi menu", log.toPlainText())
                # Include data queued before the key but not yet painted.
                self.widget._serial_data_received(session.session_id, b"old Overview")
                if use_button:
                    self.widget._unix_quick_buttons["Ctrl+L"].click()
                else:
                    QTest.keyClick(log, Qt.Key.Key_L, Qt.KeyboardModifier.ControlModifier)
                self.widget._render_log()
                self.assertEqual(sink.payloads, [b"\x0c"] * (1 + int(use_button)))
                self.assertEqual(log.toPlainText().strip(), "")
                self.assertFalse(session.unix_terminal.screen.history.top)
                self.widget._serial_data_received(session.session_id, b"\x1b[H\x1b[J~ # ")
                self.widget._render_log()
                self.assertEqual(log.toPlainText().strip(), "~ #")
                self.assertTrue(log.viewport().rect().intersects(log.cursorRect(QTextCursor(log.document()))))
                self.widget.resize(self.widget.width() - 80, self.widget.height())
                self._app.processEvents()
                self.widget._render_log()
                self.assertEqual(log.toPlainText().strip(), "~ #")
                self._select_mode("terminal")
                self.widget._render_log()
                self.assertIn("old WiFi menu", log.toPlainText())
                self._select_mode("unix")
                self.widget._render_log()
                self.assertEqual(log.toPlainText().strip(), "~ #")

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

    def test_device_term_commands_run_only_on_explicit_unix_shell_action(self) -> None:
        def term_menu():
            menu = self.widget._terminal_menu(self.widget._log)
            return next((a.menu() for a in menu.actions() if "TERM" in a.text()), None)

        for mode in ("terminal", "plain"):
            self._select_mode(mode)
            self.assertIsNone(term_menu())
        self._select_mode("unix")
        self.assertFalse(term_menu().isEnabled())
        sink = self._connect_sink()
        session = self.widget._active_session()
        self.widget._serial_opened(session.session_id, "connected")
        self.widget._serial_data_received(session.session_id, b"Error opening terminal: vt102.\r\n~ # ")
        menu = term_menu()
        self.assertTrue(menu.isEnabled())
        self.assertEqual(sink.payloads, [])
        menu.actions()[0].trigger()
        self.assertIn(b'infocmp', sink.payloads[-1])
        self.assertTrue(sink.payloads[-1].endswith(b'\r'))
        for action in menu.actions():
            if action.text().startswith("export TERM="):
                action.trigger()
                self.assertEqual(sink.payloads[-1], action.text().encode("ascii") + b"\r")

        select_vt100 = next(a for a in menu.actions() if a.text() == "export TERM=vt100")
        self.widget._newline_combo.setCurrentText("LF")
        select_vt100.trigger()
        self.assertEqual(sink.payloads[-1], b"export TERM=vt100\n")
        previous = sink.payloads.copy()
        self.widget._new_session()
        self._select_mode("unix")
        other_sink = self._connect_sink()
        self.widget._serial_opened(self.widget._active_session_id, "connected")
        select_vt100.trigger()
        self.assertEqual(other_sink.payloads, [])
        self.widget._switch_session(session.session_id)
        self.widget._serial_closed(session.session_id)
        new_connection = self._connect_sink()
        self.widget._serial_opened(session.session_id, "reconnected")
        select_vt100.trigger()
        self.assertEqual(sink.payloads, previous)
        self.assertEqual(new_connection.payloads, [])

    def test_term_selector_tracks_explicit_commands_per_open_connection(self) -> None:
        self.widget.show()
        self._select_mode("unix")
        combo = self.widget._term_type_combo
        first = self.widget._active_session()
        self.assertFalse(combo.isEnabled())
        sink = self._connect_sink()
        self.widget._serial_opened(first.session_id, "connected")
        self.assertTrue(combo.isEnabled())
        self.assertEqual(combo.currentIndex(), -1)
        combo.showPopup()
        index = combo.model().index(combo.findText("vt100"), 0)
        view = combo.view()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(index).center())
        self.assertEqual(sink.payloads, [b"export TERM=vt100\r"])
        self.assertEqual(first.last_sent_term, "vt100")

        self._select_mode("plain")
        self.assertFalse(combo.isEnabled())
        self.widget._new_session()
        self._select_mode("unix")
        self.assertEqual(combo.currentIndex(), -1)
        self.widget._switch_session(first.session_id)
        self._select_mode("unix")
        self.assertEqual(combo.currentText(), "vt100")
        self.assertEqual(sink.payloads, [b"export TERM=vt100\r"])
        self.widget._serial_error(first.session_id, "error")
        self.widget._serial_closed(first.session_id)
        self.assertFalse(combo.isEnabled())
        self.assertEqual(combo.currentIndex(), -1)
        new_sink = self._connect_sink()
        self.widget._serial_opened(first.session_id, "reconnected")
        self.assertEqual(combo.currentIndex(), -1)
        self.assertEqual(new_sink.payloads, [])

    def test_config_wraps_fields_and_keeps_trailing_actions_on_the_right(self) -> None:
        self.widget.show()
        self._select_mode("unix")
        config = self.widget._serial_config_widget
        port_row, settings_row = (config.layout().itemAt(i).layout() for i in range(2))
        fields = [row.itemAt(i).widget() for row in (port_row, settings_row) for i in range(row.count())]
        self.assertEqual(fields[:-1], [editor.parentWidget() for editor in (
            self.widget._mode_combo, self.widget._port_combo, self.widget._baud_combo,
            self.widget._bytesize_combo, self.widget._parity_combo, self.widget._stopbits_combo,
            self.widget._flow_combo, self.widget._term_type_combo,
        )])
        actions = fields[-1]
        self.assertEqual(
            [actions.layout().itemAt(i).widget() for i in range(actions.layout().count())],
            [self.widget._connect_btn, self.widget._listen_btn, self.widget._disconnect_btn, self.widget._reset_btn,
             self.widget._compact_btn, self.widget._fullscreen_btn],
        )
        for width in (920, 1360, 2600):
            self.widget.resize(width, 720)
            self._app.processEvents()
            rects = [field.geometry() for field in fields]
            self.assertEqual(rects[-1].right(), config.rect().right())
            self.assertLessEqual(rects[-1].bottom(), config.rect().bottom())
            for i, rect in enumerate(rects):
                self.assertTrue(config.rect().contains(rect))
                self.assertFalse(any(rect.intersects(other) for other in rects[i+1:]))
            self.assertEqual(rects[3].x(), rects[0].x())
            self.assertGreater(rects[3].top(), max(rect.bottom() for rect in rects[:3]))
            if width == 2600:
                self.assertEqual({rect.y() for rect in rects[3:]}, {rects[3].y()})

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
