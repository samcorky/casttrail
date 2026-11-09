import asyncio
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from casttrail.auth.errors import AccountError
from casttrail.discovery.schedule import recent_runs, run_in_background
from casttrail.web.dependencies import (
    AdminServiceDep,
    AuthServiceDep,
    CurrentAdmin,
    SessionDep,
)
from casttrail.web.routes import templates

router = APIRouter(prefix="/admin")


async def _users_page(
    request: Request,
    admin: CurrentAdmin,
    service: AdminServiceDep,
    *,
    error: str | None = None,
    form: dict[str, str] | None = None,
) -> Response:
    context = {
        "user": admin,
        "users": await service.list_users(),
        "error": error,
        "form": form or {},
    }
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        context,
        status_code=400 if error else 200,
    )


@router.get("/users", response_class=HTMLResponse, response_model=None)
async def users(
    request: Request, admin: CurrentAdmin, service: AdminServiceDep
) -> Response:
    """List the accounts and show the form for adding one.

    Args:
        request: The current request.
        admin: The signed-in administrator.
        service: The admin service.

    Returns:
        The page.
    """
    return await _users_page(request, admin, service)


@router.post("/users", response_class=HTMLResponse, response_model=None)
async def create_user(
    request: Request,
    admin: CurrentAdmin,
    service: AdminServiceDep,
    auth: AuthServiceDep,
    email: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    """Add an account.

    Args:
        request: The current request.
        admin: The signed-in administrator.
        service: The admin service.
        auth: The auth service.
        email: The new account's login address.
        display_name: The name shown in the interface.
        password: The new account's initial password.

    Returns:
        A redirect back to the list, or the page again with the reason it failed.
    """
    try:
        await auth.create_user(
            email=email, display_name=display_name, password=password
        )
    except AccountError as error:
        return await _users_page(
            request,
            admin,
            service,
            error=str(error),
            form={"email": email, "display_name": display_name},
        )
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/disable", response_model=None)
async def disable_user(
    request: Request,
    user_id: uuid.UUID,
    admin: CurrentAdmin,
    service: AdminServiceDep,
) -> Response:
    """Stop an account signing in and end its sessions.

    Args:
        request: The current request.
        user_id: The account to disable.
        admin: The signed-in administrator.
        service: The admin service.

    Returns:
        A redirect back to the list, or the page again with the reason it failed.

    Raises:
        HTTPException: A 404 if there is no such account.
    """
    try:
        user = await service.set_active(admin, user_id, active=False)
    except AccountError as error:
        return await _users_page(request, admin, service, error=str(error))
    if user is None:
        raise HTTPException(status_code=404)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/enable")
async def enable_user(
    user_id: uuid.UUID, admin: CurrentAdmin, service: AdminServiceDep
) -> RedirectResponse:
    """Let a disabled account sign in again.

    Args:
        user_id: The account to enable.
        admin: The signed-in administrator.
        service: The admin service.

    Returns:
        A redirect back to the list.

    Raises:
        HTTPException: A 404 if there is no such account.
    """
    if await service.set_active(admin, user_id, active=True) is None:
        raise HTTPException(status_code=404)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/make-admin")
async def make_admin(
    user_id: uuid.UUID, _admin: CurrentAdmin, service: AdminServiceDep
) -> RedirectResponse:
    """Give an account administrator rights.

    Args:
        user_id: The account to promote.
        _admin: The signed-in administrator, required but not otherwise used.
        service: The admin service.

    Returns:
        A redirect back to the list.

    Raises:
        HTTPException: A 404 if there is no such account.
    """
    if await service.make_admin(user_id) is None:
        raise HTTPException(status_code=404)
    return RedirectResponse("/admin/users", status_code=303)


_NOTICES = {
    "started": "A refresh has started. Reload this page in a few minutes.",
    "running": "A refresh is already running.",
}


def _summary(text: str | None) -> dict[str, object]:
    """Read a run's stored counts, tolerating a missing or damaged record."""
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


@router.get("/refresh", response_class=HTMLResponse)
async def refresh_page(
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
    notice: str | None = None,
) -> Response:
    """Show recent refreshes of the convention data, and a button to run one.

    Args:
        request: The current request.
        admin: The signed-in administrator.
        session: The request's database session.
        notice: Which message to show after starting a refresh.

    Returns:
        The page.
    """
    runs = [
        {"run": run, "counts": _summary(run.summary)}
        for run in await recent_runs(session)
    ]
    context = {
        "user": admin,
        "runs": runs,
        "notice": _NOTICES.get(notice or ""),
        "schedule": request.app.state.settings,
    }
    return templates.TemplateResponse(request, "admin_refresh.html", context)


@router.post("/refresh", response_model=None)
async def start_refresh(request: Request, admin: CurrentAdmin) -> RedirectResponse:
    """Start a refresh now, unless this process is already running one.

    The refresh takes minutes (Rostercon is asked slowly), so it runs in the
    background and this returns at once.

    Args:
        request: The current request.
        admin: The signed-in administrator.

    Returns:
        A redirect to the refresh page with a message.
    """
    del admin  # Only checked: any administrator may start a refresh.
    task = request.app.state.refresh_task
    if task is not None and not task.done():
        return RedirectResponse("/admin/refresh?notice=running", status_code=303)
    request.app.state.refresh_task = asyncio.create_task(
        run_in_background(request.app.state.refresher)
    )
    return RedirectResponse("/admin/refresh?notice=started", status_code=303)
