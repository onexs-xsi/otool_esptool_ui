from __future__ import annotations

import unittest

from src.main_window import JUMP_LIST_TASKS, terminal_mode_from_argv


class JumpListTaskTests(unittest.TestCase):
    def test_jump_list_keeps_only_primary_terminal_types(self) -> None:
        terminal_tasks = [item for item in JUMP_LIST_TASKS if item[0].startswith("终端 -")]

        self.assertEqual(
            terminal_tasks,
            [
                ("终端 - 标准终端", "--tab 4 --terminal-mode terminal"),
                ("终端 - Unix 终端", "--tab 4 --terminal-mode unix"),
            ],
        )

    def test_terminal_mode_launch_argument_is_validated(self) -> None:
        self.assertEqual(
            terminal_mode_from_argv(["app", "--tab", "4", "--terminal-mode", "unix"]),
            "unix",
        )
        self.assertIsNone(terminal_mode_from_argv(["app", "--terminal-mode", "bad"]))
        self.assertIsNone(terminal_mode_from_argv(["app", "--terminal-mode"]))
        self.assertIsNone(terminal_mode_from_argv(["app"]))


if __name__ == "__main__":
    unittest.main()
