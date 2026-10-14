#!/usr/bin/env python3
"""Record scrubbed per-version response fixtures for the mock suite (issue #28).

WHY THIS EXISTS (AGENTS.md §9): every compat lesson since August — nested read
envelopes, masked `credential_values`, absent key tokens, the `size`-vs-
`page_size` pagination quirk, injected `default_user_id` — was learned live and
then re-encoded BY HAND into `tests/fakes.py`. This module records the real
proxy's read responses over a CONVERGED variant-E state (the version-matrix
baseline, `tests/live/spec-variant-e.yml`), scrubs them, and commits one
self-describing fixture bundle per matrix version. The mock suite replays the
bytes through the REAL client/exporter/reconciler (`FixtureClient` below), so
upstream envelope/schema/masking drift fails plain `pytest` CI without docker.

Pipeline (one green compat-matrix leg, or `main()` against a converged stack):

    sweep (GET-only, mirrors api.py's own paths/params)
      -> double-sweep determinism gate (byte-equal raw payloads)
      -> scrub (secret policy + deterministic syntheticization)
      -> posture ledger (masking/key-absence invariants, see _posture_checks)
      -> normalized + exported-spec derivation (through the real client code)
      -> one directory of JSON files under tests/fixtures/responses/<tag>/

Committed bundle layout::

    <tag>/_meta.json                       # version, posture ledger, counts
    <tag>/<step>.raw.json                  # the response envelope as served
    <tag>/<step>.normalized.json           # what LiteLLMClient.list_<step> returns
    <tag>/exported_spec.json               # exporter.build_spec output (reference)

Security posture (this is the point of the fixtures, not incidental):
- **Plaintext secret material never enters git.** A string under a sensitive
  key (``secrets.is_sensitive_key``) is dropped unless it is empty or a
  server masker echo — and the drop is ledgered.
- **Masker-shaped echoes are KEPT** on purpose: they *are* the security
  posture we depend on. `credentials.raw.json` must contain masked values and
  every `keys.raw.json` row must lack a live `key` field; if a future proxy
  version stops masking or starts echoing key material, the posture checks
  fail the capture — the nightly goes red, fixtures are never written, and
  users never see plaintext (issue #28).
- **Deterministic syntheticization**: server-minted uuids become
  ``fixture-uuid-NNN`` labels and emails become ``scrub-N@example.com`` via
  one shared first-seen map, so cross-file references (credential `model_id`
  <-> `model_info.id`, `team_id` in rows vs `/team/info` query params) stay
  consistent while no real identifier survives.
- **Determinism gate**: the sweep runs TWICE; raw payloads must be byte-ident
  (canonical-compare, with a dotted path in the error). Bytes that legitimately
  change between reads must be handled explicitly, not silently committed.

The org gate is a RECORDING dimension, not a failure: on an unlicensed
>= 1.102 proxy, `GET /organization/list` 403s with the Enterprise-gate body;
the gate body is recorded verbatim into `organizations.raw.json` (guarded by
`capabilities.is_org_gate_error` — the one canonical marker check) and the
replay raises the equivalent error so the REAL gate-tolerance code is what the
mock suite exercises. On an ungated proxy the empty-array envelope shape is
recorded instead (v1.97.0).

CLI example (the harness wires this automatically under `--record-fixtures`)::

    .venv/bin/python scripts/record_fixtures.py \
        --base-url http://localhost:4001 --api-key sk-tests-master-key-change-me \
        --litellm-tag v1.97.0 --out tests/fixtures/responses
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # editable-install agnostic import of the package
    sys.path.insert(0, str(REPO_ROOT))

from litellm_as_code.api import LiteLLMClient  # noqa: E402  (after sys.path fix)
from litellm_as_code.capabilities import is_org_gate_error  # noqa: E402
from litellm_as_code.exporter import build_spec  # noqa: E402
from litellm_as_code.secrets import is_masked_value, is_sensitive_key  # noqa: E402
from litellm_as_code.types import ReconcilerError  # noqa: E402

# Bundle schema version — bump when the on-disk layout or _meta.json contract
# changes (the replay suite's moral contract is the bundle, never its bytes).
FIXTURE_VERSION = 1

# The consumed read surface, in the reconciler's fixed stage order (AGENTS.md
# §3). Names are the on-disk file stems; the actual requests are made by the
# REAL client methods in `_sweep_one_pass` — this table is the file layout and
# the deterministic walk order, never a hand-copied URL (api.py stays the only
# place that knows the paths, AGENTS.md §7).
CAPTURE_STEPS: tuple[str, ...] = (
    "budgets",
    "models",
    "credentials",
    "organizations",
    "users",
    "teams",
    "team-info",
    "keys",
    "guardrails",
    "policies",
)

# Deterministic row ordering per step (post-scrub, stable across boots).
_STEP_SORT_KEY: dict[str, str] = {
    "budgets": "budget_id",
    "models": "model_name",
    "credentials": "credential_name",
    "organizations": "organization_id",
    "users": "user_id",
    "teams": "team_id",
    "keys": "key_alias",
    "guardrails": "guardrail_name",
    "policies": "policy_name",
}

# Envelope list keys (for row extraction/sorting); budgets/organizations are
# bare arrays on current proxies, so they need no unwrapping here.
_STEP_LIST_KEY: dict[str, str | None] = {
    "budgets": None,
    "models": "data",
    "credentials": "credentials",
    "organizations": None,
    "users": "users",
    "teams": "teams",
    "team-info": None,  # dict keyed by team_id
    "keys": "keys",
    "guardrails": "guardrails",
    "policies": "policies",
}

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Rising trust-off shape: any committed bare bearer-looking token (long unmasked
# sk-...) is a leak — masked echoes ("sk****") do not match because of the
# asterisks.
_SK_TOKEN_RE = re.compile(r"^sk-[A-Za-z0-9]{12,}$")


class CaptureError(RuntimeError):
    """A capture-sweep problem that must red the matrix leg loudly."""


# ---------------------------------------------------------------------------
# small formatting helpers


def canonical_json(data: Any) -> str:
    """Deterministic serialization (sorted keys, stable indent, final newline)."""
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _first_recording(
    recordings: list[dict[str, Any]], path: str
) -> dict[str, Any]:
    """First recorded payload for a path (page-1 is the truth we commit)."""
    for rec in recordings:
        if rec["path"] == path:
            return rec
    raise CaptureError(
        f"fixture capture: no recorded response for GET {path} — the sweep "
        "never reached a consumed endpoint; check api.py's read surface"
    )


def _make_response(
    status: int, payload: Any, url: str
) -> "requests.Response":  # noqa: F821 (import inside test-only helper)
    """A real requests.Response built for replay tests (unit-test helper).

    Uses the real `requests` class (not a duck-type) so super()._request's
    raise_for_status/json/text semantics are exercised exactly as in prod.
    """
    import requests  # local import: keeps live capture importable on stdlib-only boxes

    resp = requests.Response()
    resp.status_code = status
    resp.url = url
    body = payload if isinstance(payload, (bytes, str)) else canonical_json(payload)
    resp._content = body.encode("utf-8") if isinstance(body, str) else body
    resp.headers["content-type"] = "application/json"
    return resp


class _StaticSession:
    """Stands in for requests.Session during unit tests.

    `transport(method, url, params) -> (status, payload|body-str)` mirrors raw
    HTTP; everything above it (raise_for_status, ReconcilerError formatting,
    json unwrapping) is REAL production code.
    """

    def __init__(self, transport: Callable[[str, str, dict[str, Any]], tuple[int, Any]]):
        self._transport = transport

    def request(self, method: str, url: str, json: Any = None, params: Any = None, timeout: Any = None):  # noqa: E501
        status, payload = self._transport(method, url, dict(params or {}))
        return _make_response(status, payload, url)


# ---------------------------------------------------------------------------
# transport-mirroring sweep client


class SweepClient(LiteLLMClient):
    """A LiteLLMClient that also records every raw response payload.

    All HTTP semantics (auth headers, raise_for_status, the 403-gate body,
    eventual-consistency retries) come from the production `_request`; the
    override only adds the recording hook. Read-only by convention: the sweep
    driver must only call list_*/get_* methods.
    """

    def __init__(
        self,
        transport: Callable[[str, str, dict[str, Any]], tuple[int, Any]] | None = None,
        base_url: str = "http://fixture.invalid",
        api_key: str = "sk-fixture-master-key-not-real",
    ):
        super().__init__(base_url, api_key)
        if transport is not None:
            self.session = _StaticSession(transport)
        self._recorded: list[dict[str, Any]] = []

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,  # noqa: A002 (parity with parent)
        params: dict[str, Any] | None = None,
        retry: bool = False,
    ) -> dict[str, Any]:
        if method != "GET":
            raise CaptureError(
                f"fixture capture is GET-only; refusing {method} {path}"
            )
        payload = super()._request(method, path, json=json, params=params, retry=retry)
        self._recorded.append(
            {"method": method, "path": path, "params": dict(params or {}), "payload": payload}
        )
        return payload


# ---------------------------------------------------------------------------
# the sweep (one pass over the consumed read surface)


def _gate_detail(exc: ReconcilerError) -> str:
    """Extract the proxy's error body from the gate ReconcilerError text.

    `_request` formats: f"{method} {path} failed: {requests_http_error}
    {proxy_body_text}". We keep only the last {...} group (the proxy's JSON
    body) — it carries the gate markers ("403", "enterprise") WITHOUT the
    stack URL, so committed bytes stay stable across ports/hosts.
    """
    text = str(exc)
    matches = list(re.finditer(r"\{.*\}", text, re.S))
    if matches:
        return matches[-1].group(0)  # keep the LAST braces group = proxy JSON body
    return text.split("failed:", 1)[-1].strip()


def _sweep_one_pass(client: SweepClient) -> tuple[dict[str, Any], str]:
    """Drive the read surface through the real client; return raw payloads.

    Call order: fixed stage order (AGENTS.md §3), team-info after team listing
    (paths mirror the exporter), /openapi.json last. The org gate is caught
    via `is_org_gate_error` (the canonical marker check, capabilities single
    home) and recorded instead of failing.
    """
    client.list_budgets()
    client.list_models()
    client.list_credentials()

    org_gate: dict[str, Any] | None = None
    try:
        client.list_organizations()
    except ReconcilerError as exc:
        if not is_org_gate_error(exc):
            raise
        org_gate = {
            "fixture_gate_error": True,
            "status": 403,
            "detail": _gate_detail(exc),
        }

    client.list_users()
    teams = client.list_teams()
    team_infos: dict[str, Any] = {}
    for team in teams:
        team_id = str(team.get("team_id", ""))
        if team_id:
            team_infos[team_id] = client.get_team_info(team_id)
    client.list_keys()
    client.list_guardrails()
    client.list_policies()
    openapi = client._request("GET", "/openapi.json")
    version = str(openapi.get("info", {}).get("version", ""))

    recordings = client._recorded
    steps: dict[str, Any] = {
        step: _first_recording(recordings, _step_path(step))["payload"]
        for step in CAPTURE_STEPS
        if step not in ("team-info",)
        and not (step == "organizations" and org_gate is not None)
    }
    steps["team-info"] = {}
    for rec in recordings:
        if rec["path"] == "/team/info":
            team_id = str(rec["params"].get("team_id", ""))
            steps["team-info"][team_id] = rec["payload"]
    if org_gate is not None:
        # the gate 403 never reaches the recording hook (it raises); the
        # extracted proxy body IS the recorded payload for this step
        steps["organizations"] = org_gate
    if not version:
        raise CaptureError(
            "fixture capture: GET /openapi.json served no info.version — "
            "fixtures need the version pin in _meta.json"
        )
    return steps, version


def _step_path(step: str) -> str:
    """The api.py endpoint each step's FIRST payload comes from.

    This mirrors api.py's read surface only for RAW-payload extraction; the
    requests themselves were made by the real client methods above (api.py
    stays the single source of endpoint truth, AGENTS.md §7).
    """
    return {
        "budgets": "/budget/list",
        "models": "/model/info",
        "credentials": "/credentials",
        "organizations": "/organization/list",
        "users": "/user/list",
        "teams": "/v2/team/list",
        "keys": "/key/list",
        "guardrails": "/v2/guardrails/list",
        "policies": "/policies/list",
    }[step]


# ---------------------------------------------------------------------------
# determinism gate


def _first_difference(left: Any, right: Any, path: str) -> str | None:
    """Dotted path of the first difference between two payload trees, or None."""
    if type(left) is not type(right):
        return path
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                return f"{path}.{key}"
            found = _first_difference(left[key], right[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path}[len={len(left)} vs {len(right)}]"
        for index, (l_item, r_item) in enumerate(zip(left, right)):
            found = _first_difference(l_item, r_item, f"{path}[{index}]")
            if found:
                return found
        return None
    return None if left == right else path


# ---------------------------------------------------------------------------
# scrub: secret policy + deterministic syntheticization


class _ScrubState:
    """Shared syntheticization map + scrub event ledger for one capture run.

    One map across ALL steps keeps cross-file references consistent: the same
    server uuid maps to the same `fixture-uuid-NNN` label in `teams.raw.json`,
    the `team-info` map keys, and any `user_id`/`team_id` reference elsewhere
    (and the replay dispatches `/team/info?team_id=<label>` accordingly).
    """

    def __init__(self) -> None:
        self.uuid_map: dict[str, str] = {}
        self.email_map: dict[str, str] = {}
        self.masked_paths: list[str] = []
        self.dropped_paths: list[str] = []
        self.benign_numeric_paths: list[str] = []

    def uuid_label(self, value: str) -> str:
        if value not in self.uuid_map:
            self.uuid_map[value] = f"fixture-uuid-{len(self.uuid_map) + 1:04d}"
        return self.uuid_map[value]

    def email_label(self, value: str) -> str:
        if value not in self.email_map:
            self.email_map[value] = f"scrub-{len(self.email_map) + 1}@example.com"
        return self.email_map[value]


# Benign `secrets.is_sensitive_key` keyword collisions: these field names
# contain a sensitive keyword but carry comparable/config material the replay
# NEEDS intact (identity resolution, currency conversion). Exact match only —
# plus simple suffix rules for the token-cost family. Anything NOT in this
# list under a sensitive key is treated as material and dropped.
_SCRUB_KEY_EXEMPT = frozenset(
    {
        "credential_name",
        "litellm_credential_name",
        "key_alias",
        "key_format",
        "auto_create_key",
        "key_rate_limit_update_time",
        "key_rotation_at",
        "token_expiry",
        "token_epoch",
        "token_refresh_at",
    }
)
_SCRUB_KEY_SUFFIX_EXEMPT = (
    "_cost_per_token",
    "_tokens",
)


def _scrub_key_is_material(key: str) -> bool:
    """True when a sensitive-keyed leaf under `key` is treated as secret."""
    return key not in _SCRUB_KEY_EXEMPT and not key.endswith(
        _SCRUB_KEY_SUFFIX_EXEMPT
    )


def _scrub_leaf(value: Any, key: str, path: str, state: _ScrubState) -> Any:
    """Syntheticize a SURVIVING string leaf (uuid -> label, email -> label)."""
    _ = key, path  # labels carry no per-field context by design (shared map)
    if isinstance(value, str):
        if _UUID_RE.fullmatch(value):
            return state.uuid_label(value)
        if _EMAIL_RE.fullmatch(value):
            return state.email_label(value)
    return value


def _scrub(value: Any, key: str, path: str, state: _ScrubState) -> Any:
    """Recursively project a payload under the fixture scrub policy.

    Policy (documented in the module docstring):
    - dict/list: recurse; KEYS are scanned for uuid/email shapes too (a
      `model_max_budget` map keyed by deployment id must stay referenceable).
    - leaf under a sensitive key ('secrets.is_sensitive_key'):
      * None / "" : kept (no material)
      * masker-echo shape ('secrets.is_masked_value'): KEPT + ledgered — the
        posture this fixture set exists to pin.
      * non-numeric str (incl. uuid/email shapes): DROPPED + ledgered.
      * int/float/bool: kept + ledgered as benign-numeric (timestamps, counts
        under keys like `token_expiry` are configuration, not secret material).
    - leaf under a benign key: kept, subject to uuid/email syntheticization.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k in sorted(value, key=str):
            k_path = f"{path}.{k}" if path else str(k)
            # scan the KEY itself for uuid/email shapes (map keys carry ids,
            # e.g. a `model_max_budget` map keyed by deployment id)
            child_key: Any = k
            if isinstance(k, str):
                if _UUID_RE.fullmatch(k):
                    child_key = state.uuid_label(k)
                elif _EMAIL_RE.fullmatch(k):
                    child_key = state.email_label(k)
            out[str(child_key)] = _scrub(value[k], k, k_path, state)
        return out
    if isinstance(value, list):
        return [
            _scrub(item, key, f"{path}[{i}]", state) for i, item in enumerate(value)
        ]
    # leaf
    if isinstance(value, str) and is_masked_value(value):
        # masker echoes are posture evidence regardless of key name (values,
        # not key words, carry the masking contract) - keep + ledger
        state.masked_paths.append(path)
        return value
    if is_sensitive_key(key) and _scrub_key_is_material(key):
        # sensitive-keyed values are judged by VALUE shape and decided here
        # FIRST - a dropped value never enters the syntheticization maps
        # (letting it could leak the raw identifier into the committed map).
        if value is None or value == "":
            return value
        if is_masked_value(value):
            state.masked_paths.append(path)
            return value  # the echo IS the posture evidence
        if isinstance(value, (int, float, bool)):
            state.benign_numeric_paths.append(path)
            return value  # configuration (timestamps/counts), not material
        state.dropped_paths.append(path)
        return None
    return _scrub_leaf(value, key, path, state)


def _scrub_steps(steps: dict[str, Any]) -> tuple[dict[str, Any], _ScrubState]:
    state = _ScrubState()
    scrubbed = {
        step: _scrub(steps[step], "", step, state) for step in CAPTURE_STEPS
    }
    # a dropped leaf leaves a None hole; popping is right for secrets (the
    # posture ledger carries the dropped PATHS for review) except inside
    # lists, where index alignment matters and None slots keep it.
    return scrubbed, state


# ---------------------------------------------------------------------------
# posture ledger


def _rows(step_payload: dict[str, Any], step: str) -> list[dict[str, Any]]:
    """Extract the row list from a step payload (list columns for sorting)."""
    list_key = _STEP_LIST_KEY.get(step)
    rows = step_payload if list_key is None else step_payload.get(list_key, [])
    return [row for row in rows if isinstance(row, dict)]


def _sort_rows(step_payload: dict[str, Any], step: str) -> dict[str, Any]:
    """Deterministic row order (post-scrub, key = _STEP_SORT_KEY)."""
    sort_key = _STEP_SORT_KEY.get(step)
    if not sort_key:
        return step_payload  # team-info is a map keyed by team_id (JSON sorts it)
    rows = _rows(step_payload, step)
    ordered = sorted(rows, key=lambda row: str(row.get(sort_key, "")))

    def replace(row_list: list[Any]) -> None:
        row_list[:] = ordered

    list_key = _STEP_LIST_KEY.get(step)
    if list_key is None:
        if isinstance(step_payload, list):
            replace(step_payload)
    else:
        if isinstance(step_payload, dict) and list_key in step_payload:
            row_list = step_payload[list_key]
            replace(row_list)
    return step_payload


def _walk_leaf_strings(
    value: Any, key: str, path: str, callback: Callable[[str, str, str], None]
) -> None:
    """Invoke callback(path, string, key_name) per string leaf and per dict key.

    For dict KEYS the callback gets key_name="__key__" so the scanner can (a)
    run ONLY shape checks on key strings and (b) judge leaf values by their
    ACTUAL enclosing field name, not a path-parse hack.
    """
    if isinstance(value, dict):
        for k in sorted(value, key=str):
            child_path = f"{path}.{k}" if path else str(k)
            if isinstance(k, str):
                callback(child_path, k, "__key__")
            _walk_leaf_strings(value[k], k, child_path, callback)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _walk_leaf_strings(item, key, f"{path}[{i}]", callback)
        return
    if isinstance(value, str):
        callback(path, value, key)


def _posture_checks(
    raw_steps: dict[str, Any],
    scrubbed_steps: dict[str, Any],
    state: _ScrubState,
) -> dict[str, Any]:
    """Assert the security postures the fixture set exists to pin (issue #28).

    Every check below guards a real contract with the proxy's READ surface:
    - credential_values must come back masked (issue #11: never plaintext);
    - key rows must not echo a live `key` (write-once, never read back);
    - nothing may survive the scrub as a bare handleable token shape.
    The first two judge the RAW payload (a scrub-dropped plaintext secret must
    still fail loudly — dropping is not the same as not observing posture);
    the leak scan judges the scrubbed output. A future proxy that stops
    masking/starts echoing key material fails the capture loudly (nightly
    red, no fixtures committed) instead of silently locking plaintext into
    git or silently hiding lost masking.
    """
    violated: list[str] = []

    # -- credentials: masked values must exist (judged on RAW) ----------------
    cred_rows = _rows(raw_steps["credentials"], "credentials")
    masked = any(
        any(
            is_masked_value(v)
            for v in (row.get("credential_values") or {}).values()
            if isinstance(v, str)
        )
        for row in cred_rows
    )
    if not masked:
        violated.append(
            "posture: no masked value in credentials.credential_values — the proxy "
            "stopped masking credential material (issue #11 contract); refusing to "
            "record fixtures"
        )

    # -- keys: no live key field on read rows (judged on RAW) -----------------
    key_rows = _rows(raw_steps["keys"], "keys")
    for index, row in enumerate(key_rows):
        live_key = row.get("key")
        if live_key not in (None, ""):
            violated.append(
                f"posture: keys[{index}].key is present on a READ response — the "
                "proxy started echoing key material (aggressive write-once leak); "
                "refusing to record fixtures"
            )

    # -- per-leaf scan: sensitive keys must not carry plaintext (scrubbed) -----
    labels = set(state.uuid_map.values()) | set(state.email_map.values())

    def scan(path: str, value: str, key_name: str) -> None:
        if not value:
            return
        # shape checks apply to BOTH dict keys and leaf values; the
        # sensitive-key checks below apply ONLY to leaf values (identity
        # field NAMES are not secrets and collide with the keyword heurists).
        if _SK_TOKEN_RE.fullmatch(value):
            violated.append(f"posture: bare sk-shaped token survived at {path}")
        if value in labels:
            return  # issued labels are deliberate shapes, not leaks
        if _UUID_RE.fullmatch(value) or _EMAIL_RE.fullmatch(value):
            violated.append(f"posture: unpinned identifier survived at {path}")
        if key_name == "__key__":
            return
        if is_sensitive_key(key_name) and _scrub_key_is_material(key_name):
            if not is_masked_value(value):
                violated.append(
                    f"posture: plaintext-looking value under sensitive key at {path}"
                )

    for step in CAPTURE_STEPS:
        _walk_leaf_strings(scrubbed_steps[step], "", step, scan)

    if violated:
        raise CaptureError("; ".join(violated))

    return {
        "credential_values_masked": True,
        "key_field_absent_on_key_rows": True,
        "masked_value_paths": sorted(set(state.masked_paths)),
        "dropped_sensitive_paths": sorted(set(state.dropped_paths)),
        "benign_numeric_sensitive_paths": sorted(set(state.benign_numeric_paths)),
    }


# ---------------------------------------------------------------------------
# replay transport (used by BOTH capture-time derivation and the mock suite)


class FixtureClient(LiteLLMClient):
    """LiteLLMClient whose transport serves recorded fixture payloads.

    Overrides ONLY `_request` (dispatch) and `proxy_version` (:parsed from
    the fixture's `_meta.json`); every list_*/get_*/_paginated_list/unwrap
    behavior above that is REAL production code, which is exactly what the
    replay tests pin: parser contract, export contract and the converged
    state's no-op reconcile all run against recorded upstream bytes.

    Non-GET calls raise and count (`writes`) — the no-op reconcile test proves
    the recorded converged state needs zero mutations.
    """

    def __init__(self) -> None:
        super().__init__("http://fixture.invalid", "sk-fixture-not-real")
        self._steps: dict[str, Any] = {}
        self._version = ""
        self.writes = 0

    # -- construction --------------------------------------------------------
    @classmethod
    def from_payloads(
        cls, steps: dict[str, Any], proxy_version: str
    ) -> "FixtureClient":
        client = cls()
        client._steps = deepcopy(steps)
        client._version = proxy_version
        return client

    @classmethod
    def from_dir(cls, fixture_dir: Path) -> "FixtureClient":
        """Load the on-disk bundle (tests/fixtures/responses/<tag>)."""
        client = cls()
        meta = json.loads((fixture_dir / "_meta.json").read_text(encoding="utf-8"))
        client._version = str(meta.get("proxy_openapi_version", ""))
        for step in CAPTURE_STEPS:
            raw_path = fixture_dir / f"{step}.raw.json"
            if raw_path.exists():
                client._steps[step] = json.loads(raw_path.read_text(encoding="utf-8"))
        return client

    # -- capability probe ----------------------------------------------------
    def proxy_version(self) -> tuple[int, ...] | None:
        raw = self._version.strip().lstrip("v")
        digits: list[int] = []
        for part in raw.split("."):
            prefix = ""
            for ch in part:
                if ch.isdigit():
                    prefix += ch
                else:
                    break
            if prefix:
                digits.append(int(prefix))
            else:
                break
        return tuple(digits) or None

    # -- transport -----------------------------------------------------------
    def _request(  # noqa: C901 (dispatch table, intentionally flat)
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,  # noqa: A002
        params: dict[str, Any] | None = None,
        retry: bool = False,
    ) -> dict[str, Any]:
        if method != "GET":
            self.writes += 1
            raise ReconcilerError(
                f"fixture replay: only GET is served by recorded fixtures "
                f"(got {method} {path}) — replaying the converged state must "
                "require no mutations"
            )
        paramscfg = dict(params or {})
        table = self._steps

        if path == "/openapi.json":
            return {"info": {"version": self._version}}
        if path == "/budget/list":
            return table.get("budgets", [])
        if path == "/model/info":
            return table.get("models", {})
        if path == "/credentials":
            return table.get("credentials", {})
        if path == "/organization/list":
            payload = table.get("organizations", [])
            if isinstance(payload, dict) and payload.get("fixture_gate_error"):
                raise ReconcilerError(
                    f"GET {path} failed: 403 Client Error: {payload.get('detail', '')}"
                )
            return payload
        if path == "/user/list":
            return self._paged(table.get("users", {}), "users", paramscfg)
        if path == "/v2/team/list":
            return self._paged(table.get("teams", {}), "teams", paramscfg)
        if path == "/team/info":
            team_id = str(paramscfg.get("team_id", ""))
            team_infos = table.get("team-info", {}) or {}
            if team_id not in team_infos:
                raise ReconcilerError(
                    f"GET {path} failed: fixture has no replay payload for "
                    f"team_id={team_id!r}"
                )
            return team_infos[team_id]
        if path == "/key/list":
            return self._paged(table.get("keys", {}), "keys", paramscfg)
        if path == "/v2/guardrails/list":
            return table.get("guardrails", {})
        if path == "/policies/list":
            return table.get("policies", {})
        raise ReconcilerError(
            f"fixture replay: no recorded payload for GET {path} — extend "
            "_sweep_one_pass/FixtureClient together when api.py adds a read"
        )

    @staticmethod
    def _paged(
        payload: Any, list_key: str, params: dict[str, Any]
    ) -> Any:
        """Feed the real pagination loop its recorded page(s).

        Variant-E state comfortably fits page 1, so page > 1 is synthesized as
        an empty page (the loop terminates on either `total_pages` or the
        empty page — identical semantics, no recorded page-2 noise).
        """
        page = params.get("page", 1)
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 1
        if page > 1:
            return {**payload, list_key: []}
        return payload


# ---------------------------------------------------------------------------
# capture orchestration


def normalize_steps(
    client: FixtureClient, steps: dict[str, Any]
) -> dict[str, Any]:
    """Derive the committed `.normalized.json` views through the REAL client
    methods (the same code the mock suite replays; used at capture time and
    by the replay tests).

    An org-gate payload records as a fixture dimension instead of failing the
    capture; any OTHER read failure is a capture bug.
    """
    normalized: dict[str, Any] = {}
    for step, reader in (
        ("budgets", "list_budgets"),
        ("models", "list_models"),
        ("credentials", "list_credentials"),
        ("organizations", "list_organizations"),
        ("users", "list_users"),
        ("teams", "list_teams"),
        ("keys", "list_keys"),
        ("guardrails", "list_guardrails"),
        ("policies", "list_policies"),
    ):
        try:
            normalized[step] = getattr(client, reader)()
        except ReconcilerError as exc:
            if step == "organizations" and steps["organizations"].get(
                "fixture_gate_error"
            ):
                normalized[step] = {"fixture_gate_error": True}
            else:
                raise CaptureError(
                    f"fixture capture: replay of {step} read failed: {exc}"
                ) from exc
    # team-info normalized: unwrap each recorded envelope like get_team_info
    normalized["team-info"] = {
        team_id: LiteLLMClient.unwrap(payload, "team_info")
        for team_id, payload in steps["team-info"].items()
    }
    return normalized


def run_capture(
    transport: Callable[[str, str, dict[str, Any]], tuple[int, Any]] | None,
    litellm_tag: str,
    spec_variant: str = "variant-e",
    base_url: str = "http://fixture.invalid",
    api_key: str = "sk-fixture-master-key-not-real",
) -> dict[str, Any]:
    """Capture + scrub + derive + posture-check; returns files/meta bundle.

    `transport=None` performs the real HTTP sweep against `base_url` (the
    compat-matrix harness path); unit tests inject a canned transport. The
    double-sweep determinism gate runs here — never below it.
    """
    sweep1, version1 = _sweep_one_pass(SweepClient(transport, base_url, api_key))
    sweep2, version2 = _sweep_one_pass(SweepClient(transport, base_url, api_key))
    if version1 != version2:
        raise CaptureError(
            f"fixture capture: /openapi.json version changed between sweeps "
            f"({version1!r} vs {version2!r})"
        )
    diff = _first_difference(sweep1, sweep2, "")
    if diff:
        raise CaptureError(
            "fixture capture: read payloads are not deterministic between "
            f"sweeps (first volatile path: {diff}) — handle the volatile "
            "field explicitly in the scrub policy instead of committing noise"
        )

    # sort FIRST (deterministic row order by _STEP_SORT_KEY), then scrub: the
    # posture ledger paths must index into the committed files exactly as
    # reviewed, without a post-index shift from row reordering.
    sorted_sweep1 = {
        step: _sort_rows(copy.deepcopy(sweep1[step]), step)
        for step in CAPTURE_STEPS
    }
    steps, state = _scrub_steps(sorted_sweep1)
    posture = _posture_checks(sweep1, steps, state)

    client = FixtureClient.from_payloads(steps, version1)
    normalized = normalize_steps(client, steps)

    exported = build_spec(client)

    org_payload = steps.get("organizations", [])
    org_gate_recorded = isinstance(org_payload, dict) and bool(
        org_payload.get("fixture_gate_error")
    )
    meta: dict[str, Any] = {
        "harness": "record_fixtures",
        "fixture_version": FIXTURE_VERSION,
        "litellm_tag": litellm_tag,
        "proxy_openapi_version": version1,
        "spec_variant": spec_variant,
        "org_gate_recorded": org_gate_recorded,
        "syntheticized": {
            # counts + issued labels only: the ORIGINAL values behind the
            # labels are deliberately NOT committed (uuid_map/email_map stay
            # capture-internal - key tokens in particular are live handles
            # against the proxy, exactly the material this module keeps out
            # of git).
            "uuids": len(state.uuid_map),
            "emails": len(state.email_map),
            "issued": sorted(
                list(state.uuid_map.values()) + list(state.email_map.values())
            ),
        },
        "posture": posture,
    }

    files: dict[str, str] = {
        "_meta.json": canonical_json(meta),
        "exported_spec.json": canonical_json(exported),
    }
    for step in CAPTURE_STEPS:
        files[f"{step}.raw.json"] = canonical_json(steps[step])
        files[f"{step}.normalized.json"] = canonical_json(normalized[step])
    return {"meta": meta, "files": files, "steps": steps, "exported": exported}


def capture_from_stack(
    *,
    port: int,
    master_key: str,
    litellm_tag: str,
    out_dir: Path,
    spec_variant: str = "variant-e",
) -> Path:
    """Read-and-record from a healthy, CONVERGED stack; write one bundle dir.

    Callers (compat_matrix.py) guarantee the stack is healthy and green. This
    function performs NO writes against the proxy — the state is expected to
    be the converged variant-E baseline (see tests/live/README.md).
    """
    base_url = f"http://localhost:{port}"
    bundle = run_capture(
        transport=None,  # real HTTP via the production client
        litellm_tag=litellm_tag,
        spec_variant=spec_variant,
        base_url=base_url,
        api_key=master_key,
    )
    fixture_dir = out_dir / litellm_tag
    fixture_dir.mkdir(parents=True, exist_ok=True)
    for name, text in bundle["files"].items():
        (fixture_dir / name).write_text(text, encoding="utf-8")
    return fixture_dir


# ---------------------------------------------------------------------------
# CLI (manual capture against a converged stack; the harness wires it for you)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="record_fixtures",
        description=(
            "Record scrubbed per-version response fixtures from a healthy, "
            "CONVERGED LiteLLM stack (variant-E state; GET-only). Writes one "
            "bundle per version under --out/<litellm-tag>/."
        ),
    )
    parser.add_argument("--base-url", required=True, help="e.g. http://localhost:4001")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--litellm-tag", required=True, help="e.g. v1.97.0")
    parser.add_argument(
        "--out", type=Path, required=True, help="parent dir (a <tag>/ subdir is written)"
    )
    parser.add_argument(
        "--spec-variant",
        default="variant-e",
        help="which test spec converged the stack (recorded in _meta.json)",
    )
    args = parser.parse_args(argv)
    fixture_dir = capture_from_stack(
        port=int(args.base_url.rsplit(":", 1)[-1]),
        master_key=args.api_key,
        litellm_tag=args.litellm_tag,
        out_dir=args.out,
        spec_variant=args.spec_variant,
    )
    print(f"fixtures recorded under {fixture_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
