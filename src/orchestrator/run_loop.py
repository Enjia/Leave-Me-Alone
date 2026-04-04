from __future__ import annotations

import json
import time
from pathlib import Path

from core.models import (
    FailureClassification,
    FailureEventArtifact,
    JudgeGateReview,
    RunSummary,
    RuntimeStatusSnapshot,
    StageResult,
    StageSpec,
)
from engine.orchestration import build_stage_dag_plan
from .stage_runner import run_single_stage


def _empty_judge_gate(stage_name: str) -> JudgeGateReview:
    """Return a minimal JudgeGateReview for budget-blocked stages."""
    return JudgeGateReview(
        stage_name=stage_name,
        round_index=0,
        pass_gate=False,
        rationale="Stage blocked by budget policy.",
    )


def _build_runtime_sli(
    flow: object,
    *,
    run_started_monotonic: float,
    stage_results: list[StageResult],
) -> tuple[dict[str, float], list[str]]:
    now = time.monotonic()
    elapsed_sec = max(0.001, now - run_started_monotonic)
    elapsed_min = elapsed_sec / 60.0

    metrics = getattr(getattr(flow, "state", object()), "harness_metrics", {}) or {}
    retries = float(metrics.get("judge_retry_count", 0))
    stages = max(1.0, float(len(stage_results)))
    avg_rounds = float(sum(item.rounds_used for item in stage_results) / stages) if stage_results else 0.0
    retry_rate = retries / stages

    cost_snapshot = flow._get_cost_snapshot()
    estimated_cost = float(getattr(cost_snapshot, "estimated_cost_usd", 0.0) or 0.0)
    cost_burn_rate = estimated_cost / elapsed_min if elapsed_min > 0 else 0.0

    compression_rate = 0.0
    runtime_dir = getattr(getattr(flow, "cfg", object()), "runtime_dir", None)
    if runtime_dir:
        compression_path = Path(runtime_dir) / "artifacts" / "context_compression.jsonl"
        original = 0
        compressed = 0
        if compression_path.exists():
            try:
                for line in compression_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    original += int(payload.get("original_chars", 0) or 0)
                    compressed += int(payload.get("compressed_chars", 0) or 0)
            except OSError:
                pass
        if original > 0:
            compression_rate = max(0.0, min(1.0, (original - compressed) / float(original)))

    sli = {
        "run_elapsed_sec": round(elapsed_sec, 2),
        "avg_stage_rounds": round(avg_rounds, 3),
        "retry_rate_per_stage": round(retry_rate, 4),
        "cost_burn_rate_usd_per_min": round(cost_burn_rate, 6),
        "compression_rate": round(compression_rate, 4),
    }

    alerts: list[str] = []
    if retry_rate >= 1.0:
        alerts.append("retry_rate_high")
    if cost_burn_rate >= 1.0:
        alerts.append("cost_burn_rate_high")
    if compression_rate >= 0.8:
        alerts.append("compression_aggressive")

    return sli, alerts


async def run_review_inner(flow: object) -> RunSummary:
    run_started_monotonic = time.monotonic()
    flow._persist_harness_spec_snapshot()
    flow._persist_governance_policy_snapshot()
    flow._persist_runtime_status(
        RuntimeStatusSnapshot(
            target_repo=flow.state.target_repo,
            overall_state="running",
            phase="startup",
            notes=["Initializing review flow."],
        )
    )
    stages = [
        stage if isinstance(stage, StageSpec) else StageSpec.model_validate(stage)
        for stage in flow.state.stages
    ]
    if not stages:
        raise ValueError("No stages provided. Pass stages via --stages-file")
    for stage in stages:
        flow._hydrate_stage_spec_defaults(stage)
    catalog_errors = flow._validate_stage_catalog(stages)
    if catalog_errors:
        flow._persist_failure_event(
            FailureEventArtifact(
                stage_name="<catalog>",
                round_index=0,
                source="catalog_validation",
                classification=FailureClassification(
                    code="stage_catalog_invalid",
                    category="planner",
                    disposition="blocked",
                    summary="Stage catalog failed static validation.",
                    owner="system",
                    retryable=False,
                    evidence=catalog_errors,
                ),
                details={"errors": catalog_errors},
            )
        )
        raise ValueError(
            "Stage catalog validation failed: " + "; ".join(catalog_errors)
        )

    seeded_artifacts = {
        artifact.artifact_name
        for artifact_list in flow.state.stage_artifacts.values()
        for artifact in artifact_list
    }
    stage_dag_plan = build_stage_dag_plan(
        stages,
        initial_artifacts=seeded_artifacts,
    )
    flow.state.stage_dag_plan = stage_dag_plan
    flow._persist_stage_dag_plan(stage_dag_plan)
    if stage_dag_plan.validation_errors:
        raise ValueError(
            "Stage DAG planning failed: "
            + "; ".join(stage_dag_plan.validation_errors)
        )
    stage_lookup = {stage.name: stage for stage in stages}
    ordered_stages = [
        stage_lookup[stage_name]
        for stage_name in stage_dag_plan.serial_execution_order
        if stage_name in stage_lookup
    ] or stages

    resumed_stage_results = [
        result if isinstance(result, StageResult) else StageResult.model_validate(result)
        for result in getattr(flow.state, "resumed_stage_results", [])
    ]
    resume_passed_stage_names = {
        str(name)
        for name in getattr(flow.state, "resume_passed_stage_names", [])
        if str(name).strip()
    }
    stage_results: list[StageResult] = list(resumed_stage_results)
    overall_passed = True

    for stage in ordered_stages:
        apply_stage_policy = getattr(flow, "_apply_stage_policy_overrides", None)
        if callable(apply_stage_policy):
            apply_stage_policy(stage)
        flow._persist_stage_spec_snapshot(stage)
        if stage.name in resume_passed_stage_names:
            continue
        definition_errors = flow._validate_stage_definition(stage)
        if definition_errors:
            flow._persist_failure_event(
                FailureEventArtifact(
                    stage_name=stage.name,
                    round_index=0,
                    source="stage_definition",
                    classification=FailureClassification(
                        code="stage_definition_invalid",
                        category="input_contract",
                        disposition="blocked",
                        summary="Stage definition failed static harness validation.",
                        owner="system",
                        retryable=False,
                        evidence=definition_errors,
                    ),
                    details={"errors": definition_errors},
                )
            )
            raise ValueError(
                f"Stage '{stage.name}' has invalid harness configuration: {definition_errors}"
            )

        missing_inputs = flow._check_required_inputs(stage)
        if missing_inputs:
            flow._persist_failure_event(
                FailureEventArtifact(
                    stage_name=stage.name,
                    round_index=0,
                    source="required_inputs",
                    classification=FailureClassification(
                        code="required_inputs_missing",
                        category="input_contract",
                        disposition="blocked",
                        summary="Stage cannot start because required input artifacts are missing.",
                        owner="system",
                        retryable=False,
                        evidence=missing_inputs,
                    ),
                    details={"missing_inputs": missing_inputs},
                )
            )
            raise ValueError(
                f"Stage '{stage.name}' cannot start: missing required inputs "
                f"{missing_inputs}. Ensure previous stages produce these artifacts."
            )

        unapproved = flow._check_blocking_decisions(stage)
        if unapproved:
            flow._persist_decision_request(stage, unapproved)
            flow._persist_failure_event(
                FailureEventArtifact(
                    stage_name=stage.name,
                    round_index=0,
                    source="blocking_decision",
                    classification=FailureClassification(
                        code="blocking_decision_required",
                        category="human_decision",
                        disposition="blocked",
                        summary="Stage is waiting for explicit human decision approval.",
                        owner="shared",
                        retryable=False,
                        evidence=unapproved,
                    ),
                    details={"decisions": unapproved},
                )
            )
            flow._raise_blocking_decision_required(
                stage_name=stage.name,
                decisions=unapproved,
            )

        # Reset per-stage cost counter so each stage attempt starts fresh.
        flow._reset_stage_cost(stage.name)

        # Budget hard-limit check before starting a new stage.
        if flow._check_budget_hard_limit():
            flow._persist_cost_ledger()
            flow._persist_failure_event(
                FailureEventArtifact(
                    stage_name=stage.name,
                    round_index=0,
                    source="budget_policy",
                    classification=FailureClassification(
                        code="budget_hard_limit_exceeded",
                        category="input_contract",
                        disposition="blocked",
                        summary="Run-level hard budget limit exceeded. Blocking new stages.",
                        owner="system",
                        retryable=False,
                        evidence=[],
                    ),
                    details={},
                )
            )
            stage_results.append(
                StageResult(
                    stage_name=stage.name,
                    passed=False,
                    rounds_used=0,
                    gate=_empty_judge_gate(stage.name),
                    round_logs=[],
                )
            )
            overall_passed = False
            break

        result = await run_single_stage(flow, stage)

        # Downsample round logs and GC stage memory after completion.
        flow._gc_stage_memory(stage.name, result)

        stage_results.append(result)

        # Persist cost ledger after each stage completes.
        flow._persist_cost_ledger()

        if result.passed and stage.produces_artifacts:
            flow._persist_stage_artifacts(stage, result)

        if not result.passed:
            overall_passed = False
            break

        # Per-stage budget check *after* execution: if this stage exceeded its
        # budget, block subsequent stages.
        stage_budget_reason = flow._check_stage_budget(stage.name)
        if stage_budget_reason:
            flow._persist_failure_event(
                FailureEventArtifact(
                    stage_name=stage.name,
                    round_index=result.rounds_used,
                    source="budget_policy",
                    classification=FailureClassification(
                        code="stage_budget_exceeded",
                        category="input_contract",
                        disposition="blocked",
                        summary=stage_budget_reason,
                        owner="system",
                        retryable=False,
                        evidence=[],
                    ),
                    details={},
                )
            )
            overall_passed = False
            break

    summary = RunSummary(
        target_repo=flow.state.target_repo,
        overall_passed=overall_passed,
        stage_results=stage_results,
    )
    flow.state.summary = summary
    flow._persist_harness_metrics()
    flow._persist_cost_ledger()
    sli_metrics, sli_alerts = _build_runtime_sli(
        flow,
        run_started_monotonic=run_started_monotonic,
        stage_results=stage_results,
    )
    flow._persist_runtime_status(
        RuntimeStatusSnapshot(
            target_repo=flow.state.target_repo,
            current_stage=stage_results[-1].stage_name if stage_results else "",
            current_round=stage_results[-1].rounds_used if stage_results else 0,
            phase="completed",
            overall_state="passed" if overall_passed else "failed",
            worker_states={},
            judge_state="done",
            latest_artifacts=["artifacts/harness_metrics.json", "artifacts/cost_ledger.json"],
            notes=["Review flow completed."],
            sli_metrics=sli_metrics,
            sli_alerts=sli_alerts,
            cost_snapshot=flow._get_cost_snapshot(),
        )
    )
    return summary
