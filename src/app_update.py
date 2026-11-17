"""Read official GitHub releases and stage a verified Windows update."""

from __future__ import annotations

import base64
from collections.abc import Callable
import ctypes
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from threading import Event
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .constants import APP_VERSION, RELEASE_REPOSITORY, _FROZEN


RELEASE_ROOT = f"https://github.com/{RELEASE_REPOSITORY}/releases"
RELEASE_API = f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases/latest"
EXECUTABLE_NAME = "otool_esptool_ui.exe"
_MAX_PACKAGE_SIZE = 300 * 1024 * 1024
_MAX_RELEASE_SIZE = 1024 * 1024
_SESSION_PREFIX = "otool-update-"
_SESSION_ARGUMENT = "--otool-update-session="


class UpdateError(Exception):
    """An error that can be shown directly in the update dialog."""


@dataclass(frozen=True)
class ReleaseAsset:
    url: str
    size: int
    digest: str


@dataclass(frozen=True)
class Release:
    tag: str
    version: str
    notes: str
    asset: ReleaseAsset | None

    @property
    def url(self) -> str:
        return f"{RELEASE_ROOT}/tag/{self.tag}"


def _version_tuple(tag: str) -> tuple[int, ...]:
    if not re.fullmatch(r"v?[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}", tag):
        return ()
    return tuple(int(part) for part in tag.removeprefix("v").split("."))


def parse_release(payload: object, current_version: str) -> Release | None:
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag = payload.get("tag_name")
    current = _version_tuple(current_version)
    version = _version_tuple(tag) if isinstance(tag, str) else ()
    if not current or version <= current:
        return None

    expected_url = f"{RELEASE_ROOT}/download/{tag}/{EXECUTABLE_NAME}"
    assets = payload.get("assets")
    package = None
    for asset in assets if isinstance(assets, list) else []:
        if not isinstance(asset, dict) or asset.get("name") != EXECUTABLE_NAME:
            continue
        digest = asset.get("digest")
        size = asset.get("size")
        if (
            asset.get("state") == "uploaded"
            and asset.get("browser_download_url") == expected_url
            and type(size) is int and 0 < size <= _MAX_PACKAGE_SIZE
            and isinstance(digest, str) and re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest)
        ):
            package = ReleaseAsset(expected_url, size, digest.removeprefix("sha256:").lower())
            break
    notes = payload.get("body")
    return Release(tag, ".".join(map(str, version)), notes if isinstance(notes, str) else "", package)


class _GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urlsplit(newurl)
        if url.scheme != "https" or url.netloc not in {
            "github.com", "api.github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
        }:
            raise UpdateError("GitHub 下载跳转地址无效。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _github_request(url: str, *, api: bool = False):
    headers = {"User-Agent": f"OTool-Esptool-UI/{APP_VERSION}"}
    if api:
        headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    return build_opener(_GitHubRedirects()).open(Request(url, headers=headers), timeout=8)


def check_for_release(current_version: str = APP_VERSION) -> Release | None:
    for attempt in range(2):
        try:
            with _github_request(RELEASE_API, api=True) as response:
                raw = response.read(_MAX_RELEASE_SIZE + 1)
            if len(raw) <= _MAX_RELEASE_SIZE:
                return parse_release(json.loads(raw), current_version)
            break
        except HTTPError:
            raise
        except URLError:
            if attempt == 1:
                raise
    return None


def discard_download(directory: Path) -> None:
    # The staging directory is private to the update. Remove the files we own.
    for name in (EXECUTABLE_NAME, "ready", "commit", "started", "error.txt"):
        try:
            (directory / name).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        directory.rmdir()
    except OSError:
        pass


def download_release(asset: ReleaseAsset, cancel: Event, progress: Callable[[int], None]) -> Path:
    directory = Path(tempfile.mkdtemp(prefix=_SESSION_PREFIX))
    digest = sha256()
    received = last_percent = 0
    deadline = monotonic() + 600
    try:
        with _github_request(asset.url) as response, (directory / EXECUTABLE_NAME).open("xb") as output:
            while not cancel.is_set():
                if monotonic() >= deadline:
                    raise UpdateError("下载超时，请重试。")
                chunk = response.read1(256 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > asset.size:
                    raise UpdateError("下载文件大小与 GitHub 发布记录不一致。")
                output.write(chunk)
                digest.update(chunk)
                percent = received * 100 // asset.size
                if percent != last_percent:
                    progress(percent)
                    last_percent = percent
        if cancel.is_set():
            raise UpdateError("下载已取消。")
        if received != asset.size or digest.hexdigest() != asset.digest:
            raise UpdateError("下载文件校验失败，请重新下载。")
    except Exception:
        discard_download(directory)
        raise
    return directory


def can_install_release() -> bool:
    return sys.platform == "win32" and _FROZEN


@dataclass(frozen=True)
class PreparedUpdate:
    directory: Path
    process: subprocess.Popen

    def commit(self) -> None:
        if self.process.poll() is not None or not (self.directory / "ready").is_file():
            raise UpdateError("更新程序尚未就绪，请重试。")
        (self.directory / "commit").touch(exist_ok=False)

    def cancel(self) -> None:
        # A committed helper owns the staging directory until it finishes.
        if (self.directory / "commit").is_file():
            return
        try:
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=2)
        finally:
            discard_download(self.directory)


def prepare_installation(directory: Path, release: Release, restart_arguments: list[str]) -> PreparedUpdate:
    if not can_install_release() or release.asset is None:
        raise UpdateError("请从 GitHub 下载 Windows 发布包。")

    # The helper must outlive both onefile processes, so its code and data are
    # passed by value. It never executes a temporary script file as administrator.
    system_dir = ctypes.create_unicode_buffer(32768)
    kernel32 = ctypes.windll.kernel32
    length = kernel32.GetSystemDirectoryW(system_dir, len(system_dir))
    if not 0 < length < len(system_dir):
        raise UpdateError("找不到 Windows PowerShell。")
    powershell = Path(system_dir.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    target = Path(sys.executable).resolve(strict=True)
    payload = {
        "directory": str(directory),
        "target": str(target),
        "digest": release.asset.digest,
        "current": f"{APP_VERSION}.0",
        "version": f"{release.version}.0",
        "guiPid": os.getpid(),
        "bootPid": os.getppid(),
        "mutex": "Local\\OToolUpdate-" + sha256(str(target).casefold().encode("utf-8")).hexdigest(),
        "arguments": subprocess.list2cmdline(restart_arguments),
        "startupArgument": _SESSION_ARGUMENT + directory.name,
    }
    data = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    script = (Path(__file__).parent / "assets" / "apply_update.ps1").read_text(encoding="utf-8")
    command = base64.b64encode(f"& {{\n{script}\n}} '{data}'".encode("utf-16-le")).decode("ascii")
    env = os.environ.copy()
    bundle_dir = str(sys._MEIPASS)
    for key in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        if env.get(key, "").casefold().startswith(bundle_dir.casefold()):
            env.pop(key)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    kernel32.SetDllDirectoryW(None)
    try:
        process = subprocess.Popen(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
            cwd=system_dir.value, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    finally:
        kernel32.SetDllDirectoryW(bundle_dir)
    return PreparedUpdate(directory, process)


def acknowledge_updated_startup(arguments: list[str]) -> None:
    """Acknowledge only after the relaunched workbench enters its event loop."""
    for argument in arguments:
        session = argument.removeprefix(_SESSION_ARGUMENT)
        if argument.startswith(_SESSION_ARGUMENT) and re.fullmatch(_SESSION_PREFIX + r"[a-z0-9_]{8}", session):
            directory = Path(tempfile.gettempdir()) / session
            try:
                if (directory / "commit").is_file():
                    (directory / "started").touch(exist_ok=False)
            except OSError:
                pass
            break
