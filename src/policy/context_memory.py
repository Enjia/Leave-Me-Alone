from __future__ import annotations

import hashlib
import logging

from core.models import CompressionEvent, ContextBudgetConfig, ContextSynthesis, PeerReviewResult, ReportMemoryEntry, StageContextPacket, StageExecutionPlan, StageGate, StageSpec
from .context_budget import (
    allocate_zone_budgets,
    compress_check_summary_by_round,
    downsample_review_memory,
    downsample_synthesis_facts,
)

logger = logging.getLogger(__name__)

def build_context_synthesis(
    flow: object,
    *,
    judge_feedback: list[str],
    prev_check_summary_a: str,
    prev_check_summary_b: str,
    review_memory: list[ReportMemoryEntry],
    round_index: int = 1,
    context_budget: ContextBudgetConfig | None = None,
) -> tuple[ContextSynthesis, list[CompressionEvent]]:
    """Build a ContextSynthesis with optional layered compression.

    Returns ``(synthesis, compression_events)`` so callers can persist events.
    """
    compression_events: list[CompressionEvent] = []
    budget = context_budget or ContextBudgetConfig()
    zone_budgets = allocate_zone_budgets(budget)
    fixed_budget = zone_budgets["fixed"]
    current_round_budget = zone_budgets["current_round"]
    history_budget = zone_budgets["history"]

    # --- Downsample review_memory by recency (history zone) ---
    downsampled_memory, mem_event = downsample_review_memory(
        review_memory,
        current_round=round_index,
        history_budget_chars=history_budget,
    )
    if mem_event.dropped_items > 0:
        compression_events.append(mem_event)

    # --- Compress check summaries by round (current-round zone budget) ---
    is_current = round_index <= 1
    current_check_limit = max(800, current_round_budget // 4)
    history_check_limit = max(400, history_budget // 6)
    compressed_check_a = compress_check_summary_by_round(
        prev_check_summary_a,
        is_current_round=is_current,
        max_current_chars=current_check_limit,
        max_history_chars=history_check_limit,
    )
    compressed_check_b = compress_check_summary_by_round(
        prev_check_summary_b,
        is_current_round=is_current,
        max_current_chars=current_check_limit,
        max_history_chars=history_check_limit,
    )

    # --- Build confirmed_facts with fixed-zone truncation ---
    truncate_limit = max(300, fixed_budget // 4)
    confirmed_facts = [f"judge_required_action:{item}" for item in judge_feedback]
    if compressed_check_a:
        confirmed_facts.append("worker_a_previous_checks:" + flow._truncate_text(compressed_check_a, truncate_limit))
    if compressed_check_b:
        confirmed_facts.append("worker_b_previous_checks:" + flow._truncate_text(compressed_check_b, truncate_limit))

    active_inferences: list[str] = []
    verification_backlog: list[str] = []
    dedupe_report_ids: list[str] = []
    resolved_report_ids: list[str] = []
    for entry in downsampled_memory:
        dedupe_report_ids.append(entry.report_id)
        if entry.status != "open":
            resolved_report_ids.append(entry.report_id)
        if entry.status != "open":
            continue
        label = f"{entry.report_id}:{entry.severity}:{entry.target_worker}:{entry.file_path}:{entry.title}"
        if entry.certainty == "fact":
            confirmed_facts.append(f"open_fact:{label}")
        elif entry.certainty == "inference":
            active_inferences.append(label)
        else:
            verification_backlog.append(label)

    raw_synthesis = ContextSynthesis(
        confirmed_facts=confirmed_facts,
        active_inferences=active_inferences,
        verification_backlog=verification_backlog,
        open_required_actions=list(judge_feedback),
        dedupe_report_ids=sorted(set(dedupe_report_ids)),
        resolved_report_ids=sorted(set(resolved_report_ids)),
    )

    # --- Downsample synthesis lists if round > 1 ---
    if round_index > 1:
        compressed_synthesis, synth_event = downsample_synthesis_facts(
            raw_synthesis,
            current_round=round_index,
            history_budget_chars=history_budget,
        )
        if synth_event.dropped_items > 0:
            compression_events.append(synth_event)
        return compressed_synthesis, compression_events

    return raw_synthesis, compression_events

def build_stage_context_packet(
    flow: object,
    *,
    stage: StageSpec,
    stage_gate: StageGate,
    stage_plan: StageExecutionPlan,
    round_index: int,
    judge_feedback: list[str],
    prev_check_summary_a: str,
    prev_check_summary_b: str,
    review_memory: list[ReportMemoryEntry],
) -> StageContextPacket:
    synthesis, compression_events = build_context_synthesis(
        flow,
        judge_feedback=judge_feedback,
        prev_check_summary_a=prev_check_summary_a,
        prev_check_summary_b=prev_check_summary_b,
        review_memory=review_memory,
        round_index=round_index,
        context_budget=getattr(flow, "_context_budget_config", None),
    )
    if compression_events:
        logger.info(
            "context_compression: stage=%s round=%d events=%d",
            stage.name, round_index, len(compression_events),
        )
        _persist_compression_events(flow, stage.name, compression_events)
    immutable_requirements: list[str] = []
    immutable_requirements.extend(f"test_command:{cmd}" for cmd in stage.test_commands)
    immutable_requirements.extend(f"lint_command:{cmd}" for cmd in stage.lint_commands)
    immutable_requirements.extend(f"perf_check:{cmd}" for cmd in stage.perf_checks)
    immutable_requirements.extend(f"remote_gate:{cmd}" for cmd in stage.gate_commands_remote)
    immutable_requirements.extend(f"artifact:{path}" for path in stage.expected_artifact_paths)
    immutable_requirements.extend(f"harness_constraint:{item}" for item in stage.harness_constraints)
    immutable_requirements.extend(f"invariant:{item}" for item in stage.invariants)
    immutable_requirements.extend(f"acceptance_criteria:{item}" for item in stage.acceptance_criteria)
    immutable_requirements.extend(f"non_goal:{item}" for item in stage.non_goals)
    immutable_requirements.extend(f"trust_source:{item}" for item in stage.trust_sources)
    immutable_requirements.extend(f"trust_priority:{item}" for item in stage.trust_priority)
    plan_summary = [f"{node.node_id} -> depends_on={node.depends_on} wait_for={node.wait_for}" for node in stage_plan.nodes]
    packet = StageContextPacket(
        stage_name=stage.name,
        round_index=round_index,
        objective=stage_gate.objective,
        source_of_truth=stage.source_file or stage.stage_id or stage.objective,
        plan_summary=plan_summary,
        immutable_requirements=immutable_requirements,
        synthesis=synthesis,
        review_patch_strategy="delta_since_last_round",
        notes=[
            "Use synthesized context instead of replaying full prior prompts.",
            "Peer review should focus on net-new delta and unresolved fact-grade risk.",
        ],
    )
    flow.state.stage_context_packets.setdefault(stage.name, []).append(packet)
    return packet


def _persist_compression_events(
    flow: object,
    stage_name: str,
    events: list[CompressionEvent],
) -> None:
    """Append compression events to the flow's artifact directory."""
    import json
    from pathlib import Path

    for event in events:
        event.stage_name = stage_name

    runtime_dir = getattr(getattr(flow, "cfg", None), "runtime_dir", None)
    if runtime_dir is None:
        return
    artifact_path = Path(runtime_dir) / "artifacts" / "context_compression.jsonl"
    try:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        with artifact_path.open("a", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event.model_dump(), ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("failed to persist compression events to %s", artifact_path)


def report_ids_for_stage(review_memory: list[ReportMemoryEntry]) -> list[str]:
    return sorted({entry.report_id for entry in review_memory})


def resolved_report_ids_for_stage(review_memory: list[ReportMemoryEntry]) -> list[str]:
    return sorted({entry.report_id for entry in review_memory if entry.status in {"resolved", "rejected", "deferred"}})


def stable_report_id(
    *,
    stage_name: str,
    target_worker: str,
    file_path: str,
    line: int | None,
    title: str,
) -> str:
    raw = "|".join(
        [
            stage_name.strip().lower(),
            target_worker.strip().lower(),
            file_path.strip().lower(),
            str(line or 0),
            title.strip().lower(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def canonicalize_peer_review_result(
    flow: object,
    *,
    stage_name: str,
    round_index: int,
    reviewer: str,
    target_worker: str,
    review: PeerReviewResult,
    review_memory: list[ReportMemoryEntry],
) -> PeerReviewResult:
    del round_index
    existing = {entry.report_id: entry for entry in review_memory}
    seen_in_result: set[str] = set()
    normalized_reports = []
    for report in review.reports:
        semantics = report.evidence_semantics
        semantics.facts = [item.strip() for item in semantics.facts if item.strip()]
        semantics.inferences = [item.strip() for item in semantics.inferences if item.strip()]
        semantics.to_verify = [item.strip() for item in semantics.to_verify if item.strip()]
        canonical_id = stable_report_id(
            stage_name=stage_name,
            target_worker=target_worker,
            file_path=report.file_path,
            line=report.line,
            title=report.title,
        )
        report.report_id = canonical_id
        if canonical_id in seen_in_result:
            flow._bump_metric("peer_review_duplicate_in_result_count")
            continue
        prior = existing.get(canonical_id)
        if prior is not None and prior.severity == report.severity and prior.certainty == report.evidence_semantics.certainty:
            flow._bump_metric("peer_review_cross_round_duplicate_count")
            continue
        seen_in_result.add(canonical_id)
        normalized_reports.append(report)
    return PeerReviewResult(
        reviewer=reviewer,
        target_worker=target_worker,
        reports=normalized_reports,
        overall_notes=review.overall_notes,
    )


def merge_stage_report_memory(
    *,
    round_index: int,
    review_memory: list[ReportMemoryEntry],
    review_a_on_b: PeerReviewResult,
    review_b_on_a: PeerReviewResult,
    triage_a: object,
    triage_b: object,
) -> list[ReportMemoryEntry]:
    memory_by_id = {entry.report_id: entry for entry in review_memory}
    decisions_by_report_id = {
        decision.report_id: decision.action
        for decision in triage_a.decisions + triage_b.decisions
    }
    for review in (review_a_on_b, review_b_on_a):
        for report in review.reports:
            status = "open"
            action = decisions_by_report_id.get(report.report_id)
            if action == "accept_fix":
                status = "resolved"
            elif action == "reject":
                status = "deferred" if report.evidence_semantics.certainty == "to_verify" else "rejected"
            existing = memory_by_id.get(report.report_id)
            if existing is None:
                memory_by_id[report.report_id] = ReportMemoryEntry(
                    report_id=report.report_id,
                    reviewer=review.reviewer,
                    target_worker=review.target_worker,
                    severity=report.severity,
                    certainty=report.evidence_semantics.certainty,
                    title=report.title,
                    file_path=report.file_path,
                    line=report.line,
                    status=status,
                    first_round=round_index,
                    last_round=round_index,
                )
                continue
            existing.severity = report.severity
            existing.certainty = report.evidence_semantics.certainty
            existing.status = status
            existing.last_round = round_index
    return sorted(memory_by_id.values(), key=lambda item: item.report_id)
