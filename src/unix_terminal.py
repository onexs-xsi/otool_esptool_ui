from __future__ import annotations

import codecs
from collections.abc import Iterable, Mapping
from html import escape

import pyte
from PyQt6.QtCore import Qt


DEFAULT_COLUMNS = 80
DEFAULT_LINES = 24
DEFAULT_HISTORY_LINES = 1000


_ANSI_COLORS = {
    "black": "#111827",
    "red": "#f87171",
    "green": "#86efac",
    "brown": "#facc15",
    "blue": "#60a5fa",
    "magenta": "#c084fc",
    "cyan": "#67e8f9",
    "white": "#d1d5db",
    "brightblack": "#6b7280",
    "brightred": "#fca5a5",
    "brightgreen": "#bbf7d0",
    "brightbrown": "#fde68a",
    "brightblue": "#93c5fd",
    "brightmagenta": "#d8b4fe",
    "brightcyan": "#a5f3fc",
    "brightwhite": "#f9fafb",
}

_DEFAULT_FOREGROUND = "#d1d5db"
_DEFAULT_BACKGROUND = "#111827"


_CURSOR_KEYS = {
    int(Qt.Key.Key_Up): "A",
    int(Qt.Key.Key_Down): "B",
    int(Qt.Key.Key_Right): "C",
    int(Qt.Key.Key_Left): "D",
    int(Qt.Key.Key_Home): "H",
    int(Qt.Key.Key_End): "F",
}

_TILDE_KEYS = {
    int(Qt.Key.Key_Insert): 2,
    int(Qt.Key.Key_Delete): 3,
    int(Qt.Key.Key_PageUp): 5,
    int(Qt.Key.Key_PageDown): 6,
}

_FUNCTION_KEYS = {
    int(Qt.Key.Key_F1): "P",
    int(Qt.Key.Key_F2): "Q",
    int(Qt.Key.Key_F3): "R",
    int(Qt.Key.Key_F4): "S",
}

_FUNCTION_TILDE_KEYS = {
    int(Qt.Key.Key_F5): 15,
    int(Qt.Key.Key_F6): 17,
    int(Qt.Key.Key_F7): 18,
    int(Qt.Key.Key_F8): 19,
    int(Qt.Key.Key_F9): 20,
    int(Qt.Key.Key_F10): 21,
    int(Qt.Key.Key_F11): 23,
    int(Qt.Key.Key_F12): 24,
}


def encode_unix_key(
    key: int,
    text: str,
    modifiers: Qt.KeyboardModifier,
    encoding: str,
    newline: str,
) -> bytes | None:
    """Translate a Qt key press into an xterm-compatible byte sequence."""

    key_value = int(key)
    shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
    alt = bool(
        modifiers
        & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
    )
    control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)

    if control:
        control_byte = _control_key_byte(key_value, text)
        if control_byte is not None:
            return (b"\x1b" if alt else b"") + bytes([control_byte])

    modifier_code = 1 + int(shift) + (int(alt) * 2) + (int(control) * 4)

    if key_value in _CURSOR_KEYS:
        suffix = _CURSOR_KEYS[key_value]
        if modifier_code == 1:
            return f"\x1b[{suffix}".encode("ascii")
        return f"\x1b[1;{modifier_code}{suffix}".encode("ascii")

    if key_value in _TILDE_KEYS:
        number = _TILDE_KEYS[key_value]
        if modifier_code == 1:
            return f"\x1b[{number}~".encode("ascii")
        return f"\x1b[{number};{modifier_code}~".encode("ascii")

    if key_value in _FUNCTION_KEYS:
        suffix = _FUNCTION_KEYS[key_value]
        if modifier_code == 1:
            return f"\x1bO{suffix}".encode("ascii")
        return f"\x1b[1;{modifier_code}{suffix}".encode("ascii")

    if key_value in _FUNCTION_TILDE_KEYS:
        number = _FUNCTION_TILDE_KEYS[key_value]
        if modifier_code == 1:
            return f"\x1b[{number}~".encode("ascii")
        return f"\x1b[{number};{modifier_code}~".encode("ascii")

    if key_value in (int(Qt.Key.Key_Return), int(Qt.Key.Key_Enter)):
        return newline.encode(encoding)
    if key_value == int(Qt.Key.Key_Backspace):
        return b"\x7f"
    if key_value == int(Qt.Key.Key_Tab):
        return b"\x1b[Z" if shift else b"\t"
    if key_value == int(Qt.Key.Key_Escape):
        return b"\x1b"

    if text and not control:
        payload = text.encode(encoding)
        return (b"\x1b" if alt else b"") + payload
    return None


def normalize_pasted_text(text: str, newline: str) -> str:
    """Apply the selected terminal newline convention to pasted text."""

    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)


def _control_key_byte(key: int, text: str) -> int | None:
    if int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z):
        return key - int(Qt.Key.Key_A) + 1
    if key in (int(Qt.Key.Key_Space), int(Qt.Key.Key_At), int(Qt.Key.Key_2)):
        return 0x00
    if key in (int(Qt.Key.Key_BracketLeft), int(Qt.Key.Key_3)):
        return 0x1B
    if key in (int(Qt.Key.Key_Backslash), int(Qt.Key.Key_4)):
        return 0x1C
    if key in (int(Qt.Key.Key_BracketRight), int(Qt.Key.Key_5)):
        return 0x1D
    if key in (int(Qt.Key.Key_AsciiCircum), int(Qt.Key.Key_6)):
        return 0x1E
    if key in (int(Qt.Key.Key_Underscore), int(Qt.Key.Key_Minus), int(Qt.Key.Key_7)):
        return 0x1F
    if key in (int(Qt.Key.Key_Question), int(Qt.Key.Key_8)) or text == "?":
        return 0x7F
    return None


class UnixTerminalEmulator:
    """Stateful VTXXX screen backed by serial RX log records."""

    def __init__(
        self,
        columns: int = DEFAULT_COLUMNS,
        lines: int = DEFAULT_LINES,
        history_lines: int = DEFAULT_HISTORY_LINES,
    ) -> None:
        self.columns = columns
        self.lines = lines
        self.history_lines = history_lines
        self._encoding = ""
        self._processed_record_ids: tuple[int, ...] = ()
        self._reset_screen("utf-8")

    @property
    def screen(self) -> pyte.HistoryScreen:
        return self._screen

    def reset(self) -> None:
        self._reset_screen(self._encoding or "utf-8")

    def sync(self, records: Iterable[tuple[int, bytes]], encoding: str) -> None:
        entries = tuple(records)
        record_ids = tuple(record_id for record_id, _payload in entries)
        normalized_encoding = encoding.strip() or "utf-8"
        prefix_matches = (
            len(record_ids) >= len(self._processed_record_ids)
            and record_ids[: len(self._processed_record_ids)]
            == self._processed_record_ids
        )
        if normalized_encoding != self._encoding or not prefix_matches:
            self._reset_screen(normalized_encoding)

        start = len(self._processed_record_ids)
        for _record_id, payload in entries[start:]:
            decoded = self._decoder.decode(payload, final=False)
            if decoded:
                self._stream.feed(decoded)
        self._processed_record_ids = record_ids

    def visible_text(self) -> str:
        return "\n".join(self._screen.display)

    def render_html(self, show_cursor: bool = True) -> str:
        history = list(self._screen.history.top)
        screen_lines = [self._screen.buffer[row] for row in range(self._screen.lines)]
        cursor_row = len(history) + self._screen.cursor.y
        rendered: list[str] = []
        for row, line in enumerate([*history, *screen_lines]):
            rendered.append(
                self._render_line(
                    line,
                    cursor_column=self._screen.cursor.x if row == cursor_row else None,
                    show_cursor=show_cursor and not self._screen.cursor.hidden,
                )
            )
        return "\n".join(rendered)

    def _reset_screen(self, encoding: str) -> None:
        try:
            decoder_factory = codecs.getincrementaldecoder(encoding)
        except LookupError:
            encoding = "utf-8"
            decoder_factory = codecs.getincrementaldecoder(encoding)
        self._encoding = encoding
        self._processed_record_ids = ()
        self._decoder = decoder_factory(errors="replace")
        self._screen = pyte.HistoryScreen(
            self.columns,
            self.lines,
            history=self.history_lines,
        )
        self._stream = pyte.Stream(self._screen, strict=False)

    def _render_line(
        self,
        line: Mapping[int, pyte.screens.Char],
        cursor_column: int | None,
        show_cursor: bool,
    ) -> str:
        groups: list[str] = []
        current_style: tuple[object, ...] | None = None
        current_text: list[str] = []

        def flush() -> None:
            if not current_text or current_style is None:
                return
            text = escape("".join(current_text))
            css = self._style_css(current_style)
            groups.append(f'<span style="{css}">{text}</span>' if css else text)

        for column in range(self._screen.columns):
            char = line[column]
            cursor = show_cursor and cursor_column == column
            style = (
                char.fg,
                char.bg,
                char.bold,
                char.italics,
                char.underscore,
                char.strikethrough,
                char.reverse,
                char.blink,
                cursor,
            )
            if current_style is not None and style != current_style:
                flush()
                current_text = []
            current_style = style
            current_text.append(char.data or "")
        flush()
        return "".join(groups)

    def _style_css(self, style: tuple[object, ...]) -> str:
        fg_name, bg_name, bold, italics, underline, strike, reverse, blink, cursor = style
        foreground = self._resolve_color(str(fg_name), _DEFAULT_FOREGROUND)
        background = self._resolve_color(str(bg_name), _DEFAULT_BACKGROUND)
        if reverse:
            foreground, background = background, foreground
        if cursor:
            foreground, background = background, foreground

        declarations: list[str] = []
        if foreground != _DEFAULT_FOREGROUND or reverse or cursor:
            declarations.append(f"color:{foreground}")
        if background != _DEFAULT_BACKGROUND or reverse or cursor:
            declarations.append(f"background-color:{background}")
        if bold:
            declarations.append("font-weight:700")
        if italics:
            declarations.append("font-style:italic")
        decorations: list[str] = []
        if underline:
            decorations.append("underline")
        if strike:
            decorations.append("line-through")
        if decorations:
            declarations.append(f"text-decoration:{' '.join(decorations)}")
        if blink:
            declarations.append("opacity:0.65")
        if cursor:
            declarations.append("outline:1px solid #e5e7eb")
        return ";".join(declarations)

    def _resolve_color(self, color: str, default: str) -> str:
        if color == "default":
            return default
        if color in _ANSI_COLORS:
            return _ANSI_COLORS[color]
        if len(color) == 6 and all(char in "0123456789abcdefABCDEF" for char in color):
            return f"#{color}"
        return default
