"""OAuth authorization server (ChatGPT / Claude.ai connectors), end to end over HTTP.

The Entra sign-in is replaced by a fake upstream; everything else (DCR, consent,
PKCE, tokens, refresh rotation, MCP calls with hub tokens) is the real code on
Azurite.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from collab_hub.app import create_app
from collab_hub.config import Settings
from collab_hub.oauth import UpstreamError, redirect_allowed

from .conftest import OWNER, STRANGER, _free_port, _wait_port, payload

CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
CHATGPT_CB = "https://chatgpt.com/connector_platform_oauth_redirect"


class FakeUpstream:
    """Stands in for Entra: code 'ok:<oid>' signs in <oid>; 'fail' fails."""

    def authorize_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        return f"https://idp.test/authorize?state={state}&nonce={nonce}"

    async def redeem(self, *, code: str, code_verifier: str, nonce: str) -> str:
        if not code.startswith("ok:"):
            raise UpstreamError("upstream refused")
        return code[3:]


@pytest.fixture(scope="session")
def oauth_url(settings: Settings):
    port = _free_port()
    s = settings.model_copy(
        update={
            "oauth_enabled": True,
            "oauth_login_client_id": "login-test",
            "public_url": f"http://127.0.0.1:{port}",
        }
    )
    s.__dict__.pop("host_allowlist", None)  # cached_property from the copied settings
    app = create_app(s, upstream=FakeUpstream())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_port(port, 30)
    yield s.public_url
    server.should_exit = True
    thread.join(10)


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    return verifier, challenge.rstrip("=")


async def register(http, base, redirect=CLAUDE_CB, name="Claude"):
    return await http.post(
        f"{base}/register",
        json={
            "redirect_uris": [redirect],
            "client_name": name,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )


async def authorize(
    http, base, client_id, challenge, redirect=CLAUDE_CB, resource=None, state="st-1"
):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "Collab.ReadWrite",
        "resource": resource or f"{base}/mcp",
    }
    return await http.get(f"{base}/authorize", params=params)


def upstream_state(resp) -> str:
    assert resp.status_code == 302, resp.text
    loc = urlparse(resp.headers["location"])
    assert loc.hostname == "idp.test"
    return parse_qs(loc.query)["state"][0]


async def sign_in(http, base, client_id, oid=OWNER, redirect=CLAUDE_CB, decision="allow"):
    """Run authorize -> fake Entra -> consent (if shown). Returns (final response, verifier)."""
    verifier, challenge = pkce()
    st = upstream_state(await authorize(http, base, client_id, challenge, redirect))
    resp = await http.get(f"{base}/oauth/callback", params={"code": f"ok:{oid}", "state": st})
    if resp.status_code == 200 and "Allow access" in resp.text:
        approval = re.search(r'name=approval_id value="([^"]+)"', resp.text).group(1)
        assert resp.headers["x-frame-options"] == "DENY"
        resp = await http.post(
            f"{base}/oauth/approve", data={"approval_id": approval, "decision": decision}
        )
    return resp, verifier


def redirect_params(resp) -> dict[str, str]:
    assert resp.status_code == 302, resp.text
    q = parse_qs(urlparse(resp.headers["location"]).query)
    return {k: v[0] for k, v in q.items()}


async def token(http, base, client_id, code, verifier, redirect=CLAUDE_CB):
    return await http.post(
        f"{base}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": f"{base}/mcp",
        },
    )


async def test_metadata_advertises_hub_as_authorization_server(oauth_url):
    async with httpx2.AsyncClient() as http:
        prm = (await http.get(f"{oauth_url}/.well-known/oauth-protected-resource/mcp")).json()
        meta = (await http.get(f"{oauth_url}/.well-known/oauth-authorization-server")).json()
    assert prm["authorization_servers"] == [f"{oauth_url}/"]
    assert meta["issuer"] == f"{oauth_url}/"  # must equal the PRM entry exactly
    assert meta["registration_endpoint"] == f"{oauth_url}/register"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert set(meta["scopes_supported"]) == {"Collab.Read", "Collab.ReadWrite"}


async def test_full_flow_then_mcp_call_with_hub_token(oauth_url):
    async with httpx2.AsyncClient() as http:
        client_id = (await register(http, oauth_url)).json()["client_id"]
        resp, verifier = await sign_in(http, oauth_url, client_id)
        params = redirect_params(resp)
        assert resp.headers["location"].startswith(CLAUDE_CB)
        assert params["state"] == "st-1" and params["iss"] == f"{oauth_url}/"
        tok = await token(http, oauth_url, client_id, params["code"], verifier)
        assert tok.status_code == 200, tok.text
        body = tok.json()
        assert body["access_token"].startswith("chb_at_") and body["refresh_token"]

        replay = await token(http, oauth_url, client_id, params["code"], verifier)
        assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"

    # The connector sends no X-Collab-Agent header; the agent comes from the client.
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=30) as http,
        Client(streamable_http_client(f"{oauth_url}/mcp", http_client=http)) as client,
    ):
        slug = f"oa-{secrets.token_hex(4)}"
        payload(
            await client.call_tool(
                "project_create", {"slug": slug, "name": "OAuth", "purpose": "connector test"}
            )
        )
        projects = payload(await client.call_tool("project_list", {"limit": 50}))
        mine = next(p for p in projects["items"] if p["slug"] == slug)
        assert mine["created_by"] == "owner/claude-desktop"


async def test_consent_is_remembered_per_client(oauth_url):
    async with httpx2.AsyncClient() as http:
        client_id = (await register(http, oauth_url)).json()["client_id"]
        first, _ = await sign_in(http, oauth_url, client_id)
        assert "code" in redirect_params(first)
        # Second sign-in: no consent page, straight back to the client.
        _, challenge = pkce()
        st = upstream_state(await authorize(http, oauth_url, client_id, challenge))
        again = await http.get(
            f"{oauth_url}/oauth/callback", params={"code": f"ok:{OWNER}", "state": st}
        )
        assert "code" in redirect_params(again)


async def test_refresh_rotation_and_reuse_detection(oauth_url):
    async with httpx2.AsyncClient() as http:
        client_id = (await register(http, oauth_url, CHATGPT_CB, "ChatGPT")).json()["client_id"]
        resp, verifier = await sign_in(http, oauth_url, client_id, redirect=CHATGPT_CB)
        code = redirect_params(resp)["code"]
        first = (await token(http, oauth_url, client_id, code, verifier, CHATGPT_CB)).json()
        data = {
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
            "client_id": client_id,
        }
        second = await http.post(f"{oauth_url}/token", data=data)
        assert (
            second.status_code == 200 and second.json()["refresh_token"] != first["refresh_token"]
        )
        reuse = await http.post(f"{oauth_url}/token", data=data)
        assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"

        widen = await http.post(
            f"{oauth_url}/token",
            data={**data, "refresh_token": second.json()["refresh_token"], "scope": "Collab.Admin"},
        )
        assert widen.status_code == 400


@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example/cb",
        "http://localhost:1@evil.example/cb",
        "https://claude.ai.evil.example/api/mcp/auth_callback",
        "https://claude.ai/api/mcp/auth_callback?next=https://evil.example",
        "javascript:alert(1)",
    ],
)
async def test_registration_rejects_unlisted_redirects(oauth_url, redirect):
    async with httpx2.AsyncClient() as http:
        resp = await register(http, oauth_url, redirect)
    assert resp.status_code == 400
    assert resp.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")


async def test_stranger_and_denied_and_failed_signins_get_no_code(oauth_url):
    async with httpx2.AsyncClient() as http:
        client_id = (await register(http, oauth_url)).json()["client_id"]
        stranger, _ = await sign_in(http, oauth_url, client_id, oid=STRANGER)
        assert redirect_params(stranger)["error"] == "access_denied"
        denied, _ = await sign_in(http, oauth_url, client_id, decision="deny")
        p = redirect_params(denied)
        assert p["error"] == "access_denied" and "code" not in p

        _, challenge = pkce()
        st = upstream_state(await authorize(http, oauth_url, client_id, challenge))
        failed = await http.get(f"{oauth_url}/oauth/callback", params={"code": "fail", "state": st})
        assert redirect_params(failed)["error"] == "access_denied"
        # The state is single use.
        reused = await http.get(
            f"{oauth_url}/oauth/callback", params={"code": f"ok:{OWNER}", "state": st}
        )
        assert reused.status_code == 400


async def test_wrong_resource_is_refused(oauth_url):
    async with httpx2.AsyncClient() as http:
        client_id = (await register(http, oauth_url)).json()["client_id"]
        _, challenge = pkce()
        resp = await authorize(
            http, oauth_url, client_id, challenge, resource="https://other.example/mcp"
        )
    assert redirect_params(resp)["error"] == "invalid_target"


async def test_entra_jwt_still_accepted_and_bad_hub_token_rejected(oauth_url, mint):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    accept = {"Accept": "application/json, text/event-stream"}
    async with httpx2.AsyncClient() as http:
        ok = await http.post(
            f"{oauth_url}/mcp", json=body, headers={**accept, "Authorization": f"Bearer {mint()}"}
        )
        bad = await http.post(
            f"{oauth_url}/mcp",
            json=body,
            headers={**accept, "Authorization": "Bearer chb_at_forged"},
        )
    assert ok.status_code == 200
    assert bad.status_code == 401


def test_redirect_allowlist_matching():
    allow = Settings.model_fields["oauth_redirect_allowlist"].default.split(",")
    assert redirect_allowed(CLAUDE_CB, allow)
    assert redirect_allowed("https://chatgpt.com/connector/oauth/abc123", allow)
    assert redirect_allowed("http://localhost:33418/callback", allow)
    assert redirect_allowed("http://127.0.0.1:5000/cb", allow)
    assert not redirect_allowed("http://localhost:1@evil.example/", allow)
    assert not redirect_allowed("https://chatgpt.com.evil.example/connector/oauth/x", allow)
    assert not redirect_allowed("https://claude.ai/other", allow)
    assert not redirect_allowed("http://claude.ai/api/mcp/auth_callback", allow)
    assert time.time() > 0


def test_entra_sign_in_requests_profile_scope_for_oid(settings):
    """Entra v2 id_tokens carry `oid` only with the `profile` scope (live bug 2026-10-04)."""
    from collab_hub.oauth import EntraUpstream

    s = settings.model_copy(
        update={"oauth_enabled": True, "oauth_login_client_id": "login-app", "tenant_id": "tid"}
    )
    url = EntraUpstream(s, credential=None).authorize_url(state="s", nonce="n", code_challenge="c")
    scope = parse_qs(urlparse(url).query)["scope"][0].split()
    assert {"openid", "profile"} <= set(scope)
