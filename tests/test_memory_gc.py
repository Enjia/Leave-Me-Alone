"""Tests for state/memory_gc.py — round-log downsampling and stage memory GC."""
from __future__ import annotations

import types

import pytest

from core.models import (
    BugReport,
    CompactStageRoundLog,
    JudgeGateReview,
    OwnerDecision,
    OwnerTriageResult,
    PeerReviewResult,
    SelfReviewResult,
    StageResult,
    StageRoundLog,
    VerifierReport,
    WorkerDelivery,
)
from state.memory_gc import (
    compact_round_log,
    downsample_stage_result,
    gc_stage_memory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_delivery(worker: str = "worker_a", summary: str = "did stuff") -> WorkerDelivery:
    return WorkerDelivery(worker=worker, summary=summary)


def _make_self_review(worker: str = "worker_a") -> SelfReviewResult:
    return SelfReviewResult(worker=worker)


def _make_peer_review(
    reviewer: str = "worker_a",
    target: str = "worker_b",
    report_ids: list[str] | None = None,
) -> PeerReviewResult:
    reports = []
    for rid in (report_ids or []):
        reports.append(
            BugReport(
                report_id=rid,
                severity="S2",
                title=f"Bug {rid}",
                file_path="foo.py",
                evidence="evidence " * 50,
                reasoning="reasoning " * 50,
                reproduction_or_inference="repro",
                fix_suggestion="fix it",
            )
        )
    return PeerReviewResult(reviewer=reviewer, target_worker=target, reports=reports)


def _make_triage(
    owner: str = "owner",
    accepted_ids: list[str] | None = None,
    rejected_ids: list[str] | None = None,
) -> OwnerTriageResult:
    decisions = []
    for rid in (accepted_ids or []):
        decisions.append(
            OwnerDecision(report_id=rid, action="accept_fix", rationale="ok")
        )
    for rid in (rejected_ids or []):
        decisions.append(
            OwnerDecision(report_id=rid, action="reject", rationale="not a bug")
        )
    return OwnerTriageResult(owner=owner, decisions=decisions)


def _make_judge_gate(
    stage: str = "s1",
    round_index: int = 1,
    passed: bool = False,
) -> JudgeGateReview:
    return JudgeGateReview(
        stage_name=stage,
        round_index=round_index,
        pass_gate=passed,
        rationale="gate rationale " * 20,
    )


def _make_verifier(stage: str = "s1", round_index: int = 1) -> VerifierReport:
    return VerifierReport(stage_name=stage, round_index=round_index)


def _make_round_log(
    round_index: int = 1,
    report_ids_a_on_b: list[str] | None = None,
    report_ids_b_on_a: list[str] | None = None,
    accepted_ids_a: list[str] | None = None,
    accepted_ids_b: list[str] | None = None,
    rejected_ids_a: list[str] | None = None,
    rejected_ids_b: list[str] | None = None,
) -> StageRoundLog:
    return StageRoundLog(
        round_index=round_index,
        worker_a_delivery=_make_delivery("worker_a", "worker_a did round " + str(round_index)),
        worker_b_delivery=_make_delivery("worker_b", "worker_b did round " + str(round_index)),
        worker_a_self_review=_make_self_review("worker_a"),
        worker_b_self_review=_make_self_review("worker_b"),
        peer_review_a_on_b=_make_peer_review("worker_a", "worker_b", report_ids_a_on_b),
        peer_review_b_on_a=_make_peer_review("worker_b", "worker_a", report_ids_b_on_a),
        triage_a=_make_triage("owner", accepted_ids_a, rejected_ids_a),
        triage_b=_make_triage("owner", accepted_ids_b, rejected_ids_b),
        verifier_report=_make_verifier("s1", round_index),
        judge_gate=_make_judge_gate("s1", round_index),
    )


def _make_fake_state() -> types.SimpleNamespace:
    """Build a lightweight mock state with all purgeable fields populated."""
    state = types.SimpleNamespace()
    # Purgeable fields (15 total):
    state.stage_context_packets = {"s1": ["packet1"]}
    state.worker_plans = {"s1": ["plan1"]}
    state.plan_gate_reviews = {"s1": ["review1"]}
    state.plan_drift_artifacts = {"s1": ["drift1"]}
    state.verifier_reports = {"s1": ["vr1"]}
    state.spec_gap_reports = {"s1": ["sg1"]}
    state.runtime_nudges = {"s1": ["nudge1"]}
    state.check_summary_artifacts = {"s1": ["cs1"]}
    state.task_handoffs = {"s1": ["th1"]}
    state.convergence_signals = {"s1": ["conv1"]}
    state.stage_progress_ledgers = {"s1": ["ledger1"]}
    state.worker_entry_packets = {"s1": ["entry1"]}
    state.baseline_status_artifacts = {"s1": ["baseline1"]}
    state.clean_state_artifacts = {"s1": ["clean1"]}
    state.feature_checklists = {"s1": ["checklist1"]}
    # Non-purgeable fields:
    state.stage_artifacts = {"s1": ["artifact_keep"]}
    state.report_memory = {"s1": ["memory_keep"]}
    return state


def _make_fake_flow(state: types.SimpleNamespace | None = None) -> object:
    flow = types.SimpleNamespace()
    flow.state = state or _make_fake_state()
    return flow


# ---------------------------------------------------------------------------
# Tests: compact_round_log
# ---------------------------------------------------------------------------

class TestCompactRoundLog:
    def test_basic_compaction_with_accept_fix(self) -> None:
        log = _make_round_log(
            round_index=2,
            report_ids_a_on_b=["R-001", "R-002"],
            report_ids_b_on_a=["R-003"],
            accepted_ids_a=["R-003"],
        )
        compact = compact_round_log(log)

        assert isinstance(compact, CompactStageRoundLog)
        assert compact.round_index == 2
        assert "worker_a" in compact.worker_a_summary
        assert "worker_b" in compact.worker_b_summary
        # R-003 was accepted → closed; R-001, R-002 remain open
        assert set(compact.open_report_ids) == {"R-001", "R-002"}
        assert compact.closed_report_ids == ["R-003"]

    def test_reject_counts_as_closed(self) -> None:
        log = _make_round_log(
            round_index=1,
            report_ids_a_on_b=["R-010", "R-011"],
            rejected_ids_b=["R-010"],
        )
        compact = compact_round_log(log)
        # R-010 rejected → closed; R-011 still open
        assert compact.open_report_ids == ["R-011"]
        assert compact.closed_report_ids == ["R-010"]

    def test_mixed_accept_and_reject(self) -> None:
        log = _make_round_log(
            round_index=3,
            report_ids_a_on_b=["R-A", "R-B"],
            report_ids_b_on_a=["R-C"],
            accepted_ids_a=["R-C"],
            rejected_ids_b=["R-A"],
        )
        compact = compact_round_log(log)
        assert compact.open_report_ids == ["R-B"]
        assert set(compact.closed_report_ids) == {"R-A", "R-C"}

    def test_truncation(self) -> None:
        log = _make_round_log(round_index=1)
        compact = compact_round_log(log)
        assert len(compact.gate_reasoning) <= 303  # 300 + "..."

    def test_empty_reviews(self) -> None:
        log = _make_round_log(round_index=1)
        compact = compact_round_log(log)
        assert compact.open_report_ids == []
        assert compact.closed_report_ids == []

    def test_legacy_resolved_report_ids_compat(self) -> None:
        """Deserializing old JSON with ``resolved_report_ids`` must not fail."""
        legacy_json = {
            "round_index": 1,
            "worker_a_summary": "did stuff",
            "worker_b_summary": "did stuff",
            "gate_decision": "fail",
            "gate_reasoning": "reason",
            "open_report_ids": ["R-001"],
            "resolved_report_ids": ["R-002"],
        }
        compact = CompactStageRoundLog.model_validate(legacy_json)
        assert compact.closed_report_ids == ["R-002"]
        assert compact.open_report_ids == ["R-001"]

    def test_new_closed_report_ids_takes_precedence(self) -> None:
        """If both old and new field names exist, new field wins."""
        data = {
            "round_index": 1,
            "closed_report_ids": ["R-NEW"],
            "resolved_report_ids": ["R-OLD"],
        }
        compact = CompactStageRoundLog.model_validate(data)
        assert compact.closed_report_ids == ["R-NEW"]


# ---------------------------------------------------------------------------
# Tests: downsample_stage_result
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tests: downsample_stage_result
# ---------------------------------------------------------------------------

class TestDownsampleStageResult:
    def test_replaces_round_logs_with_compact(self) -> None:
        logs = [_make_round_log(i) for i in range(1, 4)]
        result = StageResult(
            stage_name="s1",
            passed=True,
            rounds_used=3,
            gate=_make_judge_gate("s1", 3, True),
            round_logs=logs,
        )
        downsampled = downsample_stage_result(result)

        assert downsampled.round_logs == []
        assert len(downsampled.compact_round_logs) == 3
        for i, compact in enumerate(downsampled.compact_round_logs, start=1):
            assert compact.round_index == i

    def test_noop_when_no_round_logs(self) -> None:
        result = StageResult(
            stage_name="s1",
            passed=True,
            rounds_used=0,
            gate=_make_judge_gate("s1", 0, True),
        )
        downsampled = downsample_stage_result(result)
        assert downsampled.round_logs == []
        assert downsampled.compact_round_logs == []

    def test_memory_reduction(self) -> None:
        """Compact logs should be significantly smaller than full logs."""
        logs = [
            _make_round_log(
                i,
                report_ids_a_on_b=[f"R-{i}-1", f"R-{i}-2"],
                report_ids_b_on_a=[f"R-{i}-3"],
            )
            for i in range(1, 6)
        ]
        result = StageResult(
            stage_name="s1",
            passed=True,
            rounds_used=5,
            gate=_make_judge_gate("s1", 5, True),
            round_logs=logs,
        )
        full_size = len(result.model_dump_json())
        downsample_stage_result(result)
        compact_size = len(result.model_dump_json())
        assert compact_size < full_size * 0.5


# ---------------------------------------------------------------------------
# Tests: gc_stage_memory
# ---------------------------------------------------------------------------

class TestGcStageMemory:
    def test_purges_purgeable_fields(self) -> None:
        flow = _make_fake_flow()
        cleared = gc_stage_memory(flow, "s1")

        assert cleared == 15  # 15 purgeable fields
        assert "s1" not in flow.state.stage_context_packets
        assert "s1" not in flow.state.worker_plans
        assert "s1" not in flow.state.plan_gate_reviews

    def test_preserves_stage_artifacts(self) -> None:
        flow = _make_fake_flow()
        gc_stage_memory(flow, "s1")

        assert "s1" in flow.state.stage_artifacts
        assert flow.state.stage_artifacts["s1"] == ["artifact_keep"]

    def test_preserves_report_memory(self) -> None:
        flow = _make_fake_flow()
        gc_stage_memory(flow, "s1")

        assert "s1" in flow.state.report_memory
        assert flow.state.report_memory["s1"] == ["memory_keep"]

    def test_noop_for_unknown_stage(self) -> None:
        flow = _make_fake_flow()
        cleared = gc_stage_memory(flow, "unknown_stage")
        assert cleared == 0

    def test_noop_when_no_state(self) -> None:
        flow = types.SimpleNamespace()
        cleared = gc_stage_memory(flow, "s1")
        assert cleared == 0

    def test_idempotent(self) -> None:
        flow = _make_fake_flow()
        first = gc_stage_memory(flow, "s1")
        second = gc_stage_memory(flow, "s1")
        assert first == 15
        assert second == 0
