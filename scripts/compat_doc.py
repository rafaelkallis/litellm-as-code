"""COMPAT.md generator & lint (issue #30).

COMPAT.md is the single answer to "can I run litellm-as-code against proxy
vX?" (AGENTS.md §9). Its facts are **checked content**: derived or verified
against repo state every run, so a stale doc fails CI instead of shipping.

What is checked, and against what source of truth:

- the **verified block** is derived from the committed last-known-good
  OpenAPI sidecar (``tests/live/compat/openapi-last-known-good.meta.json``);
  the nightly canary refreshes (and re-commits) that sidecar itself on every
  green, drift-free run, and the workflow regenerates this block in the same
  commit — the doc cannot drift more than one nightly behind reality.
- the **caveats table** is rendered from the ``CAVEATS`` ledger below. Each
  row's evidence refs are ``path -> marker`` pairs (a repo file plus a
  literal string that must appear in it), so a refactor that removes the
  referenced semantics fails the lint until the ledger is re-pointed.
- the **floor pin** and the **matrix legs** are cross-checked against the
  workflows (publish-pypi.yml, publish-image.yml, compat-matrix.yml,
  compat-nightly.yml), so the doc cannot claim a different floor than the
  release gate pins.

Usage (also mirrored by tests/test_compat_doc.py, which calls the functions
directly — no shell involved):

    python scripts/compat_doc.py build   # regenerate the generated blocks
    python scripts/compat_doc.py check   # lint staleness + evidence refs
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPAT_PATH = REPO / "COMPAT.md"
SIDECAR_PATH = REPO / "tests" / "live" / "compat" / "openapi-last-known-good.meta.json"
FILINGS_PATH = REPO / "FILINGS.md"

WF_PUBLISH_PYPI = REPO / ".github" / "workflows" / "publish-pypi.yml"
WF_PUBLISH_IMAGE = REPO / ".github" / "workflows" / "publish-image.yml"
WF_MATRIX = REPO / ".github" / "workflows" / "compat-matrix.yml"
WF_NIGHTLY = REPO / ".github" / "workflows" / "compat-nightly.yml"

BEGIN_VERIFIED = "<!-- BEGIN GENERATED: verified — scripts/compat_doc.py; do not edit -->"
END_VERIFIED = "<!-- END GENERATED: verified -->"
BEGIN_CAVEATS = "<!-- BEGIN GENERATED: caveats — scripts/compat_doc.py; do not edit -->"
END_CAVEATS = "<!-- END GENERATED: caveats -->"

NIGHTLY_BADGE_URL = (
    "https://github.com/rafaelkallis/litellm-as-code/actions/workflows/"
    "compat-nightly.yml/badge.svg"
)

# ---------------------------------------------------------------------------
# Caveats ledger: one entry per absorbed upstream behavior. Keep rows
# version-annotated as far as they were actually probed — everything in
# COMPAT.md cites evidence, not lore (issue #30).
#
# `refs` are (repo_relative_path, literal_marker) pairs — the lint asserts the
# file exists AND the marker still appears in it. `links` are evidence URLs
# (issues/PRs/commits) appended to the row's Evidence cell.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Caveat:
    id: str
    name: str
    affects: str
    behavior: str
    absorbed: str
    refs: tuple[tuple[str, str], ...] = ()
    links: tuple[tuple[str, str], ...] = field(default=())  # (text, url)


CAVEATS: tuple[Caveat, ...] = (
    Caveat(
        id="costs-persist-dropped",
        name="Per-deployment cost persistence dropped (>= 1.102)",
        affects="LiteLLM >= 1.102.0. Floor v1.97.0 (and < 1.102) persists.",
        behavior=(
            "`input_cost_per_token` / `output_cost_per_token` sent to"
            " `POST /model/new` and `PATCH /model/{id}/update` are accepted"
            " but not persisted; `/model/info` reads them back as null ("
            "pricing is server-derived from the proxy's cost map)."
        ),
        absorbed=(
            "When the `model_costs_persist` hint announces >= 1.102 the two"
            " cost keys are declared server-owned for the run"
            " (`SERVER_DERIVED_PRICING`): cost-only drift becomes an accepted"
            " NOOP, mixed cost+real drift keeps its writable part. Probe"
            " unknown -> apply-then-verify fallback."
        ),
        refs=(
            ("litellm_as_code/capabilities.py", "_COSTS_PERSIST_BELOW = (1, 102, 0)"),
            (
                "litellm_as_code/resources/models.py",
                "SERVER_DERIVED_PRICING — distinguishing marker",
            ),
            ("litellm_as_code/classification.py", "def classify_survivors("),
        ),
        links=(
            (
                "capabilities 76cb0fec1",
                "https://github.com/BerriAI/litellm/commit/76cb0fec1",
            ),
            ("#27", "https://github.com/rafaelkallis/litellm-as-code/issues/27"),
            ("PR #34", "https://github.com/rafaelkallis/litellm-as-code/pull/34"),
            ("PR #36", "https://github.com/rafaelkallis/litellm-as-code/pull/36"),
        ),
    ),
    Caveat(
        id="org-enterprise-gate",
        name="/organization/* enterprise gate (>= 1.102, unlicensed)",
        affects=(
            ">= 1.102 on unlicensed proxies (403). Floor v1.97.0 ungated; a"
            " LICENSED >= 1.102 proxy exposes /organization."
        ),
        behavior=(
            "Every `/organization/*` call answers HTTP 403 with an"
            " enterprise/premium body; the OpenAPI doc still advertises the"
            " router, so the gate is only discoverable by probing."
        ),
        absorbed=(
            "Behavior-probed on the first org call (`ORG_GATE_MARKERS` ="
            " 403 + enterprise body): export degrades with a visible notice;"
            " apply hard-errors only when the spec declares organizations."
        ),
        refs=(
            (
                "litellm_as_code/capabilities.py",
                'ORG_GATE_MARKERS = ("403", "enterprise")',
            ),
        ),
        links=(
            ("PR #24", "https://github.com/rafaelkallis/litellm-as-code/pull/24"),
            ("#27", "https://github.com/rafaelkallis/litellm-as-code/issues/27"),
        ),
    ),
    Caveat(
        id="team-admin-role-enterprise",
        name="Team member role `admin` is enterprise-only",
        affects=(
            "OSS/self-hosted unlicensed proxies of every probed generation"
            " (v1.97.0 and up)."
        ),
        behavior=(
            "Assigning team admins is a premium feature — role `admin` in"
            " `members_with_roles` is rejected."
        ),
        absorbed=(
            "OSS-convergent fixtures declare role `user`; variant A/B/C are"
            " opt-in on licensed proxies and the matrix documents the skips."
        ),
        refs=(
            (
                "tests/live/README.md",
                "Assigning team admins is a premium feature",
            ),
        ),
        links=(),
    ),
    Caveat(
        id="member-add-ghost-upsert",
        name="/organization/member_add upserts a ghost user row",
        affects="v1.97.0 (probed live); presumed on later generations.",
        behavior=(
            "Adding an organization member for an unknown user_id does not"
            " reject — the proxy upserts an unmanaged user row (an alias/"
            "email-less ghost, role from the call)."
        ),
        absorbed=(
            "The Users stage runs before org members, so a membership always"
            " attaches to a row the run owns and just wrote — the spec-"
            "declared row stays authoritative (version-independent)."
        ),
        refs=(
            (
                "litellm_as_code/reconciler.py",
                "UPSERTS an unmanaged ghost user row",
            ),
        ),
        links=(("#12", "https://github.com/rafaelkallis/litellm-as-code/issues/12"),),
    ),
    Caveat(
        id="org-new-requires-model",
        name="`POST /organization/new` needs >= 1 configured model",
        affects="v1.97.0 (probed live on a fresh proxy).",
        behavior=(
            "Creating an organization fails (\"No models configured\") until"
            " at least one model exists on the proxy."
        ),
        absorbed=(
            "Stage order: models -> credentials -> organizations, so a spec"
            " that declares both converges on a fresh proxy."
        ),
        refs=(
            (
                "litellm_as_code/reconciler.py",
                "at least one model configured",
            ),
        ),
        links=(("#23", "https://github.com/rafaelkallis/litellm-as-code/issues/23"),),
    ),
    Caveat(
        id="server-injected-read-defaults",
        name="Server-injected defaults/metadata on reads",
        affects=(
            "Observed on v1.104.2 matrix legs (PR #24); present across"
            " generations in various fields."
        ),
        behavior=(
            "Reads inject state the operator never declared: a"
            " `server-internal default_user_id row`, `budget_duration`,"
            " `mode` inference, tpm/rpm metadata interns (`tpm_limit_type`/"
            "`rpm_limit_type`)."
        ),
        absorbed=(
            "Per-resource COMPARABLE lists exclude them; the exporter drops"
            " the server-internal default_user_id row; member defaults are"
            " realigned on both compare sides."
        ),
        refs=(
            (
                "litellm_as_code/diff.py",
                "server defaults & runtime metrics",
            ),
            (
                "litellm_as_code/exporter.py",
                "server-internal default_user_id row",
            ),
            ("litellm_as_code/resources/roles.py", "Only fields the server actually echoes"),
        ),
        links=(
            ("PR #24", "https://github.com/rafaelkallis/litellm-as-code/pull/24"),
        ),
    ),
    Caveat(
        id="unset-scalars-read-null",
        name="Unset limit scalars read back explicit null",
        affects="v1.97.0 (live-verified); fixture-replayed on v1.104.2.",
        behavior=(
            "Every unset budget-table limit and team/key `max_budget` reads"
            " back as explicit `null` — never `0`, never absent."
        ),
        absorbed=(
            "No `None == 0.0` tolerance exists (`equiv` adds empty-"
            "collection tolerance only); diffing tolerates the nulls as-is."
        ),
        refs=(
            (
                "litellm_as_code/diff.py",
                "Deliberately no scalar-default tolerance",
            ),
        ),
        links=(("#12", "https://github.com/rafaelkallis/litellm-as-code/issues/12"),),
    ),
    Caveat(
        id="key-list-pagination",
        name="`/key/list` pagination ignores `page_size`, uses `size`",
        affects="Upstream bug; observed on v1.97.0 and v1.104.2.",
        behavior=(
            "A `page_size` query param is ignored entirely (smallest page"
            " silently served); the endpoint reads `size` instead."
        ),
        absorbed=(
            "The client pins the page-size param per endpoint: `size` on"
            " `/key/list`, the documented `page_size` everywhere else."
        ),
        refs=(
            (
                "litellm_as_code/api.py",
                "ignores a page_size query param entirely",
            ),
        ),
        links=(),
    ),
    Caveat(
        id="member-role-echo",
        name="Member roles always echo; defaults realigned",
        affects="v1.97.0 (live-verified, roles.py scope note).",
        behavior=(
            "Membership roles echo back verbatim (team `role`, org"
            " `user_role`); a diff on the raw echoes would pin the server's"
            " own default."
        ),
        absorbed=(
            "The default role is resolved and aligned on BOTH compare sides"
            " so the realignment cannot erase a genuine drifted value."
        ),
        refs=(
            (
                "litellm_as_code/resources/roles.py",
                "Only fields the server actually echoes",
            ),
        ),
        links=(("#12", "https://github.com/rafaelkallis/litellm-as-code/issues/12"),),
    ),
    Caveat(
        id="cost-echo-unit-variance",
        name="`/model/info` cost echo unit variance on DB-backed models",
        affects="All probed generations with `store_model_in_db: true`.",
        behavior=(
            "Costs echo in two units: a recomputed per-token value (often 0"
            " when the cost table is not loaded) plus the authoritative"
            " per-million DB columns."
        ),
        absorbed=(
            "Model costs compare per-token against per-million/1e6, with"
            " numeric tolerance (`0 == 0.0`, int/float); both units count as"
            " an equal echo."
        ),
        refs=(
            (
                "litellm_as_code/resources/models.py",
                "def _per_token_equal(",
            ),
        ),
        links=(("#24", "https://github.com/rafaelkallis/litellm-as-code/pull/24"),),
    ),
)

# ---------------------------------------------------------------------------


def _workflow_floor(path: Path, pattern: str) -> str | None:
    m = re.search(pattern, path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def publish_pins() -> dict[str, str | None]:
    """Floor pin + matrix legs + nightly floor, parsed from the workflows."""
    return {
        "pypi_pin": _workflow_floor(WF_PUBLISH_PYPI, r"LITELLM_VERSION:\s*(v\d+\.\d+\.\d+)"),
        "image_pin": _workflow_floor(WF_PUBLISH_IMAGE, r"LITELLM_VERSION:\s*(v\d+\.\d+\.\d+)"),
        "matrix_legs": _workflow_floor(
            WF_MATRIX, r"litellm-version:\s*\[([^\]]+)\]"
        ),
        "nightly_floor": _workflow_floor(
            WF_NIGHTLY, r'--versions\s*"(v[\d.]+),\$\{\{'
        ),
    }


def sidecar_facts() -> dict[str, str]:
    """Dated verification facts from the nightly's committed sidecar."""
    data = json.loads(SIDECAR_PATH.read_text(encoding="utf-8"))
    version = f"v{data['litellm_version']}"
    return {
        "captured_at": data["captured_at"],
        "version": version,
        "run_url": data["run_url"],
    }


def _escape_cell(text: str) -> str:
    return text.replace("|", "/")


def render_verified() -> str:
    facts = sidecar_facts()
    pins = publish_pins()
    legs = [v.strip() for v in (pins["matrix_legs"] or "").split(",") if v.strip()]
    floor = pins["nightly_floor"] or pins["pypi_pin"] or "?"
    date = facts["captured_at"]
    lines = [
        f"Verified as of **{date}** — the nightly canary ran floor `{floor}`"
        f" and latest stable `{facts['version']}` green:"
        f" [{facts['run_url'].rsplit('/', 1)[-1]}]({facts['run_url']})",
        "",
        "| tier | LiteLLM | what verifies it | where to check |",
        "|---|---|---|---|",
        (
            f"| floor | `{floor}` | publish gates (PyPI + GHCR), nightly floor,"
            " first matrix leg |"
            f" green on the linked run above ({date}) |"
        ),
    ]
    for leg in legs:
        lines.append(
            f"| matrix leg | `{leg}` | PR/push compat matrix"
            " (`.github/workflows/compat-matrix.yml`), per-leg digest artifact |"
            " status of the latest run on the"
            " [workflow page](https://github.com/rafaelkallis/litellm-as-code/actions/workflows/compat-matrix.yml)"
            " |"
        )
    lines.append(
        f"| canary | `{facts['version']}` | nightly canary leg — latest stable"
        " at 04:00 UTC, resolved from `releases/latest` |"
        f" green {date} (run linked above) |"
    )
    lines.append(
        "| unlisted | any other version | assume the admin-API contract of the"
        " nearest verified minor above (the consumed-surface digest watches"
        " structural moves); run the matrix yourself"
        " — `scripts/compat_matrix.py --versions vX.Y.Z` — and [report"
        " reds](https://github.com/rafaelkallis/litellm-as-code/issues/new) |"
        " — |"
    )
    return "\n".join(lines)


def render_caveats() -> str:
    lines = [
        "| caveat | affects | behavior | absorbed as | evidence |",
        "|---|---|---|---|---|",
    ]
    for c in CAVEATS:
        evidence = []
        for path, marker in c.refs:
            evidence.append(f"[`{path}` → `{marker}`]({path})")
        for text, url in c.links:
            evidence.append(f"[{text}]({url})")
        lines.append(
            "| **{name}** | {affects} | {behavior} | {absorbed} | {evidence} |".format(
                name=_escape_cell(c.name),
                affects=_escape_cell(c.affects),
                behavior=_escape_cell(c.behavior),
                absorbed=_escape_cell(c.absorbed),
                evidence=" · ".join(evidence),
            )
        )
    return "\n".join(lines)


def _replace_block(text: str, begin: str, end: str, content: str) -> tuple[str, str]:
    """Swap the block between `begin`/`end` for `content` (newline-framed).

    Returns (new_text, old_between), where old_between is the raw original
    block INCLUDING its framing newlines — staleness compares it against
    `content` after stripping exactly those framing newlines.
    """
    start = text.index(begin)
    stop = text.index(end)
    between = text[start + len(begin) : stop]
    new = text[: start + len(begin)] + "\n" + content + "\n" + text[stop:]
    return new, between


def build() -> bool:
    """Rewrite the generated blocks; True when the file changed."""
    text = COMPAT_PATH.read_text(encoding="utf-8")
    changed = False
    for begin, end, rendered in (
        (BEGIN_VERIFIED, END_VERIFIED, render_verified()),
        (BEGIN_CAVEATS, END_CAVEATS, render_caveats()),
    ):
        new, between = _replace_block(text, begin, end, rendered)
        if between.strip("\n") != rendered:
            changed = True
        text = new
    if changed:
        COMPAT_PATH.write_text(text, encoding="utf-8")
    return changed


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------


def _check_generated_blocks(text: str, problems: list[str]) -> None:
    for begin, end, rendered, label in (
        (BEGIN_VERIFIED, END_VERIFIED, render_verified(), "verified"),
        (BEGIN_CAVEATS, END_CAVEATS, render_caveats(), "caveats"),
    ):
        try:
            _, between = _replace_block(text, begin, end, rendered)
        except ValueError:
            problems.append(f"generated {label} block markers missing/overlapping in COMPAT.md")
            continue
        if between.strip("\n") != rendered:
            problems.append(
                f"generated {label} block is stale — run"
                " `python scripts/compat_doc.py build`"
            )


def _check_evidence_refs(problems: list[str]) -> None:
    for c in CAVEATS:
        for path, marker in c.refs:
            f = REPO / path
            if not f.is_file():
                problems.append(f"caveat {c.id}: evidence file missing: {path}")
                continue
            if marker not in f.read_text(encoding="utf-8"):
                problems.append(
                    f"caveat {c.id}: evidence marker no longer in {path}:"
                    f" {marker!r} — re-point the ledger or re-word the source"
                )


def _check_floors_agree(problems: list[str]) -> None:
    pins = publish_pins()
    legs = [v.strip() for v in (pins["matrix_legs"] or "").split(",") if v.strip()]
    floors = {
        "publish-pypi": pins["pypi_pin"],
        "publish-image": pins["image_pin"],
        "compat-nightly": pins["nightly_floor"],
        "compat-matrix[0]": legs[0] if legs else None,
    }
    values = {k: v for k, v in floors.items() if v}
    if len(values) < len(floors):
        problems.append(f"could not parse a floor pin from the workflows: {floors}")
    if len(set(values.values())) != 1:
        problems.append(f"floor pins disagree across the workflows: {floors}")
    if not legs:
        problems.append("compat-matrix.yml declares no legs")


def _check_verified_block_render_inputs(text: str, problems: list[str]) -> None:
    if not SIDECAR_PATH.is_file():
        problems.append("nightly sidecar missing: tests/live/compat/openapi-last-known-good.meta.json")
        return
    facts = sidecar_facts()
    for key in ("captured_at", "version", "run_url"):
        if not facts.get(key):
            problems.append(f"nightly sidecar lacks {key!r}")
    if facts and not facts.get("run_url", "").startswith("https://"):
        problems.append("nightly sidecar run_url is not an https URL")


def _check_links(text: str, problems: list[str]) -> None:
    # relative (in-repo) markdown links must resolve to real files
    for m in re.finditer(r"\]\((?!https?://|#)([^)#]+)", text):
        path = m.group(1)
        if not (REPO / path).exists():
            problems.append(f"COMPAT.md links to a missing path: {path}")
    if not FILINGS_PATH.is_file():
        problems.append("FILINGS.md (pending upstream filings) is missing")
    if NIGHTLY_BADGE_URL not in text:
        problems.append("COMPAT.md lost the nightly canary badge URL")


def check() -> list[str]:
    """All lint problems, or an empty list when COMPAT.md is clean."""
    problems: list[str] = []
    if not COMPAT_PATH.is_file():
        problems.append("COMPAT.md is missing")
        return problems
    text = COMPAT_PATH.read_text(encoding="utf-8")
    _check_generated_blocks(text, problems)
    _check_evidence_refs(problems)
    _check_floors_agree(problems)
    _check_verified_block_render_inputs(text, problems)
    _check_links(text, problems)
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    cmd = (args[0] if args else "check").lower()
    if cmd == "build":
        if build():
            print(f"regenerated {COMPAT_PATH.relative_to(REPO)}")
        else:
            print("COMPAT.md up to date")
        problems = check()
        if problems:
            for p in problems:
                print(f"lint: {p}", file=sys.stderr)
            return 1
        return 0
    if cmd == "check":
        problems = check()
        for p in problems:
            print(f"lint: {p}", file=sys.stderr)
        print("COMPAT.md ok" if not problems else f"{len(problems)} problem(s)")
        return 0 if not problems else 1
    print(f"unknown command {cmd!r} (build | check)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
