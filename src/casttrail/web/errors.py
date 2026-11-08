from typing import TYPE_CHECKING

from casttrail.web.routes import templates

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.responses import HTMLResponse


class PageError(Exception):
    """A request failed in a way the person should be told about on a page."""

    def __init__(self, message: str, *, status_code: int) -> None:
        """Create the error.

        Args:
            message: What to tell the person. It must hold no secrets.
            status_code: The HTTP status to answer with.
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def render_page_error(request: Request, error: Exception) -> HTMLResponse:
    """Show a ``PageError`` as a small page.

    Args:
        request: The current request.
        error: The ``PageError``.

    Returns:
        The page, with the error's status code.
    """
    assert isinstance(error, PageError)
    return templates.TemplateResponse(
        request,
        "message.html",
        {"message": error.message},
        status_code=error.status_code,
    )
