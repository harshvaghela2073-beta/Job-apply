"""Synthetic Chrome cookie databases for tests, encrypted like Chrome on macOS with a known test
password. Never the user's real Chrome profile."""

import hashlib
import sqlite3
import time
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.browser.chrome_cookies import CHROME_EPOCH_OFFSET, derive_key

TEST_PASSWORD = b"test-chrome-safe-storage"

SCHEMA = """
CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
CREATE TABLE cookies(
    creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL, top_frame_site_key TEXT NOT NULL,
    name TEXT NOT NULL, value TEXT NOT NULL, encrypted_value BLOB NOT NULL, path TEXT NOT NULL,
    expires_utc INTEGER NOT NULL, is_secure INTEGER NOT NULL, is_httponly INTEGER NOT NULL,
    last_access_utc INTEGER NOT NULL, has_expires INTEGER NOT NULL, is_persistent INTEGER NOT NULL,
    priority INTEGER NOT NULL, samesite INTEGER NOT NULL, source_scheme INTEGER NOT NULL,
    source_port INTEGER NOT NULL, last_update_utc INTEGER NOT NULL, source_type INTEGER NOT NULL,
    has_cross_site_ancestor INTEGER NOT NULL);
"""


def chrome_time(unix: float) -> int:
    return int((unix + CHROME_EPOCH_OFFSET) * 1_000_000)


def encrypt(value: str, host: str, password: bytes = TEST_PASSWORD, version: int = 24) -> bytes:
    plain = value.encode()
    if version >= 24:
        plain = hashlib.sha256(host.encode()).digest() + plain
    pad = 16 - len(plain) % 16
    plain += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(derive_key(password)), modes.CBC(b" " * 16)).encryptor()
    return b"v10" + encryptor.update(plain) + encryptor.finalize()


def cookie(host, name, value, *, age_hours=1.0, expires_in_days=30.0, secure=True, http_only=True,
           same_site=-1, path="/", encrypted=True):
    return dict(host=host, name=name, value=value, age_hours=age_hours, expires_in_days=expires_in_days,
                secure=secure, http_only=http_only, same_site=same_site, path=path, encrypted=encrypted)


def make_profile(root: Path, profile: str, cookies: list[dict], *, version: int = 24, network_dir: bool = True,
                 password: bytes = TEST_PASSWORD) -> Path:
    folder = root / profile / ("Network" if network_dir else "")
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "Cookies"
    now = time.time()
    with sqlite3.connect(db) as connection:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
        for c in cookies:
            expires = now + c["expires_in_days"] * 86400 if c["expires_in_days"] is not None else 0
            encrypted = encrypt(c["value"], c["host"], password, version) if c["encrypted"] else b""
            connection.execute(
                "INSERT INTO cookies VALUES (?,?,'',?,?,?,?,?,?,?,?,?,1,1,?,2,443,?,0,0)",
                (
                    chrome_time(now - 86400), c["host"], c["name"], "" if c["encrypted"] else c["value"],
                    encrypted, c["path"], chrome_time(expires) if expires else 0, int(c["secure"]),
                    int(c["http_only"]), chrome_time(now - c["age_hours"] * 3600), int(bool(expires)),
                    c["same_site"], chrome_time(now),
                ),
            )
    return db
