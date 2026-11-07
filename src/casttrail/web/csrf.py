from urllib.parse import urlsplit

from fastapi import HTTPException, Request

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _own_origin(request: Request) -> str:
    host = request.headers.get("host", request.url.netloc)
    return f"{request.url.scheme}://{host}".lower()


def _claimed_origin(origin: str) -> str | None:
    """Return ``scheme://host[:port]`` for an Origin value, or ``None`` if malformed."""
    parts = urlsplit(origin)
    if not parts.scheme or not parts.netloc or parts.path not in ("", "/"):
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


async def verify_same_origin(request: Request) -> None:
    """Refuse an unsafe request that did not come from this app's own pages.

    The browser's ``Sec-Fetch-Site`` header decides first, because page script cannot
    set it: ``same-origin`` passes and ``cross-site`` is refused. Otherwise the
    ``Origin`` header must equal the app's own origin. A request with neither header
    is refused too: every browser sends one of them on a form POST, so only a
    non-browser client lacks both. This covers sign-in as well as signed-in actions,
    which stops a malicious site signing a visitor in to an attacker's account.

    Args:
        request: The incoming request.

    Raises:
        HTTPException: With status 403 if the request is not from this site.
    """
    if request.method in _SAFE_METHODS:
        return
    site = request.headers.get("sec-fetch-site")
    if site == "same-origin":
        return
    if site != "cross-site":
        origin = request.headers.get("origin")
        if origin is not None and _claimed_origin(origin) == _own_origin(request):
            return
    raise HTTPException(status_code=403, detail="Cross-site request refused.")
