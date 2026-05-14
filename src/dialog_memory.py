"""会话级文件对话框目录记忆。"""

from pathlib import Path

from PyQt6.QtWidgets import QFileDialog, QWidget

from .constants import DEFAULT_FIRMWARE_DIR, TOOL_DIR

_last_dialog_dir: Path | None = None


def _default_dialog_dir() -> Path:
    """第一次打开对话框的目录：优先 firmware/README.md 所在目录，否则程序目录。"""
    firmware_readme = DEFAULT_FIRMWARE_DIR / "README.md"
    if firmware_readme.is_file():
        return DEFAULT_FIRMWARE_DIR
    return TOOL_DIR


def current_dialog_dir() -> Path:
    if _last_dialog_dir is not None and _last_dialog_dir.exists():
        return _last_dialog_dir
    return _default_dialog_dir()


def remember_dialog_path(path: str | Path) -> None:
    """记住用户最后一次选择的文件或目录所在目录，仅在本次进程内有效。"""
    global _last_dialog_dir
    if not path:
        return
    selected = Path(path)
    directory = selected if selected.is_dir() else selected.parent
    if directory.exists():
        _last_dialog_dir = directory.resolve()


def remembered_start_path(default_name: str = "") -> str:
    base = current_dialog_dir()
    return str(base / default_name) if default_name else str(base)


def get_open_file_name(
    parent: QWidget | None,
    caption: str,
    file_filter: str = "All Files (*.*)",
    default_name: str = "",
) -> tuple[str, str]:
    path, selected_filter = QFileDialog.getOpenFileName(
        parent,
        caption,
        remembered_start_path(default_name),
        file_filter,
    )
    if path:
        remember_dialog_path(path)
    return path, selected_filter


def get_save_file_name(
    parent: QWidget | None,
    caption: str,
    default_name: str = "",
    file_filter: str = "All Files (*.*)",
) -> tuple[str, str]:
    path, selected_filter = QFileDialog.getSaveFileName(
        parent,
        caption,
        remembered_start_path(default_name),
        file_filter,
    )
    if path:
        remember_dialog_path(path)
    return path, selected_filter


def get_existing_directory(parent: QWidget | None, caption: str) -> str:
    directory = QFileDialog.getExistingDirectory(
        parent,
        caption,
        remembered_start_path(),
    )
    if directory:
        remember_dialog_path(directory)
    return directory
