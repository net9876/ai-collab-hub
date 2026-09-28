"""Domain errors. Each maps to a stable code that clients can branch on."""

from __future__ import annotations

from typing import Literal

ErrorCode = Literal[
    "validation",  # bad input the schema could not catch (e.g. unknown project)
    "not_found",
    "conflict",  # optimistic concurrency or claim race lost
    "forbidden",  # authenticated, but not allowed
    "unavailable",  # storage or dependency failure; safe to retry later
]


class HubError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def not_found(kind: str, ident: str) -> HubError:
    return HubError("not_found", f"{kind} '{ident}' does not exist")


def conflict(message: str) -> HubError:
    return HubError("conflict", message)
