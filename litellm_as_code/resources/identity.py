"""Shared identity matchers for teams and organizations.

IDENTITY CONTRACT (issue #9 review — one authoritative copy lives here):

A spec entry that declares a fixed id (``team_id`` / ``organization_id``)
matches ONLY on that id. The alias fallback exists SOLELY for alias-only
entries; letting it fire for an unmatched fixed id would retarget (and, with
id-pinned updates, silently mutate) a different alias-matched resource.

``remote_id_from_live`` follows the same rule when locating a row that was
created moments ago in a refreshed listing: for fixed-id entries the id is
authoritative even when the listing read is stale (eventual consistency);
only alias-only entries may fall back to the alias.
"""

from __future__ import annotations

from typing import Any


def find_row(
    rows: list[dict[str, Any]],
    *,
    id_field: str,
    alias_field: str,
    fixed_id: str | None,
    alias: str | None,
) -> dict[str, Any] | None:
    """Match a live row by the identity contract documented in this module."""
    if fixed_id:
        for row in rows:
            if row.get(id_field) == fixed_id:
                return row
        return None
    if alias:
        for row in rows:
            if row.get(alias_field) == alias:
                return row
    return None


def remote_id_from_live(
    rows: list[dict[str, Any]],
    *,
    id_field: str,
    alias_field: str,
    fixed_id: str | None,
    alias: str | None,
    created_id: str | None = None,
) -> str:
    """Resolve a just-created row's remote id from the refreshed listing.

    Prefers the spec's fixed id (or the id minted by the create call), then
    — alias-only entries only — the alias. Returns "" when the id cannot be
    located; callers must treat that as a hard error, never a silent member
    drop (issue #4).
    """
    want_id = fixed_id or created_id
    for row in rows:
        if want_id and row.get(id_field) == want_id:
            return row[id_field]
    if alias and not fixed_id:
        for row in rows:
            if row.get(alias_field) == alias:
                return row[id_field]
    return want_id or ""
