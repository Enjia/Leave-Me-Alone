from __future__ import annotations

import logging
from typing import Any

from core.models import (
    CheckSummaryArtifact,
    CleanStateArtifact,
    ConvergenceSignal,
    FeatureChecklistArtifact,
    FailureEventArtifact,
    FailureClassification,
    RuntimeNudgeArtifact,
    StageContextPacket,
    StageExecutionPlan,
    StageGateDriftArtifact,
    StageProgressLedger,
    StageSpec,
    TaskHandoffPacket,
    TriageAuditArtifact,
    VerifierReport,
    WorkerDelivery,
    WorkerEntryPacket,
    WorkerPlan,
    BaselineStatusArtifact,
    InitializerReportArtifact,
    PlanDriftArtifact,
    PlanGateReview,
    PromotionReadinessArtifact,
    SpecGapReport,
)
from .stage_artifact_builders import (
    build_remote_preflight_payload,
    build_repo_progress_markdown,
    build_repo_progress_note,
    build_source_preflight_payload,
    build_spec_gap_failure_classification,
    build_spec_gap_report,
    build_stage_completion_artifacts,
    build_stage_spec_snapshot,
)
from core.prompts import SourceRequirementsReport


logger = logging.getLogger(__name__)


def _write_json(flow: object, path: Any, payload: object) -> None:
    flow.artifact_store.write_json(path, payload)


def _write_model(flow: object, path: Any, model: Any) -> None:
    _write_json(flow, path, model.model_dump())


def persist_stage_progress_ledger(flow: object, stage: StageSpec, ledger: StageProgressLedger) -> None:
    path = flow._repo_progress_path(stage, "ledger.json")
    _write_model(flow, path, ledger)
    flow.state.stage_progress_ledgers.setdefault(stage.name, []).append(ledger)


def persist_repo_progress_note(
    flow: object,
    *,
    stage: StageSpec,
    ledger: StageProgressLedger,
    verified_facts: list[str],
    repeated_failure_points: list[str],
    stable_workarounds: list[str],
) -> None:
    note = build_repo_progress_note(
        stage=stage,
        ledger=ledger,
        verified_facts=verified_facts,
        repeated_failure_points=repeated_failure_points,
        stable_workarounds=stable_workarounds,
    )
    flow.artifact_store.write_text(
        flow._repo_progress_path(stage, "progress.md"),
        build_repo_progress_markdown(note),
    )


def persist_feature_checklist(flow: object, artifact: FeatureChecklistArtifact) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(
            artifact.stage_name,
            f"round{artifact.round_index}_feature_checklist.json",
        ),
        artifact,
    )
    flow.state.feature_checklists.setdefault(artifact.stage_name, []).append(artifact)


def persist_worker_entry_packet(flow: object, packet: WorkerEntryPacket) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(
            packet.stage_name,
            f"round{packet.round_index}_{packet.worker}_entry_packet.json",
        ),
        packet,
    )
    flow.state.worker_entry_packets.setdefault(packet.stage_name, []).append(packet)


def persist_baseline_status_artifact(flow: object, artifact: BaselineStatusArtifact) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(
            artifact.stage_name,
            f"round{artifact.round_index}_{artifact.worker}_baseline_status.json",
        ),
        artifact,
    )
    flow.state.baseline_status_artifacts.setdefault(artifact.stage_name, []).append(artifact)


def persist_initializer_artifacts(
    flow: object,
    *,
    stage: StageSpec,
    report: InitializerReportArtifact,
    constraints: Any,
    checklist: FeatureChecklistArtifact,
) -> None:
    _write_model(flow, flow._stage_artifact_path(stage.name, "initializer_report.json"), report)
    _write_json(
        flow,
        flow._stage_artifact_path(stage.name, "active_constraints.json"),
        constraints.model_dump(),
    )
    persist_feature_checklist(flow, checklist)


def persist_clean_state_artifact(flow: object, artifact: CleanStateArtifact) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(
            artifact.stage_name,
            f"round{artifact.round_index}_{artifact.worker}_clean_state.json",
        ),
        artifact,
    )
    flow.state.clean_state_artifacts.setdefault(artifact.stage_name, []).append(artifact)


def persist_stage_execution_plan(flow: object, plan: StageExecutionPlan) -> None:
    _write_model(flow, flow._stage_artifact_path(plan.stage_name, "execution_plan.json"), plan)


def persist_stage_dag_plan(flow: object, plan: Any) -> None:
    payload = plan.model_dump() if hasattr(plan, "model_dump") else plan
    _write_json(flow, flow._artifact_path("stage_dag", "plan.json"), payload)


def persist_context_packet(flow: object, packet: StageContextPacket) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(packet.stage_name, f"round{packet.round_index}_context_packet.json"),
        packet,
    )


def persist_worker_plan(flow: object, plan: WorkerPlan) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(plan.stage_name, f"round{plan.round_index}_{plan.worker}_plan.json"),
        plan,
    )
    flow.state.worker_plans.setdefault(plan.stage_name, []).append(plan)


def persist_plan_gate_review(flow: object, review: PlanGateReview) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(review.stage_name, f"round{review.round_index}_plan_gate_review.json"),
        review,
    )
    flow.state.plan_gate_reviews.setdefault(review.stage_name, []).append(review)


def persist_plan_drift_artifact(flow: object, artifact: PlanDriftArtifact) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(
            artifact.stage_name,
            f"round{artifact.round_index}_{artifact.worker}_plan_drift.json",
        ),
        artifact,
    )
    flow.state.plan_drift_artifacts.setdefault(artifact.stage_name, []).append(artifact)


def persist_worker_delivery(
    flow: object,
    *,
    stage_name: str,
    round_index: int,
    delivery: WorkerDelivery,
) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(stage_name, f"round{round_index}_{delivery.worker}_delivery.json"),
        delivery,
    )


def persist_verifier_report(flow: object, report: VerifierReport) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(report.stage_name, f"round{report.round_index}_verifier_report.json"),
        report,
    )
    flow.state.verifier_reports.setdefault(report.stage_name, []).append(report)


def persist_spec_gap_report(flow: object, report: SpecGapReport) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(report.stage_name, f"round{report.round_index}_spec_gap_report.json"),
        report,
    )
    flow.state.spec_gap_reports.setdefault(report.stage_name, []).append(report)


def persist_and_build_spec_gap_report(
    flow: object,
    *,
    stage: StageSpec,
    round_index: int,
    verifier_report: VerifierReport,
) -> SpecGapReport:
    spec_gap_report = build_spec_gap_report(
        stage=stage,
        round_index=round_index,
        verifier_report=verifier_report,
    )
    persist_spec_gap_report(flow, spec_gap_report)
    persist_failure_event(
        flow,
        FailureEventArtifact(
            stage_name=stage.name,
            round_index=round_index,
            source="spec_gap",
            classification=build_spec_gap_failure_classification(spec_gap_report),
            details=spec_gap_report.model_dump(),
        ),
    )
    return spec_gap_report


def persist_stage_spec_snapshot(flow: object, stage: StageSpec) -> None:
    snapshot = build_stage_spec_snapshot(stage)
    _write_model(flow, flow._stage_artifact_path(stage.name, "stage_spec_snapshot.json"), snapshot)


def persist_failure_event(flow: object, event: FailureEventArtifact) -> None:
    recovery = flow._recommended_recovery_for_failure(event.classification)
    payload = event.model_copy(
        update={"details": {**event.details, "recommended_recovery": recovery}}
    )
    _write_model(
        flow,
        flow._stage_artifact_path(
            payload.stage_name,
            f"round{payload.round_index}_{payload.source}_failure_event.json",
        ),
        payload,
    )


def persist_stage_gate_drift_artifact(flow: object, artifact: StageGateDriftArtifact) -> None:
    _write_model(flow, flow._stage_artifact_path(artifact.stage_name, "stage_gate_drift.json"), artifact)


def persist_triage_audit_artifact(flow: object, artifact: TriageAuditArtifact) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(artifact.stage_name, f"round{artifact.round_index}_triage_audit.json"),
        artifact,
    )


def persist_promotion_readiness_artifact(flow: object, artifact: PromotionReadinessArtifact) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(
            artifact.stage_name,
            f"round{artifact.round_index}_promotion_readiness.json",
        ),
        artifact,
    )


def persist_check_summary_artifact(flow: object, artifact: CheckSummaryArtifact) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(
            artifact.stage_name,
            f"round{artifact.round_index}_{artifact.worker}_{artifact.phase}_checks.json",
        ),
        artifact,
    )
    flow.state.check_summary_artifacts.setdefault(artifact.stage_name, []).append(artifact)
    _emit_check_result_event(flow, artifact)


def _emit_check_result_event(flow: object, artifact: CheckSummaryArtifact) -> None:
    """Emit a CheckResultEvent after persisting a check summary (fail-silent)."""
    emit = getattr(flow, "_emit_event", None)
    if emit is None:
        return
    try:
        from events.models import CheckResultEvent

        failed_count = sum(
            1 for entry in (artifact.entries if hasattr(artifact, "entries") else [])
            if hasattr(entry, "passed") and not entry.passed
        )
        all_passed = failed_count == 0

        emit(CheckResultEvent(
            stage_name=artifact.stage_name,
            round_index=artifact.round_index,
            worker=str(getattr(artifact, "worker", "")),
            check_kind=str(getattr(artifact, "phase", "")),
            passed=all_passed,
            failed_count=failed_count,
        ))
    except Exception:
        pass


def persist_task_handoff_packet(flow: object, packet: TaskHandoffPacket) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(
            packet.stage_name,
            f"round{packet.round_index}_{packet.worker}_{packet.trigger}_handoff.json",
        ),
        packet,
    )
    flow.state.task_handoffs.setdefault(packet.stage_name, []).append(packet)


def persist_convergence_signal(flow: object, signal: ConvergenceSignal) -> None:
    _write_model(
        flow,
        flow._stage_artifact_path(
            signal.stage_name,
            f"round{signal.round_index}_convergence_signal.json",
        ),
        signal,
    )
    flow.state.convergence_signals.setdefault(signal.stage_name, []).append(signal)


def persist_runtime_nudge(flow: object, artifact: RuntimeNudgeArtifact) -> None:
    existing = flow.state.runtime_nudges.get(artifact.stage_name, [])
    dedupe_key = artifact.dedupe_key or f"{artifact.target}:{artifact.category}:{artifact.message}"
    for previous in reversed(existing):
        previous_key = previous.dedupe_key or f"{previous.target}:{previous.category}:{previous.message}"
        if previous.round_index == artifact.round_index and previous_key == dedupe_key:
            return
    _write_model(
        flow,
        flow._stage_artifact_path(
            artifact.stage_name,
            f"round{artifact.round_index}_{artifact.target}_{artifact.category}_nudge.json",
        ),
        artifact,
    )
    flow.state.runtime_nudges.setdefault(artifact.stage_name, []).append(artifact)


def persist_harness_metrics(flow: object) -> None:
    _write_json(
        flow,
        flow._artifact_path("harness", "harness_metrics.json"),
        {"metrics": flow.state.harness_metrics},
    )


def persist_decision_request(flow: object, stage: StageSpec, decisions: list[str]) -> None:
    _write_json(
        flow,
        flow._stage_artifact_path(stage.name, "decision_request.json"),
        {
            "stage_name": stage.name,
            "blocking_decisions": decisions,
            "status": "awaiting_approval",
            "instructions": (
                "Approve each decision by adding it to the "
                "'approved_decisions' list in the flow state, "
                "or use --auto-approve-decisions when running."
            ),
        },
    )
    logger.info("Decision request written to %s", flow._stage_artifact_path(stage.name, "decision_request.json"))


def persist_source_preflight(flow: object, stage: StageSpec, report: SourceRequirementsReport) -> dict[str, Any]:
    payload = build_source_preflight_payload(stage, report)
    _write_json(flow, flow._stage_artifact_path(stage.name, "source_preflight.json"), payload)
    logger.info("Source preflight written: %s", flow._stage_artifact_path(stage.name, "source_preflight.json"))
    return payload


def persist_source_preflight_with_failure(
    flow: object,
    stage: StageSpec,
    report: SourceRequirementsReport,
) -> None:
    payload = persist_source_preflight(flow, stage, report)
    if report.errors or report.truncated:
        evidence = list(report.errors)
        if report.truncated:
            evidence.append("source extraction truncated")
        persist_failure_event(
            flow,
            FailureEventArtifact(
                stage_name=stage.name,
                round_index=0,
                source="source_preflight",
                classification=FailureClassification(
                    code="source_preflight_failed",
                    category="input_contract",
                    disposition="blocked",
                    summary="Stage source preflight failed or produced truncated requirements.",
                    owner="system",
                    retryable=False,
                    evidence=evidence,
                ),
                details=payload,
            ),
        )


def persist_remote_preflight_results(
    flow: object,
    stage: StageSpec,
    worker: str,
    results: list[Any],
) -> str:
    file_name = f"{worker}_remote_preflight.json"
    payload = build_remote_preflight_payload(stage, worker, results)
    _write_json(
        flow,
        flow._stage_artifact_path(stage.name, file_name),
        payload,
    )
    logger.info("Remote preflight written: %s", flow._stage_artifact_path(stage.name, file_name))
    return flow._stage_artifact_ref(stage.name, file_name)


def persist_stage_artifacts(flow: object, stage: StageSpec, result: Any) -> None:
    stage_artifacts = build_stage_completion_artifacts(stage, result)
    for artifact in stage_artifacts:
        path = flow._stage_artifact_path(
            stage.name,
            flow._stage_output_artifact_suffix(artifact.artifact_name),
        )
        _write_model(flow, path, artifact)
        logger.info("Stage artifact written: %s", path)

    flow.state.stage_artifacts[stage.name] = stage_artifacts
