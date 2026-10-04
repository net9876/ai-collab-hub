"""OAuth 2.1 authorization server for remote MCP connectors (ChatGPT, Claude.ai).

Why: those clients need Dynamic Client Registration (or CIMD) and send the
RFC 8707 `resource` parameter; Microsoft Entra ID supports neither DCR nor
CIMD. The hub therefore acts as its own authorization server and delegates
*user sign-in* to Entra:

  client --DCR--> /register
  client --> /authorize --> Entra sign-in (login app, PKCE, managed-identity
             client assertion: no secret) --> /oauth/callback
         --> consent page (once per user+client; confused-deputy protection
             required by the MCP security best practices for proxies with a
             static upstream client) --> client redirect_uri?code=...&iss=...
  client --> /token (PKCE) --> opaque access token (1 h) + rotating refresh token

Only identities in COLLAB_PRINCIPALS can complete sign-in or use tokens.
Tokens and codes are random 256-bit strings stored only as SHA-256 hashes.
Entra JWTs from the Azure CLI (Claude Code / Codex) are still accepted.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import secrets
import time
from fnmatch import fnmatchcase
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse

import anyio
import httpx2
import jwt
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .auth import JwtTokenVerifier, _fingerprint
from .config import Settings
from .errors import HubError
from .store import Store

log = logging.getLogger("collab_hub.oauth")

# Entra v2 puts `oid` into the id_token only when `profile` is requested
# (seen live 2026-10-04: "id_token has no oid" with scope=openid alone).
OIDC_SCOPES = "openid profile"

ACCESS_PREFIX = "chb_at_"
REFRESH_PREFIX = "chb_rt_"
PENDING_TTL = 600
CODE_TTL = 300
CALLBACK_PATH = "/oauth/callback"
APPROVE_PATH = "/oauth/approve"

P_CLIENT = "oauth~client"
P_PENDING = "oauth~pending"
P_APPROVAL = "oauth~approval"
P_CONSENT = "oauth~consent"
P_CODE = "oauth~code"
P_ACCESS = "oauth~access"
P_REFRESH = "oauth~refresh"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> int:
    return int(time.time())


def agent_for_redirect(uri: str) -> str:
    host = (urlparse(uri).hostname or "").lower()
    if host in ("chatgpt.com", "chat.openai.com"):
        return "chatgpt"
    if host in ("claude.ai", "claude.com"):
        return "claude-desktop"
    return "other"


def redirect_allowed(uri: str, patterns: list[str]) -> bool:
    """Structured allowlist match: scheme and host exact, port exact or '*', path glob.

    Parsing (instead of globbing the whole string) blocks tricks such as
    http://localhost:1@evil.example/ whose real host is evil.example.
    """
    try:
        u = urlparse(uri)
        u_port = u.port
    except ValueError:
        return False
    if u.username or u.password or u.fragment or u.query or not u.hostname:
        return False
    for pattern in patterns:
        p = urlparse(pattern.replace(":*", ":0", 1))
        if u.scheme != p.scheme or u.hostname != p.hostname:
            continue
        port_any = ":*" in pattern.split("//", 1)[-1].split("/", 1)[0]
        if not port_any and u_port != p.port:
            continue
        if fnmatchcase(u.path or "/", p.path or "*"):
            return True
    return False


class UpstreamError(Exception):
    pass


class Upstream(Protocol):
    """The identity provider that signs the user in (Entra in production)."""

    def authorize_url(self, *, state: str, nonce: str, code_challenge: str) -> str: ...

    async def redeem(self, *, code: str, code_verifier: str, nonce: str) -> str:
        """Exchange the upstream code; return the verified subject (Entra oid)."""
        ...


class EntraUpstream:
    """Entra ID sign-in with PKCE and a managed-identity federated credential.

    The login app trusts the Container App's user-assigned managed identity
    (federated identity credential), so the token request authenticates with a
    short-lived MI token as client_assertion: no client secret anywhere.
    """

    def __init__(self, settings: Settings, credential: Any) -> None:
        assert settings.tenant_id and settings.oauth_login_client_id
        self._s = settings
        self._tenant = settings.tenant_id
        self._client_id = settings.oauth_login_client_id
        self._credential = credential
        self._redirect_uri = settings.public_url.rstrip("/") + CALLBACK_PATH
        base = f"https://login.microsoftonline.com/{self._tenant}"
        self._authorize = f"{base}/oauth2/v2.0/authorize"
        self._token = f"{base}/oauth2/v2.0/token"
        self._issuer = f"{base}/v2.0"
        self._jwks = jwt.PyJWKClient(
            f"{base}/discovery/v2.0/keys", cache_jwk_set=True, lifespan=3600
        )

    def authorize_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "response_type": "code",
                "redirect_uri": self._redirect_uri,
                "response_mode": "query",
                "scope": OIDC_SCOPES,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._authorize}?{query}"

    async def redeem(self, *, code: str, code_verifier: str, nonce: str) -> str:
        mi = await self._credential.get_token("api://AzureADTokenExchange/.default")
        form = {
            "client_id": self._client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri,
            "code_verifier": code_verifier,
            "scope": OIDC_SCOPES,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": mi.token,
        }
        async with httpx2.AsyncClient(timeout=20) as http:
            resp = await http.post(self._token, data=form)
        if resp.status_code != 200:
            err = resp.json().get("error", "unknown") if resp.content else "unknown"
            raise UpstreamError(f"entra token request failed: {err}")
        id_token = resp.json().get("id_token")
        if not id_token:
            raise UpstreamError("entra returned no id_token")

        def _decode() -> dict[str, Any]:
            key = self._jwks.get_signing_key_from_jwt(id_token).key
            return jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                audience=self._client_id,
                issuer=self._issuer,
                leeway=60,
            )

        try:
            claims = await anyio.to_thread.run_sync(_decode)
        except jwt.PyJWTError as exc:
            raise UpstreamError(f"id_token rejected: {type(exc).__name__}") from exc
        if claims.get("nonce") != nonce or claims.get("tid") != self._tenant:
            raise UpstreamError("id_token nonce/tenant mismatch")
        oid = claims.get("oid")
        if not isinstance(oid, str):
            raise UpstreamError("id_token has no oid")
        return oid


class HubAuthorizationCode(AuthorizationCode):
    etag: str
    agent: str


class HubRefreshToken(RefreshToken):
    etag: str
    agent: str


class HubOAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider."""

    def __init__(
        self, settings: Settings, store: Store, jwt_verifier: JwtTokenVerifier, upstream: Upstream
    ) -> None:
        self.s = settings
        self.store = store
        self.jwt_verifier = jwt_verifier
        self.upstream = upstream
        # Trailing slash on purpose: the SDK serialises the protected-resource
        # metadata's authorization_servers with one, and clients compare the
        # issuer (metadata + RFC 9207 `iss`) as an exact string.
        self.issuer = settings.public_url.rstrip("/") + "/"
        self.valid_scopes = [settings.scope_read, settings.scope_write]

    # ---------------------------------------------------------------- clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await self.store.get(P_CLIENT, client_id)
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row["data"])

    def _redirect_allowed(self, uri: str) -> bool:
        return redirect_allowed(uri, self.s.redirect_allowlist)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        bad = [u for u in uris if not self._redirect_allowed(u)]
        if not uris or bad:
            raise RegistrationError(
                "invalid_redirect_uri", "redirect_uri is not on this server's allowlist"
            )
        if await self.store.count(P_CLIENT, self.s.oauth_max_clients) >= self.s.oauth_max_clients:
            raise RegistrationError("invalid_client_metadata", "client registration limit reached")
        await self.store.create(
            {
                "PartitionKey": P_CLIENT,
                "RowKey": client_info.client_id,
                "data": client_info.model_dump_json(),
                "created_at": _now(),
            }
        )
        log.info(
            json.dumps({"event": "oauth_client_registered", "agent": agent_for_redirect(uris[0])})
        )

    # ---------------------------------------------------------------- authorize

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource and params.resource.rstrip("/") != self.s.resource_url:
            raise AuthorizeError("invalid_target", "resource must be this MCP server")
        scopes = [s for s in (params.scopes or [self.s.scope_write]) if s in self.valid_scopes]
        if not scopes:
            raise AuthorizeError(
                "invalid_scope", f"supported scopes: {' '.join(self.valid_scopes)}"
            )
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        await self.store.create(
            {
                "PartitionKey": P_PENDING,
                "RowKey": _hash(state),
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_explicit": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "scopes": " ".join(scopes),
                "client_state": params.state or "",
                "has_state": params.state is not None,
                "verifier": verifier,
                "nonce": nonce,
                "expires_at": _now() + PENDING_TTL,
            }
        )
        return self.upstream.authorize_url(state=state, nonce=nonce, code_challenge=challenge)

    def _error_redirect(self, pending: dict[str, Any], error: str, description: str) -> Response:
        url = construct_redirect_uri(
            pending["redirect_uri"],
            error=error,
            error_description=description,
            state=pending["client_state"] if pending.get("has_state") else None,
            iss=self.issuer,
        )
        return RedirectResponse(url, status_code=302)

    async def _take(self, pk: str, rk: str) -> dict[str, Any] | None:
        """Load a single-use row and atomically mark it used."""
        row = await self.store.get(pk, rk)
        reason = None
        if row is None:
            reason = "unknown"
        elif row.get("used"):
            reason = "already used"
        elif int(row.get("expires_at", 0)) < _now():
            reason = "expired"
        elif not await self.store.consume(pk, rk, row["_etag"]):
            reason = "lost race"
        if reason:
            log.info("oauth single-use %s rejected: %s", pk.split("~")[1], reason)
            return None
        return row

    async def handle_callback(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        pending = await self._take(P_PENDING, _hash(state)) if state else None
        if pending is None:
            return _page("Sign-in link expired", "Start the connection again from your app.", 400)
        if request.query_params.get("error"):
            return self._error_redirect(pending, "access_denied", "sign-in was cancelled")
        code = request.query_params.get("code", "")
        try:
            subject = await self.upstream.redeem(
                code=code, code_verifier=pending["verifier"], nonce=pending["nonce"]
            )
        except UpstreamError as exc:
            log.warning("oauth upstream sign-in failed: %s", exc)
            return self._error_redirect(pending, "access_denied", "sign-in failed")
        if subject not in self.s.principals:
            log.info("oauth sign-in refused: principal not allowed (fp=%s)", _fingerprint(subject))
            return self._error_redirect(pending, "access_denied", "this account is not allowed")

        client = await self.get_client(pending["client_id"])
        if client is None:
            return _page("Unknown client", "The connecting app is no longer registered.", 400)
        if await self.store.get(P_CONSENT, f"{subject}|{client.client_id}") is not None:
            return await self._issue_code_redirect(pending, subject)

        approval_id = secrets.token_urlsafe(32)
        await self.store.create(
            {
                "PartitionKey": P_APPROVAL,
                "RowKey": _hash(approval_id),
                "pending": json.dumps({k: v for k, v in pending.items() if not k.startswith("_")}),
                "subject": subject,
                "expires_at": _now() + PENDING_TTL,
            }
        )
        return _consent_page(client, pending["redirect_uri"], pending["scopes"], approval_id)

    async def handle_approve(self, request: Request) -> Response:
        form = await request.form()
        approval_id = str(form.get("approval_id", ""))
        row = await self._take(P_APPROVAL, _hash(approval_id)) if approval_id else None
        if row is None:
            if not approval_id:
                log.info("oauth approve refused: missing approval_id")
            return _page("Request expired", "Start the connection again from your app.", 400)
        pending = json.loads(row["pending"])
        if form.get("decision") != "allow":
            return self._error_redirect(pending, "access_denied", "the user denied access")
        try:
            await self.store.create(
                {
                    "PartitionKey": P_CONSENT,
                    "RowKey": f"{row['subject']}|{pending['client_id']}",
                    "approved_at": _now(),
                }
            )
        except HubError as err:
            if err.code != "conflict":  # already approved concurrently: fine
                raise
        return await self._issue_code_redirect(pending, row["subject"])

    async def _issue_code_redirect(self, pending: dict[str, Any], subject: str) -> Response:
        code = secrets.token_urlsafe(32)
        await self.store.create(
            {
                "PartitionKey": P_CODE,
                "RowKey": _hash(code),
                "client_id": pending["client_id"],
                "redirect_uri": pending["redirect_uri"],
                "redirect_explicit": pending["redirect_explicit"],
                "code_challenge": pending["code_challenge"],
                "scopes": pending["scopes"],
                "subject": subject,
                "expires_at": _now() + CODE_TTL,
            }
        )
        url = construct_redirect_uri(
            pending["redirect_uri"],
            code=code,
            state=pending["client_state"] if pending.get("has_state") else None,
            iss=self.issuer,
        )
        return RedirectResponse(url, status_code=302)

    # ---------------------------------------------------------------- tokens

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> HubAuthorizationCode | None:
        row = await self.store.get(P_CODE, _hash(authorization_code))
        if row is None or row.get("used") or row["client_id"] != client.client_id:
            return None
        return HubAuthorizationCode(
            code=authorization_code,
            scopes=row["scopes"].split(),
            expires_at=float(row["expires_at"]),
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_explicit"]),
            resource=self.s.resource_url,
            subject=row["subject"],
            etag=row["_etag"],
            agent=agent_for_redirect(row["redirect_uri"]),
        )

    async def _issue_tokens(
        self, client_id: str, scopes: list[str], subject: str, agent: str, refresh_expires: int
    ) -> OAuthToken:
        access = ACCESS_PREFIX + secrets.token_urlsafe(32)
        refresh = REFRESH_PREFIX + secrets.token_urlsafe(32)
        common = {
            "client_id": client_id,
            "scopes": " ".join(scopes),
            "subject": subject,
            "agent": agent,
        }
        await self.store.create(
            {
                "PartitionKey": P_ACCESS,
                "RowKey": _hash(access),
                **common,
                "expires_at": _now() + self.s.oauth_access_ttl_seconds,
            }
        )
        await self.store.create(
            {
                "PartitionKey": P_REFRESH,
                "RowKey": _hash(refresh),
                **common,
                "expires_at": refresh_expires,
            }
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",  # noqa: S106 - OAuth token type, not a secret
            expires_in=self.s.oauth_access_ttl_seconds,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: HubAuthorizationCode
    ) -> OAuthToken:
        if not await self.store.consume(
            P_CODE, _hash(authorization_code.code), authorization_code.etag
        ):
            raise TokenError("invalid_grant", "authorization code was already used")
        assert authorization_code.subject
        return await self._issue_tokens(
            client.client_id,
            authorization_code.scopes,
            authorization_code.subject,
            authorization_code.agent,
            _now() + self.s.oauth_refresh_ttl_seconds,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> HubRefreshToken | None:
        if not refresh_token.startswith(REFRESH_PREFIX):
            return None
        row = await self.store.get(P_REFRESH, _hash(refresh_token))
        if row is None or row.get("used") or row["client_id"] != client.client_id:
            return None
        return HubRefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=int(row["expires_at"]),
            resource=self.s.resource_url,
            subject=row["subject"],
            etag=row["_etag"],
            agent=row["agent"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: HubRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if not await self.store.consume(P_REFRESH, _hash(refresh_token.token), refresh_token.etag):
            raise TokenError("invalid_grant", "refresh token was already used")
        if refresh_token.subject not in self.s.principals:
            raise TokenError("invalid_grant", "account is no longer allowed")
        requested = scopes or refresh_token.scopes
        if not set(requested) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "cannot widen scopes on refresh")
        assert refresh_token.subject and refresh_token.expires_at
        return await self._issue_tokens(
            client.client_id,
            requested,
            refresh_token.subject,
            refresh_token.agent,
            refresh_token.expires_at,  # absolute lifetime: rotation does not extend it
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        if not token.startswith(ACCESS_PREFIX):
            # Entra JWT path (Claude Code / Codex via the Azure CLI).
            return await self.jwt_verifier.verify_token(token)
        try:
            row = await self.store.get(P_ACCESS, _hash(token))
        except HubError:
            return None
        if row is None or row.get("used") or int(row["expires_at"]) < _now():
            return None
        if row["subject"] not in self.s.principals:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=int(row["expires_at"]),
            resource=self.s.resource_url,
            subject=row["subject"],
            claims={"iss": self.issuer, "agent": row["agent"]},
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        digest = _hash(token.token)
        await self.store.delete(P_ACCESS, digest)
        await self.store.delete(P_REFRESH, digest)


# ---------------------------------------------------------------- pages


_PAGE_HEADERS = {
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    ),
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}
_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;"
    "line-height:1.5;color:#1f2328;background:#fff}"
    "@media(prefers-color-scheme:dark){body{color:#e6edf3;background:#0d1117}}"
    "button{font-size:1rem;padding:.5rem 1.25rem;margin-right:.5rem;border-radius:6px;"
    "border:1px solid #8b949e;cursor:pointer}button.allow{background:#1f883d;color:#fff;border:0}"
    "code{word-break:break-all}"
)


def _page(title: str, text: str, status: int = 200) -> HTMLResponse:
    body = (
        f"<!doctype html><html lang=en><meta charset=utf-8><title>{html.escape(title)}</title>"
        f"<style>{_STYLE}</style><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p></html>"
    )
    return HTMLResponse(body, status_code=status, headers=_PAGE_HEADERS)


def _consent_page(
    client: OAuthClientInformationFull, redirect_uri: str, scopes: str, approval_id: str
) -> HTMLResponse:
    name = html.escape(client.client_name or "An unnamed app")
    host = html.escape(urlparse(redirect_uri).netloc or redirect_uri)
    access = "read and write" if "ReadWrite" in scopes else "read"
    body = f"""<!doctype html><html lang=en><meta charset=utf-8>
<title>Allow access to AI Collab Hub?</title><style>{_STYLE}</style>
<h1>Allow access to AI Collab Hub?</h1>
<p><strong>{name}</strong> wants to {access} your AI Collab Hub records
(memories, decisions, tasks, messages).</p>
<p>After you allow it, the app will be sent to <code>{host}</code>.
Only continue if you started this connection yourself.</p>
<form method=post action="{APPROVE_PATH}">
<input type=hidden name=approval_id value="{html.escape(approval_id)}">
<button class=allow name=decision value=allow>Allow</button>
<button name=decision value=deny>Deny</button>
</form></html>"""
    # Chrome applies CSP form-action to the redirect that follows the POST, so
    # the client's (allowlisted) redirect origin must be allowed explicitly;
    # with 'self' alone the 302 to claude.ai was silently blocked (2026-10-04).
    u = urlparse(redirect_uri)
    headers = dict(_PAGE_HEADERS)
    headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; "
        f"form-action 'self' {u.scheme}://{u.netloc}; frame-ancestors 'none'"
    )
    return HTMLResponse(body, headers=headers)
