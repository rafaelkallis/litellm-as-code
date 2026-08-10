"""Team reconciler.

Identity: for teams with a fixed `team_id` in the spec, match on that; otherwise
match on `team_alias`.

Memberships (`members_with_roles`) are handled by `reconcile_team_members`:
- create the team first,
- then reconcile its members (member_add / member_delete).

Mutation: POST /team/new | /team/update | /team/member_add | /team/member_delete.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..types import Action, Diff

COMPARABLE = ["team_alias", "organization_id", "max_budget", "budget_duration", "models"]


def _find_remote(
    teams: list[dict[str, Any]], team_id: str | None, team_alias: str | None
) -> dict[str, Any] | None:
    for t in teams:
        if team_id and t.get("team_id") == team_id:
            return t
    if team_alias:
        for t in teams:
            if t.get("team_alias") == team_alias:
                return t
    return None


def reconcile_teams(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
    dry_run: bool = False,
) -> tuple[list[Diff], list[dict[str, Any]]]:
    """Reconcile teams, return (diffs, reconciled_team_specs).

    `reconciled_team_specs` lets `reconcile_team_members` find the remote
    team_id after a create, without re-listing.
    """
    diffs: list[Diff] = []
    reconciled: list[dict[str, Any]] = []
    live = client.list_teams()

    for entry in spec_entries:
        team_id = entry.get("team_id")
        team_alias = entry.get("team_alias")
        display = team_alias or team_id or "(unnamed)"

        existing = _find_remote(live, team_id, team_alias)
        if existing is None:
            diffs.append(Diff("team", display, Action.CREATE))
            if not dry_run:
                payload = dict(entry)
                if team_id:
                    payload["team_id"] = team_id
                client.create_team(payload)
                live = client.list_teams()  # refresh so member reconcile finds it
            else:
                # pretend-created: members can't be resolved in dry-run, skip members
                diffs.append(
                    Diff(
                        "team_member",
                        f"{display}/*",
                        Action.CREATE,
                        message="members (team will be created)",
                    )
                )
                continue
        else:
            changes = comparable_diff(entry, existing, COMPARABLE)
            diffs.append(
                Diff("team", display, Action.UPDATE if changes else Action.NOOP, changes)
            )
            if changes and not dry_run:
                client.update_team(entry)

        reconciled.append(dict(entry, _remote_team_id=existing_team_id(existing, client, live) if existing is None else existing.get("team_id")))

    return diffs, reconciled


def existing_team_id(existing: dict[str, Any] | None, client: LiteLLMClient, live: list[dict[str, Any]]) -> str:
    # find the remote team_id for the just-created/updated team
    alias = (existing or {}).get("team_alias")
    for t in live:
        if t.get("team_alias") == alias:
            return t["team_id"]
    return (existing or {}).get("team_id") or ""


def reconcile_team_members(
    client: LiteLLMClient,
    team_specs: list[dict[str, Any]],
    dry_run: bool = False,
) -> list[Diff]:
    """Make each team's live members match `members_with_roles` in the spec.

    Only handles members *declared* in the spec (adds missing, updates roles,
    removes members not in the spec for that team).
    """
    diffs: list[Diff] = []
    for team in team_specs:
        want = team.get("members_with_roles", [])
        team_id = team.get("_remote_team_id") or team.get("team_id")
        display = team.get("team_alias") or team_id or "(unnamed)"
        if not team_id:
            continue

        # find current members via team info (list returns member objects)
        team_info = _team_info(client, team_id)
        live_members = team_info.get("members_with_roles", [])

        want_by_id = {m["user_id"]: m.get("role") for m in want}
        live_by_id = {m["user_id"]: m.get("role") for m in live_members if "user_id" in m}

        for uid, role in want_by_id.items():
            if uid not in live_by_id:
                diffs.append(Diff("team_member", f"{display}/{uid}", Action.CREATE))
                if not dry_run:
                    client.add_team_members(team_id, [{"user_id": uid, "role": role}])
            elif live_by_id[uid] != role:
                diffs.append(
                    Diff(
                        "team_member",
                        f"{display}/{uid}",
                        Action.UPDATE,
                        {"role": (live_by_id[uid], role)},
                    )
                )
                if not dry_run:
                    client.add_team_members(team_id, [{"user_id": uid, "role": role}])

        for uid in live_by_id:
            if uid not in want_by_id:
                diffs.append(Diff("team_member", f"{display}/{uid}", Action.UPDATE, {}))
                if not dry_run:
                    client.delete_team_member(team_id, user_id=uid)

    return diffs


def _team_info(client: LiteLLMClient, team_id: str) -> dict[str, Any]:
    # GET /team/info?team_id=... returns {team_info: {...}} per provider research
    payload = client._request("GET", "/team/info", params={"team_id": team_id})
    return client.unwrap(payload, "team_info")
