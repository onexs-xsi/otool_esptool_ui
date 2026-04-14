# -*- mode: python ; coding: utf-8 -*-
import re
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_all

TOOL_DIR = Path(SPECPATH)

# ── 从入口脚本解析版本/作者/标题（单一来源）──────────────────────────────────

def _parse_entry_metadata() -> dict:
    """Read __version__, __author__, __title__ from the entry script."""
    text = (TOOL_DIR / "otool_esptool_ui.py").read_text(encoding="utf-8")
    meta = {}
    for m in re.finditer(
        r'^__(version|author|title)__\s*=\s*["\'](.+?)["\']', text, re.MULTILINE
    ):
        meta[m.group(1)] = m.group(2)
    return meta

_META = _parse_entry_metadata()
_APP_VERSION = _META.get("version", "0.0.0")
_APP_AUTHOR = _META.get("author", "ONEXS")
_APP_TITLE = _META.get("title", "OTool Esptool UI")

def _version_tuple(ver: str) -> tuple:
    """'0.1.0' -> (0, 1, 0, 0)"""
    parts = [int(x) for x in ver.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])

_VER_TUPLE = _version_tuple(_APP_VERSION)
_VER_WIN = ".".join(str(x) for x in _VER_TUPLE)
LOCAL_ESPTOOL_ROOT = TOOL_DIR / "esptool"

if str(LOCAL_ESPTOOL_ROOT.resolve()) not in sys.path:
    sys.path.insert(0, str(LOCAL_ESPTOOL_ROOT.resolve()))

# ── 不吝空间，把所有相关包的 data + submodules + binaries 全部打包 ──

_PACKAGES = [
    "esptool",
    "espefuse",
    "espsecure",
    "serial",
    "bitstring",
]

added_datas = []
added_binaries = []
hidden_imports = []

for pkg in _PACKAGES:
    try:
        d, b, h = collect_all(pkg)
        added_datas += d
        added_binaries += b
        hidden_imports += h
    except Exception:
        # collect_all 失败则 fallback 分別收集
        try:
            added_datas += collect_data_files(pkg)
        except Exception:
            pass
        try:
            hidden_imports += collect_submodules(pkg)
        except Exception:
            pass

# src 子包
try:
    hidden_imports += collect_submodules("src")
except Exception:
    hidden_imports += [
        "src",
        "src.bootstrap",
        "src.constants",
        "src.helpers",
        "src.models",
        "src.flow_layout",
        "src.efuse_dialog",
        "src.device_card",
        "src.main_window",
    ]

# 额外确保这些常遗漏的模块
hidden_imports += [
    "rich_click",
    "bitstring.bitstore_bitarray",
    "bitstring.bitstore_bitarray_helpers",
    "bitstring.bitstore_common_helpers",
    "bitstring.bitstore_tibs",
    "bitstring.bitstore_tibs_helpers",
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
    "serial.tools.list_ports_common",
    "packaging",
    "packaging.version",
    "packaging.specifiers",
    "packaging.requirements",
]

custom_datas = [
    (str(TOOL_DIR / "THIRD_PARTY_NOTICES.md"), "."),
    (str(TOOL_DIR / "assets" / "onexs_avatar.png"), "assets"),
    (str(TOOL_DIR / "config.yaml"), "."),
    (str(TOOL_DIR / "src" / "assets" / "chevron_down.svg"), "src/assets"),
    (str(TOOL_DIR / "logo_all_size.ico"), "."),
    (str(TOOL_DIR / "otool_esptool_ui.py"), "."),  # 运行时读取版本号
]

# ── 自动生成 file_version_info.txt（从入口脚本读取版本号） ───────────────────────

_VERSION_INFO_TEMPLATE = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_VER_TUPLE},
    prodvers={_VER_TUPLE},
    mask=0x3F,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          "040904B0",
          [
            StringStruct("CompanyName", "{_APP_AUTHOR}"),
            StringStruct("FileDescription", "{_APP_TITLE}"),
            StringStruct("FileVersion", "{_VER_WIN}"),
            StringStruct("InternalName", "otool_esptool_ui"),
            StringStruct("Comments", "Author: {_APP_AUTHOR}"),
            StringStruct("OriginalFilename", "otool_esptool_ui.exe"),
            StringStruct("ProductName", "{_APP_TITLE}"),
            StringStruct("ProductVersion", "{_VER_WIN}")
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct("Translation", [1033, 1200])])
  ]
)
"""

_version_info_path = TOOL_DIR / "file_version_info.txt"
_version_info_path.write_text(_VERSION_INFO_TEMPLATE, encoding="utf-8")
print(f"[spec] Generated file_version_info.txt  version={_VER_WIN}")

a = Analysis(
    [str(TOOL_DIR / "otool_esptool_ui.py")],
    pathex=[str(TOOL_DIR), str(LOCAL_ESPTOOL_ROOT)],
    binaries=added_binaries,
    datas=custom_datas + added_datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="otool_esptool_ui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    # Use the default OS temp directory so onefile extraction does not leave _MEI
    # folders inside the project tree.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(TOOL_DIR / "file_version_info.txt"),
    icon=[str(TOOL_DIR / "logo_all_size.ico")],
)

