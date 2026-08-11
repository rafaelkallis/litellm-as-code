"""Declarative per-resource validation of the YAML spec (Pydantic models).

Layer this on top of `spec.load_spec`'s top-level structural checks:

- **errors**   — a spec that breaks one of these models is rejected up front,
                before any API call, and *all* problems are reported in one
                pass (collect, don't fail-fast) via a single `SpecError`.
- **warnings** — unknown *per-resource* keys are non-fatal (forward compat:
                LiteLLM keeps adding fields). Entries are passed through
                verbatim (``extra="allow"`` keeps values intact for the
                reconcilers) and we surface the extras as `[warn]` lines.

Scope discipline (see AGENTS.md §4):
- Identity fields match exactly what reconcilers index (`entry["user_id"]`
  etc.) so a missing identity is caught here instead of a bare `KeyError`
  mid-reconcile.
- Nested opaque payloads (`credential_values`, `litellm_params`,
  `model_info`, `config`) are deliberately *not* closed schemas: providers
  pass arbitrary params. Only their well-known typed subfields are checked.
- Cross-resource references (e.g. a key's `user_id` existing in `users`) are
  intentionally out of scope.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

# -- shared field types ------------------------------------------------------

# Budget-shaped numbers appear as specs as floats; ints coerce cleanly.
Float = Annotated[float, Field(strict=False)]

DurationStr = Annotated[str, Field(pattern=r"^\d+(s|m|h|d|w|mo|hr|min)?$")]

# A list of model names / routes / guardrails (strings).
StrList = Annotated[list[str], Field(strict=False)]


# -- members ----------------------------------------------------------------

OrgRole = Literal["org_admin", "internal_user", "internal_user_viewer"]
TeamRole = Literal["admin", "user"]
UserRole = Literal["proxy_admin", "admin", "internal_user", "internal_user_viewer"]

_USER_ROLES: set[str] = set(UserRole.__args__)  # type: ignore[attr-defined]
_USER_ROLE_LIST = ", ".join(sorted(_USER_ROLES))


class OrgMember(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: str
    role: OrgRole = "internal_user"


class TeamMember(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: str
    role: TeamRole = "user"


# -- resource models ---------------------------------------------------------
#
# Note: `extra="allow"` on every model is non-negotiable — reconcilers pass
# the entry dict through to the API untouched, so Pydantic must not drop
# unknown keys. We warn about them from the raw input instead.


class _Budget(BaseModel):
    model_config = ConfigDict(extra="allow")

    budget_id: str | None = None
    max_budget: Float | None = None
    soft_budget: Float | None = None
    max_parallel_requests: int | None = None
    tpm_limit: int | None = None
    rpm_limit: int | None = None
    model_max_budget: Any = None
    budget_duration: DurationStr | None = None


class _Organization(BaseModel):
    model_config = ConfigDict(extra="allow")

    organization_id: str | None = None
    organization_alias: str | None = None
    models: StrList | None = None
    members_with_roles: list[OrgMember] = []

    @model_validator(mode="after")
    def _require_identity(self) -> _Organization:
        if not self.organization_id and not self.organization_alias:
            raise ValueError(
                "must set at least one of 'organization_id' or 'organization_alias'"
            )
        return self


class _User(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: str
    user_alias: str | None = None
    user_email: str | None = None
    user_role: str | None = None
    auto_create_key: bool | str | None = None

    @field_validator("user_role")
    @classmethod
    def _check_role(cls, v: str | None) -> str | None:
        if v is not None and v not in _USER_ROLES:
            raise ValueError(
                f"invalid role {v!r} (expected one of: {_USER_ROLE_LIST})"
            )
        return v


class _Team(BaseModel):
    model_config = ConfigDict(extra="allow")

    team_id: str | None = None
    team_alias: str | None = None
    organization_id: str | None = None
    max_budget: Float | None = None
    budget_duration: DurationStr | None = None
    models: StrList | None = None
    members_with_roles: list[TeamMember] = []

    @model_validator(mode="after")
    def _require_identity(self) -> _Team:
        if not self.team_id and not self.team_alias:
            raise ValueError("must set at least one of 'team_id' or 'team_alias'")
        return self


class _Key(BaseModel):
    model_config = ConfigDict(extra="allow")

    key_alias: str
    key: str | None = Field(default=None, min_length=1)
    user_id: str | None = None
    team_id: str | None = None
    models: StrList | None = None
    max_budget: Float | None = None
    budget_duration: DurationStr | None = None
    allowed_routes: StrList | None = None


class _Credential(BaseModel):
    model_config = ConfigDict(extra="allow")

    credential_name: str
    credential_info: dict[str, Any] = {}
    credential_values: dict[str, Any] = {}
    model_id: str | None = None


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")

    model_name: str
    model_info: dict[str, Any] = {}
    litellm_params: dict[str, Any] = {}


class _Guardrail(BaseModel):
    model_config = ConfigDict(extra="allow")

    guardrail_name: str
    litellm_params: dict[str, Any] = {}
    guardrail_info: dict[str, Any] = {}


class _Policy(BaseModel):
    model_config = ConfigDict(extra="allow")

    policy_name: str
    inherit: str | None = None
    description: str | None = None
    guardrails_add: StrList | None = None
    guardrails_remove: StrList | None = None


# -- validation helpers ------------------------------------------------------


def _entry_extra_keys(entry: dict[str, Any], model: type[BaseModel]) -> list[str]:
    """Keys on a raw entry that the model doesn't declare (-> warning)."""
    known = set(model.model_fields)
    return [k for k in entry if k not in known]


def _validate_section(
    *,
    section: str,
    entries: list[dict[str, Any]],
    model: type[BaseModel],
    errors: list[str],
    warnings: list[str],
) -> None:
    """Validate a list of raw dicts; append human-readable findings."""

    for i, raw in enumerate(entries):
        if not isinstance(raw, dict):
            errors.append(
                f"spec.{section}[{i}]: expected a mapping, got {type(raw).__name__}"
            )
            continue

        # Non-fatal: unknown per-resource keys are passed through verbatim.
        for key in _entry_extra_keys(raw, model):
            warnings.append(f"spec.{section}[{i}]: unknown key {key!r}")

        try:
            model.model_validate(raw)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                errors.append(
                    f"spec.{section}[{i}].{loc}: {err['msg']}"
                    if loc
                    else f"spec.{section}[{i}]: {err['msg']}"
                )


def validate_spec(
    data: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Validate top-level spec structure; return (errors, warnings).

    Errors are collected across *all* sections in one pass (no fail-fast).
    Warnings are unknown per-resource keys (non-fatal, pass-through).
    """
    errors: list[str] = []
    warnings: list[str] = []

    sections: dict[str, tuple[list[dict[str, Any]], type[BaseModel]]] = {
        "budgets": (data.get("budgets", []) or [], _Budget),
        "organizations": (data.get("organizations", []) or [], _Organization),
        "users": (data.get("users", []) or [], _User),
        "teams": (data.get("teams", []) or [], _Team),
        "virtual_keys": (data.get("virtual_keys", []) or [], _Key),
        "credentials": (data.get("credentials", []) or [], _Credential),
        "models": (data.get("models", []) or [], _Model),
        "guardrails": (data.get("guardrails", []) or [], _Guardrail),
        "policies": (data.get("policies", []) or [], _Policy),
    }

    for section, (entries, model) in sections.items():
        if entries is None:
            continue
        if not isinstance(entries, list):
            errors.append(f"spec.{section}: expected a list, got {type(entries).__name__}")
            continue
        _validate_section(
            section=section,
            entries=entries,
            model=model,
            errors=errors,
            warnings=warnings,
        )

    return errors, warnings


def format_spec_errors(errors: list[str]) -> str:
    """Turn collected validation findings into one human-readable message."""
    return "\n".join(f"  - {e}" for e in errors)
