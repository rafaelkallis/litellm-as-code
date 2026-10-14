"""Tiny logging helper that also supports dry-run banner rendering."""

from __future__ import annotations

import sys

# When --quiet, the classification notice channel (server_owned) is
# suppressed. ALL other warns (masked credentials, masker echoes, capability
# degrades) stay always-on: scripts must keep seeing "fill masked values
# manually". Issue #29: only drift-classification notices are quiet-gated.
_quiet_classified = False


def set_quiet_classified(quiet: bool) -> None:
    global _quiet_classified
    _quiet_classified = quiet


def info(step: str, message: str = "") -> None:
    if message:
        print(f"[{step}] {message}", file=sys.stderr)
    else:
        print(f"[{step}]", file=sys.stderr)


def warn(step: str, message: str = "") -> None:
    """Non-fatal warning on stderr; never affects exit codes.

    Always on — NOT affected by --quiet (only the classification channel
    below is, issue #29).
    """
    if message:
        print(f"[warn][{step}] {message}", file=sys.stderr)
    else:
        print(f"[warn][{step}]", file=sys.stderr)


def server_owned(message: str) -> None:
    """Drift-classification notice (issue #29).

    Same [warn] shape as warn() so existing stderr grep patterns keep
    matching, but suppressed under --quiet: a converged run that quietly
    accepts server-owned drift is expected, not an anomaly the operator
    must see. The classification itself is always stamped on the plan line
    (stdout) regardless of --quiet — only the stderr echo is gated.
    """
    if _quiet_classified:
        return
    print(f"[warn][litellm-as-code] {message}", file=sys.stderr)


def banner(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}", file=sys.stderr)
