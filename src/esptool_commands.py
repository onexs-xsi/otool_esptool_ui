from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import re


def resolve_existing_path(filename: str | Path) -> Path:
    """Prefer a literal Windows path, repairing Markdown escapes only if missing."""
    path = Path(filename)
    if path.exists():
        return path
    if "\\_" in str(filename):
        candidate = Path(str(filename).replace("\\_", "_"))
        if candidate.exists():
            return candidate
    text = str(filename)
    if len(text) > 2 and text[0] == text[-1] == '"':
        candidate = resolve_existing_path(text[1:-1])
        if candidate.exists():
            return candidate
    return path


def validate_flash_entries(entries: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Validate files and erase-sector ranges before any device is opened."""
    validated: list[tuple[str, str]] = []
    ranges: list[tuple[int, int, int]] = []
    for row, (address, filename) in enumerate(entries, 1):
        address, filename = address.strip(), filename.strip()
        if not address and not filename:
            continue
        if not re.fullmatch(r"0[xX][0-9a-fA-F]+", address):
            raise ValueError(f"第 {row} 行烧录地址无效，请使用十六进制，如 0x0、0x10000。")
        try:
            path = resolve_existing_path(filename)
            if not filename or not path.is_file():
                raise ValueError(f"第 {row} 行请选择有效的固件文件：{filename!r}。")
            size = path.stat().st_size
            # Also catch unreadable files before acquiring a serial-port lease.
            with path.open("rb"):
                pass
            path = path.resolve()
        except OSError as exc:
            raise ValueError(f"第 {row} 行无法读取固件：{exc}") from exc
        if size == 0:
            raise ValueError(f"第 {row} 行固件为空文件。")
        start = int(address, 16)
        if start + size > 0x100000000:
            raise ValueError(f"第 {row} 行固件超出 32 位 Flash 地址范围。")
        # esptool checks overlap at the 4 KiB erase-sector granularity.
        ranges.append((start & ~0xFFF, (start + size + 0xFFF) & ~0xFFF, row))
        validated.append((address, str(path)))
    if not validated:
        raise ValueError("请至少填写一个有效的烧录条目。")
    ordered = sorted(ranges)
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise ValueError(
                f"第 {previous[2]} 行与第 {current[2]} 行的烧录区域重叠"
                "（含 4 KiB 擦除扇区），请调整地址。"
            )
    return validated


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
