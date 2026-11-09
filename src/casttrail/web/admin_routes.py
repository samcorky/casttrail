import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import casttrail.maintenance as maintenance
from casttrail.auth.errors import AccountError
from casttrail.discovery.health import rostercon_health
from casttrail.discovery.schedule import recent_runs, run_in_background
from casttrail.startup import run_backup
from casttrail.web.dependencies import (
    AdminServiceDep,
    AuthServiceDep,
    CurrentAdmin,
    SessionDep,
)
from casttrail.web.routes import templates

logger = logging.getLogger(__name__)

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


_HEALTH_NOTICES = {
    "backed-up": "A backup was taken.",
    "backup-failed": "The backup failed. See the server log.",
    "backup-off": "Backups are switched off.",
}


@router.get("/health", response_class=HTMLResponse)
async def source_health(
    request: Request,
    admin: CurrentAdmin,
    session: SessionDep,
    notice: str | None = None,
) -> Response:
    """Show how the app, its backups and the convention sources are doing.

    Args:
        request: The current request.
        admin: The signed-in administrator.
        session: The request's database session.
        notice: Which message to show after an action.

    Returns:
        The page.
    """
    settings = request.app.state.settings
    folder = maintenance.backup_folder(settings.database_url, settings.backup_dir)
    backups = (
        await asyncio.to_thread(maintenance.list_backups, folder) if folder else []
    )
    context = {
        "user": admin,
        "sources": [await rostercon_health(session, now=datetime.now(UTC))],
        "warnings": request.app.state.startup.warnings,
        "backups": backups,
        "backups_enabled": settings.backups_enabled,
        "keep": settings.backup_keep,
        "notice": _HEALTH_NOTICES.get(notice or ""),
    }
    return templates.TemplateResponse(request, "admin_health.html", context)


@router.post("/backups/now", response_model=None)
async def back_up_now(request: Request, admin: CurrentAdmin) -> RedirectResponse:
    """Take a backup of the database now.

    Args:
        request: The current request.
        admin: The signed-in administrator.

    Returns:
        A redirect back to the health page with a message.
    """
    del admin  # Only checked: any administrator may take a backup.
    settings = request.app.state.settings
    if not settings.backups_enabled:
        return RedirectResponse("/admin/health?notice=backup-off", status_code=303)
    try:
        made = await run_backup(settings, "manual")
    except Exception:
        logger.exception("The manual backup failed")
        return RedirectResponse("/admin/health?notice=backup-failed", status_code=303)
    notice = "backed-up" if made is not None else "backup-failed"
    return RedirectResponse(f"/admin/health?notice={notice}", status_code=303)
