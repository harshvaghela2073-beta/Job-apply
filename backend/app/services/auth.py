"""App login (docs/API_CONTRACT.md, "Deployment: login, pushing logins and data").

With ``APP_PASSWORD`` empty (the local default) no login is required. Otherwise a correct password
gets a token: ``base64url(payload) "." base64url(HMAC-SHA256(key, base64url(payload)))``, the
payload being ``{"exp": <unix seconds>}``. The key comes from ``AUTH_SECRET`` and ``APP_PASSWORD``
together, so changing either one invalidates every token (logs every device out). Without
``AUTH_SECRET`` the key is stretched from the password with PBKDF2, but a random ``AUTH_SECRET`` is
stronger. Passwords and signatures are compared in constant time; neither the password nor tokens
are ever logged.
"""

import base64
import binascii
import hashlib
import hmac
import json
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.config import Settings

TOKEN_TTL = timedelta(days=30)
# Wrong passwords from one client address: this many within WINDOW blocks it for BLOCK.
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 15 * 60
BLOCK_SECONDS = 15 * 60
# Wrong passwords from all addresses together: guessing from many addresses blocks every login too.
MAX_FAILURES_ALL = 20
_ALL = "*"
# Tokens are short; anything longer is rejected before any work.
MAX_TOKEN_LENGTH = 512
_KDF_SALT = b"job-apply/auth-token-key/v1"
_KDF_ROUNDS = 200_000
# The limiter forgets idle addresses beyond this many (memory bound).
_MAX_TRACKED = 10_000


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


class LoginLimiter:
    """In-memory count of wrong passwords per client address (a restart forgets them)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def retry_after(self, address: str) -> int:
        """Seconds until this address may try again (also while all logins are blocked); 0 when free."""
        return max(self._retry_after(address), self._retry_after(_ALL))

    def _retry_after(self, key: str) -> int:
        now = self.clock()
        with self._lock:
            until = self._blocked_until.get(key)
            if until is None:
                return 0
            if now >= until:
                del self._blocked_until[key]
                return 0
            return max(1, int(until - now + 0.999))

    def failed(self, address: str) -> None:
        self._count(address, MAX_FAILURES)
        self._count(_ALL, MAX_FAILURES_ALL)

    def _count(self, key: str, limit: int) -> None:
        now = self.clock()
        with self._lock:
            failures = self._failures.setdefault(key, deque())
            failures.append(now)
            while failures and now - failures[0] >= FAILURE_WINDOW_SECONDS:
                failures.popleft()
            if len(failures) >= limit:
                self._blocked_until[key] = now + BLOCK_SECONDS
                del self._failures[key]
            self._prune(now)

    def succeeded(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)

    def _prune(self, now: float) -> None:
        if len(self._failures) + len(self._blocked_until) <= _MAX_TRACKED:
            return
        for address, failures in list(self._failures.items()):
            if not failures or now - failures[-1] >= FAILURE_WINDOW_SECONDS:
                del self._failures[address]
        for address, until in list(self._blocked_until.items()):
            if now >= until:
                del self._blocked_until[address]


class AuthService:
    def __init__(self, settings: Settings, *, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.limiter = LoginLimiter()
        password = settings.app_password.get_secret_value()
        self.required = bool(password)
        self._password_digest = _digest(password)
        self._key = b""
        if self.required:
            secret = settings.auth_secret.get_secret_value().encode("utf-8")
            self._key = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), _KDF_SALT + b"\0" + secret, _KDF_ROUNDS
            )

    def check_password(self, password: str) -> bool:
        # Comparing digests keeps the time independent of the password's length too.
        return self.required and hmac.compare_digest(_digest(password), self._password_digest)

    def _sign(self, payload: str) -> bytes:
        return hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest()

    def issue(self) -> tuple[str, datetime]:
        """A new token and when it expires."""
        expires = int(self.clock() + TOKEN_TTL.total_seconds())
        payload = _b64encode(json.dumps({"exp": expires}, separators=(",", ":")).encode("ascii"))
        token = f"{payload}.{_b64encode(self._sign(payload))}"
        return token, datetime.fromtimestamp(expires, UTC)

    def verify(self, token: str) -> bool:
        """Whether ``token`` was signed with the current key and hasn't expired."""
        if not self.required or not token or len(token) > MAX_TOKEN_LENGTH:
            return False
        payload, dot, signature = token.partition(".")
        if not dot or not payload or not signature:
            return False
        try:
            expected = self._sign(payload)
            valid = hmac.compare_digest(_b64decode(signature), expected)
            if not valid:
                return False
            claims = json.loads(_b64decode(payload))
        except (UnicodeError, binascii.Error, ValueError):
            return False
        expires = claims.get("exp") if isinstance(claims, dict) else None
        return isinstance(expires, int) and not isinstance(expires, bool) and self.clock() < expires

    def authorized(self, authorization: str | None) -> bool:
        """Whether a request with this ``Authorization`` header may use the API."""
        if not self.required:
            return True
        scheme, _, token = (authorization or "").strip().partition(" ")
        return scheme.lower() == "bearer" and self.verify(token.strip())
