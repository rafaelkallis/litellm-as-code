#!/usr/bin/env python3
"""Nightly compat-canary glue (stdlib-only): tag resolve + tracking issue.

WHY THIS EXISTS (issue #25): upstream ships a new LiteLLM minor roughly every
6-7 days, but a break discovered on their schedule only surfaces in this repo
when somebody next pushes. This script is the early-warning glue between

- the compat-matrix harness (``scripts/compat_matrix.py``), which re-probes
  the live suite against a pair of proxy versions and (since ``--report-out``)
  writes a machine-readable JSON report, and
- ONE refreshable GitHub tracking issue (label ``compat-canary``) that says
  whether litellm-as-code converges on the current stable generation.

It deliberately does NOT boot anything (the harness owns docker) and owns NO
persisted state (the open issue IS the state; AGENTS.md forbids applied-state
files). All GitHub interaction goes through the ``gh`` CLI (auth stems from
``GITHUB_TOKEN`` in CI, from your ``gh auth`` locally)::

    .venv/bin/python scripts/compat_canary.py resolve
    .venv/bin/python scripts/compat_canary.py manage \
        --report reports/nightly.json --run-url "https://..."
    .venv/bin/python scripts/compat_canary.py manage --report ... --dry-run
    .venv/bin/python scripts/compat_canary.py manage --report ... --simulate red

Lifecycle, decided purely by (matrix verdict, open canary issue?):

    red   & no issue  -> CREATE one tracking issue (table, pytest tails, run
                         URL); title names the first red leg (the canary on
                         an upstream break, the floor on a regression bug)
    red   & one issue -> COMMENT "still red on <tag>" + evidence (no dup)
    green & an issue  -> COMMENT + CLOSE with the green run link as evidence
    green & no issue  -> nothing: all-green runs post NOTHING

Exit code: 0 iff the lifecycle achieved its intent (including the green
no-op), 1 when GitHub interaction or the report read fails, 2 on usage
errors (same convention as the harness). Floor-tag maintenance is NOT
automated here (issue #25): bumping the floor pin re-verifies a generation
and stays a deliberate, documented human change.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Reuse the harness's vocabulary (tag regex, report schema) instead of
# duplicating them; the harness is import-friendly (tests load it by path).
_HARNESS_PATH = REPO_ROOT / "scripts" / "compat_matrix.py"
_spec = importlib.util.spec_from_file_location("compat_matrix", _HARNESS_PATH)
assert _spec and _spec.loader  # file-path import: loader is always present
compat_matrix = importlib.util.module_from_spec(_spec)
sys.modules["compat_matrix"] = compat_matrix
_spec.loader.exec_module(compat_matrix)

UPSTREAM = "BerriAI/litellm"
REPO = os.environ.get("GITHUB_REPOSITORY", "rafaelkallis/litellm-as-code")
LABEL = "compat-canary"
CANARY_ENV = "LITELLM_CANARY_TAG"  # resolve: explicit override, still validated
GH_TIMEOUT_S = 60.0
MAX_TAILS_CREATE = 4  # cap evidence in a fresh issue body (bodies cap ~64k)
MAX_TAILS_COMMENT = 2

# Canary tags must be BARE stable tags: 'v<major>.<minor>.<patch>'. The
# harness regex tolerates suffixes (its --versions is human-driven); a canary
# resolved from releases/latest must never be an rc/dev or suffixed tag.
_CANARY_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

GhRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _valid_canary_tag(tag: str) -> bool:
    """A bare stable tag: v<major>.<minor>.<patch>, no suffix, no metadata."""
    return bool(_CANARY_TAG_RE.match(tag))


def run_gh(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one ``gh`` invocation; env passthrough keeps GITHUB_TOKEN working."""
    return subprocess.run(
        ["gh", *argv],
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        timeout=GH_TIMEOUT_S,
    )


def _fail(what: str, proc: subprocess.CompletedProcess[str]) -> RuntimeError:
    """RuntimeError with gh's own stderr/detail (testable failure contract)."""
    detail = proc.stderr.strip() or f"gh exited {proc.returncode}"
    return RuntimeError(f"{what}: {detail}")


# --- resolve: today's latest stable LiteLLM tag ---------------------------


def stable_tag_from_release(release: dict[str, object]) -> str | None:
    """Tag of a ``/releases`` entry iff it is a published bare stable.

    Stable = not a draft, not marked prerelease, and a bare ``vX.Y.Z`` tag
    (``_CANARY_TAG_RE``). rc/dev tags like ``v1.106.0-rc.1`` are typically
    both flagged prerelease upstream AND rejected by the shape check as a
    backstop — the canary gets docker-pulled, so suffixes are never safe.
    """
    if release.get("draft") or release.get("prerelease"):
        return None
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not _valid_canary_tag(tag):
        return None
    return tag


def resolve_latest_stable(run: GhRunner) -> str:
    """Latest stable LiteLLM tag: releases/latest, else first stable in list.

    Raises RuntimeError with actionable text on every failure path; never
    returns a prerelease, draft, or suffixed tag.
    """
    latest = run(["api", f"repos/{UPSTREAM}/releases/latest", "--jq", ".tag_name"])
    if latest.returncode == 0:
        tag = latest.stdout.strip()
        if _valid_canary_tag(tag):
            return tag
    # Fallback: scan the release list newest-first, skipping drafts/RCs.
    # GitHub /releases is newest-first, so the first survivor IS the newest
    # stable — do not "help" by sorting (tag strings sort lexically, not
    # semantically: v1.9 > v1.104 stringwise).
    listing = run(
        [
            "api",
            f"repos/{UPSTREAM}/releases?per_page=100",
            "--jq",
            "[.[] | select((.draft|not) and (.prerelease|not)) | .tag_name]",
        ]
    )
    if listing.returncode != 0:
        raise _fail(
            "could not resolve the latest stable LiteLLM tag via gh "
            "(releases/latest also failed)", listing
        )
    try:
        tags = json.loads(listing.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"unparseable LiteLLM release list from gh: {exc}") from exc
    if not isinstance(tags, list):
        raise RuntimeError("unparseable LiteLLM release list from gh: not a list")
    for tag in tags:
        if isinstance(tag, str) and _valid_canary_tag(tag):
            return tag
    raise RuntimeError("no stable release tag found in the LiteLLM releases list")


# --- manage: the compat-canary tracking-issue lifecycle -------------------


@dataclass
class CanaryReport:
    """What the lifecycle needs out of a harness report file."""

    versions: list[str]
    all_green: bool
    failed_legs: list[dict[str, object]]
    canary: str  # the proxy generation this lifecycle action talks about


def read_report(path: Path) -> CanaryReport:
    """Parse + validate a ``compat_matrix.py --report-out`` file.

    The verdict is re-derived from the legs; the report's own ``all_green``
    must agree (a contradiction is a harness bug — refuse to act rather than
    acting on it; deriving red-on-disagreement would e.g. misfile an issue on
    a truncated report). ``canary`` = the FIRST red leg's version when red
    (that is the break the issue title names — canary on an upstream break,
    floor on a self-inflicted regression), else the LAST requested version:
    the nightly always passes the pair floor-first, canary last.
    """
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read compat-matrix report {path}: {exc}") from exc
    if not isinstance(report, dict) or report.get("harness") != "compat_matrix":
        raise RuntimeError(f"{path} is not a compat-matrix report")
    versions = report.get("versions")
    legs = report.get("legs")
    if (
        not isinstance(versions, list)
        or not versions
        or not all(isinstance(v, str) for v in versions)
        or not isinstance(legs, list)
        or not legs
        or not all(isinstance(leg, dict) for leg in legs)
    ):
        raise RuntimeError(f"{path}: malformed report (versions/legs)")
    claimed = report.get("all_green")
    if not isinstance(claimed, bool):
        raise RuntimeError(f"{path}: malformed 'all_green'")
    failed = [leg for leg in legs if not leg.get("green")]
    if bool(claimed) != (not failed):
        raise RuntimeError(f"{path}: 'all_green'={claimed} contradicts the legs")
    canary = str(failed[0]["version"]) if failed else versions[-1]
    return CanaryReport(versions, not failed, failed, canary)


def open_canary_issue(run: GhRunner, repo: str, label: str = LABEL) -> int | None:
    """Number of the single open tracking issue, or None when none is open.

    Read-only (safe under --dry-run). Two+ open trackers are an invariant
    violation we refuse to guess around: the lifecycle never duplicates an
    issue silently, and a stale surplus must be resolved by a human.
    """
    proc = run(
        [
            "issue", "list", "-R", repo, "--state", "open", "--label", label,
            "--json", "number,createdAt",
        ]
    )
    if proc.returncode != 0:
        raise _fail("gh issue list failed", proc)
    try:
        issues = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"unparseable gh issue list output: {exc}") from exc
    if not isinstance(issues, list) or not all(isinstance(i, dict) for i in issues):
        raise RuntimeError("unparseable gh issue list output: not a list of issues")
    numbers = [
        issue["number"]
        for issue in issues
        if isinstance(issue.get("number"), int) and isinstance(issue.get("createdAt"), str)
    ]
    numbers.sort()  # oldest-first: with (impossible-by-design) duplicates, the
    # oldest tracker is the one the refresh comment belongs to.
    if len(numbers) > 1:
        raise RuntimeError(
            f"multiple open '{label}' issues found ({numbers}) — close the "
            "stale one by hand before the next nightly run"
        )
    return numbers[0] if numbers else None


def ensure_label(run: GhRunner, repo: str, label: str = LABEL) -> None:
    """Create the tracking label if absent — and touch nothing if it exists.

    View-then-create (never ``--force``): --force would rewrite an existing
    label's color/description on every filing, a silent mutation we won't
    make. Forks of this repo need the create path; trackers here never do.
    """
    proc = run(["label", "view", repo, label])
    if proc.returncode == 0:
        return
    combined = (proc.stdout + proc.stderr).lower()
    if "not found" not in combined:
        raise _fail("gh label view failed", proc)
    created = run(
        [
            "label", "create", repo, label, "--color", "D93F0B",
            "--description", "nightly compat canary: upstream break under investigation",
        ]
    )
    if created.returncode != 0 and "already exists" not in created.stdout + created.stderr:
        raise _fail("gh label create failed", created)


def _cell(value: str) -> str:
    """Escape one markdown-table cell (harness notes may contain pipes/LFs)."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_matrix_table(raw_report: dict[str, object]) -> str:
    """The harness's summary table, rebuilt in markdown from the report."""
    leg_list = raw_report.get("legs")
    legs = leg_list if isinstance(leg_list, list) else []
    not_run = raw_report.get("not_run")
    versions = not_run if isinstance(not_run, list) else []
    rows = [
        "| version | result | passed | failed | errors | skipped | note |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        counts = leg.get("counts")
        counts = counts if isinstance(counts, dict) else {}
        numbers = [counts.get(key) for key in ("passed", "failed", "errors", "skipped")]
        note = leg.get("note")
        rows.append(
            "| {version} | {result} | {passed} | {failed} | {errors} | {skipped} | {note} |".format(
                version=_cell(str(leg.get("version", "?"))),
                result="PASS" if leg.get("green") else "FAIL",
                passed=numbers[0] if isinstance(numbers[0], int) else 0,
                failed=numbers[1] if isinstance(numbers[1], int) else 0,
                errors=numbers[2] if isinstance(numbers[2], int) else 0,
                skipped=numbers[3] if isinstance(numbers[3], int) else 0,
                note=_cell(note if isinstance(note, str) else ""),
            )
        )
    for version in versions:
        rows.append(f"| {version} | NOT RUN | - | - | - | - | (fail-fast cut) |")
    return "\n".join(rows)


def _render_tails(failed_legs: list[dict[str, object]], cap: int) -> str:
    """Failing legs' pytest tails as collapsed <details> blocks (capped)."""
    blocks = []
    for leg in failed_legs[:cap]:
        note = leg.get("note")
        prefix = (
            f" (NOTE: {_cell(note)})"
            if isinstance(note, str) and note
            else ""
        )
        tail = str(leg.get("tail") or "").rstrip() or "(no tail captured)"
        blocks.append(
            f"<details><summary>pytest tail — {leg.get('version', '?')} "
            f"(exit {leg.get('exit_code')}){prefix}</summary>\n\n"
            f"```\n{tail}\n```\n</details>"
        )
    if len(failed_legs) > cap:
        blocks.append("Further failing legs omitted (see the run link below the matrix).")
    return "\n\n".join(blocks)


def red_title(failed_legs: list[dict[str, object]]) -> str:
    """Tracking-issue title — names the FIRST red leg (boundary of the break)."""
    return f"compat break: LiteLLM {failed_legs[0].get('version', '?')} (nightly canary red)"


def red_body(raw_report: dict[str, object], failed_legs: list[dict[str, object]], run_url: str) -> str:
    """The evidence body of a freshly filed tracking issue."""
    return (
        "The nightly canary is **red** — litellm-as-code does not converge on "
        "the proxy version(s) below (floor pin "
        f"{compat_matrix.DEFAULT_VERSIONS.split(',')[0]}).\n\n"
        f"- run: {run_url}\n"
        "- auto-filed by `scripts/compat_canary.py` (the #25 nightly "
        "lifecycle); this issue is refreshed nightly and auto-closes with a "
        "green run link.\n\n"
        f"### matrix\n\n{render_matrix_table(raw_report)}\n\n"
        f"### evidence\n\n{_render_tails(failed_legs, MAX_TAILS_CREATE)}\n"
    )


def still_red_comment(canary: str, failed_legs: list[dict[str, object]], run_url: str) -> str:
    """Evidence body appended nightly while a break persists."""
    lines = [f"Nightly still red on LiteLLM **{canary}** — run: {run_url}"]
    for leg in failed_legs[:MAX_TAILS_COMMENT]:
        counts = leg.get("counts")
        counts = counts if isinstance(counts, dict) else {}
        note = leg.get("note")
        lines.append(
            "- `{version}`: {failed} failed, {errors} errors{note}".format(
                version=leg.get("version", "?"),
                failed=counts.get("failed", 0) if isinstance(counts.get("failed"), int) else 0,
                errors=counts.get("errors", 0) if isinstance(counts.get("errors"), int) else 0,
                note=f" — {note}" if isinstance(note, str) and note else "",
            )
        )
    return "\n".join(lines) + f"\n\n{_render_tails(failed_legs, MAX_TAILS_COMMENT)}\n"


def recovered_comment(canary: str, run_url: str) -> str:
    """Closing comment: the green run link is the recovery evidence."""
    return (
        f"**Green again** on LiteLLM `{canary}` — run: {run_url}\n\n"
        "Auto-closed as recovered (informational — not a release gate). If "
        "this same generation regresses again, the next red nightly opens a "
        "fresh tracking issue."
    )


def decide_action(green: bool, open_issue: int | None) -> str:
    """Lifecycle decision table (pure; see module docstring)."""
    if green:
        return "noop" if open_issue is None else "close"
    return "create" if open_issue is None else "comment"


def create_tracking_issue(
    run: GhRunner,
    repo: str,
    raw_report: dict[str, object],
    failed_legs: list[dict[str, object]],
    run_url: str,
) -> int:
    """Create the tracking issue; return its number (parsed from the URL)."""
    ensure_label(run, repo)
    proc = run(
        [
            "issue", "create", "-R", repo,
            "--title", red_title(failed_legs),
            "--body", red_body(raw_report, failed_legs, run_url),
            "--label", LABEL,
        ]
    )
    if proc.returncode != 0:
        raise _fail("gh issue create failed", proc)
    for token in proc.stdout.split():
        if "/issues/" in token:
            try:
                return int(token.rstrip("/").rsplit("/", 1)[-1])
            except ValueError:
                break
    raise RuntimeError(f"gh issue create printed no parseable issue URL: {proc.stdout!r}")


def comment_on_issue(run: GhRunner, repo: str, number: int, body: str) -> None:
    proc = run(["issue", "comment", str(number), "-R", repo, "--body", body])
    if proc.returncode != 0:
        raise _fail("gh issue comment failed", proc)


def close_issue(run: GhRunner, repo: str, number: int, comment: str) -> None:
    """Close the tracker as completed, with the green run link as evidence."""
    proc = run(
        [
            "issue", "close", str(number), "-R", repo,
            "--reason", "completed", "--comment", comment,
        ]
    )
    if proc.returncode != 0:
        raise _fail("gh issue close failed", proc)


def manage(args: argparse.Namespace) -> int:
    """Subcommand ``manage``: exactly one lifecycle action, per the verdict.

    --dry-run only skips the WRITES (create/comment/close); the open-issue
    search is read-only and runs regardless so the rehearsal reflects what
    would really happen.
    """
    parsed = read_report(Path(args.report))
    if args.simulate == "red":
        parsed = CanaryReport(
            versions=list(parsed.versions),
            all_green=False,
            failed_legs=(
                list(parsed.failed_legs)
                if parsed.failed_legs
                else [{
                    "version": parsed.canary,
                    "green": False,
                    "note": "simulated red (dispatch rehearsal)",
                    "exit_code": None,
                    "counts": {},
                    "no_tests": False,
                    "tail": "",
                }]
            ),
            canary=parsed.canary,
        )
    elif args.simulate == "green":
        parsed = CanaryReport(list(parsed.versions), True, [], parsed.canary)

    open_issue = open_canary_issue(run_gh, args.repo, args.label)
    action = decide_action(parsed.all_green, open_issue)
    raw_report = json.loads(Path(args.report).read_text(encoding="utf-8"))

    if action == "noop":
        print(
            f"nightly canary green on {parsed.canary}; no open tracker — "
            "nothing posted"
        )
        return 0

    if action == "create":
        title = red_title(parsed.failed_legs)
        body = red_body(raw_report, parsed.failed_legs, args.run_url)
        if args.dry_run:
            print(f"[dry-run] would CREATE issue '{title}' (label {args.label})\n\n{body}")
            return 0
        number = create_tracking_issue(
            run_gh, args.repo, raw_report, parsed.failed_legs, args.run_url
        )
        print(f"filed tracking issue #{number}: {title}")
        print("further red nights refresh this issue instead of duplicating it")
        return 0

    assert open_issue is not None  # decide_action guarantees a number here
    if action == "comment":
        body = still_red_comment(parsed.canary, parsed.failed_legs, args.run_url)
        if args.dry_run:
            print(f"[dry-run] would COMMENT on #{open_issue} (still red)\n\n{body}")
            return 0
        comment_on_issue(run_gh, args.repo, open_issue, body)
        print(f"refreshed tracking issue #{open_issue} (still red on {parsed.canary})")
        return 0

    # close
    body = recovered_comment(parsed.canary, args.run_url)
    if args.dry_run:
        print(f"[dry-run] would CLOSE #{open_issue} as recovered\n\n{body}")
        return 0
    comment_on_issue(run_gh, args.repo, open_issue, body)
    close_issue(run_gh, args.repo, open_issue, body)
    print(f"closed tracking issue #{open_issue}: green again on {parsed.canary}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compat_canary",
        description=(
            "Nightly compat-canary glue: resolve today's latest stable "
            "LiteLLM tag, and manage the 'compat-canary' tracking issue from "
            "a compat-matrix report (scripts/compat_matrix.py --report-out)."
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser(
        "resolve", help="print the latest stable LiteLLM tag (one line)"
    )

    p_manage = sub.add_parser(
        "manage", help="run one lifecycle action from a compat-matrix report"
    )
    p_manage.add_argument(
        "--report", required=True, metavar="PATH",
        help="compat-matrix report (compat_matrix.py --report-out)",
    )
    p_manage.add_argument(
        "--run-url", required=True, metavar="URL",
        help="URL of the nightly run, embedded into the evidence",
    )
    p_manage.add_argument(
        "--repo", default=REPO, metavar="OWNER/REPO",
        help="repository owning the tracking issue (default: GITHUB_REPOSITORY)",
    )
    p_manage.add_argument(
        "--label", default=LABEL, metavar="NAME",
        help="tracking-issue label (default: compat-canary)",
    )
    p_manage.add_argument(
        "--simulate", choices=["red", "green"], default=None,
        help="override the verdict for lifecycle rehearsal (no docker needed)",
    )
    p_manage.add_argument(
        "--dry-run", action="store_true",
        help="print the action + evidence body; write nothing to GitHub",
    )
    return parser


def resolve_subcommand() -> int:
    """Print the resolved canary tag; the workflow feeds it to --versions."""
    override = os.environ.get(CANARY_ENV, "").strip()
    if override:  # explicit override wins — but never silently ships junk
        if not _valid_canary_tag(override):
            raise RuntimeError(
                f"${CANARY_ENV}={override!r} is not a bare stable tag (vX.Y.Z)"
            )
        print(override)
        return 0
    print(resolve_latest_stable(run_gh))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.subcommand == "resolve":
            return resolve_subcommand()
        return manage(args)
    except RuntimeError as exc:
        print(f"compat_canary: {exc}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        # the report was readable a moment ago (read_report passed); a vanish
        # or corruption between the two reads must fail loudly, not traceback
        print(f"compat_canary: report unreadable mid-run: {exc}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("compat_canary: gh timed out", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
