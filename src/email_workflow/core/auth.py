"""Local login for the app.

This program can read a real mailbox and send email on your behalf, so it asks
who you are before it opens.

The password itself is never stored. What is saved is a PBKDF2-SHA256 hash and
a random per-user salt, so the file on disk cannot be turned back into the
password, and two people choosing the same password still get different hashes.
"""

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Optional

from email_workflow.core.paths import resolve_project_file

# Cost of one password check, at current OWASP guidance for PBKDF2-HMAC-SHA256.
# High enough to make guessing the file expensive, still unnoticeable on login.
# Stored per record, so raising it later does not lock anyone out.
ITERATIONS = 600_000
SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    ).hex()


class AuthManager:
    def __init__(self, store_path: str = "auth.json"):
        self.store_path: Path = resolve_project_file(store_path)

    # --- state ------------------------------------------------------------

    def _read(self) -> Optional[dict]:
        if not self.store_path.exists():
            return None
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        if not all(k in data for k in ("username", "salt", "hash", "iterations")):
            return None
        return data

    def is_configured(self) -> bool:
        return self._read() is not None

    @property
    def username(self) -> str:
        data = self._read()
        return data["username"] if data else ""

    # --- writing ----------------------------------------------------------

    def set_credentials(self, username: str, password: str) -> None:
        username = (username or "").strip()
        if not username:
            raise ValueError("Username cannot be empty.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )

        salt = secrets.token_bytes(SALT_BYTES)
        payload = {
            "username": username,
            "salt": salt.hex(),
            "hash": hash_password(password, salt),
            "iterations": ITERATIONS,
        }

        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.store_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        # Best effort: readable only by this user. Windows largely ignores
        # this, so it is a hardening step, not a guarantee.
        try:
            os.chmod(self.store_path, 0o600)
        except OSError:
            pass

    def disable(self) -> None:
        if self.store_path.exists():
            self.store_path.unlink()

    # --- checking ---------------------------------------------------------

    def verify(self, username: str, password: str) -> bool:
        data = self._read()
        if not data:
            return False

        # compare_digest on both halves: no early exit that leaks which part
        # was wrong, and no timing difference between a near miss and a miss.
        salt = bytes.fromhex(data["salt"])
        candidate = hash_password(password, salt, int(data["iterations"]))

        user_ok = hmac.compare_digest(
            (username or "").strip().lower(), data["username"].lower()
        )
        pass_ok = hmac.compare_digest(candidate, data["hash"])
        return user_ok and pass_ok
