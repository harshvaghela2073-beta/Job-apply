"""Reading one platform's cookies from the user's own Google Chrome on macOS.

Used only when the user clicks Connect (and, with an already-unlocked key, before logged-in scans),
and by ``python -m app.tools.push login`` on the Mac, which sends the login to a deployed backend.

Safety rules:
- Only cookies whose host is the platform's domain or one of its subdomains are read.
- Chrome's cookie database is opened read-only (SQLite "immutable" mode) and never copied.
- Values are decrypted in memory and handed only to the tool's own browser profile. They are never
  logged, returned by the API or written anywhere else. Logs may list cookie *names*.
- The decryption password is the macOS Keychain item "Chrome Safe Storage"; macOS asks the user
  the first time. The derived key is cached in memory only.

Chrome's format (macOS): ``encrypted_value`` = ``b"v10"`` + AES-128-CBC(key, IV = 16 spaces) of the
value with PKCS#7 padding; key = PBKDF2-HMAC-SHA1(password, b"saltysalt", 1003 rounds, 16 bytes).
Since cookie database version 24, the plaintext starts with SHA-256(host_key) (32 bytes).
"""

import hashlib
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

logger = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "Chrome Safe Storage"
KEYCHAIN_TIMEOUT_SECONDS = 120
_SALT = b"saltysalt"
_ITERATIONS = 1003
_KEY_LENGTH = 16
_IV = b" " * 16
_ENCRYPTED_PREFIXES = (b"v10", b"v11")
# Seconds between Chrome's epoch (1601-01-01) and the Unix epoch.
CHROME_EPOCH_OFFSET = 11_644_473_600
_SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}  # -1 (unspecified) -> Lax


class ChromeCookiesError(RuntimeError):
    """A user-facing reason why Chrome's cookies couldn't be read."""


def on_macos() -> bool:
    """Chrome's cookies can only be decrypted on macOS (Keychain). Tests replace this."""
    return sys.platform == "darwin"


def keychain_password() -> bytes:
    """The "Chrome Safe Storage" password from the macOS Keychain (macOS may ask the user)."""
    if sys.platform != "darwin":
        raise ChromeCookiesError("Importing the login from Chrome works on macOS only. Use the login window instead.")
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE],
            capture_output=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ChromeCookiesError(
            "macOS didn't get an answer to the Keychain prompt in time. Try again and click Allow."
        ) from error
    except OSError as error:
        raise ChromeCookiesError(f"The macOS Keychain couldn't be read ({error.strerror}).") from error
    password = result.stdout.strip()
    if result.returncode != 0 or not password:
        raise ChromeCookiesError(
            'Access to "Chrome Safe Storage" in the macOS Keychain was denied or it wasn\'t found. '
            'Try again and click "Always Allow" (or "Allow") when macOS asks.'
        )
    return password


def derive_key(password: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, _SALT, _ITERATIONS, _KEY_LENGTH)


def host_matches(host: str, domain: str) -> bool:
    host = host.lstrip(".").lower()
    return host == domain or host.endswith("." + domain)


@dataclass(frozen=True)
class ChromeCookie:
    host: str  # Chrome's host_key: ".linkedin.com" (domain cookie) or "www.linkedin.com" (host-only)
    name: str
    path: str
    expires: float | None  # Unix time; None for a session cookie
    secure: bool
    http_only: bool
    same_site: str
    value: str = field(repr=False)

    def to_playwright(self) -> dict:
        cookie = {"name": self.name, "value": self.value, "httpOnly": self.http_only, "secure": self.secure}
        same_site = self.same_site if (self.same_site != "None" or self.secure) else "Lax"
        cookie["sameSite"] = same_site
        if self.host.startswith("."):
            cookie.update(domain=self.host, path=self.path or "/")
        else:  # host-only (incl. __Host- cookies): set through a URL so no Domain attribute is used
            cookie["url"] = f"https://{self.host}{self.path or '/'}"
        if self.expires is not None:
            cookie["expires"] = self.expires
        return cookie

    def to_storage_cookie(self) -> dict:
        """Playwright's storage-state shape, as ``PUT /api/connections/{platform}/cookies`` takes it:
        ``domain`` keeps Chrome's host key (a leading dot = a domain cookie, none = host-only)."""
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.host,
            "path": self.path or "/",
            "expires": self.expires if self.expires is not None else -1,
            "httpOnly": self.http_only,
            "secure": self.secure,
            "sameSite": self.same_site,
        }

    @classmethod
    def from_storage_cookie(
        cls, *, name: str, value: str, domain: str, path: str, expires: float | None, http_only: bool,
        secure: bool, same_site: str,
    ) -> "ChromeCookie":
        """The reverse of ``to_storage_cookie`` (an expiry of -1 or less means a session cookie)."""
        return cls(
            host=domain.lower(),
            name=name,
            path=path or "/",
            expires=expires if expires is not None and expires > 0 else None,
            secure=secure,
            http_only=http_only,
            same_site=same_site,
            value=value,
        )


@dataclass
class ProfileCookies:
    """Cookie names (no values) of one platform in one Chrome profile."""

    profile: str  # "Default", "Profile 1", ...
    db_path: Path
    names: list[str]
    last_access: int  # Chrome time (microseconds since 1601) of the most recently used cookie


def _chrome_time_to_unix(value: int) -> float:
    return value / 1_000_000 - CHROME_EPOCH_OFFSET


def _profile_dirs(user_data_dir: Path) -> list[Path]:
    dirs = [user_data_dir / "Default"] if (user_data_dir / "Default").is_dir() else []
    numbered = [p for p in user_data_dir.glob("Profile *") if p.is_dir() and p.name[8:].isdigit()]
    return dirs + sorted(numbered, key=lambda p: int(p.name[8:]))


def _cookie_db(profile_dir: Path) -> Path | None:
    for candidate in (profile_dir / "Network" / "Cookies", profile_dir / "Cookies"):
        if candidate.is_file():
            return candidate
    return None


def _connect(db_path: Path) -> sqlite3.Connection:
    # Read-only and "immutable": no locks, no journal, no copy of the file.
    return sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro&immutable=1", uri=True)


def _now_chrome_time() -> int:
    return int((time.time() + CHROME_EPOCH_OFFSET) * 1_000_000)


def _domain_rows(connection: sqlite3.Connection, domain: str, columns: str) -> list[tuple]:
    rows = connection.execute(
        f"SELECT host_key, {columns} FROM cookies "  # noqa: S608 - fixed column list
        "WHERE (host_key = ? OR host_key LIKE ?) AND (has_expires = 0 OR expires_utc > ?)",
        (domain, f"%.{domain}", _now_chrome_time()),
    ).fetchall()
    return [row for row in rows if host_matches(row[0], domain)]


class ChromeCookieReader:
    def __init__(self, user_data_dir: Path, password_provider: Callable[[], bytes] | None = None):
        self.user_data_dir = user_data_dir
        # None means the macOS Keychain (looked up at call time, so tests can replace it).
        self.password_provider = password_provider
        self._key: bytes | None = None
        self._key_lock = threading.Lock()

    @property
    def available(self) -> bool:
        """Whether the user's Chrome is on this machine (macOS, with its data folder)."""
        return on_macos() and self.user_data_dir.is_dir()

    @property
    def has_key(self) -> bool:
        return self._key is not None

    def forget_key(self) -> None:
        self._key = None

    def profiles(self, domain: str) -> list[ProfileCookies]:
        """Cookie names for ``domain`` in every Chrome profile that has any. No decryption."""
        if not self.available:
            raise ChromeCookiesError(
                f"Google Chrome's data folder wasn't found at {self.user_data_dir}. "
                "Set CHROME_USER_DATA_DIR, or use the login window instead."
            )
        found = []
        for profile_dir in _profile_dirs(self.user_data_dir):
            db_path = _cookie_db(profile_dir)
            if db_path is None:
                continue
            try:
                with _connect(db_path) as connection:
                    rows = _domain_rows(connection, domain, "name, last_access_utc")
            except sqlite3.Error as error:
                logger.warning("Couldn't read Chrome cookies in %s: %s", profile_dir.name, error)
                continue
            if rows:
                found.append(
                    ProfileCookies(
                        profile=profile_dir.name,
                        db_path=db_path,
                        names=[row[1] for row in rows],
                        last_access=max(row[2] or 0 for row in rows),
                    )
                )
        return found

    def newest_logged_in(self, domain: str, is_login_cookie: Callable[[str], bool]) -> ProfileCookies | None:
        """The Chrome profile logged in to ``domain`` (a cookie name ``is_login_cookie`` accepts) that
        was used most recently, or None. Reads names only: nothing is decrypted, macOS isn't asked.
        Used by Connect (``ConnectionService.import_from_chrome``) and by the push command."""
        profiles = self.profiles(domain)
        for profile in profiles:  # names only, never values
            logger.info("Chrome %s cookies for %s: %s", profile.profile, domain, ", ".join(sorted(set(profile.names))))
        logged_in = [p for p in profiles if any(is_login_cookie(name) for name in p.names)]
        return max(logged_in, key=lambda p: p.last_access, default=None)

    def _get_key(self) -> bytes:
        with self._key_lock:
            if self._key is None:
                provider = self.password_provider or keychain_password
                self._key = derive_key(provider())
            return self._key

    def read(self, profile: ProfileCookies, domain: str) -> list[ChromeCookie]:
        """Decrypted cookies for ``domain`` from one profile (may ask macOS for the Keychain)."""
        try:
            with _connect(profile.db_path) as connection:
                version_row = connection.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
                rows = _domain_rows(
                    connection,
                    domain,
                    "name, value, encrypted_value, path, expires_utc, has_expires, is_secure, "
                    "is_httponly, samesite",
                )
        except sqlite3.Error as error:
            raise ChromeCookiesError(f"Chrome's cookies couldn't be read right now ({error}). Try again.") from error
        version = int(version_row[0]) if version_row and str(version_row[0]).isdigit() else 0
        key = self._get_key() if any(row[3] for row in rows) else None
        cookies = []
        for host, name, value, encrypted, path, expires, has_expires, secure, http_only, same_site in rows:
            if encrypted:
                value = self._decrypt(key, bytes(encrypted), host, version)
            cookies.append(
                ChromeCookie(
                    host=host,
                    name=name,
                    path=path or "/",
                    expires=_chrome_time_to_unix(expires) if has_expires and expires else None,
                    secure=bool(secure),
                    http_only=bool(http_only),
                    same_site=_SAME_SITE.get(same_site, "Lax"),
                    value=value,
                )
            )
        return cookies

    def _decrypt(self, key: bytes, encrypted: bytes, host: str, version: int) -> str:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        if encrypted[:3] not in _ENCRYPTED_PREFIXES:
            return encrypted.decode("utf-8", errors="replace")
        decryptor = Cipher(algorithms.AES(key), modes.CBC(_IV)).decryptor()
        try:
            plain = decryptor.update(encrypted[3:]) + decryptor.finalize()
        except ValueError as error:
            self.forget_key()
            raise ChromeCookiesError("Chrome's cookies couldn't be decrypted. Try again.") from error
        pad = plain[-1] if plain else 0
        if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
            self.forget_key()
            raise ChromeCookiesError(
                "Chrome's cookies couldn't be decrypted with the Keychain password. Try again."
            )
        plain = plain[:-pad]
        # Database version 24+ prefixes the value with SHA-256 of the host.
        if version >= 24 and plain[:32] == hashlib.sha256(host.encode()).digest():
            plain = plain[32:]
        try:
            return plain.decode("utf-8")
        except UnicodeDecodeError as error:
            self.forget_key()
            raise ChromeCookiesError("Chrome's cookies couldn't be decrypted. Try again.") from error
