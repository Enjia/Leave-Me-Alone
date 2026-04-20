from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from core.models import (
    ActiveConstraintsArtifact,
    FailureClassification,
    FailureEventArtifact,
    FeatureChecklistArtifact,
    InitializerReportArtifact,
    JudgeGateReview,
    RuntimeStatusSnapshot,
    StageExecutionPlan,
    StageGate,
    StageResult,
    StageRoundLog,
    StageSpec,
)
from engine.orchestration import build_stage_execution_plan
from core.prompts import judge_stage_gate_prompt


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StageBootstrap:
    stage_plan: StageExecutionPlan
    stage_deadline_monotonic: float
    idle_timeout_sec: int
    stage_gate: StageGate
    max_round: int


async def _await_stage_setup_tasks(
    initialize_operation: object,
    preflight_operation: object,
) -> tuple[StageBootstrap | None, StageResult | None]:
    initialize_task = asyncio.create_task(initialize_operation)
    preflight_task = asyncio.create_task(preflight_operation)
    tasks = [initialize_task, preflight_task]
    try:
        bootstrap: StageBootstrap | None = None
        preflight_result: StageResult | None = None
        pending: set[asyncio.Task[object]] = {initialize_task, preflight_task}
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            first_exception: BaseException | None = None
            blocked_preflight_seen = False
            for task in done:
                try:
                    result = task.result()
                except BaseException as exc:
                    if first_exception is None:
                        first_exception = exc
                    continue
                if task is initialize_task:
                    bootstrap = result
                else:
                    preflight_result = result
                    if preflight_result is not None:
                        blocked_preflight_seen = True
            if first_exception is not None:
                for pending_task in pending:
                    pending_task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise first_exception
            if blocked_preflight_seen:
                for pending_task in pending:
                    pending_task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                return bootstrap, preflight_result
        return bootstrap, preflight_result
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def run_single_stage(flow: object, stage: StageSpec) -> StageResult:
    bootstrap, preflight_result = await _await_stage_setup_tasks(
        initialize_stage(flow, stage),
        run_remote_preflight(flow, stage),
    )
    if preflight_result is not None:
        return preflight_result
    return await flow._run_single_stage_impl_after_preflight(
        stage=stage,
        stage_plan=bootstrap.stage_plan,
        stage_deadline_monotonic=bootstrap.stage_deadline_monotonic,
        idle_timeout_sec=bootstrap.idle_timeout_sec,
        stage_gate=bootstrap.stage_gate,
        max_round=bootstrap.max_round,
    )


async def initialize_stage(flow: object, stage: StageSpec) -> StageBootstrap:
    flow._persist_runtime_status(
        RuntimeStatusSnapshot(
            target_repo=flow.state.target_repo,
            current_stage=stage.name,
            current_round=0,
            phase="stage_start",
            overall_state="running",
            worker_states={"worker": "idle"},
            judge_state="planning_stage_gate",
            latest_artifacts=[flow._stage_artifact_ref(stage.name, "stage_spec_snapshot.json")],
            notes=[f"Starting stage {stage.name}."],
        )
    )
    flow._persist_stage_dashboard_artifact(
        flow._build_stage_dashboard_artifact(
            stage=stage,
            status="running",
            current_round=0,
            worker_states={"worker": "idle"},
            judge_state="planning_stage_gate",
            unresolved_actions=[],
            latest_artifacts=[flow._stage_artifact_ref(stage.name, "stage_spec_snapshot.json")],
        )
    )
    initial_subgoal_id, initial_subgoal_title, _ = flow._select_active_subgoal(
        stage=stage,
        round_index=1,
    )
    flow._persist_initializer_artifacts(
        stage=stage,
        report=InitializerReportArtifact(
            stage_name=stage.name,
            round_index=0,
            objective=stage.objective,
            source_file=stage.source_file,
            seed_artifacts=[
                str(path)
                for path in sorted((flow.cfg.seed_artifacts_dir or Path()).glob("*"))
            ][:20]
            if flow.cfg.seed_artifacts_dir and flow.cfg.seed_artifacts_dir.exists()
            else [],
            initialization_steps=[
                "persist stage spec snapshot",
                "run remote preflight",
                "persist active constraints",
                "persist feature checklist",
            ],
            environment_ready=True,
            notes=["Initializer contract generated before round execution."],
        ),
        constraints=ActiveConstraintsArtifact(
            stage_name=stage.name,
            round_index=0,
            active_subgoal_id=initial_subgoal_id,
            active_subgoal_title=initial_subgoal_title,
            immutable_requirements=list(stage.invariants) + list(stage.acceptance_criteria),
            frozen_non_goals=list(stage.non_goals),
            allowed_write_scope=list(stage.write_scope or stage.scope_hint),
            notes=["Stage-start active constraints."],
        ),
        checklist=FeatureChecklistArtifact(
            stage_name=stage.name,
            stage_id=stage.stage_id,
            round_index=0,
            items=list(stage.feature_checklist),
        ),
    )
    initial_ledger = flow._build_stage_progress_ledger(
        stage=stage,
        round_index=0,
        status="running",
        passed_gates=["stage_initialized"],
        latest_artifacts=[
            flow._stage_artifact_ref(stage.name, "stage_spec_snapshot.json"),
            flow._stage_artifact_ref(stage.name, "initializer_report.json"),
            flow._stage_artifact_ref(stage.name, "active_constraints.json"),
        ],
        notes=["Stage initialized."],
    )
    flow._persist_stage_progress_ledger(stage, initial_ledger)
    flow._persist_repo_progress_note(
        stage=stage,
        ledger=initial_ledger,
        verified_facts=["Stage initialization artifacts persisted."],
        repeated_failure_points=[],
        stable_workarounds=[],
    )
    stage_plan = build_stage_execution_plan(stage)
    flow.state.stage_execution_plans[stage.name] = stage_plan
    flow._persist_stage_execution_plan(stage_plan)
    if stage_plan.validation_errors:
        flow._persist_failure_event(
            FailureEventArtifact(
                stage_name=stage.name,
                round_index=0,
                source="stage_execution_plan",
                classification=FailureClassification(
                    code="stage_execution_plan_invalid",
                    category="planner",
                    disposition="blocked",
                    summary="Stage execution plan failed validation.",
                    owner="system",
                    retryable=False,
                    evidence=stage_plan.validation_errors,
                ),
                details={"errors": stage_plan.validation_errors},
            )
        )
        raise ValueError(
            f"Stage '{stage.name}' execution plan failed validation: "
            f"{stage_plan.validation_errors}"
        )

    stage_timeout_sec = flow._resolve_stage_timeout_sec()
    idle_timeout_sec = flow._read_positive_env_int(
        "MULTI_CODEX_AGENT_IDLE_TIMEOUT_SEC",
        600,
    )
    # Expose per-stage abnormal idle timeout for downstream agent invocation.
    # This value is consumed as an upper-bound timeout hint in structured agent calls.
    flow._current_stage_idle_timeout_sec = idle_timeout_sec
    stage_deadline_monotonic = time.monotonic() + stage_timeout_sec
    logger.info(
        "Stage %s timeout policy: stage_timeout=%ss (hard cap per-stage), "
        "abnormal_idle_timeout=%ss. "
        "Abnormal condition is strict: no stdout/stderr output AND no workspace "
        "file mtime change within idle window.",
        stage.name,
        stage_timeout_sec,
        idle_timeout_sec,
    )
    stage_gate, max_round = await prepare_stage_gate(
        flow,
        stage=stage,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )
    return StageBootstrap(
        stage_plan=stage_plan,
        stage_deadline_monotonic=stage_deadline_monotonic,
        idle_timeout_sec=idle_timeout_sec,
        stage_gate=stage_gate,
        max_round=max_round,
    )


async def prepare_stage_gate(
    flow: object,
    *,
    stage: StageSpec,
    stage_deadline_monotonic: float,
) -> tuple[StageGate, int]:
    try:
        stage_gate = await flow._invoke_agent_structured(
            flow.agents.judge,
            judge_stage_gate_prompt(stage, flow.state.max_round_per_stage),
            StageGate,
            stage_name=stage.name,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )
    except Exception as exc:
        logger.warning(
            "Judge stage-gate generation failed for %s; using StageSpec fallback gate. Error: %s",
            stage.name,
            str(exc)[:600],
        )
        flow._bump_metric("judge_stage_gate_fallback_count")
        flow._persist_failure_event(
            FailureEventArtifact(
                stage_name=stage.name,
                round_index=0,
                source="judge_stage_gate",
                classification=FailureClassification(
                    code="judge_stage_gate_fallback",
                    category="structured_output",
                    disposition="retry_next_round",
                    summary="Judge failed to produce structured StageGate; fallback gate was used.",
                    owner="judge",
                    retryable=True,
                    evidence=[str(exc)[:600]],
                ),
                details={"exception": str(exc)[:1200]},
            )
        )
        fallback_contracts = [f"Expected evidence artifact: {path}" for path in stage.expected_artifact_paths]
        fallback_contracts.extend(f"Harness constraint: {item}" for item in stage.harness_constraints)
        fallback_contracts.extend(f"Invariant: {item}" for item in stage.invariants)
        fallback_contracts.extend(f"Trusted input/source: {item}" for item in stage.trust_sources)
        stage_gate = StageGate(
            stage_name=stage.name,
            objective=stage.objective,
            test_commands=list(stage.test_commands),
            lint_commands=list(stage.lint_commands),
            perf_checks=list(stage.perf_checks),
            interface_contracts=fallback_contracts,
            pass_criteria=[
                f"Must satisfy StageSpec hard requirements for {stage.name}",
                "Fallback gate was used due judge structured output failure.",
            ] + list(stage.acceptance_criteria),
            max_round_per_stage=flow.state.max_round_per_stage,
        )

    stage_gate.stage_name = stage.name
    stage_gate.objective = stage.objective
    flow._merge_stage_gate_with_stage_spec(stage, stage_gate)
    raw_stage_gate = stage_gate.model_copy(deep=True)
    drift_artifact = flow._build_stage_gate_drift_artifact(stage=stage, raw_stage_gate=raw_stage_gate)
    flow._persist_stage_gate_drift_artifact(drift_artifact)
    if drift_artifact.policy_blockers:
        raise ValueError(
            f"Stage '{stage.name}' gate drift violated policy: {drift_artifact.policy_blockers}"
        )
    max_round = max(1, min(flow.state.max_round_per_stage, stage_gate.max_round_per_stage))
    return stage_gate, max_round


async def run_remote_preflight(flow: object, stage: StageSpec) -> StageResult | None:
    preflight_results = await flow.check_runner.run_remote_preflight("worker", stage, flow.agents.worker_workspace)
    preflight_artifacts = [
        flow._persist_remote_preflight_results(stage, "worker", preflight_results),
    ]
    preflight_failures = [
        ("worker", result)
        for result in preflight_results
        if not result.passed
    ]
    if not preflight_failures:
        return None

    evidence: list[str] = []
    required_actions: list[str] = []
    for worker, result in preflight_failures:
        summary = flow._summarize_check_failure(result.stdout, result.stderr)
        evidence.append(f"{worker}: {result.command}: {summary}")
        required_actions.append(
            f"{worker}: fix remote preflight failure for `{result.command}` — {summary}"
        )
    evidence = list(dict.fromkeys(evidence))
    required_actions = list(dict.fromkeys(required_actions))
    flow._persist_failure_event(
        FailureEventArtifact(
            stage_name=stage.name,
            round_index=0,
            source="remote_preflight",
            classification=FailureClassification(
                code="remote_preflight_failed",
                category="remote_gate",
                disposition="blocked",
                summary="Stage remote preflight failed before worker delivery started.",
                owner="shared",
                retryable=False,
                evidence=evidence,
            ),
            details={
                "worker": [item.model_dump() for item in preflight_results],
            },
        )
    )
    final_gate = JudgeGateReview(
        stage_name=stage.name,
        round_index=0,
        pass_gate=False,
        high_severity_open=[],
        disputed_items=[],
        required_actions=required_actions,
        rationale=(
            "Remote preflight failed before worker delivery. "
            "Fix remote access, sync, or workdir issues before spending coding rounds."
        ),
    )
    flow._persist_task_handoff_packet(
        flow._build_terminal_handoff_packet(
            worker="worker",
            stage=stage,
            round_index=0,
            final_gate=final_gate,
        )
    )
    flow._persist_runtime_status(
        RuntimeStatusSnapshot(
            target_repo=flow.state.target_repo,
            current_stage=stage.name,
            current_round=0,
            phase="remote_preflight_failed",
            overall_state="blocked",
            worker_states={"worker": "blocked"},
            judge_state="not_started",
            latest_artifacts=preflight_artifacts,
            notes=required_actions,
        )
    )
    flow._persist_stage_dashboard_artifact(
        flow._build_stage_dashboard_artifact(
            stage=stage,
            status="blocked",
            current_round=0,
            worker_states={"worker": "blocked"},
            judge_state="not_started",
            unresolved_actions=required_actions,
            latest_artifacts=preflight_artifacts,
        )
    )
    return StageResult(
        stage_name=stage.name,
        passed=False,
        rounds_used=0,
        gate=final_gate,
        round_logs=[],
    )


def finalize_failed_stage(
    flow: object,
    *,
    stage: StageSpec,
    max_round: int,
    final_gate: JudgeGateReview,
    round_logs: list,
) -> StageResult:
    flow._persist_task_handoff_packet(
        flow._build_terminal_handoff_packet(
            worker="worker",
            stage=stage,
            round_index=max_round,
            final_gate=final_gate,
        )
    )
    flow._persist_runtime_status(
        RuntimeStatusSnapshot(
            target_repo=flow.state.target_repo,
            current_stage=stage.name,
            current_round=max_round,
            phase="stage_failed",
            overall_state="failed",
            worker_states={"worker": "blocked"},
            judge_state="rejected",
            latest_artifacts=[
                flow._stage_artifact_ref(stage.name, f"round{max_round}_worker_stage_fail_handoff.json"),
            ],
            notes=list(final_gate.required_actions),
        )
    )
    failed_ledger = flow._build_stage_progress_ledger(
        stage=stage,
        round_index=max_round,
        status="failed",
        passed_gates=["stage_initialized", "remote_preflight", "stage_gate"],
        latest_artifacts=[
            flow._stage_artifact_ref(stage.name, f"round{max_round}_worker_stage_fail_handoff.json"),
        ],
        current_blocker=(final_gate.required_actions[0] if final_gate.required_actions else ""),
        current_blocker_category="judge_gate",
        notes=list(final_gate.required_actions),
    )
    flow._persist_stage_progress_ledger(stage, failed_ledger)
    flow._persist_repo_progress_note(
        stage=stage,
        ledger=failed_ledger,
        verified_facts=["Stage failed closed."],
        repeated_failure_points=list(final_gate.required_actions),
        stable_workarounds=[],
    )
    flow._persist_stage_dashboard_artifact(
        flow._build_stage_dashboard_artifact(
            stage=stage,
            status="failed",
            current_round=max_round,
            worker_states={"worker": "blocked"},
            judge_state="rejected",
            unresolved_actions=list(final_gate.required_actions),
            latest_artifacts=[flow._stage_artifact_ref(stage.name, f"round{max_round}_convergence_signal.json")],
        )
    )
    return StageResult(
        stage_name=stage.name,
        passed=False,
        rounds_used=max_round,
        gate=final_gate,
        round_logs=round_logs,
    )
