from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from casttrail.auth.errors import AccountError
from casttrail.auth.throttle import SignInThrottledError
from casttrail.web.cookies import read_session_token
from casttrail.web.dependencies import AuthServiceDep, CurrentUser, SettingsDep
from casttrail.web.routes import templates

router = APIRouter(prefix="/account")

THROTTLED = "Too many failed attempts. Try again later."


def _page(
    request: Request,
    user: CurrentUser,
    *,
    error: str | None = None,
    done: bool = False,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    return templates.TemplateResponse(
        request,
        "account_password.html",
        {"user": user, "error": error, "done": done},
        status_code=status_code,
        headers=headers,
    )


@router.get("/password", response_class=HTMLResponse, response_model=None)
async def password_page(
    request: Request, user: CurrentUser, done: bool = False
) -> Response:
    """Show the change-password form.

    Args:
        request: The current request.
        user: The signed-in user.
        done: Whether to confirm that the password was just changed.

    Returns:
        The page.
    """
    return _page(request, user, done=done)


@router.post("/password", response_class=HTMLResponse, response_model=None)
async def change_password(
    request: Request,
    user: CurrentUser,
    auth: AuthServiceDep,
    settings: SettingsDep,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
) -> Response:
    """Change the signed-in user's password.

    Other sessions end and remembered browsers are forgotten; this one stays in.

    Args:
        request: The current request.
        user: The signed-in user.
        auth: The auth service.
        settings: The application settings.
        current_password: The password they have now.
        new_password: The password to set.
        confirm_password: The new password again.

    Returns:
        A redirect back to the form with a confirmation, or the form again with the
        reason it failed (a 429 when there have been too many failures).
    """
    if new_password != confirm_password:
        return _page(
            request, user, error="The two new passwords do not match.", status_code=400
        )
    try:
        await auth.change_password(
            user,
            current_password,
            new_password,
            keep_token=read_session_token(request, settings),
            client_ip=request.client.host if request.client else None,
        )
    except SignInThrottledError as error:
        return _page(
            request,
            user,
            error=THROTTLED,
            status_code=429,
            headers={"Retry-After": str(int(error.retry_after.total_seconds()))},
        )
    except AccountError as error:
        return _page(request, user, error=str(error), status_code=400)
    return RedirectResponse("/account/password?done=true", status_code=303)
