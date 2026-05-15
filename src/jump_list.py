"""Windows 任务栏跳转列表（Jump List）支持。

使用纯 ctypes 直接调用 Windows COM vtable，无需 comtypes，
与 PyInstaller frozen 环境完全兼容。

注册的任务项：
  - 新建窗口     : 再次启动同一 exe（仅打包后生效）
  - 打开固件目录 : 用文件管理器打开固件文件夹（路径存在时才注册）

非 Windows 平台、开发模式（python.exe）或调用失败时均静默跳过。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import (
    POINTER, WINFUNCTYPE, Structure, Union, byref,
    c_int, c_uint, c_uint8, c_uint16, c_uint32, c_ushort, c_void_p, c_wchar_p,
)
from pathlib import Path

# ── Windows COM 基础类型 ──────────────────────────────────────────────────────

HRESULT = ctypes.HRESULT
_PTR_SIZE = ctypes.sizeof(c_void_p)


class GUID(Structure):
    _fields_ = [
        ("Data1", c_uint32),
        ("Data2", c_uint16),
        ("Data3", c_uint16),
        ("Data4", c_uint8 * 8),
    ]

    def __init__(self, s: str = "") -> None:
        super().__init__()
        if not s:
            return
        s = s.strip("{} ")
        p = s.split("-")
        self.Data1 = int(p[0], 16)
        self.Data2 = int(p[1], 16)
        self.Data3 = int(p[2], 16)
        b = bytes.fromhex(p[3] + p[4])
        for i, v in enumerate(b):
            self.Data4[i] = v


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_uint32)]


class _PVUnion(Union):
    # union 最大成员为 DECIMAL（16 bytes），确保结构体总大小为 24 bytes
    _fields_ = [("pwszVal", c_wchar_p), ("_bytes", c_uint8 * 16)]


class PROPVARIANT(Structure):
    _fields_ = [
        ("vt",  c_uint16), ("_r1", c_uint16), ("_r2", c_uint16), ("_r3", c_uint16),
        ("_val", _PVUnion),
    ]


VT_LPWSTR = 31

# ── COM 底层辅助 ──────────────────────────────────────────────────────────────

_ole32 = ctypes.windll.ole32 if sys.platform == "win32" else None  # type: ignore[attr-defined]


def _co_create(clsid_str: str, iid_str: str) -> c_void_p:
    clsid, iid, ptr = GUID(clsid_str), GUID(iid_str), c_void_p()
    hr = _ole32.CoCreateInstance(byref(clsid), None, 1, byref(iid), byref(ptr))
    if hr < 0:
        raise OSError(f"CoCreateInstance {clsid_str}: 0x{hr & 0xFFFFFFFF:08X}")
    return ptr


def _vtbl(ptr: c_void_p, index: int) -> int:
    """从 COM 对象指针读取第 index 个 vtable 函数地址。"""
    vtbl_addr = c_void_p.from_address(ptr.value).value          # 读 vptr
    return c_void_p.from_address(vtbl_addr + index * _PTR_SIZE).value  # 读函数指针


def _qif(ptr: c_void_p, iid_str: str) -> c_void_p:
    """QueryInterface (vtable[0])。"""
    iid, out = GUID(iid_str), c_void_p()
    fn = WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))(_vtbl(ptr, 0))
    hr = fn(ptr, byref(iid), byref(out))
    if hr < 0:
        raise OSError(f"QueryInterface {iid_str}: 0x{hr & 0xFFFFFFFF:08X}")
    return out


def _release(ptr: c_void_p) -> None:
    """Release (vtable[2])。"""
    if ptr and ptr.value:
        WINFUNCTYPE(c_uint, c_void_p)(_vtbl(ptr, 2))(ptr)


def _chk(hr: int, label: str = "") -> None:
    if hr < 0:
        raise OSError(f"COM 0x{hr & 0xFFFFFFFF:08X}" + (f" [{label}]" if label else ""))

# ── ICustomDestinationList vtable 封装 ───────────────────────────────────────
# vtable[0]=QI  [1]=AddRef  [2]=Release  [3]=SetAppID  [4]=BeginList
# [5]=AppendCategory  [6]=AppendKnownCategory  [7]=AddUserTasks  [8]=CommitList

def _dest_set_app_id(p: c_void_p, app_id: str) -> None:
    _chk(WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p)(_vtbl(p, 3))(p, app_id), "SetAppID")


def _dest_begin_list(p: c_void_p) -> None:
    iid_obj_array = GUID("{92CA9DCD-5622-4BBA-A805-5E9F541BD8C9}")
    slots, ppv = c_uint(), c_void_p()
    fn = WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_uint), POINTER(GUID), POINTER(c_void_p))(
        _vtbl(p, 4)
    )
    _chk(fn(p, byref(slots), byref(iid_obj_array), byref(ppv)), "BeginList")
    if ppv.value:
        _release(ppv)   # 释放 removed destinations（不需要其内容）


def _dest_add_user_tasks(p: c_void_p, coll: c_void_p) -> None:
    _chk(WINFUNCTYPE(HRESULT, c_void_p, c_void_p)(_vtbl(p, 7))(p, coll), "AddUserTasks")


def _dest_commit(p: c_void_p) -> None:
    _chk(WINFUNCTYPE(HRESULT, c_void_p)(_vtbl(p, 8))(p), "CommitList")

# ── IObjectCollection vtable 封装 ────────────────────────────────────────────
# vtable[3]=GetCount  [4]=GetAt  [5]=AddObject  [6]=AddFromArray  ...

def _coll_add(p: c_void_p, obj: c_void_p) -> None:
    _chk(WINFUNCTYPE(HRESULT, c_void_p, c_void_p)(_vtbl(p, 5))(p, obj), "AddObject")

# ── IShellLinkW vtable 封装 ──────────────────────────────────────────────────
# vtable[3]=GetPath  [4]=GetIDList  [5]=SetIDList  [6]=GetDescription
# [7]=SetDescription  [8]=GetWorkingDirectory  [9]=SetWorkingDirectory
# [10]=GetArguments  [11]=SetArguments  [12]=GetHotkey  [13]=SetHotkey
# [14]=GetShowCmd  [15]=SetShowCmd  [16]=GetIconLocation
# [17]=SetIconLocation  [18]=SetRelativePath  [19]=Resolve  [20]=SetPath

def _link_set_description(p: c_void_p, s: str) -> None:
    _chk(WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p)(_vtbl(p, 7))(p, s), "SetDescription")


def _link_set_arguments(p: c_void_p, s: str) -> None:
    _chk(WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p)(_vtbl(p, 11))(p, s), "SetArguments")


def _link_set_working_dir(p: c_void_p, s: str) -> None:
    _chk(WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p)(_vtbl(p, 9))(p, s), "SetWorkingDirectory")


def _link_set_icon_location(p: c_void_p, icon_path: str, idx: int) -> None:
    _chk(
        WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int)(_vtbl(p, 17))(p, icon_path, idx),
        "SetIconLocation",
    )


def _link_set_path(p: c_void_p, s: str) -> None:
    _chk(WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p)(_vtbl(p, 20))(p, s), "SetPath")

# ── IPropertyStore vtable 封装 ───────────────────────────────────────────────
# vtable[3]=GetCount  [4]=GetAt  [5]=GetValue  [6]=SetValue  [7]=Commit

def _store_set_value(p: c_void_p, pkey: PROPERTYKEY, pv: PROPVARIANT) -> None:
    fn = WINFUNCTYPE(HRESULT, c_void_p, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(
        _vtbl(p, 6)
    )
    _chk(fn(p, byref(pkey), byref(pv)), "SetValue")


def _store_commit(p: c_void_p) -> None:
    _chk(WINFUNCTYPE(HRESULT, c_void_p)(_vtbl(p, 7))(p), "Commit")

# ── PKEY_Title ────────────────────────────────────────────────────────────────

def _make_pkey_title() -> PROPERTYKEY:
    pk = PROPERTYKEY()
    pk.fmtid = GUID("{F29F85E0-4FF9-1068-AB91-08002B27B3D9}")
    pk.pid = 2
    return pk


def _make_pkey_app_user_model_id() -> PROPERTYKEY:
    pk = PROPERTYKEY()
    pk.fmtid = GUID("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}")
    pk.pid = 5
    return pk


_PKEY_TITLE = _make_pkey_title() if sys.platform == "win32" else None  # type: ignore[assignment]
_PKEY_APP_USER_MODEL_ID = (
    _make_pkey_app_user_model_id() if sys.platform == "win32" else None
)  # type: ignore[assignment]

# ── CLSIDs / IIDs ─────────────────────────────────────────────────────────────

_CLSID_DEST_LIST  = "{77F10CF0-3DB5-4966-B520-B7C54FD35ED6}"
_IID_DEST_LIST    = "{6332DEBF-87B5-4670-90C0-5E57B408A49E}"
_CLSID_ENUM_COLL  = "{2D3468C1-36A7-43B6-AC24-D3F02FD9607A}"
_IID_OBJ_COLL     = "{5632B1A4-E38A-400A-928A-D4CD63230295}"
_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
_IID_SHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
_IID_PROP_STORE   = "{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}"

# ── 公开 API ──────────────────────────────────────────────────────────────────


def setup_jump_list(
    app_id: str,
    exe_path: str | Path,
    tasks: list[tuple[str, str]] | None = None,
    *,
    script_path: str | Path | None = None,
    icon_path: str | Path | None = None,
) -> None:
    """向 Windows 任务栏注册跳转列表任务（仅 frozen 打包模式生效）。

    tasks: [(标题, 命令行参数字符串), ...]，例如：
        [("烧录", ""), ("合成", "--tab 3"), ("终端", "--tab 4")]
    """
    if sys.platform != "win32" or not tasks:
        return
    try:
        launcher, prepared_tasks, working_dir, icon = _prepare_links(
            exe_path=Path(exe_path),
            tasks=tasks,
            script_path=Path(script_path) if script_path else None,
            icon_path=Path(icon_path) if icon_path else None,
        )
        _register(app_id, launcher, prepared_tasks, working_dir, icon)
    except Exception:
        pass  # 非关键功能，静默跳过


_PYTHON_LAUNCHER_STEMS = {"python", "python3", "pythonw", "pythonw3"}


def _is_python_launcher(path: Path) -> bool:
    return path.stem.lower() in _PYTHON_LAUNCHER_STEMS


def _prefer_pythonw(path: Path) -> Path:
    stem = path.stem.lower()
    if stem.startswith("pythonw"):
        return path
    candidate_stem = "pythonw3" if stem == "python3" else "pythonw"
    candidate = path.with_name(candidate_stem + path.suffix)
    return candidate if candidate.exists() else path


def _join_arguments(prefix: list[str], extra: str) -> str:
    base = subprocess.list2cmdline(prefix)
    extra = extra.strip()
    return f"{base} {extra}" if extra else base


def _prepare_links(
    exe_path: Path,
    tasks: list[tuple[str, str]],
    script_path: Path | None,
    icon_path: Path | None,
) -> tuple[Path, list[tuple[str, str]], Path, Path]:
    exe = exe_path.resolve()
    icon = (icon_path or exe).resolve()
    if not icon.exists():
        icon = exe

    if not _is_python_launcher(exe):
        return exe, tasks, exe.parent, icon

    script = (script_path or Path(sys.argv[0])).resolve()
    if not script.is_file():
        raise FileNotFoundError(script)

    launcher = _prefer_pythonw(exe)
    prepared = [(title, _join_arguments([str(script)], args)) for title, args in tasks]
    return launcher, prepared, script.parent, icon


def _register(
    app_id: str,
    exe: Path,
    tasks: list[tuple[str, str]],
    working_dir: Path,
    icon_path: Path,
) -> None:
    _ole32.CoInitialize(None)   # Qt 已初始化则返回 S_FALSE，忽略即可

    dest_list = _co_create(_CLSID_DEST_LIST, _IID_DEST_LIST)
    try:
        _dest_set_app_id(dest_list, app_id)
        _dest_begin_list(dest_list)

        coll = _co_create(_CLSID_ENUM_COLL, _IID_OBJ_COLL)
        try:
            exe_str = str(exe)
            working_dir_str = str(working_dir)
            icon_str = str(icon_path)
            for title, args in tasks:
                lnk = _make_link(exe_str, args, title, working_dir_str, icon_str, app_id)
                _coll_add(coll, lnk)
                _release(lnk)

            _dest_add_user_tasks(dest_list, coll)
        finally:
            _release(coll)

        _dest_commit(dest_list)
    finally:
        _release(dest_list)


def _set_string_property(store: c_void_p, pkey: PROPERTYKEY, value: str) -> None:
    pv = PROPVARIANT()
    pv.vt = VT_LPWSTR
    pv._val.pwszVal = value
    _store_set_value(store, pkey, pv)


def _make_link(
    path: str,
    args: str,
    title: str,
    working_dir: str,
    icon_path: str,
    app_id: str,
) -> c_void_p:
    lnk = _co_create(_CLSID_SHELL_LINK, _IID_SHELL_LINK_W)
    _link_set_path(lnk, path)
    _link_set_working_dir(lnk, working_dir)
    if args:
        _link_set_arguments(lnk, args)
    _link_set_description(lnk, title)
    _link_set_icon_location(lnk, icon_path, 0)

    # 通过 IPropertyStore 写入显示标题（Windows 用此字段渲染 Jump List 任务名）
    store = _qif(lnk, _IID_PROP_STORE)
    try:
        _set_string_property(store, _PKEY_TITLE, title)  # type: ignore[arg-type]
        _set_string_property(store, _PKEY_APP_USER_MODEL_ID, app_id)  # type: ignore[arg-type]
        _store_commit(store)
    finally:
        _release(store)

    return lnk
