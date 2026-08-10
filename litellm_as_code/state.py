"""Local applied-state file.

The proxy's admin API does NOT return secret material back:

* `credential_values` (api_key/api_base/...) are never read back
* a virtual key's raw `key` value is never read back (only a hashed name)

So the reconciler cannot detect drift for these fields by diffing against
the live API. Instead it keeps a local "applied state" — the same trick that
Terraform's `.tfstate` uses. The live API decides drift for every comparable
field; the state file decides whether a secret was already sent (and with what
value), so we don't re-rotate keys / re-send credentials on every run.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class State:
    version: int = 1
    # identity -> applied secret payload (maps a resource's stable key to its secret fields)
    credentials: dict[str, Any] = field(default_factory=dict)
    keys: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "State":
        return cls(
            version=data.get("version", 1),
            credentials=data.get("credentials", {}),
            keys=data.get("keys", {}),
        )


class StateStore:
    """Reads/writes a State JSON file. Creates parent dirs, chmod 600."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def load(self) -> State:
        if not self.path.exists():
            return State()
        try:
            with self.path.open() as f:
                return State.from_dict(json.load(f))
        except (json.JSONDecodeError, OSError):
            # Corrupt/missing state is not fatal: treat as empty and re-apply.
            return State()

    def save(self, state: State) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # atomic-ish write (same dir, then rename)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state.to_dict(), f, indent=2, sort_keys=True)
                f.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
