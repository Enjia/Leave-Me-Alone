"""Layered context compression: budget allocation + history downsampling.

Three zones:
  1. Fixed zone (immutable requirements, stage spec, objective) — not compressible.
  2. Current-round zone (current patch, check results, judge feedback) — high priority.
  3. History zone (prior round facts, resolved reports) — compressed by recency.
"""
from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from core.models import CompressionEvent, ContextBudgetConfig, ContextSynthesis, ReportMemoryEntry

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

DEFAULT_BUDGET = ContextBudgetConfig()


def allocate_zone_budgets(config: ContextBudgetConfig) -> dict[str, int]:
    """Return per-zone character budgets derived from *config*."""
    total = config.max_total_chars
    return {
        "fixed": int(total * config.fixed_zone_ratio),
        "current_round": int(total * config.current_round_ratio),
        "history": int(total * config.history_zone_ratio),
    }


def downsample_review_memory(
    review_memory: list[ReportMemoryEntry],
    *,
    current_round: int,
    history_budget_chars: int,
) -> tuple[list[ReportMemoryEntry], CompressionEvent]:
    """Downsample *review_memory* based on recency relative to *current_round*.

    Strategy:
      - Current round entries: keep fully.
      - Previous round (current_round - 1): keep open entries only.
      - Older rounds: keep only open fact-grade entries.
      - Resolved/rejected/deferred entries older than current_round - 1: drop details,
        retain only report_id in the returned list (with a sentinel title).

    Returns the downsampled list and a CompressionEvent for observability.
    """
    original_count = len(review_memory)
    kept: list[ReportMemoryEntry] = []
    dropped_count = 0

    for entry in review_memory:
        age = current_round - entry.last_round

        if age <= 0:
            kept.append(entry)
        elif age == 1:
            if entry.status == "open":
                kept.append(entry)
            else:
                kept.append(_summarize_entry(entry))
                dropped_count += 1
        else:
            if entry.status == "open" and entry.certainty == "fact":
                kept.append(entry)
            else:
                dropped_count += 1

    total_chars = sum(len(str(e.model_dump())) for e in kept)
    if total_chars > history_budget_chars and kept:
        before_trim_count = len(kept)
        kept = _trim_to_budget(kept, history_budget_chars)
        dropped_count += before_trim_count - len(kept)

    compressed_chars = sum(len(str(e.model_dump())) for e in kept)
    original_chars = sum(len(str(e.model_dump())) for e in review_memory)

    event = CompressionEvent(
        stage_name="",
        round_index=current_round,
        zone="history_review_memory",
        original_chars=original_chars,
        compressed_chars=compressed_chars,
        dropped_items=dropped_count,
        timestamp_iso=datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
    )
    return kept, event


def downsample_synthesis_facts(
    synthesis: ContextSynthesis,
    *,
    current_round: int,
    history_budget_chars: int,
) -> tuple[ContextSynthesis, CompressionEvent]:
    """Compress *synthesis* lists based on a character budget.

    The budget drives how many items to keep: we start with generous defaults
    and progressively shrink until the result fits within *history_budget_chars*.
    ``open_required_actions``, ``dedupe_report_ids``, and ``resolved_report_ids``
    are always preserved (they are critical or cheap).
    """
    original_chars = _synthesis_chars(synthesis)

    facts_head = min(4, len(synthesis.confirmed_facts))
    facts_tail = min(4, max(0, len(synthesis.confirmed_facts) - facts_head))
    inferences_limit = min(6, len(synthesis.active_inferences))
    backlog_limit = min(4, len(synthesis.verification_backlog))

    compressed_facts = _keep_bookends(synthesis.confirmed_facts, head=facts_head, tail=facts_tail)
    compressed_inferences = synthesis.active_inferences[:inferences_limit]
    compressed_backlog = synthesis.verification_backlog[:backlog_limit]

    compressed = ContextSynthesis(
        confirmed_facts=compressed_facts,
        active_inferences=compressed_inferences,
        verification_backlog=compressed_backlog,
        open_required_actions=list(synthesis.open_required_actions),
        dedupe_report_ids=list(synthesis.dedupe_report_ids),
        resolved_report_ids=list(synthesis.resolved_report_ids),
    )

    current_chars = _synthesis_chars(compressed)
    while current_chars > history_budget_chars:
        reduced = False
        if len(compressed.confirmed_facts) > 2:
            compressed.confirmed_facts = _keep_bookends(
                compressed.confirmed_facts,
                head=max(1, len(compressed.confirmed_facts) // 2),
                tail=max(1, len(compressed.confirmed_facts) // 2),
            )
            reduced = True
        if len(compressed.active_inferences) > 1:
            compressed.active_inferences = compressed.active_inferences[: len(compressed.active_inferences) // 2]
            reduced = True
        if len(compressed.verification_backlog) > 1:
            compressed.verification_backlog = compressed.verification_backlog[: len(compressed.verification_backlog) // 2]
            reduced = True
        if not reduced:
            break
        current_chars = _synthesis_chars(compressed)

    compressed_chars = _synthesis_chars(compressed)

    event = CompressionEvent(
        stage_name="",
        round_index=current_round,
        zone="history_synthesis",
        original_chars=original_chars,
        compressed_chars=compressed_chars,
        dropped_items=(
            len(synthesis.confirmed_facts) - len(compressed.confirmed_facts)
            + len(synthesis.active_inferences) - len(compressed.active_inferences)
            + len(synthesis.verification_backlog) - len(compressed.verification_backlog)
        ),
        timestamp_iso=datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
    )
    return compressed, event


def compress_check_summary_by_round(
    summary: str,
    *,
    is_current_round: bool,
    max_current_chars: int = 2_500,
    max_history_chars: int = 800,
) -> str:
    """Compress a check summary based on whether it belongs to the current round."""
    if not summary.strip():
        return ""
    limit = max_current_chars if is_current_round else max_history_chars
    if len(summary) <= limit:
        return summary
    return summary[:limit] + "\n...<CHECK SUMMARY COMPRESSED>..."


def _summarize_entry(entry: ReportMemoryEntry) -> ReportMemoryEntry:
    """Create a lightweight copy of a resolved entry (keeps ID + status, drops detail)."""
    return ReportMemoryEntry(
        report_id=entry.report_id,
        reviewer=entry.reviewer,
        target_worker=entry.target_worker,
        severity=entry.severity,
        certainty=entry.certainty,
        title=f"[compressed] {entry.title[:60]}",
        file_path=entry.file_path,
        line=entry.line,
        status=entry.status,
        first_round=entry.first_round,
        last_round=entry.last_round,
    )


def _trim_to_budget(
    entries: list[ReportMemoryEntry],
    budget_chars: int,
) -> list[ReportMemoryEntry]:
    """Keep entries from the end (most recent) until budget is exhausted."""
    result: list[ReportMemoryEntry] = []
    total = 0
    for entry in reversed(entries):
        entry_chars = len(str(entry.model_dump()))
        if total + entry_chars > budget_chars:
            break
        result.append(entry)
        total += entry_chars
    result.reverse()
    return result


def _keep_bookends(items: list[str], *, head: int, tail: int) -> list[str]:
    """Keep the first *head* and last *tail* items, deduplicating overlap."""
    if len(items) <= head + tail:
        return list(items)
    head_items = items[:head]
    tail_items = items[-tail:]
    seen = set(head_items)
    result = list(head_items)
    for item in tail_items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _synthesis_chars(synthesis: ContextSynthesis) -> int:
    """Estimate character count for a ContextSynthesis."""
    return sum(
        len(item)
        for lst in (
            synthesis.confirmed_facts,
            synthesis.active_inferences,
            synthesis.verification_backlog,
            synthesis.open_required_actions,
            synthesis.dedupe_report_ids,
            synthesis.resolved_report_ids,
        )
        for item in lst
    )
