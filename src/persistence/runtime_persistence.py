from __future__ import annotations

from core.models import (
    GovernancePolicySnapshot,
    HarnessRetryPolicy,
    HarnessRoleSpec,
    HarnessSpecSnapshot,
    HarnessStageProfile,
    HarnessStopPolicy,
    RuntimeStatusSnapshot,
    StageDashboardArtifact,
)


def build_governance_policy_snapshot(flow: object) -> GovernancePolicySnapshot:
    return GovernancePolicySnapshot(
        triage_require_reject_rationale=flow.cfg.triage_require_reject_rationale,
        triage_block_fact_high_severity_reject=flow.cfg.triage_block_fact_high_severity_reject,
        promotion_require_all_checks=flow.cfg.promotion_require_all_checks,
        promotion_require_no_open_fact_high_severity=flow.cfg.promotion_require_no_open_fact_high_severity,
        promotion_require_no_disputes=flow.cfg.promotion_require_no_disputes,
        drift_fail_on_suspicious_items=flow.cfg.drift_fail_on_suspicious_items,
        drift_fail_on_extra_commands=flow.cfg.drift_fail_on_extra_commands,
    )


def build_harness_spec_snapshot(flow: object) -> HarnessSpecSnapshot:
    return HarnessSpecSnapshot(
        provider=flow.cfg.provider,
        owner_worker=flow.cfg.owner_worker,
        topology="judge + verifier + worker_a + worker_b",
        roles=[
            HarnessRoleSpec(role="judge", responsibility="Final gate decision, dispute resolution, pass/fail governance."),
            HarnessRoleSpec(role="verifier", responsibility="Criterion-by-criterion contract audit without final gate authority."),
            HarnessRoleSpec(role="worker_a", responsibility="Independent implementation, peer review on worker_b, owner triage for own workspace."),
            HarnessRoleSpec(role="worker_b", responsibility="Independent implementation, peer review on worker_a, owner triage for own workspace."),
            HarnessRoleSpec(role="system", responsibility="Execution planning, checks, persistence, promotion, and fail-closed control."),
        ],
        validation_gates=[
            "stage_gate",
            "plan_gate",
            "remote_preflight",
            "remote_gate",
            "artifact_contract",
            "verifier_review",
            "judge_gate",
            "promotion_readiness",
        ],
        retry_policy=HarnessRetryPolicy(
            max_round_per_stage=flow.state.max_round_per_stage,
            max_no_progress_rounds=flow.cfg.max_no_progress_rounds,
            max_repeated_failure_rounds=flow.cfg.max_repeated_failure_rounds,
        ),
        stop_policy=HarnessStopPolicy(
            enable_convergence_signals=flow.cfg.enable_convergence_signals,
            stop_on_spec_gap=True,
            promotion_require_all_checks=flow.cfg.promotion_require_all_checks,
            promotion_require_no_open_fact_high_severity=flow.cfg.promotion_require_no_open_fact_high_severity,
            promotion_require_no_disputes=flow.cfg.promotion_require_no_disputes,
            drift_fail_on_suspicious_items=flow.cfg.drift_fail_on_suspicious_items,
            drift_fail_on_extra_commands=flow.cfg.drift_fail_on_extra_commands,
        ),
        state_semantics=[
            "stage state is derived from persisted artifacts, not chat history",
            "spec_gap blocks judge and promotion",
            "promotion requires explicit readiness, not only judge pass",
            "no-progress and repeated-failure signals can stop retries early",
        ],
        adapter_policy=[
            "provider=codex is authoritative for execution",
            "stage execution must not route through opencode",
            "remote execution must pass explicit preflight before worker implementation",
        ],
        failure_taxonomy=[
            "spec_gap",
            "structured_output",
            "automated_checks",
            "remote_gate",
            "artifact_contract",
            "planner",
            "workspace_state",
            "promotion",
            "timeout",
            "input_contract",
            "human_decision",
            "unknown",
        ],
        stage_profiles=[
            HarnessStageProfile(
                profile_id="design_probe",
                stage_types=["design_probe"],
                execution_envs=["local_only", "node0_container"],
                required_artifacts=["stage_spec_snapshot", "context_packet", "worker_plan"],
                preferred_validation_gates=["stage_gate", "plan_gate", "verifier_review", "judge_gate"],
                retry_bias="conservative",
                stop_conditions=["spec_gap", "repeated planner failure"],
                notes=["Prefer analysis artifacts over broad code churn."],
            ),
            HarnessStageProfile(
                profile_id="implementation",
                stage_types=["implementation"],
                execution_envs=["local_only", "node0_container", "node1_container"],
                required_artifacts=["worker_plan", "check_summary", "task_handoff"],
                preferred_validation_gates=["plan_gate", "remote_preflight", "artifact_contract", "judge_gate"],
                retry_bias="balanced",
                stop_conditions=["spec_gap", "convergence_guard", "promotion_not_ready"],
                notes=["Use delta patch semantics and plan drift detection."],
            ),
            HarnessStageProfile(
                profile_id="integration",
                stage_types=["integration", "full_regression"],
                execution_envs=["node0_container", "node1_container", "node0_and_node1"],
                required_artifacts=["worker_plan", "check_summary", "verifier_report", "promotion_readiness"],
                preferred_validation_gates=["plan_gate", "remote_preflight", "remote_gate", "artifact_contract", "verifier_review", "judge_gate", "promotion_readiness"],
                retry_bias="conservative",
                stop_conditions=["spec_gap", "remote_gate_repeated_failure", "artifact_contract_failure"],
                notes=["Remote gates and artifact contracts outrank docs-only success signals."],
            ),
        ],
    )


def persist_runtime_status(flow: object, snapshot: RuntimeStatusSnapshot) -> None:
    flow.artifact_store.write_json(
        flow._artifact_path("runtime", "runtime_status.json"),
        snapshot.model_dump(),
    )
    # Cache the latest snapshot so other subsystems (e.g. AgentCallEvent) can
    # read current_round / current_stage without adding fields to ReviewFlowState.
    flow._last_runtime_snapshot = snapshot  # type: ignore[attr-defined]
    _emit_runtime_status_event(flow, snapshot)


def _emit_runtime_status_event(flow: object, snapshot: RuntimeStatusSnapshot) -> None:
    """Map a RuntimeStatusSnapshot to a typed event and emit it to the event bus.

    Phase values used by the orchestrator (as of 2026-04):
      stage_start, stage_passed, stage_failed,
      round_start, plan_gate_review, judge_gate_review
    """
    emit = getattr(flow, "_emit_event", None)
    if emit is None:
        return

    from events.models import (
        RoundFinishedEvent,
        RoundStartedEvent,
        StageFinishedEvent,
        StageStartedEvent,
    )

    phase = snapshot.phase or ""
    stage_name = snapshot.current_stage or ""
    round_index = snapshot.current_round or 0
    overall_state = snapshot.overall_state

    # --- Stage lifecycle ---
    if phase == "stage_start":
        emit(StageStartedEvent(stage_name=stage_name, target_repo=snapshot.target_repo))
        return

    if phase in ("stage_passed", "stage_failed"):
        emit(StageFinishedEvent(
            stage_name=stage_name,
            passed=(phase == "stage_passed"),
            rounds_used=round_index,
            overall_state=overall_state,
        ))
        return

    # --- Round lifecycle ---
    if phase == "round_start":
        emit(RoundStartedEvent(
            stage_name=stage_name,
            round_index=round_index,
            phase=phase,
        ))
        return

    if phase in ("plan_gate_review", "judge_gate_review"):
        # Derive gate_passed from judge_state rather than overall_state.
        # overall_state is typically "running" during gate review, which would
        # make gate_passed always True.  judge_state carries the actual verdict.
        judge_state = (snapshot.judge_state or "").lower()
        gate_passed = judge_state not in ("rejected", "blocked", "plan_rejected")
        emit(RoundFinishedEvent(
            stage_name=stage_name,
            round_index=round_index,
            gate_passed=gate_passed,
            phase=phase,
            worker_states=dict(snapshot.worker_states),
        ))
        return


def persist_governance_policy_snapshot(flow: object) -> None:
    flow.artifact_store.write_json(
        flow._artifact_path("governance", "governance_policy.json"),
        build_governance_policy_snapshot(flow).model_dump(),
    )


def persist_harness_spec_snapshot(flow: object) -> None:
    flow.artifact_store.write_json(
        flow._artifact_path("harness", "harness_spec.json"),
        build_harness_spec_snapshot(flow).model_dump(),
    )


def persist_stage_dashboard_artifact(flow: object, artifact: StageDashboardArtifact) -> None:
    flow.artifact_store.write_json(
        flow._stage_artifact_path(artifact.stage_name, "dashboard.json"),
        artifact.model_dump(),
    )
