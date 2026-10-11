"""scripts/bridge.py against the real test server: one-shot poll, print, ack, catch-up."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from pathlib import Path

import pytest

from .conftest import payload
from .test_domain import new_project

BRIDGE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "bridge.py"


@pytest.fixture(scope="module")
def bridge():
    spec = importlib.util.spec_from_file_location("collab_bridge_under_test", BRIDGE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bridge_text_is_sanitized(bridge):
    event = {
        "occurred_at": "2026-10-11T00:00:00Z",
        "event_type": "message.sent",
        "project": "p",
        "record_id": "01J",
        "payload": {"subject": "evil\x1b]0;title\x07\nsecond line" + "x" * 300},
        "actor": "owner/codex",
        "to_agent": "",
    }
    line = bridge.describe(event)
    assert "\x1b" not in line and "\n" not in line and "\x07" not in line
    assert "to=-" in line and len(line) < 400


async def test_bridge_once_prints_acks_and_catches_up(
    bridge, open_client, mint, server_url, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(bridge, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("COLLAB_MCP_URL", f"{server_url}/mcp")
    token = mint()
    (tmp_path / "headers-codex.json").write_text(
        json.dumps({"Authorization": f"Bearer {token}", "X-Collab-Agent": "codex"}),
        encoding="utf-8",
    )
    name = f"bridge-{uuid.uuid4().hex[:8]}"
    log = tmp_path / "bridge.log"
    argv = ["--agent", "codex", "--once", "--subscription", name, "--log", str(log)]

    async def run_once() -> tuple[int, str]:
        args = bridge.parse_args(argv)
        bridge.setup_logging(args.log)
        code = await bridge.Bridge(args).run()
        return code, capsys.readouterr().out

    code, out = await run_once()  # creates the subscription (from now)
    assert code == 0 and out == ""

    async with open_client(mint(), agent="claude-code") as cl:
        proj = await new_project(cl)
        args = {"to_agent": "codex", "subject": "Bridge hello", "body": "BODY", "project": proj}
        key = {"idempotency_key": "bridge-" + uuid.uuid4().hex}
        sent = payload(await cl.call_tool("message_send", {**args, **key}))
        payload(await cl.call_tool("message_send", {**args, **key}))  # replay: same event
        code, out = await run_once()
        assert code == 0
        lines = out.strip().splitlines()
        assert len(lines) == 1
        assert "message.sent" in lines[0] and sent["id"] in lines[0]
        assert '"Bridge hello"' in lines[0] and "from=owner/claude-code" in lines[0]
        assert "BODY" not in out

        code, out = await run_once()  # acknowledged: nothing new
        assert code == 0 and out == ""

        # Offline catch-up: two events while the bridge is not running.
        payload(await cl.call_tool("task_create", {"project": proj, "title": "Later one"}))
        payload(
            await cl.call_tool("message_send", {**args, "subject": "Later two", "to_agent": "any"})
        )
        code, out = await run_once()
        assert code == 0
        assert ["task.created", "message.sent"] == [ln.split()[1] for ln in out.splitlines()]

    text = log.read_text(encoding="utf-8")
    assert token not in text and "Bearer" not in text
    assert "poll ok" in text


async def test_bridge_once_reports_missing_token(bridge, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bridge, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("COLLAB_MCP_URL", "http://127.0.0.1:9/mcp")
    args = bridge.parse_args(["--once", "--log", str(tmp_path / "b.log")])
    bridge.setup_logging(args.log)
    assert await bridge.Bridge(args).run() == 1
    assert "no cached token" in capsys.readouterr().err


def test_bridge_interval_floor(bridge):
    with pytest.raises(SystemExit):
        bridge.parse_args(["--interval", "5"])
