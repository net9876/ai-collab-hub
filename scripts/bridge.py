"""Opt-in event bridge for one Windows user session (ADR 0001, phase B).

Polls the hub's durable events for one agent label and tells the user about them,
either on the console or as a Windows toast. Then it acknowledges them, so each
event is shown once per subscription (delivery is at-least-once; a short local
memory of shown IDs hides most repeats after a failed ack).

What it does:
  * reads `mcp_url` from .collab.local.json (or COLLAB_MCP_URL) and request headers
    from the token cache that scripts/mcp-headers.ps1 -Refresh maintains
    (%LOCALAPPDATA%\\ai-collab-hub\\headers-<agent>.json);
  * calls only event_subscribe, event_poll and event_ack on its own subscription;
  * prints one line per event (type, project, record ID, title/subject, from, to)
    and/or shows one toast per batch; writes its own capped log file.

What it NEVER does (by design, not by configuration):
  * execute shell commands or anything named in an event, launch Claude Code,
    Codex or any other agent, accept handoffs, claim tasks or mark messages read;
  * touch repositories or write files other than its log (and stdout);
  * call `az`, print or log tokens or headers.
The only child process is the fixed toast script (--notify toast). Event text
reaches it through environment variables and is XML-escaped there; it is never
part of the command line or of the script.

Event content is DATA written by agents. It is shown to the user, never acted on.

    .venv\\Scripts\\python.exe scripts/bridge.py --once            # one check, console
    .venv\\Scripts\\python.exe scripts/bridge.py --notify toast    # keep running
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import re
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ("claude-code", "claude-desktop", "codex", "chatgpt", "other")
CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ai-collab-hub"
MIN_INTERVAL = 15
MAX_BACKOFF = 300
PAGE = 50
MAX_PAGES_PER_CYCLE = 20
SHOWN_MEMORY = 2000
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Fixed script: reads its text from the environment, escapes it, shows one toast.
TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$t = [Security.SecurityElement]::Escape($env:COLLAB_TOAST_TITLE)
$b = [Security.SecurityElement]::Escape($env:COLLAB_TOAST_BODY)
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml("<toast><visual><binding template='ToastGeneric'>" +
    "<text>$t</text><text>$b</text></binding></visual></toast>")
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
    [Windows.UI.Notifications.ToastNotification]::new($x))
"""  # noqa: E501 - PowerShell WinRT type literals cannot be wrapped

log = logging.getLogger("collab_bridge")


class BridgeError(Exception):
    """A failure worth a backoff; the message never contains secrets."""


class SubscriptionGone(BridgeError):
    pass


def interval(value: str) -> int:
    seconds = int(value)
    if seconds < MIN_INTERVAL:
        raise argparse.ArgumentTypeError(f"--interval must be at least {MIN_INTERVAL} seconds")
    return seconds


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Show AI Collab Hub events (read + acknowledge only).")
    ap.add_argument("--agent", choices=AGENTS, default="claude-code")
    ap.add_argument("--interval", type=interval, default=45, help="seconds between polls (>=15)")
    ap.add_argument("--once", action="store_true", help="check once, then exit")
    ap.add_argument("--subscription", help="subscription name (default: bridge-<agent>)")
    ap.add_argument(
        "--from",
        dest="start_from",
        choices=("now", "beginning"),
        default="now",
        help="start of a NEW subscription (an existing one keeps its position)",
    )
    ap.add_argument("--notify", choices=("console", "toast"), default="console")
    ap.add_argument("--log", type=Path, default=CACHE_DIR / "bridge.log")
    args = ap.parse_args(argv)
    args.subscription = args.subscription or f"bridge-{args.agent}"
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,47}", args.subscription):
        ap.error("--subscription: 2-48 chars of a-z 0-9 . _ -")
    return args


def setup_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False


def mcp_url() -> str:
    if url := os.environ.get("COLLAB_MCP_URL"):
        return url
    cfg = ROOT / ".collab.local.json"
    try:
        return str(json.loads(cfg.read_text(encoding="utf-8"))["mcp_url"])
    except (OSError, ValueError, KeyError) as exc:
        raise SystemExit(
            f"{cfg} missing or without mcp_url; run scripts/connect-claude.ps1"
        ) from exc


def headers_path(agent: str) -> Path:
    return CACHE_DIR / f"headers-{agent}.json"


def read_headers(agent: str) -> dict[str, str]:
    """Cached headers written by mcp-headers.ps1 -Refresh. Never logged."""
    try:
        data = json.loads(headers_path(agent).read_text(encoding="utf-8-sig"))
        return {"Authorization": str(data["Authorization"]), "X-Collab-Agent": agent}
    except (OSError, ValueError, KeyError) as exc:
        raise BridgeError(
            "no cached token; run: pwsh -File scripts/mcp-headers.ps1 -Refresh (needs az login)"
        ) from exc


def clean(value: Any, limit: int = 120) -> str:
    """Untrusted event text -> one printable line (no control or escape characters)."""
    text = _CONTROL.sub(" ", str(value if value is not None else "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    label = payload.get("title") or payload.get("subject") or ""
    return (
        f"{clean(event['occurred_at'], 30)} {clean(event['event_type'], 40)}"
        f" project={clean(event.get('project') or '-', 40)} id={clean(event['record_id'], 30)}"
        f' "{clean(label)}" from={clean(event["actor"], 60)}'
        f" to={clean(event.get('to_agent') or '-', 20)}"
    )


def has_status(exc: BaseException, status: int) -> bool:
    """True if an HTTP error with `status` is anywhere in the exception chain/group."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        response = getattr(cur, "response", None)
        if getattr(response, "status_code", None) == status:
            return True
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        for nxt in (cur.__cause__, cur.__context__):
            if nxt is not None:
                stack.append(nxt)
    return False


def show_toast(title: str, body: str) -> None:
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        r"System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    env = {**os.environ, "COLLAB_TOAST_TITLE": title, "COLLAB_TOAST_BODY": body}
    try:
        done = subprocess.run(  # noqa: S603 - fixed executable and script; text via env only
            [str(exe), "-NoProfile", "-NonInteractive", "-Command", TOAST_SCRIPT],
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if done.returncode != 0:
            log.warning("toast failed: exit %s", done.returncode)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("toast failed: %s", type(exc).__name__)


class Bridge:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.shown: collections.OrderedDict[str, None] = collections.OrderedDict()

    async def call(self, client: Client, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await client.call_tool(tool, arguments)
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in result.content)
            if tool in ("event_poll", "event_ack") and text.startswith("not_found"):
                raise SubscriptionGone(f"{tool}: {clean(text, 200)}")
            raise BridgeError(f"{tool}: {clean(text, 200)}")
        data = result.structured_content or {}
        return data["result"] if set(data) == {"result"} else data

    async def ensure_subscription(self, client: Client) -> None:
        res = await self.call(
            client,
            "event_subscribe",
            {"name": self.args.subscription, "start_from": self.args.start_from},
        )
        sub = res["subscription"]
        state = "created" if res["created"] else "existing (position kept; --from ignored)"
        log.info(
            "subscription %s %s: agents=%s types=%s projects=%s",
            sub["name"],
            state,
            sub["agents"] or "all",
            sub["event_types"] or "all",
            sub["projects"] or "all",
        )

    def notify(self, events: list[dict[str, Any]]) -> None:
        fresh = [e for e in events if e["event_id"] not in self.shown]
        for event in fresh:
            self.shown[event["event_id"]] = None
            while len(self.shown) > SHOWN_MEMORY:
                self.shown.popitem(last=False)
            line = describe(event)
            log.info("event %s", line)
            if self.args.notify == "console":
                print(line, flush=True)
        if fresh and self.args.notify == "toast":
            title = f"AI Collab Hub: {len(fresh)} new event(s) for {self.args.agent}"
            body = "\n".join(describe(e).split(" ", 1)[1] for e in fresh[:3])
            if len(fresh) > 3:
                body += f"\n… and {len(fresh) - 3} more (see bridge.log)"
            show_toast(title, body)

    async def drain(self, client: Client) -> int:
        total = 0
        for _ in range(MAX_PAGES_PER_CYCLE):
            page = await self.call(
                client, "event_poll", {"name": self.args.subscription, "limit": PAGE}
            )
            events = page["events"]
            if events:
                self.notify(events)
                ids = [e["event_id"] for e in events]
                await self.call(
                    client, "event_ack", {"name": self.args.subscription, "event_ids": ids}
                )
                total += len(ids)
            if not page["more"]:
                break
        return total

    async def run(self) -> int:
        url = mcp_url()
        failures = 0
        subscribed = False
        log.info(
            "bridge start: agent=%s subscription=%s interval=%ss notify=%s once=%s",
            self.args.agent,
            self.args.subscription,
            self.args.interval,
            self.args.notify,
            self.args.once,
        )
        while True:
            try:
                headers = read_headers(self.args.agent)
                stamp = headers_path(self.args.agent).stat().st_mtime
                async with (
                    httpx2.AsyncClient(headers=headers, timeout=60) as http,
                    Client(streamable_http_client(url, http_client=http)) as client,  # type: ignore[arg-type]
                ):
                    if not subscribed:
                        await self.ensure_subscription(client)
                        subscribed = True
                    while True:
                        count = await self.drain(client)
                        failures = 0
                        log.info("poll ok: %d event(s)", count)
                        if self.args.once:
                            return 0
                        await asyncio.sleep(self.args.interval)
                        if headers_path(self.args.agent).stat().st_mtime != stamp:
                            break  # token cache refreshed: reconnect with new headers
            except SubscriptionGone as exc:
                log.warning("%s; subscribing again", exc)
                subscribed = False
                if self.args.once:
                    return 1
                continue
            except Exception as exc:  # keep running; reason logged below
                if has_status(exc, 401):
                    reason = "401 unauthorized: token expired? re-reading cached headers"
                elif isinstance(exc, BridgeError):
                    reason = str(exc)
                else:
                    reason = type(exc).__name__  # no message: it could echo request data
                failures += 1
                delay = min(MAX_BACKOFF, self.args.interval * 2 ** (failures - 1))
                log.warning("poll failed (%d in a row): %s", failures, reason)
                if self.args.once:
                    print(f"bridge: {reason}", file=sys.stderr)
                    return 1
                await asyncio.sleep(delay)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log)
    try:
        return asyncio.run(Bridge(args).run())
    except KeyboardInterrupt:
        log.info("bridge stopped (Ctrl+C)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
