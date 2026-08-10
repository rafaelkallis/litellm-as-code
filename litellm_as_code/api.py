"""Authenticated HTTP client for the LiteLLM admin REST API.

Thin, typed layer over `requests.Session`. Handles:

* Bearer auth + a shared `litellm-changed-by` header
* nested-response unwrapping helpers (proxy nests reads: /key/info -> {info},
  /user/info -> {user_info}, /team/info -> {team_info}, /model/info -> {data})
* error surfacing via ReconcilerError (with the proxy's `error.message`)
* exponential-backoff retries (proxy is eventually consistent after create)
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin, quote

import requests

from .types import ReconcilerError


class LiteLLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        changed_by: str = "litellm-as-code",
        timeout: float = 30.0,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "litellm-changed-by": changed_by,
                "Content-Type": "application/json",
            }
        )
        self.session.trust_env = True
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retry: bool = False,
    ) -> dict[str, Any]:
        url = self._url(path)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(
                    method, url, json=json, params=params, timeout=self.timeout
                )
                if resp.status_code == 404 and retry and attempt < self.max_retries:
                    # eventual consistency: back off and retry read-after-create
                    time.sleep(self.retry_base_delay * (2**attempt))
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as e:
                body = e.response.text if e.response is not None else ""
                detail = " ".join(body.split())[:500]
                raise ReconcilerError(
                    f"{method} {path} failed: {e} {detail}".strip()
                ) from e
            except requests.RequestException as e:
                last_err = e
                if attempt < self.max_retries:
                    time.sleep(self.retry_base_delay * (2**attempt))
                    continue
                raise ReconcilerError(f"{method} {path} failed: {last_err}") from last_err

        raise ReconcilerError(f"{method} {path} failed: {last_err}")

    # -- helpers to unwrap LiteLLM's nested read envelopes ------------------
    @staticmethod
    def unwrap(payload: dict[str, Any], *keys: str) -> dict[str, Any]:
        cur: Any = payload
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return payload
            cur = cur[k]
        return cur if isinstance(cur, dict) else payload

    # -- users --------------------------------------------------------------
    def list_users(self) -> list[dict[str, Any]]:
        return self._request("GET", "/user/list").get("users", [])

    def create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/user/new", json=payload)

    def update_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/user/update", json=payload)

    def delete_user(self, user_id: str) -> dict[str, Any]:
        return self._request("POST", "/user/delete", json={"user_ids": [user_id]})

    # -- teams --------------------------------------------------------------
    def list_teams(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v2/team/list").get("teams", [])

    def create_team(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/team/new", json=payload)

    def update_team(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/team/update", json=payload)

    def delete_team(self, team_id: str) -> dict[str, Any]:
        return self._request("POST", "/team/delete", json={"team_ids": [team_id]})

    def add_team_members(
        self, team_id: str, members: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._request(
            "POST", "/team/member_add", json={"team_id": team_id, "member": members}
        )

    def delete_team_member(
        self, team_id: str, user_id: str | None = None, user_email: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"team_id": team_id}
        if user_id:
            payload["user_id"] = user_id
        if user_email:
            payload["user_email"] = user_email
        return self._request("POST", "/team/member_delete", json=payload)

    # -- virtual keys -------------------------------------------------------
    def list_keys(self) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/key/list", params={"return_full_object": "true"}
        ).get("keys", [])

    def generate_key(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/key/generate", json=payload)

    def update_key(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/key/update", json=payload)

    def delete_key(self, key: str) -> dict[str, Any]:
        return self._request("POST", "/key/delete", json={"keys": [key]})

    # -- credentials --------------------------------------------------------
    def list_credentials(self) -> list[dict[str, Any]]:
        return self._request("GET", "/credentials").get("credentials", [])

    def create_credential(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/credentials", json=payload)

    def patch_credential(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/credentials/{quote(name, safe='')}", json=payload)

    def delete_credential(self, name: str) -> dict[str, Any]:
        return self._request("DELETE", f"/credentials/{quote(name, safe='')}")

    # -- models -------------------------------------------------------------
    def list_models(self) -> list[dict[str, Any]]:
        return self._request("GET", "/model/info").get("data", [])

    def create_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/model/new", json=payload)

    def patch_model(self, model_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/model/{quote(model_id, safe='')}/update", json=payload)

    def delete_model(self, model_id: str) -> dict[str, Any]:
        return self._request("POST", "/model/delete", json={"id": model_id})
