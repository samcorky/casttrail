from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from casttrail.auth.throttle import SignInThrottledError
from casttrail.web.cookies import (
    clear_session_cookie,
    read_device_token,
    read_session_token,
    set_device_cookie,
    set_session_cookie,
)
from casttrail.web.dependencies import AuthServiceDep, CurrentUserOptional, SettingsDep
from casttrail.web.routes import templates

router = APIRouter()

# One message for every refusal, so it never reveals which accounts exist.
INVALID_CREDENTIALS = "Incorrect email or password."
THROTTLED = "Too many failed attempts. Try again later."


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(request: Request, user: CurrentUserOptional) -> Response:
    """Show the sign-in form, or go home if already signed in.

    Args:
        request: The current request.
        user: The signed-in user, if any.

    Returns:
        The form, or a redirect home.
    """
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse, response_model=None)
async def login(
    request: Request,
    auth: AuthServiceDep,
    settings: SettingsDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    """Sign in with an email and password.

    Args:
        request: The current request.
        auth: The auth service.
        settings: The application settings.
        email: The submitted email.
        password: The submitted password.

    Returns:
        A redirect home with the session cookie set, or the form again with a 401,
        or a 429 when there have been too many failures.
    """
    try:
        signed_in = await auth.sign_in(
            email,
            password,
            user_agent=request.headers.get("user-agent"),
            client_ip=request.client.host if request.client else None,
            device_token=read_device_token(request, settings),
        )
    except SignInThrottledError as error:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": THROTTLED},
            status_code=429,
            headers={"Retry-After": str(int(error.retry_after.total_seconds()))},
        )
    if signed_in is None:
        return templates.TemplateResponse(
            request, "login.html", {"error": INVALID_CREDENTIALS}, status_code=401
        )
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(request, response, signed_in.token, settings)
    set_device_cookie(request, response, signed_in.device_token, settings)
    return response


@router.post("/logout")
async def logout(
    request: Request, auth: AuthServiceDep, settings: SettingsDep
) -> RedirectResponse:
    """End the current session and clear the cookie.

    Args:
        request: The current request.
        auth: The auth service.
        settings: The application settings.

    Returns:
        A redirect to the sign-in page.
    """
    token = read_session_token(request, settings)
    if token is not None:
        await auth.sign_out(token)
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(request, response, settings)
    return response
