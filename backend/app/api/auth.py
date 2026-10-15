"""App login (docs/API_CONTRACT.md, "Deployment: login, pushing logins and data").

``RequireLogin`` guards every ``/api/*`` request while ``APP_PASSWORD`` is set, except
``/api/health``, ``/api/auth/*`` and CORS preflights. ``create_app`` adds it inside the CORS
middleware, so its 401 responses carry CORS headers and the frontend can read them.
"""

from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.schemas import AuthStatusRead, LoginRead, LoginWrite
from app.services.auth import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])

PUBLIC_PATHS = frozenset({"/api/health", "/api/auth"})
PUBLIC_PREFIX = "/api/auth/"


def _auth(request: Request) -> AuthService:
    return request.app.state.auth


def client_address(request: Request) -> str:
    """The caller's address: the last ``X-Forwarded-For`` entry, the one Render's proxy added. Earlier
    entries come from the client and can be made up to dodge the wrong-password limit."""
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
    if forwarded:
        return forwarded[-1]
    return request.client.host if request.client else "unknown"


@router.get("/status", response_model=AuthStatusRead)
def auth_status(request: Request) -> AuthStatusRead:
    auth = _auth(request)
    return AuthStatusRead(required=auth.required, authenticated=auth.authorized(request.headers.get("authorization")))


@router.post("/login", response_model=LoginRead)
def login(body: LoginWrite, request: Request) -> LoginRead:
    auth = _auth(request)
    if not auth.required:
        raise HTTPException(status_code=400, detail="This server doesn't need a login.")
    address = client_address(request)
    wait = auth.limiter.retry_after(address)
    if wait:
        raise HTTPException(
            status_code=429,
            detail="Too many wrong passwords. Try again in 15 minutes.",
            headers={"Retry-After": str(wait)},
        )
    if not auth.check_password(body.password):
        auth.limiter.failed(address)
        raise HTTPException(status_code=401, detail="Wrong password.")
    auth.limiter.succeeded(address)
    token, expires_at = auth.issue()
    return LoginRead(token=token, expires_at=expires_at)


def needs_login(path: str, method: str) -> bool:
    if method == "OPTIONS" or not (path == "/api" or path.startswith("/api/")):
        return False
    return path not in PUBLIC_PATHS and not path.startswith(PUBLIC_PREFIX)


class RequireLogin:
    """ASGI middleware: 401 ``{"detail": "Login required."}`` without a valid bearer token."""

    def __init__(self, app: ASGIApp, auth: AuthService):
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and self.auth.required
            and needs_login(scope["path"], scope["method"])
            and not self.auth.authorized(Headers(scope=scope).get("authorization"))
        ):
            response = JSONResponse(
                {"detail": "Login required."}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
