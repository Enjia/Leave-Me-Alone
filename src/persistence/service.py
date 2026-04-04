from __future__ import annotations

from typing import Any

from core.models import (
    ActiveConstraintsArtifact,
    BaselineStatusArtifact,
    CheckSummaryArtifact,
    CleanStateArtifact,
    ConvergenceSignal,
    FeatureChecklistArtifact,
    FailureEventArtifact,
    PromotionReadinessArtifact,
    RuntimeNudgeArtifact,
    RuntimeStatusSnapshot,
    StageContextPacket,
    StageDashboardArtifact,
    StageExecutionPlan,
    StageGateDriftArtifact,
    StageProgressLedger,
    StageResult,
    StageSpec,
    TaskHandoffPacket,
    TriageAuditArtifact,
    VerifierReport,
    WorkerDelivery,
    WorkerEntryPacket,
    WorkerPlan,
    InitializerReportArtifact,
    PlanDriftArtifact,
    PlanGateReview,
    SpecGapReport,
)
from core.prompts import SourceRequirementsReport
from .runtime_persistence import (
    persist_governance_policy_snapshot,
    persist_harness_spec_snapshot,
    persist_runtime_status,
    persist_stage_dashboard_artifact,
)
from .stage_artifacts import (
    persist_and_build_spec_gap_report,
    persist_baseline_status_artifact,
    persist_check_summary_artifact,
    persist_clean_state_artifact,
    persist_context_packet,
    persist_convergence_signal,
    persist_decision_request,
    persist_failure_event,
    persist_feature_checklist,
    persist_harness_metrics,
    persist_initializer_artifacts,
    persist_plan_drift_artifact,
    persist_plan_gate_review,
    persist_promotion_readiness_artifact,
    persist_remote_preflight_results,
    persist_repo_progress_note,
    persist_runtime_nudge,
    persist_source_preflight_with_failure,
    persist_spec_gap_report,
    persist_stage_artifacts,
    persist_stage_dag_plan,
    persist_stage_execution_plan,
    persist_stage_gate_drift_artifact,
    persist_stage_progress_ledger,
    persist_stage_spec_snapshot,
    persist_task_handoff_packet,
    persist_triage_audit_artifact,
    persist_verifier_report,
    persist_worker_delivery,
    persist_worker_entry_packet,
    persist_worker_plan,
)


class PersistenceService:
    def persist_stage_progress_ledger(self, flow: object, stage: StageSpec, ledger: StageProgressLedger) -> None:
        persist_stage_progress_ledger(flow, stage, ledger)

    def persist_repo_progress_note(self, flow: object, **kwargs: object) -> None:
        persist_repo_progress_note(flow, **kwargs)

    def persist_feature_checklist(self, flow: object, artifact: FeatureChecklistArtifact) -> None:
        persist_feature_checklist(flow, artifact)

    def persist_worker_entry_packet(self, flow: object, packet: WorkerEntryPacket) -> None:
        persist_worker_entry_packet(flow, packet)

    def persist_baseline_status_artifact(self, flow: object, artifact: BaselineStatusArtifact) -> None:
        persist_baseline_status_artifact(flow, artifact)

    def persist_initializer_artifacts(
        self,
        flow: object,
        *,
        stage: StageSpec,
        report: InitializerReportArtifact,
        constraints: ActiveConstraintsArtifact,
        checklist: FeatureChecklistArtifact,
    ) -> None:
        persist_initializer_artifacts(
            flow,
            stage=stage,
            report=report,
            constraints=constraints,
            checklist=checklist,
        )

    def persist_clean_state_artifact(self, flow: object, artifact: CleanStateArtifact) -> None:
        persist_clean_state_artifact(flow, artifact)

    def persist_stage_execution_plan(self, flow: object, plan: StageExecutionPlan) -> None:
        persist_stage_execution_plan(flow, plan)

    def persist_stage_dag_plan(self, flow: object, plan: Any) -> None:
        persist_stage_dag_plan(flow, plan)

    def persist_context_packet(self, flow: object, packet: StageContextPacket) -> None:
        persist_context_packet(flow, packet)

    def persist_worker_plan(self, flow: object, plan: WorkerPlan) -> None:
        persist_worker_plan(flow, plan)

    def persist_plan_gate_review(self, flow: object, review: PlanGateReview) -> None:
        persist_plan_gate_review(flow, review)

    def persist_plan_drift_artifact(self, flow: object, artifact: PlanDriftArtifact) -> None:
        persist_plan_drift_artifact(flow, artifact)

    def persist_worker_delivery(
        self,
        flow: object,
        *,
        stage_name: str,
        round_index: int,
        delivery: WorkerDelivery,
    ) -> None:
        persist_worker_delivery(flow, stage_name=stage_name, round_index=round_index, delivery=delivery)

    def persist_verifier_report(self, flow: object, report: VerifierReport) -> None:
        persist_verifier_report(flow, report)

    def persist_spec_gap_report(self, flow: object, report: SpecGapReport) -> None:
        persist_spec_gap_report(flow, report)

    def persist_and_build_spec_gap_report(
        self,
        flow: object,
        *,
        stage: StageSpec,
        round_index: int,
        verifier_report: VerifierReport,
    ) -> SpecGapReport:
        return persist_and_build_spec_gap_report(
            flow,
            stage=stage,
            round_index=round_index,
            verifier_report=verifier_report,
        )

    def persist_stage_spec_snapshot(self, flow: object, stage: StageSpec) -> None:
        persist_stage_spec_snapshot(flow, stage)

    def persist_failure_event(self, flow: object, event: FailureEventArtifact) -> None:
        persist_failure_event(flow, event)

    def persist_runtime_status(self, flow: object, snapshot: RuntimeStatusSnapshot) -> None:
        persist_runtime_status(flow, snapshot)

    def persist_governance_policy_snapshot(self, flow: object) -> None:
        persist_governance_policy_snapshot(flow)

    def persist_harness_spec_snapshot(self, flow: object) -> None:
        persist_harness_spec_snapshot(flow)

    def persist_stage_dashboard_artifact(self, flow: object, artifact: StageDashboardArtifact) -> None:
        persist_stage_dashboard_artifact(flow, artifact)

    def persist_stage_gate_drift_artifact(self, flow: object, artifact: StageGateDriftArtifact) -> None:
        persist_stage_gate_drift_artifact(flow, artifact)

    def persist_triage_audit_artifact(self, flow: object, artifact: TriageAuditArtifact) -> None:
        persist_triage_audit_artifact(flow, artifact)

    def persist_promotion_readiness_artifact(self, flow: object, artifact: PromotionReadinessArtifact) -> None:
        persist_promotion_readiness_artifact(flow, artifact)

    def persist_check_summary_artifact(self, flow: object, artifact: CheckSummaryArtifact) -> None:
        persist_check_summary_artifact(flow, artifact)

    def persist_task_handoff_packet(self, flow: object, packet: TaskHandoffPacket) -> None:
        persist_task_handoff_packet(flow, packet)

    def persist_convergence_signal(self, flow: object, signal: ConvergenceSignal) -> None:
        persist_convergence_signal(flow, signal)

    def persist_runtime_nudge(self, flow: object, artifact: RuntimeNudgeArtifact) -> None:
        persist_runtime_nudge(flow, artifact)

    def persist_harness_metrics(self, flow: object) -> None:
        persist_harness_metrics(flow)

    def persist_decision_request(self, flow: object, stage: StageSpec, decisions: list[str]) -> None:
        persist_decision_request(flow, stage, decisions)

    def persist_source_preflight(
        self,
        flow: object,
        stage: StageSpec,
        report: SourceRequirementsReport,
    ) -> None:
        persist_source_preflight_with_failure(flow, stage, report)

    def persist_remote_preflight_results(
        self,
        flow: object,
        stage: StageSpec,
        worker: str,
        results: list[Any],
    ) -> str:
        return persist_remote_preflight_results(flow, stage, worker, results)

    def persist_stage_artifacts(self, flow: object, stage: StageSpec, result: StageResult) -> None:
        persist_stage_artifacts(flow, stage, result)
