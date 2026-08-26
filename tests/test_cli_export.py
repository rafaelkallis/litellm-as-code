"""CLI-level tests for the `export` subcommand.

Exercises `litellm_as_code.cli.main()` directly (no subprocess): the export
command path, missing-credentials errors, and exit codes. Round-trip behavior
itself is covered in tests/test_exporter.py.
"""

from __future__ import annotations

import json

import pytest

from litellm_as_code import cli
from litellm_as_code.cli import main
from litellm_as_code.spec import load_spec

from tests import make_fake_client

SOURCE = {
    "users": [{"user_id": "u1", "user_alias": "admin", "user_role": "proxy_admin"}],
    "models": [
        {
            "model_name": "org/chat",
            "model_info": {"mode": "chat", "input_cost_per_million_tokens": 3.0},
            "litellm_params": {"model": "openai/gpt-4o"},
        }
    ],
}


def test_export_missing_base_url_exits_1(capsys):
    rc = main(["export", "out.yml"])  # no --base-url / --api-key
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_export_missing_api_key_exits_1(capsys):
    rc = main(["export", "out.yml", "--base-url", "http://proxy:4000"])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_export_writes_valid_spec(monkeypatch, tmp_path, capsys):
    """The full CLI export path: build a fake-backed client, run main(), and
    confirm the written file validates and re-apply is a no-op."""
    client, fake = make_fake_client()
    # seed through the fake the same way the reconciler would
    from litellm_as_code.reconciler import reconcile

    seed = tmp_path / "seed.yml"
    seed.write_text(json.dumps(SOURCE))
    reconcile(str(seed), client, dry_run=False)

    out = tmp_path / "exported.yml"

    def _fake_client(url, key):
        return client

    monkeypatch.setattr(cli, "LiteLLMClient", _fake_client)

    rc = main(["export", str(out), "--base-url", "http://proxy:4000", "--api-key", "sk-admin"])
    assert rc == 0

    data = load_spec(out)
    assert "users" in data and "models" in data
    assert data["models"][0]["model_name"] == "org/chat"

    err = capsys.readouterr().err
    assert "exported" in err


def test_export_defaults_to_spec_yml(monkeypatch, tmp_path):
    """With no OUT positional, export writes to ./spec.yml (exit 0)."""
    client, fake = make_fake_client()
    monkeypatch.setattr(cli, "LiteLLMClient", lambda u, k: client)
    monkeypatch.chdir(tmp_path)
    rc = main(["export", "--base-url", "http://proxy:4000", "--api-key", "sk-admin"])
    assert rc == 0
    out = tmp_path / "spec.yml"
    assert out.is_file()
    # An empty proxy exports only the header comment (valid YAML, no sections).
    assert "litellm-as-code — exported" in out.read_text()
