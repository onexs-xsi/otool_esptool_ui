"""Persistent device exclusions shared by all serial workbenches."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

from PyQt6.QtCore import QObject, QStandardPaths, pyqtSignal


def canonical_port(port: str) -> str:
    return str(port or "").strip().casefold()


@dataclass(frozen=True)
class DeviceIdentity:
    port: str
    key: str
    label: str
    portable: bool

    @classmethod
    def from_device(cls, device: object) -> "DeviceIdentity":
        port = str(getattr(device, "port", "") or getattr(device, "device", ""))
        serial = str(getattr(device, "serial_number", "") or "")
        location = str(getattr(device, "location", "") or "")
        vid, pid = getattr(device, "vid", None), getattr(device, "pid", None)
        label = str(getattr(device, "label", "") or getattr(device, "description", "") or port)
        if serial:
            parts = ["serial", vid, pid, serial]
        elif location and vid is not None and pid is not None:
            parts = ["location", vid, pid, location]
        else:
            # A generic VID/PID or description is not a unique device identity.
            # Never accidentally exclude every adapter of the same model.
            parts = ["port", canonical_port(port)]
        return cls(port, json.dumps(parts, ensure_ascii=False, separators=(",", ":")), label, parts[0] != "port")


class DevicePolicyStore(QObject):
    changed = pyqtSignal()

    def __init__(self, path: Path | None = None) -> None:
        super().__init__()
        self.path = path
        self.blocked_ports: set[str] = set()
        self.blocked_devices: dict[str, str] = {}
        self.load_error = ""
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("version") != 1:
                    raise ValueError("不支持的屏蔽记录格式")
                ports, devices = data["blocked_ports"], data["blocked_devices"]
                if not isinstance(ports, list) or not all(isinstance(p, str) for p in ports):
                    raise ValueError("端口屏蔽记录无效")
                if not isinstance(devices, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in devices.items()):
                    raise ValueError("设备屏蔽记录无效")
                self.blocked_ports = {canonical_port(p) for p in ports}
                self.blocked_devices = devices
            except (OSError, ValueError, KeyError, TypeError) as exc:
                # Do not silently resume unattended flashing after losing exclusions.
                self.load_error = f"无法读取屏蔽记录 {path}：{exc}"

    @classmethod
    def persistent(cls) -> "DevicePolicyStore":
        override = os.environ.get("OTOOL_ESPTOOL_UI_STATE_DIR", "").strip()
        directory = Path(override) if override else Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericConfigLocation)
        ) / "OTool" / "EsptoolUI"
        return cls(directory / "device_policies.json")

    def is_port_rule(self, port: str) -> bool:
        return canonical_port(port) in self.blocked_ports

    def is_device_rule(self, identity: DeviceIdentity) -> bool:
        return identity.key in self.blocked_devices

    def is_blocked(self, identity: DeviceIdentity) -> bool:
        return bool(self.load_error) or self.is_port_rule(identity.port) or self.is_device_rule(identity)

    def set_blocked(self, identity: DeviceIdentity, scope: str, blocked: bool) -> None:
        if self.load_error:
            raise OSError(self.load_error)
        ports, devices = set(self.blocked_ports), dict(self.blocked_devices)
        if scope == "port":
            if blocked:
                ports.add(canonical_port(identity.port))
            else:
                ports.discard(canonical_port(identity.port))
        elif scope == "device":
            if blocked:
                devices[identity.key] = identity.label
            else:
                devices.pop(identity.key, None)
        else:
            raise ValueError(f"未知屏蔽范围：{scope}")
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                                 prefix="device_policies-", suffix=".tmp", delete=False) as stream:
                    temporary = stream.name
                    json.dump({"version": 1, "blocked_ports": sorted(ports), "blocked_devices": devices},
                              stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary and Path(temporary).exists():
                    Path(temporary).unlink()
        self.blocked_ports, self.blocked_devices = ports, devices
        self.changed.emit()
