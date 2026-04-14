"""Allow running as: python -m otool_esptool_ui"""

from .src.bootstrap import (
    frozen_esptool_dispatch,
    dispatch_internal_worker,
    configure_qt_dll_path,
)

frozen_esptool_dispatch()
dispatch_internal_worker()
configure_qt_dll_path()

from .src.main_window import main  # noqa: E402

raise SystemExit(main())
