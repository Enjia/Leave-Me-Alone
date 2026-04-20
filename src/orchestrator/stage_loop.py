from __future__ import annotations

from core.models import JudgeGateReview, StageExecutionPlan, StageGate, StageResult, StageRoundLog, StageSpec
from orchestrator.round_runner import (
    RoundPassResult,
    RoundPlanRejected,
    apply_round_outcome,
    run_round_delivery_phase,
    run_round_plan_phase,
    run_round_review_gate_phase,
)
from orchestrator.stage_runner import finalize_failed_stage


async def run_stage_round_loop(
    flow: object,
    *,
    stage: StageSpec,
    stage_plan: StageExecutionPlan,
    stage_deadline_monotonic: float,
    idle_timeout_sec: int,
    stage_gate: StageGate,
    max_round: int,
) -> StageResult:
    del idle_timeout_sec

    round_logs: list[StageRoundLog] = []
    judge_feedback: list[str] = []
    rejected_plan_feedback: list[str] = []
    prev_check_summary = ""
    final_gate: JudgeGateReview | None = None
    review_memory = list(flow.state.report_memory.get(stage.name, []))
    review_baseline = flow.workspace_port.capture_snapshot(flow.agents.worker_workspace)
    flow._current_stage_gate = stage_gate
    no_progress_rounds = 0
    repeated_failure_rounds = 0
    prev_failure_signature = ""

    for round_index in range(1, max_round + 1):
        plan_phase_result = await run_round_plan_phase(
            flow,
            stage=stage,
            stage_plan=stage_plan,
            round_index=round_index,
            judge_feedback=judge_feedback,
            prev_check_summary=prev_check_summary,
            review_memory=review_memory,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )
        if isinstance(plan_phase_result, RoundPlanRejected):
            judge_feedback = plan_phase_result.judge_feedback
            rejected_plan_feedback.extend(item for item in plan_phase_result.judge_feedback if item)
            continue
        context_packet = plan_phase_result.context_packet
        context_packet_json = plan_phase_result.context_packet_json
        worker_entry_packet = plan_phase_result.worker_entry_packet
        worker_plan = plan_phase_result.worker_plan

        delivery_phase_result = await run_round_delivery_phase(
            flow,
            stage=stage,
            stage_gate=stage_gate,
            round_index=round_index,
            judge_feedback=judge_feedback,
            worker_plan=worker_plan,
            worker_entry_packet=worker_entry_packet,
            prev_check_summary=prev_check_summary,
            context_packet_json=context_packet_json,
            review_baseline=review_baseline,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )

        review_gate_result = await run_round_review_gate_phase(
            flow,
            stage=stage,
            round_index=round_index,
            context_packet=context_packet,
            context_packet_json=context_packet_json,
            review_memory=review_memory,
            review_baseline=review_baseline,
            delivery_phase_result=delivery_phase_result,
            no_progress_rounds=no_progress_rounds,
            repeated_failure_rounds=repeated_failure_rounds,
            prev_failure_signature=prev_failure_signature,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )
        review_memory = review_gate_result.review_memory
        final_gate = review_gate_result.final_gate
        auto_checks = review_gate_result.auto_checks
        check_summary = review_gate_result.check_summary
        no_progress_rounds = review_gate_result.no_progress_rounds
        repeated_failure_rounds = review_gate_result.repeated_failure_rounds
        prev_failure_signature = review_gate_result.prev_failure_signature
        round_logs.append(review_gate_result.round_log)

        round_outcome = await apply_round_outcome(
            flow,
            stage=stage,
            round_index=round_index,
            round_logs=round_logs,
            review_memory=review_memory,
            final_gate=final_gate,
            auto_checks=auto_checks,
            check_summary=check_summary,
            review_baseline=review_baseline,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )
        if isinstance(round_outcome, RoundPassResult):
            return round_outcome.stage_result
        judge_feedback = round_outcome.judge_feedback
        prev_check_summary = round_outcome.prev_check_summary
        review_baseline = round_outcome.review_baseline

    if final_gate is None:
        deduped_actions = list(dict.fromkeys(rejected_plan_feedback or judge_feedback))
        final_gate = JudgeGateReview(
            stage_name=stage.name,
            round_index=max_round,
            pass_gate=False,
            high_severity_open=[],
            disputed_items=[],
            required_actions=deduped_actions,
            rationale=(
                "All available rounds were rejected at plan_gate. "
                "Stage failed closed before implementation."
            ),
        )
    return finalize_failed_stage(
        flow,
        stage=stage,
        max_round=max_round,
        final_gate=final_gate,
        round_logs=round_logs,
    )
