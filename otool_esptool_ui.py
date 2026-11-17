"""OTool Esptool UI — ESP 烧录、eFuse、校验、固件分合与串口终端工作台

Entry point for direct script execution and worker subprocess re-invocation.

For package usage::

    import otool_esptool_ui
    otool_esptool_ui.main()
"""

import sys

__version__ = "0.7.3"
__author__ = "ONEXS"
__title__ = "OTool Esptool UI | byonexs."

# ── Bootstrap dispatch ────────────────────────────────────────────────────────
# Must execute before any PyQt6 import.
# Worker and frozen-exe sub-processes may sys.exit() here.

from src.bootstrap import (
    frozen_esptool_dispatch,
    dispatch_internal_worker,
    configure_qt_dll_path,
)

frozen_esptool_dispatch()
dispatch_internal_worker()
configure_qt_dll_path()

# ── Application entry ────────────────────────────────────────────────────────

_QT_STARTUP_SMOKE_ARG = "__otool_qt_startup_smoke__"
_qt_startup_smoke = (
    getattr(sys, "frozen", False) and sys.argv[1:] == [_QT_STARTUP_SMOKE_ARG]
)

try:
    from src.main_window import main  # noqa: E402
except Exception:
    if _qt_startup_smoke:
        raise SystemExit(1) from None
    raise

if _qt_startup_smoke:
    raise SystemExit(0)

if __name__ == "__main__":
    raise SystemExit(main())
