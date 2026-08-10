"""Tiny logging helper that also supports dry-run banner rendering."""

from __future__ import annotations

import sys


def info(step: str, message: str = "") -> None:
    if message:
        print(f"[{step}] {message}", file=sys.stderr)
    else:
        print(f"[{step}]", file=sys.stderr)


def banner(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}", file=sys.stderr)
