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
from ..log import warn
from ..types import Action, Diff, ReconcilerError
from ..validation import DEFAULT_ORG_ROLE

COMPARABLE = ["organization_alias", "models"]

# Since LiteLLM v1.102.0 the ENTIRE /organization router is wrapped with a
# dependency that gates it on an Enterprise license; on unlicensed
# self-hosted proxies every /organization/* call answers HTTP 403 with a body
# like "Organizations are only available for LiteLLM Enterprise users", and
# LiteLLMClient._request embeds that body verbatim in the ReconcilerError
# text. Tolerance requires EVERY marker below (case-insensitive) in the error
# text — a plain 401, a 500, or a 403 with a different body must never be
# swallowed as the gate (see tests/test_org_gate.py).
ORG_GATE_MARKERS = ("403", "enterprise")


def is_org_gate_error(exc: ReconcilerError) -> bool:
    """True only when the error text carries every enterprise-gate marker."""
    text = str(exc).lower()
    return all(marker in text for marker in ORG_GATE_MARKERS)


def _list_organizations_tolerating_gate(
    client: LiteLLMClient,
) -> list[dict[str, Any]] | None:
    """list_organizations(), or None when the proxy 403'd the enterprise gate.

    Any other ReconcilerError (401, 500, a 403 without the gate body, ...)
    propagates unchanged.
    """
    try:
        return client.list_organizations()
    except ReconcilerError as exc:
        if is_org_gate_error(exc):
            return None
        raise


def reconcile_organizations(
    client: LiteLLMClient,
    spec_entries: list[dict[str, Any]],
    dry_run: bool = False,
) -> tuple[list[Diff], list[dict[str, Any]]]:
    """Reconcile organizations, return (diffs, reconciled_org_specs).

    `reconciled_org_specs` lets `reconcile_org_members` find the remote
    organization_id after a create, without re-listing.

    LiteLLM Enterprise gate: on unlicensed proxies (>=1.102.0) every
    /organization/* call 403s. With no organizations declared the gate is
    tolerated (empty list, stderr warning); with organizations declared the
    reconciliation cannot converge and an actionable error is raised.
    """
    diffs: list[Diff] = []
    reconciled: list[dict[str, Any]] = []
    live = _list_organizations_tolerating_gate(client)
    if live is None:
        if spec_entries:
            raise ReconcilerError(
                f"spec declares {len(spec_entries)} organization(s), but this "
                "proxy refuses /organization endpoints (LiteLLM Enterprise "
                "license required). License the proxy, pin an older LiteLLM, "
                "or drop organizations from the spec."
            )
        warn(
            "litellm-as-code",
            "organization management skipped: this proxy only exposes "
            "/organization endpoints with a LiteLLM Enterprise license; "
            "continuing without organizations",
        )
        return [], []

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
                # Same gate tolerance as the initial listing: if the refresh
                # ever 403'd the gate, `live` would read empty and "live is
                # empty" unresolves the id below — a hard error, never a
                # silent member drop.
                live = _list_organizations_tolerating_gate(client) or []
                remote_org_id = _remote_org_id_from_live(live, entry, created_id)
                if not remote_org_id:
                    # The organization was created but its remote id cannot be
                    # confirmed; member reconcile must not silently drop the
                    # org's members (parity with the teams fix, issue #4).
                    raise ReconcilerError(
                        f"organization {display!r} was created but its remote "
                        "organization_id could not be resolved from the create "
                        "response or the organization listing; member "
                        "reconciliation aborted"
                    )
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
            # Mirror the create path (issue #9): members are reconciled
            # separately by reconcile_org_members and the remote id must
            # always be pinned on PATCH /organization/update.
            payload = {k: v for k, v in entry.items() if k != "members_with_roles"}
            payload["organization_id"] = existing["organization_id"]
            client.update_organization(payload)
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
    if not org_specs:
        # Nothing spec-owned to reconcile: skip the /organization/list
        # round-trip entirely — required so the enterprise-gate degradation
        # path above (gate hit with an org-free spec) can hand back empty
        # org_specs without this stage re-hitting the gated endpoint.
        return diffs
    live_by_id = {o.get("organization_id"): o for o in client.list_organizations()}

    for org in org_specs:
        want = org.get("members_with_roles", [])
        org_id = org.get("_remote_org_id") or org.get("organization_id")
        display = org.get("organization_alias") or org_id or "(unnamed)"
        if not org_id:
            # A spec-declared org with members whose identity cannot be
            # resolved is a hard error: silently skipping members would make a
            # successful run lie about convergence (parity with issue #4).
            raise ReconcilerError(
                f"organization {display!r} has no resolvable organization_id; "
                "members cannot be reconciled"
            )

        live_org = live_by_id.get(org_id, {})
        live_members = live_org.get("members", []) or []

        # An omitted role is server-defaulted ("internal_user") and echoed
        # back on the next read; resolving the shared default here is what
        # stops the perpetual role churn (issue #7) — and guarantees the role
        # update below never fires with role=None.
        want_by_id = {
            m["user_id"]: m.get("role") or DEFAULT_ORG_ROLE
            for m in want
            if m.get("user_id")
        }
        # The spec side may omit the role (issue #7): the shared server
        # default is resolved inline at BOTH comparison sides so an unset
        # value can never churn (issue #12). The pinned proxy (v1.97.0,
        # probed) always echoes member roles — making the live side symmetric
        # to the want side is a convergence guarantee, not an observed drift
        # class. Single-direction: an explicit spec role still diffs and
        # fires member_update. (Org member rows echo `user_role`; team rows
        # echo `role`.)
        live_by_user = {
            m.get("user_id"): m.get("user_role") or DEFAULT_ORG_ROLE
            for m in live_members
            if m.get("user_id")
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
                # Removals render as DELETE, not UPDATE({}), so the plan
                # describes a delete (issue #7).
                diffs.append(
                    Diff("organization_member", f"{display}/{uid}", Action.DELETE)
                )
                if not dry_run:
                    client.delete_organization_member(org_id, uid)

    return diffs


def _find_remote(
    orgs: list[dict[str, Any]], org_id: str | None, org_alias: str | None
) -> dict[str, Any] | None:
    # Identity contract (issue #9 review, teams.py parity): a spec entry that
    # declares a fixed organization_id matches ONLY on that id; the alias
    # fallback exists solely for alias-only entries. Letting it fire for an
    # unmatched fixed id would retarget (and, with id-pinned updates, silently
    # mutate) a different alias-matched organization.
    if org_id:
        for o in orgs:
            if o.get("organization_id") == org_id:
                return o
        return None
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
    # Same identity rule as _find_remote (issue #9 review): for a fixed-id
    # entry the id is authoritative even when the listing read is stale; only
    # alias-only entries may fall back to organization_alias.
    if alias and not entry.get("organization_id"):
        for o in live:
            if o.get("organization_alias") == alias:
                return o["organization_id"]
    return org_id or ""
