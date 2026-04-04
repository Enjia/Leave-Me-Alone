from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from core.models import StrictBaseModel

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 2
_V1_SCHEMA_VERSION = 1


class PolicyValues(StrictBaseModel):
    max_round_per_stage: int | None = None
    context_budget_max_chars: int | None = None
    warn_budget_usd: float | None = None
    hard_budget_usd: float | None = None
    per_stage_budget_usd: float | None = None


class StagePolicySpec(StrictBaseModel):
    profile: str = ""
    overrides: PolicyValues = Field(default_factory=PolicyValues)


class LayeredPolicyConfig(StrictBaseModel):
    schema_version: int = CURRENT_SCHEMA_VERSION
    global_policy: PolicyValues = Field(default_factory=PolicyValues, alias="global")
    project: PolicyValues = Field(default_factory=PolicyValues)
    profiles: dict[str, PolicyValues] = Field(default_factory=dict)
    stages: dict[str, StagePolicySpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_schema_and_profiles(self) -> LayeredPolicyConfig:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported layered policy schema_version={self.schema_version}; "
                f"expected {CURRENT_SCHEMA_VERSION}.",
            )
        undefined_profiles = sorted(
            {
                stage_spec.profile
                for stage_spec in self.stages.values()
                if stage_spec.profile and stage_spec.profile not in self.profiles
            }
        )
        if undefined_profiles:
            raise ValueError(
                "Layered policy references undefined profiles: "
                + ", ".join(undefined_profiles),
            )
        return self


def load_layered_policy(
    path: Path | None,
    *,
    log_migration: bool = True,
) -> LayeredPolicyConfig | None:
    if path is None:
        return None
    if not path.exists():
        raise ValueError(f"Layered policy file does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid layered policy JSON at {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"Failed reading layered policy file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Layered policy must be a JSON object: {path}")
    migrated_payload, migration_steps = migrate_layered_policy_payload(payload)
    if migration_steps and log_migration:
        logger.warning(
            "layered policy migrated to schema_version=%s for %s: %s",
            CURRENT_SCHEMA_VERSION,
            path,
            "; ".join(migration_steps),
        )
    return LayeredPolicyConfig.model_validate(migrated_payload)


def migrate_layered_policy_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(payload, dict):
        raise ValueError("Layered policy payload must be a dictionary.")
    working = dict(payload)
    schema_version = _parse_schema_version(working.get("schema_version"))

    if schema_version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported layered policy schema_version={schema_version}; "
            f"max supported={CURRENT_SCHEMA_VERSION}.",
        )
    if schema_version < _V1_SCHEMA_VERSION:
        raise ValueError(f"Invalid layered policy schema_version={schema_version}.")

    migration_steps: list[str] = []
    while schema_version < CURRENT_SCHEMA_VERSION:
        if schema_version == _V1_SCHEMA_VERSION:
            working, steps = _migrate_v1_to_v2(working)
            migration_steps.extend(steps)
            schema_version = CURRENT_SCHEMA_VERSION
            continue
        raise ValueError(f"No migration path from schema_version={schema_version}.")
    return working, migration_steps


def resolve_stage_policy(
    config: LayeredPolicyConfig | None,
    *,
    stage_name: str,
) -> PolicyValues:
    if config is None:
        return PolicyValues()

    resolved = PolicyValues()
    _merge_policy_values(resolved, config.global_policy)
    _merge_policy_values(resolved, config.project)

    stage_spec = config.stages.get(stage_name)
    if stage_spec is not None:
        if stage_spec.profile and stage_spec.profile in config.profiles:
            _merge_policy_values(resolved, config.profiles[stage_spec.profile])
        _merge_policy_values(resolved, stage_spec.overrides)

    return resolved


def _merge_policy_values(target: PolicyValues, source: PolicyValues) -> None:
    data = source.model_dump(exclude_none=True)
    for key, value in data.items():
        setattr(target, key, value)


def _parse_schema_version(raw_version: Any) -> int:
    if raw_version is None:
        return _V1_SCHEMA_VERSION
    if isinstance(raw_version, bool):
        raise ValueError(f"Invalid layered policy schema_version={raw_version}.")
    try:
        parsed = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid layered policy schema_version={raw_version}.") from exc
    return parsed


def _migrate_v1_to_v2(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Migrate legacy layered policy payloads to schema v2.

    Supported legacy patterns:
    - Missing ``schema_version`` (implicitly v1)
    - ``global_policy`` key (renamed to ``global``)
    - Stage-level ``profile_id`` (renamed to ``profile``)
    - Stage-level inline override fields (wrapped into ``overrides``)
    """
    migrated = dict(payload)
    steps: list[str] = []
    policy_keys = set(PolicyValues.model_fields)

    if "global_policy" in migrated:
        legacy_global = migrated.pop("global_policy")
        if "global" not in migrated:
            migrated["global"] = legacy_global
            steps.append("renamed top-level key global_policy -> global")
        else:
            steps.append("dropped redundant global_policy because global already exists")

    stages = migrated.get("stages")
    if isinstance(stages, dict):
        migrated_stages: dict[str, Any] = {}
        for stage_name, stage_value in stages.items():
            if not isinstance(stage_value, dict):
                migrated_stages[stage_name] = stage_value
                continue
            stage_dict = dict(stage_value)
            if "profile_id" in stage_dict and "profile" not in stage_dict:
                stage_dict["profile"] = stage_dict.pop("profile_id")
                steps.append(f"stages.{stage_name}: renamed profile_id -> profile")

            inline_overrides: dict[str, Any] = {}
            for key in list(stage_dict):
                if key in policy_keys:
                    inline_overrides[key] = stage_dict.pop(key)

            if inline_overrides:
                raw_overrides = stage_dict.get("overrides")
                if isinstance(raw_overrides, dict):
                    merged_overrides = dict(raw_overrides)
                    for key, value in inline_overrides.items():
                        merged_overrides.setdefault(key, value)
                    stage_dict["overrides"] = merged_overrides
                elif "overrides" not in stage_dict:
                    stage_dict["overrides"] = inline_overrides
                else:
                    # Preserve fail-closed behavior when overrides has invalid type.
                    stage_dict.update(inline_overrides)
                steps.append(f"stages.{stage_name}: normalized inline policy fields into overrides")

            if "profile" not in stage_dict:
                stage_dict["profile"] = ""
            if "overrides" not in stage_dict:
                stage_dict["overrides"] = {}
            migrated_stages[stage_name] = stage_dict

        migrated["stages"] = migrated_stages

    migrated["schema_version"] = CURRENT_SCHEMA_VERSION
    steps.append("set schema_version=2")
    return migrated, steps
