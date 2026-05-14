import os
import sys
import json
import importlib
import importlib.util
import site
import traceback
from pathlib import Path

from .constants import (
    _INTERNAL_WORKER_MARKER,
    _INTERNAL_WORKER_EVENT_PREFIX,
    LOCAL_ESPTOOL_DIR,
    _local_esptool_available,
)


def frozen_esptool_dispatch() -> None:
    """When packaged as a onefile .exe, re-route esptool sub-process calls.

    Without this, sys.executable == the GUI exe, so every subprocess spawned
    to run esptool.py would re-launch the full GUI -> infinite windows.

    NOTE: Do NOT use runpy.run_path() here. run_path() prepends the script's
    directory to sys.path, which makes ``import esptool`` inside esptool.py
    resolve to the script FILE itself instead of the frozen package -- the
    script module has no _main() and crashes immediately.
    Instead, import the frozen package directly and call its entry point.
    """
    if not getattr(sys, 'frozen', False):
        return
    if len(sys.argv) < 2:
        return
    script_name = Path(sys.argv[1]).name.lower()
    _dispatch = {
        'esptool': 'esptool',
        'esptool.py':  'esptool',
        'espefuse': 'espefuse',
        'espefuse.py': 'espefuse',
        'espsecure': 'espsecure',
        'espsecure.py': 'espsecure',
    }
    pkg_name = _dispatch.get(script_name)
    if pkg_name is None:
        return
    # Re-map argv so the package entry point sees the canonical module name,
    # which avoids upstream deprecation warnings for legacy *.py wrappers.
    sys.argv = [pkg_name] + sys.argv[2:]
    pkg = importlib.import_module(pkg_name)
    try:
        pkg._main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[esptool dispatch error] {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


def dispatch_internal_worker() -> None:
    """Handle worker subprocess mode (__otool_worker__)."""
    if len(sys.argv) < 3 or sys.argv[1] != _INTERNAL_WORKER_MARKER:
        return

    if not getattr(sys, "frozen", False) and _local_esptool_available():
        local_root = str(LOCAL_ESPTOOL_DIR)
        if local_root not in sys.path:
            sys.path.insert(0, local_root)

    def emit_worker_event(event: dict[str, object]) -> None:
        print(
            f"{_INTERNAL_WORKER_EVENT_PREFIX}{json.dumps(event, ensure_ascii=False)}",
            flush=True,
        )

    # Emit a startup marker so the parent UI immediately knows the worker path is active.
    emit_worker_event({"type": "log", "level": "info", "text": "[Worker] 工作进程已启动"})

    from esptool.logger import TemplateLogger, log as esptool_log
    from esptool.util import FatalError as EsptoolFatalError

    class WorkerLogger(TemplateLogger):
        # NOTE: set_logger() uses __class__ reassignment, not instance copy.
        # __init__ runs on a throwaway instance, not on `log` itself.
        # _line_buffer is initialized on `log` after set_logger() below.

        def _emit_log_line(self, text: str, level: str = "info") -> None:
            emit_worker_event({"type": "log", "level": level, "text": text})

        def _feed(self, text: str, end: str = "\n", level: str = "info") -> None:
            self._line_buffer += text + end
            while "\n" in self._line_buffer:
                line, self._line_buffer = self._line_buffer.split("\n", 1)
                self._emit_log_line(line, level)
            # Emit partial content immediately (e.g. "Connecting...." dots with end="")
            if self._line_buffer:
                self._emit_log_line(self._line_buffer, level)
                self._line_buffer = ""

        def flush_pending(self) -> None:
            if self._line_buffer:
                self._emit_log_line(self._line_buffer)
                self._line_buffer = ""

        def print(self, *args, **kwargs):
            self._feed("".join(map(str, args)), kwargs.get("end", "\n"), "info")

        def note(self, message: str):
            self._feed(f"Note: {message}", "\n", "note")

        def warning(self, message: str):
            self._feed(f"Warning: {message}", "\n", "warning")

        def error(self, message: str):
            self._feed(message, "\n", "error")

        def stage(self, finish: bool = False):
            if finish:
                self.flush_pending()
            emit_worker_event({"type": "stage", "finish": finish})

        def progress_bar(
            self,
            cur_iter: int,
            total_iters: int,
            prefix: str = "",
            suffix: str = "",
            bar_length: int = 30,
        ):
            percent = 0 if total_iters <= 0 else int((cur_iter / total_iters) * 100)
            emit_worker_event(
                {
                    "type": "progress",
                    "current": cur_iter,
                    "total": total_iters,
                    "percent": percent,
                    "prefix": prefix,
                    "suffix": suffix,
                    "bar_length": bar_length,
                }
            )

        def set_verbosity(self, verbosity: str):
            emit_worker_event({"type": "verbosity", "value": verbosity})

    esptool_log.set_logger(WorkerLogger())
    esptool_log._line_buffer = ""

    tool_name = sys.argv[2]
    tool_args = sys.argv[3:]
    exit_code = 0
    try:
        module = importlib.import_module(tool_name)
        if hasattr(module, "main"):
            module.main(tool_args)
        else:
            saved_argv = sys.argv[:]
            try:
                sys.argv = [tool_name] + tool_args
                module._main()
            finally:
                sys.argv = saved_argv
    except SystemExit as exc:
        if isinstance(exc.code, int):
            exit_code = exc.code
        elif exc.code:
            exit_code = 1
    except EsptoolFatalError as exc:
        emit_worker_event(
            {
                "type": "fatal",
                "text": str(exc).rstrip(),
            }
        )
        exit_code = 1
    except BaseException as exc:
        emit_worker_event(
            {
                "type": "fatal",
                "text": "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ).rstrip(),
            }
        )
        exit_code = 1
    finally:
        if hasattr(esptool_log, "flush_pending"):
            esptool_log.flush_pending()
        sys.stdout.flush()
    # Use os._exit() instead of sys.exit() to avoid blocking on non-daemon threads
    # that serial / USB libraries may leave running after the flash completes.
    os._exit(exit_code)


def configure_qt_dll_path() -> None:
    """Configure Qt DLL search paths for PyQt6 on Windows."""
    if not hasattr(os, "add_dll_directory"):
        return

    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_candidate(pyqt6_root: Path | None) -> None:
        if pyqt6_root is None:
            return
        resolved_root = pyqt6_root.resolve()
        root_key = str(resolved_root)
        if root_key in seen or not resolved_root.is_dir():
            return
        seen.add(root_key)
        candidates.append(
            {
                "bin": str(resolved_root / "Qt6" / "bin"),
                "plugins": str(resolved_root / "Qt6" / "plugins"),
            }
        )

    spec = importlib.util.find_spec("PyQt6")
    if spec is not None and spec.submodule_search_locations:
        for location in spec.submodule_search_locations:
            add_candidate(Path(location))

    site_roots: list[str] = []
    try:
        site_roots.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        site_roots.append(site.getusersitepackages())
    except Exception:
        pass

    for site_root in site_roots:
        if site_root:
            add_candidate(Path(site_root) / "PyQt6")

    for base in {sys.prefix, sys.base_prefix, os.path.dirname(sys.executable)}:
        if base:
            add_candidate(Path(base) / "Lib" / "site-packages" / "PyQt6")

    for candidate in candidates:
        dll_dir = candidate["bin"]
        plugins_dir = candidate["plugins"]
        if os.path.isdir(dll_dir):
            os.add_dll_directory(dll_dir)
            if os.path.isdir(plugins_dir):
                os.environ.setdefault("QT_PLUGIN_PATH", plugins_dir)
                os.environ.setdefault(
                    "QT_QPA_PLATFORM_PLUGIN_PATH",
                    os.path.join(plugins_dir, "platforms"),
                )
            break
