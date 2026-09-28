"""Azure Table + Blob persistence.

Layout (one table, one blob container):

* PartitionKey = "<workspace>~<kind>" — a workspace is the isolation boundary
  between principals; the server derives it from the verified identity, never
  from tool arguments.
* RowKey "r:<key>"          current record (ULIDs are stored inverted so that
                            ascending RowKey order means newest first)
* RowKey "v:<id>:<rev>"     immutable revision (history) row
* RowKey "i:<hash>"         idempotency row for create calls

Because a record, its revision row and its idempotency row share a partition,
every mutation is a single entity-group transaction: all rows commit or none
do. Optimistic concurrency uses the record's ETag (If-Match).

Large Markdown bodies live in blobs at "<workspace>/<kind>/<id>/<rev>.md",
written *before* the table transaction under a new immutable name. If the
transaction fails the blob is deleted best-effort; a leftover blob is never
referenced, so readers cannot observe a half-written change.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.data.tables import TableTransactionError, UpdateMode
from azure.data.tables.aio import TableServiceClient
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import BlobServiceClient

from .config import Settings
from .errors import HubError

log = logging.getLogger("collab_hub.store")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_INVERT = str.maketrans(_CROCKFORD, _CROCKFORD[::-1])

MAX_SCAN_PER_CALL = 500  # rows examined per search/list call before returning a cursor


def invert_ulid(ulid: str) -> str:
    """Order-reversing, self-inverse mapping of a ULID string."""
    return ulid.translate(_INVERT)


def record_rk(kind: str, key: str) -> str:
    return "r:" + (key if kind == "project" else invert_ulid(key))


def revision_rk(key: str, revision: int) -> str:
    return f"v:{key}:{revision:08d}"


def idem_rk(actor_alias: str, tool: str, key: str) -> str:
    digest = hashlib.sha256(f"{actor_alias}|{tool}|{key}".encode()).hexdigest()[:40]
    return f"i:{digest}"


def partition(workspace: str, kind: str) -> str:
    return f"{workspace}~{kind}"


RECORD_RANGE = "RowKey ge 'r:' and RowKey lt 'r;'"


@dataclass
class Cursor:
    token: Any = None
    skip: int = 0

    def encode(self) -> str:
        raw = json.dumps({"t": self.token, "s": self.skip}, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str | None) -> Cursor:
        if not value:
            return cls()
        try:
            padded = value + "=" * (-len(value) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode()))
            skip = int(data.get("s", 0))
            if skip < 0 or skip > 1000:
                raise ValueError
            return cls(token=data.get("t"), skip=skip)
        except Exception as exc:
            raise HubError("validation", "cursor is invalid or expired") from exc


def storage_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Translate transport/service failures to a retryable 'unavailable' error."""

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except HubError:
            raise
        except AzureError as exc:
            log.warning("storage error in %s: %s", fn.__name__, type(exc).__name__)
            raise HubError(
                "unavailable", "storage is temporarily unavailable; retry later"
            ) from exc

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class Store:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._credential: Any = None
        if settings.storage_connection_string:
            self._tables = TableServiceClient.from_connection_string(
                settings.storage_connection_string
            )
            self._blobs = BlobServiceClient.from_connection_string(
                settings.storage_connection_string
            )
        else:
            from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential

            if settings.managed_identity_client_id:
                self._credential = ManagedIdentityCredential(
                    client_id=settings.managed_identity_client_id
                )
            else:
                self._credential = DefaultAzureCredential(
                    exclude_interactive_browser_credential=True
                )
            acct = settings.storage_account
            self._tables = TableServiceClient(
                endpoint=f"https://{acct}.table.core.windows.net", credential=self._credential
            )
            self._blobs = BlobServiceClient(
                account_url=f"https://{acct}.blob.core.windows.net", credential=self._credential
            )
        self.table = self._tables.get_table_client(settings.table_name)
        self.container = self._blobs.get_container_client(settings.blob_container)

    async def ensure_created(self) -> None:
        """Create table/container if missing (local dev; Terraform owns them in Azure)."""
        try:
            await self._tables.create_table(self._s.table_name)
        except ResourceExistsError:
            pass
        try:
            await self.container.create_container()
        except ResourceExistsError:
            pass

    async def close(self) -> None:
        await self.table.close()
        await self._tables.close()
        await self._blobs.close()
        if self._credential is not None:
            await self._credential.close()

    # --- rows -------------------------------------------------------------

    @storage_errors
    async def get(self, pk: str, rk: str) -> dict[str, Any] | None:
        try:
            ent = await self.table.get_entity(pk, rk)
        except ResourceNotFoundError:
            return None
        row = dict(ent)
        row["_etag"] = ent.metadata["etag"]
        return row

    @storage_errors
    async def transact(self, ops: list[tuple[str, dict[str, Any], str | None]]) -> None:
        """Run create/replace operations atomically in one partition.

        ops: (op, entity, etag) with op in {"create", "replace"}; replace requires etag.
        Raises HubError("conflict") on a lost race or existing row. The index of the
        failing operation is available on the error as ``conflict_index``.
        """
        batch: list[Any] = []
        for op, entity, etag in ops:
            clean = {k: v for k, v in entity.items() if not k.startswith("_")}
            if op == "create":
                batch.append(("create", clean))
            elif op == "replace":
                assert etag, "replace needs an etag"
                batch.append(
                    (
                        "update",
                        clean,
                        {
                            "mode": UpdateMode.REPLACE,
                            "etag": etag,
                            "match_condition": MatchConditions.IfNotModified,
                        },
                    )
                )
            else:  # pragma: no cover
                raise ValueError(op)
        try:
            await self.table.submit_transaction(batch)
        except TableTransactionError as exc:
            if exc.status_code in (409, 412):
                err = HubError("conflict", "the record changed concurrently or already exists")
                err.conflict_index = exc.index  # type: ignore[attr-defined]
                raise err from exc
            raise

    @storage_errors
    async def scan(
        self,
        pk: str,
        extra_filter: str | None,
        params: dict[str, Any],
        predicate: Callable[[dict[str, Any]], bool],
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None, int]:
        """Keyword/metadata scan with a bounded budget and a resumable cursor."""
        cur = Cursor.decode(cursor)
        query = f"PartitionKey eq @pk and {RECORD_RANGE}"
        if extra_filter:
            query += f" and ( {extra_filter} )"  # SDK needs spaces around @params
        params = {"pk": pk, **params}
        results: list[dict[str, Any]] = []
        scanned = 0
        token, skip = cur.token, cur.skip
        page_size = 100
        while True:
            pager = self.table.query_entities(
                query, parameters=params, results_per_page=page_size
            ).by_page(continuation_token=token)
            try:
                page = await pager.__anext__()
            except StopAsyncIteration:
                return results, None, scanned
            rows = [ent async for ent in page]
            next_token = pager.continuation_token  # type: ignore[attr-defined]
            for idx in range(skip, len(rows)):
                scanned += 1
                ent = rows[idx]
                row = dict(ent)
                row["_etag"] = ent.metadata["etag"]
                if predicate(row):
                    results.append(row)
                if len(results) >= limit or scanned >= MAX_SCAN_PER_CALL:
                    if idx + 1 < len(rows):
                        return results, Cursor(token, idx + 1).encode(), scanned
                    nxt = Cursor(next_token, 0).encode() if next_token else None
                    return results, nxt, scanned
            if not next_token:
                return results, None, scanned
            token, skip = next_token, 0

    @storage_errors
    async def list_prefix(self, pk: str, prefix: str, limit: int) -> list[dict[str, Any]]:
        end = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        pager = self.table.query_entities(
            "PartitionKey eq @pk and RowKey ge @a and RowKey lt @b",
            parameters={"pk": pk, "a": prefix, "b": end},
            results_per_page=min(limit, 100),
        )
        out: list[dict[str, Any]] = []
        async for ent in pager:
            out.append(dict(ent))
            if len(out) >= limit:
                break
        return out

    # --- blobs ------------------------------------------------------------

    @storage_errors
    async def put_blob(self, name: str, text: str) -> str:
        data = text.encode("utf-8")
        await self.container.upload_blob(
            name,
            data,
            overwrite=False,
            content_settings=ContentSettings(content_type="text/markdown; charset=utf-8"),
        )
        return hashlib.sha256(data).hexdigest()

    @storage_errors
    async def get_blob(self, name: str, expected_sha256: str) -> str:
        try:
            downloader = await self.container.download_blob(name)
        except ResourceNotFoundError as exc:
            raise HubError("unavailable", "record content is missing") from exc
        data = await downloader.readall()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            log.error("content hash mismatch for blob in %s", name.split("/")[1])
            raise HubError("unavailable", "record content failed its integrity check")
        return data.decode("utf-8")

    async def delete_blob_quietly(self, name: str) -> None:
        try:
            await self.container.delete_blob(name)
        except (HttpResponseError, AzureError):
            log.warning("could not remove orphan blob (kind=%s)", name.split("/")[1])
