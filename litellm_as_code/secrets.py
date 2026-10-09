"""Shared secret-shape helpers for masked values coming off the wire.

The LiteLLM read API masks secret material (see ../exporter.py and
resources/guardrails.py): values come back with a run of asterisks —
`_get_masked_values` keeps a short prefix/suffix ("ab****cd") and emits
"*****" for short values — and never as plaintext, so a masked echo can
never be compared against desired state. Both the exporter (do not persist
masked echoes) and the reconcilers (do not diff masked echoes) need the
SAME detection rules — keeping them in one place prevents the two from
drifting apart (issue #11).

Secret material also hides NESTED inside otherwise-benign containers, e.g.
the supported `litellm_params.headers.Authorization` shape (plain bearer
token under a non-sensitive key). `scrub_value` projects payloads
recursively so neither side of the export/re-apply contract can leak or
mis-read a nested secret (Copilot r9, PR #20).
"""

from __future__ import annotations

import re
from typing import Any

# Key-name keywords the API masks. Mirrors LiteLLM v1.97.0's sensitive-key
# handling (it also masks `authorization`, e.g. guardrail Authorization
# headers). Stronger guard than the value heuristic: a key carrying any of
# these substrings is treated as secret regardless of its value shape.
SENSITIVE_KEYWORDS = (
    "token",
    "key",
    "secret",
    "credential",
    "password",
    "passwd",
    "authorization",
)


def is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return any(kw in k for kw in SENSITIVE_KEYWORDS)


# Placeholder left in place of a scrubbed LIST element: removing the entry
# would shift every later index and desynchronize index-based reconciliation
# (Copilot r11 on PR #20). The marker is deliberately NOT a secret-matched
# shape; re-declaring the plaintext simply replaces it.
MASK_PLACEHOLDER = "<masked>"

# Masker output shapes (LiteLLM v1.97.0 `_get_masked_values`), matched
# WHOLE-VALUE so legitimate content containing "***" (e.g. a Markdown
# "use *** emphasis *** here") is not torn out (Copilot r14 on PR #20):
#   - legacy/simple:  value ending in a 3+ asterisk run ("abcd***")
#   - short values:   nothing but asterisks ("*****"), 3+ of them
#   - prefix/suffix:  exactly 2 kept characters, asterisk run between
#                     them ("ab****cd")
_MASK_SHAPES = re.compile(r"^(?:.*\*{3,}|\*{3,}|.{2}\*{3,}.{2})$")


def is_masked_value(value: Any) -> bool:
    # A lone embedded "*" in a glob/pattern value ("openai/*", "gpt-4*")
    # is a legitimate configuration value, not a masked secret — exporting
    # it is required for re-apply to reproduce the row (issue #11).
    return isinstance(value, str) and bool(_MASK_SHAPES.match(value))


def is_secret_entry(key: str, value: Any) -> bool:
    """True when the key is sensitive and the value is non-None (a non-None
    value on a sensitive key is material we must not persist or diff), or
    the value itself has the masker's output shape."""
    if is_sensitive_key(key) and value is not None:
        return True
    return is_masked_value(value)


def scrub_value(
    value: Any,
    prefix: str = "",
) -> tuple[Any, list[str]]:
    """Recursively project a payload with its secret material removed.

    Mappings are scrubbed per key: a sensitive key with a non-None value is
    dropped whole; a masker-shaped scalar is dropped; every other dict/list
    value is scrubbed recursively. An emptied container stays in place so
    that "the spec declares this header map" (vacuously, after the strip)
    stays distinguishable from "the spec declares nothing". Returns
    (projected, dotted paths of the dropped entries).

    `prefix` builds readable dotted paths ("headers.Authorization") for the
    dropped-entries report.
    """
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        masked: list[str] = []
        for k, v in value.items():
            path = f"{prefix}.{k}" if prefix else k
            if is_sensitive_key(k):  # None-valued secrets: dropped silently
                if v is not None:
                    masked.append(path)
                continue
            if is_masked_value(v):  # masker-echo scalar: carries no signal
                masked.append(path)
                continue
            carved, sub = scrub_value(v, path)
            masked.extend(sub)
            clean[k] = carved
        return clean, masked
    if isinstance(value, list):
        carved = []
        masked = []
        for i, item in enumerate(value):
            path = f"{prefix}[{i}]"
            if isinstance(item, (dict, list)):
                item_clean, sub = scrub_value(item, path)
                masked.extend(sub)
                carved.append(item_clean)
            elif is_masked_value(item):
                # keep a placeholder so later indices stay aligned with the
                # live vector (Copilot r11 on PR #20)
                masked.append(path)
                carved.append(MASK_PLACEHOLDER)
            else:
                carved.append(item)
        return carved, masked
    return value, []


def split_masked(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return (clean, masked_keys) for a nested payload (e.g. guardrail
    `litellm_params` / `guardrail_info`). Recursive: entries that are
    provably masked, whose key the API would mask, or which hide a secret
    below them are dropped (list elements keep a MASK_PLACEHOLDER slot so
    indices stay aligned), keeping only non-secret configuration the
    operator can re-declare."""
    return scrub_value(payload)
