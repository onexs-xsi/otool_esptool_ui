"""Import an ESP-IDF build without running build scripts or opening a device."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .esptool_commands import resolve_existing_path, validate_flash_entries


@dataclass(frozen=True)
class BuildFlashConfig:
    directory: Path
    entries: list[tuple[str, str]]
    chip: str | None = None
    before: str | None = None
    after: str | None = None
    stub: bool | None = None
    flash_mode: str | None = None
    flash_size: str | None = None
    flash_freq: str | None = None

    @property
    def global_args(self) -> list[str]:
        args: list[str] = []
        for name in ("chip", "before", "after"):
            value = getattr(self, name)
            if value is not None:
                args.extend([f"--{name}", value])
        if self.stub is False:
            args.append("--no-stub")
        return args

    @property
    def write_flash_args(self) -> list[str]:
        args: list[str] = []
        for name in ("flash_mode", "flash_size", "flash_freq"):
            value = getattr(self, name)
            if value is not None:
                args.extend(["--" + name.replace("_", "-"), value])
        return args

    @property
    def summary(self) -> str:
        values = [self.chip or "自动识别芯片"]
        if self.flash_mode:
            values.append(self.flash_mode.upper())
        if self.flash_freq:
            values.append(self.flash_freq)
        if self.flash_size:
            values.append(self.flash_size)
        if self.before or self.after:
            values.append(f"复位 {self.before or '默认'} → {self.after or '默认'}")
        if self.stub is False:
            values.append("禁用 stub")
        return "项目参数：" + " · ".join(values)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"构建清单包含重复字段或地址：{key}。")
        result[key] = value
    return result


def _normalize_parameter(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"构建参数 {name} 必须是非空字符串。")
    value = value.strip()
    if name == "chip":
        value = value.lower().replace("-", "")
    elif name in ("before", "after"):
        value = value.replace("_", "-")
    # Use the installed/bundled backend's choices so new targets need no UI table.
    try:
        from esptool import cli
    except ImportError as exc:
        raise ValueError(f"无法验证构建参数，请先准备可用的 esptool：{exc}") from exc
    params = cli.params if name in ("chip", "before", "after") else cli.commands["write-flash"].params
    parameter = next(param for param in params if param.name == name)
    if value not in parameter.type.choices:
        raise ValueError(f"构建参数 {name} 的值不受支持：{value!r}。")
    return value


def load_idf_build(directory: Path) -> BuildFlashConfig:
    """Return a fully validated, editable flash configuration from flasher_args.json."""
    try:
        directory = resolve_existing_path(directory).resolve()
        if not directory.is_dir():
            raise ValueError(f"请选择有效的 ESP-IDF 构建目录：{directory}。")
        manifest = directory / "flasher_args.json"
        data = json.loads(manifest.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取构建清单 flasher_args.json：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("构建清单必须是 JSON 对象。")
    files = data.get("flash_files")
    if not isinstance(files, dict) or not files:
        raise ValueError("构建清单 flash_files 必须包含地址和固件文件。")
    entries = []
    for address, filename in files.items():
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError(f"构建清单地址 {address!r} 对应的固件路径无效。")
        path = Path(filename)
        entries.append((address, str(path if path.is_absolute() else directory / path)))
    entries = validate_flash_entries(entries)
    entries.sort(key=lambda entry: int(entry[0], 16))

    settings = data.get("flash_settings", {})
    extra = data.get("extra_esptool_args", {})
    write_args = data.get("write_flash_args", [])
    if not isinstance(settings, dict) or not isinstance(extra, dict):
        raise ValueError("构建参数 flash_settings 和 extra_esptool_args 必须是对象。")
    if not isinstance(write_args, list) or any(not isinstance(arg, str) for arg in write_args):
        raise ValueError("构建参数 write_flash_args 必须是字符串列表。")
    flash_names = {"flash_mode", "flash_size", "flash_freq"}
    if settings.keys() - flash_names or extra.keys() - {"chip", "before", "after", "stub"}:
        raise ValueError("构建清单包含不支持的烧录参数。")
    normalized = {name: _normalize_parameter(name, value) for name, value in settings.items()}
    if len(write_args) % 2:
        raise ValueError("构建参数 write_flash_args 的选项缺少值。")
    seen = set()
    for flag, value in zip(write_args[::2], write_args[1::2]):
        name = flag[2:].replace("-", "_")
        if not flag.startswith("--") or name not in flash_names or name in seen:
            raise ValueError(f"构建清单包含重复或不支持的烧录参数：{flag}。")
        seen.add(name)
        value = _normalize_parameter(name, value)
        if name in normalized and normalized[name] != value:
            raise ValueError(f"构建参数 {name} 在 flash_settings 和 write_flash_args 中不一致。")
        normalized[name] = value
    for name, value in extra.items():
        if name == "stub":
            if not isinstance(value, bool):
                raise ValueError("构建参数 stub 必须是布尔值。")
            normalized[name] = value
        else:
            normalized[name] = _normalize_parameter(name, value)
    # An explicit 'auto' is not a target lock.
    if normalized.get("chip") == "auto":
        normalized["chip"] = None
    return BuildFlashConfig(directory=directory, entries=entries, **normalized)
