from __future__ import annotations

import unittest

from PyQt6.QtCore import Qt

from src.unix_terminal import UnixTerminalEmulator, encode_unix_key, normalize_pasted_text


class UnixTerminalEmulatorTests(unittest.TestCase):
    def test_top_refresh_sequences_replace_the_visible_screen(self) -> None:
        emulator = UnixTerminalEmulator(columns=80, lines=24)
        first_frame = (
            b"CPU: 50.0% sys 50.0% idle\r\n"
            b"Load average: 1.00 0.50 0.25\r\n"
            b"\x1b[7m  PID USER     COMMAND\x1b[0m\r\n"
            b"  100 root     old-top"
        )
        second_frame = (
            b"\x1b[H\x1b[J"
            b"Mem: 12368K used, 13380K free\r\n"
            b"CPU: 100% idle\r\n"
            b"Load average: 0.00 0.00 0.00\r\n"
            b"\x1b[7m  PID  PPID USER     COMMAND\x1b[0m\r\n"
            b"  842   839 root     top"
        )

        emulator.sync(((1, first_frame), (2, second_frame)), "utf-8")

        lines = emulator.screen.display
        self.assertTrue(lines[0].startswith("Mem: 12368K used"))
        self.assertTrue(lines[3].startswith("  PID  PPID USER"))
        self.assertTrue(lines[4].startswith("  842   839 root"))
        self.assertNotIn("old-top", emulator.visible_text())
        self.assertNotIn("\x1b", emulator.visible_text())
        self.assertTrue(emulator.screen.buffer[3][0].reverse)

    def test_split_escape_and_utf8_sequences_are_parsed_incrementally(self) -> None:
        emulator = UnixTerminalEmulator(columns=20, lines=4)

        emulator.sync(
            (
                (1, b"stale"),
                (2, b"\x1b["),
                (3, b"2J\x1b[H\xe4"),
                (4, b"\xb8\xad"),
            ),
            "utf-8",
        )

        self.assertTrue(emulator.screen.display[0].startswith("中"))
        self.assertNotIn("stale", emulator.visible_text())

    def test_render_html_preserves_reverse_video_and_cursor(self) -> None:
        emulator = UnixTerminalEmulator(columns=10, lines=2)
        emulator.sync(((1, b"\x1b[7mHEAD\x1b[0m"),), "utf-8")

        html = emulator.render_html(show_cursor=True)

        self.assertIn("HEAD", html)
        self.assertIn("background-color:#d1d5db", html)
        self.assertIn("outline:1px solid #e5e7eb", html)


class UnixTerminalKeyTests(unittest.TestCase):
    def test_printable_control_and_navigation_keys_use_terminal_sequences(self) -> None:
        none = Qt.KeyboardModifier.NoModifier
        control = Qt.KeyboardModifier.ControlModifier
        control_shift = control | Qt.KeyboardModifier.ShiftModifier

        self.assertEqual(encode_unix_key(Qt.Key.Key_A, "a", none, "utf-8", "\r\n"), b"a")
        self.assertEqual(encode_unix_key(Qt.Key.Key_C, "c", control, "utf-8", "\r\n"), b"\x03")
        self.assertEqual(encode_unix_key(Qt.Key.Key_Up, "", none, "utf-8", "\r\n"), b"\x1b[A")
        self.assertEqual(
            encode_unix_key(Qt.Key.Key_Up, "", control_shift, "utf-8", "\r\n"),
            b"\x1b[1;6A",
        )
        self.assertEqual(encode_unix_key(Qt.Key.Key_F5, "", none, "utf-8", "\r\n"), b"\x1b[15~")
        self.assertEqual(encode_unix_key(Qt.Key.Key_Return, "", none, "utf-8", "\n"), b"\n")

    def test_paste_uses_selected_newline(self) -> None:
        self.assertEqual(normalize_pasted_text("a\r\nb\rc\n", "\r\n"), "a\r\nb\r\nc\r\n")


if __name__ == "__main__":
    unittest.main()
