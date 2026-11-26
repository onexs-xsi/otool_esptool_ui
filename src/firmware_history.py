"""A bounded, most-recently-used list of firmware and ESP-IDF build sources."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

HISTORY_LIMIT = 16
HISTORY_LABEL_MAX_CHARS = 56
HISTORY_LABEL_MAX_WIDTH = 420


@dataclass(frozen=True)
class FirmwareSource:
    kind: str
    path: str

    @property
    def available(self) -> bool:
        path = Path(self.path)
        return (path / "flasher_args.json").is_file() if self.kind == "build" else path.is_file()


def shorten_source_path(source: FirmwareSource, limit: int = HISTORY_LABEL_MAX_CHARS) -> str:
    text = source.path
    if len(text) <= limit:
        return text
    path = Path(text)
    tail = str(Path(path.parent.name) / path.name)
    marker = "******"
    prefix_minimum = min(8, max(1, (limit - len(marker)) // 3))
    tail = tail[-max(1, limit - len(marker) - prefix_minimum):]
    return text[:max(0, limit - len(marker) - len(tail))] + marker + tail


class FirmwareHistoryStore:
    def __init__(self, path: Path | None = None, limit: int = HISTORY_LIMIT) -> None:
        self.path = path
        self.limit = max(1, limit)
        self.entries: list[FirmwareSource] = []
        self.load_error = ""
        if path is not None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), list):
                    raise ValueError("历史记录格式无效")
                seen = set()
                for item in data["entries"]:
                    if not isinstance(item, dict) or item.get("kind") not in ("bin", "build") or not isinstance(item.get("path"), str):
                        raise ValueError("历史记录条目无效")
                    entry = FirmwareSource(item["kind"], item["path"])
                    key = (entry.kind, os.path.normcase(entry.path))
                    if key not in seen:
                        self.entries.append(entry)
                        seen.add(key)
                self.entries = self.entries[:self.limit]
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError) as exc:
                self.entries = []
                self.load_error = str(exc)

    def remember(self, kind: str, path: str | Path) -> None:
        if kind not in ("bin", "build"):
            raise ValueError(f"未知历史记录类型：{kind}")
        entry = FirmwareSource(kind, str(Path(path).expanduser().resolve()))
        if not entry.available:
            return
        key = os.path.normcase(entry.path)
        entries = [entry] + [item for item in self.entries
                             if item.kind != kind or os.path.normcase(item.path) != key]
        self._save(entries[:self.limit])

    def forget(self, source: FirmwareSource) -> None:
        entries = [item for item in self.entries if item != source]
        if len(entries) != len(self.entries):
            self._save(entries)

    def _save(self, entries: list[FirmwareSource]) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                                 prefix="firmware_history-", suffix=".tmp", delete=False) as stream:
                    temporary = stream.name
                    json.dump({"version": 1, "entries": [vars(item) for item in entries]}, stream,
                              ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary and Path(temporary).exists():
                    Path(temporary).unlink()
        self.entries = entries
        self.load_error = ""
