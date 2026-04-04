from __future__ import annotations

import re

from core.models import CheckSummaryArtifact, ConvergenceSignal, PlanDriftArtifact, RuntimeNudgeArtifact, StageSpec, WorkerDelivery, WorkerPlan


def build_plan_drift_artifact(
    *,
    stage_name: str,
    round_index: int,
    worker: str,
    plan: WorkerPlan,
    delivery: WorkerDelivery,
) -> PlanDriftArtifact:
    plan_files = set(plan.relevant_files)
    for step in plan.planned_steps:
        plan_files.update(step.files)
    changed_files = [item.strip() for item in delivery.changed_files if item.strip()]
    out_of_plan_files = [path for path in changed_files if plan_files and path not in plan_files]
    missing_verification = [
        item
        for item in plan.verification_steps
        if item not in delivery.tests_executed
        and item not in delivery.lint_executed
        and item not in delivery.perf_executed
    ]
    severity = "none"
    notes: list[str] = []
    drift_detected = bool(out_of_plan_files or missing_verification)
    if out_of_plan_files:
        severity = "blocking"
        notes.append("delivery changed files outside approved plan file set")
    elif missing_verification:
        severity = "warning"
        notes.append("delivery did not report any approved verification step")
    return PlanDriftArtifact(
        stage_name=stage_name,
        round_index=round_index,
        worker=worker,
        drift_detected=drift_detected,
        severity=severity,
        out_of_plan_files=out_of_plan_files,
        missing_verification=missing_verification,
        notes=notes,
    )


def check_summary_has_failures(summary: str) -> bool:
    if not summary:
        return False
    return any(
        marker in summary
        for marker in (
            "Tests passed: False",
            "Lint passed: False",
            "Perf passed: False",
            "Harness passed: False",
            "[TEST] FAIL:",
            "[LINT] FAIL:",
            "[PERF] FAIL:",
            "[HARNESS] FAIL:",
        )
    )


def classify_required_action_scope(action: str) -> tuple[str, str]:
    stripped = action.strip()
    lowered = stripped.lower()
    explicit_prefix = re.match(r"^(worker_[ab])\s*:\s*(.+)$", stripped, flags=re.IGNORECASE)
    if explicit_prefix:
        return explicit_prefix.group(1).lower(), explicit_prefix.group(2).strip()
    if any(
        marker in lowered
        for marker in (
            "both workers",
            "for both workers",
            "all workers",
            "each worker",
            "worker_a and worker_b",
            "worker_b and worker_a",
        )
    ):
        return "shared", stripped
    mentioned_workers = set(re.findall(r"\bworker_[ab]\b", lowered))
    if mentioned_workers == {"worker_a"}:
        return "worker_a", stripped
    if mentioned_workers == {"worker_b"}:
        return "worker_b", stripped
    if mentioned_workers == {"worker_a", "worker_b"}:
        return "shared", stripped
    return "unscoped", stripped


def latest_plan_drift_actions(flow: object, *, stage_name: str, worker_name: str) -> list[str]:
    latest: PlanDriftArtifact | None = None
    for artifact in flow.state.plan_drift_artifacts.get(stage_name, []):
        if artifact.worker == worker_name:
            latest = artifact
    if latest is None or not latest.drift_detected:
        return []
    actions: list[str] = []
    if latest.out_of_plan_files:
        actions.append("Plan drift: either remove or justify out-of-plan files " + ", ".join(latest.out_of_plan_files[:6]))
    if latest.missing_verification:
        actions.append("Plan drift: execute or explicitly report verification steps " + ", ".join(latest.missing_verification[:4]))
    return actions


def build_worker_required_actions(
    flow: object,
    *,
    stage_name: str,
    worker_name: str,
    judge_feedback: list[str],
    auto_check_summary: str,
) -> list[str]:
    actions: list[str] = []
    for item in judge_feedback:
        stripped = item.strip()
        if not stripped:
            continue
        scope, normalized = classify_required_action_scope(stripped)
        if scope == worker_name or scope == "shared":
            actions.append(normalized)
        elif scope == "unscoped":
            actions.append(stripped)
    if check_summary_has_failures(auto_check_summary):
        actions.append("Fix the failing automated checks shown below until all required checks pass.")
    actions.extend(
        latest_plan_drift_actions(flow, stage_name=stage_name, worker_name=worker_name)
    )
    deduped: list[str] = []
    seen: set[str] = set()
    for action in actions:
        if action not in seen:
            seen.add(action)
            deduped.append(action)
    return deduped


def build_runtime_nudges(
    *,
    stage: StageSpec,
    round_index: int,
    convergence_signal: ConvergenceSignal | None = None,
    drift_a: PlanDriftArtifact | None = None,
    drift_b: PlanDriftArtifact | None = None,
    check_artifact_a: CheckSummaryArtifact | None = None,
    check_artifact_b: CheckSummaryArtifact | None = None,
) -> list[RuntimeNudgeArtifact]:
    nudges: list[RuntimeNudgeArtifact] = []
    if convergence_signal is not None and convergence_signal.no_progress_detected:
        nudges.append(RuntimeNudgeArtifact(
            stage_name=stage.name,
            round_index=round_index,
            target="shared",
            category="no_progress",
            severity="warning",
            message="No new review_patch detected this round; converge on blocker summary or change strategy.",
            recommended_action="Summarize the blocker, narrow scope, and change repair strategy before another retry.",
            evidence=list(convergence_signal.reasons),
            dedupe_key=f"shared:no_progress:{round_index}",
        ))
    if convergence_signal is not None and convergence_signal.repeated_failure_signature:
        nudges.append(RuntimeNudgeArtifact(
            stage_name=stage.name,
            round_index=round_index,
            target="shared",
            category="error_recovery",
            severity="blocking" if convergence_signal.recommended_action == "stop_and_replan" else "warning",
            message="The failure signature repeated across rounds; stop re-running the same fix path.",
            recommended_action="Re-plan from the current failing evidence instead of repeating the same repair loop.",
            evidence=list(convergence_signal.reasons),
            dedupe_key=f"shared:repeated_failure:{round_index}",
        ))
    for drift in (drift_a, drift_b):
        if drift is None or not drift.drift_detected:
            continue
        nudges.append(RuntimeNudgeArtifact(
            stage_name=stage.name,
            round_index=round_index,
            target=drift.worker,
            category="todo_enforcement",
            severity="blocking" if drift.severity == "blocking" else "warning",
            message="Return to approved plan scope and verification obligations before broadening implementation.",
            recommended_action="Only touch in-plan files and execute the promised verification steps first.",
            evidence=list(drift.out_of_plan_files) + list(drift.missing_verification),
            dedupe_key=f"{drift.worker}:plan_drift:{round_index}",
        ))
    if stage.expected_artifact_paths:
        nudges.append(RuntimeNudgeArtifact(
            stage_name=stage.name,
            round_index=round_index,
            target="shared",
            category="artifact_missing",
            severity="info",
            message="Before pass, ensure all declared evidence artifacts are complete and contract-compliant.",
            recommended_action="Fill every declared artifact and satisfy its contract before promotion.",
            evidence=list(stage.expected_artifact_paths),
            dedupe_key=f"shared:artifact_missing:{stage.name}",
        ))
    for check_artifact in (check_artifact_a, check_artifact_b):
        if check_artifact is None or check_artifact.all_passed:
            continue
        target = check_artifact.worker
        if check_artifact.likely_subsystem == "remote_gate":
            nudges.append(RuntimeNudgeArtifact(
                stage_name=stage.name,
                round_index=round_index,
                target=target,
                category="error_recovery",
                severity="warning",
                message="Remote gate is failing; diagnose the concrete subsystem instead of expanding implementation scope.",
                recommended_action="Focus on the failing remote command, sync state, and environment assumptions before more edits.",
                evidence=list(check_artifact.representative_log_lines[:3]),
                dedupe_key=f"{target}:remote_gate:{check_artifact.signal_hash}",
            ))
        if check_artifact.likely_subsystem == "artifact_contract":
            nudges.append(RuntimeNudgeArtifact(
                stage_name=stage.name,
                round_index=round_index,
                target=target,
                category="artifact_missing",
                severity="warning",
                message="Artifact contract is failing; fix evidence completeness before adding more implementation churn.",
                recommended_action="Regenerate the declared artifact and satisfy its required keys/values/substrings.",
                evidence=list(check_artifact.representative_log_lines[:3]),
                dedupe_key=f"{target}:artifact_contract:{check_artifact.signal_hash}",
            ))
        if check_artifact.normalized_error_class in {"judge_stage_gate_fallback", "verifier_fallback", "judge_final_gate_fallback"}:
            nudges.append(RuntimeNudgeArtifact(
                stage_name=stage.name,
                round_index=round_index,
                target=target,
                category="json_retry",
                severity="warning",
                message="Structured output fallback fired; return strict schema-compliant JSON only on the next attempt.",
                recommended_action="Strip narration and emit only the required JSON object for this role.",
                evidence=[check_artifact.normalized_error_class],
                dedupe_key=f"{target}:json_retry:{check_artifact.signal_hash}",
            ))
    return nudges


def latest_runtime_nudges_text(flow: object, *, stage_name: str, target: str, limit: int = 6) -> str:
    items: list[str] = []
    for artifact in reversed(flow.state.runtime_nudges.get(stage_name, [])):
        if artifact.target not in {target, "shared"}:
            continue
        rendered = f"[{artifact.category}/{artifact.severity}] {artifact.message}"
        if rendered not in items:
            items.append(rendered)
        if len(items) >= limit:
            break
    if not items:
        return ""
    return "\n".join(f"- {item}" for item in reversed(items))
