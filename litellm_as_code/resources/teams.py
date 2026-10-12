"""Team reconciler.

Identity: the shared id-vs-alias contract in `resources.identity` — a fixed
`team_id` matches only on that id; the `team_alias` fallback exists solely
for alias-only entries.

Memberships (`members_with_roles`) are handled by `reconcile_team_members`:
- create the team first,
- then reconcile its members (member_add / member_update / member_delete;
  the shared loop lives in `resources.members`).

Mutation: POST /team/new | /team/update | /team/member_add | /team/member_delete.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..types import Action, Diff, ReconcilerError
from ..validation import DEFAULT_TEAM_ROLE
from .identity import find_row, remote_id_from_live
from .members import reconcile_members

COMPARABLE = ["team_alias", "organization_id", "max_budget", "budget_duration", "models"]


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

        existing = find_row(
            live,
            id_field="team_id",
            alias_field="team_alias",
            fixed_id=team_id,
            alias=team_alias,
        )
        if existing is None:
            diffs.append(Diff("team", display, Action.CREATE))
            if not dry_run:
                # Members are reconciled separately by reconcile_team_members
                # (issue #9): never ship members_with_roles on POST /team/new.
                payload = {k: v for k, v in entry.items() if k != "members_with_roles"}
                if team_id:
                    payload["team_id"] = team_id
                # POST /team/new returns the minted team_id flat at the root
                # (verified against terraform-provider-litellm); it is a
                # dependable identity source even when the refreshed listing
                # briefly misses the new row (eventual consistency).
                created = client.create_team(payload)
                live = client.list_teams()  # refresh so member reconcile finds it
                # Carry the remote team_id forward so member reconcile can attach
                # to it without a second lookup.
                remote_team_id = remote_id_from_live(
                    live,
                    id_field="team_id",
                    alias_field="team_alias",
                    fixed_id=entry.get("team_id"),
                    alias=team_alias,
                    created_id=created.get("team_id"),
                )
                if not remote_team_id:
                    # Never interpolate: the team was created but its remote id
                    # cannot be confirmed; member reconcile must not silently
                    # drop the team's members (issue #4).
                    raise ReconcilerError(
                        f"team {display!r} was created but its remote team_id "
                        "could not be resolved from the create response or the "
                        "team listing; member reconciliation aborted"
                    )
                reconciled.append(
                    dict(entry, _remote_team_id=remote_team_id, _just_created=True)
                )
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

        changes = comparable_diff(entry, existing, COMPARABLE)
        diffs.append(
            Diff("team", display, Action.UPDATE if changes else Action.NOOP, changes)
        )
        if changes and not dry_run:
            # Curated payload (issue #9): never ship members_with_roles
            # (reconciled separately), and always pin the resolved remote
            # team_id — the real proxy requires it on /team/update, even for
            # alias-only teams.
            payload = {k: v for k, v in entry.items() if k != "members_with_roles"}
            payload["team_id"] = existing["team_id"]
            client.update_team(payload)

        reconciled.append(dict(entry, _remote_team_id=existing["team_id"]))

    return diffs, reconciled


def reconcile_team_members(
    client: LiteLLMClient,
    team_specs: list[dict[str, Any]],
    dry_run: bool = False,
) -> list[Diff]:
    """Make each team's live members match `members_with_roles` in the spec.

    Only handles members *declared* in the spec (adds missing, updates roles,
    removes members not in the spec for that team). The add / role-fix /
    removal loop with the issue #7/#12 role-default handling is shared with
    organizations in `resources.members`; this wrapper binds the team client
    calls and the unresolvable-team_id hard error (issue #4).
    """
    diffs: list[Diff] = []
    for team in team_specs:
        want = team.get("members_with_roles", [])
        team_id = team.get("_remote_team_id") or team.get("team_id")
        display = team.get("team_alias") or team_id or "(unnamed)"
        if not team_id:
            # A spec-declared team with members whose identity cannot be
            # resolved is a hard error: silently skipping members would make a
            # successful run lie about convergence (issue #4).
            raise ReconcilerError(
                f"team {display!r} has no resolvable team_id; members cannot "
                "be reconciled"
            )

        # find current members via team info (list returns member objects).
        # Only a team created moments ago may 404 briefly read-after-write
        # (eventual consistency): the client's retry path stays scoped to
        # `_just_created` entries so unchanged/dry-run teams never pay the
        # exponential-backoff cost on a persistent 404.
        team_info = _team_info(client, team_id, retry=bool(team.get("_just_created")))
        live_members = team_info.get("members_with_roles", [])

        diffs.extend(
            reconcile_members(
                resource_type="team_member",
                container_display=display,
                want=want,
                live_members=live_members,
                default_role=DEFAULT_TEAM_ROLE,
                live_role_key="role",
                add=lambda members: client.add_team_members(team_id, members),
                set_role=lambda uid, role: client.update_team_member_role(
                    team_id, uid, role=role
                ),
                remove=lambda uid: client.delete_team_member(team_id, user_id=uid),
                dry_run=dry_run,
            )
        )

    return diffs


def _team_info(client: LiteLLMClient, team_id: str, retry: bool = False) -> dict[str, Any]:
    # GET /team/info?team_id=... returns {team_info: {...}} per provider research
    return client.get_team_info(team_id, retry=retry)
