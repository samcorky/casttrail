from pathlib import Path

from fastapi.staticfiles import StaticFiles

WEB_DIR = Path(__file__).parent


def static_files() -> StaticFiles:
    """Return the handler that serves the vendored CSS and JavaScript.

    Returns:
        A ``StaticFiles`` app rooted at ``web/static``.
    """
    return StaticFiles(directory=WEB_DIR / "static")
