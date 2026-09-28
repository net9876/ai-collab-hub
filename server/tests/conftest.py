"""Test harness: Azurite emulator + real HTTP server + real MCP client.

* Azurite (Blob + Table) is started with `npx azurite` on free ports, in-memory,
  unless AZURITE_CONNECTION_STRING points at a running instance.
* Tokens are RS256 JWTs signed by a key generated per test session; the server
  trusts it through COLLAB_OIDC_JWKS_JSON. There is no auth bypass in the app.
* The app runs under uvicorn in a background thread, and tests talk to it over
  HTTP with the official MCP client (handshake, tools/list, tools/call).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from collab_hub.app import create_app
from collab_hub.config import Settings
from collab_hub.store import Store

ISSUER = "https://login.example.test/tenant-1/v2.0"
AUDIENCE = "00000000-0000-0000-0000-00000000c0de"
TENANT = "tenant-1"
OWNER = "11111111-1111-1111-1111-111111111111"
READER = "22222222-2222-2222-2222-222222222222"
OTHER_WS = "33333333-3333-3333-3333-333333333333"
STRANGER = "99999999-9999-9999-9999-999999999999"
KID = "test-key-1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_port(port: int, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"port {port} did not open")


@pytest.fixture(scope="session")
def azurite() -> Iterator[str]:
    preset = os.environ.get("AZURITE_CONNECTION_STRING")
    if preset:
        yield preset
        return
    node = shutil.which("node")
    local = Path(__file__).resolve().parents[2] / ".tools/node_modules/azurite/dist/src/azurite.js"
    npx = shutil.which("npx")
    if node and local.exists():
        launcher = [node, str(local)]  # installed by scripts/setup.ps1 (fast, offline)
    elif npx:
        launcher = [npx, "--yes", "azurite@3.37.0"]
    else:
        pytest.skip("Node.js not found; install it or set AZURITE_CONNECTION_STRING")
    blob, queue, table = _free_port(), _free_port(), _free_port()
    workdir = tempfile.mkdtemp(prefix="azurite-")
    cmd = [
        *launcher,
        "--silent",
        "--inMemoryPersistence",
        "--skipApiVersionCheck",
        "--loose",
        "--blobPort",
        str(blob),
        "--queuePort",
        str(queue),
        "--tablePort",
        str(table),
    ]
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, test only
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        _wait_port(blob, 180)
        _wait_port(table, 60)
        # Well-known Azurite development account (public, emulator-only credential).
        key = (  # gitleaks:allow
            "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
        )
        yield (
            "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
            f"AccountKey={key};"
            f"BlobEndpoint=http://127.0.0.1:{blob}/devstoreaccount1;"
            f"TableEndpoint=http://127.0.0.1:{table}/devstoreaccount1;"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if os.name == "nt":  # npx spawns node as a child; make sure it is gone
            subprocess.run(  # noqa: S603
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],  # noqa: S607
                capture_output=True,
                check=False,
            )
        shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwks_json(signing_key: rsa.RSAPrivateKey) -> str:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return json.dumps({"keys": [jwk]})


@pytest.fixture(scope="session")
def mint(signing_key: rsa.RSAPrivateKey):
    def _mint(
        oid: str = OWNER,
        scp: str = "Collab.ReadWrite",
        aud: str = AUDIENCE,
        iss: str = ISSUER,
        tid: str = TENANT,
        exp_in: int = 3600,
        key: Any = None,
        kid: str = KID,
        alg: str = "RS256",
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": iss,
            "aud": aud,
            "tid": tid,
            "oid": oid,
            "scp": scp,
            "azp": "04b07795-8ddb-461a-bbee-02f9e1bf7b46",
            "iat": now - 10,
            "nbf": now - 10,
            "exp": now + exp_in,
            "name": "Should Not Be Logged",
        }
        if alg == "HS256":
            return jwt.encode(claims, "x" * 32, algorithm="HS256", headers={"kid": kid})
        return jwt.encode(claims, key or signing_key, algorithm="RS256", headers={"kid": kid})

    return _mint


@pytest.fixture(scope="session")
def settings(azurite: str, jwks_json: str) -> Settings:
    port = _free_port()
    suffix = uuid.uuid4().hex[:8]
    return Settings(
        storage_connection_string=azurite,
        table_name=f"hub{suffix}",
        blob_container=f"content-{suffix}",
        oidc_issuer=ISSUER,
        oidc_jwks_json=jwks_json,
        oidc_audience=AUDIENCE,
        tenant_id=TENANT,
        principals={
            OWNER: {"alias": "owner", "workspace": "main", "role": "writer"},
            READER: {"alias": "viewer", "workspace": "main", "role": "reader"},
            OTHER_WS: {"alias": "guest", "workspace": "sandbox", "role": "writer"},
        },
        public_url=f"http://127.0.0.1:{port}",
        log_level="INFO",
    )


@pytest.fixture(scope="session")
def hub_app(settings: Settings) -> Any:
    return create_app(settings)


@pytest.fixture(scope="session")
async def server_url(settings: Settings, hub_app: Any) -> AsyncIterator[str]:
    store = Store(settings)
    await store.ensure_created()
    await store.close()
    app = hub_app
    port = int(settings.public_url.rsplit(":", 1)[1])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_port(port, 30)
    yield settings.public_url
    server.should_exit = True
    thread.join(10)


@pytest.fixture(scope="session")
def open_client(server_url: str):
    @asynccontextmanager
    async def _open(
        token: str | None, agent: str = "claude-code", mode: str = "auto"
    ) -> AsyncIterator[Client]:
        headers = {"X-Collab-Agent": agent}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        http = httpx2.AsyncClient(headers=headers, timeout=30)
        transport = streamable_http_client(f"{server_url}/mcp", http_client=http)
        async with http, Client(transport, mode=mode) as client:  # type: ignore[arg-type]
            yield client

    return _open


def payload(result: Any) -> Any:
    """Structured content of a successful tool result."""
    assert not result.is_error, result.content
    data = result.structured_content
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def error_text(result: Any) -> str:
    assert result.is_error, result.structured_content
    return " ".join(getattr(c, "text", "") for c in result.content)
