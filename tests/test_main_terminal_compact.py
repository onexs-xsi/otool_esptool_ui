from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from src.main_window import OtoolEsptoolUI


class MainTerminalCompactTests(unittest.TestCase):
    _app: QApplication

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_compact_mode_hides_only_the_application_shell(self) -> None:
        with (
            patch("src.main_window.list_ports.comports", return_value=[]),
            patch("src.terminal_widget.list_ports.comports", return_value=[]),
        ):
            window = OtoolEsptoolUI()
            window.tab_switcher.set_current(4, animated=False)
            window.show()
            self._app.processEvents()

            terminal = window._terminal_widget
            terminal._compact_btn.setChecked(True)
            self._app.processEvents()

            self.assertTrue(window._toolbar.isHidden())
            self.assertTrue(window.floating_tab_panel.isHidden())
            self.assertTrue(window.floating_info_panel.isHidden())
            self.assertEqual(window.page_stack.currentIndex(), 4)
            self.assertEqual(window._terminal_page_layout.contentsMargins().bottom(), 0)
            self.assertFalse(terminal._serial_config_widget.isHidden())
            self.assertFalse(terminal._input_frame.isHidden())

            terminal._compact_btn.setChecked(False)
            self._app.processEvents()

            self.assertFalse(window._toolbar.isHidden())
            self.assertFalse(window.floating_tab_panel.isHidden())
            self.assertFalse(window.floating_info_panel.isHidden())
            self.assertEqual(window._terminal_page_layout.contentsMargins().bottom(), 70)

        window.close()
        window.deleteLater()
        self._app.processEvents()


if __name__ == "__main__":
    unittest.main()
