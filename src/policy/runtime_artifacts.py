from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from errors.taxonomy import classify_check_failure as classify_check_failure_core
from core.models import (
    BaselineStatusArtifact,
    CheckFailure,
    CheckSummaryArtifact,
    CleanStateArtifact,
    ConvergenceSignal,
    FailureClassification,
    PromotionReadinessArtifact,
    ReportMemoryEntry,
    StageContextPacket,
    StageDashboardArtifact,
    StageGate,
    StageGateDriftArtifact,
    StageProgressLedger,
    StageSpec,
    TaskHandoffPacket,
    TriageAuditArtifact,
    WorkerDelivery,
    WorkerEntryPacket,
    WorkerPlan,
)
from ports.workspace import WorkspaceArtifactsLike


def build_stage_progress_ledger(
    flow: object,
    *,
    stage: StageSpec,
    round_index: int,
    status: str,
    passed_gates: list[str],
    latest_artifacts: list[str],
    current_blocker: str = "",
    current_blocker_category: str = "",
    notes: list[str] | None = None,
) -> StageProgressLedger:
    subgoal_id, subgoal_title, _ = flow._select_active_subgoal(stage=stage, round_index=round_index)
    return StageProgressLedger(
        stage_name=stage.name,
        stage_id=stage.stage_id,
        round_index=round_index,
        status=status,
        active_subgoal_id=subgoal_id,
        active_subgoal_title=subgoal_title,
        passed_gates=list(dict.fromkeys(passed_gates)),
        current_blocker=current_blocker,
        current_blocker_category=current_blocker_category,
        frozen_non_goals=list(stage.non_goals),
        latest_artifacts=list(dict.fromkeys(latest_artifacts)),
        notes=list(notes or []),
    )


def build_worker_entry_packet(
    flow: object,
    *,
    stage: StageSpec,
    round_index: int,
    worker: str,
    context_packet: StageContextPacket,
    passed_gates: list[str],
    current_blocker: str,
    current_blocker_category: str,
    artifact_refs: list[str],
) -> WorkerEntryPacket:
    subgoal_id, subgoal_title, subgoal_description = flow._select_active_subgoal(
        stage=stage,
        round_index=round_index,
    )
    allowed_reads = [item for item in [stage.source_file] if item]
    return WorkerEntryPacket(
        stage_name=stage.name,
        round_index=round_index,
        worker=worker,
        objective=stage.objective,
        active_subgoal_id=subgoal_id,
        active_subgoal_title=subgoal_title,
        active_subgoal_description=subgoal_description,
        passed_gates=passed_gates,
        current_blocker=current_blocker,
        current_blocker_category=current_blocker_category,
        allowed_write_scope=list(stage.write_scope or stage.scope_hint),
        allowed_read_refs=allowed_reads,
        immutable_requirements=list(context_packet.immutable_requirements),
        frozen_non_goals=list(stage.non_goals),
        artifact_refs=list(dict.fromkeys(artifact_refs)),
        notes=list(context_packet.notes),
    )


def run_stage_baseline_sanity(flow: object, *, stage: StageSpec, round_index: int) -> list[BaselineStatusArtifact]:
    shared_failures: list[str] = []
    checks: list[str] = []
    if stage.source_file:
        checks.append(f"source_file:{stage.source_file}")
        if not Path(stage.source_file).exists():
            shared_failures.append(f"source_file missing: {stage.source_file}")
    if flow.cfg.seed_artifacts_dir:
        checks.append(f"seed_artifacts_dir:{flow.cfg.seed_artifacts_dir}")
        if not flow.cfg.seed_artifacts_dir.exists():
            shared_failures.append(f"seed_artifacts_dir missing: {flow.cfg.seed_artifacts_dir}")
    if not flow.cfg.runtime_dir.exists():
        shared_failures.append(f"runtime_dir missing: {flow.cfg.runtime_dir}")
    artifacts = [
        BaselineStatusArtifact(
            stage_name=stage.name,
            round_index=round_index,
            worker="shared",
            passed=not shared_failures,
            checks=checks,
            failures=shared_failures,
        )
    ]
    for worker, workspace in (
        ("worker_a", flow.agents.worker_a_workspace),
        ("worker_b", flow.agents.worker_b_workspace),
    ):
        failures: list[str] = []
        worker_checks = [f"workspace:{workspace}"]
        if not Path(str(workspace)).exists():
            failures.append(f"workspace missing: {workspace}")
        artifacts.append(
            BaselineStatusArtifact(
                stage_name=stage.name,
                round_index=round_index,
                worker=worker,
                passed=not failures,
                checks=worker_checks,
                failures=failures,
            )
        )
    return artifacts


def build_clean_state_artifact(
    *,
    stage_name: str,
    round_index: int,
    worker: str,
    delivery: WorkerDelivery,
) -> CleanStateArtifact:
    undocumented_blockers = []
    if delivery.unresolved_items and not delivery.risks:
        undocumented_blockers = list(delivery.unresolved_items)
    unresolved_changes = list(delivery.changed_files) if undocumented_blockers else []
    return CleanStateArtifact(
        stage_name=stage_name,
        round_index=round_index,
        worker=worker,
        passed=not undocumented_blockers,
        unresolved_changes=unresolved_changes,
        undocumented_blockers=undocumented_blockers,
        notes=["Session-end clean-state gate."],
    )


def summarize_check_failure(stdout: str, stderr: str) -> str:
    merged = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    if not merged:
        return "Command failed without stdout/stderr."
    first_line = next((line.strip() for line in merged.splitlines() if line.strip()), "")
    return first_line[:240] if first_line else "Command failed."


def classify_check_failure(
    flow: object,
    *,
    worker: str,
    check_type: str,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> FailureClassification:
    del flow  # kept for backward-compatible call sites
    return classify_check_failure_core(
        worker=worker,
        check_type=check_type,
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    )


def build_check_summary_artifact(
    flow: object,
    *,
    worker: str,
    stage_name: str,
    round_index: int,
    phase: str,
    checks: Any,
    raw_summary: str,
) -> CheckSummaryArtifact:
    failed_checks: list[CheckFailure] = []
    passed_commands: list[str] = []
    representative_log_lines: list[str] = []

    for check_type, results in (
        ("test", checks.test_results),
        ("lint", checks.lint_results),
        ("perf", checks.perf_results),
        ("harness", checks.harness_results),
    ):
        for result in results:
            if result.passed:
                passed_commands.append(result.command)
                continue
            failed_checks.append(
                CheckFailure(
                    check_type=check_type,
                    command=result.command,
                    exit_code=result.exit_code,
                    summary=summarize_check_failure(result.stdout, result.stderr),
                    classification=classify_check_failure(
                        flow,
                        worker=worker,
                        check_type=check_type,
                        command=result.command,
                        exit_code=result.exit_code,
                        stdout=result.stdout,
                        stderr=result.stderr,
                    ),
                )
            )
            for block in (result.stderr, result.stdout):
                for line in block.splitlines():
                    line = line.strip()
                    if line and line not in representative_log_lines:
                        representative_log_lines.append(line[:240])
                    if len(representative_log_lines) >= 6:
                        break
                if len(representative_log_lines) >= 6:
                    break

    signal_hash = hashlib.sha256(
        json.dumps(
            {
                "worker": worker,
                "phase": phase,
                "failed_checks": [item.model_dump() for item in failed_checks],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    normalized_error_class = ""
    likely_subsystem = "checks"
    if failed_checks:
        normalized_error_class = failed_checks[0].classification.code
        first_command = failed_checks[0].command.lower()
        if "remote" in first_command or "dev_env_remote" in first_command:
            likely_subsystem = "remote_gate"
        elif "artifact" in first_command or "json" in first_command:
            likely_subsystem = "artifact_contract"
        elif "lint" in first_command or "ruff" in first_command:
            likely_subsystem = "lint"
        elif "perf" in first_command or "benchmark" in first_command:
            likely_subsystem = "perf"
        else:
            likely_subsystem = failed_checks[0].check_type

    return CheckSummaryArtifact(
        stage_name=stage_name,
        round_index=round_index,
        worker=worker,
        phase=phase,
        all_passed=not failed_checks,
        passed_commands=passed_commands,
        failed_checks=failed_checks,
        normalized_error_class=normalized_error_class,
        likely_subsystem=likely_subsystem,
        representative_log_lines=representative_log_lines,
        environment_metadata={
            "provider": flow.cfg.provider,
            "remote_host": flow.cfg.remote_host,
            "remote_workdir": flow.cfg.remote_workdir,
        },
        raw_summary=raw_summary,
        signal_hash=signal_hash,
    )


def build_stage_gate_drift_artifact(flow: object, *, stage: StageSpec, raw_stage_gate: StageGate) -> StageGateDriftArtifact:
    def _extras(actual: list[str], allowed: list[str]) -> list[str]:
        allowed_set = set(allowed)
        return [item for item in actual if item not in allowed_set]

    stage_contracts = [f"Remote gate commands are immutable: {cmd}" for cmd in stage.gate_commands_remote]
    stage_contracts.extend(
        f"Remote gate evidence contract is immutable: {contract.model_dump()}"
        for contract in stage.remote_gate_contracts
    )
    stage_contracts.extend(f"Expected evidence artifact: {path}" for path in stage.expected_artifact_paths)
    stage_contracts.extend(f"Harness constraint: {constraint}" for constraint in stage.harness_constraints)
    stage_contracts.extend(f"Invariant: {item}" for item in stage.invariants)
    stage_contracts.extend(f"Trusted input/source: {item}" for item in stage.trust_sources)
    stage_contracts.extend(f"Non-goal boundary: {item}" for item in stage.non_goals)

    suspicious_items: list[str] = []
    for command in raw_stage_gate.test_commands + raw_stage_gate.lint_commands + raw_stage_gate.perf_checks:
        if "/enjia/" in command:
            suspicious_items.append(f"gate command references remote path: {command}")
    artifact = StageGateDriftArtifact(
        stage_name=stage.name,
        round_index=0,
        extra_test_commands=_extras(raw_stage_gate.test_commands, stage.test_commands),
        extra_lint_commands=_extras(raw_stage_gate.lint_commands, stage.lint_commands),
        extra_perf_checks=_extras(raw_stage_gate.perf_checks, stage.perf_checks),
        extra_interface_contracts=_extras(raw_stage_gate.interface_contracts, stage_contracts),
        extra_pass_criteria=_extras(
            raw_stage_gate.pass_criteria,
            [f"Must satisfy StageSpec hard requirements for {stage.name}"] + list(stage.acceptance_criteria),
        ),
        suspicious_items=suspicious_items,
    )
    if flow.cfg.drift_fail_on_suspicious_items:
        artifact.policy_blockers.extend(artifact.suspicious_items)
    else:
        artifact.policy_warnings.extend(artifact.suspicious_items)

    extra_commands = artifact.extra_test_commands + artifact.extra_lint_commands + artifact.extra_perf_checks
    if extra_commands:
        messages = [f"judge added command outside StageSpec: {item}" for item in extra_commands]
        if flow.cfg.drift_fail_on_extra_commands:
            artifact.policy_blockers.extend(messages)
        else:
            artifact.policy_warnings.extend(messages)

    artifact.policy_warnings.extend(f"extra_interface_contract:{item}" for item in artifact.extra_interface_contracts)
    artifact.policy_warnings.extend(f"extra_pass_criteria:{item}" for item in artifact.extra_pass_criteria)
    artifact.policy_blockers = list(dict.fromkeys(artifact.policy_blockers))
    artifact.policy_warnings = list(dict.fromkeys(artifact.policy_warnings))
    return artifact


def build_triage_audit_artifact(
    flow: object,
    *,
    stage_name: str,
    round_index: int,
    review_a_on_b: Any,
    review_b_on_a: Any,
    triage_a: Any,
    triage_b: Any,
) -> TriageAuditArtifact:
    invalid_rejections: list[str] = []
    fact_high_severity_rejections: list[str] = []
    unresolved_owner_items: list[str] = []

    report_lookup = {
        report.report_id: report for report in review_a_on_b.reports + review_b_on_a.reports
    }
    for decision in triage_a.decisions + triage_b.decisions:
        report = report_lookup.get(decision.report_id)
        if report is None:
            continue
        rationale = decision.rationale.strip()
        if decision.action == "reject" and not rationale and flow.cfg.triage_require_reject_rationale:
            invalid_rejections.append(decision.report_id)
            unresolved_owner_items.append(f"{decision.report_id}: rejection rationale must not be empty")
        if (
            decision.action == "reject"
            and report.severity in {"S0", "S1"}
            and report.evidence_semantics.certainty == "fact"
            and flow.cfg.triage_block_fact_high_severity_reject
        ):
            fact_high_severity_rejections.append(decision.report_id)
            unresolved_owner_items.append(
                f"{decision.report_id}: fact-grade {report.severity} rejection requires judge scrutiny"
            )

    policy_blockers = list(dict.fromkeys(unresolved_owner_items))
    return TriageAuditArtifact(
        stage_name=stage_name,
        round_index=round_index,
        invalid_rejections=invalid_rejections,
        fact_high_severity_rejections=fact_high_severity_rejections,
        unresolved_owner_items=unresolved_owner_items,
        policy_blockers=policy_blockers,
        passed=not policy_blockers,
    )


def build_promotion_readiness_artifact(
    flow: object,
    *,
    stage_name: str,
    round_index: int,
    final_gate: Any,
    auto_checks_a: Any,
    auto_checks_b: Any,
    review_memory: list[ReportMemoryEntry],
) -> PromotionReadinessArtifact:
    all_checks_passed = (
        auto_checks_a.all_tests_passed
        and auto_checks_a.all_lint_passed
        and auto_checks_a.all_perf_passed
        and auto_checks_a.all_harness_passed
        and auto_checks_b.all_tests_passed
        and auto_checks_b.all_lint_passed
        and auto_checks_b.all_perf_passed
        and auto_checks_b.all_harness_passed
    )
    blocking_report_ids = sorted(
        {
            entry.report_id
            for entry in review_memory
            if entry.status == "open" and entry.certainty == "fact" and entry.severity in {"S0", "S1"}
        }
    )
    unresolved_blockers = list(final_gate.required_actions)
    policy_rules: list[str] = []
    if flow.cfg.promotion_require_all_checks:
        policy_rules.append("require_all_checks")
    if flow.cfg.promotion_require_no_open_fact_high_severity:
        policy_rules.append("require_no_open_fact_high_severity")
    if flow.cfg.promotion_require_no_disputes:
        policy_rules.append("require_no_disputes")
    if (
        flow.cfg.promotion_require_all_checks
        and not all_checks_passed
        and "Automated checks must pass for both workers before promotion." not in unresolved_blockers
    ):
        unresolved_blockers.append("Automated checks must pass for both workers before promotion.")
    if flow.cfg.promotion_require_no_open_fact_high_severity and blocking_report_ids:
        unresolved_blockers.append("Open fact-grade S0/S1 reports remain before promotion.")
    if flow.cfg.promotion_require_no_disputes and final_gate.disputed_items:
        unresolved_blockers.append("Disputed items must be resolved before promotion.")
    unresolved_blockers = list(dict.fromkeys(unresolved_blockers))
    ready = final_gate.pass_gate and not unresolved_blockers
    return PromotionReadinessArtifact(
        stage_name=stage_name,
        round_index=round_index,
        owner_worker=flow.cfg.owner_worker,
        all_checks_passed=all_checks_passed,
        final_gate_passed=final_gate.pass_gate,
        unresolved_blockers=unresolved_blockers,
        blocking_report_ids=blocking_report_ids,
        disputed_items=list(final_gate.disputed_items),
        policy_rules=policy_rules,
        ready=ready,
    )


def build_stage_dashboard_artifact(
    flow: object,
    *,
    stage: StageSpec,
    status: str,
    current_round: int,
    worker_states: dict[str, str],
    judge_state: str,
    unresolved_actions: list[str],
    latest_artifacts: list[str],
    sli_metrics: dict[str, float] | None = None,
    sli_alerts: list[str] | None = None,
) -> StageDashboardArtifact:
    latest_plans = flow.state.worker_plans.get(stage.name, [])
    latest_plan_by_worker: dict[str, WorkerPlan] = {}
    for artifact in latest_plans:
        latest_plan_by_worker[artifact.worker] = artifact
    latest_plan_overview = {
        worker: f"steps:{len(artifact.planned_steps)} verify:{len(artifact.verification_steps)}"
        for worker, artifact in latest_plan_by_worker.items()
    }

    latest_checks = flow.state.check_summary_artifacts.get(stage.name, [])
    latest_by_worker: dict[str, CheckSummaryArtifact] = {}
    for artifact in latest_checks:
        latest_by_worker[artifact.worker] = artifact
    latest_check_overview = {
        worker: f"{artifact.phase}:{'pass' if artifact.all_passed else f'fail:{len(artifact.failed_checks)}'}"
        for worker, artifact in latest_by_worker.items()
    }

    latest_convergence = ""
    convergence_items = flow.state.convergence_signals.get(stage.name, [])
    if convergence_items:
        latest_convergence = convergence_items[-1].recommended_action
    latest_nudges: list[str] = []
    for artifact in reversed(flow.state.runtime_nudges.get(stage.name, [])):
        rendered = f"{artifact.target}:{artifact.category}:{artifact.severity}:{artifact.message}"
        if artifact.recommended_action:
            rendered += f" -> {artifact.recommended_action}"
        if rendered not in latest_nudges:
            latest_nudges.append(rendered)
        if len(latest_nudges) >= 6:
            break

    open_fact_high = [
        entry.report_id
        for entry in flow.state.report_memory.get(stage.name, [])
        if entry.status == "open" and entry.certainty == "fact" and entry.severity in {"S0", "S1"}
    ]

    failure_codes: list[str] = []
    artifacts_dir = flow.cfg.runtime_dir / "artifacts"
    for event_file in sorted(artifacts_dir.glob(f"{flow._artifact_slug(stage.name)}_round*_failure_event.json"))[-5:]:
        try:
            payload = json.loads(event_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        classification = payload.get("classification", {})
        code = classification.get("code")
        if isinstance(code, str) and code:
            failure_codes.append(code)

    return StageDashboardArtifact(
        stage_name=stage.name,
        stage_id=stage.stage_id,
        objective=stage.objective,
        status=status,
        current_round=current_round,
        max_rounds=flow.state.max_round_per_stage,
        worker_states=worker_states,
        judge_state=judge_state,
        latest_plan_overview=latest_plan_overview,
        latest_check_overview=latest_check_overview,
        latest_nudges=list(reversed(latest_nudges)),
        latest_convergence_action=latest_convergence,
        open_fact_high_severity_reports=open_fact_high,
        unresolved_actions=unresolved_actions,
        latest_failure_codes=failure_codes,
        latest_artifacts=latest_artifacts,
        sli_metrics=dict(sli_metrics or {}),
        sli_alerts=list(sli_alerts or []),
    )


def build_acceptance_backlog(stage: StageSpec) -> list[str]:
    backlog: list[str] = []
    backlog.extend(f"acceptance_pending:{item}" for item in stage.acceptance_criteria)
    backlog.extend(f"artifact_pending:{item}" for item in stage.expected_artifact_paths)
    return backlog


def build_task_handoff_packet(
    flow: object,
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
    completed_facts = [delivery.summary] if delivery.summary.strip() else []
    if check_artifact.all_passed:
        completed_facts.append(f"{check_artifact.phase}:all_checks_passed")
    else:
        completed_facts.extend(f"{check_artifact.phase}:failed:{item.command}" for item in check_artifact.failed_checks)
    evidence_artifacts = [
        flow._stage_artifact_ref(stage.name, f"round{round_index}_{worker}_{check_artifact.phase}_checks.json"),
        flow._stage_artifact_ref(stage.name, f"round{round_index}_context_packet.json"),
    ]
    related_report_ids = sorted(
        {
            entry.report_id
            for entry in review_memory
            if entry.target_worker == worker or entry.reviewer == worker
        }
    )
    return TaskHandoffPacket(
        stage_name=stage.name,
        round_index=round_index,
        worker=worker,
        trigger=trigger,
        objective=stage.objective,
        completed_facts=completed_facts,
        current_status=list(patch.status_lines),
        changed_files=list(patch.changed_files),
        evidence_artifacts=evidence_artifacts,
        open_blockers=flow._build_worker_required_actions(
            stage_name=stage.name,
            worker_name=worker,
            judge_feedback=judge_feedback,
            auto_check_summary=check_artifact.raw_summary,
        ) + build_acceptance_backlog(stage),
        immutable_requirements=list(context_packet.immutable_requirements),
        related_report_ids=related_report_ids,
    )


def build_round_start_handoff_packet(
    flow: object,
    *,
    worker: str,
    stage: StageSpec,
    round_index: int,
    context_packet: StageContextPacket,
    review_memory: list[ReportMemoryEntry],
) -> TaskHandoffPacket:
    return TaskHandoffPacket(
        stage_name=stage.name,
        round_index=round_index,
        worker=worker,
        trigger="round_start",
        objective=stage.objective,
        completed_facts=[],
        current_status=[],
        changed_files=[],
        evidence_artifacts=[
            flow._stage_artifact_ref(stage.name, "stage_spec_snapshot.json"),
            flow._stage_artifact_ref(stage.name, f"round{round_index}_context_packet.json"),
        ],
        open_blockers=list(context_packet.synthesis.open_required_actions) + build_acceptance_backlog(stage),
        immutable_requirements=list(context_packet.immutable_requirements),
        related_report_ids=sorted({entry.report_id for entry in review_memory}),
    )


def build_terminal_handoff_packet(
    flow: object,
    *,
    worker: str,
    stage: StageSpec,
    round_index: int,
    final_gate: Any,
    trigger: str = "stage_fail",
) -> TaskHandoffPacket:
    return TaskHandoffPacket(
        stage_name=stage.name,
        round_index=round_index,
        worker=worker,
        trigger=trigger,
        objective=stage.objective,
        completed_facts=[],
        current_status=[],
        changed_files=[],
        evidence_artifacts=[],
        open_blockers=list(final_gate.required_actions) + build_acceptance_backlog(stage),
        immutable_requirements=list(stage.invariants) + list(stage.acceptance_criteria),
        related_report_ids=[],
    )


def combine_failure_signatures(*values: str) -> str:
    material = "|".join(value for value in values if value)
    if not material:
        return ""
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def is_non_substantive_changed_file(path: str) -> bool:
    normalized = path.strip()
    if not normalized:
        return True
    base = normalized.rsplit("/", 1)[-1]
    if base == "Makefile":
        return True
    if normalized.startswith("docs/"):
        return True
    if normalized.endswith(".md") or normalized.endswith(".txt"):
        return True
    return False


def has_only_non_substantive_delta(patch_a: WorkspaceArtifactsLike, patch_b: WorkspaceArtifactsLike) -> bool:
    changed_files = sorted(set(patch_a.changed_files + patch_b.changed_files))
    if not changed_files:
        return False
    return all(is_non_substantive_changed_file(path) for path in changed_files)


def build_convergence_signal(
    flow: object,
    *,
    stage: StageSpec,
    stage_name: str,
    round_index: int,
    patch_a: WorkspaceArtifactsLike,
    patch_b: WorkspaceArtifactsLike,
    check_artifact_a: CheckSummaryArtifact,
    check_artifact_b: CheckSummaryArtifact,
    prev_failure_signature: str,
    no_progress_rounds: int,
    repeated_failure_rounds: int,
) -> ConvergenceSignal:
    no_progress_detected = not patch_a.review_patch.strip() and not patch_b.review_patch.strip()
    current_failure_signature = combine_failure_signatures(
        check_artifact_a.signal_hash,
        check_artifact_b.signal_hash,
    )
    repeated_failure_signature = bool(
        current_failure_signature
        and prev_failure_signature
        and current_failure_signature == prev_failure_signature
        and (check_artifact_a.failed_checks or check_artifact_b.failed_checks)
    )
    reasons: list[str] = []
    if no_progress_detected:
        reasons.append("No delta review patch was produced by either worker in this round.")
    only_non_substantive_delta = has_only_non_substantive_delta(patch_a, patch_b)
    if only_non_substantive_delta and (check_artifact_a.failed_checks or check_artifact_b.failed_checks):
        no_progress_detected = True
        reasons.append(
            "Only docs/Makefile-style delta was produced while blocking checks still fail; count this as no real progress."
        )
    if repeated_failure_signature:
        reasons.append("Post-triage failed check signature is unchanged from the previous round.")

    recommended_action = "continue"
    if flow.cfg.enable_convergence_signals:
        next_no_progress_rounds = no_progress_rounds + (1 if no_progress_detected else 0)
        next_repeated_failure_rounds = repeated_failure_rounds + (1 if repeated_failure_signature else 0)
        if next_no_progress_rounds > 0:
            reasons.append(f"no_progress_rounds={next_no_progress_rounds}/{flow.cfg.max_no_progress_rounds}")
        if next_repeated_failure_rounds > 0:
            reasons.append(
                f"repeated_failure_rounds={next_repeated_failure_rounds}/{flow.cfg.max_repeated_failure_rounds}"
            )
        if (
            next_no_progress_rounds >= flow.cfg.max_no_progress_rounds
            or next_repeated_failure_rounds >= flow.cfg.max_repeated_failure_rounds
        ):
            recommended_action = "stop_and_replan"
        elif no_progress_detected or repeated_failure_signature:
            recommended_action = "retry"

    return ConvergenceSignal(
        stage_name=stage_name,
        round_index=round_index,
        no_progress_detected=no_progress_detected,
        repeated_failure_signature=repeated_failure_signature,
        recommended_action=recommended_action,
        reasons=reasons,
    )
