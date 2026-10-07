"""Read-only dashboard: sign-in, allow-list, hidden projects, no bodies, logout."""

from __future__ import annotations

import contextlib
import threading
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
import uvicorn

from collab_hub.app import create_app
from collab_hub.auth import Principal
from collab_hub.config import Settings
from collab_hub.service import HubService
from collab_hub.store import Store

from .conftest import OWNER, STRANGER, _free_port, _wait_port
from .test_oauth import FakeUpstream

SECRET_BODY = "SECRET-TASK-BODY-do-not-show"


@pytest.fixture(scope="module")
async def dash_url(settings: Settings, server_url):
    port = _free_port()
    s = settings.model_copy(
        update={
            "oauth_enabled": True,
            "oauth_login_client_id": "login-test",
            "dashboard_enabled": True,
            "public_url": f"http://127.0.0.1:{port}",
        }
    )
    s.__dict__.pop("host_allowlist", None)
    # Seed data through a separate service instance in this event loop.
    store = Store(s)
    svc = HubService(s, store)
    w = Principal(OWNER, "owner", "main", "writer", "other", "t", frozenset({s.scope_write}))
    for slug, name in (("dash-visible", "Visible lab"), ("personal-dash-secret", "Hidden private")):
        with contextlib.suppress(Exception):  # already seeded by an earlier run
            await svc.project_create(w, slug, name, "purpose text", None)
            await svc.task_create(w, slug, f"Task in {slug}", SECRET_BODY, "high", [], None)
    await store.close()
    app = create_app(s, upstream=FakeUpstream())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_port(port, 30)
    yield s.public_url
    server.should_exit = True
    thread.join(10)


async def sign_in(base: str, oid: str):
    """Returns (callback response, session cookie value or None)."""
    async with httpx2.AsyncClient(follow_redirects=False) as http:
        r = await http.get(f"{base}/dashboard/login")
        assert r.status_code == 302
        loc = urlparse(r.headers["location"])
        assert parse_qs(loc.query)["cb"] == ["/dashboard/callback"]
        state = parse_qs(loc.query)["state"][0]
        login_cookie = r.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        cb = await http.get(
            f"{base}/dashboard/callback",
            params={"code": f"ok:{oid}", "state": state},
            headers={"cookie": f"__Host-collab_login={login_cookie}"},
        )
        sess = None
        for h in cb.headers.get_list("set-cookie"):
            if h.startswith("__Host-collab_dash="):
                sess = h.split(";")[0].split("=", 1)[1]
        return cb, sess, state, login_cookie


async def test_anonymous_gets_sign_in_page_only(dash_url):
    async with httpx2.AsyncClient() as http:
        r = await http.get(f"{dash_url}/dashboard")
    assert (
        r.status_code == 200 and 'href="/dashboard/login"' in r.text and "Visible lab" not in r.text
    )
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


async def test_owner_sees_projects_and_tasks_but_not_hidden_or_bodies(dash_url):
    cb, sess, _, _ = await sign_in(dash_url, OWNER)
    assert cb.status_code == 303 and sess
    async with httpx2.AsyncClient() as http:
        r = await http.get(
            f"{dash_url}/dashboard", headers={"cookie": f"__Host-collab_dash={sess}"}
        )
    assert r.status_code == 200
    assert "Visible lab" in r.text and "Task in dash-visible" in r.text
    assert "personal-dash-secret" not in r.text and "Hidden private" not in r.text
    assert SECRET_BODY not in r.text


async def test_unlisted_account_is_refused(dash_url):
    cb, sess, _, _ = await sign_in(dash_url, STRANGER)
    assert cb.status_code == 403 and sess is None


async def test_login_is_bound_to_browser_and_single_use(dash_url):
    _, _, state, cookie = await sign_in(dash_url, OWNER)
    async with httpx2.AsyncClient(follow_redirects=False) as http:
        replay = await http.get(
            f"{dash_url}/dashboard/callback",
            params={"code": f"ok:{OWNER}", "state": state},
            headers={"cookie": f"__Host-collab_login={cookie}"},
        )
        no_cookie = await http.get(
            f"{dash_url}/dashboard/callback", params={"code": f"ok:{OWNER}", "state": state}
        )
    assert replay.status_code == 400 and no_cookie.status_code == 400


async def test_bad_cookie_and_logout(dash_url):
    async with httpx2.AsyncClient(follow_redirects=False) as http:
        r = await http.get(f"{dash_url}/dashboard", headers={"cookie": "__Host-collab_dash=nope"})
        assert "Visible lab" not in r.text
        _, sess, _, _ = await sign_in(dash_url, OWNER)
        hdr = {"cookie": f"__Host-collab_dash={sess}"}
        assert "Visible lab" in (await http.get(f"{dash_url}/dashboard", headers=hdr)).text
        assert (await http.post(f"{dash_url}/dashboard/logout", headers=hdr)).status_code == 303
        assert "Visible lab" not in (await http.get(f"{dash_url}/dashboard", headers=hdr)).text
