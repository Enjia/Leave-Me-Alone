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
    prev_check_summary_a = ""
    prev_check_summary_b = ""
    final_gate: JudgeGateReview | None = None
    review_memory = list(flow.state.report_memory.get(stage.name, []))
    review_baseline_a = flow.workspace_port.capture_snapshot(flow.agents.worker_a_workspace)
    review_baseline_b = flow.workspace_port.capture_snapshot(flow.agents.worker_b_workspace)
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
            prev_check_summary_a=prev_check_summary_a,
            prev_check_summary_b=prev_check_summary_b,
            review_memory=review_memory,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )
        if isinstance(plan_phase_result, RoundPlanRejected):
            judge_feedback = plan_phase_result.judge_feedback
            rejected_plan_feedback.extend(item for item in plan_phase_result.judge_feedback if item)
            continue
        context_packet = plan_phase_result.context_packet
        context_packet_json = plan_phase_result.context_packet_json
        worker_a_entry_packet = plan_phase_result.worker_a_entry_packet
        worker_b_entry_packet = plan_phase_result.worker_b_entry_packet
        worker_a_plan = plan_phase_result.worker_a_plan
        worker_b_plan = plan_phase_result.worker_b_plan

        delivery_phase_result = await run_round_delivery_phase(
            flow,
            stage=stage,
            stage_gate=stage_gate,
            round_index=round_index,
            judge_feedback=judge_feedback,
            worker_a_plan=worker_a_plan,
            worker_b_plan=worker_b_plan,
            worker_a_entry_packet=worker_a_entry_packet,
            worker_b_entry_packet=worker_b_entry_packet,
            prev_check_summary_a=prev_check_summary_a,
            prev_check_summary_b=prev_check_summary_b,
            context_packet_json=context_packet_json,
            review_baseline_a=review_baseline_a,
            review_baseline_b=review_baseline_b,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )

        review_gate_result = await run_round_review_gate_phase(
            flow,
            stage=stage,
            round_index=round_index,
            context_packet=context_packet,
            context_packet_json=context_packet_json,
            review_memory=review_memory,
            review_baseline_a=review_baseline_a,
            review_baseline_b=review_baseline_b,
            delivery_phase_result=delivery_phase_result,
            no_progress_rounds=no_progress_rounds,
            repeated_failure_rounds=repeated_failure_rounds,
            prev_failure_signature=prev_failure_signature,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )
        review_memory = review_gate_result.review_memory
        final_gate = review_gate_result.final_gate
        auto_checks_a = review_gate_result.auto_checks_a
        auto_checks_b = review_gate_result.auto_checks_b
        check_summary_a = review_gate_result.check_summary_a
        check_summary_b = review_gate_result.check_summary_b
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
            auto_checks_a=auto_checks_a,
            auto_checks_b=auto_checks_b,
            check_summary_a=check_summary_a,
            check_summary_b=check_summary_b,
            review_baseline_a=review_baseline_a,
            review_baseline_b=review_baseline_b,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )
        if isinstance(round_outcome, RoundPassResult):
            return round_outcome.stage_result
        judge_feedback = round_outcome.judge_feedback
        prev_check_summary_a = round_outcome.prev_check_summary_a
        prev_check_summary_b = round_outcome.prev_check_summary_b
        review_baseline_a = round_outcome.review_baseline_a
        review_baseline_b = round_outcome.review_baseline_b

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
