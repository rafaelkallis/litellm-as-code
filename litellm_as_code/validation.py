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
- Cross-entry **identity uniqueness** is checked per section (spec issue #8):
  two entries resolving to the same identity would reconcile last-wins on the
  same live resource, and a shared alias between an id-carrying and an
  alias-only team/organization can match & update the *wrong* live one via the
  reconcilers' by-alias fallback. Duplicate `user_id`s inside one
  `members_with_roles` are likewise rejected (the reconciler's `want_by_id`
  would silently keep the last role).
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

# Identity fields are non-empty: on an optional field an empty string counts
# as missing (falsy to the validators and the reconcilers' identity lookups);
# on a required field "" would reconcile against a bogus empty identity. The
# cross-entry uniqueness check then only ever skips values that per-entry
# validation already rejected.
NonEmptyStr = Annotated[str, Field(min_length=1, strict=True)]


# -- members ----------------------------------------------------------------

OrgRole = Literal["org_admin", "internal_user", "internal_user_viewer"]
TeamRole = Literal["admin", "user"]
UserRole = Literal["proxy_admin", "admin", "internal_user", "internal_user_viewer"]

_USER_ROLES: set[str] = set(UserRole.__args__)  # type: ignore[attr-defined]
_USER_ROLE_LIST = ", ".join(sorted(_USER_ROLES))

# Server-side role defaults for members. The member reconcilers read the RAW
# spec dicts (load_spec returns the raw YAML; the validated models are
# discarded), so Pydantic's per-model defaults never apply at reconcile time.
# These constants are the single source of truth for the server defaults — the
# Pydantic models below reference them so the two cannot drift (issue #7: an
# omitted role must not churn against the server's defaulted echo).
DEFAULT_ORG_ROLE: str = "internal_user"
DEFAULT_TEAM_ROLE: str = "user"


class OrgMember(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: NonEmptyStr
    role: OrgRole = DEFAULT_ORG_ROLE


class TeamMember(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: NonEmptyStr
    role: TeamRole = DEFAULT_TEAM_ROLE


# -- resource models ---------------------------------------------------------
#
# Note: `extra="allow"` on every model is non-negotiable — reconcilers pass
# the entry dict through to the API untouched, so Pydantic must not drop
# unknown keys. We warn about them from the raw input instead.


class _Budget(BaseModel):
    model_config = ConfigDict(extra="allow")

    budget_id: NonEmptyStr | None = None
    max_budget: Float | None = None
    soft_budget: Float | None = None
    max_parallel_requests: int | None = None
    tpm_limit: int | None = None
    rpm_limit: int | None = None
    model_max_budget: Any = None
    budget_duration: DurationStr | None = None

    @model_validator(mode="after")
    def _require_identity(self) -> _Budget:
        # Unlike teams/orgs there is no alias fallback: budget_id is the sole
        # identity (LiteLLM has no find-budget-by-alias surface, and ids it
        # generates server-side are not reconcilable from the spec).
        if not self.budget_id:
            raise ValueError(
                "'budget_id' is required (LiteLLM-generated ids are not reconcilable)"
            )
        return self


class _Organization(BaseModel):
    model_config = ConfigDict(extra="allow")

    organization_id: NonEmptyStr | None = None
    organization_alias: NonEmptyStr | None = None
    models: StrList | None = None
    members_with_roles: list[OrgMember] = []

    @model_validator(mode="after")
    def _require_identity(self) -> _Organization:
        if not self.organization_id and not self.organization_alias:
            raise ValueError(
                "must set at least one of 'organization_id' or 'organization_alias'"
            )
        return self

    @model_validator(mode="after")
    def _unique_members(self) -> _Organization:
        # The reconciler builds `want_by_id = {m["user_id"]: ...}`, which would
        # silently keep the *last* role for a repeated user_id.
        seen: set[str] = set()
        for member in self.members_with_roles:
            if member.user_id in seen:
                raise ValueError(
                    f"duplicate user_id {member.user_id!r} in members_with_roles"
                )
            seen.add(member.user_id)
        return self


class _User(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: NonEmptyStr
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

    team_id: NonEmptyStr | None = None
    team_alias: NonEmptyStr | None = None
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

    @model_validator(mode="after")
    def _unique_members(self) -> _Team:
        # The reconciler builds `want_by_id = {m["user_id"]: ...}`, which would
        # silently keep the *last* role for a repeated user_id.
        seen: set[str] = set()
        for member in self.members_with_roles:
            if member.user_id in seen:
                raise ValueError(
                    f"duplicate user_id {member.user_id!r} in members_with_roles"
                )
            seen.add(member.user_id)
        return self


class _Key(BaseModel):
    model_config = ConfigDict(extra="allow")

    key_alias: NonEmptyStr
    key: str | None = Field(default=None, min_length=1)
    user_id: str | None = None
    team_id: str | None = None
    models: StrList | None = None
    max_budget: Float | None = None
    budget_duration: DurationStr | None = None
    allowed_routes: StrList | None = None


class _Credential(BaseModel):
    model_config = ConfigDict(extra="allow")

    credential_name: NonEmptyStr
    credential_info: dict[str, Any] = {}
    credential_values: dict[str, Any] = {}
    model_id: str | None = None


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")

    model_name: NonEmptyStr
    model_info: dict[str, Any] = {}
    litellm_params: dict[str, Any] = {}


class _Guardrail(BaseModel):
    model_config = ConfigDict(extra="allow")

    guardrail_name: NonEmptyStr
    litellm_params: dict[str, Any] = {}
    guardrail_info: dict[str, Any] = {}


class _Policy(BaseModel):
    model_config = ConfigDict(extra="allow")

    policy_name: NonEmptyStr
    inherit: str | None = None
    description: str | None = None
    guardrails_add: StrList | None = None
    guardrails_remove: StrList | None = None


# -- validation helpers ------------------------------------------------------


def _entry_extra_keys(entry: dict[str, Any], model: type[BaseModel]) -> list[str]:
    """Keys on a raw entry that the model doesn't declare (-> warning)."""
    known = set(model.model_fields)
    return [k for k in entry if k not in known]


# model_info.tier is an API enum ('free' | 'paid'); reject anything else at
# spec-load time instead of a mid-run 422 from POST/PATCH /model/*.
_MODEL_TIERS = ("free", "paid")


def _check_model_tier(entry: dict[str, Any]) -> str | None:
    """Return an error string when model_info.tier is set to a non-enum value."""
    mi = entry.get("model_info")
    if not isinstance(mi, dict):
        return None
    tier = mi.get("tier")
    if tier is None:
        return None
    if not isinstance(tier, str) or tier not in _MODEL_TIERS:
        return (
            f"spec.models[...].model_info.tier: invalid tier {tier!r} "
            f"(expected one of: {', '.join(_MODEL_TIERS)})"
        )
    return None


# Identity field(s) per section over which entries must be unique. Teams/orgs
# have *two* (id + alias): the reconcilers match on id first, then fall back to
# by-alias lookup, so any two entries sharing either value are ambiguous and
# can converge onto (and clobber) the wrong live team/organization.
_UNIQUE_FIELDS: dict[str, tuple[str, ...]] = {
    "budgets": ("budget_id",),
    "organizations": ("organization_id", "organization_alias"),
    "users": ("user_id",),
    "teams": ("team_id", "team_alias"),
    "virtual_keys": ("key_alias",),
    "credentials": ("credential_name",),
    "models": ("model_name",),
    "guardrails": ("guardrail_name",),
    "policies": ("policy_name",),
}


def _check_entry_uniqueness(
    *,
    section: str,
    entries: list[dict[str, Any]],
    errors: list[str],
) -> None:
    """Flag duplicate identity values across entries of the same section.

    Checked per identity field (teams/orgs on id and alias separately), so:
    - two alias-only teams with the same alias are caught (the reconciler's
      by-alias fallback would match the first one and re-update it instead of
      creating a second team),
    - an id-carrying entry and an alias-only entry sharing an alias are caught
      (same wrong-team match), as are two id-carrying entries with one id.

    ``None``/absent identity counts as absent and is never compared. Empty
    strings cannot reach this point on identity fields (``NonEmptyStr``
    rejects them per-entry); the falsy skip below is defense-in-depth.
    Non-dict entries are skipped here — already reported by the per-entry path.
    """
    fields = _UNIQUE_FIELDS.get(section)
    if not fields:
        return
    for field in fields:
        first_seen: dict[str, int] = {}
        for i, raw in enumerate(entries):
            if not isinstance(raw, dict):
                continue
            value = raw.get(field)
            if not isinstance(value, str) or not value:
                continue
            if value in first_seen:
                errors.append(
                    f"spec.{section}[{i}]: duplicate {field} {value!r} "
                    f"(first declared at spec.{section}[{first_seen[value]}])"
                )
            else:
                first_seen[value] = i


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

        # model_info.tier is an API enum; check it before the API would 422.
        if section == "models":
            tier_error = _check_model_tier(raw)
            if tier_error:
                errors.append(tier_error)

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
        # `data.get(section, [])` above already normalizes an omitted or
        # explicit-null section to [] — the isinstance guard is only reached
        # for a truthy, non-list section value.
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
        # Cross-entry check: two entries resolving to the same identity make
        # the reconcile last-wins ambiguous (and, for alias collisions, can
        # match & update the *wrong* live resource).
        _check_entry_uniqueness(
            section=section, entries=entries, errors=errors
        )

    return errors, warnings


def format_spec_errors(errors: list[str]) -> str:
    """Turn collected validation findings into one human-readable message."""
    return "\n".join(f"  - {e}" for e in errors)
