"""Shared dataclasses & enums for litellm-as-code."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Action(str, Enum):
    """The diff actions the reconciler can apply to a resource."""

    CREATE = "create"
    UPDATE = "update"
    NOOP = "noop"

    # Human-friendly past/present tense for CLI output.
    @property
    def past(self) -> str:
        return {
            Action.CREATE: "created",
            Action.UPDATE: "updated",
            Action.NOOP: "unchanged",
        }[self]


@dataclass
class Diff:
    """A single resource-level diff: what changed and what to do about it."""

    resource_type: str  # "user", "team", "key", "credential", "model"
    name: str  # stable identifier for display (alias/id/model_name)
    action: Action
    # Human-readable field changes, e.g. {"user_role": "user" -> "proxy_admin"}.
    changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    message: str = ""

    def __str__(self) -> str:
        if self.action is Action.NOOP:
            return f"{self.resource_type:<11} {self.name:<38} ok"
        if self.action is Action.CREATE:
            return f"{self.resource_type:<11} {self.name:<38} would be created"
        parts = ", ".join(
            f"{k}: {old!r} -> {new!r}" for k, (old, new) in self.changes.items()
        )
        return f"{self.resource_type:<11} {self.name:<38} would be updated ({parts})"


@dataclass
class Plan:
    """The result of diffing a spec against the live API."""

    diffs: list[Diff] = field(default_factory=list)

    @property
    def create_count(self) -> int:
        return sum(1 for d in self.diffs if d.action is Action.CREATE)

    @property
    def update_count(self) -> int:
        return sum(1 for d in self.diffs if d.action is Action.UPDATE)

    @property
    def noop_count(self) -> int:
        return sum(1 for d in self.diffs if d.action is Action.NOOP)


class ReconcilerError(RuntimeError):
    """Raised when an API call fails or the proxy returns an error."""
