"""Tests for events/event_bus.py and events/models.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from events.event_bus import EventBus, make_event_bus
from events.models import (
    AgentCallEvent,
    CheckResultEvent,
    CostEvent,
    PolicyEvent,
    RoundFinishedEvent,
    RoundStartedEvent,
    StageFinishedEvent,
    StageStartedEvent,
)
from app.monitor import (
    _read_event_stream,
    _summarize_event_stream,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bus(tmp_path: Path) -> EventBus:
    return make_event_bus(tmp_path)


# ---------------------------------------------------------------------------
# Tests: EventBus.emit
# ---------------------------------------------------------------------------

class TestEventBusEmit:
    def test_creates_events_jsonl(self, tmp_path: Path) -> None:
        bus = _bus(tmp_path)
        bus.emit(StageStartedEvent(stage_name="s1", target_repo="/repo"))
        assert (tmp_path / "events.jsonl").exists()

    def test_each_event_is_one_line(self, tmp_path: Path) -> None:
        bus = _bus(tmp_path)
        bus.emit(StageStartedEvent(stage_name="s1"))
        bus.emit(RoundStartedEvent(stage_name="s1", round_index=1))
        bus.emit(StageFinishedEvent(stage_name="s1", passed=True))
        lines = (tmp_path / "events.jsonl").read_text().splitlines()
        assert len(lines) == 3

    def test_emitted_line_is_valid_json(self, tmp_path: Path) -> None:
        bus = _bus(tmp_path)
        bus.emit(CostEvent(stage_name="s1", delta_usd=0.01, cumulative_usd=0.05))
        line = (tmp_path / "events.jsonl").read_text().strip()
        payload = json.loads(line)
        assert payload["event_type"] == "cost"
        assert payload["delta_usd"] == pytest.approx(0.01)

    def test_emit_is_fail_silent_on_bad_path(self, tmp_path: Path) -> None:
        bus = EventBus(tmp_path / "nonexistent_dir" / "sub")
        # Should not raise even though directory doesn't exist.
        bus.emit(StageStartedEvent(stage_name="s1"))


# ---------------------------------------------------------------------------
# Tests: EventBus.tail
# ---------------------------------------------------------------------------

class TestEventBusTail:
    def test_tail_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        bus = _bus(tmp_path)
        events, offset = bus.tail()
        assert events == []
        assert offset == 0

    def test_tail_reads_all_events_from_start(self, tmp_path: Path) -> None:
        bus = _bus(tmp_path)
        bus.emit(StageStartedEvent(stage_name="s1"))
        bus.emit(RoundStartedEvent(stage_name="s1", round_index=1))
        events, offset = bus.tail(since_offset=0)
        assert len(events) == 2
        assert offset > 0

    def test_tail_incremental_read(self, tmp_path: Path) -> None:
        bus = _bus(tmp_path)
        bus.emit(StageStartedEvent(stage_name="s1"))
        _, offset_after_first = bus.tail(since_offset=0)

        bus.emit(RoundStartedEvent(stage_name="s1", round_index=1))
        events, new_offset = bus.tail(since_offset=offset_after_first)

        assert len(events) == 1
        assert events[0].event_type == "round_started"  # type: ignore[union-attr]
        assert new_offset > offset_after_first

    def test_tail_discriminated_union_parsing(self, tmp_path: Path) -> None:
        bus = _bus(tmp_path)
        bus.emit(StageStartedEvent(stage_name="s1"))
        bus.emit(RoundFinishedEvent(stage_name="s1", round_index=1, gate_passed=True))
        bus.emit(CostEvent(stage_name="s1", delta_usd=0.02))
        bus.emit(PolicyEvent(stage_name="s1", policy_kind="budget", triggered=True))
        bus.emit(AgentCallEvent(stage_name="s1", round_index=1, agent_role="judge"))
        bus.emit(CheckResultEvent(stage_name="s1", round_index=1, passed=True))

        events, _ = bus.tail()
        types = [e.event_type for e in events]  # type: ignore[union-attr]
        assert types == [
            "stage_started",
            "round_finished",
            "cost",
            "policy",
            "agent_call",
            "check_result",
        ]

    def test_all_events_convenience(self, tmp_path: Path) -> None:
        bus = _bus(tmp_path)
        for i in range(5):
            bus.emit(RoundStartedEvent(stage_name="s1", round_index=i + 1))
        events = bus.all_events()
        assert len(events) == 5


# ---------------------------------------------------------------------------
# Tests: monitor._read_event_stream
# ---------------------------------------------------------------------------

class TestReadEventStream:
    def test_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        events, offset = _read_event_stream(tmp_path)
        assert events == []
        assert offset == 0

    def test_reads_events_as_dicts(self, tmp_path: Path) -> None:
        bus = _bus(tmp_path)
        bus.emit(StageStartedEvent(stage_name="s1", target_repo="/repo"))
        bus.emit(CostEvent(stage_name="s1", delta_usd=0.1))

        events, offset = _read_event_stream(tmp_path)
        assert len(events) == 2
        assert events[0]["event_type"] == "stage_started"
        assert events[1]["event_type"] == "cost"
        assert offset > 0

    def test_incremental_read(self, tmp_path: Path) -> None:
        bus = _bus(tmp_path)
        bus.emit(StageStartedEvent(stage_name="s1"))
        _, offset = _read_event_stream(tmp_path, since_offset=0)

        bus.emit(RoundStartedEvent(stage_name="s1", round_index=1))
        events, new_offset = _read_event_stream(tmp_path, since_offset=offset)

        assert len(events) == 1
        assert events[0]["event_type"] == "round_started"
        assert new_offset > offset

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        events_file = tmp_path / "events.jsonl"
        valid_line_1 = '{"event_type": "stage_started", "stage_name": "s1", "timestamp_monotonic": 0.0, "timestamp_iso": ""}'
        invalid_line = "not-valid-json"
        valid_line_2 = '{"event_type": "cost", "stage_name": "s1", "timestamp_monotonic": 0.0, "timestamp_iso": "", "delta_usd": 0.0, "cumulative_usd": 0.0, "round_index": 0, "input_tokens": 0, "output_tokens": 0, "model": ""}'
        events_file.write_text(
            "\n".join([valid_line_1, invalid_line, valid_line_2]) + "\n",
            encoding="utf-8",
        )
        events, _ = _read_event_stream(tmp_path)
        assert len(events) == 2


# ---------------------------------------------------------------------------
# Tests: monitor._summarize_event_stream
# ---------------------------------------------------------------------------

class TestSummarizeEventStream:
    def test_empty_stream(self) -> None:
        summary = _summarize_event_stream([])
        assert summary["total_events"] == 0
        assert summary["stage_summaries"] == {}
        assert summary["latest_cost_event"] == {}

    def test_counts_events_per_stage(self) -> None:
        events = [
            {"event_type": "stage_started", "stage_name": "s1"},
            {"event_type": "round_started", "stage_name": "s1", "round_index": 1},
            {"event_type": "round_finished", "stage_name": "s1", "round_index": 1},
            {"event_type": "stage_started", "stage_name": "s2"},
        ]
        summary = _summarize_event_stream(events)
        assert summary["total_events"] == 4
        assert summary["stage_summaries"]["s1"]["event_count"] == 3
        assert summary["stage_summaries"]["s2"]["event_count"] == 1

    def test_tracks_max_round_per_stage(self) -> None:
        events = [
            {"event_type": "round_started", "stage_name": "s1", "round_index": 1},
            {"event_type": "round_finished", "stage_name": "s1", "round_index": 1},
            {"event_type": "round_started", "stage_name": "s1", "round_index": 2},
            {"event_type": "round_finished", "stage_name": "s1", "round_index": 2},
        ]
        summary = _summarize_event_stream(events)
        assert summary["stage_summaries"]["s1"]["rounds_seen"] == 2

    def test_latest_cost_event(self) -> None:
        events = [
            {"event_type": "cost", "stage_name": "s1", "delta_usd": 0.01, "cumulative_usd": 0.01},
            {"event_type": "cost", "stage_name": "s1", "delta_usd": 0.02, "cumulative_usd": 0.03},
        ]
        summary = _summarize_event_stream(events)
        assert summary["latest_cost_event"]["cumulative_usd"] == pytest.approx(0.03)

    def test_byte_offset_in_summary(self, tmp_path: Path) -> None:
        bus = _bus(tmp_path)
        bus.emit(StageStartedEvent(stage_name="s1"))
        events, offset = _read_event_stream(tmp_path)
        summary = _summarize_event_stream(events)
        # build_monitor_payload injects byte_offset; verify _read_event_stream returns > 0
        assert offset > 0
        assert summary["total_events"] == 1


# ---------------------------------------------------------------------------
# Tests: _emit_runtime_status_event phase mapping (fix-1/fix-2)
# ---------------------------------------------------------------------------

class TestEmitRuntimeStatusEventMapping:
    """Verify that _emit_runtime_status_event maps real orchestrator phases
    to the correct event types."""

    def _make_flow_with_bus(self, tmp_path: Path) -> object:
        """Create a minimal flow-like object with an event bus."""
        bus = _bus(tmp_path)

        class FakeFlow:
            def _emit_event(self, event: object) -> None:
                bus.emit(event)

        flow = FakeFlow()
        flow._bus = bus  # type: ignore[attr-defined]
        return flow

    def test_stage_start_emits_stage_started(self, tmp_path: Path) -> None:
        from core.models import RuntimeStatusSnapshot
        from persistence.runtime_persistence import (
            _emit_runtime_status_event,
        )

        flow = self._make_flow_with_bus(tmp_path)
        snapshot = RuntimeStatusSnapshot(
            target_repo="/repo",
            current_stage="lint",
            current_round=0,
            phase="stage_start",
            overall_state="running",
            worker_states={"worker_a": "idle"},
        )
        _emit_runtime_status_event(flow, snapshot)
        events = flow._bus.all_events()  # type: ignore[attr-defined]
        assert len(events) == 1
        assert events[0].event_type == "stage_started"  # type: ignore[union-attr]

    def test_stage_passed_emits_stage_finished(self, tmp_path: Path) -> None:
        from core.models import RuntimeStatusSnapshot
        from persistence.runtime_persistence import (
            _emit_runtime_status_event,
        )

        flow = self._make_flow_with_bus(tmp_path)
        snapshot = RuntimeStatusSnapshot(
            target_repo="/repo",
            current_stage="lint",
            current_round=3,
            phase="stage_passed",
            overall_state="passed",
            worker_states={"worker_a": "done"},
        )
        _emit_runtime_status_event(flow, snapshot)
        events = flow._bus.all_events()  # type: ignore[attr-defined]
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "stage_finished"  # type: ignore[union-attr]
        assert event.passed is True  # type: ignore[union-attr]
        assert event.rounds_used == 3  # type: ignore[union-attr]

    def test_stage_failed_emits_stage_finished(self, tmp_path: Path) -> None:
        from core.models import RuntimeStatusSnapshot
        from persistence.runtime_persistence import (
            _emit_runtime_status_event,
        )

        flow = self._make_flow_with_bus(tmp_path)
        snapshot = RuntimeStatusSnapshot(
            target_repo="/repo",
            current_stage="lint",
            current_round=5,
            phase="stage_failed",
            overall_state="failed",
            worker_states={"worker_a": "blocked"},
        )
        _emit_runtime_status_event(flow, snapshot)
        events = flow._bus.all_events()  # type: ignore[attr-defined]
        assert len(events) == 1
        assert events[0].event_type == "stage_finished"  # type: ignore[union-attr]
        assert events[0].passed is False  # type: ignore[union-attr]

    def test_round_start_emits_round_started(self, tmp_path: Path) -> None:
        from core.models import RuntimeStatusSnapshot
        from persistence.runtime_persistence import (
            _emit_runtime_status_event,
        )

        flow = self._make_flow_with_bus(tmp_path)
        snapshot = RuntimeStatusSnapshot(
            target_repo="/repo",
            current_stage="lint",
            current_round=2,
            phase="round_start",
            overall_state="running",
            worker_states={"worker_a": "planning"},
        )
        _emit_runtime_status_event(flow, snapshot)
        events = flow._bus.all_events()  # type: ignore[attr-defined]
        assert len(events) == 1
        assert events[0].event_type == "round_started"  # type: ignore[union-attr]

    def test_judge_gate_review_emits_round_finished(self, tmp_path: Path) -> None:
        from core.models import RuntimeStatusSnapshot
        from persistence.runtime_persistence import (
            _emit_runtime_status_event,
        )

        flow = self._make_flow_with_bus(tmp_path)
        snapshot = RuntimeStatusSnapshot(
            target_repo="/repo",
            current_stage="lint",
            current_round=2,
            phase="judge_gate_review",
            overall_state="running",
            worker_states={"worker_a": "triaged", "worker_b": "triaged"},
        )
        _emit_runtime_status_event(flow, snapshot)
        events = flow._bus.all_events()  # type: ignore[attr-defined]
        assert len(events) == 1
        assert events[0].event_type == "round_finished"  # type: ignore[union-attr]

    def test_plan_gate_review_emits_round_finished(self, tmp_path: Path) -> None:
        from core.models import RuntimeStatusSnapshot
        from persistence.runtime_persistence import (
            _emit_runtime_status_event,
        )

        flow = self._make_flow_with_bus(tmp_path)
        snapshot = RuntimeStatusSnapshot(
            target_repo="/repo",
            current_stage="lint",
            current_round=1,
            phase="plan_gate_review",
            overall_state="running",
            worker_states={"worker_a": "planning"},
        )
        _emit_runtime_status_event(flow, snapshot)
        events = flow._bus.all_events()  # type: ignore[attr-defined]
        assert len(events) == 1
        assert events[0].event_type == "round_finished"  # type: ignore[union-attr]

    def test_unknown_phase_emits_nothing(self, tmp_path: Path) -> None:
        from core.models import RuntimeStatusSnapshot
        from persistence.runtime_persistence import (
            _emit_runtime_status_event,
        )

        flow = self._make_flow_with_bus(tmp_path)
        snapshot = RuntimeStatusSnapshot(
            target_repo="/repo",
            current_stage="lint",
            current_round=1,
            phase="some_unknown_phase",
            overall_state="running",
            worker_states={},
        )
        _emit_runtime_status_event(flow, snapshot)
        events = flow._bus.all_events()  # type: ignore[attr-defined]
        assert len(events) == 0


# ---------------------------------------------------------------------------
# Tests: AgentCallEvent.round_index via _last_runtime_snapshot
# ---------------------------------------------------------------------------

class TestAgentCallEventRoundIndex:
    """Verify that _emit_agent_call_event reads round_index from
    flow._last_runtime_snapshot (set by persist_runtime_status)."""

    def _make_flow_with_bus(self, tmp_path: Path) -> object:
        bus = make_event_bus(tmp_path)

        class FakeArtifactStore:
            def write_json(self, path: object, data: object) -> None:
                pass

        class FakeFlow:
            artifact_store = FakeArtifactStore()

            def _emit_event(self, event: object) -> None:
                bus.emit(event)

            @staticmethod
            def _artifact_path(category: str, filename: str) -> str:
                return f"{category}/{filename}"

        flow = FakeFlow()
        flow._bus = bus  # type: ignore[attr-defined]
        return flow

    def test_round_index_from_cached_snapshot(self, tmp_path: Path) -> None:
        from adapters.structured_agents import (
            _emit_agent_call_event,
        )
        from core.models import RuntimeStatusSnapshot
        from persistence.runtime_persistence import (
            persist_runtime_status,
        )

        flow = self._make_flow_with_bus(tmp_path)

        # Simulate persist_runtime_status caching the snapshot on flow.
        persist_runtime_status(
            flow,
            RuntimeStatusSnapshot(
                target_repo="/repo",
                current_stage="lint",
                current_round=3,
                phase="round_start",
                overall_state="running",
                worker_states={"worker_a": "planning"},
            ),
        )

        _emit_agent_call_event(
            flow,
            agent_role="judge",
            stage_name="lint",
        )

        events = flow._bus.all_events()  # type: ignore[attr-defined]
        agent_events = [
            e for e in events
            if getattr(e, "event_type", None) == "agent_call"
        ]
        assert len(agent_events) == 1
        assert agent_events[0].round_index == 3  # type: ignore[union-attr]

    def test_round_index_fallback_without_snapshot(self, tmp_path: Path) -> None:
        from adapters.structured_agents import (
            _emit_agent_call_event,
        )

        flow = self._make_flow_with_bus(tmp_path)

        # No persist_runtime_status called → no _last_runtime_snapshot.
        _emit_agent_call_event(
            flow,
            agent_role="worker_a",
            stage_name="lint",
        )

        events = flow._bus.all_events()  # type: ignore[attr-defined]
        assert len(events) == 1
        assert events[0].round_index == 0  # type: ignore[union-attr]
