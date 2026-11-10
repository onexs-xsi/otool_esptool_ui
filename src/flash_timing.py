"""Monotonic phase timing; this module is safe to import in an esptool worker."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
import math
import re
import time
from uuid import uuid4

PHASES = {
    "prepare": ("准备", "#94a3b8"),
    "connect": ("连接", "#3b82f6"),
    "erase": ("擦除", "#f59e0b"),
    "write": ("写入", "#8b5cf6"),
    "verify": ("校验", "#14b8a6"),
    "reset": ("复位", "#0ea5e9"),
    "finish": ("收尾", "#64748b"),
}
CST = timezone(timedelta(hours=8))


def normalize_mac(value: str) -> str:
    text = str(value or "").strip().lower().replace("-", ":")
    return text if re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", text) else ""


class FlashRunTimer:
    def __init__(self, port: str, firmware: list[str], *, clock=time.monotonic,
                 started_at: str | None = None) -> None:
        self.clock = clock
        self.origin = clock()
        self.phase_start = self.origin
        self.phase = "prepare"
        self.spans: list[dict] = []
        self.worker_offset: float | None = None
        self.record = {"id": uuid4().hex, "mac": "", "chip": "未识别", "port": port,
                       "firmware": firmware, "started_at": started_at or datetime.now(CST).isoformat(timespec="milliseconds"),
                       "status": "running", "elapsed_ms": 0.0, "phases": []}
        self.finished = False

    def switch(self, phase: str, worker_elapsed_ms=None) -> None:
        if self.finished or phase not in PHASES or phase == self.phase:
            return
        now = self.clock()
        if worker_elapsed_ms is not None:
            try:
                elapsed = float(worker_elapsed_ms) / 1000
                if not math.isfinite(elapsed) or elapsed < 0:
                    return
                if self.worker_offset is None:
                    self.worker_offset = now - elapsed
                now = min(now, self.worker_offset + elapsed)
            except (ValueError, TypeError, OverflowError):
                return
        now = max(now, self.phase_start)
        self.spans.append({"phase": self.phase, "start_ms": (self.phase_start - self.origin) * 1000,
                           "duration_ms": (now - self.phase_start) * 1000})
        self.phase, self.phase_start = phase, now

    def snapshot(self) -> dict:
        now = self.phase_start if self.finished else self.clock()
        spans = list(self.spans)
        if not self.finished:
            spans.append({"phase": self.phase, "start_ms": (self.phase_start - self.origin) * 1000,
                          "duration_ms": max(0.0, (now - self.phase_start) * 1000)})
        return {**self.record, "phases": spans,
                "elapsed_ms": self.record["elapsed_ms"] if self.finished else max(0.0, (now - self.origin) * 1000)}

    def finish(self, status: str, **metadata) -> dict:
        if not self.finished:
            now = self.clock()
            self.spans.append({"phase": self.phase, "start_ms": (self.phase_start - self.origin) * 1000,
                               "duration_ms": max(0.0, (now - self.phase_start) * 1000)})
            self.record.update(metadata, status=status, elapsed_ms=max(0.0, (now - self.origin) * 1000),
                               finished_at=datetime.now(CST).isoformat(timespec="milliseconds"))
            self.finished = True
        return self.snapshot()


@contextmanager
def instrument_esptool_phases(module, emit):
    """Emit boundaries from real loader calls, not progress percentages."""
    started = time.monotonic()
    last = None
    originals = []

    def phase(name):
        nonlocal last
        if name != last:
            last = name
            emit({"type": "phase", "phase": name, "elapsed_ms": (time.monotonic() - started) * 1000})

    def wrap(owner, name, stage):
        original = getattr(owner, name, None)
        if not callable(original):
            return
        @wraps(original)
        def call(*args, **kwargs):
            phase(stage)
            return original(*args, **kwargs)
        originals.append((owner, name, original, name in vars(owner)))
        setattr(owner, name, call)

    wrap(module, "connect_esp", "connect")
    loader = getattr(module, "ESPLoader", None)
    if loader is not None:
        classes, pending = [], [loader]
        while pending:
            cls = pending.pop()
            if cls in classes:
                continue
            classes.append(cls)
            pending.extend(cls.__subclasses__())
        methods = {
            "flash_begin": "erase", "flash_defl_begin": "erase", "erase_flash": "erase",
            "erase_region": "erase", "flash_block": "write", "flash_defl_block": "write",
            "flash_md5sum": "verify", "read_flash": "verify", "hard_reset": "reset", "soft_reset": "reset",
        }
        for cls in classes:
            for name, stage in methods.items():
                wrap(cls, name, stage)
    try:
        yield
    finally:
        phase("finish")
        for owner, name, original, owned in reversed(originals):
            if owned:
                setattr(owner, name, original)
            else:
                delattr(owner, name)
