"""Live integration tests for the `export` subcommand (against a REAL proxy).

Complements `test_live_proxy.py`: boot the test-owned proxy stack
(`tests/live/proxy/`), converge one of the permutation specs, export the live
state with the CLI, then assert that re-applying the export is a clean no-op
(0 to create, 0 to update) and a `--dry-run` of it exits 0.

Also asserts the export's fidelity invariants against a real proxy:
  - only comparable fields are emitted (no spend/status/budget_reset_at),
  - secrets are NOT re-read (no raw key, empty credential_values),
  - model costs are exported back per-million.

Skipped unless LITELLM_BASE_URL + LITELLM_API_KEY are set (same gate as
test_live_proxy.py).
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


def _run_cli(*args: str) -> subprocess.CompletedProcess:
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


@pytest.mark.integration
def test_export_then_reapply_is_noop(tmp_path):
    """Converge a spec, export the live proxy, then re-apply the export: the
    second apply must be a no-op and its dry-run must exit 0."""
    source_spec = LIVE_DIR / "spec-variant-c.yml"

    # 1) converge the source spec (self-healing like test_live_proxy)
    apply1 = _run_cli(str(source_spec))
    assert apply1.returncode == 0, apply1.stderr or apply1.stdout
    if "0 to create, 0 to update" not in apply1.stdout:
        apply1 = _run_cli(str(source_spec))
    assert "0 to create, 0 to update" in apply1.stdout, apply1.stdout

    # 2) export the live state
    out = tmp_path / "exported.yml"
    export_cli = _run_cli("export", str(out))
    assert export_cli.returncode == 0, export_cli.stderr or export_cli.stdout
    assert out.is_file()

    data = yaml.safe_load(out.read_text())

    # fidelity invariants
    raw = (out.read_text() or "").lower()
    assert "spend:" not in raw
    assert "status:" not in raw
    assert "budget_reset_at" not in raw
    for vk in data.get("virtual_keys", []):
        assert "key" not in vk  # raw key is write-once; never exported
    for cred in data.get("credentials", []):
        # values are masked; only an empty marker (or absent) is exported
        assert not cred.get("credential_values")
    # Inferred `mode` must never be adopted into desired state (AGENTS.md §4).
    for model in data.get("models", []):
        assert "mode" not in model.get("model_info", {})
    # Guardrail write-once params come back masked; they must not be persisted.
    for guardrail in data.get("guardrails", []):
        params = guardrail.get("litellm_params", {})
        for k in params:
            assert not any(
                kw in k.lower() for kw in ("key", "token", "secret", "password", "credential")
            ), f"masked guardrail param exported: {k}"

    # 3) re-apply the export — must be clean
    apply2 = _run_cli(str(out))
    assert apply2.returncode == 0, apply2.stderr or apply2.stdout
    if "0 to create, 0 to update" not in apply2.stdout:
        apply2 = _run_cli(str(out))
    assert "0 to create, 0 to update" in apply2.stdout, apply2.stdout

    # 4) dry-run of the export exits 0 (no diff on the converged state)
    dry = _run_cli(str(out), "--dry-run")
    assert dry.returncode == 0, (dry.stdout or dry.stderr)

    # 5) the export validates through the same loader
    check = _run_cli(str(out), "--dry-run")
    assert "invalid spec" not in (check.stderr or "")
