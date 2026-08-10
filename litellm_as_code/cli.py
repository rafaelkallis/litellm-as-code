"""CLI entrypoint: litellm-as-code."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .reconciler import run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="litellm-as-code",
        description=(
            "Declarative runtime-state management for a LiteLLM proxy: "
            "reconcile users/teams/keys/credentials/models from a YAML spec."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

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
        "--state",
        default=os.environ.get("LITELLM_STATE", "state.json"),
        help="path to the applied-state file (default: state.json)",
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.base_url:
        print("error: --base-url (or LITELLM_BASE_URL) is required", file=sys.stderr)
        return 1
    if not args.api_key:
        print("error: --api-key (or LITELLM_API_KEY) is required", file=sys.stderr)
        return 1

    try:
        return run(
            args.spec,
            base_url=args.base_url,
            api_key=args.api_key,
            state_path=args.state,
            dry_run=args.dry_run,
            prune=args.prune,
        )
    except Exception as e:  # noqa: BLE001 - top-level CLI error surfacing
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
