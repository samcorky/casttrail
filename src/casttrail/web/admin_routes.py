import uuid
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from casttrail.auth.errors import AccountError
from casttrail.web.dependencies import AdminServiceDep, AuthServiceDep, CurrentAdmin
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
