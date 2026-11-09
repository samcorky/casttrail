import time
import uuid
from typing import TYPE_CHECKING

import structlog
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from fastapi import Request, Response
    from starlette.middleware.base import RequestResponseEndpoint

logger = structlog.get_logger(__name__)

# Answered often and not interesting: logged at debug unless they go wrong.
_QUIET_PREFIXES = ("/static/", "/health", "/favicon.ico")
_ID_HEADER = "X-Request-ID"
_MAX_ID = 64


def _request_id(request: Request) -> str:
    """Use the caller's request id if it is a short plain token, else make one."""
    given = request.headers.get(_ID_HEADER, "")
    if given and len(given) <= _MAX_ID and given.isascii() and given.isalnum():
        return given
    return uuid.uuid4().hex[:12]


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Logs every request once it is answered, and tags what the app logs meanwhile.

    The line has the method, the path, the status, how long it took and who it was for.
    The query string, headers, cookies and body are never logged, because a search term
    or a form can hold something private. Each request gets an id, put on every line the
    app logs while handling it and sent back as ``X-Request-ID``.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Answer the request, then log it.

        Args:
            request: The incoming request.
            call_next: The rest of the app.

        Returns:
            The app's answer, with the request id added.
        """
        request_id = _request_id(request)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        base = {
            "method": request.method,
            "path": request.url.path,
            "client": request.client.host if request.client else None,
        }
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed",
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                user_id=getattr(request.state, "user_id", None),
                **base,
            )
            raise
        status = response.status_code
        quiet = request.url.path.startswith(_QUIET_PREFIXES) and status < 400
        log = logger.debug if quiet else logger.error if status >= 500 else logger.info
        log(
            "request",
            status=status,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            user_id=getattr(request.state, "user_id", None),
            **base,
        )
        response.headers[_ID_HEADER] = request_id
        return response
