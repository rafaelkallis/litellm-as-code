#!/usr/bin/env python3
"""LiteLLM compatibility-matrix harness (stdlib-only).

WHY THIS EXISTS (AGENTS.md §9): litellm's admin API is only loosely coupled
to semver — endpoints move, response envelopes shift, and server-injected
defaults change between even minor releases. "Green on v1.97.0" therefore
says nothing about v1.104.x, and a single pinned release gate can silently
accept an incompatible proxy. This harness is how we stay honest: for every
requested LiteLLM tag it deploys the TEST-OWNED compose fixture
(``tests/live/proxy/compose.yml``: postgres + litellm-database, throwaway
master key, no inference upstream), waits for the proxy healthcheck, runs
the live integration suite (``pytest tests/live -m "integration and not
slow"``) against that one proxy, tears the stack down again, and collects a
pass/fail matrix.

Examples::

    .venv/bin/python scripts/compat_matrix.py                  # default matrix
    .venv/bin/python scripts/compat_matrix.py --versions v1.97.0 v1.104.2
    .venv/bin/python scripts/compat_matrix.py --versions v1.104.2 --keep

Exit code: 0 iff EVERY requested version is green, 1 otherwise (so CI fails
loudly), 2 on usage errors.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "tests" / "live" / "proxy" / "compose.yml"
LITELLM_CONTAINER = "litellm-as-code-tests-litellm"
DEFAULT_VERSIONS = "v1.97.0,v1.104.2"  # pinned release gate + current stable
DEFAULT_MASTER_KEY = "sk-tests-master-key-change-me"  # fixture's throwaway key
HEALTH_POLL_INTERVAL_S = 3.0
HEALTH_TIMEOUT_S = 300.0  # first boot runs DB migrations
COMPOSE_TIMEOUT_S = 600.0  # image pulls can be slow
PYTEST_TIMEOUT_S = 900.0
PYTEST_TARGET = "tests/live"
PYTEST_MARKER = "integration and not slow"
TAIL_LINES = 60

_TAG_RE = re.compile(r"^v\d+\.\d+(?:\.\d+)?(?:[-+][\w.]+)?$")


def parse_versions(spec: str) -> list[str]:
    """Split a comma- and/or space-separated tag list; validate tag shape."""
    tags: list[str] = []
    for raw in re.split(r"[,\s]+", spec.strip()):
        if not raw:
            continue
        if not _TAG_RE.match(raw):
            raise ValueError(
                f"invalid LiteLLM tag {raw!r} — expected 'v<major>.<minor>[.patch]' (e.g. v1.97.0)"
            )
        if raw not in tags:
            tags.append(raw)
    if not tags:
        raise ValueError("no versions requested")
    return tags


def parse_set_overrides(pairs: Iterable[str]) -> dict[str, str]:
    """Turn repeated --set K=V strings into an env dict."""
    env: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"--set expects K=V, got {pair!r}")
        env[key] = value
    return env


def _run_capture(
    cmd: list[str],
    env: dict[str, str],
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _stack_env(
    port: int,
    version: str,
    master_key: str,
    extra_env: dict[str, str],
) -> dict[str, str]:
    """Env for the compose stack (and, with base URL added, for pytest).

    --set extras are applied FIRST: the harness-owned values below win, so a
    stray --set LITELLM_VERSION/PORT cannot desynchronize a matrix leg.
    """
    env = dict(os.environ)
    env.update(extra_env)
    env["LITELLM_VERSION"] = version
    env["LITELLM_PORT"] = str(port)
    env["LITELLM_MASTER_KEY"] = master_key
    return env


def _compose(env: dict[str, str], *args: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return _run_capture(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        env=env,
        timeout=timeout,
    )


def _wait_healthy(env: dict[str, str]) -> bool:
    deadline_at = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline_at:
        probe = _run_capture(
            ["docker", "inspect", "-f", "{{.State.Health.Status}}", LITELLM_CONTAINER],
            env=env,
        )
        if probe.returncode == 0 and probe.stdout.strip() == "healthy":
            return True
        time.sleep(HEALTH_POLL_INTERVAL_S)
    return False


@dataclass
class LegResult:
    """Outcome of one matrix version."""

    version: str
    green: bool = False
    note: str = ""  # why not green ("" on green)
    exit_code: int | None = None  # None = pytest never ran
    counts: dict[str, int] = field(default_factory=dict)
    no_tests: bool = False
    tail: str = ""  # last TAIL_LINES lines of pytest stdout (on failure)


# pytest -q summary line, e.g. "6 passed, 4 skipped in 1.2s" / "1 failed, 3 passed, 1 error in ..."
_COUNT_RES: dict[str, re.Pattern[str]] = {
    "passed": re.compile(r"(\d+)\s+passed"),
    "failed": re.compile(r"(\d+)\s+failed"),
    "errors": re.compile(r"(\d+)\s+errors?\b"),
    "skipped": re.compile(r"(\d+)\s+skipped"),
}


def _extract_summary(output: str) -> tuple[dict[str, int], bool]:
    """Pull (counts, no_tests_ran) out of pytest's (q)uite output."""
    counts = {key: 0 for key in _COUNT_RES}
    no_tests = "no tests ran" in output
    summary = None
    for line in reversed(output.splitlines()):
        if "no tests ran" in line or re.search(
            r"\d+\s+(?:passed|failed|errors?\b|skipped)", line
        ):
            summary = line
            break
    if summary:
        for key, pattern in _COUNT_RES.items():
            match = pattern.search(summary)
            if match:
                counts[key] = int(match.group(1))
    return counts, no_tests


def _tail(text: str | None) -> str:
    return "\n".join((text or "").splitlines()[-TAIL_LINES:])


def run_leg(
    index: int,
    total: int,
    version: str,
    port: int,
    master_key: str,
    extra_env: dict[str, str],
    pytest_extra: list[str],
) -> LegResult:
    """Run one matrix version: boot -> health -> live suite -> teardown."""
    print(f"\n[{index}/{total}] LiteLLM {version}: booting test stack on port {port} ...", flush=True)
    result = LegResult(version=version)
    stack_env = _stack_env(port, version, master_key, extra_env)

    up = _compose(stack_env, "up", "-d", timeout=COMPOSE_TIMEOUT_S)
    if up.returncode != 0:
        result.note = f"compose up failed (exit {up.returncode})"
        result.tail = _tail(up.stdout + up.stderr)
        print(result.tail, file=sys.stderr, flush=True)
        return result

    if not _wait_healthy(stack_env):
        logs = _compose(stack_env, "logs", "--tail", "50", "litellm")
        result.note = f"proxy never became healthy within {int(HEALTH_TIMEOUT_S)}s"
        result.tail = _tail(logs.stdout + logs.stderr)
        print(result.tail, file=sys.stderr, flush=True)
        return result

    print(f"[{index}/{total}] LiteLLM {version}: proxy healthy — running live suite ...", flush=True)
    test_env = dict(stack_env)
    test_env["LITELLM_BASE_URL"] = f"http://localhost:{port}"
    test_env["LITELLM_API_KEY"] = master_key
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        PYTEST_TARGET,
        "-m",
        PYTEST_MARKER,
        "-q",
        *pytest_extra,
    ]
    try:
        proc = _run_capture(cmd, env=test_env, timeout=PYTEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        result.note = f"pytest timed out after {int(PYTEST_TIMEOUT_S)}s"
        return result

    result.exit_code = proc.returncode
    result.counts, result.no_tests = _extract_summary(proc.stdout)
    result.tail = _tail(proc.stdout)
    if result.no_tests:
        result.note = "pytest reported 'no tests ran' — a vacuous matrix leg is a failure"
    elif proc.returncode != 0:
        result.note = f"pytest exited {proc.returncode}"
    result.green = result.exit_code == 0 and not result.no_tests
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compat_matrix",
        description=(
            "Run the live integration suite against several pinned LiteLLM proxy "
            "versions (deploy via tests/live/proxy/compose.yml, one version at a "
            "time) and print a pass/fail compatibility matrix."
        ),
    )
    parser.add_argument(
        "--versions",
        default=DEFAULT_VERSIONS,
        help="comma- and/or space-separated LiteLLM tags (default: v1.97.0,v1.104.2)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=4001,
        help="host port for the proxy stack (default: 4001)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the last run version's stack up for debugging (prints how to "
        "reach it) instead of tearing it down after that version",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop at the first red version instead of running the whole matrix",
    )
    parser.add_argument(
        "--pytest-args",
        default="",
        help='extra pytest arguments appended after the marker and -q '
        '(e.g. --pytest-args "--deselect tests/live/test_live_export.py::test_export_then_reapply_is_noop")',
    )
    parser.add_argument(
        "--set",
        dest="set_overrides",
        action="append",
        default=[],
        metavar="K=V",
        help="extra env var forwarded to BOTH the compose stack and pytest (repeatable)",
    )
    return parser


def _print_summary(results: list[LegResult], not_run: list[str]) -> bool:
    """Print the matrix table + verdict; return True (all green) or False."""
    print("\nLiteLLM compatibility matrix")
    print("-" * 72)
    header = (
        f"{'version':<14}{'result':<8}{'passed':>8}{'failed':>8}{'errors':>8}{'skipped':>9}"
    )
    print(header)
    print("-" * 72)
    for result in results:
        counts = result.counts
        row = (
            f"{result.version:<14}"
            f"{'PASS' if result.green else 'FAIL':<8}"
            f"{counts.get('passed', 0):>8}"
            f"{counts.get('failed', 0):>8}"
            f"{counts.get('errors', 0):>8}"
            f"{counts.get('skipped', 0):>9}"
        )
        if not result.green and result.note:
            row += f"   ({result.note})"
        print(row)
    for version in not_run:
        print(f"{version:<14}{'N/A':<8}{'-':>8}{'-':>8}{'-':>8}{'-':>9}   (not run)")
    print("-" * 72)
    all_green = bool(results) and all(result.green for result in results)
    green = sum(1 for result in results if result.green)
    print(f"verdict: {green}/{len(results)} green — {'PASS' if all_green else 'FAIL'}")
    return all_green


def run_matrix(
    port: int,
    versions: list[str],
    master_key: str,
    extra_env: dict[str, str],
    pytest_extra: list[str],
    keep: bool,
    fail_fast: bool,
) -> int:
    if extra_env:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(extra_env.items()))
        print(f"LiteLLM compatibility matrix: {', '.join(versions)} on port {port} (extra env: {rendered})")
    else:
        print(f"LiteLLM compatibility matrix: {', '.join(versions)} on port {port}")

    results: list[LegResult] = []
    kept_version: str | None = None
    for index, version in enumerate(versions, 1):
        stack_env = _stack_env(port, version, master_key, extra_env)
        result = run_leg(index, len(versions), version, port, master_key, extra_env, pytest_extra)
        results.append(result)
        counts = result.counts
        status = "PASS" if result.green else "FAIL"
        print(
            f"[{index}/{len(versions)}] LiteLLM {version}: {status} (exit {result.exit_code}, "
            f"passed {counts.get('passed', 0)}, failed {counts.get('failed', 0)}, "
            f"errors {counts.get('errors', 0)}, skipped {counts.get('skipped', 0)})"
            + ("" if result.green else f" — {result.note}"),
            flush=True,
        )
        if not result.green:
            print(result.tail, file=sys.stderr, flush=True)
        # Intermediate legs must always come down (the container name is fixed,
        # so the next version — and the user's other stacks on other ports —
        # start clean). Only the last leg stays up under --keep; under
        # --fail-fast the red leg's stack is kept for debugging.
        keep_up = keep and (index == len(versions) or (fail_fast and not result.green))
        if keep_up:
            kept_version = version
        else:
            _compose(stack_env, "down", "-v")
        if not result.green and fail_fast:
            break

    if kept_version is not None:
        print(
            f"\n--keep: the {kept_version} stack is still up for debugging "
            f"(container {LITELLM_CONTAINER}).\n"
            f"point the suite at it:  LITELLM_BASE_URL=http://localhost:{port} "
            f"LITELLM_API_KEY={master_key}\n"
            f"tear it down with:      docker compose -f tests/live/proxy/compose.yml down -v"
        )
    not_run = versions[len(results):] if fail_fast else []
    all_green = _print_summary(results, not_run)
    return 0 if all_green else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        versions = parse_versions(args.versions)
        extra_env = parse_set_overrides(args.set_overrides)
        pytest_extra = shlex.split(args.pytest_args)
    except ValueError as exc:
        parser.error(str(exc))
    master_key = os.environ.get("LITELLM_MASTER_KEY", DEFAULT_MASTER_KEY)
    # The version and port come only from the harness (one leg at a time).
    extra_env.pop("LITELLM_PORT", None)
    return run_matrix(
        port=args.port,
        versions=versions,
        master_key=master_key,
        extra_env=extra_env,
        pytest_extra=pytest_extra,
        keep=args.keep,
        fail_fast=args.fail_fast,
    )


if __name__ == "__main__":
    sys.exit(main())
