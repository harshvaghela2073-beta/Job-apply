"""Platform logins (docs/API_CONTRACT.md, "Connections" and "Deployment: login, pushing logins and
data")."""

import re

from fastapi import APIRouter, HTTPException, Request

from app.browser.chrome_cookies import ChromeCookie
from app.models import Connection, Platform
from app.schemas import ConnectionRead, CookiesWrite, Page
from app.services.connections import (
    LOGIN_SPECS,
    AlreadyConnecting,
    ChromeUnavailable,
    ConnectionService,
    LoginBusy,
    LoginWindowOpen,
    NoLoginCookies,
)

router = APIRouter(prefix="/connections", tags=["connections"])

# A cookie domain: a host name, optionally with a leading dot (anything else is dropped).
_HOST = re.compile(r"\.?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


def _service(request: Request) -> ConnectionService:
    return request.app.state.connections


def _read(row: Connection) -> ConnectionRead:
    return ConnectionRead(
        platform=row.platform,
        status=row.status,
        account_name=row.account_name,
        checked_at=row.checked_at,
        message=row.message,
        login_url=LOGIN_SPECS[Platform(row.platform)].login_url,
    )


@router.get("", response_model=Page[ConnectionRead])
def list_connections(request: Request) -> Page[ConnectionRead]:
    rows = _service(request).all()
    return Page(items=[_read(row) for row in rows], total=len(rows))


@router.post("/{platform}/import", response_model=ConnectionRead)
def import_from_chrome(platform: Platform, request: Request) -> ConnectionRead:
    """Copy the platform's login from the user's own Google Chrome (the main way to connect).
    The frontend calls this repeatedly until the status is ``connected``. 409 on a server without
    the user's Chrome (push the login from the Mac instead)."""
    try:
        row = _service(request).import_from_chrome(platform)
    except (LoginWindowOpen, ChromeUnavailable) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _read(row)


@router.put("/{platform}/cookies", response_model=ConnectionRead)
def push_cookies(platform: Platform, body: CookiesWrite, request: Request) -> ConnectionRead:
    """Install a login pushed from the Mac (``python -m app.tools.push login``): Playwright-shaped
    cookies, of which only the platform's domain is kept. Then the login is checked, as on import.
    422 when none of them is a login (the saved login is left alone); 409 while a login window is
    open or another import or push of this platform is running."""
    cookies = [
        ChromeCookie.from_storage_cookie(
            name=c.name, value=c.value, domain=c.domain, path=c.path, expires=c.expires,
            http_only=c.http_only, secure=c.secure, same_site=c.same_site,
        )
        for c in body.cookies
        if _HOST.fullmatch(c.domain)
    ]
    try:
        row = _service(request).install_cookies(platform, cookies)
    except NoLoginCookies as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (LoginWindowOpen, LoginBusy) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _read(row)


@router.post("/{platform}/connect", response_model=ConnectionRead, status_code=202)
def connect(platform: Platform, request: Request) -> ConnectionRead:
    """Fallback: open a visible login window; the status becomes ``connected`` once a login is
    detected."""
    try:
        row = _service(request).connect(platform)
    except AlreadyConnecting as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _read(row)


@router.post("/{platform}/disconnect", response_model=ConnectionRead)
def disconnect(platform: Platform, request: Request) -> ConnectionRead:
    """Delete the saved browser profile (and close a login window that is still open)."""
    return _read(_service(request).disconnect(platform))
