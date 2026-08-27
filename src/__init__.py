from .constants import APP_TITLE, APP_VERSION, APP_VERSION_WIN, APP_AUTHOR, APP_GITHUB_URL

__all__ = ["APP_TITLE", "APP_VERSION", "APP_VERSION_WIN", "APP_AUTHOR", "APP_GITHUB_URL", "main"]


def main() -> int:
    """Start the GUI without importing PyQt during bootstrap package loading."""
    from .main_window import main as _main

    return _main()
