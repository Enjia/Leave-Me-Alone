from __future__ import annotations

import json
import logging
from typing import Any

from core.models import PlanGateReview, StageGate, StageSpec, WorkerDelivery, WorkerPlan
from ports.agent import AgentPort
from core.prompts import judge_plan_gate_prompt, worker_implementation_prompt, worker_planner_prompt, worker_repair_prompt


logger = logging.getLogger(__name__)


async def invoke_worker_delivery_for_round(
    flow: object,
    *,
    worker_name: str,
    agent: AgentPort,
    workspace: Any,
    stage: StageSpec,
    stage_gate: StageGate,
    round_index: int,
    judge_feedback: list[str],
    approved_plan: WorkerPlan,
    auto_check_summary: str,
    context_packet_json: str,
    worker_entry_packet_json: str,
    stage_deadline_monotonic: float,
) -> WorkerDelivery:
    if round_index <= 1:
        return await flow._invoke_agent_structured(
            agent,
            worker_implementation_prompt(
                worker_name,
                stage,
                stage_gate,
                str(workspace),
                judge_feedback,
                approved_plan_json=json.dumps(approved_plan.model_dump(), ensure_ascii=False, indent=2),
                auto_check_summary=auto_check_summary,
                context_packet_json=context_packet_json,
                worker_entry_packet_json=worker_entry_packet_json,
                runtime_nudges_text=flow._latest_runtime_nudges_text(stage_name=stage.name, target=worker_name),
                context_budget_max_chars=getattr(flow.cfg, "context_budget_max_chars", 80_000),
            ),
            WorkerDelivery,
            stage_name=stage.name,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )

    worker_actions = flow._build_worker_required_actions(
        stage_name=stage.name,
        worker_name=worker_name,
        judge_feedback=judge_feedback,
        auto_check_summary=auto_check_summary,
    )
    logger.info(
        "Repair round actions for %s at stage %s round %d: %s",
        worker_name,
        stage.name,
        round_index,
        worker_actions,
    )
    current_workspace_artifacts = flow.workspace_port.capture_artifacts(workspace)
    if not worker_actions:
        logger.info(
            "Skipping repair implementation for %s at stage %s round %d: no worker-scoped actions remain.",
            worker_name,
            stage.name,
            round_index,
        )
        return WorkerDelivery(
            worker=worker_name,
            summary=(
                f"No repair changes required for {worker_name} in round {round_index}; "
                "carried forward prior workspace state."
            ),
            changed_files=[],
            tests_executed=[],
            lint_executed=[],
            perf_executed=[],
            risks=[],
            unresolved_items=[],
        )

    return await flow._invoke_agent_structured(
        agent,
        worker_repair_prompt(
            worker_name,
            stage,
            str(workspace),
            worker_actions,
            approved_plan_json=json.dumps(approved_plan.model_dump(), ensure_ascii=False, indent=2),
            auto_check_summary=auto_check_summary,
            context_packet_json=context_packet_json,
            worker_entry_packet_json=worker_entry_packet_json,
            previous_handoff_json=flow._latest_task_handoff_json(
                stage_name=stage.name,
                worker=worker_name,
                exclude_triggers={"round_start"},
            ),
            current_changed_files=current_workspace_artifacts.changed_files,
            current_status_lines=current_workspace_artifacts.status_lines,
            current_patch=current_workspace_artifacts.patch,
            runtime_nudges_text=flow._latest_runtime_nudges_text(stage_name=stage.name, target=worker_name),
            current_blocker=worker_actions[0] if worker_actions else "",
            context_budget_max_chars=getattr(flow.cfg, "context_budget_max_chars", 80_000),
        ),
        WorkerDelivery,
        stage_name=stage.name,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )


async def invoke_worker_plan_for_round(
    flow: object,
    *,
    worker_name: str,
    agent: AgentPort,
    workspace: Any,
    stage: StageSpec,
    round_index: int,
    judge_feedback: list[str],
    auto_check_summary: str,
    context_packet_json: str,
    worker_entry_packet_json: str,
    stage_deadline_monotonic: float,
) -> WorkerPlan:
    planner_agent = flow._build_read_only_planner_agent(agent)
    plan = await flow._invoke_agent_structured(
        planner_agent,
        worker_planner_prompt(
            worker_name,
            stage,
            str(workspace),
            round_index,
            judge_feedback,
            context_packet_json=context_packet_json,
            auto_check_summary=auto_check_summary,
            worker_entry_packet_json=worker_entry_packet_json,
            runtime_nudges_text=flow._latest_runtime_nudges_text(stage_name=stage.name, target=worker_name),
            context_budget_max_chars=getattr(flow.cfg, "context_budget_max_chars", 80_000),
        ),
        WorkerPlan,
        stage_name=stage.name,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )
    normalized = flow._normalize_worker_plan_payload(plan, worker=worker_name)
    flow._validate_worker_plan(stage=stage, plan=normalized)
    flow._persist_worker_plan(normalized)
    return normalized


async def invoke_plan_gate_review(
    flow: object,
    *,
    stage: StageSpec,
    round_index: int,
    context_packet_json: str,
    worker_plan: WorkerPlan,
    stage_deadline_monotonic: float,
) -> PlanGateReview:
    review = await flow._invoke_agent_structured(
        flow.agents.judge,
        judge_plan_gate_prompt(
            stage,
            round_index,
            json.dumps(worker_plan.model_dump(), ensure_ascii=False, indent=2),
            context_packet_json=context_packet_json,
        ),
        PlanGateReview,
        stage_name=stage.name,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )
    normalized = review.model_copy(
        update={
            "stage_name": stage.name,
            "round_index": round_index,
            "worker_required_actions": [item.strip() for item in review.worker_required_actions if item.strip()],
            "blockers": [item.strip() for item in review.blockers if item.strip()],
            "rationale": review.rationale.strip(),
        }
    )
    flow._persist_plan_gate_review(normalized)
    return normalized
