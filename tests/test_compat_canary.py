"""Mock tests for the nightly compat-canary glue (scripts/compat_canary.py).

Everything gh- or docker-facing is exercised through a scripted fake runner;
the real ``gh`` calls only happen in the compat-nightly workflow. The report
files are written by the harness's own pure ``report_payload``/``write_report``
under test_compat_matrix.py, so the two sides of the contract are pinned
against each other in the mock gate.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader  # file-path import: loader is always present
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


canary = _load("compat_canary")

# --------------------------------------------------------------------- fakes


class FakeGh:
    """Scripted gh runner: records argv, returns canned CompletedProcesses
    (list-mode: results are consumed sequentially, one per call)."""

    def __init__(self, results: list | None = None):
        self.calls: list[list[str]] = []
        self.results = results if results is not None else []
        self.cursor = 0

    def __call__(self, argv: list[str]):
        self.calls.append(list(argv))
        result = self.results[self.cursor] if self.cursor < len(self.results) else ok()
        self.cursor += 1
        if isinstance(result, tuple):  # (returncode, stdout, stderr)
            result = SimpleNamespace(returncode=result[0], stdout=result[1], stderr=result[2])
        if isinstance(result, int):  # bare returncode
            result = SimpleNamespace(returncode=result, stdout="", stderr=f"gh exited {result}")
        return result

    def joined(self) -> list[str]:
        """One-line-per-call rendering ('issue create -R ...') for asserts."""
        return [" ".join(argv) for argv in self.calls]

    @property
    def invocations(self) -> int:
        return len(self.calls)


def ok(stdout: str = "") -> object:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def fail(stderr: str = "boom", code: int = 1) -> object:
    return SimpleNamespace(returncode=code, stdout="", stderr=stderr)


# --------------------------------------------------------------------- resolve


def test_stable_tag_from_release_filters_the_full_ladder():
    bare = {"tag_name": "v1.105.0", "draft": False, "prerelease": False}
    assert canary.stable_tag_from_release(bare) == "v1.105.0"
    # every non-stable shape must be refused (the tag gets docker-pulled):
    assert canary.stable_tag_from_release({**bare, "draft": True}) is None
    assert canary.stable_tag_from_release({**bare, "prerelease": True}) is None
    assert canary.stable_tag_from_release({**bare, "tag_name": "v1.106.0-rc.1"}) is None
    assert canary.stable_tag_from_release({**bare, "tag_name": "1.105.0"}) is None
    assert canary.stable_tag_from_release({**bare, "tag_name": None}) is None


def test_resolve_latest_stable_uses_releases_latest_when_it_yields_a_bare_tag():
    gh = FakeGh([ok("v1.105.0\n")])
    assert canary.resolve_latest_stable(gh) == "v1.105.0"
    assert len(gh.calls) == 1  # no fallback lookup needed
    assert gh.joined()[0].startswith("api repos/BerriAI/litellm/releases/latest")


def test_resolve_latest_stable_falls_back_to_the_release_list_newest_first():
    # /releases/latest 404s; the fallback must take the FIRST stable in list
    # order WITHOUT re-sorting (v1.9 > v1.104 stringwise would corrupt the pick).
    gh = FakeGh([fail(code=404), ok(json.dumps(["v1.99.0", "v1.104.2"]))])
    assert canary.resolve_latest_stable(gh) == "v1.99.0"
    assert gh.invocations == 2


@pytest.mark.parametrize(
    "list_payload",
    [
        json.dumps(["v1.106.0-rc.1", "v1.106.0-rc.2"]),  # only prereleases
        json.dumps([]),  # empty list
        json.dumps({"unexpected": "shape"}),  # not a list
    ],
)
def test_resolve_latest_stable_refuses_unusable_fallback_lists(list_payload):
    gh = FakeGh([fail(code=404), ok(list_payload)])
    with pytest.raises(RuntimeError):
        canary.resolve_latest_stable(gh)


def test_resolve_latest_stable_gives_up_when_gh_fails_twice():
    gh = FakeGh([fail(code=403), fail(stderr="rate limited", code=403)])
    with pytest.raises(RuntimeError, match="could not resolve"):
        canary.resolve_latest_stable(gh)


def test_resolve_subcommand_honours_a_validated_env_override(monkeypatch, capsys):
    monkeypatch.setenv("LITELLM_CANARY_TAG", "v1.105.0")
    assert canary.resolve_subcommand() == 0
    assert capsys.readouterr().out.strip() == "v1.105.0"


@pytest.mark.parametrize("junk", ["1.105.0", "v1.106.0-rc.1", "latest", "v1.105.0 extra"])
def test_resolve_subcommand_rejects_a_junk_env_override(monkeypatch, capsys, junk):
    monkeypatch.setenv("LITELLM_CANARY_TAG", junk)
    with pytest.raises(RuntimeError):
        canary.resolve_subcommand()


def test_main_resolve_rejects_a_junk_env_override_with_exit_1(monkeypatch, capsys):
    monkeypatch.setenv("LITELLM_CANARY_TAG", "bogus")
    assert canary.main(["resolve"]) == 1
    assert "not a bare stable tag" in capsys.readouterr().err


# ----------------------------------------------------------------- report io


def _write_report(
    path: Path,
    *,
    harness: str = "compat_matrix",
    versions: list[str] | None = None,
    legs: list[dict] | None = None,
    all_green: bool | None = None,
) -> Path:
    payload = {
        "harness": harness,
        "versions": versions
        if versions is not None
        else ["v1.97.0", "v1.105.0"],
        "all_green": all_green if all_green is not None else False,
        "ran_all": True,
        "legs": legs
        if legs is not None
        else [
            {"version": "v1.97.0", "green": True, "note": "", "exit_code": 0, "counts": {"passed": 9, "skipped": 4}, "no_tests": False, "tail": ""},
            {"version": "v1.105.0", "green": False, "note": "pytest exited 1", "exit_code": 1, "counts": {"passed": 8, "failed": 1, "skipped": 4}, "no_tests": False, "tail": "FAILED tests/live/test_live_proxy.py::test_drift"},
        ],
        "not_run": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_read_report_derives_the_verdict_and_names_the_red_leg(tmp_path):
    parsed = canary.read_report(_write_report(tmp_path / "report.json"))
    assert parsed.all_green is False
    assert parsed.failed_legs[0]["version"] == "v1.105.0"
    assert parsed.canary == "v1.105.0"  # first red leg, not the floor


def test_read_report_green_report_talks_about_the_canary_leg(tmp_path):
    red = {
        "version": "v1.97.0",
        "green": True,
        "note": "",
        "exit_code": 0,
        "counts": {"passed": 9},
        "no_tests": False,
        "tail": "",
    }
    current = dict(red, version="v1.105.0", counts={"passed": 10})
    parsed = canary.read_report(
        _write_report(
            tmp_path / "report.json",
            versions=["v1.97.0", "v1.105.0"],
            all_green=True,
            legs=[red, current],
        )
    )
    assert parsed.all_green is True and parsed.failed_legs == []
    assert parsed.canary == "v1.105.0"  # last requested = the canary


def test_read_report_rejects_broken_reports(tmp_path):
    with pytest.raises(RuntimeError):
        canary.read_report(tmp_path / "missing.json")
    junk = tmp_path / "junk.json"
    junk.write_text("not json at all", encoding="utf-8")
    with pytest.raises(RuntimeError):
        canary.read_report(junk)
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps({"harness": "other"}), encoding="utf-8")
    with pytest.raises(RuntimeError):
        canary.read_report(foreign)


def test_read_report_rejects_contradicted_or_empty_leg_lists(tmp_path):
    legs_green = {
        "version": "v1.97.0",
        "green": True,
        "note": "",
        "exit_code": 0,
        "counts": {},
        "no_tests": False,
        "tail": "",
    }
    # report screams all_green=True while a leg is red
    lied = tmp_path / "lied.json"
    _write_report(lied, all_green=True, legs=[legs_green, dict(legs_green, version="v1.105.0", green=False)])
    with pytest.raises(RuntimeError, match="contradicts"):
        canary.read_report(lied)
    # truncated run: no legs at all
    empty = tmp_path / "empty.json"
    _write_report(empty, legs=[])
    with pytest.raises(RuntimeError, match="malformed"):
        canary.read_report(empty)


# --------------------------------------------------------------- decide/x


def test_decide_action_truth_table():
    assert canary.decide_action(green=True, open_issue=None) == "noop"
    assert canary.decide_action(green=True, open_issue=17) == "close"
    assert canary.decide_action(green=False, open_issue=None) == "create"
    assert canary.decide_action(green=False, open_issue=17) == "comment"


def test_open_canary_issue_returns_the_single_open_number():
    gh = FakeGh([ok(json.dumps([{"number": 17, "createdAt": "2026-10-01T00:00:00Z"}]))])
    assert canary.open_canary_issue(gh, "owner/repo", "compat-canary") == 17


def test_open_canary_issue_noop_when_none_is_open():
    gh = FakeGh([ok(json.dumps([]))])
    assert canary.open_canary_issue(gh, "owner/repo", "compat-canary") is None


def test_open_canary_issue_refuses_multiple_trackers():
    gh = FakeGh([
        ok(
            json.dumps([
                {"number": 9, "createdAt": "2026-10-01T00:00:00Z"},
                {"number": 17, "createdAt": "2026-10-02T00:00:00Z"},
            ])
        )
    ])
    with pytest.raises(RuntimeError, match="multiple open"):
        canary.open_canary_issue(gh, "owner/repo", "compat-canary")


def test_open_canary_issue_propagates_gh_failures():
    gh = FakeGh([fail(stderr="issues:write missing")])
    with pytest.raises(RuntimeError, match="gh issue list failed"):
        canary.open_canary_issue(gh, "owner/repo", "compat-canary")


# ------------------------------------------------------------- rendering


def test_render_matrix_table_pass_fail_rows_and_not_run():
    table = canary.render_matrix_table(
        {
            "legs": [
                {"version": "v1.97.0", "green": True, "note": "", "counts": {"passed": 9, "skipped": 4}, "exit_code": 0},
                {"version": "v1.105.0", "green": False, "note": "pytest exited 1", "counts": {"passed": 8, "failed": 1}, "exit_code": 1},
            ],
            "not_run": ["v1.106.0"],
        }
    )
    assert "| v1.97.0 | PASS | 9 | 0 | 0 | 4 |  |" in table
    assert "| v1.105.0 | FAIL | 8 | 1 | 0 | 0 | pytest exited 1 |" in table
    assert "| v1.106.0 | NOT RUN | - | - | - | - | (fail-fast cut) |" in table


def test_render_matrix_table_escapes_pipes_and_missing_counts():
    table = canary.render_matrix_table(
        {"legs": [{"version": "v1.99.0", "green": False, "note": "compose up failed (a|b)", "counts": {}}]}
    )
    assert r"a\|b" in table
    assert "| v1.99.0 | FAIL | 0 | 0 | 0 | 0 | compose up failed (a\\|b) |" in table


def test_red_evidence_bodies_include_the_needed_fragments():
    failed = [
        {"version": "v1.105.0", "green": False, "note": "pytest exited 1", "exit_code": 1,
         "counts": {"passed": 8, "failed": 1}, "tail": "FAILED tests/live/test_live_proxy.py::test_drift"}
    ]
    title = canary.red_title(failed)
    assert title == "compat break: LiteLLM v1.105.0 (nightly canary red)"
    body = canary.red_body({"legs": failed, "not_run": []}, failed, "https://run/42")
    assert "https://run/42" in body
    assert "| v1.105.0 | FAIL" in body
    assert "pytest tail — v1.105.0 (exit 1)" in body
    assert "pytest exited 1" in body
    comment = canary.still_red_comment("v1.105.0", failed, "https://run/43")
    assert "still red on LiteLLM **v1.105.0**" in comment
    assert "1 failed, 0 errors" in comment
    closed = canary.recovered_comment("v1.105.0", "https://run/44")
    assert "**Green again** on LiteLLM `v1.105.0`" in closed
    assert "https://run/44" in closed


def test_tails_are_capped_in_issue_bodies():
    many = [
        {"version": f"v1.10{i}.0", "green": False, "note": "x", "exit_code": 1, "counts": {}, "tail": f"tail {i}"}
        for i in range(6)
    ]
    created = canary.red_body({"legs": many, "not_run": []}, many, "https://run/1")
    assert created.count("<details>") == canary.MAX_TAILS_CREATE
    assert "Further failing legs omitted" in created
    commented = canary.still_red_comment("v1.10x.0", many, "https://run/2")
    assert commented.count("<details>") == canary.MAX_TAILS_COMMENT


# ----------------------------------------------------------------- writers


def test_ensure_label_creates_only_when_missing():
    exists = FakeGh([ok(json.dumps({"name": "compat-canary"}))])
    canary.ensure_label(exists, "owner/repo")
    assert [c[0] for c in exists.calls] == ["label"]  # view only, no mutation

    missing = FakeGh([fail(stderr="label not found"), ok()])
    canary.ensure_label(missing, "owner/repo")
    verbs = [c[0] for c in missing.calls]
    assert verbs == ["label", "label"]  # view then create
    assert missing.calls[1][1] == "create"

    # a gh failure that is NOT 'not found' is surfaced, never blindly retried
    broken = FakeGh([fail(stderr="permission denied")])
    with pytest.raises(RuntimeError, match="gh label view failed"):
        canary.ensure_label(broken, "owner/repo")


def test_ensure_label_tolerates_a_create_race():
    gh = FakeGh([fail(stderr="label not found"), (422, "", "already exists")])
    canary.ensure_label(gh, "owner/repo")  # someone else created it meanwhile: fine
    assert len(gh.calls) == 2


def test_write_github_calls_failures_surface(monkeypatch):
    def boomed(argv, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="nope")

    monkeypatch.setattr(canary, "run_gh", boomed)
    with pytest.raises(RuntimeError, match="gh issue comment failed"):
        canary.comment_on_issue(boomed, "owner/repo", 3, "body")
    with pytest.raises(RuntimeError, match="gh issue close failed"):
        canary.close_issue(boomed, "owner/repo", 3, "body")


# ------------------------------------------------------------ CI glue guard


def test_nightly_workflow_manage_step_invokes_the_subcommand():
    """The 'manage' step's run block must name the manage subcommand.

    Regression guard for run 38392163481: the shell array was built without
    the subcommand, so the report path became the subcommand and argparse
    died (exit 2) — the lifecycle would never have run in CI.
    """
    wf_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "compat-nightly.yml"
    steps = yaml.safe_load(wf_path.read_text(encoding="utf-8"))["jobs"]["canary"]["steps"]
    manage = next(s for s in steps if s.get("name") == "Manage compat-canary tracking issue")
    run_block = manage["run"]
    assert "python scripts/compat_canary.py" in run_block
    assert "args=(manage" in run_block  # the subcommand survives the glue
    assert "--report reports/nightly-report.json" in run_block
    # the simulate choice must be guarded — a bare --simulate dies on schedule
    assert "inputs.simulate" in run_block and "--simulate" in run_block


def test_nightly_workflow_wires_the_contract_digest_glue():
    """Issue #26 glue: per-leg capture, drift diff, green-only refresh.

    Run-blocks are test-blind in CI (a missing subcommand broke the whole
    nightly once — run 38392163481), so the YAML glue is pinned here.
    """
    wf_path = Path(__file__).resolve().parents[1] / "tests" / ".." / ".github" / "workflows" / "compat-nightly.yml"
    workflow = yaml.safe_load(wf_path.read_text(encoding="utf-8"))
    steps = {s.get("name"): s for s in workflow["jobs"]["canary"]["steps"]}
    # the leg step captures a digest next to the report
    assert "--digest-out reports/nightly-digest.json" in steps["Run canary legs (floor + latest stable)"]["run"]
    # the drift diff artifacts exist and the refresh gate is green+identical
    diffstep = steps["Diff consumed-surface digest vs last-known-good"]
    assert "openapi-last-known-good.json" in diffstep["run"]
    assert "contract-drift.txt" in diffstep["run"]
    refresh = steps["Refresh last-known-good digest (green + drift-free only)"]
    assert "steps.digestdiff.outputs.code == '0'" in refresh["if"]
    assert "steps.digestdiff.outputs.all_green == 'true'" in refresh["if"]
    # the manage step forwards drift text when it exists
    manage_run = steps["Manage compat-canary tracking issue"]["run"]
    assert "--digest-diff" in manage_run and "contract-drift.txt" in manage_run
    # repo write permission for the refresh commit
    assert workflow["permissions"]["contents"] == "write"


# ------------------------------------------------------- digest drift (#26)


def _write_drift(tmp_path: Path, text: str = "POST /user/new:\n    …renamed param") -> Path:
    path = tmp_path / "contract-drift.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_digest_section_embedded_in_red_body_create(tmp_path, monkeypatch, capsys):
    report = _write_report(tmp_path / "report.json")
    drift = _write_drift(tmp_path)
    gh = FakeGh([
        ok("https://github.com/owner/repo/issues/21\n"),  # issue create (label exists)
    ])
    monkeypatch.setattr(canary, "open_canary_issue", lambda *a, **k: None)
    monkeypatch.setattr(canary, "ensure_label", lambda *a, **k: None)
    monkeypatch.setattr(canary, "run_gh", gh)
    args = _manage_args(tmp_path, report, dry_run=False, digest_diff=str(drift))
    assert canary.manage(args) == 0
    body = " ".join(gh.joined())
    assert "contract drift on the consumed surface" in body
    assert "renamed param" in body
    assert "NOT a break by itself" in body  # informational, never a gate


def test_digest_section_embedded_in_still_red_comment(tmp_path, monkeypatch, capsys):
    report = _write_report(tmp_path / "report.json")
    drift = _write_drift(tmp_path)
    gh = FakeGh([])  # comment only
    monkeypatch.setattr(canary, "open_canary_issue", lambda *a, **k: 17)
    monkeypatch.setattr(canary, "run_gh", gh)
    args = _manage_args(tmp_path, report, dry_run=False, digest_diff=str(drift))
    assert canary.manage(args) == 0
    body = " ".join(gh.joined())
    assert "contract drift on the consumed surface" in body


def test_digest_section_embedded_in_recovering_close(tmp_path, monkeypatch, capsys):
    green = _write_report(
        tmp_path / "report.json",
        all_green=True,
        legs=[{
            "version": "v1.97.0", "green": True, "note": "", "exit_code": 0,
            "counts": {"passed": 9, "failed": 0, "errors": 0, "skipped": 4},
            "no_tests": False, "tail": "",
        }],
    )
    drift = _write_drift(
        tmp_path, text="GET /user/list: parameters.0.schema.type: 'integer' -> 'string'"
    )
    gh = FakeGh([])
    monkeypatch.setattr(canary, "open_canary_issue", lambda *a, **k: 17)
    monkeypatch.setattr(canary, "run_gh", gh)
    args = _manage_args(tmp_path, green, dry_run=False, digest_diff=str(drift))
    assert canary.manage(args) == 0
    body = " ".join(gh.joined())
    assert "auto-closed as recovered" in body.lower() or "Green again" in body
    assert "contract drift on the consumed surface" in body
    assert "'integer' -> 'string'" in body


def test_digest_diff_tolerates_missing_file_and_default_none(tmp_path, monkeypatch, capsys):
    report = _write_report(tmp_path / "report.json")
    # default None and missing file both behave identically: no drift section
    for digest_diff in (None, str(tmp_path / "does-not-exist.txt")):
        gh = FakeGh([])
        monkeypatch.setattr(canary, "open_canary_issue", lambda *a, **k: 17)
        monkeypatch.setattr(canary, "run_gh", gh)
        args = _manage_args(tmp_path, report, digest_diff=digest_diff)
        assert canary.manage(args) == 0
        assert "contract drift" not in " ".join(gh.joined())


def test_parser_has_digest_diff_optional():
    parser = canary.build_parser()
    args = parser.parse_args(["manage", "--report", "r.json", "--run-url", "u"])
    assert args.digest_diff is None
    args = parser.parse_args(
        ["manage", "--report", "r.json", "--run-url", "u", "--digest-diff", "d.txt"]
    )
    assert args.digest_diff == "d.txt"



def _manage_args(tmp_path: Path, report: Path, **overrides) -> SimpleNamespace:
    base = dict(
        report=str(report),
        run_url="https://github.com/owner/repo/actions/runs/42",
        repo="owner/repo",
        label="compat-canary",
        simulate=None,
        dry_run=True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


SHORT_TAIL = "FAILED tests/live/test_live_proxy.py::test_drift"


def test_manage_red_no_tracker_files_one_issue(tmp_path, monkeypatch, capsys):
    report = _write_report(tmp_path / "report.json")
    gh = FakeGh([
        ok(json.dumps([])),  # issue list: none open
        fail(stderr="label compat-canary: not found"),  # ensure_label: view
        ok(),  # ensure_label: create (fork safety; here it exists)
        ok("https://github.com/owner/repo/issues/17\n"),  # issue create
    ])
    monkeypatch.setattr(canary, "run_gh", gh)
    args = _manage_args(tmp_path, report, dry_run=False)
    assert canary.manage(args) == 0
    out = capsys.readouterr().out
    assert "filed tracking issue #17" in out
    joined = gh.joined()
    assert joined[0].startswith("issue list")
    assert any(call.startswith("label create") for call in joined)
    create_call = [call for call in joined if call.startswith("issue create")][0]
    assert "--label compat-canary" in create_call
    assert "--title" in create_call
    # the created body carries the matrix + the failed tail + the run URL
    assert SHORT_TAIL in " ".join(joined)


def test_manage_red_with_open_tracker_only_comments(monkeypatch, capsys, tmp_path):
    report = _write_report(tmp_path / "report.json")
    gh = FakeGh([
        ok(json.dumps([{"number": 17, "createdAt": "2026-10-01T00:00:00Z"}])),
        ok(),
    ])
    monkeypatch.setattr(canary, "run_gh", gh)
    assert canary.manage(_manage_args(tmp_path, report, dry_run=False)) == 0
    assert capsys.readouterr().out.startswith("refreshed tracking issue #17")
    joined = gh.joined()
    assert len(joined) == 2  # list + comment — never create, never close
    assert any(call.startswith("issue comment") for call in joined)
    assert not any(call.startswith("issue create") for call in joined)
    assert not any(call.startswith("issue close") for call in joined)


def test_manage_green_with_open_tracker_comments_and_closes(monkeypatch, capsys, tmp_path):
    leg = {
        "version": "v1.97.0",
        "green": True,
        "note": "",
        "exit_code": 0,
        "counts": {"passed": 9, "skipped": 4},
        "no_tests": False,
        "tail": "",
    }
    report = _write_report(
        tmp_path / "report.json",
        versions=["v1.97.0", "v1.105.0"],
        all_green=True,
        legs=[leg, dict(leg, version="v1.105.0", counts={"passed": 10})],
    )
    gh = FakeGh([
        ok(json.dumps([{"number": 17, "createdAt": "2026-10-01T00:00:00Z"}])),
        ok(),  # comment
        ok(),  # close
    ])
    monkeypatch.setattr(canary, "run_gh", gh)
    assert canary.manage(_manage_args(tmp_path, report, dry_run=False)) == 0
    out = capsys.readouterr().out
    assert "closed tracking issue #17" in out and "green again on v1.105.0" in out
    joined = gh.joined()
    assert any(call.startswith("issue comment") for call in joined)
    close_call = [call for call in joined if call.startswith("issue close")][0]
    assert "--reason completed" in close_call
    assert "Green again" in " ".join(joined)  # evidence lives in the close comment
    assert not any(call.startswith("issue create") for call in joined)


def test_manage_green_no_tracker_posts_nothing(monkeypatch, capsys, tmp_path):
    leg = {
        "version": "v1.97.0",
        "green": True,
        "note": "",
        "exit_code": 0,
        "counts": {"passed": 9},
        "no_tests": False,
        "tail": "",
    }
    report = _write_report(
        tmp_path / "report.json",
        versions=["v1.97.0", "v1.105.0"],
        all_green=True,
        legs=[leg, dict(leg, version="v1.105.0", counts={"passed": 10})],
    )
    gh = FakeGh([ok(json.dumps([]))])
    monkeypatch.setattr(canary, "run_gh", gh)
    assert canary.manage(_manage_args(tmp_path, report, dry_run=False)) == 0
    out = capsys.readouterr().out
    assert "nothing posted" in out
    # exactly one read-only call: the open-issue search
    assert gh.joined() == [
        "issue list -R owner/repo --state open --label compat-canary --json number,createdAt"
    ]


def test_manage_dry_run_red_creates_nothing_but_prints_the_body(monkeypatch, capsys, tmp_path):
    report = _write_report(tmp_path / "report.json")
    gh = FakeGh([ok(json.dumps([]))])
    monkeypatch.setattr(canary, "run_gh", gh)
    assert canary.manage(_manage_args(tmp_path, report, dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "[dry-run] would CREATE issue 'compat break: LiteLLM v1.105.0" in out
    assert "https://github.com/owner/repo/actions/runs/42" in out
    assert gh.invocations == 1  # read-only search only; no writes, not even labels


def test_manage_dry_run_green_with_tracker_prints_the_close(monkeypatch, capsys, tmp_path):
    leg = {
        "version": "v1.105.0",
        "green": True,
        "note": "",
        "exit_code": 0,
        "counts": {"passed": 10},
        "no_tests": False,
        "tail": "",
    }
    report = _write_report(
        tmp_path / "report.json",
        versions=["v1.97.0", "v1.105.0"],
        all_green=True,
        legs=[dict(leg, version="v1.97.0"), leg],
    )
    gh = FakeGh([ok(json.dumps([{"number": 17, "createdAt": "2026-10-01T00:00:00Z"}]))])
    monkeypatch.setattr(canary, "run_gh", gh)
    assert canary.manage(_manage_args(tmp_path, report, dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "[dry-run] would CLOSE #17 as recovered" in out
    assert gh.invocations == 1  # the search only


def test_manage_simulate_red_ignores_a_green_report(monkeypatch, capsys, tmp_path):
    leg = {
        "version": "v1.105.0",
        "green": True,
        "note": "",
        "exit_code": 0,
        "counts": {"passed": 10},
        "no_tests": False,
        "tail": "",
    }
    report = _write_report(
        tmp_path / "report.json",
        versions=["v1.97.0", "v1.105.0"],
        all_green=True,
        legs=[dict(leg, version="v1.97.0"), leg],
    )
    gh = FakeGh([
        ok(json.dumps([])),  # issue list: none open
        fail(stderr="label not found"),  # ensure_label: view
        ok(),  # ensure_label: create
        ok("https://github.com/owner/repo/issues/18\n"),
    ])
    monkeypatch.setattr(canary, "run_gh", gh)
    assert canary.manage(_manage_args(tmp_path, report, dry_run=False, simulate="red")) == 0
    out = capsys.readouterr().out
    assert "filed tracking issue #18: compat break: LiteLLM v1.105.0" in out
    assert "simulated red (dispatch rehearsal)" in " ".join(gh.joined())


def test_manage_simulate_green_closes_an_issue_despite_a_red_report(monkeypatch, capsys, tmp_path):
    report = _write_report(tmp_path / "report.json")
    gh = FakeGh([
        ok(json.dumps([{"number": 17, "createdAt": "2026-10-01T00:00:00Z"}])),
        ok(),
        ok(),
    ])
    monkeypatch.setattr(canary, "run_gh", gh)
    assert canary.manage(_manage_args(tmp_path, report, dry_run=False, simulate="green")) == 0
    out = capsys.readouterr().out
    assert "closed tracking issue #17: green again on v1.105.0" in out
    assert any(call.startswith("issue close") for call in gh.joined())


def test_manage_propagates_lifecycle_failures_as_exit_1(tmp_path, monkeypatch, capsys):
    report = _write_report(tmp_path / "report.json")
    open_list = json.dumps([{"number": 17, "createdAt": "2026-10-01T00:00:00Z"}])
    gh = FakeGh([
        ok(open_list),  # dry-run: the search only
        ok(open_list),  # real run: the search again
        fail(stderr="422 unrecoverable"),  # ... whose comment then fails
    ])
    monkeypatch.setattr(canary, "run_gh", gh)
    argv = ["manage", "--report", str(report), "--run-url",
            "https://github.com/owner/repo/actions/runs/42", "--repo", "owner/repo"]
    assert canary.main([*argv, "--dry-run"]) == 0  # dry path untouched by gh errors
    assert canary.main(argv) == 1
    err = capsys.readouterr().err
    assert "gh issue comment failed" in err


def test_manage_multiple_open_trackers_are_a_loud_error(tmp_path, monkeypatch, capsys):
    report = _write_report(tmp_path / "report.json")
    gh = FakeGh([ok(json.dumps([
        {"number": 9, "createdAt": "2026-10-01T00:00:00Z"},
        {"number": 17, "createdAt": "2026-10-02T00:00:00Z"},
    ]))])
    monkeypatch.setattr(canary, "run_gh", gh)
    assert canary.main(["manage", "--report", str(report), "--run-url",
                        "https://github.com/owner/repo/actions/runs/42",
                        "--repo", "owner/repo"]) == 1
    assert "multiple open" in capsys.readouterr().err
