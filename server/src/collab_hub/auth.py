"""Bearer token verification (resource-server side) and principal resolution.

The server never issues tokens. It validates JWTs from one configured issuer
(Microsoft Entra ID in production) against that issuer's JWKS, checks
issuer, audience, expiry, tenant and scopes, and maps the subject to a
configured grant. Unknown subjects are rejected even with a valid token.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import anyio
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

from .config import PrincipalGrant, Settings
from .errors import HubError

log = logging.getLogger("collab_hub.auth")

AGENT_HEADER = "x-collab-agent"
KNOWN_AGENTS = frozenset({"claude-code", "claude-desktop", "codex", "chatgpt", "other"})
_AGENT_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def _fingerprint(value: str) -> str:
    """Short, non-reversible identifier for logs (never log raw subject IDs)."""
    return hashlib.sha256(value.encode()).hexdigest()[:10]


class JwtTokenVerifier:
    """Implements mcp.server.auth.provider.TokenVerifier for RS256 JWTs."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._static_keys: jwt.PyJWKSet | None = None
        self._jwks_client: jwt.PyJWKClient | None = None
        if settings.oidc_jwks_json:
            self._static_keys = jwt.PyJWKSet.from_json(settings.oidc_jwks_json)
        else:
            assert settings.jwks_url
            self._jwks_client = jwt.PyJWKClient(
                settings.jwks_url, cache_jwk_set=True, lifespan=3600, timeout=10
            )

    def _signing_key(self, token: str) -> Any:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":
            raise jwt.InvalidAlgorithmError("only RS256 is accepted")
        kid = str(header.get("kid") or "")
        if self._static_keys is not None:
            for key in self._static_keys.keys:
                if key.key_id == kid:
                    return key.key
            raise jwt.InvalidKeyError("unknown kid")
        assert self._jwks_client is not None
        return self._jwks_client.get_signing_key(kid).key

    def _decode(self, token: str) -> dict[str, Any]:
        key = self._signing_key(token)
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=self._s.oidc_audience,
            issuer=self._s.issuer,
            leeway=60,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await anyio.to_thread.run_sync(self._decode, token)
        except jwt.PyJWTError as exc:
            log.info("token rejected: %s", type(exc).__name__)
            return None
        except Exception as exc:  # JWKS fetch failures etc.; do not leak details
            log.warning("token verification error: %s", type(exc).__name__)
            return None

        if self._s.tenant_id and claims.get("tid") != self._s.tenant_id:
            log.info("token rejected: tenant mismatch")
            return None
        subject = claims.get(self._s.subject_claim)
        if not isinstance(subject, str) or subject not in self._s.principals:
            fp = _fingerprint(subject) if isinstance(subject, str) else "none"
            log.info("token rejected: principal not allowed (fp=%s)", fp)
            return None
        scopes = str(claims.get("scp", "")).split()
        if not ({self._s.scope_read, self._s.scope_write} & set(scopes)):
            log.info("token rejected: no collab scope")
            return None

        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("appid") or "unknown"),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self._s.resource_url,  # audience checked above
            subject=subject,
            # Keep only non-personal claims; names and emails are dropped.
            claims={k: claims[k] for k in ("iss", "tid", "azp", "appid") if k in claims},
        )


@dataclass(frozen=True)
class Principal:
    subject: str
    alias: str
    workspace: str
    role: str
    agent: str
    client_id: str
    scopes: frozenset[str]

    @property
    def actor(self) -> str:
        """Recorded author: verified alias + self-declared agent label."""
        return f"{self.alias}/{self.agent}"

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.subject)


def resolve_principal(settings: Settings, headers: Mapping[str, str] | None) -> Principal:
    token = get_access_token()
    if token is None or token.subject is None:
        # Transport auth should make this unreachable; fail closed anyway.
        raise HubError("forbidden", "no authenticated principal")
    grant: PrincipalGrant | None = settings.principals.get(token.subject)
    if grant is None:
        raise HubError("forbidden", "principal is not allowed")
    agent = "other"
    if headers:
        raw = (headers.get(AGENT_HEADER) or "").strip().lower()
        if raw and _AGENT_RE.match(raw) and raw in KNOWN_AGENTS:
            agent = raw
    return Principal(
        subject=token.subject,
        alias=grant.alias,
        workspace=grant.workspace,
        role=grant.role,
        agent=agent,
        client_id=token.client_id,
        scopes=frozenset(token.scopes),
    )


def require(settings: Settings, principal: Principal, *, write: bool) -> None:
    """Server-side authorization for one operation."""
    if write:
        if principal.role != "writer" or settings.scope_write not in principal.scopes:
            raise HubError("forbidden", "write access requires the writer role and write scope")
    elif not ({settings.scope_read, settings.scope_write} & principal.scopes):
        raise HubError("forbidden", "read scope required")
