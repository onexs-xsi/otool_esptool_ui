"""OTool Esptool UI — ESP 多设备烧录工具

A multi-device ESP chip flashing tool with PyQt6 GUI.

Usage as a package::

    import otool_esptool_ui
    otool_esptool_ui.main()

Usage as a script::

    python otool_esptool_ui.py

Usage as a module::

    python -m otool_esptool_ui
"""

from .src.constants import APP_TITLE, APP_VERSION, APP_VERSION_WIN, APP_AUTHOR, APP_GITHUB_URL

__version__ = APP_VERSION
__author__ = APP_AUTHOR
__title__ = APP_TITLE

__all__ = [
    "APP_TITLE",
    "APP_VERSION",
    "APP_VERSION_WIN",
    "APP_AUTHOR",
    "APP_GITHUB_URL",
    "main",
]


def main() -> int:
    """启动 OTool Esptool UI 图形界面"""
    from .src.bootstrap import (
        frozen_esptool_dispatch,
        dispatch_internal_worker,
        configure_qt_dll_path,
    )

    frozen_esptool_dispatch()
    dispatch_internal_worker()
    configure_qt_dll_path()

    from .src.main_window import main as _main

    return _main()
