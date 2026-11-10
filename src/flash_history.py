"""Local flash attempts, grouped by MAC in the history UI."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile

from .flash_timing import PHASES, normalize_mac


def validated_record(record: dict) -> dict:
    if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
        raise ValueError("烧录历史条目无效")
    for key in ("port", "chip", "started_at", "finished_at"):
        if key in record and not isinstance(record[key], str):
            raise ValueError(f"烧录历史字段无效：{key}")
    if record.get("status") not in {"running", "success", "failed", "stopped", "interrupted"}:
        raise ValueError("烧录历史状态无效")
    firmware, phases = record.get("firmware", []), record.get("phases", [])
    if not isinstance(firmware, list) or any(not isinstance(p, str) for p in firmware):
        raise ValueError("烧录历史固件路径无效")
    if not isinstance(phases, list):
        raise ValueError("烧录历史阶段无效")

    def milliseconds(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("烧录历史耗时无效")
        return value

    checked = []
    for span in phases:
        if not isinstance(span, dict) or span.get("phase") not in PHASES:
            raise ValueError("烧录历史阶段无效")
        checked.append({"phase": span["phase"], "start_ms": milliseconds(span.get("start_ms", 0)),
                        "duration_ms": milliseconds(span.get("duration_ms", 0))})
    return {**record, "mac": normalize_mac(record.get("mac", "")), "firmware": list(firmware),
            "phases": checked, "elapsed_ms": milliseconds(record.get("elapsed_ms", 0))}


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


class FlashHistoryStore:
    def __init__(self, path: Path | None = None, limit: int = 1000) -> None:
        self.path, self.limit = path, limit
        self.records: list[dict] = []
        self.load_error = ""
        if path is not None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("records"), list):
                    raise ValueError("烧录历史格式无效")
                for record in data["records"][:limit]:
                    record = validated_record(record)
                    if record.get("status") == "running":
                        record["status"] = "interrupted"
                    self.records.append(record)
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError) as exc:
                self.records = []
                self.load_error = str(exc)

    def upsert(self, record: dict) -> None:
        record = validated_record(record)
        self.records = ([record] + [r for r in self.records if r["id"] != record["id"]])[:self.limit]
        if self.path is not None:
            if self.load_error:
                raise ValueError(f"原历史文件读取失败，已保留原文件；新记录暂存于本次运行。{self.load_error}")
            atomic_json(self.path, {"version": 1, "records": self.records})


class HistoryPreferences:
    def __init__(self, path: Path | None = None) -> None:
        self.path, self.show_history = path, False
        if path is not None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.show_history = data.get("show_history") is True
            except (OSError, ValueError, TypeError, AttributeError):
                pass

    def set_visible(self, visible: bool) -> None:
        if self.path is not None:
            atomic_json(self.path, {"show_history": bool(visible)})
        self.show_history = bool(visible)
