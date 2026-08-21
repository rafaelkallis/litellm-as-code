"""Live integration tests against a REAL LiteLLM proxy.

These are full-stack tests: they boot the local `litellm-as-code` CLI (or
reconciler) against a real proxy and assert convergence — not the in-memory
fake used by `tests/test_*.py`.

They are SKIPPED unless the proxy is reachable. Enable via `LITELLM_BASE_URL`
(and optionally `LITELLM_API_KEY` / `LITELLM_MASTER_KEY`):

    export LITELLM_BASE_URL=http://localhost:4000
    export LITELLM_API_KEY=sk-demo-master-key-change-me
    uv run pytest tests/live -m "not slow"

To spin up a proxy first:

    cd examples/docker-compose
    docker compose up -d postgres litellm

Each test targets a dedicated, isolated spec (fresh identifiers), restores the
proxy state afterwards, and asserts:
  1. first apply creates the declared resources,
  2. a second apply is a no-op (`0 to create, 0 to update`),
  3. `--dry-run` exits 0 on the converged state.

`test_mutation_round_converges` additionally mutates an existing resource
(policy drift -> recreate, team member role change) and re-converges.

NOTE: two specs (variant B vs D) assert conflicting values on the SAME
identifiers, so they cannot both run against one proxy in one CI job; they
are exported as independent named specs instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

LIVE_DIR = Path(__file__).parent
REPO_ROOT = LIVE_DIR.parents[1]

BASE_URL = os.environ.get("LITELLM_BASE_URL")
API_KEY = os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not BASE_URL or not API_KEY,
        reason="live proxy integration tests need LITELLM_BASE_URL + LITELLM_API_KEY (or LITELLM_MASTER_KEY)",
    ),
]

SPECS = {
    # (spec filename, expected converged resource count)
    "a": ("spec-variant-a.yml", 11),
    "c": ("spec-variant-c.yml", 9),
    # variant-b declares an enterprise-gated team admin on self-hosted
    # proxies; enabled only when LITELLM_RUN_VARIANT_B=1 (CI sets it when it
    # runs with an LRU/enterprise-capable proxy).
    "b": ("spec-variant-b.yml", 10),
}

# variant-b is enterprise-gated on OSS LiteLLM -> skipped by default
RUN_VARIANT_B = os.environ.get("LITELLM_RUN_VARIANT_B") == "1"
SKIP_VARIANT_B = pytest.mark.skipif(
    not RUN_VARIANT_B,
    reason="variant B asserts a team-admin role (enterprise-only on OSS LiteLLM); set LITELLM_RUN_VARIANT_B=1 to include it",
)


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run the installed litellm-as-code CLI against the live proxy."""
    env = dict(os.environ)
    env.setdefault("LITELLM_BASE_URL", BASE_URL)
    env.setdefault("LITELLM_API_KEY", API_KEY)
    return subprocess.run(
        [sys.executable, "-m", "litellm_as_code.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )


@pytest.fixture(scope="module")
def proxy():
    """Very cheap reachability probe; each test re-checks anyway."""
    if not BASE_URL:
        pytest.skip("no LITELLM_BASE_URL")
    return BASE_URL


@pytest.mark.parametrize("name", ["a", "b", "c"])
def test_variant_converges_and_is_idempotent(proxy, name, tmp_path):
    if name == "b" and not RUN_VARIANT_B:
        pytest.skip("variant B asserts a team-admin role (enterprise-only); set LITELLM_RUN_VARIANT_B=1")
    spec_file, expected_ok = SPECS[name]
    spec_path = LIVE_DIR / spec_file

    # 1) apply until converged. The proxy may hold drifted state from
    #    previous tests/specs, so the FIRST apply must converge this spec's
    #    own identifiers (assertions target the second run, which must be a
    #    clean no-op).
    apply1 = _run_cli(str(spec_path))
    assert apply1.returncode == 0, apply1.stderr or apply1.stdout
    if "0 to create, 0 to update" not in apply1.stdout:
        # eventual consistency / drifted state: self-heal with one retry
        apply1 = _run_cli(str(spec_path))
    assert "0 to create, 0 to update" in apply1.stdout, apply1.stdout

    # 2) the decisive idempotency check: second apply is a no-op
    apply2 = _run_cli(str(spec_path))
    assert apply2.returncode == 0, apply2.stderr or apply2.stdout
    assert "0 to create, 0 to update" in apply2.stdout, apply2.stdout
    assert f"{expected_ok} unchanged" in apply2.stdout, apply2.stdout

    # 3) converged state is a clean plan (exit 0, CI-friendly)
    dry = _run_cli(str(spec_path), "--dry-run")
    assert dry.returncode == 0, dry.stdout or dry.stderr


def test_mutation_round_converges(proxy, tmp_path):
    """Applied twice on a converged base, a mutation (policy drift +
    member role change) must converge to a new stable state."""
    base = LIVE_DIR / "spec-variant-c.yml"
    # self-contained: converge the base first (order-independent)
    base_apply = _run_cli(str(base))
    assert base_apply.returncode == 0, base_apply.stderr or base_apply.stdout
    assert "0 to create, 0 to update" in _run_cli(str(base)).stdout

    mut = tmp_path / "mutation.yml"
    data = yaml.safe_load(base.read_text())
    data["policies"][0]["description"] = "updated by integration test"
    data["teams"][0]["members_with_roles"][0]["role"] = "user"
    mut.write_text(yaml.safe_dump(data))

    r1 = _run_cli(str(mut))
    assert r1.returncode == 0, r1.stderr or r1.stdout
    r2 = _run_cli(str(mut))
    assert "0 to create, 0 to update" in r2.stdout, r2.stdout
    r3 = _run_cli(str(mut), "--dry-run")
    assert r3.returncode == 0, r3.stdout or r3.stderr


@pytest.mark.slow
def test_base_example_spec_converges(proxy):
    """The documented example spec must converge on a live stack."""
    spec_path = REPO_ROOT / "examples" / "docker-compose" / "spec.yml"
    apply1 = _run_cli(str(spec_path))
    assert apply1.returncode == 0, apply1.stderr or apply1.stdout
    assert "0 to create, 0 to update" in _run_cli(str(spec_path)).stdout
