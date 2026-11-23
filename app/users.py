"""Local user accounts: hashing, API tokens, login throttling and user management.

Standard library only (hashlib.scrypt); no external identity provider.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time

from .db import Database
from .models import Role, User, utcnow

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
MIN_PASSWORD_LENGTH = 8
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# --- passwords and tokens ---------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n),
                                r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# A fixed hash so unknown usernames cost the same time as wrong passwords.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_api_token() -> tuple[str, str]:
    token = "plt_" + secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")


def validate_email(email: str) -> str:
    email = email.strip()
    if email and not EMAIL_RE.match(email):
        raise UserError("That email address does not look right")
    return email


# --- login throttling -------------------------------------------------------

class LoginThrottle:
    """Counts failed logins per key (IP, username). Delay grows after 5 failures."""

    FREE_ATTEMPTS = 5
    MAX_DELAY = 30.0
    FORGET_AFTER = 15 * 60

    def __init__(self) -> None:
        self._failures: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def delay(self, *keys: str) -> float:
        now = time.monotonic()
        worst = 0
        with self._lock:
            for key in keys:
                count, last = self._failures.get(key, (0, 0.0))
                if now - last > self.FORGET_AFTER:
                    self._failures.pop(key, None)
                    continue
                worst = max(worst, count)
        if worst < self.FREE_ATTEMPTS:
            return 0.0
        return min(2.0 ** (worst - self.FREE_ATTEMPTS), self.MAX_DELAY)

    def failed(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                count, _ = self._failures.get(key, (0, 0.0))
                self._failures[key] = (count + 1, now)

    def succeeded(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)


# --- user management --------------------------------------------------------

class UserService:
    def __init__(self, db: Database):
        self.db = db

    @property
    def needs_setup(self) -> bool:
        return self.db.count_users() == 0

    def bootstrap(self, password: str) -> User | None:
        """Create admin "admin" from PLATEN_PASSWORD when there are no users (v0.1 upgrade)."""
        if password and self.needs_setup:
            return self.db.add_user(User(username="admin", password_hash=hash_password(password),
                                         role=Role.ADMIN))
        return None

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.db.get_user_by_name(username.strip().lower())
        if user is None or not user.active:
            verify_password(password, _DUMMY_HASH)
            return None
        if not verify_password(password, user.password_hash):
            return None
        return self.db.update_user(user.id, last_login_at=utcnow())

    def from_api_token(self, token: str) -> User | None:
        user = self.db.get_user_by_token_hash(token_hash(token))
        return user if user and user.active else None

    def create(self, username: str, password: str, role: str = Role.USER, email: str = "",
               allowed_printers: str = "") -> User:
        username = username.strip().lower()
        if not USERNAME_RE.match(username):
            raise UserError("Username: 2-32 characters, lowercase letters, digits, . _ -")
        if self.db.get_user_by_name(username):
            raise UserError("That username is taken", 409)
        if role not in (Role.ADMIN, Role.USER):
            raise UserError("Invalid role")
        validate_password(password)
        return self.db.add_user(User(
            username=username, password_hash=hash_password(password), role=role,
            email=validate_email(email), allowed_printers=_clean_list(allowed_printers),
        ))

    def get(self, user_id: int) -> User:
        user = self.db.get_user(user_id)
        if user is None:
            raise UserError("User not found", 404)
        return user

    def update(self, user_id: int, *, role: str | None = None, active: bool | None = None,
               email: str | None = None, allowed_printers: str | None = None,
               paperless_token: str | None = None) -> User:
        user = self.get(user_id)
        fields: dict = {}
        losing_admin = (
            user.is_admin and user.active
            and ((role is not None and role != Role.ADMIN) or active is False)
        )
        if losing_admin and self.db.count_active_admins() <= 1:
            raise UserError("There must be at least one active admin", 409)
        if role is not None:
            if role not in (Role.ADMIN, Role.USER):
                raise UserError("Invalid role")
            fields["role"] = role
        if active is not None:
            fields["active"] = active
            if not active:
                fields["session_epoch"] = user.session_epoch + 1
        if email is not None:
            fields["email"] = validate_email(email)
        if allowed_printers is not None:
            fields["allowed_printers"] = _clean_list(allowed_printers)
        if paperless_token is not None:
            fields["paperless_token"] = paperless_token.strip()
        return self.db.update_user(user_id, **fields) if fields else user

    def set_password(self, user_id: int, password: str) -> User:
        validate_password(password)
        user = self.get(user_id)
        return self.db.update_user(user_id, password_hash=hash_password(password),
                                   session_epoch=user.session_epoch + 1)

    def change_own_password(self, user: User, current: str, new: str) -> User:
        if not verify_password(current, user.password_hash):
            raise UserError("Current password is wrong", 403)
        return self.set_password(user.id, new)

    def issue_api_token(self, user_id: int) -> str:
        token, hashed = new_api_token()
        self.db.update_user(user_id, api_token_hash=hashed)
        return token

    def revoke_api_token(self, user_id: int) -> None:
        self.db.update_user(user_id, api_token_hash="")

    def logout_everywhere(self, user_id: int) -> User:
        user = self.get(user_id)
        return self.db.update_user(user_id, session_epoch=user.session_epoch + 1)


def _clean_list(value: str) -> str:
    return ",".join(p.strip() for p in value.split(",") if p.strip())
