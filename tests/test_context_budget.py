"""Tests for layered context compression: budget allocation, downsampling, and compression events."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.models import (
    CompressionEvent,
    ContextBudgetConfig,
    ContextSynthesis,
    ReportMemoryEntry,
)
from policy.context_budget import (
    allocate_zone_budgets,
    compress_check_summary_by_round,
    downsample_review_memory,
    downsample_synthesis_facts,
)


class TestAllocateZoneBudgets:
    def test_default_budget_splits(self) -> None:
        config = ContextBudgetConfig()
        budgets = allocate_zone_budgets(config)
        assert budgets["fixed"] == 20_000
        assert budgets["current_round"] == 36_000
        assert budgets["history"] == 24_000

    def test_custom_budget(self) -> None:
        config = ContextBudgetConfig(max_total_chars=100_000, fixed_zone_ratio=0.1)
        budgets = allocate_zone_budgets(config)
        assert budgets["fixed"] == 10_000

    def test_ratios_sum_to_total(self) -> None:
        config = ContextBudgetConfig(max_total_chars=50_000)
        budgets = allocate_zone_budgets(config)
        assert sum(budgets.values()) == 50_000


class TestDownsampleReviewMemory:
    @staticmethod
    def _entry(
        report_id: str,
        last_round: int,
        status: str = "open",
        certainty: str = "fact",
    ) -> ReportMemoryEntry:
        return ReportMemoryEntry(
            report_id=report_id,
            reviewer="worker_a",
            target_worker="worker_b",
            severity="S1",
            certainty=certainty,
            title=f"Issue {report_id}",
            file_path="src/foo.py",
            status=status,
            first_round=1,
            last_round=last_round,
        )

    def test_current_round_kept_fully(self) -> None:
        entries = [self._entry("r1", last_round=3)]
        kept, event = downsample_review_memory(entries, current_round=3, history_budget_chars=999_999)
        assert len(kept) == 1
        assert kept[0].report_id == "r1"
        assert event.dropped_items == 0

    def test_previous_round_open_kept(self) -> None:
        entries = [
            self._entry("r1", last_round=2, status="open"),
            self._entry("r2", last_round=2, status="resolved"),
        ]
        kept, event = downsample_review_memory(entries, current_round=3, history_budget_chars=999_999)
        assert len(kept) == 2
        open_entry = next(e for e in kept if e.report_id == "r1")
        resolved_entry = next(e for e in kept if e.report_id == "r2")
        assert "[compressed]" not in open_entry.title
        assert "[compressed]" in resolved_entry.title
        assert event.dropped_items == 1

    def test_old_round_only_open_facts_kept(self) -> None:
        entries = [
            self._entry("r1", last_round=1, status="open", certainty="fact"),
            self._entry("r2", last_round=1, status="open", certainty="inference"),
            self._entry("r3", last_round=1, status="resolved"),
        ]
        kept, event = downsample_review_memory(entries, current_round=4, history_budget_chars=999_999)
        assert len(kept) == 1
        assert kept[0].report_id == "r1"
        assert event.dropped_items == 2

    def test_empty_memory_returns_empty(self) -> None:
        kept, event = downsample_review_memory([], current_round=1, history_budget_chars=999_999)
        assert kept == []
        assert event.dropped_items == 0

    def test_compression_event_has_zone(self) -> None:
        entries = [self._entry("r1", last_round=1, status="resolved")]
        _, event = downsample_review_memory(entries, current_round=3, history_budget_chars=999_999)
        assert event.zone == "history_review_memory"
        assert event.round_index == 3


class TestDownsampleSynthesisFacts:
    def test_small_synthesis_unchanged(self) -> None:
        synthesis = ContextSynthesis(
            confirmed_facts=["f1", "f2"],
            active_inferences=["i1"],
            verification_backlog=["v1"],
        )
        compressed, event = downsample_synthesis_facts(
            synthesis, current_round=2, history_budget_chars=999_999,
        )
        assert compressed.confirmed_facts == ["f1", "f2"]
        assert event.dropped_items == 0

    def test_large_facts_list_trimmed(self) -> None:
        facts = [f"fact_{i}" for i in range(20)]
        synthesis = ContextSynthesis(confirmed_facts=facts)
        compressed, event = downsample_synthesis_facts(
            synthesis, current_round=2, history_budget_chars=999_999,
        )
        assert len(compressed.confirmed_facts) <= 8
        assert compressed.confirmed_facts[:4] == facts[:4]
        assert event.dropped_items > 0

    def test_inferences_capped_at_6(self) -> None:
        synthesis = ContextSynthesis(
            active_inferences=[f"inf_{i}" for i in range(10)],
        )
        compressed, _ = downsample_synthesis_facts(
            synthesis, current_round=2, history_budget_chars=999_999,
        )
        assert len(compressed.active_inferences) == 6

    def test_backlog_capped_at_4(self) -> None:
        synthesis = ContextSynthesis(
            verification_backlog=[f"bl_{i}" for i in range(10)],
        )
        compressed, _ = downsample_synthesis_facts(
            synthesis, current_round=2, history_budget_chars=999_999,
        )
        assert len(compressed.verification_backlog) == 4

    def test_open_required_actions_preserved(self) -> None:
        actions = [f"action_{i}" for i in range(15)]
        synthesis = ContextSynthesis(open_required_actions=actions)
        compressed, _ = downsample_synthesis_facts(
            synthesis, current_round=2, history_budget_chars=999_999,
        )
        assert compressed.open_required_actions == actions

    def test_compression_event_zone(self) -> None:
        synthesis = ContextSynthesis(confirmed_facts=[f"f_{i}" for i in range(20)])
        _, event = downsample_synthesis_facts(
            synthesis, current_round=3, history_budget_chars=999_999,
        )
        assert event.zone == "history_synthesis"


class TestCompressCheckSummaryByRound:
    def test_current_round_uses_higher_limit(self) -> None:
        summary = "x" * 2000
        result = compress_check_summary_by_round(summary, is_current_round=True)
        assert result == summary

    def test_history_round_uses_lower_limit(self) -> None:
        summary = "x" * 2000
        result = compress_check_summary_by_round(summary, is_current_round=False)
        assert len(result) < 2000
        assert result.endswith("...<CHECK SUMMARY COMPRESSED>...")

    def test_short_summary_unchanged(self) -> None:
        summary = "all tests passed"
        assert compress_check_summary_by_round(summary, is_current_round=False) == summary

    def test_empty_summary(self) -> None:
        assert compress_check_summary_by_round("", is_current_round=True) == ""
        assert compress_check_summary_by_round("  ", is_current_round=False) == ""


class TestContextBudgetConfig:
    def test_default_values(self) -> None:
        config = ContextBudgetConfig()
        assert config.max_total_chars == 80_000
        assert config.fixed_zone_ratio == 0.25
        assert config.current_round_ratio == 0.45
        assert config.history_zone_ratio == 0.30

    def test_custom_values(self) -> None:
        config = ContextBudgetConfig(max_total_chars=50_000, fixed_zone_ratio=0.3)
        assert config.max_total_chars == 50_000
        assert config.fixed_zone_ratio == 0.3


class TestCompressionEvent:
    def test_model_dump(self) -> None:
        event = CompressionEvent(
            stage_name="stage-1",
            round_index=2,
            zone="history_review_memory",
            original_chars=5000,
            compressed_chars=2000,
            dropped_items=3,
        )
        data = event.model_dump()
        assert data["stage_name"] == "stage-1"
        assert data["dropped_items"] == 3
        assert data["original_chars"] - data["compressed_chars"] == 3000

    def test_persist_and_load(self, tmp_path: Path) -> None:
        events = [
            CompressionEvent(
                stage_name="s1", round_index=1, zone="z1",
                original_chars=100, compressed_chars=50, dropped_items=2,
            ),
            CompressionEvent(
                stage_name="s1", round_index=2, zone="z2",
                original_chars=200, compressed_chars=80, dropped_items=5,
            ),
        ]
        artifact = tmp_path / "context_compression.jsonl"
        with artifact.open("w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event.model_dump(), ensure_ascii=False) + "\n")

        loaded = []
        for line in artifact.read_text(encoding="utf-8").splitlines():
            loaded.append(json.loads(line))
        assert len(loaded) == 2
        assert loaded[0]["dropped_items"] == 2
        assert loaded[1]["compressed_chars"] == 80
