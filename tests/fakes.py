"""Shared test scaffolding: an in-memory fake LiteLLM API.

Mimics the *shape* of the real proxy responses (including the nested read
envelopes the real API returns) so reconcilers can be tested without a live
proxy. Mirrors the "mock-only unit tests" convention used by BerriAI/litellm.
"""

from __future__ import annotations

import json
from typing import Any

from litellm_as_code.api import LiteLLMClient


class FakeLiteLLM:
    """In-memory stand-in for a LiteLLM proxy admin API."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.teams: dict[str, dict[str, Any]] = {}
        self.team_members: dict[str, dict[str, str]] = {}  # (team_id,user_id)->role
        self.keys: dict[str, dict[str, Any]] = {}  # alias -> key obj
        self.credentials: dict[str, dict[str, Any]] = {}  # name -> cred obj
        self._stored_credential_values: dict[str, dict[str, Any]] = {}  # name -> values
        self.models: dict[str, dict[str, Any]] = {}  # model_name -> model obj
        self.budgets: dict[str, dict[str, Any]] = {}  # budget_id -> budget obj
        self.organizations: dict[str, dict[str, Any]] = {}  # org_id -> org obj
        self.org_members: dict[str, dict[str, str]] = {}  # (org_id,user_id)->role
        self.guardrails: dict[str, dict[str, Any]] = {}  # name -> guardrail obj
        self.guardrail_ids: dict[str, str] = {}  # name -> guardrail_id
        self.policies: dict[str, dict[str, Any]] = {}  # name -> policy obj
        self.policy_ids: dict[str, str] = {}  # name -> policy_id

    # -- wire up to LiteLLMClient via monkeypatched session --
    def attach(self, client: LiteLLMClient) -> None:
        # We replace client methods rather than requests: keeps tests honest
        # about which endpoints are called.
        client.list_users = lambda: list(self.users.values())  # type: ignore[method-assign]
        client.create_user = self._create_user  # type: ignore[method-assign]
        client.update_user = self._update_user  # type: ignore[method-assign]

        client.list_teams = lambda: list(self.teams.values())  # type: ignore[method-assign]
        client.create_team = self._create_team  # type: ignore[method-assign]
        client.update_team = self._update_team  # type: ignore[method-assign]
        client.add_team_members = self._add_members  # type: ignore[method-assign]
        client.update_team_member_role = self._update_member_role  # type: ignore[method-assign]
        client.delete_team_member = self._delete_member  # type: ignore[method-assign]
        client._request = self._team_info_raw  # type: ignore[method-assign]

        client.list_keys = lambda: list(self.keys.values())  # type: ignore[method-assign]
        client.generate_key = self._generate_key  # type: ignore[method-assign]
        client.update_key = self._update_key  # type: ignore[method-assign]

        client.list_credentials = lambda: list(self.credentials.values())  # type: ignore[method-assign]
        client.create_credential = self._create_credential  # type: ignore[method-assign]
        client.patch_credential = self._patch_credential  # type: ignore[method-assign]
        client.get_credential_by_name = self._credential_by_name  # type: ignore[method-assign]

        client.list_models = lambda: list(self.models.values())  # type: ignore[method-assign]
        client.create_model = self._create_model  # type: ignore[method-assign]
        client.patch_model = self._patch_model  # type: ignore[method-assign]

        client.list_budgets = lambda: list(self.budgets.values())  # type: ignore[method-assign]
        client.create_budget = self._create_budget  # type: ignore[method-assign]
        client.update_budget = self._update_budget  # type: ignore[method-assign]
        client.delete_budget = self._delete_budget  # type: ignore[method-assign]

        client.list_organizations = lambda: self._organization_list()  # type: ignore[method-assign]
        client.create_organization = self._create_organization  # type: ignore[method-assign]
        client.update_organization = self._update_organization  # type: ignore[method-assign]
        client.delete_organization = self._delete_organization  # type: ignore[method-assign]
        client.add_organization_members = self._add_org_members  # type: ignore[method-assign]
        client.update_organization_member = self._update_org_member  # type: ignore[method-assign]
        client.delete_organization_member = self._delete_org_member  # type: ignore[method-assign]

        client.list_guardrails = lambda: self._list_guardrails()  # type: ignore[method-assign]
        client.create_guardrail = self._create_guardrail  # type: ignore[method-assign]
        client.update_guardrail = self._update_guardrail  # type: ignore[method-assign]
        client.delete_guardrail = self._delete_guardrail  # type: ignore[method-assign]

        client.list_policies = lambda: self._list_policies()  # type: ignore[method-assign]
        client.create_policy = self._create_policy  # type: ignore[method-assign]
        client.update_policy = self._update_policy  # type: ignore[method-assign]
        client.delete_policy = self._delete_policy  # type: ignore[method-assign]

    # -- users --------------------------------------------------------------
    def _create_user(self, payload):  # type: ignore[no-untyped-def]
        self.users[payload["user_id"]] = dict(payload)
        return {"user_id": payload["user_id"]}

    def _update_user(self, payload):  # type: ignore[no-untyped-def]
        self.users[payload["user_id"]].update(payload)
        return {}

    # -- teams --------------------------------------------------------------
    def _create_team(self, payload):  # type: ignore[no-untyped-def]
        # Real LiteLLM accepts alias-only teams and mints a team_id; the
        # reconciler's identity rule matches on team_id if fixed, else
        # team_alias. Mirror that.
        tid = payload.get("team_id") or f"team-{len(self.teams) + 1}"
        obj = dict(payload)
        obj["team_id"] = tid
        self.teams[tid] = obj
        return {"team_id": tid}

    def _update_team(self, payload):  # type: ignore[no-untyped-def]
        # find by team_id if present, else by team_alias
        if "team_id" in payload and payload["team_id"] in self.teams:
            self.teams[payload["team_id"]].update(payload)
            return {}
        for t in self.teams.values():
            if t.get("team_alias") == payload.get("team_alias"):
                t.update(payload)
                return {}
        return {}

    def _add_members(self, team_id, members):  # type: ignore[no-untyped-def]
        for m in members:
            self.team_members[(team_id, m["user_id"])] = m["role"]
        return {}

    def _update_member_role(self, team_id, user_id, *, role):  # type: ignore[no-untyped-def]
        # POST /team/member_update — change an EXISTING member's role.
        # (Real proxy 400s on member_add for an existing member.)
        self.team_members[(team_id, user_id)] = role
        return {}

    def _delete_member(self, team_id, user_id=None, user_email=None):  # type: ignore[no-untyped-def]
        for k in list(self.team_members):
            if k[0] == team_id and user_id and k[1] == user_id:
                del self.team_members[k]
        return {}

    def _team_info_raw(self, method, path, **kwargs):  # type: ignore[no-untyped-def]
        # used by teams.py _team_info; mirrors the real GET /team/info read:
        # members are ONLY members added via member_add (no placeholder
        # default_user_id row, no automatic members from the create payload).
        team_id = kwargs.get("params", {}).get("team_id")
        team = self.teams.get(team_id, {})
        members = [{"user_id": uid, "role": role} for (tid, uid), role in self.team_members.items() if tid == team_id]
        return {"team_info": {**team, "members_with_roles": members}}

    # -- keys ---------------------------------------------------------------
    def _generate_key(self, payload):  # type: ignore[no-untyped-def]
        alias = payload["key_alias"]
        key = payload.get("key") or f"sk-generated-{len(self.keys)}"
        obj = dict(payload, key=key)
        obj.pop("key_type", None)  # api doesn't store key_type
        self.keys[alias] = obj
        return {"key": key}

    def _update_key(self, payload):  # type: ignore[no-untyped-def]
        alias = payload["key_alias"]
        self.keys[alias].update(payload)
        return {}

    # -- credentials --------------------------------------------------------
    def _create_credential(self, payload):  # type: ignore[no-untyped-def]
        name = payload["credential_name"]
        # mimic real API: values stored server-side, never echoed back on read
        self._stored_credential_values[name] = dict(payload.get("credential_values", {}))
        stored = {**payload, "credential_values": {}}
        self.credentials[name] = stored
        return {}

    def _patch_credential(self, name, payload):  # type: ignore[no-untyped-def]
        # If values are re-asserted on the wire, update the stored (server-side)
        # value too; the read path still masks them.
        if payload.get("credential_values"):
            self._stored_credential_values[name] = dict(payload["credential_values"])
        stored = {**payload, "credential_values": {}}
        self.credentials[name].update(stored)
        return {}

    def _credential_by_name(self, name):  # type: ignore[no-untyped-def]
        # GET /credentials/by_name/{name} masks values, like the real API.
        cred = dict(self.credentials.get(name, {}))
        stored = self._stored_credential_values.get(name, {})
        masked = {k: (f"{str(v)[:4]}***" if v else v) for k, v in stored.items()}
        cred["credential_values"] = masked
        return cred

    # -- models -------------------------------------------------------------
    def _create_model(self, payload):  # type: ignore[no-untyped-def]
        name = payload["model_name"]
        # Mimic the real API: store cost in litellm_params per-token, not
        # as per-million in model_info (which is how the spec expresses it).
        self.models[name] = self._normalize_model(payload)
        return {}

    def _patch_model(self, model_id, payload):  # type: ignore[no-untyped-def]
        for m in self.models.values():
            if (m.get("model_info") or {}).get("id") == model_id:
                m.update(self._normalize_model(payload))
                return {}
        return {}

    @staticmethod
    def _normalize_model(payload):  # type: ignore[no-untyped-def]
        p = json.loads(json.dumps(payload))  # deep copy
        mi = p.setdefault("model_info", {})
        lp = p.setdefault("litellm_params", {})
        for key in ("input_cost_per_million_tokens", "output_cost_per_million_tokens"):
            if key in mi:
                v = mi.pop(key)
                lp[f"{key.replace('_per_million_tokens', '')}_per_token"] = v / 1_000_000.0
        return p

    # -- budgets ------------------------------------------------------------
    def _create_budget(self, payload):  # type: ignore[no-untyped-def]
        budget_id = payload["budget_id"]
        obj = dict(payload)
        obj["budget_reset_at"] = "2026-09-01T00:00:00Z" if payload.get("budget_duration") else None
        self.budgets[budget_id] = obj
        return {"budget_id": budget_id}

    def _update_budget(self, payload):  # type: ignore[no-untyped-def]
        budget_id = payload["budget_id"]
        if budget_id in self.budgets:
            self.budgets[budget_id].update(payload)
            # server recomputes reset_at whenever duration changes
            self.budgets[budget_id]["budget_reset_at"] = (
                "2026-09-01T00:00:00Z" if payload.get("budget_duration") else None
            )
        return {}

    def _delete_budget(self, budget_id):  # type: ignore[no-untyped-def]
        self.budgets.pop(budget_id, None)
        return {}

    # -- organizations ------------------------------------------------------
    def _create_organization(self, payload):  # type: ignore[no-untyped-def]
        org_id = payload.get("organization_id") or f"org-{len(self.organizations)}"
        obj = {k: v for k, v in payload.items() if k != "members_with_roles"}
        obj["organization_id"] = org_id
        obj["members"] = []
        self.organizations[org_id] = obj
        return {"organization_id": org_id}

    def _organization_list(self):  # type: ignore[no-untyped-def]
        out = []
        for org_id, org in self.organizations.items():
            entry = dict(org)
            entry["members"] = [
                {"user_id": uid, "user_role": role}
                for (oid, uid), role in self.org_members.items()
                if oid == org_id
            ]
            out.append(entry)
        return out

    def _update_organization(self, payload):  # type: ignore[no-untyped-def]
        org_id = payload.get("organization_id")
        if org_id and org_id in self.organizations:
            self.organizations[org_id].update(payload)
        else:
            for o in self.organizations.values():
                if o.get("organization_alias") == payload.get("organization_alias"):
                    o.update(payload)
                    return {}
        return {}

    def _delete_organization(self, org_id):  # type: ignore[no-untyped-def]
        self.organizations.pop(org_id, None)
        for k in [key for key in self.org_members if key[0] == org_id]:
            del self.org_members[k]
        return {}

    def _add_org_members(self, org_id, members):  # type: ignore[no-untyped-def]
        for m in members:
            self.org_members[(org_id, m["user_id"])] = m["role"]
        return {}

    def _update_org_member(self, org_id, user_id, role=None):  # type: ignore[no-untyped-def]
        if (org_id, user_id) in self.org_members and role is not None:
            self.org_members[(org_id, user_id)] = role
        return {}

    def _delete_org_member(self, org_id, user_id):  # type: ignore[no-untyped-def]
        self.org_members.pop((org_id, user_id), None)
        return {}

    # -- guardrails ----------------------------------------------------------
    def _list_guardrails(self):  # type: ignore[no-untyped-def]
        return [
            {
                "guardrail_id": self.guardrail_ids.get(name),
                "guardrail_name": name,
                "litellm_params": dict(g.get("litellm_params", {})),
                "guardrail_info": g.get("guardrail_info"),
            }
            for name, g in self.guardrails.items()
        ]

    def _create_guardrail(self, payload):  # type: ignore[no-untyped-def]
        name = payload["guardrail_name"]
        gid = f"gr-{len(self.guardrail_ids)}"
        self.guardrails[name] = dict(payload)
        self.guardrail_ids[name] = gid
        return {"guardrail_id": gid}

    def _update_guardrail(self, guardrail_id, payload):  # type: ignore[no-untyped-def]
        for name, gid in self.guardrail_ids.items():
            if gid == guardrail_id:
                # rename updates the name key too
                new_name = payload.get("guardrail_name", name)
                if new_name != name:
                    self.guardrails.pop(name)
                    self.guardrail_ids.pop(name)
                    self.guardrails[new_name] = dict(payload)
                    self.guardrail_ids[new_name] = gid
                else:
                    self.guardrails[name].update(payload)
                return {}
        return {}

    def _delete_guardrail(self, guardrail_id):  # type: ignore[no-untyped-def]
        for name, gid in list(self.guardrail_ids.items()):
            if gid == guardrail_id:
                self.guardrails.pop(name, None)
                self.guardrail_ids.pop(name, None)
                return {}
        return {}

    # -- policies -----------------------------------------------------------
    def _list_policies(self):  # type: ignore[no-untyped-def]
        return [
            {
                "policy_id": self.policy_ids.get(name),
                "policy_name": name,
                "version_status": "production",
                "inherit": p.get("inherit"),
                "description": p.get("description"),
                "guardrails_add": p.get("guardrails_add", []),
                "guardrails_remove": p.get("guardrails_remove", []),
                "definition_location": "db",
            }
            for name, p in self.policies.items()
        ]

    def _create_policy(self, payload):  # type: ignore[no-untyped-def]
        name = payload["policy_name"]
        pid = f"pol-{len(self.policy_ids)}"
        self.policies[name] = dict(payload)
        self.policy_ids[name] = pid
        return {"policy_id": pid, "policy_name": name}

    def _update_policy(self, policy_id, payload):  # type: ignore[no-untyped-def]
        # Mirror the real proxy: PUT only applies to DRAFT versions.
        # Published (production) policies reject updates with the proxy's
        # exact error, forcing the reconciler's recreate path.
        from litellm_as_code.types import ReconcilerError

        for name, pid in self.policy_ids.items():
            if pid == policy_id:
                raise ReconcilerError(
                    "PUT /policies/{id} failed: 400 Client Error: Bad Request for "
                    f"url: /policies/{policy_id} {{\"detail\":\"Only draft versions "
                    "can be updated. Publish or create a new version to change "
                    "published/production.\"}}"
                )

    def _delete_policy(self, policy_id):  # type: ignore[no-untyped-def]
        for name, pid in list(self.policy_ids.items()):
            if pid == policy_id:
                self.policies.pop(name, None)
                self.policy_ids.pop(name, None)
                return {}
        return {}



