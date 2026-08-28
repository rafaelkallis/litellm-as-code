"""CLI entrypoint: litellm-as-code."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from ._meta import AUTHOR, LICENSE
from .api import LiteLLMClient
from .exporter import export_spec
from .notice import print_notice
from .reconciler import run


_DESCRIPTION = (
    f"litellm-as-code ({AUTHOR}, {LICENSE} license): declarative runtime-state "
    "management for a LiteLLM proxy — reconcile "
    "users/teams/keys/credentials/models from a YAML spec, or export a live "
    "proxy to a spec."
)


_QUIET_HELP = (
    "suppress the author & license notice (for scripting); this does not "
    "suppress command output"
)


def _add_quiet_flag(p: argparse.ArgumentParser) -> None:
    """Add the shared `--quiet` flag; keeps the notice-suppression option in one
    place across the reconcile and export parsers."""
    p.add_argument(
        "--quiet",
        action="store_true",
        help=_QUIET_HELP,
    )


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False: argparse would otherwise accept unambiguous prefixes
    # (--q for --quiet, --ver for --version, ...) which the notice pre-scan
    # can't mirror reliably. Exact option names only keeps the CLI surface and
    # the "every invocation" notice behavior deterministic.
    p = argparse.ArgumentParser(
        prog="litellm-as-code",
        description=_DESCRIPTION,
        allow_abbrev=False,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_quiet_flag(p)

    p.add_argument(
        "spec",
        nargs="?",
        default=os.environ.get("LITELLM_SPEC", "spec.yml"),
        help="path to the declarative YAML spec (env: LITELLM_SPEC)",
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get("LITELLM_BASE_URL") or os.environ.get("BASE_URL"),
        help="LiteLLM proxy base URL (env: LITELLM_BASE_URL)",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("LITELLM_API_KEY") or os.environ.get("API_KEY"),
        help="admin API key (env: LITELLM_API_KEY)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would change without applying anything (exit 2 on diff)",
    )
    p.add_argument(
        "--prune",
        action="store_true",
        help="(reserved) delete live resources not present in the spec",
    )
    return p


def build_export_parser() -> argparse.ArgumentParser:
    """`litellm-as-code export [OUT]`: read a live proxy and write a spec."""
    p = argparse.ArgumentParser(
        prog="litellm-as-code export",
        description=(
            f"litellm-as-code ({AUTHOR}, {LICENSE} license): read the live "
            "LiteLLM proxy's runtime (DB-backed) state and write a declarative "
            "spec that reproduces it. Secrets are never read back (write-once), "
            "so credential_values/key come out as placeholders to fill in."
        ),
        allow_abbrev=False,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_quiet_flag(p)
    p.add_argument(
        "out",
        nargs="?",
        default=os.environ.get("LITELLM_SPEC", "spec.yml"),
        help="path to write the exported spec (env: LITELLM_SPEC, default spec.yml)",
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get("LITELLM_BASE_URL") or os.environ.get("BASE_URL"),
        help="LiteLLM proxy base URL (env: LITELLM_BASE_URL)",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("LITELLM_API_KEY") or os.environ.get("API_KEY"),
        help="admin API key (env: LITELLM_API_KEY)",
    )
    return p


def _require_credentials(base_url: str | None, api_key: str | None) -> bool:
    if not base_url:
        print("error: --base-url (or LITELLM_BASE_URL) is required", file=sys.stderr)
        return False
    if not api_key:
        print("error: --api-key (or LITELLM_API_KEY) is required", file=sys.stderr)
        return False
    return True


def _notice_requested(argv: list[str]) -> bool:
    """Whether this invocation should print the author & license notice.

    Skipped when ``--quiet`` is given, or when the invocation asks for
    ``-h``/``--help``/``--version`` (both parsers now define ``--version``) —
    those are argparse actions that run before ``main()`` can finish, and the
    docs promise they're unaffected by the notice.

    Only the tokens BEFORE the first ``--`` are scanned: argparse treats
    everything after ``--`` as positionals, so e.g. ``["--", "--quiet"]`` has
    ``--quiet`` as the spec path (not a flag) and must not suppress the
    notice. In every other case the notice is emitted BEFORE full parsing so
    even a malformed invocation (which argparse aborts with an error and a
    non-zero exit) still prints it first, per the "every invocation" contract.
    """
    if "--" in argv:
        argv = argv[: argv.index("--")]
    if "--quiet" in argv:
        return False
    return not any(a in ("-h", "--help", "--version") for a in argv)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = list(argv)

    # `litellm-as-code export [OUT]` — a read-only subcommand distinct from
    # the default reconcile flow (which treats the first positional as a spec).
    if args and args[0] == "export":
        sub = args[1:]
        if _notice_requested(sub):
            print_notice()
        ep = build_export_parser()
        eargs = ep.parse_args(sub)
        if not _require_credentials(eargs.base_url, eargs.api_key):
            return 1
        try:
            client = LiteLLMClient(eargs.base_url, eargs.api_key)
            export_spec(client, eargs.out)
            print(f"exported {eargs.out}", file=sys.stderr)
            return 0
        except Exception as e:  # noqa: BLE001 - top-level CLI error surfacing
            print(f"error: {e}", file=sys.stderr)
            return 1

    if _notice_requested(args):
        print_notice()
    args = build_parser().parse_args(args)

    if not _require_credentials(args.base_url, args.api_key):
        return 1

    try:
        return run(
            args.spec,
            base_url=args.base_url,
            api_key=args.api_key,
            dry_run=args.dry_run,
            prune=args.prune,
        )
    except Exception as e:  # noqa: BLE001 - top-level CLI error surfacing
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
