"""Memory garbage collection for completed stages.

After a stage finishes, most of its in-memory state has already been persisted
to disk as artifacts.  This module provides helpers to:

1. **Downsample round logs** — replace full ``StageRoundLog`` objects with
   lightweight ``CompactStageRoundLog`` summaries.
2. **Purge stage-scoped state** — clear large dict entries that are no longer
   needed for subsequent stages.
"""
from __future__ import annotations

import logging

from core.models import (
    CompactStageRoundLog,
    StageResult,
    StageRoundLog,
)

logger = logging.getLogger(__name__)

# State fields that are safe to clear after a stage is persisted.
# These are keyed by stage_name in ReviewFlowState.
_PURGEABLE_STAGE_FIELDS: list[str] = [
    "stage_context_packets",
    "worker_plans",
    "plan_gate_reviews",
    "plan_drift_artifacts",
    "verifier_reports",
    "spec_gap_reports",
    "runtime_nudges",
    "check_summary_artifacts",
    "task_handoffs",
    "convergence_signals",
    "stage_progress_ledgers",
    "worker_entry_packets",
    "baseline_status_artifacts",
    "clean_state_artifacts",
    "feature_checklists",
]

# Fields that must NOT be purged (needed across stages).
# - stage_artifacts: used by run_loop for DAG artifact tracking
# - report_memory: used for cross-stage review continuity


def compact_round_log(log: StageRoundLog) -> CompactStageRoundLog:
    """Downsample a full round log to a compact summary.

    ``closed_report_ids`` includes both ``accept_fix`` and ``reject`` decisions
    (both represent a closed/terminal state).  ``open_report_ids`` contains only
    report IDs that were raised but *not* closed in this round's triage.
    """
    all_report_ids: set[str] = set()
    closed_ids: set[str] = set()

    for review in (log.peer_review_a_on_b, log.peer_review_b_on_a):
        for report in review.reports:
            report_id = getattr(report, "report_id", "")
            if report_id:
                all_report_ids.add(report_id)

    for triage in (log.triage_a, log.triage_b):
        for decision in getattr(triage, "decisions", []):
            action = getattr(decision, "action", "")
            if action in ("accept_fix", "reject"):
                report_id = getattr(decision, "report_id", "")
                if report_id:
                    closed_ids.add(report_id)

    open_ids = all_report_ids - closed_ids

    return CompactStageRoundLog(
        round_index=log.round_index,
        worker_a_summary=_truncate(log.worker_a_delivery.summary, 200),
        worker_b_summary=_truncate(log.worker_b_delivery.summary, 200),
        gate_decision="pass" if log.judge_gate.pass_gate else "fail",
        gate_reasoning=_truncate(log.judge_gate.rationale, 300),
        open_report_ids=sorted(open_ids),
        closed_report_ids=sorted(closed_ids),
    )


def downsample_stage_result(result: StageResult) -> StageResult:
    """Replace full round_logs with compact summaries, freeing memory."""
    if not result.round_logs:
        return result

    compact_logs = [compact_round_log(log) for log in result.round_logs]
    result.compact_round_logs = compact_logs
    result.round_logs = []
    return result


def gc_stage_memory(flow: object, stage_name: str) -> int:
    """Purge stage-scoped state that has been persisted to disk.

    Returns the number of fields cleared.
    """
    state = getattr(flow, "state", None)
    if state is None:
        return 0

    cleared = 0
    for field_name in _PURGEABLE_STAGE_FIELDS:
        field_dict = getattr(state, field_name, None)
        if isinstance(field_dict, dict) and stage_name in field_dict:
            del field_dict[stage_name]
            cleared += 1

    if cleared > 0:
        logger.info(
            "memory_gc: cleared %d state fields for stage '%s'",
            cleared, stage_name,
        )
    return cleared


def _truncate(text: str, limit: int) -> str:
    """Truncate text to *limit* characters."""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."
