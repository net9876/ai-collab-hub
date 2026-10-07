"""Read-only web dashboard: projects, task status, open issues.

Signed in with the same Entra login app as the connector OAuth flow (PKCE,
managed-identity client assertion: no secret). Only identities in
COLLAB_PRINCIPALS get a session. The page is server-rendered HTML without
JavaScript; it shows titles and statuses only (no memory, message or task
bodies), and projects whose slug starts with a hidden prefix never leave the
server. Sessions are random 256-bit tokens kept as SHA-256 hashes in the table.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import secrets
import time
from collections import Counter
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .auth import Principal, _fingerprint
from .config import Settings
from .models import Project, Task
from .oauth import Upstream, UpstreamError
from .service import HubService

log = logging.getLogger("collab_hub.dashboard")

DASH_PATH = "/dashboard"
LOGIN_PATH = "/dashboard/login"
CALLBACK_PATH = "/dashboard/callback"
LOGOUT_PATH = "/dashboard/logout"

P_PENDING = "dash~pending"
P_SESSION = "dash~session"
PENDING_TTL = 600
SESSION_COOKIE = "__Host-collab_dash"
LOGIN_COOKIE = "__Host-collab_login"

HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

_ACTIVE = ("blocked", "in_progress", "open")  # display order
_MAX_PAGES = 6


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> int:
    return int(time.time())


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


class Dashboard:
    def __init__(self, settings: Settings, service: HubService, upstream: Upstream) -> None:
        self.s = settings
        self.service = service
        self.store = service.store
        self.upstream = upstream
        self.hidden = tuple(
            x.strip() for x in settings.dashboard_hidden_prefixes.split(",") if x.strip()
        )

    # ------------------------------------------------------------ sign-in

    async def login(self, request: Request) -> Response:
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        await self.store.create(
            {
                "PartitionKey": P_PENDING,
                "RowKey": _hash(state),
                "nonce": nonce,
                "verifier": verifier,
                "expires_at": _now() + PENDING_TTL,
            }
        )
        url = self.upstream.authorize_url(
            state=state, nonce=nonce, code_challenge=challenge, callback_path=CALLBACK_PATH
        )
        resp = RedirectResponse(url, status_code=302, headers=HEADERS)
        # Binds the sign-in to this browser (login CSRF protection).
        resp.set_cookie(
            LOGIN_COOKIE, state, max_age=PENDING_TTL, secure=True, httponly=True, samesite="lax"
        )
        return resp

    async def callback(self, request: Request) -> Response:
        state = request.query_params.get("state", "")
        bound = request.cookies.get(LOGIN_COOKIE, "")
        if not state or not secrets.compare_digest(state, bound):
            return _page("Sign-in link expired", "Open the dashboard again to sign in.", 400)
        pending = await self._take(P_PENDING, _hash(state))
        if pending is None or request.query_params.get("error"):
            return _page("Sign-in cancelled or expired", "Open the dashboard again.", 400)
        try:
            subject = await self.upstream.redeem(
                code=request.query_params.get("code", ""),
                code_verifier=pending["verifier"],
                nonce=pending["nonce"],
                callback_path=CALLBACK_PATH,
            )
        except UpstreamError as exc:
            log.warning("dashboard sign-in failed: %s", exc)
            return _page("Sign-in failed", "Try again.", 400)
        if subject not in self.s.principals:
            log.info("dashboard sign-in refused (fp=%s)", _fingerprint(subject))
            return _page("Not allowed", "This account is not allowed here.", 403)
        token = secrets.token_urlsafe(32)
        ttl = self.s.dashboard_session_hours * 3600
        await self.store.create(
            {
                "PartitionKey": P_SESSION,
                "RowKey": _hash(token),
                "subject": subject,
                "expires_at": _now() + ttl,
            }
        )
        log.info("dashboard sign-in ok (fp=%s)", _fingerprint(subject))
        resp = RedirectResponse(DASH_PATH, status_code=303, headers=HEADERS)
        resp.set_cookie(
            SESSION_COOKIE, token, max_age=ttl, secure=True, httponly=True, samesite="lax", path="/"
        )
        resp.delete_cookie(LOGIN_COOKIE, secure=True, httponly=True, samesite="lax")
        return resp

    async def logout(self, request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            await self.store.delete(P_SESSION, _hash(token))
        resp = RedirectResponse(DASH_PATH, status_code=303, headers=HEADERS)
        resp.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax")
        return resp

    async def _take(self, pk: str, rk: str) -> dict[str, Any] | None:
        row = await self.store.get(pk, rk)
        if row is None or row.get("used") or int(row.get("expires_at", 0)) < _now():
            return None
        return row if await self.store.consume(pk, rk, row["_etag"]) else None

    async def _subject(self, request: Request) -> str | None:
        token = request.cookies.get(SESSION_COOKIE, "")
        if not token:
            return None
        row = await self.store.get(P_SESSION, _hash(token))
        if row is None or int(row.get("expires_at", 0)) < _now():
            return None
        subject = row.get("subject")
        # Re-checked on every request: removing a principal ends its sessions.
        return subject if isinstance(subject, str) and subject in self.s.principals else None

    # ------------------------------------------------------------ page

    async def page(self, request: Request) -> Response:
        subject = await self._subject(request)
        if subject is None:
            return _page(
                "AI Collab Hub",
                "Sign in with your Microsoft account to see projects and tasks.",
                200,
                action=(LOGIN_PATH, "GET", "Sign in"),
            )
        grant = self.s.principals[subject]
        p = Principal(
            subject=subject,
            alias=grant.alias,
            workspace=grant.workspace,
            role=grant.role,
            agent="other",
            client_id="dashboard",
            scopes=frozenset({self.s.scope_read}),
        )
        projects = await self._projects(p)
        tasks = await self._tasks(p)
        return HTMLResponse(self._render(projects, tasks), headers=HEADERS)

    def _visible(self, slug: str) -> bool:
        return not any(slug.startswith(prefix) for prefix in self.hidden)

    async def _projects(self, p: Principal) -> list[Project]:
        out: list[Project] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page = await self.service.project_list(p, None, 50, cursor)
            out += [x for x in page.items if self._visible(x.slug)]
            cursor = page.next_cursor
            if not cursor:
                break
        return out

    async def _tasks(self, p: Principal) -> list[Task]:
        out: list[Task] = []
        for status in (*_ACTIVE, "done"):
            cursor: str | None = None
            for _ in range(1 if status == "done" else _MAX_PAGES):
                page = await self.service.task_list(p, None, status, False, None, 50, cursor)
                out += [t for t in page.items if self._visible(t.project)]
                cursor = page.next_cursor
                if not cursor:
                    break
        return out

    def _render(self, projects: list[Project], tasks: list[Task]) -> str:
        by_project: dict[str, Counter[str]] = {}
        for t in tasks:
            by_project.setdefault(t.project, Counter())[t.status] += 1
        live = [x for x in projects if x.status != "archived"]
        archived = [x for x in projects if x.status == "archived"]
        names = {x.slug: x.name for x in projects}
        issues = [t for t in tasks if t.status in _ACTIVE]
        issues.sort(
            key=lambda t: (
                _ACTIVE.index(t.status),
                {"high": 0, "normal": 1, "low": 2}.get(t.priority, 1),
                t.updated_at,
            )
        )
        done = sorted((t for t in tasks if t.status == "done"), key=lambda t: t.updated_at)[-10:]

        def project_row(x: Project) -> str:
            c = by_project.get(x.slug, Counter())
            return (
                f"<tr><td><b>{_esc(x.name)}</b><br><code>{_esc(x.slug)}</code></td>"
                f"<td>{_badge(x.status)}</td><td>{_esc(x.purpose)}</td>"
                f"<td class=n>{c['open']}</td><td class=n>{c['in_progress']}</td>"
                f"<td class=n>{c['blocked']}</td><td class=n>{c['done']}</td></tr>"
            )

        def task_row(t: Task) -> str:
            extra = f"<br><small>blocker: {_esc(t.blocker)}</small>" if t.blocker else ""
            return (
                f"<tr><td>{_badge(t.status)}</td><td>{_badge(t.priority)}</td>"
                f"<td>{_esc(t.title)}{extra}</td><td>{_esc(names.get(t.project, t.project))}</td>"
                f"<td>{_esc(t.claimed_by or '')}</td><td>{_esc(t.updated_at[:16])}</td></tr>"
            )

        counts = Counter(t.status for t in tasks)
        head = "<tr><th>Project<th>Status<th>Purpose<th>Open<th>In progress<th>Blocked<th>Done"
        thead = "<tr><th>Status<th>Priority<th>Task<th>Project<th>Claimed by<th>Updated (UTC)"
        body = (
            "<h1>AI Collab Hub</h1>"
            f"<p class=sub>{len(live)} projects · {counts['open']} open · "
            f"{counts['in_progress']} in progress · {counts['blocked']} blocked · "
            f"{counts['done']} done (recent). Read-only; titles only.</p>"
            f"<h2>Issues in progress ({len(issues)})</h2>"
            f"<table><thead>{thead}</thead><tbody>"
            + ("".join(task_row(t) for t in issues) or "<tr><td colspan=6>Nothing open.")
            + "</tbody></table>"
            f"<h2>Projects ({len(live)})</h2><table><thead>{head}</thead><tbody>"
            + "".join(project_row(x) for x in live)
            + "</tbody></table>"
            + (
                f"<details><summary>Archived ({len(archived)})</summary><table><thead>{head}"
                f"</thead><tbody>{''.join(project_row(x) for x in archived)}</tbody></table>"
                "</details>"
                if archived
                else ""
            )
            + (
                "<h2>Recently completed</h2><table><thead>"
                + thead
                + "</thead><tbody>"
                + "".join(task_row(t) for t in reversed(done))
                + "</tbody></table>"
                if done
                else ""
            )
            + f'<form method=post action="{LOGOUT_PATH}"><button>Sign out</button></form>'
        )
        return _doc("AI Collab Hub", body, refresh=60)


def _badge(value: str) -> str:
    return f'<span class="b {_esc(value)}">{_esc(value.replace("_", " "))}</span>'


_CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1b1f24;--mut:#6b7280;--line:#e5e7eb;--card:#f8fafc}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e6e8eb;--mut:#9aa3af;--line:#262b33;--card:#161a20}}
body{margin:0 auto;max-width:1100px;padding:16px;background:var(--bg);color:var(--fg);
font:15px/1.45 system-ui,sans-serif}
h1{margin:.2em 0}h2{margin:1.6em 0 .4em;font-size:1.1em}.sub,small{color:var(--mut)}
table{width:100%;border-collapse:collapse;display:block;overflow-x:auto}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:.85em}td.n{text-align:right}
.b{padding:1px 8px;border-radius:10px;font-size:.8em;background:var(--card);
border:1px solid var(--line)}
.blocked,.high{border-color:#dc2626;color:#dc2626}
.in_progress,.active{border-color:#2563eb;color:#2563eb}
.done{border-color:#16a34a;color:#16a34a}code{color:var(--mut)}
button{padding:8px 16px;margin-top:1.5em}
details{margin-top:1.2em}
"""


def _doc(title: str, body: str, *, refresh: int | None = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<meta name=robots content=noindex>{meta}<title>{_esc(title)}</title>"
        f"<style>{_CSS}</style><body>{body}</body></html>"
    )


def _page(
    title: str, text: str, status: int = 200, action: tuple[str, str, str] | None = None
) -> HTMLResponse:
    form = ""
    if action:
        path, method, label = action
        form = (
            f'<form method={method.lower()} action="{path}"><button>{_esc(label)}</button></form>'
        )
    return HTMLResponse(
        _doc(title, f"<h1>{_esc(title)}</h1><p>{_esc(text)}</p>{form}"),
        status_code=status,
        headers=HEADERS,
    )
