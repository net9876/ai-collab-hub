"""Domain operations. Every public method takes a verified Principal.

Authorization is enforced here (not only at the transport): read vs write,
workspace isolation via partition keys, and claimant checks for tasks.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from .auth import Principal, require
from .config import Settings
from .errors import HubError, conflict, not_found
from .models import (
    CreateResult,
    Decision,
    MarkReadResult,
    Memory,
    MemoryWithHistory,
    Message,
    Page,
    Project,
    ProjectContext,
    RevisionInfo,
    Task,
)
from .store import Store, idem_rk, partition, record_rk, revision_rk

_WORD = re.compile(r"[^\w]+", re.UNICODE)
SEARCH_TEXT_MAX = 16_000
SNIPPET_LEN = 240


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_WORD.sub(" ", text).split())


def _tokens(query: str | None) -> list[str]:
    return normalize(query or "").split()[:12]


def _jl(values: list[str] | None) -> str:
    return json.dumps(values or [], ensure_ascii=False)


def _pl(value: Any) -> list[str]:
    if not value:
        return []
    return list(json.loads(value))


def _request_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class HubService:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.s = settings
        self.store = store

    # ------------------------------------------------------------------ helpers

    def _audit_new(self, p: Principal) -> dict[str, Any]:
        ts = now()
        return {
            "revision": 1,
            "created_at": ts,
            "created_by": p.actor,
            "updated_at": ts,
            "updated_by": p.actor,
        }

    def _revision_row(
        self, pk: str, key: str, row: dict[str, Any], change: str, p: Principal
    ) -> dict[str, Any]:
        snapshot = {
            k: v
            for k, v in row.items()
            if not k.startswith("_") and k not in ("PartitionKey", "RowKey", "search_text")
        }
        return {
            "PartitionKey": pk,
            "RowKey": revision_rk(key, int(row["revision"])),
            "row_type": "revision",
            "record_id": key,
            "revision": int(row["revision"]),
            "at": row["updated_at"],
            "actor": p.actor,
            "change": change,
            "snapshot": json.dumps(snapshot, ensure_ascii=False)[:30_000],
        }

    async def _require_project(self, p: Principal, slug: str) -> dict[str, Any]:
        row = await self.store.get(partition(p.workspace, "project"), record_rk("project", slug))
        if row is None:
            raise HubError(
                "validation",
                f"unknown project '{slug}'. Call project_list, or ask the user to create it "
                "with project_create.",
            )
        return row

    async def _create(
        self,
        p: Principal,
        kind: str,
        key: str,
        row: dict[str, Any],
        tool: str,
        idempotency_key: str | None,
        request: dict[str, Any],
        extra_ops: list[tuple[str, dict[str, Any], str | None]] | None = None,
        blob_name: str | None = None,
    ) -> CreateResult:
        pk = partition(p.workspace, kind)
        row = {"PartitionKey": pk, "RowKey": record_rk(kind, key), "row_type": "record", **row}
        ops: list[tuple[str, dict[str, Any], str | None]] = [
            ("create", row, None),
            ("create", self._revision_row(pk, key, row, "created", p), None),
        ]
        req_hash = _request_hash(request)
        if idempotency_key:
            ops.append(
                (
                    "create",
                    {
                        "PartitionKey": pk,
                        "RowKey": idem_rk(p.alias, tool, idempotency_key),
                        "row_type": "idempotency",
                        "record_id": key,
                        "request_hash": req_hash,
                        "created_at": row["created_at"],
                    },
                    None,
                )
            )
        ops.extend(extra_ops or [])
        try:
            await self.store.transact(ops)
        except HubError as err:
            if blob_name:
                await self.store.delete_blob_quietly(blob_name)
            if err.code != "conflict":
                raise
            if idempotency_key:
                prior = await self.store.get(pk, idem_rk(p.alias, tool, idempotency_key))
                if prior is not None:
                    if prior.get("request_hash") != req_hash:
                        raise conflict(
                            "idempotency_key was already used with different arguments"
                        ) from err
                    rec = await self.store.get(pk, record_rk(kind, prior["record_id"]))
                    rev = int(rec["revision"]) if rec else 1
                    return CreateResult(id=prior["record_id"], revision=rev, idempotent_replay=True)
            if kind == "project":
                raise conflict(f"project '{key}' already exists") from err
            raise
        return CreateResult(id=key, revision=1)

    async def _load(self, p: Principal, kind: str, key: str) -> dict[str, Any]:
        row = await self.store.get(partition(p.workspace, kind), record_rk(kind, key))
        if row is None:
            raise not_found(kind, key)
        return row

    async def _save(
        self,
        p: Principal,
        kind: str,
        key: str,
        row: dict[str, Any],
        change: str,
        expected_revision: int | None,
        extra_ops: list[tuple[str, dict[str, Any], str | None]] | None = None,
        blob_name: str | None = None,
    ) -> int:
        """Bump revision and persist with If-Match; history row in the same transaction."""
        current = int(row["revision"])
        if expected_revision is not None and expected_revision != current:
            if blob_name:
                await self.store.delete_blob_quietly(blob_name)
            raise conflict(
                f"{kind} '{key}' is at revision {current}, not {expected_revision}; "
                "re-read it and reconcile"
            )
        etag = row["_etag"]
        row = dict(row)
        row["revision"] = current + 1
        row["updated_at"] = now()
        row["updated_by"] = p.actor
        pk = row["PartitionKey"]
        ops: list[tuple[str, dict[str, Any], str | None]] = [
            ("replace", row, etag),
            ("create", self._revision_row(pk, key, row, change, p), None),
        ]
        ops.extend(extra_ops or [])
        try:
            await self.store.transact(ops)
        except HubError:
            if blob_name:
                await self.store.delete_blob_quietly(blob_name)
            raise
        return current + 1

    @staticmethod
    def _audit(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "revision": int(row["revision"]),
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }

    # ------------------------------------------------------------------ projects

    async def project_create(
        self,
        p: Principal,
        slug: str,
        name: str,
        purpose: str,
        idempotency_key: str | None,
    ) -> CreateResult:
        require(self.s, p, write=True)
        row = {
            "slug": slug,
            "name": name,
            "purpose": purpose,
            "status": "active",
            "search_text": normalize(f"{slug} {name} {purpose}"),
            **self._audit_new(p),
        }
        return await self._create(
            p,
            "project",
            slug,
            row,
            "project_create",
            idempotency_key,
            {"slug": slug, "name": name, "purpose": purpose},
        )

    def _project(self, row: dict[str, Any]) -> Project:
        return Project(
            slug=row["slug"],
            name=row["name"],
            purpose=row["purpose"],
            status=row["status"],
            **self._audit(row),
        )

    async def project_list(
        self, p: Principal, status: str | None, limit: int, cursor: str | None
    ) -> Page[Project]:
        require(self.s, p, write=False)
        flt, params = (None, {})
        if status:
            flt, params = "status eq @status", {"status": status}
        rows, nxt, scanned = await self.store.scan(
            partition(p.workspace, "project"), flt, params, lambda r: True, limit, cursor
        )
        return Page[Project](
            items=[self._project(r) for r in rows], next_cursor=nxt, scanned=scanned
        )

    async def project_get_context(self, p: Principal, slug: str) -> ProjectContext:
        require(self.s, p, write=False)
        proj = await self._require_project(p, slug)
        flt = "project eq @project"
        prm = {"project": slug}
        decisions, _, _ = await self.store.scan(
            partition(p.workspace, "decision"),
            flt + " and status ne 'superseded'",
            prm,
            lambda r: True,
            10,
            None,
        )
        tasks, _, _ = await self.store.scan(
            partition(p.workspace, "task"),
            flt + " and (status eq 'open' or status eq 'in_progress' or status eq 'blocked')",
            prm,
            lambda r: True,
            20,
            None,
        )
        memories, _, _ = await self.store.scan(
            partition(p.workspace, "memory"),
            flt + " and status eq 'active'",
            prm,
            lambda r: True,
            10,
            None,
        )
        unread, _, _ = await self.store.scan(
            partition(p.workspace, "message"),
            "read eq false and (to_agent eq @agent or to_agent eq 'any')",
            {"agent": p.agent},
            lambda r: True,
            50,
            None,
        )
        return ProjectContext(
            project=self._project(proj),
            decisions=[self._decision(r) for r in decisions],
            open_tasks=[self._task(r) for r in tasks],
            memories=[self._memory(r, body=None) for r in memories],
            unread_messages_for_you=len(unread),
        )

    # ------------------------------------------------------------------ memories

    def _memory(self, row: dict[str, Any], body: str | None, snippet: bool = False) -> Memory:
        return Memory(
            id=row["record_id"],
            project=row["project"],
            title=row["title"],
            body=body,
            snippet=row.get("snippet") if snippet else None,
            tags=_pl(row.get("tags")),
            verified=bool(row["verified"]),
            sources=_pl(row.get("sources")),
            status=row["status"],
            content_sha256=row.get("content_sha256"),
            **self._audit(row),
        )

    @staticmethod
    def _memory_search_text(title: str, body: str, tags: list[str]) -> str:
        return normalize(f"{title} {' '.join(tags)} {body}")[:SEARCH_TEXT_MAX]

    async def memory_add(
        self,
        p: Principal,
        project: str,
        title: str,
        body: str,
        tags: list[str],
        verified: bool,
        sources: list[str],
        idempotency_key: str | None,
    ) -> CreateResult:
        require(self.s, p, write=True)
        await self._require_project(p, project)
        mid = str(ULID())
        blob = f"{p.workspace}/memory/{mid}/{1:08d}.md"
        request = {
            "project": project,
            "title": title,
            "body": body,
            "tags": tags,
            "verified": verified,
            "sources": sources,
        }
        if idempotency_key:
            # Replays must not upload a second blob; check the key first.
            pk = partition(p.workspace, "memory")
            prior = await self.store.get(pk, idem_rk(p.alias, "memory_add", idempotency_key))
            if prior is not None:
                if prior.get("request_hash") != _request_hash(request):
                    raise conflict("idempotency_key was already used with different arguments")
                return CreateResult(id=prior["record_id"], revision=1, idempotent_replay=True)
        sha = await self.store.put_blob(blob, body)
        row = {
            "record_id": mid,
            "project": project,
            "title": title,
            "tags": _jl(tags),
            "verified": verified,
            "sources": _jl(sources),
            "status": "active",
            "content_blob": blob,
            "content_sha256": sha,
            "content_len": len(body),
            "snippet": body[:SNIPPET_LEN],
            "search_text": self._memory_search_text(title, body, tags),
            **self._audit_new(p),
        }
        return await self._create(
            p, "memory", mid, row, "memory_add", idempotency_key, request, blob_name=blob
        )

    async def memory_get(
        self, p: Principal, memory_id: str, revision: int | None, include_history: bool
    ) -> MemoryWithHistory:
        require(self.s, p, write=False)
        row = await self._load(p, "memory", memory_id)
        if revision is not None and revision != int(row["revision"]):
            rev = await self.store.get(row["PartitionKey"], revision_rk(memory_id, revision))
            if rev is None:
                raise not_found("memory revision", f"{memory_id}@{revision}")
            snap = json.loads(rev["snapshot"])
            snap["record_id"] = memory_id
            row = snap
        body = await self.store.get_blob(row["content_blob"], row["content_sha256"])
        mem = self._memory(row, body)
        history: list[RevisionInfo] = []
        if include_history:
            rows = await self.store.list_prefix(
                row.get("PartitionKey") or partition(p.workspace, "memory"),
                f"v:{memory_id}:",
                100,
            )
            history = [
                RevisionInfo(
                    revision=int(r["revision"]), at=r["at"], actor=r["actor"], change=r["change"]
                )
                for r in rows
            ]
        return MemoryWithHistory(**mem.model_dump(), history=history)

    async def memory_search(
        self,
        p: Principal,
        query: str | None,
        project: str | None,
        tags: list[str],
        status: str,
        verified_only: bool,
        limit: int,
        cursor: str | None,
    ) -> Page[Memory]:
        require(self.s, p, write=False)
        clauses, params = ["status eq @status"], {"status": status}
        if project:
            clauses.append("project eq @project")
            params["project"] = project
        if verified_only:
            clauses.append("verified eq true")
        words = _tokens(query)
        want_tags = set(tags)

        def match(r: dict[str, Any]) -> bool:
            text = r.get("search_text", "")
            if any(w not in text for w in words):
                return False
            return not want_tags or want_tags.issubset(set(_pl(r.get("tags"))))

        rows, nxt, scanned = await self.store.scan(
            partition(p.workspace, "memory"), " and ".join(clauses), params, match, limit, cursor
        )
        return Page[Memory](
            items=[self._memory(r, body=None, snippet=True) for r in rows],
            next_cursor=nxt,
            scanned=scanned,
        )

    async def memory_update(
        self,
        p: Principal,
        memory_id: str,
        expected_revision: int,
        title: str | None,
        body: str | None,
        tags: list[str] | None,
        verified: bool | None,
        sources: list[str] | None,
        status: str | None,
        change_note: str | None,
    ) -> CreateResult:
        require(self.s, p, write=True)
        row = await self._load(p, "memory", memory_id)
        if int(row["revision"]) != expected_revision:
            raise conflict(
                f"memory '{memory_id}' is at revision {row['revision']}, not "
                f"{expected_revision}; re-read it and reconcile"
            )
        blob_name = None
        if title is not None:
            row["title"] = title
        if tags is not None:
            row["tags"] = _jl(tags)
        if verified is not None:
            row["verified"] = verified
        if sources is not None:
            row["sources"] = _jl(sources)
        if status is not None:
            row["status"] = status
        if body is not None:
            blob_name = f"{p.workspace}/memory/{memory_id}/{expected_revision + 1:08d}.md"
            row["content_sha256"] = await self.store.put_blob(blob_name, body)
            row["content_blob"] = blob_name
            row["content_len"] = len(body)
            row["snippet"] = body[:SNIPPET_LEN]
        if body is not None or title is not None or tags is not None:
            current_body = body
            if current_body is None:
                current_body = await self.store.get_blob(row["content_blob"], row["content_sha256"])
            row["search_text"] = self._memory_search_text(
                row["title"], current_body, _pl(row.get("tags"))
            )
        change = "archived" if status == "archived" else "updated"
        if change_note:
            change += f": {change_note[:200]}"
        rev = await self._save(
            p, "memory", memory_id, row, change, expected_revision, blob_name=blob_name
        )
        return CreateResult(id=memory_id, revision=rev)

    # ------------------------------------------------------------------ decisions

    def _decision(self, row: dict[str, Any]) -> Decision:
        return Decision(
            id=row["record_id"],
            project=row["project"],
            title=row["title"],
            context=row["context"],
            decision=row["decision"],
            alternatives=row.get("alternatives", ""),
            consequences=row.get("consequences", ""),
            evidence=_pl(row.get("evidence")),
            status=row["status"],
            supersedes=row.get("supersedes") or None,
            superseded_by=row.get("superseded_by") or None,
            **self._audit(row),
        )

    async def decision_add(
        self,
        p: Principal,
        project: str,
        title: str,
        context: str,
        decision: str,
        alternatives: str,
        consequences: str,
        evidence: list[str],
        status: str,
        supersedes: str | None,
        idempotency_key: str | None,
    ) -> CreateResult:
        require(self.s, p, write=True)
        await self._require_project(p, project)
        did = str(ULID())
        extra: list[tuple[str, dict[str, Any], str | None]] = []
        if supersedes:
            old = await self._load(p, "decision", supersedes)
            if old["status"] == "superseded":
                raise conflict(f"decision '{supersedes}' is already superseded")
            if old["project"] != project:
                raise HubError(
                    "validation", "a decision can only supersede one in the same project"
                )
            old = dict(old)
            etag = old.pop("_etag")
            old["status"] = "superseded"
            old["superseded_by"] = did
            old["revision"] = int(old["revision"]) + 1
            old["updated_at"] = now()
            old["updated_by"] = p.actor
            extra = [
                ("replace", old, etag),
                (
                    "create",
                    self._revision_row(
                        old["PartitionKey"], supersedes, old, f"superseded by {did}", p
                    ),
                    None,
                ),
            ]
        row = {
            "record_id": did,
            "project": project,
            "title": title,
            "context": context,
            "decision": decision,
            "alternatives": alternatives,
            "consequences": consequences,
            "evidence": _jl(evidence),
            "status": status,
            "supersedes": supersedes or "",
            "superseded_by": "",
            "search_text": normalize(f"{title} {context} {decision} {alternatives}")[
                :SEARCH_TEXT_MAX
            ],
            **self._audit_new(p),
        }
        request = {
            "project": project,
            "title": title,
            "decision": decision,
            "supersedes": supersedes,
        }
        return await self._create(
            p, "decision", did, row, "decision_add", idempotency_key, request, extra_ops=extra
        )

    async def decision_search(
        self,
        p: Principal,
        query: str | None,
        project: str | None,
        status: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[Decision]:
        require(self.s, p, write=False)
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if project:
            clauses.append("project eq @project")
            params["project"] = project
        if status:
            clauses.append("status eq @status")
            params["status"] = status
        words = _tokens(query)
        rows, nxt, scanned = await self.store.scan(
            partition(p.workspace, "decision"),
            " and ".join(clauses) or None,
            params,
            lambda r: all(w in r.get("search_text", "") for w in words),
            limit,
            cursor,
        )
        return Page[Decision](
            items=[self._decision(r) for r in rows], next_cursor=nxt, scanned=scanned
        )

    # ------------------------------------------------------------------ tasks

    def _task(self, row: dict[str, Any]) -> Task:
        return Task(
            id=row["record_id"],
            project=row["project"],
            title=row["title"],
            description=row.get("description", ""),
            status=row["status"],
            priority=row["priority"],
            labels=_pl(row.get("labels")),
            claimed_by=row.get("claimed_by") or None,
            claimed_at=row.get("claimed_at") or None,
            result=row.get("result") or None,
            evidence=_pl(row.get("evidence")),
            blocker=row.get("blocker") or None,
            **self._audit(row),
        )

    async def task_create(
        self,
        p: Principal,
        project: str,
        title: str,
        description: str,
        priority: str,
        labels: list[str],
        idempotency_key: str | None,
    ) -> CreateResult:
        require(self.s, p, write=True)
        await self._require_project(p, project)
        tid = str(ULID())
        row = {
            "record_id": tid,
            "project": project,
            "title": title,
            "description": description,
            "status": "open",
            "priority": priority,
            "labels": _jl(labels),
            "claimed_by": "",
            "claimed_at": "",
            "result": "",
            "evidence": "[]",
            "blocker": "",
            "search_text": normalize(f"{title} {description} {' '.join(labels)}"),
            **self._audit_new(p),
        }
        request = {
            "project": project,
            "title": title,
            "description": description,
            "priority": priority,
            "labels": labels,
        }
        return await self._create(p, "task", tid, row, "task_create", idempotency_key, request)

    async def task_get(self, p: Principal, task_id: str) -> Task:
        require(self.s, p, write=False)
        return self._task(await self._load(p, "task", task_id))

    async def task_list(
        self,
        p: Principal,
        project: str | None,
        status: str | None,
        claimed_by_me: bool,
        query: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[Task]:
        require(self.s, p, write=False)
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if project:
            clauses.append("project eq @project")
            params["project"] = project
        if status:
            clauses.append("status eq @status")
            params["status"] = status
        if claimed_by_me:
            clauses.append("claimed_by eq @me")
            params["me"] = p.actor
        words = _tokens(query)
        rows, nxt, scanned = await self.store.scan(
            partition(p.workspace, "task"),
            " and ".join(clauses) or None,
            params,
            lambda r: all(w in r.get("search_text", "") for w in words),
            limit,
            cursor,
        )
        return Page[Task](items=[self._task(r) for r in rows], next_cursor=nxt, scanned=scanned)

    async def task_claim(self, p: Principal, task_id: str) -> Task:
        require(self.s, p, write=True)
        row = await self._load(p, "task", task_id)
        if row["status"] != "open":
            holder = row.get("claimed_by") or "nobody"
            raise conflict(
                f"task '{task_id}' is {row['status']} (claimed by {holder}); pick another task"
            )
        row["status"] = "in_progress"
        row["claimed_by"] = p.actor
        row["claimed_at"] = now()
        try:
            rev = await self._save(p, "task", task_id, row, "claimed", None)
        except HubError as err:
            if err.code == "conflict":
                raise conflict(
                    f"task '{task_id}' was claimed or changed concurrently; pick another task"
                ) from err
            raise
        row["revision"] = rev
        row["updated_at"] = row["claimed_at"]
        row["updated_by"] = p.actor
        return self._task(row)

    async def task_update(
        self,
        p: Principal,
        task_id: str,
        expected_revision: int,
        title: str | None,
        description: str | None,
        priority: str | None,
        labels: list[str] | None,
        status: str | None,
        release: bool,
        note: str | None,
    ) -> Task:
        require(self.s, p, write=True)
        row = await self._load(p, "task", task_id)
        if row["status"] in ("done", "cancelled"):
            raise conflict(f"task '{task_id}' is {row['status']} and can no longer change")
        if title is not None:
            row["title"] = title
        if description is not None:
            row["description"] = description
        if priority is not None:
            row["priority"] = priority
        if labels is not None:
            row["labels"] = _jl(labels)
        change = "updated"
        if release:
            row["status"], row["claimed_by"], row["claimed_at"] = "open", "", ""
            row["blocker"] = ""
            change = "released"
        if status is not None:
            allowed = {"open", "cancelled"}
            if status not in allowed:
                raise HubError(
                    "validation",
                    "task_update can set status to open (unblock) or cancelled; use task_claim, "
                    "task_block or task_complete for the other transitions",
                )
            if status == "open" and row["status"] == "blocked":
                row["status"], row["blocker"] = (
                    ("in_progress", "") if row.get("claimed_by") else ("open", "")
                )
                change = "unblocked"
            elif status == "cancelled":
                row["status"] = "cancelled"
                change = "cancelled"
        if note:
            change += f": {note[:200]}"
        row["search_text"] = normalize(
            f"{row['title']} {row.get('description', '')} {' '.join(_pl(row.get('labels')))}"
        )
        rev = await self._save(p, "task", task_id, row, change, expected_revision)
        row.update(revision=rev, updated_by=p.actor)
        return self._task(row)

    async def _finish(
        self,
        p: Principal,
        task_id: str,
        expected_revision: int,
        status: str,
        fields: dict[str, Any],
        change: str,
    ) -> Task:
        require(self.s, p, write=True)
        row = await self._load(p, "task", task_id)
        if row["status"] != "in_progress":
            raise conflict(f"task '{task_id}' is {row['status']}; claim it first")
        if row.get("claimed_by") != p.actor:
            raise HubError(
                "forbidden",
                f"task '{task_id}' is claimed by {row.get('claimed_by')}; only the claimant can "
                f"{'complete' if status == 'done' else 'block'} it",
            )
        row["status"] = status
        row.update(fields)
        rev = await self._save(p, "task", task_id, row, change, expected_revision)
        row.update(revision=rev, updated_by=p.actor)
        return self._task(row)

    async def task_complete(
        self, p: Principal, task_id: str, expected_revision: int, result: str, evidence: list[str]
    ) -> Task:
        return await self._finish(
            p,
            task_id,
            expected_revision,
            "done",
            {"result": result, "evidence": _jl(evidence), "blocker": ""},
            "completed",
        )

    async def task_block(
        self, p: Principal, task_id: str, expected_revision: int, blocker: str
    ) -> Task:
        return await self._finish(
            p, task_id, expected_revision, "blocked", {"blocker": blocker}, "blocked"
        )

    # ------------------------------------------------------------------ messages

    def _message(self, row: dict[str, Any]) -> Message:
        return Message(
            id=row["record_id"],
            project=row.get("project") or None,
            from_actor=row["created_by"],
            to_agent=row["to_agent"],
            subject=row["subject"],
            body=row["body"],
            thread_id=row["thread_id"],
            reply_to=row.get("reply_to") or None,
            read=bool(row["read"]),
            read_at=row.get("read_at") or None,
            created_at=row["created_at"],
        )

    async def message_send(
        self,
        p: Principal,
        to_agent: str,
        subject: str,
        body: str,
        project: str | None,
        idempotency_key: str | None,
        *,
        reply_to: str | None = None,
        thread_id: str | None = None,
        tool: str = "message_send",
    ) -> CreateResult:
        require(self.s, p, write=True)
        if project:
            await self._require_project(p, project)
        mid = str(ULID())
        row = {
            "record_id": mid,
            "project": project or "",
            "to_agent": to_agent,
            "subject": subject,
            "body": body,
            "thread_id": thread_id or mid,
            "reply_to": reply_to or "",
            "read": False,
            "read_at": "",
            **self._audit_new(p),
        }
        request = {
            "to": to_agent,
            "subject": subject,
            "body": body,
            "project": project,
            "reply_to": reply_to,
        }
        return await self._create(p, "message", mid, row, tool, idempotency_key, request)

    async def message_reply(
        self, p: Principal, message_id: str, body: str, idempotency_key: str | None
    ) -> CreateResult:
        original = await self._load(p, "message", message_id)
        sender_agent = str(original["created_by"]).split("/", 1)[-1]
        if sender_agent not in {"claude-code", "claude-desktop", "codex", "chatgpt", "other"}:
            sender_agent = "any"
        subject = original["subject"]
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"[:200]
        return await self.message_send(
            p,
            sender_agent,
            subject,
            body,
            original.get("project") or None,
            idempotency_key,
            reply_to=message_id,
            thread_id=original["thread_id"],
            tool="message_reply",
        )

    async def message_inbox(
        self,
        p: Principal,
        include_read: bool,
        project: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[Message]:
        require(self.s, p, write=False)
        clauses = ["(to_agent eq @agent or to_agent eq 'any')"]
        params: dict[str, Any] = {"agent": p.agent}
        if not include_read:
            clauses.append("read eq false")
        if project:
            clauses.append("project eq @project")
            params["project"] = project
        rows, nxt, scanned = await self.store.scan(
            partition(p.workspace, "message"),
            " and ".join(clauses),
            params,
            lambda r: True,
            limit,
            cursor,
        )
        return Page[Message](
            items=[self._message(r) for r in rows], next_cursor=nxt, scanned=scanned
        )

    async def message_mark_read(self, p: Principal, message_ids: list[str]) -> MarkReadResult:
        require(self.s, p, write=True)
        out = MarkReadResult(updated=[], not_found=[], skipped_not_recipient=[])
        for mid in dict.fromkeys(message_ids):
            row = await self.store.get(partition(p.workspace, "message"), record_rk("message", mid))
            if row is None:
                out.not_found.append(mid)
                continue
            if row["to_agent"] not in (p.agent, "any"):
                out.skipped_not_recipient.append(mid)
                continue
            if row["read"]:
                out.updated.append(mid)
                continue
            row["read"], row["read_at"] = True, now()
            try:
                await self._save(p, "message", mid, row, "read", None)
            except HubError as err:
                if err.code != "conflict":
                    raise
                # Someone else marked it concurrently: the end state is the same.
            out.updated.append(mid)
        return out
