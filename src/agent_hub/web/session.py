from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

SESSION_COOKIE = "agenthub_web_session"


class WebSessionError(ValueError):
    pass


class WebSession:
    """HMAC-signed, stateless browser session for the Hub web UI."""

    def __init__(self, secret: str, ttl_seconds: int = 8 * 60 * 60) -> None:
        if len(secret) < 32:
            raise ValueError("AGENT_HUB_WEB_SECRET must contain at least 32 characters")
        self._secret = secret.encode("utf-8")
        self._ttl_seconds = int(ttl_seconds)

    def create(self, claims: dict[str, Any] | None = None) -> tuple[str, str]:
        session_id = secrets.token_hex(32)
        expires = int(time.time()) + self._ttl_seconds
        payload = json.dumps(
            claims or {"role": "admin"}, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return session_id, self._sign(session_id, expires, encoded)

    def verify(self, value: str) -> tuple[str, dict[str, Any]]:
        parts = value.split(".")
        if len(parts) != 4:
            raise WebSessionError("invalid session cookie")
        session_id, expires_text, encoded, signature = parts
        try:
            expires = int(expires_text)
        except ValueError as exc:
            raise WebSessionError("invalid session cookie") from exc
        if int(time.time()) > expires:
            raise WebSessionError("session expired")
        expected = self._signature(session_id, expires, encoded)
        if not hmac.compare_digest(signature, expected):
            raise WebSessionError("invalid session signature")
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise WebSessionError("invalid session payload") from exc
        if not isinstance(claims, dict):
            raise WebSessionError("invalid session payload")
        return session_id, claims

    def csrf(self, session_id: str) -> str:
        return hmac.new(
            self._secret, f"csrf:{session_id}".encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def revocation_marker(self, password_hash: str) -> str:
        """Session-scoped marker derived from the current password hash."""

        return hmac.new(
            self._secret,
            f"rev:{password_hash}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:24]

    def session_valid_for_account(
        self, claims: dict[str, Any], password_hash: str | None
    ) -> bool:
        """Reject stateless web cookies issued before a password change."""

        rev = claims.get("rev")
        if not rev:
            return True
        if not password_hash:
            return False
        return hmac.compare_digest(
            str(rev), self.revocation_marker(password_hash)
        )

    def set_cookie(self, value: str, *, secure: bool) -> str:
        attributes = [
            f"{SESSION_COOKIE}={value}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={self._ttl_seconds}",
        ]
        if secure:
            attributes.append("Secure")
        return "; ".join(attributes)

    def clear_cookie(self, *, secure: bool) -> str:
        attributes = [
            f"{SESSION_COOKIE}=",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            "Max-Age=0",
        ]
        if secure:
            attributes.append("Secure")
        return "; ".join(attributes)

    def _sign(self, session_id: str, expires: int, encoded: str) -> str:
        return (
            f"{session_id}.{expires}.{encoded}."
            f"{self._signature(session_id, expires, encoded)}"
        )

    def _signature(self, session_id: str, expires: int, encoded: str) -> str:
        return hmac.new(
            self._secret,
            f"{session_id}.{expires}.{encoded}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
