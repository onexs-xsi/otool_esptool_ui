"""OTool Esptool UI — ESP 多设备烧录工具

Entry point for direct script execution and worker subprocess re-invocation.

For package usage::

    import otool_esptool_ui
    otool_esptool_ui.main()
"""

__version__ = "0.4.0"
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

from src.main_window import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
