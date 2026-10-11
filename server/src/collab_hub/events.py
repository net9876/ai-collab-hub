"""Durable agent events: subscriptions, polling and acknowledgement (ADR 0001).

Events are written by HubService in the SAME entity-group transaction as the change
they report, into the source record's partition ("<ws>~task|message|decision") under
RowKey "x:<ULID>". An event therefore exists if and only if its change committed.

Consumers own a *subscription* (partition "<ws>~subscription", natural RowKey
"r:<sha256(owner_actor|name)>") holding per-kind cursors (last acknowledged event row
key) and the IDs acknowledged inside the replay window. event_poll is a pure read;
event_ack advances the cursors with an ETag-guarded replace. Delivery is
at-least-once: consumers dedupe by event_id.

Replay window: a ULID is minted before its transaction commits, so an event can
become visible slightly after a newer one was already acknowledged. Every poll
therefore re-reads from (cursor time - REPLAY_WINDOW) and drops IDs that were
already acknowledged (`acked_recent`), which is pruned to the window.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from ulid import ULID

from .auth import Principal, require
from .errors import HubError, conflict, not_found
from .models import (
    AckResult,
    Event,
    PollResult,
    SubscribeResult,
    Subscription,
    SubscriptionList,
    UnsubscribeResult,
)
from .service import HubService, _jl, _pl, event_rk, now
from .store import partition, record_rk

EVENT_KINDS = ("task", "message", "decision")
REPLAY_WINDOW_SECONDS = 120
ACKED_RECENT_CAP = 500
ACK_ATTEMPTS = 4
MAX_SUBSCRIPTIONS_LISTED = 50
EVENT_HIGH = "x;"  # first key after every "x:..." row

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_VALUE = {c: i for i, c in enumerate(_CROCKFORD)}


def ulid_ms(ulid: str) -> int:
    """Millisecond timestamp encoded in the first 10 characters of a ULID."""
    value = 0
    for char in ulid[:10]:
        value = value * 32 + _CROCKFORD_VALUE[char]
    return value


def ulid_floor(ms: int) -> str:
    """Smallest ULID with the given millisecond timestamp."""
    ms = max(0, ms)
    chars = []
    for _ in range(10):
        chars.append(_CROCKFORD[ms & 31])
        ms >>= 5
    return "".join(reversed(chars)) + "0" * 16


def window_floor(cursor_rk: str, window_seconds: int = REPLAY_WINDOW_SECONDS) -> str:
    """Lowest event row key a poll re-reads for a kind whose cursor is `cursor_rk`."""
    return "x:" + ulid_floor(ulid_ms(cursor_rk[2:]) - window_seconds * 1000)


def start_rk(start_from: str) -> str:
    """Row key where a new subscription starts: 'now', 'beginning' or an ISO time."""
    if start_from == "beginning":
        return "x:"
    if start_from == "now":
        moment = datetime.now(UTC)
    else:
        try:
            moment = datetime.fromisoformat(start_from)
        except ValueError as exc:
            raise HubError("validation", "start_from must be now, beginning or ISO-8601") from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    return "x:" + ulid_floor(int(moment.timestamp() * 1000))


def subscription_key(actor: str, name: str) -> str:
    """Natural key: unique per owner actor, so a create race has exactly one winner."""
    return hashlib.sha256(f"{actor}|{name}".encode()).hexdigest()[:40]


def kinds_for(event_types: Sequence[str]) -> list[str]:
    if not event_types:
        return list(EVENT_KINDS)
    wanted = {t.split(".", 1)[0] for t in event_types}
    return [k for k in EVENT_KINDS if k in wanted]


def _any_of(field: str, values: list[str], prefix: str, params: dict[str, Any]) -> str:
    parts = []
    for i, value in enumerate(values):
        params[f"{prefix}{i}"] = value
        parts.append(f"{field} eq @{prefix}{i}")
    return "( " + " or ".join(parts) + " )"  # the SDK needs spaces around @params


class EventService:
    def __init__(self, hub: HubService) -> None:
        self.hub = hub
        self.s = hub.s
        self.store = hub.store

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _pk(p: Principal) -> str:
        return partition(p.workspace, "subscription")

    def _subscription(self, row: dict[str, Any]) -> Subscription:
        cursors = json.loads(row.get("cursors") or "{}")
        return Subscription(
            id=row["subscription_id"],
            name=row["name"],
            owner_actor=row["owner_actor"],
            status=row["status"],
            agents=_pl(row.get("agents")),
            event_types=_pl(row.get("event_types")),
            projects=_pl(row.get("projects")),
            kinds=_pl(row.get("kinds")),
            start_from=row.get("start_from", "now"),
            cursors={k: v[2:] for k, v in cursors.items()},
            acked_recent=len(json.loads(row.get("acked_recent") or "[]")),
            **self.hub._audit(row),
        )

    async def _lookup(self, p: Principal, name: str) -> dict[str, Any] | None:
        return await self.store.get(
            self._pk(p), record_rk("subscription", subscription_key(p.actor, name))
        )

    async def _owned(self, p: Principal, name: str) -> dict[str, Any]:
        """The caller's active subscription `name`; forbidden if another agent owns it."""
        row = await self._lookup(p, name)
        if row is not None and row["owner_actor"] != p.actor:  # defence in depth
            raise HubError("forbidden", f"subscription '{name}' belongs to another agent")
        if row is not None and row["status"] == "active":
            return row
        others, _, _ = await self.store.scan(
            self._pk(p),
            "name eq @name and status eq 'active'",
            {"name": name},
            lambda r: r.get("owner_actor") != p.actor,
            1,
            None,
        )
        if others:
            raise HubError(
                "forbidden",
                f"subscription '{name}' belongs to {others[0]['owner_actor']}; only its owner "
                "may poll, acknowledge or remove it",
            )
        raise not_found("subscription", name)

    @staticmethod
    def _low(row_start: str, cursor: str | None) -> str:
        if not cursor:
            return row_start
        return max(row_start, window_floor(cursor))

    @staticmethod
    def _filter(row: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        params: dict[str, Any] = {}
        clauses: list[str] = []
        agents = _pl(row.get("agents"))
        if agents:
            params["unaddressed"] = ""
            clauses.append(
                "( to_agent eq @unaddressed or " + _any_of("to_agent", agents, "a", params) + " )"
            )
        types = _pl(row.get("event_types"))
        if types:
            clauses.append(_any_of("event_type", types, "t", params))
        projects = _pl(row.get("projects"))
        if projects:
            clauses.append(_any_of("project", projects, "p", params))
        return (" and ".join(clauses) or None), params

    @staticmethod
    def _event(row: dict[str, Any]) -> Event:
        return Event(
            event_id=row["event_id"],
            event_type=row["event_type"],
            kind=row["kind"],
            record_id=row["record_id"],
            record_revision=int(row["record_revision"]),
            occurred_at=row["occurred_at"],
            actor=row["actor"],
            to_agent=row.get("to_agent", ""),
            project=row.get("project") or None,
            payload=json.loads(row.get("payload") or "{}"),
        )

    # ------------------------------------------------------------------ tools

    async def subscribe(
        self,
        p: Principal,
        name: str,
        agents: Sequence[str] | None,
        event_types: Sequence[str] | None,
        projects: Sequence[str] | None,
        start_from: str | None,
    ) -> SubscribeResult:
        require(self.s, p, write=True)
        for slug in dict.fromkeys(projects or []):
            await self.hub._require_project(p, slug)
        key = subscription_key(p.actor, name)
        for _ in range(2):  # a lost create race falls through to the update path
            row = await self._lookup(p, name)
            if row is None or row["status"] != "active":
                begin = start_from or "now"
                fields = {
                    "name": name,
                    "owner_actor": p.actor,
                    "status": "active",
                    "agents": _jl(sorted(set(agents if agents is not None else [p.agent, "any"]))),
                    "event_types": _jl(sorted(set(event_types or []))),
                    "projects": _jl(sorted(set(projects or []))),
                    "kinds": _jl(kinds_for(event_types or [])),
                    "start_from": begin,
                    "start_rk": start_rk(begin),
                    "cursors": "{}",
                    "acked_recent": "[]",
                }
                if row is None:
                    new = {
                        "record_id": key,
                        "subscription_id": str(ULID()),
                        **fields,
                        **self.hub._audit_new(p),
                    }
                    try:
                        await self.hub._create(
                            p, "subscription", key, new, "event_subscribe", None, {"name": name}
                        )
                    except HubError as err:
                        if err.code == "conflict":
                            continue
                        raise
                else:  # re-subscribing after event_unsubscribe: fresh position
                    row.update(fields)
                    await self.hub._save(p, "subscription", key, row, "resubscribed", None)
                return SubscribeResult(
                    subscription=self._subscription(await self.hub._load(p, "subscription", key)),
                    created=True,
                )
            changed = False
            for field, value in (
                ("agents", agents),
                ("event_types", event_types),
                ("projects", projects),
            ):
                if value is not None and _pl(row.get(field)) != sorted(set(value)):
                    row[field] = _jl(sorted(set(value)))
                    changed = True
            if changed:
                row["kinds"] = _jl(kinds_for(_pl(row.get("event_types"))))
                await self.hub._save(p, "subscription", key, row, "filters updated", None)
                row = await self.hub._load(p, "subscription", key)
            return SubscribeResult(subscription=self._subscription(row), created=False)
        raise conflict("subscription changed concurrently; retry")  # pragma: no cover

    async def subscriptions(self, p: Principal) -> SubscriptionList:
        require(self.s, p, write=False)
        rows, _, _ = await self.store.scan(
            self._pk(p),
            "owner_actor eq @actor and status eq 'active'",
            {"actor": p.actor},
            lambda r: True,
            MAX_SUBSCRIPTIONS_LISTED,
            None,
        )
        items = sorted((self._subscription(r) for r in rows), key=lambda s: s.name)
        return SubscriptionList(items=items)

    async def unsubscribe(self, p: Principal, name: str) -> UnsubscribeResult:
        require(self.s, p, write=True)
        row = await self._lookup(p, name)
        if row is not None and row["owner_actor"] == p.actor and row["status"] != "active":
            return UnsubscribeResult(name=name, removed=False)
        row = await self._owned(p, name)
        row["status"] = "inactive"
        await self.hub._save(
            p, "subscription", subscription_key(p.actor, name), row, "unsubscribed", None
        )
        return UnsubscribeResult(name=name, removed=True)

    async def poll(self, p: Principal, name: str, limit: int) -> PollResult:
        require(self.s, p, write=False)
        row = await self._owned(p, name)
        cursors: dict[str, str] = json.loads(row.get("cursors") or "{}")
        acked: dict[str, str] = dict(json.loads(row.get("acked_recent") or "[]"))
        flt, params = self._filter(row)

        async def read_kind(kind: str) -> tuple[list[dict[str, Any]], bool]:
            skip = {eid for eid, k in acked.items() if k == kind}
            budget = limit + len(skip)
            rows = await self.store.list_between(
                partition(p.workspace, kind),
                self._low(row["start_rk"], cursors.get(kind)),
                EVENT_HIGH,
                budget,
                flt,
                params,
            )
            return [r for r in rows if r["event_id"] not in skip], len(rows) >= budget

        results = await asyncio.gather(*(read_kind(k) for k in _pl(row.get("kinds"))))
        fresh = sorted((r for rows, _ in results for r in rows), key=lambda r: str(r["event_id"]))
        more = any(full for _, full in results) or len(fresh) > limit
        return PollResult(
            subscription=name, events=[self._event(r) for r in fresh[:limit]], more=more
        )

    async def ack(self, p: Principal, name: str, event_ids: list[str]) -> AckResult:
        require(self.s, p, write=True)
        ids = list(dict.fromkeys(event_ids))
        row = await self._owned(p, name)
        kinds = _pl(row.get("kinds"))

        async def locate(eid: str) -> str | None:
            for kind in kinds:
                if await self.store.get(partition(p.workspace, kind), event_rk(eid)):
                    return kind
            return None

        known: dict[str, str] = dict(json.loads(row.get("acked_recent") or "[]"))
        lookups = [eid for eid in ids if eid not in known]
        located = dict(zip(lookups, await asyncio.gather(*map(locate, lookups)), strict=True))

        for attempt in range(ACK_ATTEMPTS):
            if attempt:
                row = await self._owned(p, name)
            cursors: dict[str, str] = json.loads(row.get("cursors") or "{}")
            acked: dict[str, str] = dict(json.loads(row.get("acked_recent") or "[]"))
            out = AckResult(acked=[], already_acked=[], not_found=[], cursors={})
            new_ids: dict[str, str] = {}
            for eid in ids:
                kind = acked.get(eid) or located.get(eid)
                if eid in acked:
                    out.already_acked.append(eid)
                elif kind is None or kind not in kinds or event_rk(eid) < row["start_rk"]:
                    out.not_found.append(eid)
                elif event_rk(eid) < self._low(row["start_rk"], cursors.get(kind)):
                    out.already_acked.append(eid)  # behind the window: never redelivered
                else:
                    out.acked.append(eid)
                    new_ids[eid] = kind
            for eid, kind in new_ids.items():
                if event_rk(eid) > cursors.get(kind, ""):
                    cursors[kind] = event_rk(eid)
            out.cursors = {k: v[2:] for k, v in cursors.items()}
            if not new_ids:
                return out
            acked.update(new_ids)
            keep = [
                (eid, kind)
                for eid, kind in acked.items()
                if event_rk(eid) >= self._low(row["start_rk"], cursors.get(kind))
            ]
            keep.sort(key=lambda item: item[0])
            row["cursors"] = json.dumps(cursors, separators=(",", ":"))
            row["acked_recent"] = json.dumps(
                [list(item) for item in keep[-ACKED_RECENT_CAP:]], separators=(",", ":")
            )
            row["last_ack_at"] = now()
            try:
                await self.hub._save(
                    p,
                    "subscription",
                    subscription_key(p.actor, name),
                    row,
                    "ack",
                    None,
                    history=False,
                )
            except HubError as err:
                if err.code == "conflict" and attempt + 1 < ACK_ATTEMPTS:
                    continue  # another ack of the same subscription won; merge and retry
                raise
            return out
        raise conflict("subscription changed concurrently; retry")  # pragma: no cover
