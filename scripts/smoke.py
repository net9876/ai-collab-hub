"""Authenticated end-to-end smoke test against a deployed hub.

Uses the official MCP client over HTTPS with a token from the Azure CLI.
Read-only by default; --write also creates (idempotently) a `smoke-test`
project and runs one task through create -> claim -> complete.

    $env:COLLAB_MCP_URL = "https://<fqdn>/mcp"
    $env:COLLAB_API_SCOPE = "api://<client-id>/Collab.ReadWrite"
    .venv\\Scripts\\python.exe scripts/smoke.py [--write]
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import uuid

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


def token(scope: str) -> str:
    az = shutil.which("az") or shutil.which("az.cmd")
    if not az:
        sys.exit("Azure CLI not found")
    out = subprocess.run(  # noqa: S603 - fixed argv
        [
            az,
            "account",
            "get-access-token",
            "--scope",
            scope,
            "--query",
            "accessToken",
            "-o",
            "tsv",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        sys.exit("could not get a token; run az login (stderr suppressed)")
    return out.stdout.strip()


def ok(result) -> dict:
    if result.is_error:
        raise SystemExit(f"tool error: {result.content}")
    return result.structured_content


async def main(write: bool) -> None:
    url, scope = os.environ["COLLAB_MCP_URL"], os.environ["COLLAB_API_SCOPE"]
    async with httpx2.AsyncClient() as anon:
        r = await anon.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert r.status_code == 401, f"expected 401 without token, got {r.status_code}"
        print("unauthenticated call rejected: 401")
    headers = {"Authorization": f"Bearer {token(scope)}", "X-Collab-Agent": "other"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=60) as http,
        Client(streamable_http_client(url, http_client=http)) as client,
    ):  # type: ignore[arg-type]
        tools = await client.list_tools()
        print(f"tools/list: {len(tools.tools)} tools")
        projects = ok(await client.call_tool("project_list", {"limit": 5}))
        print(f"project_list: {len(projects['items'])} item(s)")
        if write:
            ok(
                await client.call_tool(
                    "project_create",
                    {
                        "slug": "smoke-test",
                        "name": "Smoke test",
                        "purpose": "deployment checks",
                        "idempotency_key": "smoke-test-project",
                    },
                )
            )
            t = ok(
                await client.call_tool(
                    "task_create",
                    {
                        "project": "smoke-test",
                        "title": f"smoke {uuid.uuid4().hex[:6]}",
                    },
                )
            )
            c = ok(await client.call_tool("task_claim", {"task_id": t["id"]}))
            d = ok(
                await client.call_tool(
                    "task_complete",
                    {
                        "task_id": t["id"],
                        "expected_revision": c["revision"],
                        "result": "smoke test passed",
                        "evidence": ["scripts/smoke.py --write"],
                    },
                )
            )
            print(f"task lifecycle ok: {d['status']} by {d['updated_by']}")
    print("SMOKE OK")


if __name__ == "__main__":
    asyncio.run(main("--write" in sys.argv))
