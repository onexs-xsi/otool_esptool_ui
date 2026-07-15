from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


@dataclass(frozen=True)
class EfuseTarget:
    name: str
    value: str
    description: str = ""


@dataclass(frozen=True)
class EfuseRunConfig:
    fields: tuple[EfuseTarget, ...]
    chip: str
    baud: str


@dataclass(frozen=True)
class EfusePrecheckResult:
    to_burn: tuple[EfuseTarget, ...]
    skipped: tuple[str, ...]
    conflicts: tuple[str, ...]

    @property
    def can_burn(self) -> bool:
        return bool(self.to_burn) and not self.conflicts

    @property
    def all_satisfied(self) -> bool:
        return not self.to_burn and not self.conflicts


def normalize_efuse_value(value: str) -> str:
    """Normalize an eFuse value string for comparison."""

    normalized = value.strip().lower()
    if normalized in ("true", "enable", "enabled"):
        return "1"
    if normalized in ("false", "disable", "disabled", "none", ""):
        return "0"
    if normalized.startswith("0x"):
        try:
            return str(int(normalized, 16))
        except ValueError:
            pass
    return normalized


def _parse_numeric_efuse_value(value: object) -> int | None:
    """Parse CLI-compatible numeric/boolean/hex-byte values without guessing."""

    normalized = str(value).strip().lower()
    if normalized in ("true", "enable", "enabled"):
        return 1
    if normalized in ("false", "disable", "disabled", "none", ""):
        return 0
    if re.fullmatch(r"(?:[0-9a-f]{2}[:.\-\s]){5,7}[0-9a-f]{2}", normalized):
        normalized = re.sub(r"[:.\-\s]", "", normalized)
        return int(normalized, 16)
    try:
        parsed = int(normalized, 0)
    except ValueError:
        if not normalized.isdigit():
            return None
        parsed = int(normalized, 10)
    return parsed if parsed >= 0 else None


def _typed_values(
    target_field: EfuseTarget,
    info: Mapping[str, object],
) -> tuple[int, int] | None:
    """Return current/target raw bits when both sides are safely representable."""

    if "raw_value" in info:
        current = _parse_numeric_efuse_value(info.get("raw_value"))
    else:
        current = _parse_numeric_efuse_value(info.get("value", ""))
    target = _parse_numeric_efuse_value(target_field.value)
    if current is None or target is None:
        return None

    try:
        bit_len = int(info.get("bit_len", 0))
    except (TypeError, ValueError):
        bit_len = 0
    if bit_len > 0 and (current >= (1 << bit_len) or target >= (1 << bit_len)):
        return None
    return current, target


def _values_equal(
    target_field: EfuseTarget,
    info: Mapping[str, object],
) -> tuple[bool, tuple[int, int] | None]:
    if (
        "raw_value" in info
        and _parse_numeric_efuse_value(info.get("raw_value")) is None
    ):
        return False, None
    typed = _typed_values(target_field, info)
    if typed is not None:
        return typed[0] == typed[1], typed
    current = normalize_efuse_value(str(info.get("value", "")))
    target = normalize_efuse_value(target_field.value)
    return current == target, None


def evaluate_efuse_precheck(
    fields: tuple[EfuseTarget, ...],
    read_result: Mapping[str, Mapping[str, object]],
    *,
    force_burn: bool = False,
) -> EfusePrecheckResult:
    """Classify requested fields without allowing a partial-success result."""

    to_burn: list[EfuseTarget] = []
    skipped: list[str] = []
    conflicts: list[str] = []

    for target_field in fields:
        info = read_result.get(target_field.name)
        if info is None:
            conflicts.append(target_field.name)
            continue

        # A hidden value cannot prove either satisfaction or OTP compatibility.
        if not bool(info.get("readable", True)):
            conflicts.append(target_field.name)
            continue

        equal, typed = _values_equal(target_field, info)
        if not bool(info.get("writeable", True)):
            if equal:
                skipped.append(target_field.name)
            else:
                conflicts.append(target_field.name)
            continue

        if equal:
            if not force_burn:
                skipped.append(target_field.name)
                continue
            if typed is None:
                conflicts.append(target_field.name)
                continue

        if not equal:
            # eFuse is OTP: a requested target must be a bitwise superset of
            # the currently burned value.  If semantic text cannot be mapped
            # to raw bits, fail closed instead of asking espefuse to guess.
            if typed is None or (typed[0] & typed[1]) != typed[0]:
                conflicts.append(target_field.name)
                continue
        to_burn.append(target_field)

    return EfusePrecheckResult(
        to_burn=tuple(to_burn),
        skipped=tuple(skipped),
        conflicts=tuple(conflicts),
    )


def evaluate_efuse_verification(
    fields: tuple[EfuseTarget, ...],
    read_result: Mapping[str, Mapping[str, object]],
) -> tuple[str, ...]:
    """Return every requested field that is missing or differs after burning.

    Verification intentionally covers the immutable run configuration, including
    fields skipped during precheck because they already appeared satisfied.  This
    prevents a replacement device or a changed field from passing verification
    merely because that field was absent from the burn command.
    """

    mismatches: list[str] = []
    for target_field in fields:
        info = read_result.get(target_field.name)
        if info is None:
            mismatches.append(target_field.name)
            continue

        if not bool(info.get("readable", True)):
            mismatches.append(target_field.name)
            continue
        equal, _typed = _values_equal(target_field, info)
        if not equal:
            mismatches.append(target_field.name)

    return tuple(mismatches)


_IDENTITY_FIELD_NAMES = (
    "MAC_FACTORY",
    "FACTORY_MAC",
    "BASE_MAC_ADDR",
    "MAC_ADDRESS",
    "MAC",
)

_IDENTITY_SEPARATOR_RE = re.compile(r"[:.\-\s]+")
_IDENTITY_HEX_RE = re.compile(r"(?:[0-9a-f]{12}|[0-9a-f]{16})\Z")


def _normalize_device_identity(value: object) -> str:
    """Normalize a 48- or 64-bit MAC-like value, rejecting sentinels."""

    raw = str(value).strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    compact = _IDENTITY_SEPARATOR_RE.sub("", raw)
    if _IDENTITY_HEX_RE.fullmatch(compact) is None:
        return ""
    if set(compact) <= {"0"} or set(compact) <= {"f"}:
        return ""
    return compact


def extract_stable_device_identity(
    read_result: Mapping[str, Mapping[str, object]],
) -> str:
    """Extract a stable factory identity from an espefuse JSON summary.

    A COM port or USB-UART adapter is not a device identity.  Automatic burn
    requires a factory MAC-like value so a second read can prove that the same
    chip is still attached immediately before the irreversible command.
    """

    by_upper_name = {name.upper(): (name, value) for name, value in read_result.items()}
    for candidate in _IDENTITY_FIELD_NAMES:
        item = by_upper_name.get(candidate)
        if item is None:
            continue
        original_name, info = item
        if "raw_value" in info and str(info["raw_value"]).strip():
            compact_value = _normalize_device_identity(info["raw_value"])
        else:
            compact_value = _normalize_device_identity(info.get("value", ""))
        if compact_value:
            return f"{original_name.upper()}:{compact_value}"
    return ""


def build_transport_fingerprint(
    *,
    device: str,
    serial_number: str = "",
    location: str = "",
    vid: int | None = None,
    pid: int | None = None,
    hwid: str = "",
    description: str = "",
) -> str:
    """Build a transport identity that is stronger than a COM port alone."""

    vid_text = f"{vid:04x}" if vid is not None else "----"
    pid_text = f"{pid:04x}" if pid is not None else "----"
    serial = serial_number.strip().lower()
    usb_location = location.strip().lower()
    if serial or usb_location:
        return (
            f"usb:{vid_text}:{pid_text}:"
            f"loc={usb_location or '-'}:sn={serial or '-'}"
        )
    normalized_hwid = hwid.strip().lower()
    if normalized_hwid:
        return f"hwid:{normalized_hwid}"
    return f"port:{device.strip().upper()}:desc={description.strip().lower()}"
