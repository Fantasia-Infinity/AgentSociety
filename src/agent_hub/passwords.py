from __future__ import annotations

import re
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError


_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")

_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def validate_username(username: str) -> str:
    value = str(username).strip().lower()
    if not _USERNAME_RE.match(value):
        raise ValueError(
            "username must be 3-64 chars: lowercase letters, digits, dot, dash, underscore"
        )
    return value


def validate_password(password: str) -> str:
    value = str(password)
    if len(value) < 10:
        raise ValueError("password must contain at least 10 characters")
    if not any(ch.isalpha() for ch in value):
        raise ValueError("password must contain at least one letter")
    if not any(ch.isdigit() for ch in value):
        raise ValueError("password must contain at least one digit")
    return value


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def public_account(account: dict[str, Any]) -> dict[str, Any]:
    """Account record without the password hash."""

    return {
        key: account[key]
        for key in (
            "username",
            "principal_id",
            "tenant_id",
            "role",
            "display_name",
            "created_at",
            "updated_at",
        )
        if key in account
    }
