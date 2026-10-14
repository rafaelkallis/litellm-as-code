"""Replay suite: the committed scrubbed fixture bundles (issue #28).

Each bundle under tests/fixtures/responses/<tag>/ is per-version upstream
byte reality, recorded off green compat-matrix legs (scripts/record_fixtures
via the capture hat on scripts/compat_matrix.py --record-fixtures). The
replay drives the REAL client / exporter / reconciler against those bytes:

- parser contract: re-deriving the normalized read views through
  production code must reproduce the committed `.normalized.json` files
- export contract: `build_spec` over the recorded state must reproduce
  the committed `exported_spec.json`
- convergence contract: re-applying the exported spec against the SAME
  recorded state (the recorded state IS converged) must be a no-op with
  ZERO mutations (keys are write-once; a replay that mints keys is a bug)
- secret hygiene: the committed bytes carry no tokens, no raw uuids, no
  identity emails, and no plaintexts under sensitive keys
- the v1.104.2 org gate replays through the capabilities tolerance path

Adding/changing a consumed read endpoint means re-recording: see
tests/fixtures/responses + "Refreshing response fixtures" in tests/live/README.md.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from litellm_as_code.capabilities import is_org_gate_error
from litellm_as_code.reconciler import reconcile
from litellm_as_code.secrets import is_masked_value, is_sensitive_key
from litellm_as_code.spec import load_spec
from litellm_as_code.types import Action, ReconcilerError

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location(
    "record_fixtures", _SCRIPTS / "record_fixtures.py"
)
assert _spec is not None  # file-path import: loader is always present
record_fixtures = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("record_fixtures", record_fixtures)  # shared by other tests
_spec.loader.exec_module(record_fixtures)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "responses"
ALL_BUNDLES = (
    sorted(p for p in FIXTURE_ROOT.iterdir() if p.is_dir() and p.name.startswith("v"))
    if FIXTURE_ROOT.is_dir()
    else []
)
BUNDLE_IDS = [p.name for p in ALL_BUNDLES]


def _bundle(name: str) -> Path:
    path = FIXTURE_ROOT / name
    if not path.is_dir():
        pytest.skip(f"fixture bundle {name} not committed in this checkout")
    return path


def _require_bundles() -> None:
    if not ALL_BUNDLES:
        pytest.skip(
            "no fixture bundles committed (generate with "
            "scripts/compat_matrix.py --record-fixtures tests/fixtures/responses)"
        )


def _meta(bundle: Path) -> dict[str, Any]:
    return json.loads((bundle / "_meta.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------ parser contract


@pytest.mark.parametrize("name", BUNDLE_IDS)
def test_normalized_views_reproduce_the_committed_bytes(name: str):
    bundle = _bundle(name)
    client = record_fixtures.FixtureClient.from_dir(bundle)
    steps = {
        step: json.loads((bundle / f"{step}.raw.json").read_text(encoding="utf-8"))
        for step in record_fixtures.CAPTURE_STEPS
        if (bundle / f"{step}.raw.json").exists()
    }
    computed = record_fixtures.normalize_steps(client, steps)
    for step in steps:
        committed = json.loads((bundle / f"{step}.normalized.json").read_text(encoding="utf-8"))
        assert record_fixtures.canonical_json(computed[step]) == record_fixtures.canonical_json(
            committed
        ), f"{step}: recomputed view differs from committed {bundle.name}/{step}.normalized.json"  # noqa: E501


@pytest.mark.parametrize("name", BUNDLE_IDS)
def test_bundle_versions_are_reportable(name: str):
    bundle = _bundle(name)
    client = record_fixtures.FixtureClient.from_dir(bundle)
    meta = _meta(bundle)
    digits = tuple(int(p) for p in meta["proxy_openapi_version"].split("."))
    assert client.proxy_version() == digits
    assert meta["litellm_tag"] == bundle.name


# ------------------------------------------------------------- export contract


@pytest.mark.parametrize("name", BUNDLE_IDS)
def test_export_over_recorded_bytes_matches_the_committed_reference(name: str):
    _require_bundles()
    bundle = _bundle(name)
    from litellm_as_code.exporter import build_spec

    client = record_fixtures.FixtureClient.from_dir(bundle)
    computed = build_spec(client)
    committed = json.loads((bundle / "exported_spec.json").read_text(encoding="utf-8"))
    assert computed == committed


# ------------------------------------------------------ convergence contract


@pytest.mark.parametrize("name", BUNDLE_IDS)
def test_reapplying_the_exported_spec_against_recorded_bytes_is_a_zero_mutation_noop(
    name: str, tmp_path
):
    bundle = _bundle(name)
    exported = json.loads((bundle / "exported_spec.json").read_text(encoding="utf-8"))
    spec_path = tmp_path / "exported.yml"
    spec_path.write_text(yaml.safe_dump(exported, sort_keys=True), encoding="utf-8")
    load_spec(spec_path)  # the exported reference is a valid spec (validation gate)

    client = record_fixtures.FixtureClient.from_dir(bundle)
    assert client.writes == 0
    plan = reconcile(str(spec_path), client, dry_run=False)  # real loop, real apply path
    assert (
        plan.create_count == 0
        and plan.update_count == 0
        and plan.delete_count == 0
    ), [str(d) for d in plan.diffs if d.action is not Action.NOOP]
    assert client.writes == 0


# ----------------------------------------------------------- gate replay


def test_org_gate_fixture_replays_through_the_capability_tolerance():
    gate_bundle = None
    for p in ALL_BUNDLES:
        if _meta(p).get("org_gate_recorded"):
            gate_bundle = p
            break
    if gate_bundle is None:
        pytest.skip("no fixture bundle recorded an org gate (all proxies licensed)")
    client = record_fixtures.FixtureClient.from_dir(gate_bundle)
    with pytest.raises(ReconcilerError) as excinfo:
        client.list_organizations()
    assert is_org_gate_error(excinfo.value)


# ------------------------------------------------------------ secret hygiene

_ISSUED_EMAIL = re.compile(r"^scrub-\d+@example\.com$")
_ISSUED_UUID = re.compile(r"^fixture-uuid-\d{4}$")
_SK_TOKEN = re.compile(r"^sk-[A-Za-z0-9]{12,}$")
_RAW_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_RAW_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _walk(value: Any, key: str, path: str, hits: list[str]) -> None:
    if isinstance(value, dict):
        for k in value:
            str_k = str(k)
            k_path = f"{path}.{k}" if path else str_k
            if (
                _RAW_UUID.fullmatch(str_k) or (_RAW_EMAIL.fullmatch(str_k) and not _ISSUED_UUID.fullmatch(str_k))
            ):
                # map keys carry ids too (model_max_budget keyed by deployment id)
                hits.append(f"{k_path} (key): unpinned uuid/email as a map key")
                continue
            _walk(value[k], str_k, k_path, hits)
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _walk(item, key, f"{path}[{i}]", hits)
        return
    if isinstance(value, str):
        if _ISSUED_UUID.fullmatch(value) or _ISSUED_EMAIL.fullmatch(value):
            return
        if _SK_TOKEN.fullmatch(value):
            hits.append(f"{path}: sk-shaped token survived")
            return
        if _RAW_EMAIL.fullmatch(value):
            hits.append(f"{path}: unpinned email survived")
            return
        if _RAW_UUID.fullmatch(value):
            hits.append(f"{path}: unpinned uuid survived")
            return
        if (
            is_sensitive_key(key)
            # the exempt identity fields live in one place (the capture
            # module's scrub policy stays the single home for exemptions)
            and record_fixtures._scrub_key_is_material(key)
            and not is_masked_value(value)
        ):
            hits.append(f"{path}: plaintext-looking value under sensitive key {key!r}")


@pytest.mark.parametrize("name", BUNDLE_IDS)
def test_committed_bytes_carry_no_secret_material(name: str):
    bundle = _bundle(name)
    hits: list[str] = []
    for raw in sorted(bundle.glob("*.raw.json")) + [bundle / "exported_spec.json"]:
        payload = json.loads(raw.read_text(encoding="utf-8"))
        _walk(payload, "", "", hits)
    assert hits == []


# ------------------------------------------------ the meta posture is honest


@pytest.mark.parametrize("name", BUNDLE_IDS)
def test_posture_ledger_paths_exist_in_the_committed_files(name: str):
    """Every ledgered path must resolve in the committed bundle: the ledger is
    the review surface for what the scrub kept/dropped, not prose."""
    bundle = _bundle(name)
    meta = _meta(bundle)
    posture = meta["posture"]

    def resolve(path: str) -> bool:
        pulse = path.split(".")
        try:
            current: Any = json.loads((bundle / f"{pulse[0]}.raw.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        for seg in pulse[1:]:
            m = re.match(r"^(.*?)\[(\d+)\]$", seg)
            step, part = (m.group(1), int(m.group(2))) if m else (seg, -1)
            if isinstance(current, dict):
                if step not in current:
                    return False
                current = current[step]
            if part >= 0:
                current = current[part]
        return True

    for path in (
        posture.get("masked_value_paths", [])
        + posture.get("dropped_sensitive_paths", [])
        + posture.get("benign_numeric_sensitive_paths", [])
    ):
        step_path = ".".join(path.split(".")[:2])
        assert resolve(step_path), f"ledger path {path!r} does not resolve in {bundle.name}"
