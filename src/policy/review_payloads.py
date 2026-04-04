from __future__ import annotations

from pathlib import Path
from typing import Any

from core.models import (
    CheckSummaryArtifact,
    FailureClassification,
    FailureEventArtifact,
    OwnerTriageResult,
    PeerReviewResult,
    PlanDriftArtifact,
    StageGate,
    StageSpec,
    VerifierReport,
    WorkerPlan,
)
from ports.workspace import WorkspaceArtifactsLike


def normalize_worker_plan_payload(
    flow: object,
    payload: WorkerPlan | dict[str, Any],
    *,
    worker: str,
) -> WorkerPlan:
    if isinstance(payload, WorkerPlan):
        model = payload
    else:
        model = WorkerPlan.model_validate(payload)

    repo_root = Path(flow.state.target_repo).resolve()

    def _normalize_plan_path(value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return ""
        try:
            path = Path(cleaned)
        except (TypeError, ValueError):
            return cleaned
        if not path.is_absolute():
            return cleaned
        try:
            return path.resolve().relative_to(repo_root).as_posix()
        except (OSError, ValueError):
            try:
                return path.resolve().as_posix()
            except OSError:
                return cleaned

    def _clean_list(values: list[str]) -> list[str]:
        out: list[str] = []
        for item in values:
            cleaned = _normalize_plan_path(str(item))
            if cleaned:
                out.append(cleaned)
        return out

    planned_steps = []
    for step in model.planned_steps:
        title = step.title.strip() or "unnamed_step"
        action = step.action.strip() or title
        files = _clean_list(step.files)
        planned_steps.append(
            step.model_copy(update={"title": title, "action": action, "files": files})
        )

    goal = model.goal.strip() or model.summary.strip() or f"Implement {worker} stage work"
    summary = model.summary.strip() or f"{worker} plan with {len(planned_steps)} steps."
    return model.model_copy(
        update={
            "worker": worker,
            "goal": goal,
            "summary": summary,
            "relevant_files": _clean_list(model.relevant_files),
            "verification_steps": _clean_list(model.verification_steps),
            "risks": _clean_list(model.risks),
            "assumptions": _clean_list(model.assumptions),
            "planned_steps": planned_steps,
        }
    )


def validate_worker_plan(flow: object, *, stage: StageSpec, plan: WorkerPlan) -> None:
    errors: list[str] = []
    allowed_scope = stage.write_scope or stage.scope_hint or ["."]
    allowed_reads: set[str] = set()
    repo_root = Path(flow.state.target_repo).resolve()
    if stage.source_file and stage.source_file.strip():
        raw_source = stage.source_file.strip()
        allowed_reads.add(raw_source)
        source_path = Path(raw_source)
        if not source_path.is_absolute():
            source_path = (repo_root / source_path).resolve()
        else:
            source_path = source_path.resolve()
        allowed_reads.add(source_path.as_posix())
        try:
            allowed_reads.add(source_path.relative_to(repo_root).as_posix())
        except ValueError:
            pass
        allowed_reads.add(source_path.name)

    if not plan.planned_steps:
        errors.append("planned_steps must not be empty")
    if not plan.verification_steps:
        errors.append("verification_steps must not be empty")
    if plan.stage_name != stage.name:
        errors.append(
            f"stage_name mismatch: expected '{stage.name}', got '{plan.stage_name}'"
        )
    if plan.round_index <= 0:
        errors.append("round_index must be positive")

    for path in list(plan.relevant_files) + [
        item for step in plan.planned_steps for item in step.files
    ]:
        if path == ".":
            continue
        if path in allowed_reads:
            continue
        if not any(
            path == scope or path.startswith(f"{scope}/") or scope == "."
            for scope in allowed_scope
        ):
            errors.append(f"plan references file outside allowed scope: {path}")

    if errors:
        flow._persist_failure_event(
            FailureEventArtifact(
                stage_name=stage.name,
                round_index=plan.round_index,
                source=f"{plan.worker}_planner",
                classification=FailureClassification(
                    code="worker_plan_invalid",
                    category="planner",
                    disposition="blocked",
                    summary="Worker planner output violated Plan Mode contract.",
                    owner=plan.worker,
                    retryable=False,
                    evidence=errors,
                ),
                details=plan.model_dump(),
            )
        )
        raise ValueError(
            f"Invalid worker plan for {plan.worker} at stage '{stage.name}': {errors}"
        )


def normalize_worker_delivery_payload(payload: dict[str, Any], worker: str) -> dict[str, Any]:
    def _as_str_list(values: Any) -> list[str]:
        if values is None:
            return []
        if isinstance(values, str):
            return [values.strip()] if values.strip() else []
        if not isinstance(values, list):
            return [str(values)]
        out: list[str] = []
        for item in values:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    out.append(cleaned)
            elif isinstance(item, dict):
                candidate = (
                    item.get("file")
                    or item.get("path")
                    or item.get("command")
                    or item.get("description")
                )
                if candidate:
                    out.append(str(candidate))
            elif item is not None:
                out.append(str(item))
        return out

    summary = payload.get("summary", "")
    if isinstance(summary, list):
        summary = "; ".join(str(item).strip() for item in summary if str(item).strip())
    elif not isinstance(summary, str):
        summary = str(summary)

    changed_files = _as_str_list(payload.get("changed_files"))
    if not changed_files:
        changed_files = _as_str_list(payload.get("changes"))

    tests_executed = _as_str_list(payload.get("tests_executed"))
    if not tests_executed:
        tests_executed = _as_str_list(payload.get("tests"))

    lint_executed = _as_str_list(payload.get("lint_executed"))
    perf_executed = _as_str_list(payload.get("perf_executed"))
    risks = _as_str_list(payload.get("risks"))
    unresolved_items = _as_str_list(payload.get("unresolved_items"))
    if not unresolved_items:
        unresolved_items = _as_str_list(payload.get("unresolved"))

    return {
        "worker": str(payload.get("worker") or worker),
        "summary": summary,
        "changed_files": changed_files,
        "tests_executed": tests_executed,
        "lint_executed": lint_executed,
        "perf_executed": perf_executed,
        "risks": risks,
        "unresolved_items": unresolved_items,
    }


def normalize_stage_gate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    def _as_str_list(values: Any) -> list[str]:
        if values is None:
            return []
        if isinstance(values, str):
            cleaned = values.strip()
            return [cleaned] if cleaned else []
        if not isinstance(values, list):
            return [str(values)]
        out: list[str] = []
        for item in values:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    out.append(cleaned)
            elif item is not None:
                out.append(str(item))
        return out

    raw_max_round = payload.get("max_round_per_stage", 2)
    try:
        max_round = int(raw_max_round)
    except (TypeError, ValueError):
        max_round = 2

    stage_name = payload.get("stage_name")
    if not stage_name:
        stage_name = payload.get("stage", "")

    return {
        "stage_name": str(stage_name or ""),
        "objective": str(payload.get("objective") or ""),
        "test_commands": _as_str_list(payload.get("test_commands")),
        "lint_commands": _as_str_list(payload.get("lint_commands")),
        "perf_checks": _as_str_list(payload.get("perf_checks")),
        "interface_contracts": _as_str_list(payload.get("interface_contracts")),
        "pass_criteria": _as_str_list(payload.get("pass_criteria")),
        "max_round_per_stage": max(1, max_round),
    }


def build_verifier_payload(
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
    return {
        "stage_name": stage_name,
        "stage_contract": {
            "invariants": list(stage.invariants),
            "acceptance_criteria": list(stage.acceptance_criteria),
            "non_goals": list(stage.non_goals),
            "artifact_contracts": [item.model_dump() for item in stage.artifact_contracts],
            "remote_gate_contracts": [item.model_dump() for item in stage.remote_gate_contracts],
        },
        "checks": {
            "worker_a": check_artifact_a.model_dump(),
            "worker_b": check_artifact_b.model_dump(),
        },
        "peer_reviews": {
            "worker_a_on_b": review_a_on_b.model_dump(),
            "worker_b_on_a": review_b_on_a.model_dump(),
        },
        "triage": {
            "worker_a": triage_a.model_dump(),
            "worker_b": triage_b.model_dump(),
        },
        "plan_drift": {
            "worker_a": drift_a.model_dump(),
            "worker_b": drift_b.model_dump(),
        },
        "worker_a_status": {
            "changed_files": patch_a.changed_files,
            "status": patch_a.status_lines,
            "patch": patch_a.review_patch or patch_a.patch,
        },
        "worker_b_status": {
            "changed_files": patch_b.changed_files,
            "status": patch_b.status_lines,
            "patch": patch_b.review_patch or patch_b.patch,
        },
    }


def build_judge_gate_payload(
    stage_name: str,
    review_a_on_b: PeerReviewResult,
    review_b_on_a: PeerReviewResult,
    triage_a: OwnerTriageResult,
    triage_b: OwnerTriageResult,
    verifier_report: VerifierReport,
) -> dict[str, Any]:
    blocking_high_severity = []
    inference_reports = []
    to_verify_reports = []
    disputed = []

    reports_on_a_by_id = {report.report_id: report for report in review_b_on_a.reports}
    reports_on_b_by_id = {report.report_id: report for report in review_a_on_b.reports}

    for report in review_a_on_b.reports + review_b_on_a.reports:
        if report.severity in {"S0", "S1"}:
            certainty = report.evidence_semantics.certainty
            if certainty == "fact":
                blocking_high_severity.append(report.model_dump())
            elif certainty == "inference":
                inference_reports.append(report.model_dump())
            else:
                to_verify_reports.append(report.model_dump())

    for decision in triage_a.decisions:
        if decision.action == "reject" and decision.report_id in reports_on_a_by_id:
            disputed.append(
                {
                    "owner": "worker_a",
                    "decision": decision.model_dump(),
                    "report": reports_on_a_by_id[decision.report_id].model_dump(),
                }
            )

    for decision in triage_b.decisions:
        if decision.action == "reject" and decision.report_id in reports_on_b_by_id:
            disputed.append(
                {
                    "owner": "worker_b",
                    "decision": decision.model_dump(),
                    "report": reports_on_b_by_id[decision.report_id].model_dump(),
                }
            )

    return {
        "stage_name": stage_name,
        "verifier_report": verifier_report.model_dump(),
        "blocking_high_severity_reports": blocking_high_severity,
        "inference_reports": inference_reports,
        "to_verify_reports": to_verify_reports,
        "disputed_decisions": disputed,
    }
