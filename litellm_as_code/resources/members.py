"""Shared membership reconcile loop for teams and organizations.

``reconcile_team_members`` (teams.py) and ``reconcile_org_members``
(organizations.py) express the SAME membership contract — make the live
members of one container (a team or an organization) match the spec's
``members_with_roles``:

- an omitted role is server-defaulted and echoed back on the next read;
  resolving the shared default at BOTH comparison sides is what stops the
  perpetual role churn (issue #7 on the want side; issue #12 on the live
  side — the pinned proxy echoes member roles on every read, so making the
  live side symmetric to the want side is a convergence guarantee, not an
  observed drift class; an explicit spec role still diffs and fires
  member_update on the next run),
- additions are member_add; role fixes are member_update (member_add 400s
  with ``team_member_already_in_team`` for an existing member; issue #7),
- removals render as DELETE diffs, never ``UPDATE({})``, so the plan
  describes a delete (issue #7).

This module holds that loop ONCE; each resource passes its identity knobs,
its server-default role and thin client-method closures. The container's
own identity contract (fixed id vs alias matching) lives in identity.py.
"""

from __future__ import annotations

from typing import Any, Callable

from ..types import Action, Diff


def reconcile_members(
    *,
    resource_type: str,
    container_display: str,
    want: list[dict[str, Any]],
    live_members: list[dict[str, Any]],
    default_role: str,
    live_role_key: str,
    add: Callable[[list[dict[str, Any]]], Any],
    set_role: Callable[[str, str], Any],
    remove: Callable[[str], Any],
    member_id_key: str = "user_id",
    want_role_key: str = "role",
    dry_run: bool = False,
) -> list[Diff]:
    """Reconcile the members of ONE container (see the module docstring).

    ``add(member_list)``, ``set_role(member_id, role)`` and
    ``remove(member_id)`` are the resource-specific client calls; they fire
    only when not ``dry_run``. A member row without the id key is skipped on
    both sides (spec rows are validated to carry it; a live echo that lacks
    it cannot be addressed by any membership call anyway).
    """
    diffs: list[Diff] = []
    want_by_id = {
        m[member_id_key]: m.get(want_role_key) or default_role
        for m in want
        if m.get(member_id_key)
    }
    live_by_id = {
        m.get(member_id_key): m.get(live_role_key) or default_role
        for m in live_members
        if m.get(member_id_key)
    }

    for member_id, role in want_by_id.items():
        if member_id not in live_by_id:
            diffs.append(
                Diff(
                    resource_type,
                    f"{container_display}/{member_id}",
                    Action.CREATE,
                )
            )
            if not dry_run:
                add([{member_id_key: member_id, want_role_key: role}])
        elif live_by_id[member_id] != role:
            diffs.append(
                Diff(
                    resource_type,
                    f"{container_display}/{member_id}",
                    Action.UPDATE,
                    {"role": (live_by_id[member_id], role)},
                )
            )
            if not dry_run:
                set_role(member_id, role)

    for member_id in live_by_id:
        if member_id not in want_by_id:
            diffs.append(
                Diff(
                    resource_type,
                    f"{container_display}/{member_id}",
                    Action.DELETE,
                )
            )
            if not dry_run:
                remove(member_id)

    return diffs
