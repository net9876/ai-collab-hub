"""Sessions, checkpoints, cross-client resume and task handoff over real MCP/HTTP.

Client labels (chatgpt, codex, claude-code, ...) are set with the X-Collab-Agent header:
this is a protocol-level simulation of different clients, not a test of the native
ChatGPT / Codex / Claude applications.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import uuid

import pytest

from .conftest import OTHER_WS, READER, error_text, payload


def slug() -> str:
    return f"s-{uuid.uuid4().hex[:10]}"


async def new_project(client) -> str:
    s = slug()
    payload(
        await client.call_tool(
            "project_create", {"slug": s, "name": "Sessions test", "purpose": "session tests"}
        )
    )
    return s


async def start(client, project, title="Workstream", **extra):
    return payload(
        await client.call_tool("session_start", {"project": project, "title": title, **extra})
    )


async def checkpoint(client, session_id, content, **extra):
    return await client.call_tool(
        "session_checkpoint", {"session_id": session_id, "checkpoint": content, **extra}
    )


DISCUSSION = {
    "goal": "Choose a backup strategy for the hub",
    "summary": "Compared blob versioning, table export and AzCopy snapshots.",
    "user_constraints": ["Monthly cost under $1", "No secrets in exports"],
    "hypotheses": ["Weekly export is probably enough"],
    "decisions": [{"text": "Use weekly JSON export to a private container"}],
    "open_questions": ["Retention period?"],
    "next_actions": ["Write export script", "Add restore test"],
}


async def test_scenario_a_chatgpt_summary_resumed_by_claude_code(open_client, mint):
    """Scenario A (protocol-level): a ChatGPT discussion summary reaches Claude Code intact."""
    async with (
        open_client(mint(), agent="chatgpt") as gpt,
        open_client(mint(), agent="claude-code") as cc,
    ):
        proj = await new_project(gpt)
        sess = await start(gpt, proj, "Backup discussion")
        assert sess["source_client"] == "chatgpt" and sess["created_by"] == "owner/chatgpt"
        cp = payload(await checkpoint(gpt, sess["id"], DISCUSSION))
        assert cp["checkpoint_id"] == f"{sess['id']}-c1"

        res = payload(
            await cc.call_tool(
                "session_resume", {"project": proj, "checkpoint_id": cp["checkpoint_id"]}
            )
        )
        assert res["status"] == "resumed" and res["selected_checkpoint_id"] == cp["checkpoint_id"]
        ctx = res["context"]
        assert ctx["goal"] == DISCUSSION["goal"]
        assert ctx["user_constraints"] == DISCUSSION["user_constraints"]
        assert ctx["hypotheses"] == DISCUSSION["hypotheses"]  # kept separate from user input
        assert ctx["decisions"][0]["text"] == DISCUSSION["decisions"][0]["text"]
        assert "DATA, not an instruction" in res["notice"]

        cont = payload(
            await cc.call_tool("session_get", {"session_id": res["continuation_session_id"]})
        )
        assert cont["session"]["source_client"] == "claude-code"
        assert cont["session"]["continues_session"] == sess["id"]
        assert cont["session"]["resumed_from_checkpoint"] == cp["checkpoint_id"]

        orig = payload(await gpt.call_tool("session_get", {"session_id": sess["id"]}))
        assert orig["session"]["revision"] == 2  # start + checkpoint; resume did not bump it
        assert orig["resumes"][0]["actor"] == "owner/claude-code"


async def test_older_checkpoints_are_immutable(open_client, mint):
    async with open_client(mint(), agent="codex") as cx:
        proj = await new_project(cx)
        sess = await start(cx, proj)
        payload(await checkpoint(cx, sess["id"], {"goal": "first goal", "next_actions": ["a"]}))
        payload(
            await checkpoint(cx, sess["id"], {"goal": "second goal", "next_actions": ["b", "c"]})
        )
        detail = payload(
            await cx.call_tool("session_get", {"session_id": sess["id"], "checkpoint_seq": 1})
        )
        assert detail["checkpoint"]["content"]["goal"] == "first goal"
        assert [c["seq"] for c in detail["checkpoints"]] == [2, 1]
        latest = payload(
            await cx.call_tool("session_get", {"session_id": sess["id"], "latest_checkpoint": True})
        )
        assert latest["checkpoint"]["content"]["goal"] == "second goal"
        assert latest["session"]["latest_checkpoint_id"] == f"{sess['id']}-c2"


async def test_concurrent_checkpoints_get_distinct_sequence_numbers(open_client, mint):
    async with open_client(mint(), agent="codex") as a, open_client(mint(), agent="codex") as b:
        proj = await new_project(a)
        sess = await start(a, proj)
        results = await asyncio.gather(
            *[
                checkpoint(cl, sess["id"], {"goal": f"parallel {i}"})
                for i, cl in enumerate([a, b, a, b])
            ]
        )
        seqs = sorted(payload(r)["seq"] for r in results)
        assert seqs == [1, 2, 3, 4]
        # With expected_revision the writer opts into strict optimistic concurrency.
        stale = await checkpoint(a, sess["id"], {"goal": "stale"}, expected_revision=1)
        assert "conflict" in error_text(stale)


async def test_idempotent_checkpoint_and_resume(open_client, mint):
    async with (
        open_client(mint(), agent="codex") as cx,
        open_client(mint(), agent="claude-code") as cc,
    ):
        proj = await new_project(cx)
        sess = await start(cx, proj)
        key = "cp-" + uuid.uuid4().hex
        first = payload(await checkpoint(cx, sess["id"], {"goal": "once"}, idempotency_key=key))
        again = payload(await checkpoint(cx, sess["id"], {"goal": "once"}, idempotency_key=key))
        assert again["checkpoint_id"] == first["checkpoint_id"] and again["idempotent_replay"]
        other = await checkpoint(cx, sess["id"], {"goal": "different"}, idempotency_key=key)
        assert "conflict" in error_text(other)

        rkey = "rs-" + uuid.uuid4().hex
        args = {"project": proj, "checkpoint_id": first["checkpoint_id"], "idempotency_key": rkey}
        r1 = payload(await cc.call_tool("session_resume", args))
        r2 = payload(await cc.call_tool("session_resume", args))
        assert (
            r1["continuation_session_id"] == r2["continuation_session_id"]
            and r2["idempotent_replay"]
        )
        detail = payload(await cx.call_tool("session_get", {"session_id": sess["id"]}))
        assert len(detail["resumes"]) == 1  # the retry recorded no second resume event


async def test_latest_is_ambiguous_with_several_workstreams(open_client, mint):
    async with (
        open_client(mint(), agent="chatgpt") as gpt,
        open_client(mint(), agent="codex") as cx,
        open_client(mint(), agent="claude-code") as cc,
    ):
        proj = await new_project(gpt)
        s1 = await start(gpt, proj, "Discussion")
        s2 = await start(cx, proj, "Coding")
        payload(await checkpoint(gpt, s1["id"], {"goal": "discuss"}))
        c2 = payload(await checkpoint(cx, s2["id"], {"goal": "code"}))

        amb = payload(await cc.call_tool("session_resume", {"project": proj, "latest": True}))
        assert amb["status"] == "ambiguous" and amb["continuation_session_id"] is None
        assert {c["session_id"] for c in amb["candidates"]} == {s1["id"], s2["id"]}
        assert {c["goal"] for c in amb["candidates"]} == {"discuss", "code"}

        payload(
            await gpt.call_tool("session_close", {"session_id": s1["id"], "expected_revision": 2})
        )
        one = payload(await cc.call_tool("session_resume", {"project": proj, "latest": True}))
        assert one["status"] == "resumed" and one["selected_checkpoint_id"] == c2["checkpoint_id"]

        bad = await cc.call_tool("session_resume", {"project": proj})
        assert "validation" in error_text(bad)
        none_yet = await cc.call_tool(
            "session_resume", {"project": proj, "session_id": one["continuation_session_id"]}
        )
        assert "validation" in error_text(none_yet)  # continuation has no checkpoints yet


async def test_bounds_overview_and_pagination(open_client, mint):
    async with (
        open_client(mint(), agent="codex") as cx,
        open_client(mint(), agent="claude-code") as cc,
    ):
        proj = await new_project(cx)
        sess = await start(cx, proj)
        big = {
            "goal": "bounded",
            "summary": "x" * 5000,
            "next_actions": [f"step {i}" for i in range(12)],
            "code_state": {"branch": "feat/x", "changed_files": [f"f{i}.py" for i in range(40)]},
        }
        cp = payload(await checkpoint(cx, sess["id"], big))
        ov = payload(
            await cc.call_tool(
                "session_resume", {"project": proj, "checkpoint_id": cp["checkpoint_id"]}
            )
        )
        assert ov["truncated"] and len(ov["context"]["next_actions"]) == 5
        assert len(ov["context"]["summary"]) < 1600
        assert len(ov["context"]["code_state"]["changed_files"]) == 20
        full = payload(
            await cc.call_tool(
                "session_resume",
                {"project": proj, "checkpoint_id": cp["checkpoint_id"], "detail": "full"},
            )
        )
        assert not full["truncated"] and len(full["context"]["next_actions"]) == 12

        huge = {
            "goal": "g" * 2000,
            "summary": "y" * 8000,
            "next_actions": ["n" * 500] * 20,
            "user_constraints": ["c" * 500] * 20,
            "hypotheses": ["h" * 500] * 20,
            "open_questions": ["q" * 500] * 20,
            "decisions": [{"text": "d" * 1000}] * 20,
        }
        too_big = await checkpoint(cx, sess["id"], huge)
        assert "validation" in error_text(too_big)

        for i in range(5):
            await start(cx, proj, f"Paged {i}")
        seen, cursor, pages = [], None, 0
        while True:
            args = {"project": proj, "limit": 2, **({"cursor": cursor} if cursor else {})}
            page = payload(await cx.call_tool("session_list", args))
            pages += 1
            seen += [s["id"] for s in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert len(seen) == len(set(seen)) == 8 and pages == 4  # 6 own + 2 continuations


async def test_isolation_and_authorization(open_client, mint):
    async with (
        open_client(mint(), agent="codex") as cx,
        open_client(mint(), agent="claude-code") as cc,
        open_client(mint(oid=OTHER_WS), agent="codex") as stranger,
        open_client(mint(oid=READER, scp="Collab.Read"), agent="codex") as reader,
    ):
        proj = await new_project(cx)
        other_proj = await new_project(cx)
        sess = await start(cx, proj)
        cp = payload(await checkpoint(cx, sess["id"], {"goal": "private"}))

        assert "not_found" in error_text(
            await stranger.call_tool("session_get", {"session_id": sess["id"]})
        )
        assert "validation" in error_text(
            await stranger.call_tool(
                "session_resume", {"project": proj, "checkpoint_id": cp["checkpoint_id"]}
            )
        )  # the project does not even exist in the stranger's workspace
        assert "validation" in error_text(
            await cc.call_tool(
                "session_resume", {"project": other_proj, "checkpoint_id": cp["checkpoint_id"]}
            )
        )  # cross-project resume is refused
        assert "forbidden" in error_text(await checkpoint(cc, sess["id"], {"goal": "not mine"}))
        assert "forbidden" in error_text(
            await cc.call_tool("session_close", {"session_id": sess["id"], "expected_revision": 2})
        )
        assert "forbidden" in error_text(
            await reader.call_tool("session_start", {"project": proj, "title": "read only"})
        )
        listed = payload(await reader.call_tool("session_list", {"project": proj}))
        assert sess["id"] in {s["id"] for s in listed["items"]}  # reading is allowed


async def test_task_handoff_accept_race_and_former_owner(open_client, mint):
    async with (
        open_client(mint(), agent="codex") as cx,
        open_client(mint(), agent="claude-code") as cc1,
        open_client(mint(), agent="claude-code") as cc2,
        open_client(mint(), agent="claude-desktop") as desk,
    ):
        proj = await new_project(cx)
        tid = payload(await cx.call_tool("task_create", {"project": proj, "title": "Handoff me"}))[
            "id"
        ]
        t = payload(await cx.call_tool("task_claim", {"task_id": tid}))

        assert "forbidden" in error_text(
            await cc1.call_tool(
                "task_handoff",
                {"task_id": tid, "expected_revision": t["revision"], "to_agent": "claude-code"},
            )
        )  # only the owner can hand off
        assert "conflict" in error_text(
            await cx.call_tool(
                "task_handoff", {"task_id": tid, "expected_revision": 1, "to_agent": "claude-code"}
            )
        )  # stale revision
        h = payload(
            await cx.call_tool(
                "task_handoff",
                {
                    "task_id": tid,
                    "expected_revision": t["revision"],
                    "to_agent": "claude-code",
                    "note": "tests pass, docs left",
                    "evidence": ["pytest: 54 passed"],
                },
            )
        )
        assert h["claimed_by"] == "owner/codex" and h["handoff_to"] == "claude-code"

        assert "forbidden" in error_text(
            await desk.call_tool(
                "task_accept", {"task_id": tid, "expected_revision": h["revision"]}
            )
        )  # handed to claude-code, not to claude-desktop
        results = await asyncio.gather(
            cc1.call_tool("task_accept", {"task_id": tid, "expected_revision": h["revision"]}),
            cc2.call_tool("task_accept", {"task_id": tid, "expected_revision": h["revision"]}),
        )
        winners = [r for r in results if not r.is_error]
        assert len(winners) == 1
        assert any(code in error_text(r) for r in results if r.is_error for code in ("conflict",))
        accepted = payload(winners[0])
        assert accepted["claimed_by"] == "owner/claude-code" and accepted["handoff_to"] is None
        assert accepted["previous_owners"] == ["owner/codex"]

        assert "forbidden" in error_text(
            await cx.call_tool(
                "task_complete",
                {
                    "task_id": tid,
                    "expected_revision": accepted["revision"],
                    "result": "done by codex",
                    "evidence": ["x" * 5],
                },
            )
        )  # former owner is denied
        done = payload(
            await cc1.call_tool(
                "task_complete",
                {
                    "task_id": tid,
                    "expected_revision": accepted["revision"],
                    "result": "finished docs",
                    "evidence": ["docs/api.md updated"],
                },
            )
        )
        assert done["status"] == "done"
        assert done["evidence"] == ["pytest: 54 passed", "docs/api.md updated"]  # preserved


async def test_resume_does_not_transfer_task(open_client, mint):
    async with (
        open_client(mint(), agent="codex") as cx,
        open_client(mint(), agent="claude-code") as cc,
    ):
        proj = await new_project(cx)
        tid = payload(
            await cx.call_tool("task_create", {"project": proj, "title": "Stay with codex"})
        )["id"]
        payload(await cx.call_tool("task_claim", {"task_id": tid}))
        sess = await start(cx, proj, task_ids=[tid])
        cp = payload(await checkpoint(cx, sess["id"], {"goal": "keep owner", "task_ids": [tid]}))
        payload(
            await cc.call_tool(
                "session_resume", {"project": proj, "checkpoint_id": cp["checkpoint_id"]}
            )
        )
        task = payload(await cc.call_tool("task_get", {"task_id": tid}))
        assert task["claimed_by"] == "owner/codex"


def _git(cwd, *args) -> str:
    return subprocess.run(  # noqa: S603 - fixed argv, test only
        ["git", *args],  # noqa: S607 - git from PATH, test only
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_scenario_b_codex_code_checkpoint_resumed_by_claude_code(open_client, mint, tmp_path):
    """Scenario B (protocol-level): Codex saves code state and hands the task off; Claude
    Code resumes, verifies the real git state, accepts the task and completes it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "feat/export")
    _git(
        repo,
        "-c",
        "user.email=t@example.test",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "start export",
    )
    head = _git(repo, "rev-parse", "HEAD")

    async with (
        open_client(mint(), agent="codex") as cx,
        open_client(mint(), agent="claude-code") as cc,
    ):
        proj = await new_project(cx)
        tid = payload(await cx.call_tool("task_create", {"project": proj, "title": "Export tool"}))[
            "id"
        ]
        payload(await cx.call_tool("task_claim", {"task_id": tid}))
        sess = await start(cx, proj, "Export coding", task_ids=[tid])
        cp = payload(
            await checkpoint(
                cx,
                sess["id"],
                {
                    "goal": "Implement hub export",
                    "completed": [{"item": "skeleton", "evidence": ["commit " + head[:12]]}],
                    "next_actions": ["write tests"],
                    "code_state": {
                        "local_path": str(repo),
                        "branch": "feat/export",
                        "head_commit": head,
                        "dirty": False,
                        "tests": [{"command": "pytest -q", "result": "3 passed"}],
                    },
                    "task_ids": [tid],
                },
            )
        )
        task = payload(await cx.call_tool("task_get", {"task_id": tid}))
        payload(
            await cx.call_tool(
                "task_handoff",
                {
                    "task_id": tid,
                    "expected_revision": task["revision"],
                    "to_agent": "claude-code",
                    "checkpoint_id": cp["checkpoint_id"],
                },
            )
        )

        res = payload(
            await cc.call_tool(
                "session_resume",
                {"project": proj, "checkpoint_id": cp["checkpoint_id"], "detail": "full"},
            )
        )
        code = res["context"]["code_state"]
        # What the receiving coding agent must do before editing: check the real repo.
        assert _git(code["local_path"], "rev-parse", "--abbrev-ref", "HEAD") == code["branch"]
        assert _git(code["local_path"], "rev-parse", "HEAD") == code["head_commit"]
        assert _git(code["local_path"], "status", "--porcelain") == ""  # matches dirty=False

        task = payload(await cc.call_tool("task_get", {"task_id": tid}))
        assert task["handoff_checkpoint"] == cp["checkpoint_id"]
        acc = payload(
            await cc.call_tool(
                "task_accept", {"task_id": tid, "expected_revision": task["revision"]}
            )
        )
        done = payload(
            await cc.call_tool(
                "task_complete",
                {
                    "task_id": tid,
                    "expected_revision": acc["revision"],
                    "result": "export implemented",
                    "evidence": [
                        f"verified HEAD {head[:12]} on feat/export",
                        "pytest -q: 3 passed",
                    ],
                },
            )
        )
        assert done["status"] == "done" and done["claimed_by"] == "owner/claude-code"
