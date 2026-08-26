"""Author & license notice for the CLI.

Rendered once at the start of any `litellm-as-code` invocation as a brief
stderr banner (so it never contaminates stdout consumers like `export`).
"""

from __future__ import annotations

import sys

from . import __version__
from ._meta import AUTHOR, LICENSE


def notice() -> str:
    """A single-line, human- and CI-friendly author & license notice."""
    return f"litellm-as-code {__version__} — by {AUTHOR}, licensed under {LICENSE}"


def print_notice(*, file=None) -> None:
    """Print the notice to stderr (so stdout stays machine-consumable).

    ``file`` is resolved at call time (never as a default-arg) so the notice
    always targets the active stderr — e.g. pytest's capsys — and never a
    stale reference captured at import.
    """
    print(notice(), file=file if file is not None else sys.stderr)
