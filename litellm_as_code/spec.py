"""Load & validate the declarative YAML spec."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class SpecError(ValueError):
    """Raised when the YAML spec is malformed."""


def load_spec(path: str | Path) -> dict[str, Any]:
    """Load a spec file, return the top-level mapping.

    Structure (all sections optional except the file being YAML):
        config:     ignored by the reconciler (startup-only settings)
        users:      [{user_id, user_alias?, user_email?, user_role?, ...}]
        teams:      [{team_id?, team_alias, members_with_roles?: [{user_id, role}]}]
        virtual_keys: [{key_alias, key?, key_type?, user_id?, ...}]
        credentials: [{credential_name, credential_info?, credential_values?}]
        models:     [{model_name, model_info?, litellm_params?}]

    Note: the reconciler intentionally does NOT manage the proxy's
    startup `config.yaml` (general_settings / litellm_settings /
    router_settings). Those are applied at proxy boot; only DB-backed
    runtime resources are reconciled here.
    """
    path = Path(path)
    with path.open() as f:
        data = yaml.safe_load(f)

    if data is None:
        raise SpecError(f"spec {path} is empty")
    if not isinstance(data, dict):
        raise SpecError(f"spec {path} must be a mapping at the top level")

    allowed = {"config", "users", "teams", "virtual_keys", "credentials", "models"}
    unknown = set(data) - allowed
    if unknown:
        raise SpecError(f"unknown top-level sections in {path}: {sorted(unknown)}")

    return data
