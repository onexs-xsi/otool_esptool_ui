"""Build-time guards for keeping frozen binaries self-contained and stable."""

from __future__ import annotations

import re
from collections.abc import Iterable
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Callable


_ROOT_ICU_DLL_PATTERN = re.compile(
    r"icu(?:dt|in|io|tu|uc)?\d*\.dll",
    re.IGNORECASE,
)


def partition_root_icu_binaries(
    binaries: Iterable[tuple[str, str, str]],
) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Separate accidental root-level ICU DLLs from valid frozen binaries.

    Qt 6.11 on Windows links to the operating-system ICU forwarder.  If an
    unrelated toolchain (for example Poppler) is present on ``PATH``,
    PyInstaller may resolve that dependency to a private, ABI-incompatible ICU
    build and place it at the archive root.  Package-owned ICU DLLs remain
    valid because their destination includes the owning package directory.
    """

    kept: list[tuple[str, str, str]] = []
    excluded: list[tuple[str, str, str]] = []
    for entry in binaries:
        destination = str(entry[0]).replace("\\", "/").strip("/")
        is_root_icu = "/" not in destination and bool(
            _ROOT_ICU_DLL_PATTERN.fullmatch(destination)
        )
        (excluded if is_root_icu else kept).append(entry)
    return kept, excluded


def validate_release_inputs(
    root: Path, version_resolver: Callable[[str], str] = distribution_version,
) -> str:
    """Reject stale version metadata, unlocked installs and a stale submodule."""
    def extract(path: str, pattern: str) -> str:
        text = (root / path).read_text(encoding="utf-8")
        match = re.search(pattern, text, re.MULTILINE)
        if match is None:
            raise RuntimeError(f"Missing release version in {path}")
        return match.group(1)

    versions = {
        "entry": extract("otool_esptool_ui.py", r'^__version__\s*=\s*[\"\'](\d+\.\d+\.\d+)[\"\']'),
        "project": extract("pyproject.toml", r'^version\s*=\s*[\"\'](\d+\.\d+\.\d+)[\"\']'),
        "constants": extract("src/constants.py", r'^_DEFAULT_VERSION\s*=\s*[\"\'](\d+\.\d+\.\d+)[\"\']'),
        "README": extract("README.md", r'^>[^\n]*\bv(\d+\.\d+\.\d+)\b'),
    }
    if len(set(versions.values())) != 1:
        raise RuntimeError(f"Release version mismatch: {versions}")
    locked = {}
    for line in (root / "requirements-build.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([\w.-]+)==([^\s;]+)", line.strip())
        if match is None:
            raise RuntimeError(f"Unsupported/unlocked build requirement: {line}")
        name, expected = match.groups()
        actual = version_resolver(name)
        if actual != expected:
            raise RuntimeError(f"Installed {name} {actual} differs from build lock {expected}")
        locked[name.lower()] = expected
    declared_esptool = extract("pyproject.toml", r'[\"\']esptool==([^\"\']+)[\"\']')
    if locked.get("esptool") != declared_esptool:
        raise RuntimeError("esptool build lock differs from project dependency")
    local = root / "esptool"
    if any((local / name / "__init__.py").is_file() for name in ("esptool", "espefuse", "espsecure")):
        if not all((local / name / "__init__.py").is_file() for name in ("esptool", "espefuse", "espsecure")):
            raise RuntimeError("Incomplete esptool submodule; initialize it or use an empty directory")
        local_version = extract("esptool/esptool/__init__.py", r'^__version__\s*=\s*[\"\']([^\"\']+)[\"\']')
        if local_version != declared_esptool:
            raise RuntimeError(f"esptool submodule {local_version} differs from {declared_esptool}")
    return versions["entry"]


if __name__ == "__main__":
    print(f"Release inputs verified: {validate_release_inputs(Path(__file__).resolve().parents[1])}")
