"""Sessions, immutable checkpoints and cross-client resume (phase 1 of shared context).

A *session* is one conversation/workstream in one client (its source_client label is
self-declared). A *checkpoint* is an immutable, structured snapshot of that work: goal,
user constraints, agent hypotheses, decisions, open questions, completed work with
evidence, next actions, blockers and optional code state. Another client *resumes* an
explicit checkpoint: it gets a bounded portable context package and a new linked
continuation session of its own, so nobody ever overwrites another session's "latest".

Storage (one partition "<workspace>~session", so related writes are atomic):
  r:<inverted ULID>          session record (versioned, ETag)
  v:<session_id>:<rev>       session revision history
  c:<session_id>:<seq>       checkpoint row (immutable); body JSON in Blob, SHA-256 checked
  e:<session_id>:<ULID>      resume event (who continued this session, from which checkpoint)
  i:<hash>                   idempotency rows
"""

from __future__ import annotations

from typing import Any

from ulid import ULID

from .auth import Principal, require
from .errors import HubError, conflict, not_found
from .models import (
    CHECKPOINT_MAX_BYTES,
    Checkpoint,
    CheckpointContent,
    CheckpointResult,
    CheckpointSummary,
    Page,
    ResumeCandidate,
    ResumeEvent,
    ResumeResult,
    Session,
    SessionDetail,
)
from .service import HubService, _jl, _pl, _request_hash, _tokens, checkpoint_rk, normalize, now
from .store import idem_rk, partition

OVERVIEW_LIST = 5
OVERVIEW_SUMMARY = 1500
RECENT_CHECKPOINTS = 20
LATEST_SCAN = 50


def overview(content: CheckpointContent) -> tuple[CheckpointContent, bool]:
    """Bounded copy for the default resume response; flags whether anything was cut."""
    data = content.model_copy(deep=True)
    cut = False

    def trim(items: list[Any], n: int = OVERVIEW_LIST) -> list[Any]:
        nonlocal cut
        if len(items) > n:
            cut = True
            return items[:n]
        return items

    if len(data.summary) > OVERVIEW_SUMMARY:
        data.summary = data.summary[:OVERVIEW_SUMMARY] + " …"
        cut = True
    for field in (
        "user_constraints",
        "hypotheses",
        "decisions",
        "open_questions",
        "next_actions",
        "blockers",
    ):
        setattr(data, field, trim(getattr(data, field)))
    data.completed = trim(data.completed)
    for item in data.completed:
        item.evidence = trim(item.evidence, 2)
    if data.code_state:
        data.code_state.changed_files = trim(data.code_state.changed_files, 20)
        data.code_state.tests = trim(data.code_state.tests)
    return data, cut


def parse_checkpoint_id(checkpoint_id: str) -> tuple[str, int]:
    session_id, _, seq = checkpoint_id.partition("-c")
    return session_id, int(seq)


class SessionService:
    def __init__(self, hub: HubService) -> None:
        self.hub = hub
        self.s = hub.s
        self.store = hub.store

    # ------------------------------------------------------------------ mapping

    @staticmethod
    def _pk(p: Principal) -> str:
        return partition(p.workspace, "session")

    def _session(self, row: dict[str, Any]) -> Session:
        return Session(
            id=row["record_id"],
            project=row["project"],
            title=row["title"],
            status=row["status"],
            source_client=row["source_client"],
            continues_session=row.get("continues_session") or None,
            resumed_from_checkpoint=row.get("resumed_from_checkpoint") or None,
            checkpoint_count=int(row.get("checkpoint_count", 0)),
            latest_checkpoint_id=row.get("latest_checkpoint_id") or None,
            latest_checkpoint_at=row.get("latest_checkpoint_at") or None,
            task_ids=_pl(row.get("task_ids")),
            **self.hub._audit(row),
        )

    @staticmethod
    def _summary(row: dict[str, Any]) -> CheckpointSummary:
        return CheckpointSummary(
            id=row["record_id"],
            session_id=row["session_id"],
            seq=int(row["seq"]),
            project=row["project"],
            created_at=row["created_at"],
            created_by=row["created_by"],
            source_client=row["source_client"],
            goal=row.get("goal", ""),
            next_actions=int(row.get("next_actions", 0)),
            has_code_state=bool(row.get("has_code_state")),
            size_bytes=int(row.get("size_bytes", 0)),
        )

    async def _checkpoint(self, row: dict[str, Any]) -> Checkpoint:
        raw = await self.store.get_blob(row["content_blob"], row["content_sha256"])
        content = CheckpointContent.model_validate_json(raw)
        return Checkpoint(**self._summary(row).model_dump(), content=content)

    async def _load_checkpoint_row(self, p: Principal, session_id: str, seq: int) -> dict[str, Any]:
        row = await self.store.get(self._pk(p), checkpoint_rk(session_id, seq))
        if row is None:
            raise not_found("checkpoint", f"{session_id}-c{seq}")
        return row

    # ------------------------------------------------------------------ tools

    async def session_start(
        self,
        p: Principal,
        project: str,
        title: str,
        task_ids: list[str],
        idempotency_key: str | None,
    ) -> Session:
        require(self.s, p, write=True)
        await self.hub._require_project(p, project)
        for tid in task_ids:
            task = await self.hub._load(p, "task", tid)
            if task["project"] != project:
                raise HubError("validation", f"task '{tid}' belongs to another project")
        sid = str(ULID())
        row = {
            "record_id": sid,
            "project": project,
            "title": title,
            "status": "active",
            "source_client": p.agent,
            "continues_session": "",
            "resumed_from_checkpoint": "",
            "checkpoint_count": 0,
            "latest_checkpoint_id": "",
            "latest_checkpoint_at": "",
            "task_ids": _jl(task_ids),
            "search_text": normalize(f"{title} {p.agent}"),
            **self.hub._audit_new(p),
        }
        request = {"project": project, "title": title, "task_ids": task_ids, "agent": p.agent}
        res = await self.hub._create(
            p, "session", sid, row, "session_start", idempotency_key, request
        )
        return self._session(await self.hub._load(p, "session", res.id))

    async def session_list(
        self,
        p: Principal,
        project: str | None,
        status: str | None,
        source_client: str | None,
        query: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[Session]:
        require(self.s, p, write=False)
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if project:
            clauses.append("project eq @project")
            params["project"] = project
        if status:
            clauses.append("status eq @status")
            params["status"] = status
        if source_client:
            clauses.append("source_client eq @client")
            params["client"] = source_client
        words = _tokens(query)
        rows, nxt, scanned = await self.store.scan(
            self._pk(p),
            " and ".join(clauses) or None,
            params,
            lambda r: all(w in r.get("search_text", "") for w in words),
            limit,
            cursor,
        )
        return Page[Session](
            items=[self._session(r) for r in rows], next_cursor=nxt, scanned=scanned
        )

    async def session_get(
        self, p: Principal, session_id: str, checkpoint_seq: int | None, latest: bool
    ) -> SessionDetail:
        require(self.s, p, write=False)
        row = await self.hub._load(p, "session", session_id)
        count = int(row.get("checkpoint_count", 0))
        recent: list[dict[str, Any]] = []
        if count:
            recent = await self.store.list_between(
                self._pk(p),
                checkpoint_rk(session_id, max(1, count - RECENT_CHECKPOINTS + 1)),
                checkpoint_rk(session_id, count),
                RECENT_CHECKPOINTS,
            )
        events = await self.store.list_prefix(self._pk(p), f"e:{session_id}:", 20)
        if latest and count:
            checkpoint_seq = count
        full = None
        if checkpoint_seq is not None:
            full = await self._checkpoint(
                await self._load_checkpoint_row(p, session_id, checkpoint_seq)
            )
        return SessionDetail(
            session=self._session(row),
            checkpoints=[self._summary(r) for r in reversed(recent)],
            resumes=[
                ResumeEvent(
                    at=e["at"],
                    actor=e["actor"],
                    continuation_session_id=e["continuation_session_id"],
                    checkpoint_id=e["checkpoint_id"],
                )
                for e in events
            ],
            checkpoint=full,
        )

    async def session_checkpoint(
        self,
        p: Principal,
        session_id: str,
        content: CheckpointContent,
        expected_revision: int | None,
        idempotency_key: str | None,
    ) -> CheckpointResult:
        require(self.s, p, write=True)
        raw = content.model_dump_json()
        if len(raw.encode()) > CHECKPOINT_MAX_BYTES:
            raise HubError(
                "validation",
                f"checkpoint is {len(raw.encode())} bytes; the limit is {CHECKPOINT_MAX_BYTES}. "
                "Summarize more tightly; link details (PRs, memories) instead of pasting them.",
            )
        pk = self._pk(p)
        req_hash = _request_hash({"session": session_id, "content": raw})
        idem_key = (
            idem_rk(p.alias, "session_checkpoint", idempotency_key) if idempotency_key else None
        )
        if idem_key:
            prior = await self.store.get(pk, idem_key)
            if prior is not None:
                if prior.get("request_hash") != req_hash:
                    raise conflict("idempotency_key was already used with different arguments")
                sess = await self.hub._load(p, "session", session_id)
                return CheckpointResult(
                    checkpoint_id=prior["record_id"],
                    seq=int(prior["seq"]),
                    session_revision=int(sess["revision"]),
                    idempotent_replay=True,
                )

        # Without expected_revision, concurrent checkpoints in one session are serialized:
        # the loser re-reads and takes the next sequence number (bounded retries).
        attempts = 1 if expected_revision is not None else 4
        for attempt in range(attempts):
            row = await self.hub._load(p, "session", session_id)
            if row["status"] != "active":
                raise conflict(f"session '{session_id}' is {row['status']}; resume it to continue")
            if row["source_client"] != p.agent:
                raise HubError(
                    "forbidden",
                    f"session '{session_id}' belongs to {row['source_client']}; resume it "
                    "(session_resume) to continue in your own session",
                )
            seq = int(row.get("checkpoint_count", 0)) + 1
            cid = f"{session_id}-c{seq}"
            ts = now()
            blob = f"{p.workspace}/session/{session_id}/c{seq:06d}-{ULID()}.json"
            sha = await self.store.put_blob(blob, raw, "application/json")
            cp_row = {
                "PartitionKey": pk,
                "RowKey": checkpoint_rk(session_id, seq),
                "row_type": "checkpoint",
                "record_id": cid,
                "session_id": session_id,
                "seq": seq,
                "project": row["project"],
                "created_at": ts,
                "created_by": p.actor,
                "source_client": p.agent,
                "goal": content.goal[:200],
                "next_actions": len(content.next_actions),
                "has_code_state": content.code_state is not None,
                "size_bytes": len(raw.encode()),
                "content_blob": blob,
                "content_sha256": sha,
            }
            extra: list[tuple[str, dict[str, Any], str | None]] = [("create", cp_row, None)]
            if idem_key:
                extra.append(
                    (
                        "create",
                        {
                            "PartitionKey": pk,
                            "RowKey": idem_key,
                            "row_type": "idempotency",
                            "record_id": cid,
                            "seq": seq,
                            "request_hash": req_hash,
                            "created_at": ts,
                        },
                        None,
                    )
                )
            row["checkpoint_count"] = seq
            row["latest_checkpoint_id"] = cid
            row["latest_checkpoint_at"] = ts
            row["search_text"] = normalize(f"{row['title']} {row['source_client']} {content.goal}")
            try:
                rev = await self.hub._save(
                    p,
                    "session",
                    session_id,
                    row,
                    f"checkpoint {seq}",
                    expected_revision,
                    extra_ops=extra,
                    blob_name=blob,
                )
            except HubError as err:
                if err.code == "conflict" and attempt + 1 < attempts:
                    if idem_key and (replay := await self.store.get(pk, idem_key)) is not None:
                        sess = await self.hub._load(p, "session", session_id)
                        return CheckpointResult(
                            checkpoint_id=replay["record_id"],
                            seq=int(replay["seq"]),
                            session_revision=int(sess["revision"]),
                            idempotent_replay=True,
                        )
                    continue
                raise
            return CheckpointResult(checkpoint_id=cid, seq=seq, session_revision=rev)
        raise conflict("session changed concurrently too many times; retry")  # pragma: no cover

    async def session_close(
        self, p: Principal, session_id: str, expected_revision: int, note: str | None
    ) -> Session:
        require(self.s, p, write=True)
        row = await self.hub._load(p, "session", session_id)
        if row["source_client"] != p.agent:
            raise HubError("forbidden", f"session '{session_id}' belongs to {row['source_client']}")
        if row["status"] == "closed":
            raise conflict(f"session '{session_id}' is already closed")
        row["status"] = "closed"
        change = "closed" + (f": {note[:200]}" if note else "")
        rev = await self.hub._save(p, "session", session_id, row, change, expected_revision)
        row.update(revision=rev, updated_by=p.actor)
        return self._session(row)

    async def session_resume(
        self,
        p: Principal,
        project: str,
        session_id: str | None,
        checkpoint_id: str | None,
        latest: bool,
        detail: str,
        title: str | None,
        idempotency_key: str | None,
    ) -> ResumeResult:
        require(self.s, p, write=True)
        await self.hub._require_project(p, project)
        if sum(bool(x) for x in (session_id, checkpoint_id, latest)) != 1:
            raise HubError(
                "validation", "give exactly one of session_id, checkpoint_id or latest=true"
            )
        pk = self._pk(p)
        candidates: list[ResumeCandidate] = []
        if checkpoint_id:
            sid, seq = parse_checkpoint_id(checkpoint_id)
            src = await self.hub._load(p, "session", sid)
        elif session_id:
            src = await self.hub._load(p, "session", session_id)
            sid, seq = session_id, int(src.get("checkpoint_count", 0))
            if seq == 0:
                raise HubError("validation", f"session '{session_id}' has no checkpoints yet")
        else:
            rows, _, _ = await self.store.scan(
                pk,
                "project eq @project and status eq 'active' and checkpoint_count gt 0",
                {"project": project},
                lambda r: True,
                LATEST_SCAN,
                None,
            )
            rows.sort(key=lambda r: r.get("latest_checkpoint_at", ""), reverse=True)
            candidates = [
                ResumeCandidate(
                    session_id=r["record_id"],
                    title=r["title"],
                    source_client=r["source_client"],
                    latest_checkpoint_id=r["latest_checkpoint_id"],
                    latest_checkpoint_at=r["latest_checkpoint_at"],
                    goal="",
                )
                for r in rows
            ]
            if not rows:
                raise not_found("checkpointed active session in project", project)
            if len(rows) > 1:
                # Ambiguous: never guess between workstreams; ask for an explicit ID.
                for cand, r in zip(candidates, rows, strict=True):
                    cp = await self.store.get(
                        pk, checkpoint_rk(r["record_id"], int(r["checkpoint_count"]))
                    )
                    cand.goal = (cp or {}).get("goal", "")
                return ResumeResult(status="ambiguous", candidates=candidates[:5])
            src = rows[0]
            sid, seq = src["record_id"], int(src["checkpoint_count"])
            candidates = []
        if src["project"] != project:
            raise HubError("validation", f"session '{sid}' belongs to project '{src['project']}'")
        cp_row = await self._load_checkpoint_row(p, sid, seq)
        checkpoint = await self._checkpoint(cp_row)

        new_sid = str(ULID())
        ts = now()
        cont = {
            "record_id": new_sid,
            "project": project,
            "title": (title or f"Resumed: {src['title']}")[:200],
            "status": "active",
            "source_client": p.agent,
            "continues_session": sid,
            "resumed_from_checkpoint": checkpoint.id,
            "checkpoint_count": 0,
            "latest_checkpoint_id": "",
            "latest_checkpoint_at": "",
            "task_ids": _jl(checkpoint.content.task_ids),
            "search_text": normalize(
                f"{title or src['title']} {p.agent} {checkpoint.content.goal}"
            ),
            **self.hub._audit_new(p),
        }
        event = {
            "PartitionKey": pk,
            "RowKey": f"e:{sid}:{ULID()}",
            "row_type": "resume_event",
            "at": ts,
            "actor": p.actor,
            "continuation_session_id": new_sid,
            "checkpoint_id": checkpoint.id,
        }
        request = {"checkpoint": checkpoint.id, "agent": p.agent, "title": title}
        res = await self.hub._create(
            p,
            "session",
            new_sid,
            cont,
            "session_resume",
            idempotency_key,
            request,
            extra_ops=[("create", event, None)],
        )
        context = checkpoint.content
        truncated = False
        if detail == "overview":
            context, truncated = overview(checkpoint.content)
        return ResumeResult(
            status="resumed",
            selected_session_id=sid,
            selected_checkpoint_id=checkpoint.id,
            continuation_session_id=res.id,
            detail="full" if detail == "full" else "overview",
            truncated=truncated,
            context=context,
            idempotent_replay=res.idempotent_replay,
        )

    async def recent_active(self, p: Principal, project: str, limit: int = 5) -> list[Session]:
        rows, _, _ = await self.store.scan(
            self._pk(p),
            "project eq @project and status eq 'active'",
            {"project": project},
            lambda r: True,
            limit,
            None,
        )
        return [self._session(r) for r in rows]
