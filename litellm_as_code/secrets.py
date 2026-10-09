"""Shared secret-shape helpers for masked values coming off the wire.

The LiteLLM read API masks secret material (see ../exporter.py and
resources/guardrails.py): values come back with a run of asterisks —
`_get_masked_values` keeps a short prefix/suffix ("ab****cd") and emits
"*****" for short values — and never as plaintext, so a masked echo can
never be compared against desired state. Both the exporter (do not persist
masked echoes) and the reconcilers (do not diff masked echoes) need the
SAME detection rules — keeping them in one place prevents the two from
drifting apart (issue #11).
"""

from __future__ import annotations

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


def is_masked_value(value: Any) -> bool:
    # LiteLLM's masker keeps a short prefix/suffix and produces an interior
    # asterisk run ("ab****cd"), or "*****" for short values — so the shape
    # is ANY run of >= 3 asterisks, not a trailing one. A lone embedded "*"
    # in a glob/pattern value ("openai/*", "gpt-4*") is a legitimate
    # configuration value, not a masked secret — exporting it is required
    # for re-apply to reproduce the row (issue #11).
    return isinstance(value, str) and "***" in value


def is_secret_entry(key: str, value: Any) -> bool:
    """True when the key is sensitive and the value is non-None (a non-None
    value on a sensitive key is material we must not persist or diff), or
    the value itself has the masker's output shape."""
    if is_sensitive_key(key) and value is not None:
        return True
    return is_masked_value(value)


def split_masked(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return (clean, masked_keys) for a nested payload (e.g. guardrail
    `litellm_params` / `guardrail_info`). Drops entries that are provably
    masked or whose key the API would mask, keeping only non-secret
    configuration the operator can re-declare."""
    clean: dict[str, Any] = {}
    masked: list[str] = []
    for k, v in payload.items():
        if is_sensitive_key(k):
            if v is not None:
                masked.append(k)
            continue
        if is_masked_value(v):
            masked.append(k)
            continue
        clean[k] = v
    return clean, masked
