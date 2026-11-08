from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from casttrail.config import Settings

# The prefix makes the browser refuse the cookie unless it is Secure, HttpOnly, has
# Path=/ and no Domain, and refuse a cookie of that name set by a sibling site.
_HOST_PREFIX = "__Host-Http-"


def _secure(request: Request, settings: Settings) -> bool:
    if settings.cookie_secure in ("always", "never"):
        return settings.cookie_secure == "always"
    return request.url.scheme == "https"


def _name(request: Request, settings: Settings, base: str) -> str:
    return _HOST_PREFIX + base if _secure(request, settings) else base


def session_cookie_name(request: Request, settings: Settings) -> str:
    """Return the session cookie's name, prefixed when the cookie is Secure.

    Args:
        request: The current request.
        settings: The application settings.

    Returns:
        ``__Host-Http-<name>`` when Secure, else the plain configured name.
    """
    return _name(request, settings, settings.cookie_name)


def device_cookie_name(request: Request, settings: Settings) -> str:
    """Return the device cookie's name, prefixed when the cookie is Secure.

    Args:
        request: The current request.
        settings: The application settings.

    Returns:
        ``__Host-Http-<name>`` when Secure, else the plain configured name.
    """
    return _name(request, settings, settings.device_cookie_name)


def _read(request: Request, name: str) -> str | None:
    """Return the value of the one cookie called ``name``, strictly.

    A missing, duplicated or disguised cookie gives ``None``. A disguised cookie has
    a name that only equals the real one once Unicode spaces are trimmed, which
    Starlette's own parser does; a planted copy must not be read as the real one.
    """
    found: list[str] = []
    for header in request.headers.getlist("cookie"):
        for chunk in header.split(";"):
            key, _, value = chunk.partition("=")
            key = key.strip(" \t")
            if key == name:
                found.append(value.strip(" \t"))
            elif key.strip() == name:
                return None
    return found[0] if len(found) == 1 else None


def read_session_token(request: Request, settings: Settings) -> str | None:
    """Return the session token from the cookie, strictly.

    Args:
        request: The current request.
        settings: The application settings.

    Returns:
        The token, or ``None``.
    """
    return _read(request, session_cookie_name(request, settings))


def read_device_token(request: Request, settings: Settings) -> str | None:
    """Return the device token from the cookie, strictly.

    Args:
        request: The current request.
        settings: The application settings.

    Returns:
        The token, or ``None``.
    """
    return _read(request, device_cookie_name(request, settings))


def set_session_cookie(
    request: Request, response: Response, token: str, settings: Settings
) -> None:
    """Set the session cookie.

    ``SameSite=Lax`` rather than ``Strict`` so a link from an ntfy alert opens the
    app already signed in. Cross-site POSTs still carry no cookie.

    Args:
        request: The current request.
        response: The response to attach the cookie to.
        token: The secret session token.
        settings: The application settings.
    """
    response.set_cookie(
        session_cookie_name(request, settings),
        token,
        max_age=settings.session_ttl_hours * 3600,
        path="/",
        secure=_secure(request, settings),
        httponly=True,
        samesite="lax",
    )


def set_device_cookie(
    request: Request, response: Response, token: str, settings: Settings
) -> None:
    """Set the long-lived cookie that marks this browser as known to the account.

    Args:
        request: The current request.
        response: The response to attach the cookie to.
        token: The secret device token.
        settings: The application settings.
    """
    response.set_cookie(
        device_cookie_name(request, settings),
        token,
        max_age=settings.device_ttl_days * 86400,
        path="/",
        secure=_secure(request, settings),
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(
    request: Request, response: Response, settings: Settings
) -> None:
    """Expire the session cookie. The device cookie is kept.

    Args:
        request: The current request.
        response: The response to attach the expiry to.
        settings: The application settings.
    """
    response.delete_cookie(
        session_cookie_name(request, settings),
        path="/",
        secure=_secure(request, settings),
        httponly=True,
        samesite="lax",
    )
