import os
import re
import sys
import importlib.util
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

# ── 内部标记 ──────────────────────────────────────────────────────────────────

_INTERNAL_WORKER_MARKER = "__otool_worker__"
_INTERNAL_WORKER_EVENT_PREFIX = "[[OTOOL_EVENT]]"

# ── 路径（必须在应用信息之前，供 _read_entry_metadata 使用）────────────────────

PACKAGED_AVATAR_RELATIVE_PATH = Path("assets") / "onexs_avatar.png"
_FROZEN = getattr(sys, 'frozen', False)

if _FROZEN:
    TOOL_DIR = Path(sys.executable).resolve().parent
    LOCAL_ESPTOOL_DIR = Path("<frozen>")
    # PyInstaller onefile: 数据文件解压到 sys._MEIPASS，从那里读取入口脚本
    _ENTRY_SCRIPT = Path(getattr(sys, "_MEIPASS", TOOL_DIR)) / "otool_esptool_ui.py"
else:
    TOOL_DIR = Path(__file__).resolve().parent.parent   # src/ -> otool_esptool_ui/
    LOCAL_ESPTOOL_DIR = TOOL_DIR / "esptool"
    _ENTRY_SCRIPT = TOOL_DIR / "otool_esptool_ui.py"

# ── 应用信息（单一来源：otool_esptool_ui.py）─────────────────────────────────

_DEFAULT_TITLE = "OTool Esptool UI | byonexs."
_DEFAULT_VERSION = "0.1.0"
_DEFAULT_AUTHOR = "ONEXS"


def _read_entry_metadata() -> dict[str, str]:
    """从入口脚本解析 __version__ / __author__ / __title__。"""
    metadata: dict[str, str] = {}
    try:
        text = _ENTRY_SCRIPT.read_text(encoding="utf-8")
        for m in re.finditer(
            r'^__(version|author|title)__\s*=\s*["\'](.+?)["\']',
            text,
            re.MULTILINE,
        ):
            metadata[m.group(1)] = m.group(2)
    except Exception:
        pass
    return metadata


_ENTRY_META = _read_entry_metadata()
APP_TITLE = _ENTRY_META.get("title", _DEFAULT_TITLE)
APP_VERSION = _ENTRY_META.get("version", _DEFAULT_VERSION)
APP_VERSION_WIN = APP_VERSION + ".0" if APP_VERSION.count(".") == 2 else APP_VERSION
APP_AUTHOR = _ENTRY_META.get("author", _DEFAULT_AUTHOR)
APP_GITHUB_URL = (
    os.environ.get(
        "OTOOL_ESPTOOL_UI_GITHUB_URL",
        "https://github.com/onexs-xsi/otool_esptool_ui",
    ).strip()
    or "https://github.com/onexs-xsi/otool_esptool_ui"
)


def _resolve_github_avatar_url() -> str:
    explicit_url = os.environ.get("OTOOL_ESPTOOL_UI_GITHUB_AVATAR_URL", "").strip()
    if explicit_url:
        return explicit_url

    parsed = urlparse(APP_GITHUB_URL)
    if parsed.netloc.lower() == "github.com":
        path_parts = [part for part in parsed.path.split("/") if part]
        if path_parts:
            return f"https://github.com/{path_parts[0]}.png?size=256"

    return ""


APP_GITHUB_AVATAR_URL = _resolve_github_avatar_url()

# (路径已在文件顶部定义)


def _resource_root() -> Path:
    if _FROZEN:
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return Path(meipass)
    return TOOL_DIR


def _resolve_packaged_avatar_path() -> Path:
    explicit_path = os.environ.get("OTOOL_ESPTOOL_UI_GITHUB_AVATAR_FILE", "").strip()
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()
    return _resource_root() / PACKAGED_AVATAR_RELATIVE_PATH


# ── esptool 后端检测 ─────────────────────────────────────────────────────────

def _local_esptool_available() -> bool:
    return LOCAL_ESPTOOL_DIR.exists()


def _tool_module_available(tool: str) -> bool:
    return importlib.util.find_spec(tool) is not None


def _tool_backend_available(tool: str) -> bool:
    return _FROZEN or _local_esptool_available() or _tool_module_available(tool)


# ── 固件目录 ─────────────────────────────────────────────────────────────────

def _resolve_default_firmware_dir() -> Path:
    firmware_dir = os.environ.get("OTOOL_ESPTOOL_UI_FIRMWARE_DIR", "").strip()
    if firmware_dir:
        return Path(firmware_dir).expanduser().resolve()

    candidates = [
        TOOL_DIR / "firmware",
    ]
    if len(TOOL_DIR.parents) >= 2:
        candidates.append(TOOL_DIR.parents[1] / "firmware")

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


DEFAULT_FIRMWARE_DIR = _resolve_default_firmware_dir()
DETECT_BAUD = "115200"
FLASH_BAUD_OPTIONS = ["9600", "57600", "115200", "230400", "460800", "921600", "1500000"]
FLASH_BAUD_DEFAULT = "921600"

# ── 芯片名称映射（chip_name → esptool --chip 参数）────────────────────────────

_CHIP_ARG_MAP = [
    ("esp32-p4", "esp32p4"),
    ("esp32-s3", "esp32s3"),
    ("esp32-s2", "esp32s2"),
    ("esp32-c6", "esp32c6"),
    ("esp32-c5", "esp32c5"),
    ("esp32-c3", "esp32c3"),
    ("esp32-c2", "esp32c2"),
    ("esp32-h2", "esp32h2"),
    ("esp32",    "esp32"),
]


def resolve_chip_arg(chip_name: str) -> str:
    """将 DeviceInfo.chip_name 映射为 esptool/espefuse 的 ``--chip`` 参数值。"""
    name = (chip_name or "").lower()
    for key, val in _CHIP_ARG_MAP:
        if key in name:
            return val
    return "auto"

# ── 命令构建 ─────────────────────────────────────────────────────────────────


def _build_tool_command(tool: str, *args: str) -> list[str]:
    if _FROZEN:
        return [sys.executable, tool, *args]
    return [sys.executable, "-u", "-m", tool, *args]


def _build_tool_worker_command(tool: str, *args: str) -> list[str]:
    if _FROZEN:
        return [sys.executable, _INTERNAL_WORKER_MARKER, tool, *args]
    return [
        sys.executable,
        "-u",
        str(_ENTRY_SCRIPT),
        _INTERNAL_WORKER_MARKER,
        tool,
        *args,
    ]


def _inject_local_esptool_pythonpath(env) -> None:
    """Inject LOCAL_ESPTOOL_DIR into *env* (QProcessEnvironment)."""
    if _FROZEN or not _local_esptool_available():
        return
    existing = env.value("PYTHONPATH", "")
    env.insert(
        "PYTHONPATH",
        str(LOCAL_ESPTOOL_DIR) + (os.pathsep + existing if existing else ""),
    )


def _build_process_env_dict() -> dict[str, str]:
    """Build an ``os.environ`` copy with LOCAL_ESPTOOL_DIR on PYTHONPATH."""
    env = os.environ.copy()
    if not _FROZEN and _local_esptool_available():
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(LOCAL_ESPTOOL_DIR) + (
            os.pathsep + existing if existing else ""
        )
    return env


# ── 杂项 ─────────────────────────────────────────────────────────────────────

def _build_reference_notice() -> str:
    return "\n".join(
        [
            f"{APP_TITLE} v{APP_VERSION}",
            f"Coder: {APP_AUTHOR}",
            f"GitHub: {APP_GITHUB_URL}",
            "",
            "引用说明",
            "- esptool: 用于芯片识别、擦除、烧录与 eFuse 操作。",
            "  来源: https://github.com/espressif/esptool",
            "- PyQt6: 用于桌面图形界面。",
            "  来源: https://www.riverbankcomputing.com/software/pyqt/",
            "- pyserial: 用于串口枚举与串口访问。",
            "  来源: https://github.com/pyserial/pyserial",
            "- PyInstaller: 用于构建 Windows 可执行文件。",
            "  来源: https://pyinstaller.org/",
            "",
            "分发本工具时，请一并保留各上游项目的许可证与引用说明。",
            "详细清单见仓库根目录的 THIRD_PARTY_NOTICES.md。",
        ]
    )


def _resolve_build_timestamp_text() -> str:
    if _FROZEN:
        # PyInstaller exe 的 mtime 准确反映构建时间
        try:
            timestamp = Path(sys.executable).stat().st_mtime
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
        except OSError:
            return "未知时间"
    # 开发环境：源码 mtime 随 git clone / 文件拷贝变化，不可靠
    return "开发版本"


@lru_cache(maxsize=1)
def _load_packaged_avatar() -> bytes | None:
    avatar_path = _resolve_packaged_avatar_path()
    try:
        if avatar_path.is_file():
            return avatar_path.read_bytes()
    except Exception:
        return None
    return None


@lru_cache(maxsize=1)
def _download_github_avatar() -> bytes | None:
    if not APP_GITHUB_AVATAR_URL:
        return None
    from urllib.request import Request, urlopen

    try:
        request = Request(
            APP_GITHUB_AVATAR_URL,
            headers={"User-Agent": f"{APP_TITLE}/{APP_VERSION}"},
        )
        with urlopen(request, timeout=1) as response:
            return response.read()
    except Exception:
        return None


# ── eFuse 预设配置 ────────────────────────────────────────────────────────────

def _load_efuse_presets() -> dict[str, list[tuple[str, str, str, str]]]:
    """从 config.yaml 加载 eFuse 快捷预设，格式：{chip_key: [(label, name, value, description), ...]}。
    加载失败时返回内置默认值。"""
    import yaml  # PyYAML，在 requirements.txt 中明确列出

    _CONFIG_PATH = _resource_root() / "config.yaml"
    try:
        if _CONFIG_PATH.is_file():
            with _CONFIG_PATH.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            raw = data.get("efuse_presets") or {}
            result: dict[str, list[tuple[str, str, str, str]]] = {}
            for chip_key, entries in raw.items():
                if not isinstance(entries, list):
                    continue
                presets: list[tuple[str, str, str, str]] = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    label = str(entry.get("label", "")).strip()
                    name = str(entry.get("name", "")).strip()
                    value = str(entry.get("value", "")).strip()
                    description = str(entry.get("description", "")).strip()
                    if label and name and value:
                        presets.append((label, name, value, description))
                if presets:
                    result[str(chip_key).lower()] = presets
            return result
    except Exception:
        pass
    # 内置回退默认值
    return {
        "esp32-p4": [
            ("切换内置 USB PHY 至 USB-OTG 1.1", "USB_PHY_SEL", "1", ""),
        ],
    }


# 模块级常量，导入即可使用
EFUSE_CHIP_PRESETS: dict[str, list[tuple[str, str, str, str]]] = _load_efuse_presets()
