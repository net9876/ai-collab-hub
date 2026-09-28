"""Real MCP over HTTP: handshake, tools/list, tools/call, auth deny paths."""

from __future__ import annotations

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from .conftest import AUDIENCE, OWNER, READER, STRANGER, error_text, payload

EXPECTED_TOOLS = {
    "project_create",
    "project_list",
    "project_get_context",
    "memory_add",
    "memory_get",
    "memory_search",
    "memory_update",
    "decision_add",
    "decision_search",
    "task_create",
    "task_get",
    "task_list",
    "task_claim",
    "task_update",
    "task_complete",
    "task_block",
    "message_send",
    "message_inbox",
    "message_reply",
    "message_mark_read",
}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_handshake_and_tool_list(open_client, mint, mode):
    async with open_client(mint(), mode=mode) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert names == EXPECTED_TOOLS
        claim = next(t for t in tools.tools if t.name == "task_claim")
        assert claim.annotations is not None and claim.annotations.read_only_hint is False
        schema = next(t for t in tools.tools if t.name == "memory_add").input_schema
        assert schema["properties"]["project"]["pattern"]
        assert "project" in schema["required"]
        # No generic shell/command/path tools.
        assert not {n for n in names if any(w in n for w in ("run", "exec", "shell", "file"))}


async def test_tools_call_roundtrip(open_client, mint):
    async with open_client(mint(), mode="legacy") as client:
        res = await client.call_tool(
            "project_create",
            {"slug": "proto-test", "name": "Protocol test", "purpose": "handshake test"},
        )
        assert payload(res)["id"] == "proto-test"
        res = await client.call_tool("project_list", {"limit": 50})
        assert "proto-test" in {p["slug"] for p in payload(res)["items"]}


async def test_healthz_is_public_and_minimal(server_url):
    async with httpx2.AsyncClient() as http:
        r = await http.get(f"{server_url}/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_protected_resource_metadata(server_url):
    async with httpx2.AsyncClient() as http:
        r = await http.get(f"{server_url}/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    meta = r.json()
    assert meta["resource"] == f"{server_url}/mcp"
    assert meta["authorization_servers"]


async def _raw_post(server_url: str, token: str | None) -> httpx2.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    async with httpx2.AsyncClient() as http:
        return await http.post(f"{server_url}/mcp", json=body, headers=headers)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "garbage",
        "wrong_aud",
        "wrong_iss",
        "expired",
        "wrong_key",
        "stranger",
        "wrong_tenant",
        "no_scope",
        "hs256",
    ],
)
async def test_auth_denied(server_url, mint, case):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = {
        "missing": None,
        "garbage": "not-a-jwt",
        "wrong_aud": mint(aud="api://someone-else"),
        "wrong_iss": mint(iss="https://evil.example/v2.0"),
        "expired": mint(exp_in=-3600),
        "wrong_key": mint(key=other_key),
        "stranger": mint(oid=STRANGER),
        "wrong_tenant": mint(tid="tenant-2"),
        "no_scope": mint(scp="User.Read"),
        "hs256": mint(alg="HS256"),
    }[case]
    r = await _raw_post(server_url, token)
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers.get("www-authenticate", "")


async def test_audience_is_the_api_client_id(server_url, mint):
    r = await _raw_post(server_url, mint(aud=AUDIENCE))
    assert r.status_code == 200


async def test_reader_cannot_write(open_client, mint):
    async with open_client(mint(oid=READER, scp="Collab.Read")) as client:
        res = await client.call_tool("project_list", {})
        assert not res.is_error
        res = await client.call_tool(
            "project_create", {"slug": "nope", "name": "Nope", "purpose": "should fail"}
        )
        assert "forbidden" in error_text(res)


async def test_read_scope_alone_cannot_write(open_client, mint):
    async with open_client(mint(oid=OWNER, scp="Collab.Read")) as client:
        res = await client.call_tool(
            "project_create", {"slug": "nope2", "name": "Nope", "purpose": "should fail"}
        )
        assert "forbidden" in error_text(res)


async def test_payload_limit(server_url, mint, settings):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {mint()}",
    }
    big = "x" * (settings.max_body_bytes + 10)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"pad": big}}
    async with httpx2.AsyncClient() as http:
        r = await http.post(f"{server_url}/mcp", json=body, headers=headers)
    assert r.status_code == 413


async def test_dns_rebinding_host_rejected(server_url, mint):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {mint()}",
        "Host": "evil.example",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    async with httpx2.AsyncClient() as http:
        r = await http.post(f"{server_url}/mcp", json=body, headers=headers)
    assert r.status_code in (400, 403, 421)


async def test_schema_validation_rejects_bad_input(open_client, mint):
    async with open_client(mint()) as client:
        res = await client.call_tool(
            "project_create", {"slug": "Bad Slug!", "name": "x", "purpose": "y"}
        )
        assert res.is_error
