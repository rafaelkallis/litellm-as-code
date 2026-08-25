"""Skill eval-file consistency tests.

Validates that `skills/litellm-as-code/evals/evals.json` stays structurally
consistent with the skill and the repo **without running the eval loop**
(which needs an agent runtime + a live LiteLLM proxy — a manual/dev workflow
per the agentskills.io 'Evaluating skills' guidance, not a unit test).

The eval loop itself (with_skill vs without_skill runs, assertions, timing,
benchmark aggregation) is intentionally NOT exercised here. These tests only
guard against drift in the file/CI plumbing.

Mirrors the "mock-only unit tests" convention of the rest of the repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "litellm-as-code"
EVALS = SKILL_ROOT / "evals" / "evals.json"
REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_CASE_FIELDS = {"id", "prompt", "expected_output"}


def _load_evals() -> dict:
    return json.loads(EVALS.read_text())


def test_evals_file_exists_and_is_valid_json() -> None:
    assert EVALS.is_file(), f"missing {EVALS.relative_to(SKILL_ROOT.parents[1])}"
    data = _load_evals()
    assert isinstance(data, dict)


def test_skill_name_matches_folder() -> None:
    assert _load_evals()["skill_name"] == SKILL_ROOT.name


def test_eval_cases_have_required_fields_and_unique_ids() -> None:
    data = _load_evals()
    cases = data["evals"]
    assert isinstance(cases, list) and cases
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "eval ids must be unique"
    for case in cases:
        assert REQUIRED_CASE_FIELDS.issubset(case), f"case {case.get('id')} missing required field"


def test_eval_prompts_are_realistic_and_scoped() -> None:
    """Prompts reference realistic spec subjects and don't ask for secret reads."""
    for case in _load_evals()["evals"]:
        prompt = case["prompt"].lower()
        # should be a realistic user request, not a placeholder
        assert len(case["prompt"]) > 12
        assert "password" not in prompt and "secret" not in prompt


def test_eval_file_cases_assert_only_scoped_positives() -> None:
    """Assertions should be programmatic/objective, not brittle exact strings."""
    for case in _load_evals()["evals"]:
        asserts = case.get("assertions", [])
        assert asserts, f"case {case['id']} should have assertions (programmatic)"
        for a in asserts:
            # brittle: exact-phrase / dollar-sign expectations
            assert "$" not in a, f"brittle assertion (exact value): {a}"
            assert not a.startswith("The output is good"), "vague assertion"


@pytest.mark.skipif(
    not (REPO_ROOT / "examples" / "spec.yml").is_file(),
    reason="examples/spec.yml not present",
)
def test_eval_files_reference_only_existing_skill_files() -> None:
    """`evals.json` `files` are repo-root-relative (they reference the source
    of truth `examples/spec.yml` and the skill's own `templates/spec.yml`)."""
    data = _load_evals()
    for case in data["evals"]:
        for f in case.get("files", []):
            target = REPO_ROOT / f
            assert target.exists(), f"case {case['id']} references missing file: {f}"
