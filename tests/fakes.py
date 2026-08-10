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
        self.models: dict[str, dict[str, Any]] = {}  # model_name -> model obj

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
        client.delete_team_member = self._delete_member  # type: ignore[method-assign]
        client._request = self._team_info_raw  # type: ignore[method-assign]

        client.list_keys = lambda: list(self.keys.values())  # type: ignore[method-assign]
        client.generate_key = self._generate_key  # type: ignore[method-assign]
        client.update_key = self._update_key  # type: ignore[method-assign]

        client.list_credentials = lambda: list(self.credentials.values())  # type: ignore[method-assign]
        client.create_credential = self._create_credential  # type: ignore[method-assign]
        client.patch_credential = self._patch_credential  # type: ignore[method-assign]

        client.list_models = lambda: list(self.models.values())  # type: ignore[method-assign]
        client.create_model = self._create_model  # type: ignore[method-assign]
        client.patch_model = self._patch_model  # type: ignore[method-assign]

    # -- users --------------------------------------------------------------
    def _create_user(self, payload):  # type: ignore[no-untyped-def]
        self.users[payload["user_id"]] = dict(payload)
        return {"user_id": payload["user_id"]}

    def _update_user(self, payload):  # type: ignore[no-untyped-def]
        self.users[payload["user_id"]].update(payload)
        return {}

    # -- teams --------------------------------------------------------------
    def _create_team(self, payload):  # type: ignore[no-untyped-def]
        tid = payload["team_id"]
        self.teams[tid] = dict(payload)
        return {}

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

    def _delete_member(self, team_id, user_id=None, user_email=None):  # type: ignore[no-untyped-def]
        for k in list(self.team_members):
            if k[0] == team_id and user_id and k[1] == user_id:
                del self.team_members[k]
        return {}

    def _team_info_raw(self, method, path, **kwargs):  # type: ignore[no-untyped-def]
        # used by teams.py _team_info
        team_id = kwargs.get("params", {}).get("team_id")
        team = self.teams.get(team_id, {})
        members = [
            {"user_id": uid, "role": role}
            for (tid, uid), role in self.team_members.items()
            if tid == team_id
        ]
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
        # mimic real API: values NOT stored (never read back)
        stored = {**payload, "credential_values": {}}
        self.credentials[name] = stored
        return {}

    def _patch_credential(self, name, payload):  # type: ignore[no-untyped-def]
        stored = {**payload, "credential_values": {}}
        self.credentials[name].update(stored)
        return {}

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



