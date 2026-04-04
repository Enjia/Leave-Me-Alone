from __future__ import annotations

import json
from pathlib import Path

from adapters.workspace_ops import owner_workspace_path, promote_owner_workspace
from core.models import (
    CheckSummaryArtifact,
    ConvergenceSignal,
    JudgeGateReview,
    ReportMemoryEntry,
    StageContextPacket,
    StageSpec,
    TaskHandoffPacket,
    WorkerDelivery,
)
from ports.workspace import WorkspaceArtifactsLike
from policy.runtime_artifacts import (
    build_acceptance_backlog,
    build_check_summary_artifact,
    build_clean_state_artifact,
    build_convergence_signal,
    build_promotion_readiness_artifact,
    build_round_start_handoff_packet,
    build_stage_dashboard_artifact,
    build_stage_gate_drift_artifact,
    build_task_handoff_packet,
    build_terminal_handoff_packet,
    build_triage_audit_artifact,
    build_worker_entry_packet,
    classify_check_failure,
    combine_failure_signatures,
    has_only_non_substantive_delta,
    is_non_substantive_changed_file,
    run_stage_baseline_sanity,
    summarize_check_failure,
)
from state.memory_gc import (
    downsample_stage_result,
    gc_stage_memory,
)
from state.runtime_controls import (
    bump_metric,
    check_budget_for_agent_call,
    check_budget_hard_limit,
    check_stage_budget,
    get_cost_snapshot,
    persist_cost_ledger,
    read_positive_env_int,
    record_usage,
    remaining_stage_budget_sec,
    reset_stage_cost,
    resolve_agent_timeout_sec,
    resolve_stage_timeout_sec,
)


class FlowRuntimeFacadeMixin:
    def _build_worker_entry_packet(self, **kwargs: object):
        return build_worker_entry_packet(self, **kwargs)

    def _run_stage_baseline_sanity(self, *, stage: StageSpec, round_index: int):
        return run_stage_baseline_sanity(self, stage=stage, round_index=round_index)

    @staticmethod
    def _build_clean_state_artifact(**kwargs: object):
        return build_clean_state_artifact(**kwargs)

    def _build_check_summary_artifact(self, **kwargs: object) -> CheckSummaryArtifact:
        return build_check_summary_artifact(self, **kwargs)

    def _build_stage_gate_drift_artifact(self, *, stage: StageSpec, raw_stage_gate: object):
        return build_stage_gate_drift_artifact(self, stage=stage, raw_stage_gate=raw_stage_gate)

    def _build_triage_audit_artifact(self, **kwargs: object):
        return build_triage_audit_artifact(self, **kwargs)

    def _build_promotion_readiness_artifact(self, **kwargs: object):
        return build_promotion_readiness_artifact(self, **kwargs)

    def _build_stage_dashboard_artifact(self, **kwargs: object):
        return build_stage_dashboard_artifact(self, **kwargs)

    def _classify_check_failure(self, **kwargs: object):
        return classify_check_failure(self, **kwargs)

    @staticmethod
    def _summarize_check_failure(stdout: str, stderr: str) -> str:
        return summarize_check_failure(stdout, stderr)

    def _build_task_handoff_packet(
        self,
        *,
        worker: str,
        trigger: str,
        stage: StageSpec,
        round_index: int,
        delivery: WorkerDelivery,
        patch: WorkspaceArtifactsLike,
        check_artifact: CheckSummaryArtifact,
        context_packet: StageContextPacket,
        judge_feedback: list[str],
        review_memory: list[ReportMemoryEntry],
    ) -> TaskHandoffPacket:
        return build_task_handoff_packet(
            self,
            worker=worker,
            trigger=trigger,
            stage=stage,
            round_index=round_index,
            delivery=delivery,
            patch=patch,
            check_artifact=check_artifact,
            context_packet=context_packet,
            judge_feedback=judge_feedback,
            review_memory=review_memory,
        )

    def _build_round_start_handoff_packet(
        self,
        *,
        worker: str,
        stage: StageSpec,
        round_index: int,
        context_packet: StageContextPacket,
        review_memory: list[ReportMemoryEntry],
    ) -> TaskHandoffPacket:
        return build_round_start_handoff_packet(
            self,
            worker=worker,
            stage=stage,
            round_index=round_index,
            context_packet=context_packet,
            review_memory=review_memory,
        )

    def _build_terminal_handoff_packet(
        self,
        *,
        worker: str,
        stage: StageSpec,
        round_index: int,
        final_gate: JudgeGateReview,
        trigger: str = "stage_fail",
    ) -> TaskHandoffPacket:
        return build_terminal_handoff_packet(
            self,
            worker=worker,
            stage=stage,
            round_index=round_index,
            final_gate=final_gate,
            trigger=trigger,
        )

    @staticmethod
    def _build_acceptance_backlog(stage: StageSpec) -> list[str]:
        return build_acceptance_backlog(stage)

    def _latest_task_handoff_json(
        self,
        *,
        stage_name: str,
        worker: str,
        exclude_triggers: set[str] | None = None,
    ) -> str:
        excluded = exclude_triggers or set()
        packets = self.state.task_handoffs.get(stage_name, [])
        for packet in reversed(packets):
            if packet.worker == worker and packet.trigger not in excluded:
                return json.dumps(packet.model_dump(), ensure_ascii=False, indent=2)
        return ""

    def _build_convergence_signal(self, **kwargs: object) -> ConvergenceSignal:
        return build_convergence_signal(self, **kwargs)

    @staticmethod
    def _has_only_non_substantive_delta(
        patch_a: WorkspaceArtifactsLike,
        patch_b: WorkspaceArtifactsLike,
    ) -> bool:
        return has_only_non_substantive_delta(patch_a, patch_b)

    @staticmethod
    def _is_non_substantive_changed_file(path: str) -> bool:
        return is_non_substantive_changed_file(path)

    @staticmethod
    def _combine_failure_signatures(*values: str) -> str:
        return combine_failure_signatures(*values)

    def _promote_owner_workspace(self, stage: StageSpec) -> str | None:
        return promote_owner_workspace(self, stage)

    def _owner_workspace_path(self) -> Path:
        return owner_workspace_path(self)

    def _bump_metric(self, key: str, amount: int = 1) -> None:
        bump_metric(self, key, amount)

    def _record_usage(self, **kwargs: object) -> None:
        record_usage(self, **kwargs)

    def _get_cost_snapshot(self):
        return get_cost_snapshot(self)

    def _check_budget_hard_limit(self) -> bool:
        return check_budget_hard_limit(self)

    def _check_stage_budget(self, stage_name: str) -> str | None:
        return check_stage_budget(self, stage_name)

    def _persist_cost_ledger(self) -> None:
        persist_cost_ledger(self)

    def _reset_stage_cost(self, stage_name: str) -> None:
        reset_stage_cost(self, stage_name)

    def _gc_stage_memory(self, stage_name: str, result: object) -> None:
        """Downsample round logs and purge stage-scoped state after completion."""
        from core.models import StageResult
        if isinstance(result, StageResult):
            downsample_stage_result(result)
        gc_stage_memory(self, stage_name)

    def _check_budget_for_agent_call(self) -> None:
        check_budget_for_agent_call(self)

    @staticmethod
    def _read_positive_env_int(key: str, default: int) -> int:
        return read_positive_env_int(key, default)

    def _resolve_stage_timeout_sec(self) -> int:
        return resolve_stage_timeout_sec()

    def _resolve_agent_timeout_sec(self) -> int:
        return resolve_agent_timeout_sec()

    @staticmethod
    def _remaining_stage_budget_sec(
        *,
        stage_name: str,
        stage_deadline_monotonic: float,
    ) -> int:
        return remaining_stage_budget_sec(
            stage_name=stage_name,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )
