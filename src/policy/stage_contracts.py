from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.models import FeatureChecklistItem, StageSpec, StageSubgoal
from core.prompts import build_stage_source_requirements_report


def check_required_inputs(flow: object, stage: StageSpec) -> list[str]:
    if not stage.required_inputs:
        return []
    all_produced: set[str] = set()
    for artifact_list in flow.state.stage_artifacts.values():
        for artifact in artifact_list:
            all_produced.add(artifact.artifact_name)
    return [inp for inp in stage.required_inputs if inp not in all_produced]


def validate_stage_catalog(stages: list[StageSpec]) -> list[str]:
    errors: list[str] = []
    stage_aliases: dict[str, str] = {}
    for stage in stages:
        aliases = [stage.name]
        if stage.stage_id.strip():
            aliases.append(stage.stage_id.strip())
        for alias in aliases:
            owner = stage_aliases.get(alias)
            if owner is not None and owner != stage.name:
                errors.append(
                    f"stage alias '{alias}' is duplicated by '{owner}' and '{stage.name}'"
                )
                continue
            stage_aliases[alias] = stage.name

    for stage in stages:
        for dependency in stage.depends_on_stages:
            if dependency not in stage_aliases:
                errors.append(
                    f"stage '{stage.name}' declares unknown depends_on_stages entry '{dependency}'"
                )
    return errors


def validate_stage_definition(flow: object, stage: StageSpec) -> list[str]:
    errors: list[str] = []

    if stage.stage_id and not re.fullmatch(r"[A-Za-z0-9_.-]+", stage.stage_id):
        errors.append("stage_id must match [A-Za-z0-9_.-]+ when provided")
    if not stage.objective.strip():
        errors.append("objective must not be empty")
    if not stage.acceptance_criteria:
        errors.append("acceptance_criteria must not be empty")
    if not stage.invariants:
        errors.append("invariants must not be empty")
    if stage.trust_priority:
        missing_priority = [
            item for item in stage.trust_priority if item not in stage.trust_sources
        ]
        if missing_priority:
            errors.append(
                "trust_priority entries must be a subset of trust_sources: "
                + ", ".join(missing_priority)
            )

    for scope_path in stage.scope_hint:
        target = flow.cfg.target_repo / scope_path
        if not target.exists() and not target.parent.exists():
            errors.append(f"scope_hint path does not exist: {scope_path}")

    local_commands = stage.test_commands + stage.lint_commands + stage.perf_checks
    remote_prefixes = [
        prefix
        for prefix in (
            flow.cfg.remote_workdir,
            flow.cfg.remote_workdir_node1,
            "/enjia/",
        )
        if prefix
    ]
    for command in local_commands:
        if any(prefix in command for prefix in remote_prefixes):
            errors.append(
                "local command references a remote path and is not workspace-relative: "
                f"{command}"
            )

    if stage.execution_env == "node0_and_node1" and stage.requires_remote:
        if stage.sync_strategy != "sync_to_node0_and_node1":
            errors.append("dual-node remote stages must use sync_to_node0_and_node1")

    source_report = build_stage_source_requirements_report(stage)
    if source_report.source_path:
        flow._persist_source_preflight(stage, source_report)
    if source_report.errors:
        flow._bump_metric("source_preflight_error_stage_count")
        errors.extend(
            f"source preflight failed: {detail}" for detail in source_report.errors
        )
    if source_report.truncated:
        flow._bump_metric("source_preflight_truncated_stage_count")
        errors.append(
            "source preflight failed: extracted source requirements were truncated; "
            "narrow source_mode/source_query/source_anchor to keep complete context."
        )
    if not stage.source_file and any("唯一目标约束" in c for c in stage.harness_constraints):
        errors.append("unique-target stage must set source_file")

    if stage.remote_gate_contracts:
        declared_commands = set(stage.gate_commands_remote)
        for contract in stage.remote_gate_contracts:
            if contract.command not in declared_commands:
                flow._bump_metric("stage_definition_remote_contract_mismatch_count")
                errors.append(
                    "remote_gate_contract command is not listed in gate_commands_remote: "
                    f"{contract.command}"
                )

    artifact_output_names: dict[str, str] = {}
    for artifact_name in stage.produces_artifacts:
        normalized_name = artifact_name.strip()
        if not normalized_name:
            errors.append("produces_artifacts entries must not be empty")
            continue
        output_suffix = flow._stage_output_artifact_suffix(normalized_name)
        previous = artifact_output_names.get(output_suffix)
        if previous is not None and previous != normalized_name:
            errors.append(
                "produces_artifacts entries collide after filename normalization: "
                f"{previous!r} and {normalized_name!r} -> {output_suffix!r}"
            )
            continue
        artifact_output_names[output_suffix] = normalized_name

    return errors


def hydrate_stage_spec_defaults(stage: StageSpec) -> None:
    if not stage.stage_id.strip():
        stage.stage_id = stage.name
    if not stage.acceptance_criteria:
        acceptance_criteria: list[str] = []
        acceptance_criteria.extend(
            f"Pass test command: {command}" for command in stage.test_commands
        )
        acceptance_criteria.extend(
            f"Pass lint command: {command}" for command in stage.lint_commands
        )
        acceptance_criteria.extend(
            f"Pass perf check: {command}" for command in stage.perf_checks
        )
        acceptance_criteria.extend(
            f"Produce expected artifact: {path}"
            for path in stage.expected_artifact_paths
        )
        acceptance_criteria.extend(
            f"Honor harness constraint: {item}" for item in stage.harness_constraints
        )
        if not acceptance_criteria:
            acceptance_criteria.append(f"Satisfy stage objective: {stage.objective}")
        stage.acceptance_criteria = acceptance_criteria
    if not stage.invariants:
        invariants: list[str] = []
        invariants.extend(
            f"Write only within declared scope: {path}"
            for path in (stage.write_scope or stage.scope_hint)
        )
        invariants.extend(
            f"Keep required input available: {item}" for item in stage.required_inputs
        )
        invariants.extend(
            f"Preserve rollback requirement: {item}"
            for item in stage.rollback_requirements
        )
        if not invariants:
            invariants.append("Do not violate the declared stage objective or gate contracts.")
        stage.invariants = invariants
    if not stage.trust_sources:
        trust_sources: list[str] = []
        if stage.source_file:
            trust_sources.append(f"source_file:{stage.source_file}")
        trust_sources.extend(f"required_input:{item}" for item in stage.required_inputs)
        trust_sources.extend(
            f"artifact_contract:{contract.path}" for contract in stage.artifact_contracts
        )
        if not trust_sources:
            trust_sources.append("stage_spec")
        stage.trust_sources = trust_sources
    if not stage.trust_priority:
        stage.trust_priority = list(stage.trust_sources)
    if not stage.subgoals:
        generated_subgoals: list[StageSubgoal] = []
        source_ref = Path(stage.source_file).name if stage.source_file else ""
        for index, criterion in enumerate(stage.acceptance_criteria[:3], start=1):
            generated_subgoals.append(
                StageSubgoal(
                    subgoal_id=f"{stage.stage_id or 'stage'}_sg{index}",
                    title=f"Subgoal {index}",
                    description=criterion,
                    allowed_files=list(stage.write_scope or stage.scope_hint),
                    verification_targets=[criterion],
                )
            )
        if not generated_subgoals:
            generated_subgoals.append(
                StageSubgoal(
                    subgoal_id=f"{stage.stage_id or 'stage'}_sg1",
                    title="Primary objective",
                    description=stage.objective,
                    allowed_files=list(stage.write_scope or stage.scope_hint),
                    verification_targets=[stage.objective],
                )
            )
        if source_ref:
            generated_subgoals[0].verification_targets.append(f"source_ref:{source_ref}")
        stage.subgoals = generated_subgoals
    if not stage.feature_checklist:
        checklist: list[FeatureChecklistItem] = []
        for index, criterion in enumerate(stage.acceptance_criteria, start=1):
            checklist.append(
                FeatureChecklistItem(
                    item_id=f"{stage.stage_id or 'stage'}_item{index}",
                    description=criterion,
                    verification_hint=criterion,
                )
            )
        stage.feature_checklist = checklist


def validate_stage_outputs(
    flow: object,
    stage: StageSpec,
    *,
    base_dir: Path | None = None,
) -> list[str]:
    errors: list[str] = []
    artifact_root = (base_dir or flow.cfg.target_repo).resolve()

    for relative_path in stage.expected_artifact_paths:
        artifact_path = artifact_root / relative_path
        if not artifact_path.exists():
            errors.append(f"missing expected artifact: {relative_path}")

    for contract in stage.artifact_contracts:
        artifact_path = artifact_root / contract.path
        if not artifact_path.exists():
            errors.append(f"missing contract artifact: {contract.path}")
            continue

        try:
            payload = artifact_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"failed to read artifact {contract.path}: {exc}")
            continue

        if contract.format == "json":
            try:
                data = json.loads(payload)
            except json.JSONDecodeError as exc:
                errors.append(f"artifact is not valid JSON: {contract.path} ({exc})")
                continue
            for key in contract.required_keys:
                if key not in data:
                    errors.append(f"artifact {contract.path} missing JSON key: {key}")
            for key_path in contract.required_json_nonempty:
                found, value = lookup_json_path(data, key_path)
                if not found or not is_nonempty_json_value(value):
                    errors.append(
                        f"artifact {contract.path} missing non-empty JSON value at: {key_path}"
                    )
            for key_path, expected in contract.required_json_values.items():
                found, value = lookup_json_path(data, key_path)
                if not found:
                    errors.append(
                        f"artifact {contract.path} missing JSON value at: {key_path}"
                    )
                    continue
                if value != expected:
                    errors.append(
                        "artifact "
                        f"{contract.path} JSON value mismatch at {key_path}: "
                        f"expected={expected!r} actual={value!r}"
                    )

        for substring in contract.required_substrings:
            if substring not in payload:
                errors.append(
                    f"artifact {contract.path} missing required content: {substring}"
                )

    return errors


def lookup_json_path(payload: Any, key_path: str) -> tuple[bool, Any]:
    current = payload
    for part in [item for item in key_path.split(".") if item]:
        if isinstance(current, dict):
            if part not in current:
                return False, None
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < 0 or index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


def is_nonempty_json_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return bool(value)


def check_blocking_decisions(flow: object, stage: StageSpec) -> list[str]:
    if not stage.blocking_decisions:
        return []
    if "all" in flow.state.approved_decisions:
        return []
    return [
        decision
        for decision in stage.blocking_decisions
        if decision not in flow.state.approved_decisions
    ]
