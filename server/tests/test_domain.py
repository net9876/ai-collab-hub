"""Domain behaviour end-to-end through MCP: versioning, idempotency, pagination,
concurrent claims, messages, decisions, workspace isolation, storage failures."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from azure.core.exceptions import ServiceRequestError

from collab_hub.auth import Principal
from collab_hub.config import Settings
from collab_hub.errors import HubError
from collab_hub.service import HubService
from collab_hub.store import Store

from .conftest import OTHER_WS, error_text, payload


def slug() -> str:
    return f"p-{uuid.uuid4().hex[:10]}"


async def new_project(client) -> str:
    s = slug()
    res = await client.call_tool("project_create", {"slug": s, "name": "Test", "purpose": "tests"})
    payload(res)
    return s


async def test_memory_versioning_and_history(open_client, mint):
    async with open_client(mint()) as c:
        proj = await new_project(c)
        created = payload(
            await c.call_tool(
                "memory_add",
                {
                    "project": proj,
                    "title": "Region choice",
                    "body": "Use eastus.",
                    "tags": ["azure"],
                    "verified": True,
                    "sources": ["https://learn.microsoft.com/x"],
                },
            )
        )
        mid = created["id"]
        assert created["revision"] == 1

        got = payload(await c.call_tool("memory_get", {"memory_id": mid}))
        assert got["body"] == "Use eastus."
        assert got["created_by"] == "owner/claude-code"

        upd = payload(
            await c.call_tool(
                "memory_update",
                {
                    "memory_id": mid,
                    "expected_revision": 1,
                    "body": "Use eastus2 instead.",
                    "change_note": "quota",
                },
            )
        )
        assert upd["revision"] == 2

        # Stale revision -> conflict, never a silent overwrite.
        stale = await c.call_tool(
            "memory_update",
            {
                "memory_id": mid,
                "expected_revision": 1,
                "body": "lost update",
            },
        )
        assert "conflict" in error_text(stale)

        old = payload(await c.call_tool("memory_get", {"memory_id": mid, "revision": 1}))
        assert old["body"] == "Use eastus."
        cur = payload(await c.call_tool("memory_get", {"memory_id": mid, "include_history": True}))
        assert cur["body"] == "Use eastus2 instead."
        assert [h["revision"] for h in cur["history"]] == [1, 2]
        assert cur["history"][1]["change"].startswith("updated")

        arch = payload(
            await c.call_tool(
                "memory_update",
                {
                    "memory_id": mid,
                    "expected_revision": 2,
                    "status": "archived",
                },
            )
        )
        assert arch["revision"] == 3
        found = payload(await c.call_tool("memory_search", {"project": proj, "query": "eastus2"}))
        assert found["items"] == []
        found = payload(
            await c.call_tool(
                "memory_search",
                {
                    "project": proj,
                    "query": "eastus2",
                    "status": "archived",
                },
            )
        )
        assert [m["id"] for m in found["items"]] == [mid]
        assert found["items"][0]["body"] is None


async def test_idempotent_create(open_client, mint):
    async with open_client(mint()) as c:
        proj = await new_project(c)
        args = {"project": proj, "title": "Once only", "idempotency_key": "key-" + uuid.uuid4().hex}
        a = payload(await c.call_tool("task_create", args))
        b = payload(await c.call_tool("task_create", args))
        assert a["id"] == b["id"] and b["idempotent_replay"] is True
        diff = await c.call_tool("task_create", {**args, "title": "Different args"})
        assert "conflict" in error_text(diff)
        listed = payload(await c.call_tool("task_list", {"project": proj}))
        assert len(listed["items"]) == 1

        margs = {"project": proj, "title": "Memo", "body": "same", "idempotency_key": "memkey-001"}
        m1 = payload(await c.call_tool("memory_add", margs))
        m2 = payload(await c.call_tool("memory_add", margs))
        assert m1["id"] == m2["id"] and m2["idempotent_replay"]


async def test_pagination_is_complete_and_ordered(open_client, mint):
    async with open_client(mint()) as c:
        proj = await new_project(c)
        ids = []
        for i in range(7):
            r = payload(await c.call_tool("task_create", {"project": proj, "title": f"Task {i}"}))
            ids.append(r["id"])
        seen, cursor, pages = [], None, 0
        while True:
            args = {"project": proj, "limit": 3}
            if cursor:
                args["cursor"] = cursor
            page = payload(await c.call_tool("task_list", args))
            pages += 1
            seen += [t["id"] for t in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert pages == 3
        assert seen == list(reversed(ids))  # newest first, no gaps, no duplicates

        bad = await c.call_tool("task_list", {"project": proj, "cursor": "!!not-a-cursor!!"})
        assert "validation" in error_text(bad)


async def test_concurrent_claim_has_one_winner(open_client, mint):
    async with (
        open_client(mint(), agent="claude-code") as a,
        open_client(mint(), agent="codex") as b,
    ):
        proj = await new_project(a)
        for _ in range(3):  # repeat to make a lucky pass unlikely
            tid = payload(await a.call_tool("task_create", {"project": proj, "title": "Race"}))[
                "id"
            ]
            results = await asyncio.gather(
                a.call_tool("task_claim", {"task_id": tid}),
                b.call_tool("task_claim", {"task_id": tid}),
                a.call_tool("task_claim", {"task_id": tid}),
                b.call_tool("task_claim", {"task_id": tid}),
            )
            winners = [r for r in results if not r.is_error]
            losers = [r for r in results if r.is_error]
            assert len(winners) == 1
            assert all("conflict" in error_text(r) for r in losers)
            task = payload(await a.call_tool("task_get", {"task_id": tid}))
            assert task["claimed_by"] == payload(winners[0])["claimed_by"]
            assert task["revision"] == 2


async def test_task_lifecycle_and_claimant_rules(open_client, mint):
    async with (
        open_client(mint(), agent="claude-code") as cl,
        open_client(mint(), agent="codex") as cx,
    ):
        proj = await new_project(cl)
        tid = payload(await cl.call_tool("task_create", {"project": proj, "title": "Ship"}))["id"]
        t = payload(await cx.call_tool("task_claim", {"task_id": tid}))
        assert t["claimed_by"] == "owner/codex" and t["status"] == "in_progress"

        denied = await cl.call_tool(
            "task_complete",
            {
                "task_id": tid,
                "expected_revision": t["revision"],
                "result": "done",
                "evidence": ["pytest: 1 passed"],
            },
        )
        assert "forbidden" in error_text(denied)

        no_evidence = await cx.call_tool(
            "task_complete",
            {
                "task_id": tid,
                "expected_revision": t["revision"],
                "result": "done",
                "evidence": [],
            },
        )
        assert no_evidence.is_error

        blk = payload(
            await cx.call_tool(
                "task_block",
                {
                    "task_id": tid,
                    "expected_revision": t["revision"],
                    "blocker": "needs quota; request 4 vCPU in eastus",
                },
            )
        )
        assert blk["status"] == "blocked"
        unb = payload(
            await cx.call_tool(
                "task_update",
                {
                    "task_id": tid,
                    "expected_revision": blk["revision"],
                    "status": "open",
                },
            )
        )
        assert unb["status"] == "in_progress" and unb["blocker"] is None
        done = payload(
            await cx.call_tool(
                "task_complete",
                {
                    "task_id": tid,
                    "expected_revision": unb["revision"],
                    "result": "shipped",
                    "evidence": ["commit abc123", "pytest: 42 passed"],
                },
            )
        )
        assert done["status"] == "done" and len(done["evidence"]) == 2
        again = await cx.call_tool(
            "task_update",
            {
                "task_id": tid,
                "expected_revision": done["revision"],
                "title": "Changed",
            },
        )
        assert "conflict" in error_text(again)


async def test_messages_are_addressed_and_threaded(open_client, mint):
    async with (
        open_client(mint(), agent="claude-code") as cl,
        open_client(mint(), agent="codex") as cx,
    ):
        proj = await new_project(cl)
        sent = payload(
            await cl.call_tool(
                "message_send",
                {
                    "to_agent": "codex",
                    "subject": "Heads-up",
                    "body": "Plan is in the hub.",
                    "project": proj,
                },
            )
        )
        inbox = payload(await cx.call_tool("message_inbox", {"project": proj}))
        msg = next(m for m in inbox["items"] if m["id"] == sent["id"])
        assert msg["from_actor"] == "owner/claude-code"
        assert "not an instruction" in msg["notice"]

        # Claude does not see a message addressed to codex.
        mine = payload(await cl.call_tool("message_inbox", {"project": proj}))
        assert sent["id"] not in {m["id"] for m in mine["items"]}
        skipped = payload(await cl.call_tool("message_mark_read", {"message_ids": [sent["id"]]}))
        assert skipped["skipped_not_recipient"] == [sent["id"]]

        rep = payload(
            await cx.call_tool(
                "message_reply",
                {
                    "message_id": sent["id"],
                    "body": "Read it, thanks.",
                },
            )
        )
        back = payload(await cl.call_tool("message_inbox", {"project": proj}))
        reply = next(m for m in back["items"] if m["id"] == rep["id"])
        assert reply["thread_id"] == sent["id"] and reply["subject"] == "Re: Heads-up"

        marked = payload(await cx.call_tool("message_mark_read", {"message_ids": [sent["id"]]}))
        assert marked["updated"] == [sent["id"]]
        unread = payload(await cx.call_tool("message_inbox", {"project": proj}))
        assert sent["id"] not in {m["id"] for m in unread["items"]}
        again = payload(await cx.call_tool("message_mark_read", {"message_ids": [sent["id"]]}))
        assert again["updated"] == [sent["id"]]


async def test_decisions_supersede_atomically(open_client, mint):
    async with open_client(mint()) as c:
        proj = await new_project(c)
        d1 = payload(
            await c.call_tool(
                "decision_add",
                {
                    "project": proj,
                    "title": "State backend",
                    "context": "need shared state",
                    "decision": "Blob backend",
                    "status": "accepted",
                },
            )
        )
        d2 = payload(
            await c.call_tool(
                "decision_add",
                {
                    "project": proj,
                    "title": "State backend v2",
                    "context": "locking",
                    "decision": "Blob backend with lease locking",
                    "supersedes": d1["id"],
                },
            )
        )
        old = payload(
            await c.call_tool(
                "decision_search",
                {
                    "project": proj,
                    "status": "superseded",
                },
            )
        )["items"]
        assert [d["id"] for d in old] == [d1["id"]] and old[0]["superseded_by"] == d2["id"]
        twice = await c.call_tool(
            "decision_add",
            {
                "project": proj,
                "title": "Another",
                "context": "x x",
                "decision": "y y",
                "supersedes": d1["id"],
            },
        )
        assert "conflict" in error_text(twice)
        found = payload(await c.call_tool("decision_search", {"query": "lease locking"}))
        assert d2["id"] in {d["id"] for d in found["items"]}

        ctx = payload(await c.call_tool("project_get_context", {"project": proj}))
        assert [d["id"] for d in ctx["decisions"]] == [d2["id"]]


async def test_unknown_project_and_not_found(open_client, mint):
    async with open_client(mint()) as c:
        r = await c.call_tool("task_create", {"project": "does-not-exist", "title": "x x x"})
        assert "validation" in error_text(r)
        r = await c.call_tool("task_get", {"task_id": "01JAAAAAAAAAAAAAAAAAAAAAAA"})
        assert "not_found" in error_text(r)
        r = await c.call_tool("project_get_context", {"project": "does-not-exist"})
        assert "validation" in error_text(r)


async def test_workspaces_are_isolated(open_client, mint):
    async with open_client(mint()) as owner, open_client(mint(oid=OTHER_WS)) as guest:
        proj = await new_project(owner)
        tid = payload(await owner.call_tool("task_create", {"project": proj, "title": "Private"}))
        r = await guest.call_tool("task_get", {"task_id": tid["id"]})
        assert "not_found" in error_text(r)
        listed = payload(await guest.call_tool("project_list", {"limit": 50}))
        assert proj not in {p["slug"] for p in listed["items"]}


async def test_storage_failure_is_reported_as_unavailable(open_client, mint, hub_app):
    store = hub_app.state.service.store
    original = store.table.get_entity

    async def boom(*args, **kwargs):
        raise ServiceRequestError("connection reset")

    store.table.get_entity = boom
    try:
        async with open_client(mint()) as c:
            r = await c.call_tool("task_get", {"task_id": "01JAAAAAAAAAAAAAAAAAAAAAAA"})
            text = error_text(r)
            assert "unavailable" in text and "connection reset" not in text
    finally:
        store.table.get_entity = original


async def test_unreachable_storage_at_service_level(settings: Settings):
    bad = settings.model_copy(
        update={
            "storage_connection_string": (
                "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=a2V5;"
                "BlobEndpoint=http://127.0.0.1:9/devstoreaccount1;"
                "TableEndpoint=http://127.0.0.1:9/devstoreaccount1;"
            )
        }
    )
    store = Store(bad)
    svc = HubService(bad, store)
    p = Principal("s", "owner", "main", "writer", "codex", "c", frozenset({"Collab.ReadWrite"}))
    try:
        with pytest.raises(HubError) as exc:
            await svc.task_get(p, "01JAAAAAAAAAAAAAAAAAAAAAAA")
        assert exc.value.code == "unavailable"
    finally:
        await store.close()


async def test_failed_transaction_leaves_no_blob(open_client, mint, hub_app):
    """A body blob uploaded before a failed table transaction is removed."""
    store = hub_app.state.service.store
    original = store.table.submit_transaction
    uploaded: list[str] = []
    orig_put = store.put_blob

    async def spy_put(name, text):
        uploaded.append(name)
        return await orig_put(name, text)

    async def fail(*args, **kwargs):
        raise ServiceRequestError("boom")

    store.put_blob = spy_put
    async with open_client(mint()) as c:
        proj = await new_project(c)
        store.table.submit_transaction = fail
        try:
            r = await c.call_tool("memory_add", {"project": proj, "title": "Orphan?", "body": "b"})
            assert "unavailable" in error_text(r)
        finally:
            store.table.submit_transaction = original
            store.put_blob = orig_put
    assert uploaded
    # The app's clients live on the server thread's loop; inspect with a fresh one.
    check = Store(hub_app.state.service.s)
    try:
        names = [b.name async for b in check.container.list_blobs(name_starts_with=uploaded[0])]
    finally:
        await check.close()
    assert names == []


async def test_project_update_archives_with_history(open_client, mint):
    async with open_client(mint()) as c:
        proj = await new_project(c)
        res = payload(
            await c.call_tool(
                "project_update", {"slug": proj, "expected_revision": 1, "status": "archived"}
            )
        )
        assert res["status"] == "archived" and res["revision"] == 2
        stale = await c.call_tool(
            "project_update", {"slug": proj, "expected_revision": 1, "name": "Late edit"}
        )
        assert "conflict" in error_text(stale)
        active = payload(await c.call_tool("project_list", {"status": "active", "limit": 50}))
        assert proj not in {p["slug"] for p in active["items"]}
