from __future__ import annotations

import json
from typing import Any
from urllib.request import urlopen

from .auth import AuthenticatedContext
from .store import AgentHubStore


class JwksOidcProvider:
    """Validates RS256 OIDC ID tokens through the issuer JWKS endpoint.

    Requires the optional ``oidc`` extra (PyJWT + cryptography). The provider
    maps the token ``sub`` to a pre-registered ``hub_oidc_identities`` row, so
    an identity must exist before the first successful login.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        store: AgentHubStore,
        fetch_impl: Any = urlopen,
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._store = store
        self._fetch = fetch_impl
        self._jwks_uri: str | None = None

    def validate_id_token(self, token: str) -> AuthenticatedContext | None:
        try:
            import jwt
        except ImportError as exc:
            raise RuntimeError(
                "OIDC requires the 'oidc' optional dependency (pip install '.[oidc]')"
            ) from exc
        try:
            jwks_client = jwt.PyJWKClient(self._jwks_uri())
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
            )
        except Exception:
            return None
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            return None
        return self._store.authenticate_oidc(
            provider=self._issuer,
            subject=subject,
        )

    def _jwks_uri(self) -> str:
        if self._jwks_uri is not None:
            return self._jwks_uri
        discovery_url = f"{self._issuer}/.well-known/openid-configuration"
        with self._fetch(discovery_url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        jwks_uri = payload.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://"):
            raise ValueError("OIDC issuer discovery did not return an https jwks_uri")
        self._jwks_uri = jwks_uri
        return jwks_uri
