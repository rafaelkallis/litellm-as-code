"""The reconciler: spec + live API -> plan -> (apply)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .api import LiteLLMClient
from .log import banner, info
from .resources import (
    reconcile_budgets,
    reconcile_credentials,
    reconcile_guardrails,
    reconcile_keys,
    reconcile_models,
    reconcile_org_members,
    reconcile_organizations,
    reconcile_policies,
    reconcile_team_members,
    reconcile_teams,
    reconcile_users,
)
from .spec import load_spec
from .types import Diff, Plan


def _stage_diffs(
    client: LiteLLMClient,
    spec: dict[str, Any],
    dry_run: bool,
) -> Iterator[tuple[str, list[Diff]]]:
    """Yield (banner title, diffs) per reconcile stage, in the fixed order.

    Ordering is fixed (AGENTS.md §3): budgets -> models -> credentials ->
    organizations -> users -> org members -> teams -> team members -> keys ->
    guardrails -> policies. Acyclic & single-target. Models come before
    credentials because a credential's `model_id` must reference an existing
    model (POST /credentials 404s otherwise). Models/credentials come before
    organizations because LiteLLM requires at least one model configured
    before it lets you create an organization on a fresh proxy (probed
    live). Org members come after users because memberships must attach to
    user rows this run owns: on the pinned proxy (v1.97.0, probed)
    /organization/member_add even UPSERTS an unmanaged ghost user row for an
    unknown user, so users-first keeps the spec-declared row authoritative
    and convergence version-independent (issue #12). The same reasoning
    already applies to team members, which run right after their teams.

    Team members have no banner of their own: they are reported under the
    Teams title, exactly as before this stage table existed.
    """
    yield "Budgets", reconcile_budgets(
        client, spec.get("budgets", []), dry_run=dry_run
    )
    yield "Models", reconcile_models(client, spec.get("models", []), dry_run=dry_run)
    yield "Credentials", reconcile_credentials(
        client, spec.get("credentials", []), dry_run=dry_run
    )

    org_diffs, org_specs = reconcile_organizations(
        client, spec.get("organizations", []), dry_run=dry_run
    )
    yield "Organizations", org_diffs

    yield "Users", reconcile_users(client, spec.get("users", []), dry_run=dry_run)

    # Org members after users (issue #12): memberships must attach to user
    # rows this run owns. Rows created by the `users` step are the spec's
    # own, so members must always attach to those (rationale in the
    # docstring above).
    yield "Organization members", reconcile_org_members(
        client, org_specs, dry_run=dry_run
    )

    team_diffs, team_specs = reconcile_teams(
        client, spec.get("teams", []), dry_run=dry_run
    )
    yield "Teams", team_diffs + reconcile_team_members(
        client, team_specs, dry_run=dry_run
    )

    yield "Virtual keys", reconcile_keys(
        client, spec.get("virtual_keys", []), dry_run=dry_run
    )
    yield "Guardrails", reconcile_guardrails(
        client, spec.get("guardrails", []), dry_run=dry_run
    )
    yield "Policies", reconcile_policies(
        client, spec.get("policies", []), dry_run=dry_run
    )


def _render_plan(plan: Plan) -> None:
    banner("Plan")
    for diff in plan.diffs:
        print(str(diff))
    print(
        f"\n{plan.create_count} to create, {plan.update_count} to update, "
        f"{plan.delete_count} to delete, {plan.noop_count} unchanged."
    )


def reconcile(
    spec_path: str,
    client: LiteLLMClient,
    *,
    dry_run: bool = False,
    prune: bool = False,
) -> Plan:
    """Run the convergence loop: spec diff -> apply (unless dry-run).

    The fixed stage order and the org/team member hand-offs live in
    `_stage_diffs` (single source of truth, AGENTS.md §5).
    """
    spec = load_spec(spec_path)
    plan = Plan()

    for title, diffs in _stage_diffs(client, spec, dry_run=dry_run):
        banner(title)
        plan.diffs.extend(diffs)

    return plan


def run(
    spec_path: str,
    *,
    base_url: str,
    api_key: str,
    dry_run: bool = False,
    prune: bool = False,
) -> int:
    info("litellm-as-code", f"target={base_url} spec={spec_path}")
    if dry_run:
        info("litellm-as-code", "DRY-RUN — no changes will be applied")

    client = LiteLLMClient(base_url, api_key)
    plan = reconcile(spec_path, client, dry_run=dry_run, prune=prune)
    _render_plan(plan)

    # exit code semantics like terraform plan/apply diff detection:
    # 0 = no diff / applied cleanly, 2 = plan shows changes (dry-run only).
    # Deletions count as changes (issue #7: member removals are DELETE diffs).
    if dry_run and (plan.create_count + plan.update_count + plan.delete_count):
        return 2
    return 0
