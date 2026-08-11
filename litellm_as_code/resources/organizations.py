"""Organization reconciler.

Identity: `organization_id` (stable in the spec); organizations without a fixed
id fall back to `organization_alias`.

Memberships (`members_with_roles`) are handled by `reconcile_org_members`.

Comparable fields: `organization_alias`, `models`. Budget-shaped fields
(`max_budget`, `budget_duration`, etc.) are deliberately NOT diffed here:
when a spec sets them without a `budget_id`, LiteLLM auto-creates (or finds)
a budget row and returns the value nested under `litellm_budget_table` (plus a
server-managed `budget_id`). Diffing those top-level fields against live would
flag perpetual drift. Use the `budgets` section for named budget drift.

Mutation: POST /organization/new | PATCH /organization/update |
DELETE /organization/delete | /organization/member_add | /organization/member_update |
/organization/member_delete.
"""

from __future__ import annotations

from typing import Any

from ..api import LiteLLMClient
from ..diff import comparable_diff
from ..types import Action, Diff

COMPARABLE = ["organization_alias", "models"]


def reconcile_organizations(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
    dry_run: bool = False,
) -> tuple[list[Diff], list[dict[str, Any]]]:
    """Reconcile organizations, return (diffs, reconciled_org_specs).

    `reconciled_org_specs` lets `reconcile_org_members` find the remote
    organization_id after a create, without re-listing.
    """
    diffs: list[Diff] = []
    reconciled: list[dict[str, Any]] = []
    live = client.list_organizations()

    for entry in spec_entries:
        org_id = entry.get("organization_id")
        org_alias = entry.get("organization_alias")
        display = org_alias or org_id or "(unnamed)"

        existing = _find_remote(live, org_id, org_alias)
        if existing is None:
            diffs.append(Diff("organization", display, Action.CREATE))
            if not dry_run:
                # strip members; they are reconciled below
                create_payload = {
                    k: v for k, v in entry.items() if k != "members_with_roles"
                }
                created = client.create_organization(create_payload)
                created_id = created.get("organization_id")
                live = client.list_organizations()  # refresh
                remote_org_id = _remote_org_id_from_live(live, entry, created_id)
                reconciled.append(dict(entry, _remote_org_id=remote_org_id))
            else:
                diffs.append(
                    Diff(
                        "organization_member",
                        f"{display}/*",
                        Action.CREATE,
                        message="members (organization will be created)",
                    )
                )
            continue

        changes = comparable_diff(entry, existing, COMPARABLE)
        diffs.append(
            Diff(
                "organization",
                display,
                Action.UPDATE if changes else Action.NOOP,
                changes,
            )
        )
        if changes and not dry_run:
            client.update_organization(entry)
        reconciled.append(dict(entry, _remote_org_id=existing.get("organization_id")))

    return diffs, reconciled


def reconcile_org_members(
    client: LiteLLMClient,
    org_specs: list[dict[str, Any]],
    dry_run: bool = False,
) -> list[Diff]:
    """Make each org's live members match `members_with_roles` in the spec.

    Mirrors team members: adds members missing from the spec, updates roles,
    removes members not in the spec.

    Members are read from `/organization/list` (the API includes each org's
    `members`), so no per-org info round-trip is needed.
    """
    diffs: list[Diff] = []
    live_by_id = {o.get("organization_id"): o for o in client.list_organizations()}

    for org in org_specs:
        want = org.get("members_with_roles", [])
        org_id = org.get("_remote_org_id") or org.get("organization_id")
        display = org.get("organization_alias") or org_id or "(unnamed)"
        if not org_id:
            continue

        live_org = live_by_id.get(org_id, {})
        live_members = live_org.get("members", []) or []

        want_by_id = {m["user_id"]: m.get("role") for m in want if m.get("user_id")}
        live_by_user = {
            m.get("user_id"): m.get("user_role") for m in live_members if m.get("user_id")
        }

        for uid, role in want_by_id.items():
            if uid not in live_by_user:
                diffs.append(Diff("organization_member", f"{display}/{uid}", Action.CREATE))
                if not dry_run:
                    client.add_organization_members(
                        org_id, [{"user_id": uid, "role": role}]
                    )
            elif live_by_user[uid] != role:
                diffs.append(
                    Diff(
                        "organization_member",
                        f"{display}/{uid}",
                        Action.UPDATE,
                        {"role": (live_by_user[uid], role)},
                    )
                )
                if not dry_run:
                    client.update_organization_member(org_id, uid, role=role)

        for uid in live_by_user:
            if uid not in want_by_id:
                diffs.append(
                    Diff("organization_member", f"{display}/{uid}", Action.UPDATE, {})
                )
                if not dry_run:
                    client.delete_organization_member(org_id, uid)

    return diffs


def _find_remote(
    orgs: list[dict[str, Any]], org_id: str | None, org_alias: str | None
) -> dict[str, Any] | None:
    for o in orgs:
        if org_id and o.get("organization_id") == org_id:
            return o
    if org_alias:
        for o in orgs:
            if o.get("organization_alias") == org_alias:
                return o
    return None


def _remote_org_id_from_live(
    live: list[dict[str, Any]], entry: dict[str, Any], created_id: str | None = None
) -> str:
    """Resolve the remote organization_id for a just-created org."""
    org_id = entry.get("organization_id") or created_id
    alias = entry.get("organization_alias")
    for o in live:
        if org_id and o.get("organization_id") == org_id:
            return o["organization_id"]
    if alias:
        for o in live:
            if o.get("organization_alias") == alias:
                return o["organization_id"]
    return org_id or ""
