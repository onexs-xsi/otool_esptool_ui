"""Helpers for converting esptool flash-capacity output to byte counts."""

from __future__ import annotations

import re


_FLASH_SIZE_RE = re.compile(
    r"Detected\s+flash\s+size\s*:\s*(\d+(?:\.\d+)?)\s*(Ki?B|Mi?B|Gi?B|[KMG]B?)\b",
    re.IGNORECASE,
)


def parse_detected_flash_size(text: str) -> int | None:
    """Return the detected flash size in bytes, or ``None`` if unavailable."""
    match = _FLASH_SIZE_RE.search(text or "")
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    if unit.startswith("K"):
        multiplier = 1024
    elif unit.startswith("M"):
        multiplier = 1024**2
    elif unit.startswith("G"):
        multiplier = 1024**3
    else:
        return None
    size = int(value * multiplier)
    # ESP flash parts in scope are expected to be at least 256 KiB.  The upper
    # bound prevents malformed tool output from triggering an unbounded read.
    if not 256 * 1024 <= size <= 256 * 1024**2:
        return None
    return size
