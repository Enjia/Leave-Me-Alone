from __future__ import annotations

from pathlib import Path
from typing import Any

from core.models import (
    CheckSummaryArtifact,
    FailureClassification,
    OwnerTriageResult,
    PeerReviewResult,
    PlanDriftArtifact,
    StageGate,
    StageSpec,
    VerifierReport,
    WorkerPlan,
)
from policy.governance import (
    merge_stage_gate_with_stage_spec,
    recommended_recovery_for_failure,
)
from policy.review_payloads import (
    build_judge_gate_payload,
    build_verifier_payload,
    normalize_stage_gate_payload,
    normalize_worker_delivery_payload,
    normalize_worker_plan_payload,
    validate_worker_plan,
)
from policy.stage_contracts import (
    check_blocking_decisions,
    check_required_inputs,
    hydrate_stage_spec_defaults,
    is_nonempty_json_value,
    lookup_json_path,
    validate_stage_catalog,
    validate_stage_definition,
    validate_stage_outputs,
)
from ports.workspace import WorkspaceArtifactsLike


class FlowReviewFacadeMixin:
    def _normalize_worker_plan_payload(
        self,
        payload: WorkerPlan | dict[str, Any],
        *,
        worker: str,
    ) -> WorkerPlan:
        return normalize_worker_plan_payload(self, payload, worker=worker)

    def _validate_worker_plan(
        self,
        *,
        stage: StageSpec,
        plan: WorkerPlan,
    ) -> None:
        validate_worker_plan(self, stage=stage, plan=plan)

    @staticmethod
    def _normalize_worker_delivery_payload(
        payload: dict[str, Any],
        worker: str,
    ) -> dict[str, Any]:
        return normalize_worker_delivery_payload(payload, worker)

    @staticmethod
    def _normalize_stage_gate_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return normalize_stage_gate_payload(payload)

    @staticmethod
    def _build_verifier_payload(
        *,
        stage: StageSpec,
        stage_name: str,
        patch_a: WorkspaceArtifactsLike,
        patch_b: WorkspaceArtifactsLike,
        review_a_on_b: PeerReviewResult,
        review_b_on_a: PeerReviewResult,
        triage_a: OwnerTriageResult,
        triage_b: OwnerTriageResult,
        check_artifact_a: CheckSummaryArtifact,
        check_artifact_b: CheckSummaryArtifact,
        drift_a: PlanDriftArtifact,
        drift_b: PlanDriftArtifact,
    ) -> dict[str, Any]:
        return build_verifier_payload(
            stage=stage,
            stage_name=stage_name,
            patch_a=patch_a,
            patch_b=patch_b,
            review_a_on_b=review_a_on_b,
            review_b_on_a=review_b_on_a,
            triage_a=triage_a,
            triage_b=triage_b,
            check_artifact_a=check_artifact_a,
            check_artifact_b=check_artifact_b,
            drift_a=drift_a,
            drift_b=drift_b,
        )

    @staticmethod
    def _build_judge_gate_payload(
        stage_name: str,
        review_a_on_b: PeerReviewResult,
        review_b_on_a: PeerReviewResult,
        triage_a: OwnerTriageResult,
        triage_b: OwnerTriageResult,
        verifier_report: VerifierReport,
    ) -> dict[str, Any]:
        return build_judge_gate_payload(
            stage_name,
            review_a_on_b,
            review_b_on_a,
            triage_a,
            triage_b,
            verifier_report,
        )

    def _check_required_inputs(self, stage: StageSpec) -> list[str]:
        return check_required_inputs(self, stage)

    def _merge_stage_gate_with_stage_spec(self, stage: StageSpec, stage_gate: StageGate) -> None:
        merge_stage_gate_with_stage_spec(stage, stage_gate)

    def _validate_stage_definition(self, stage: StageSpec) -> list[str]:
        return validate_stage_definition(self, stage)

    @staticmethod
    def _hydrate_stage_spec_defaults(stage: StageSpec) -> None:
        hydrate_stage_spec_defaults(stage)

    @staticmethod
    def _validate_stage_catalog(stages: list[StageSpec]) -> list[str]:
        return validate_stage_catalog(stages)

    def _validate_stage_outputs(
        self,
        stage: StageSpec,
        *,
        base_dir: Path | None = None,
    ) -> list[str]:
        return validate_stage_outputs(self, stage, base_dir=base_dir)

    @staticmethod
    def _lookup_json_path(payload: Any, key_path: str) -> tuple[bool, Any]:
        return lookup_json_path(payload, key_path)

    @staticmethod
    def _is_nonempty_json_value(value: Any) -> bool:
        return is_nonempty_json_value(value)

    def _check_blocking_decisions(self, stage: StageSpec) -> list[str]:
        return check_blocking_decisions(self, stage)

    @staticmethod
    def _recommended_recovery_for_failure(classification: FailureClassification) -> dict[str, str]:
        return recommended_recovery_for_failure(classification)
