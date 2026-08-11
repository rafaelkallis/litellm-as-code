"""The reconciler: spec + live API -> plan -> (apply)."""

from __future__ import annotations

from typing import Any

from .api import LiteLLMClient
from .log import banner, info
from .resources import (
    reconcile_credentials,
    reconcile_keys,
    reconcile_models,
    reconcile_team_members,
    reconcile_teams,
    reconcile_users,
)
from .spec import load_spec
from .types import Plan


def _render_plan(plan: Plan) -> None:
    banner("Plan")
    for diff in plan.diffs:
        print(str(diff))
    print(
        f"\n{plan.create_count} to create, {plan.update_count} to update, "
        f"{plan.noop_count} unchanged."
    )


def reconcile(
    spec_path: str,
    client: LiteLLMClient,
    *,
    dry_run: bool = False,
    prune: bool = False,
) -> Plan:
    """Run the convergence loop: spec diff -> apply (unless dry-run).

    Ordering is fixed: users -> teams -> team members -> keys -> credentials
    -> models. This is acyclic for a single proxy, so no graph solver is needed.
    """
    spec = load_spec(spec_path)
    plan = Plan()

    def extend(diffs):
        plan.diffs.extend(diffs)

    banner("Users")
    extend(reconcile_users(client, spec.get("users", []), dry_run=dry_run))

    banner("Teams")
    team_diffs, team_specs = reconcile_teams(
        client, spec.get("teams", []), dry_run=dry_run
    )
    extend(team_diffs)
    extend(
        reconcile_team_members(client, team_specs, dry_run=dry_run)
    )

    banner("Virtual keys")
    extend(reconcile_keys(client, spec.get("virtual_keys", []), dry_run=dry_run))

    banner("Credentials")
    extend(reconcile_credentials(client, spec.get("credentials", []), dry_run=dry_run))

    banner("Models")
    extend(reconcile_models(client, spec.get("models", []), dry_run=dry_run))

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
    # 0 = no diff / applied cleanly, 2 = plan shows changes (dry-run only)
    if dry_run and (plan.create_count + plan.update_count):
        return 2
    return 0
