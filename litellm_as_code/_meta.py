"""Single source of truth for human-facing project metadata.

Kept separate from ``pyproject.toml`` so the CLI can render an author/license
notice without depending on importlib.metadata.
"""

AUTHOR = "Rafael Kallis"
LICENSE = "MIT"
