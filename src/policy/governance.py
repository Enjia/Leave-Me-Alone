from __future__ import annotations

from core.models import FailureClassification, StageGate, StageSpec


def merge_stage_gate_with_stage_spec(stage: StageSpec, stage_gate: StageGate) -> None:
    def merge_unique(*groups: list[str]) -> list[str]:
        merged: list[str] = []
        for group in groups:
            for item in group:
                if item not in merged:
                    merged.append(item)
        return merged

    stage_gate.test_commands = list(stage.test_commands)
    stage_gate.lint_commands = list(stage.lint_commands)
    stage_gate.perf_checks = list(stage.perf_checks)

    immutable_contracts = [f"Remote gate commands are immutable: {cmd}" for cmd in stage.gate_commands_remote]
    immutable_contracts.extend(
        f"Remote gate evidence contract is immutable: {contract.model_dump()}"
        for contract in stage.remote_gate_contracts
    )
    immutable_contracts.extend(f"Expected evidence artifact: {path}" for path in stage.expected_artifact_paths)
    immutable_contracts.extend(f"Harness constraint: {constraint}" for constraint in stage.harness_constraints)
    immutable_contracts.extend(f"Invariant: {item}" for item in stage.invariants)
    immutable_contracts.extend(f"Trusted input/source: {item}" for item in stage.trust_sources)
    immutable_contracts.extend(f"Non-goal boundary: {item}" for item in stage.non_goals)
    stage_gate.interface_contracts = merge_unique(immutable_contracts, stage_gate.interface_contracts)
    stage_gate.pass_criteria = merge_unique(
        [f"Must satisfy StageSpec hard requirements for {stage.name}"],
        list(stage.acceptance_criteria),
        stage_gate.pass_criteria,
    )


def recommended_recovery_for_failure(classification: FailureClassification) -> dict[str, str]:
    """Map a FailureClassification to a recovery recommendation.

    This is the lightweight version used by governance/policy layers.
    For full recovery decisions with backoff parameters, use
    ``errors.recovery.recommended_recovery()``.
    """
    action = "repair"
    if classification.category == "transient":
        action = "retry_with_backoff"
    elif classification.category == "spec_gap":
        action = "rollback_to_spec"
    elif classification.category == "input_contract":
        action = "fix_contract_before_run"
    elif classification.category in ("remote_gate", "timeout"):
        action = "rerun_gate_or_fix_environment"
    elif classification.category == "structured_output":
        action = "retry_structured_call"
    elif classification.category == "planner":
        action = "replan"
    elif classification.category == "promotion":
        action = "hold_promotion"
    elif classification.category == "workspace_state":
        action = "stop_and_replan"
    elif classification.category == "artifact_contract":
        action = "repair_or_complete_artifact"
    elif classification.category == "policy":
        action = "blocked_by_policy"
    elif classification.category == "human_decision":
        action = "human_decision_required"
    return {
        "category": classification.category,
        "disposition": classification.disposition,
        "default_action": action,
    }
