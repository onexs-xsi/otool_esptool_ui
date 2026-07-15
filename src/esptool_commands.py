from __future__ import annotations

from collections.abc import Sequence


def build_erase_flash_args(base_args: Sequence[str]) -> list[str]:
    """Build an esptool whole-chip erase command.

    esptool's ``erase-region`` command requires an integer size.  Whole-chip
    erase is a distinct command and must never be represented as
    ``erase-region 0x0 ALL``.
    """

    return [*base_args, "erase-flash"]


def is_erase_flash_command(command: Sequence[str]) -> bool:
    """Return whether *command* contains the canonical whole-chip erase action."""

    return any(part in ("erase-flash", "erase_flash") for part in command)
