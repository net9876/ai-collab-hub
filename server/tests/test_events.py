"""Durable events (ADR 0001) end-to-end through MCP: atomic event rows, subscriptions,
poll/ack, replay window, per-consumer cursors, isolation and ownership."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from azure.core.exceptions import ServiceRequestError
from ulid import ULID

from collab_hub.events import (
    REPLAY_WINDOW_SECONDS,
    start_rk,
    ulid_floor,
    ulid_ms,
    window_floor,
)
from collab_hub.store import Store

from .conftest import OTHER_WS, READER, error_text, payload
from .test_domain import new_project


def sub_name() -> str:
    return f"t-{uuid.uuid4().hex[:12]}"


def past(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


async def subscribe(client, name: str, **kwargs) -> dict:
    return payload(await client.call_tool("event_subscribe", {"name": name, **kwargs}))


async def poll(client, name: str, limit: int = 50) -> dict:
    return payload(await client.call_tool("event_poll", {"name": name, "limit": limit}))


async def ack(client, name: str, ids: list[str]) -> dict:
    return payload(await client.call_tool("event_ack", {"name": name, "event_ids": ids}))


# --------------------------------------------------------------------------- units


def test_ulid_time_helpers_roundtrip():
    eid = str(ULID())
    ms = ulid_ms(eid)
    assert ulid_floor(ms) <= eid < ulid_floor(ms + 1)
    assert ulid_ms(ulid_floor(ms)) == ms
    floor = window_floor("x:" + eid)
    assert floor.startswith("x:") and ulid_ms(floor[2:]) == ms - REPLAY_WINDOW_SECONDS * 1000
    assert start_rk("beginning") == "x:"
    assert start_rk("2026-01-01T00:00:00Z") < start_rk("2026-01-01T00:00:01Z") < start_rk("now")


# --------------------------------------------------------------------------- writes


async def test_mutations_write_one_event_each_in_order(open_client, mint):
    async with (
        open_client(mint(), agent="claude-code") as cl,
        open_client(mint(), agent="codex") as cx,
    ):
        proj = await new_project(cl)
        name = sub_name()
        await subscribe(cl, name, agents=[], projects=[proj], start_from="beginning")

        t1 = payload(
            await cl.call_tool(
                "task_create",
                {"project": proj, "title": "Ship it", "description": "DESCRIPTION-SECRET"},
            )
        )["id"]
        c1 = payload(await cx.call_tool("task_claim", {"task_id": t1}))
        b1 = payload(
            await cx.call_tool(
                "task_block",
                {"task_id": t1, "expected_revision": c1["revision"], "blocker": "BLOCKER-TEXT"},
            )
        )
        u1 = payload(
            await cx.call_tool(
                "task_update",
                {"task_id": t1, "expected_revision": b1["revision"], "status": "open"},
            )
        )
        payload(
            await cx.call_tool(
                "task_complete",
                {
                    "task_id": t1,
                    "expected_revision": u1["revision"],
                    "result": "RESULT-TEXT",
                    "evidence": ["pytest: ok"],
                },
            )
        )
        t2 = payload(await cl.call_tool("task_create", {"project": proj, "title": "Hand"}))["id"]
        c2 = payload(await cx.call_tool("task_claim", {"task_id": t2}))
        h2 = payload(
            await cx.call_tool(
                "task_handoff",
                {
                    "task_id": t2,
                    "expected_revision": c2["revision"],
                    "to_agent": "claude-code",
                    "note": "NOTE-TEXT",
                },
            )
        )
        payload(
            await cl.call_tool("task_accept", {"task_id": t2, "expected_revision": h2["revision"]})
        )
        t3 = payload(await cl.call_tool("task_create", {"project": proj, "title": "Release"}))["id"]
        c3 = payload(await cl.call_tool("task_claim", {"task_id": t3}))
        payload(
            await cl.call_tool(
                "task_update", {"task_id": t3, "expected_revision": c3["revision"], "release": True}
            )
        )
        msg = payload(
            await cl.call_tool(
                "message_send",
                {"to_agent": "codex", "subject": "Hello", "body": "BODY-SECRET", "project": proj},
            )
        )
        rep = payload(
            await cx.call_tool("message_reply", {"message_id": msg["id"], "body": "REPLY-SECRET"})
        )
        dec = payload(
            await cl.call_tool(
                "decision_add",
                {"project": proj, "title": "Use events", "context": "ctx", "decision": "yes"},
            )
        )

        page = await poll(cl, name)
        events = page["events"]
        assert [e["event_type"] for e in events] == [
            "task.created",
            "task.claimed",
            "task.blocked",
            "task.completed",
            "task.created",
            "task.claimed",
            "task.handoff_requested",
            "task.accepted",
            "task.created",
            "task.claimed",
            "task.released",
            "message.sent",
            "message.replied",
            "decision.created",
        ]
        ids = [e["event_id"] for e in events]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        assert page["more"] is False
        by_type = {e["event_type"]: e for e in events}
        assert [e["record_revision"] for e in events if e["record_id"] == t1] == [1, 2, 3, 5]
        assert by_type["task.completed"]["actor"] == "owner/codex"
        assert by_type["task.handoff_requested"]["to_agent"] == "claude-code"
        assert by_type["task.accepted"]["to_agent"] == "codex"
        assert by_type["task.accepted"]["payload"]["previous_owner"] == "owner/codex"
        assert by_type["message.sent"]["to_agent"] == "codex"
        assert by_type["message.sent"]["record_id"] == msg["id"]
        assert by_type["message.replied"]["to_agent"] == "claude-code"
        assert by_type["message.replied"]["record_id"] == rep["id"]
        assert by_type["message.replied"]["payload"]["thread_id"] == msg["id"]
        assert by_type["decision.created"]["record_id"] == dec["id"]
        assert all(e["project"] == proj for e in events)
        assert all("not an instruction" in e["notice"] for e in events)
        dumped = json.dumps(events)
        for secret in ("DESCRIPTION", "BLOCKER", "RESULT", "NOTE-TEXT", "BODY", "REPLY-SECRET"):
            assert secret not in dumped


async def test_failed_or_lost_transactions_leave_no_event(open_client, mint, hub_app):
    store = hub_app.state.service.store
    original = store.table.submit_transaction

    async def fail(*args, **kwargs):
        raise ServiceRequestError("boom")

    async with (
        open_client(mint(), agent="claude-code") as a,
        open_client(mint(), agent="codex") as b,
    ):
        proj = await new_project(a)
        name = sub_name()
        await subscribe(a, name, agents=[], projects=[proj], start_from="beginning")
        store.table.submit_transaction = fail
        try:
            r = await a.call_tool("task_create", {"project": proj, "title": "Never"})
            assert "unavailable" in error_text(r)
            r = await a.call_tool(
                "message_send",
                {"to_agent": "any", "subject": "Never", "body": "x", "project": proj},
            )
            assert "unavailable" in error_text(r)
        finally:
            store.table.submit_transaction = original
        assert payload(await a.call_tool("task_list", {"project": proj}))["items"] == []
        assert (await poll(a, name))["events"] == []

        # A lost race rolls back the losers' events with their records.
        tid = payload(await a.call_tool("task_create", {"project": proj, "title": "Race"}))["id"]
        results = await asyncio.gather(
            *(c.call_tool("task_claim", {"task_id": tid}) for c in (a, b, a, b))
        )
        assert sum(not r.is_error for r in results) == 1
        types = [e["event_type"] for e in (await poll(a, name))["events"]]
        assert types == ["task.created", "task.claimed"]


async def test_idempotent_replay_creates_exactly_one_event(open_client, mint):
    async with open_client(mint()) as c:
        proj = await new_project(c)
        name = sub_name()
        await subscribe(c, name, agents=[], projects=[proj], start_from="beginning")
        margs = {
            "to_agent": "codex",
            "subject": "Once",
            "body": "b",
            "project": proj,
            "idempotency_key": "evt-" + uuid.uuid4().hex,
        }
        m1 = payload(await c.call_tool("message_send", margs))
        m2 = payload(await c.call_tool("message_send", margs))
        assert m1["id"] == m2["id"] and m2["idempotent_replay"]
        targs = {"project": proj, "title": "Once", "idempotency_key": "evt-" + uuid.uuid4().hex}
        t1 = payload(await c.call_tool("task_create", targs))
        t2 = payload(await c.call_tool("task_create", targs))
        assert t1["id"] == t2["id"] and t2["idempotent_replay"]
        events = (await poll(c, name))["events"]
        assert [(e["event_type"], e["record_id"]) for e in events] == [
            ("message.sent", m1["id"]),
            ("task.created", t1["id"]),
        ]


# --------------------------------------------------------------------------- delivery


async def test_poll_is_repeatable_until_ack_and_ack_is_idempotent(open_client, mint):
    async with open_client(mint(), agent="claude-code") as c:
        proj = await new_project(c)
        name = sub_name()
        created = await subscribe(c, name, projects=[proj])  # default: from now
        assert created["created"] is True
        assert created["subscription"]["agents"] == ["any", "claude-code"]
        assert created["subscription"]["owner_actor"] == "owner/claude-code"
        tids = [
            payload(await c.call_tool("task_create", {"project": proj, "title": f"T {i}"}))["id"]
            for i in range(3)
        ]
        first = await poll(c, name, limit=2)
        assert [e["record_id"] for e in first["events"]] == tids[:2] and first["more"] is True
        again = await poll(c, name, limit=2)
        assert [e["event_id"] for e in again["events"]] == [e["event_id"] for e in first["events"]]

        ids = [e["event_id"] for e in first["events"]]
        res = await ack(c, name, ids)
        assert res["acked"] == ids and res["already_acked"] == [] and res["not_found"] == []
        assert res["cursors"] == {"task": ids[-1]}
        res = await ack(c, name, ids)
        assert res["acked"] == [] and res["already_acked"] == ids
        missing = str(ULID())
        assert (await ack(c, name, [missing]))["not_found"] == [missing]

        rest = await poll(c, name)
        assert [e["record_id"] for e in rest["events"]] == [tids[2]] and rest["more"] is False
        await ack(c, name, [rest["events"][0]["event_id"]])
        assert (await poll(c, name))["events"] == []

        # Acknowledging a message event does not mark the message read.
        msg = payload(
            await c.call_tool(
                "message_send",
                {"to_agent": "claude-code", "subject": "Ping", "body": "b", "project": proj},
            )
        )
        ev = (await poll(c, name))["events"]
        assert [e["record_id"] for e in ev] == [msg["id"]]
        await ack(c, name, [ev[0]["event_id"]])
        inbox = payload(await c.call_tool("message_inbox", {"project": proj}))
        assert [m["id"] for m in inbox["items"] if not m["read"]] == [msg["id"]]

        listed = payload(await c.call_tool("event_subscriptions", {}))
        mine = next(s for s in listed["items"] if s["name"] == name)
        assert mine["cursors"]["message"] == ev[0]["event_id"]


async def test_two_consumers_track_independently(open_client, mint):
    async with (
        open_client(mint(), agent="claude-code") as cl,
        open_client(mint(), agent="codex") as cx,
    ):
        proj = await new_project(cl)
        n_cl, n_cx = sub_name(), sub_name()
        await subscribe(cl, n_cl, projects=[proj])
        await subscribe(cx, n_cx, projects=[proj])
        to_any = payload(
            await cl.call_tool(
                "message_send", {"to_agent": "any", "subject": "All", "body": "b", "project": proj}
            )
        )
        to_codex = payload(
            await cl.call_tool(
                "message_send",
                {"to_agent": "codex", "subject": "Codex", "body": "b", "project": proj},
            )
        )
        got_cx = (await poll(cx, n_cx))["events"]
        assert [e["record_id"] for e in got_cx] == [to_any["id"], to_codex["id"]]
        await ack(cx, n_cx, [e["event_id"] for e in got_cx])
        assert (await poll(cx, n_cx))["events"] == []
        # The other consumer still gets the 'any' message; not the one for codex.
        got_cl = (await poll(cl, n_cl))["events"]
        assert [e["record_id"] for e in got_cl] == [to_any["id"]]


async def test_replay_window_delivers_late_commits_once(open_client, mint, settings):
    async with open_client(mint()) as c:
        proj = await new_project(c)
        name = sub_name()
        await subscribe(c, name, agents=[], projects=[proj], start_from=past(10))
        payload(await c.call_tool("task_create", {"project": proj, "title": "First"}))
        first = (await poll(c, name))["events"]
        assert len(first) == 1
        await ack(c, name, [first[0]["event_id"]])
        cursor_ms = ulid_ms(first[0]["event_id"])

        def late_row(ms: int, title: str) -> dict:
            eid = ulid_floor(ms)[:10] + str(ULID())[10:]
            return {
                "PartitionKey": "main~task",
                "RowKey": f"x:{eid}",
                "row_type": "event",
                "schema_version": 1,
                "event_id": eid,
                "event_type": "task.created",
                "workspace": "main",
                "kind": "task",
                "record_id": str(ULID()),
                "record_revision": 1,
                "occurred_at": "2026-01-01T00:00:00.000000Z",
                "actor": "owner/codex",
                "to_agent": "",
                "project": proj,
                "payload": json.dumps({"title": title}),
            }

        # Committed after the ack, but minted before the cursor (inside the window).
        late = late_row(cursor_ms - 30_000, "late")
        # Older than the replay window: documented limitation, not redelivered.
        stale = late_row(cursor_ms - (REPLAY_WINDOW_SECONDS + 60) * 1000, "stale")
        direct = Store(settings)  # the app's clients live on the server thread's loop
        try:
            await direct.create(late)
            await direct.create(stale)
        finally:
            await direct.close()

        got = (await poll(c, name))["events"]
        assert [e["event_id"] for e in got] == [late["event_id"]]
        res = await ack(c, name, [late["event_id"]])
        assert res["acked"] == [late["event_id"]]
        assert res["cursors"]["task"] == first[0]["event_id"]  # cursor never moves back
        assert (await poll(c, name))["events"] == []


# --------------------------------------------------------------------------- security


async def test_workspace_isolation(open_client, mint):
    async with open_client(mint()) as owner, open_client(mint(oid=OTHER_WS)) as guest:
        proj = await new_project(owner)
        owner_sub = sub_name()
        await subscribe(owner, owner_sub, agents=[], projects=[proj], start_from="beginning")
        tid = payload(await owner.call_tool("task_create", {"project": proj, "title": "Mine"}))
        guest_sub = sub_name()
        await subscribe(guest, guest_sub, agents=[], start_from="beginning")
        seen = (await poll(guest, guest_sub))["events"]
        assert all(e["record_id"] != tid["id"] and e["project"] != proj for e in seen)
        r = await guest.call_tool("event_poll", {"name": owner_sub})
        assert "not_found" in error_text(r)
        r = await guest.call_tool("event_subscribe", {"name": sub_name(), "projects": [proj]})
        assert "validation" in error_text(r)  # the project does not exist in their workspace


async def test_label_is_not_a_boundary_but_subscriptions_are_owned(open_client, mint):
    async with (
        open_client(mint(), agent="claude-code") as cl,
        open_client(mint(), agent="codex") as cx,
    ):
        proj = await new_project(cl)
        # Known limitation: the agent label is self-declared, so a claude-code client may
        # subscribe to codex-addressed events (same verified user, same workspace).
        spy = sub_name()
        await subscribe(cl, spy, agents=["codex"], projects=[proj])
        msg = payload(
            await cx.call_tool(
                "message_send",
                {"to_agent": "codex", "subject": "Self", "body": "b", "project": proj},
            )
        )
        assert [e["record_id"] for e in (await poll(cl, spy))["events"]] == [msg["id"]]

        # But another agent's subscription cannot be polled, acknowledged or removed.
        theirs = sub_name()
        await subscribe(cx, theirs, projects=[proj])
        eid = str(ULID())
        for tool, args in (
            ("event_poll", {"name": theirs}),
            ("event_ack", {"name": theirs, "event_ids": [eid]}),
            ("event_unsubscribe", {"name": theirs}),
        ):
            assert "forbidden" in error_text(await cl.call_tool(tool, args)), tool
        names = {s["name"] for s in payload(await cl.call_tool("event_subscriptions", {}))["items"]}
        assert spy in names and theirs not in names


async def test_reader_cannot_subscribe(open_client, mint):
    async with open_client(mint(oid=READER, scp="Collab.Read")) as r:
        res = await r.call_tool("event_subscribe", {"name": sub_name()})
        assert "forbidden" in error_text(res)
        assert payload(await r.call_tool("event_subscriptions", {}))["items"] == []


async def test_subscribe_update_unsubscribe_resubscribe(open_client, mint):
    async with open_client(mint()) as c:
        proj = await new_project(c)
        name = sub_name()
        a = await subscribe(c, name, projects=[proj], start_from="beginning")
        b = await subscribe(c, name, projects=[proj], start_from="now")
        assert a["created"] and not b["created"]
        assert a["subscription"]["id"] == b["subscription"]["id"]
        assert b["subscription"]["start_from"] == "beginning"  # position kept
        assert b["subscription"]["revision"] == 1  # nothing changed, nothing written
        upd = await subscribe(c, name, event_types=["task.created", "message.sent"])
        assert upd["subscription"]["kinds"] == ["task", "message"]
        assert upd["subscription"]["revision"] == 2

        payload(
            await c.call_tool(
                "decision_add",
                {"project": proj, "title": "Not wanted", "context": "c c", "decision": "d d"},
            )
        )
        payload(await c.call_tool("task_create", {"project": proj, "title": "Wanted"}))
        assert [e["event_type"] for e in (await poll(c, name))["events"]] == ["task.created"]

        assert payload(await c.call_tool("event_unsubscribe", {"name": name}))["removed"] is True
        assert payload(await c.call_tool("event_unsubscribe", {"name": name}))["removed"] is False
        assert "not_found" in error_text(await c.call_tool("event_poll", {"name": name}))
        again = await subscribe(c, name, projects=[proj])
        assert again["created"] is True and again["subscription"]["start_from"] == "now"
        assert (await poll(c, name))["events"] == []


@pytest.mark.parametrize("bad", ["Bad Name", "x", "yesterday"])
async def test_subscription_input_validation(open_client, mint, bad):
    async with open_client(mint()) as c:
        if bad == "yesterday":
            res = await c.call_tool("event_subscribe", {"name": sub_name(), "start_from": bad})
        else:
            res = await c.call_tool("event_subscribe", {"name": bad})
        assert res.is_error
