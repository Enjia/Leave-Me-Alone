from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from core.models import (
    AutoCheckResult,
    BuildStrategyProfile,
    CheckSummaryArtifact,
    FailureClassification,
    JudgeGateReview,
    PlanDriftArtifact,
    TieredGateCommand,
    StageSpec,
    WorkerDelivery,
    WorkerEntryPacket,
    WorkerPlan,
)
from orchestrator.round_runner import (
    RoundContinueResult,
    RoundPassResult,
    _capture_workspace_artifacts_pair,
    _run_stage_checks,
    _run_stage_checks_pair,
    apply_round_outcome,
    run_round_delivery_phase,
)


def test_run_round_delivery_phase_invokes_workers_concurrently() -> None:
    stage = StageSpec(
        name="stage3",
        objective="implement stage3",
        acceptance_criteria=["done"],
        invariants=["keep api stable"],
    )
    worker_a_plan = WorkerPlan(worker="worker_a", stage_name=stage.name, round_index=1, goal="a")
    worker_b_plan = WorkerPlan(worker="worker_b", stage_name=stage.name, round_index=1, goal="b")
    worker_a_entry_packet = WorkerEntryPacket(
        worker="worker_a",
        stage_name=stage.name,
        round_index=1,
        objective=stage.objective,
    )
    worker_b_entry_packet = WorkerEntryPacket(
        worker="worker_b",
        stage_name=stage.name,
        round_index=1,
        objective=stage.objective,
    )

    class FakeCheckRunner:
        def __init__(self) -> None:
            self.gate_tiers: list[str] = []

        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
        ) -> AutoCheckResult:
            del worker, stage_obj, workspace
            self.gate_tiers.append(gate_tier)
            return AutoCheckResult(
                worker="worker",
                stage_name=stage.name,
                all_tests_passed=True,
                all_lint_passed=True,
                all_perf_passed=True,
                all_harness_passed=True,
            )

    class FakeWorkspacePort:
        def capture_artifacts(self, workspace: str, baseline_snapshot: object | None = None) -> SimpleNamespace:
            del baseline_snapshot
            return SimpleNamespace(
                workspace=workspace,
                changed_files=[],
                status_lines=[],
                patch="",
                review_patch="",
            )

    class FakeFlow:
        def __init__(self) -> None:
            self.state = SimpleNamespace(target_repo="/tmp/repo")
            self.agents = SimpleNamespace(
                worker_a=object(),
                worker_b=object(),
                worker_a_workspace="/tmp/worker_a",
                worker_b_workspace="/tmp/worker_b",
            )
            self.check_runner = FakeCheckRunner()
            self.workspace_port = FakeWorkspacePort()
            self.starts = {"worker_a": asyncio.Event(), "worker_b": asyncio.Event()}
            self.persisted_deliveries: list[str] = []

        async def _invoke_worker_delivery_for_round(self, **kwargs: object) -> WorkerDelivery:
            worker = str(kwargs["worker_name"])
            other = "worker_b" if worker == "worker_a" else "worker_a"
            self.starts[worker].set()
            await asyncio.wait_for(self.starts[other].wait(), timeout=0.2)
            return WorkerDelivery(worker=worker, summary=f"{worker} done")

        def _persist_worker_delivery(self, *, stage_name: str, round_index: int, delivery: WorkerDelivery) -> None:
            del stage_name, round_index
            self.persisted_deliveries.append(delivery.worker)

        def _build_clean_state_artifact(
            self,
            *,
            stage_name: str,
            round_index: int,
            worker: str,
            delivery: WorkerDelivery,
        ) -> SimpleNamespace:
            del stage_name, round_index, worker, delivery
            return SimpleNamespace(passed=True, worker="worker", undocumented_blockers=[], model_dump=lambda: {})

        def _persist_clean_state_artifact(self, artifact: object) -> None:
            del artifact

        def _persist_failure_event(self, artifact: object) -> None:
            del artifact

        def _build_plan_drift_artifact(
            self,
            *,
            stage_name: str,
            round_index: int,
            worker: str,
            plan: WorkerPlan,
            delivery: WorkerDelivery,
        ) -> PlanDriftArtifact:
            del plan, delivery
            return PlanDriftArtifact(stage_name=stage_name, round_index=round_index, worker=worker, severity="none")

        def _persist_plan_drift_artifact(self, artifact: object) -> None:
            del artifact

        def _build_runtime_nudges(self, **kwargs: object) -> list[object]:
            del kwargs
            return []

        def _persist_runtime_nudge(self, artifact: object) -> None:
            del artifact

        def _remaining_stage_budget_sec(self, *, stage_name: str, stage_deadline_monotonic: float) -> int:
            del stage_name, stage_deadline_monotonic
            return 60

        def _persist_runtime_status(self, status: object) -> None:
            del status

        def _build_stage_dashboard_artifact(self, **kwargs: object) -> object:
            return SimpleNamespace(**kwargs)

        def _persist_stage_dashboard_artifact(self, artifact: object) -> None:
            del artifact

        def _stage_artifact_ref(self, stage_name: str, suffix: str) -> str:
            return f"{stage_name}:{suffix}"

        def _build_check_summary_artifact(
            self,
            *,
            worker: str,
            stage_name: str,
            round_index: int,
            phase: str,
            checks: AutoCheckResult,
            raw_summary: str,
        ) -> CheckSummaryArtifact:
            del checks
            return CheckSummaryArtifact(
                worker=worker,
                stage_name=stage_name,
                round_index=round_index,
                phase=phase,
                raw_summary=raw_summary,
            )

        def _persist_check_summary_artifact(self, artifact: object) -> None:
            del artifact

    flow = FakeFlow()
    result = asyncio.run(
        run_round_delivery_phase(
            flow,
            stage=stage,
            stage_gate=SimpleNamespace(),
            round_index=1,
            judge_feedback=[],
            worker_a_plan=worker_a_plan,
            worker_b_plan=worker_b_plan,
            worker_a_entry_packet=worker_a_entry_packet,
            worker_b_entry_packet=worker_b_entry_packet,
            prev_check_summary_a="",
            prev_check_summary_b="",
            context_packet_json="{}",
            review_baseline_a=object(),
            review_baseline_b=object(),
            stage_deadline_monotonic=0.0,
        )
    )

    assert result.worker_a_delivery.worker == "worker_a"
    assert result.worker_b_delivery.worker == "worker_b"
    assert sorted(flow.persisted_deliveries) == ["worker_a", "worker_b"]
    assert sorted(flow.check_runner.gate_tiers) == ["fast_round", "fast_round"]


def test_run_round_delivery_phase_cancels_peer_on_failure() -> None:
    stage = StageSpec(
        name="stage3",
        objective="implement stage3",
        acceptance_criteria=["done"],
        invariants=["keep api stable"],
    )
    worker_a_plan = WorkerPlan(worker="worker_a", stage_name=stage.name, round_index=1, goal="a")
    worker_b_plan = WorkerPlan(worker="worker_b", stage_name=stage.name, round_index=1, goal="b")
    worker_a_entry_packet = WorkerEntryPacket(
        worker="worker_a",
        stage_name=stage.name,
        round_index=1,
        objective=stage.objective,
    )
    worker_b_entry_packet = WorkerEntryPacket(
        worker="worker_b",
        stage_name=stage.name,
        round_index=1,
        objective=stage.objective,
    )

    class FakeFlow:
        def __init__(self) -> None:
            self.state = SimpleNamespace(target_repo="/tmp/repo")
            self.agents = SimpleNamespace(
                worker_a=object(),
                worker_b=object(),
                worker_a_workspace="/tmp/worker_a",
                worker_b_workspace="/tmp/worker_b",
            )
            self.check_runner = SimpleNamespace()
            self.workspace_port = SimpleNamespace()
            self.side_effects: list[str] = []
            self.worker_b_cancelled = asyncio.Event()

        async def _invoke_worker_delivery_for_round(self, **kwargs: object) -> WorkerDelivery:
            worker = str(kwargs["worker_name"])
            if worker == "worker_a":
                raise RuntimeError("worker_a failed")
            try:
                await asyncio.sleep(1)
                self.side_effects.append("worker_b_completed")
                return WorkerDelivery(worker=worker, summary="worker_b done")
            except asyncio.CancelledError:
                self.worker_b_cancelled.set()
                raise

        def _persist_worker_delivery(self, *, stage_name: str, round_index: int, delivery: WorkerDelivery) -> None:
            del stage_name, round_index, delivery
            raise AssertionError("delivery should not persist after failure")

    flow = FakeFlow()

    with pytest.raises(RuntimeError, match="worker_a failed"):
        asyncio.run(
            run_round_delivery_phase(
                flow,
                stage=stage,
                stage_gate=SimpleNamespace(),
                round_index=1,
                judge_feedback=[],
                worker_a_plan=worker_a_plan,
                worker_b_plan=worker_b_plan,
                worker_a_entry_packet=worker_a_entry_packet,
                worker_b_entry_packet=worker_b_entry_packet,
                prev_check_summary_a="",
                prev_check_summary_b="",
                context_packet_json="{}",
                review_baseline_a=object(),
                review_baseline_b=object(),
                stage_deadline_monotonic=0.0,
            )
        )

    assert flow.worker_b_cancelled.is_set()
    assert flow.side_effects == []


def test_capture_workspace_artifacts_pair_runs_concurrently() -> None:
    class FakeWorkspacePort:
        def __init__(self) -> None:
            self.starts = {"worker_a": asyncio.Event(), "worker_b": asyncio.Event()}

        def capture_artifacts(self, workspace: str, baseline_snapshot: object | None = None) -> SimpleNamespace:
            del baseline_snapshot
            worker = "worker_a" if workspace.endswith("worker_a") else "worker_b"

            async def _wait_for_other() -> None:
                other = "worker_b" if worker == "worker_a" else "worker_a"
                self.starts[worker].set()
                await asyncio.wait_for(self.starts[other].wait(), timeout=0.2)

            asyncio.run(_wait_for_other())
            return SimpleNamespace(workspace=workspace, changed_files=[], status_lines=[], patch="", review_patch="")

    flow = SimpleNamespace(
        workspace_port=FakeWorkspacePort(),
        agents=SimpleNamespace(worker_a_workspace="/tmp/worker_a", worker_b_workspace="/tmp/worker_b"),
    )

    patch_a, patch_b = asyncio.run(
        _capture_workspace_artifacts_pair(flow, baseline_a=object(), baseline_b=object())
    )

    assert patch_a.workspace.endswith("worker_a")
    assert patch_b.workspace.endswith("worker_b")


def test_run_stage_checks_passes_budget_kwargs_when_supported() -> None:
    stage = StageSpec(
        name="stage3",
        objective="implement stage3",
        acceptance_criteria=["done"],
        invariants=["keep api stable"],
    )
    captured: list[dict[str, int | str | None]] = []

    class FakeCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
            stage_budget_sec: int | None = None,
            round_budget_sec: int | None = None,
            phase_timeout_cap_sec: int | None = None,
            heartbeat_sink: object | None = None,
        ) -> AutoCheckResult:
            del stage_obj, workspace
            captured.append(
                {
                    "worker": worker,
                    "gate_tier": gate_tier,
                    "stage_budget_sec": stage_budget_sec,
                    "round_budget_sec": round_budget_sec,
                    "phase_timeout_cap_sec": phase_timeout_cap_sec,
                    "has_heartbeat_sink": callable(heartbeat_sink),
                }
            )
            return AutoCheckResult(worker=worker, stage_name=stage.name)

    flow = SimpleNamespace(
        state=SimpleNamespace(max_round_per_stage=4),
        check_runner=FakeCheckRunner(),
        _remaining_stage_budget_sec=lambda **kwargs: 1_200,
    )
    result = asyncio.run(
        _run_stage_checks(
            flow,
            worker="worker_a",
            stage=stage,
            workspace="/tmp/worker_a",
            gate_tier="pre_promotion",
            round_index=2,
            stage_deadline_monotonic=10.0,
        )
    )

    assert isinstance(result, AutoCheckResult)
    assert captured == [
        {
            "worker": "worker_a",
            "gate_tier": "pre_promotion",
            "stage_budget_sec": 1_200,
            "round_budget_sec": 400,
            "phase_timeout_cap_sec": 1_800,
            "has_heartbeat_sink": False,
        }
    ]


def test_run_stage_checks_legacy_signature_is_compatible() -> None:
    stage = StageSpec(
        name="stage3",
        objective="implement stage3",
        acceptance_criteria=["done"],
        invariants=["keep api stable"],
    )
    captured: list[tuple[str, str]] = []

    class LegacyCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
        ) -> AutoCheckResult:
            del stage_obj, workspace
            captured.append((worker, gate_tier))
            return AutoCheckResult(worker=worker, stage_name=stage.name)

    flow = SimpleNamespace(
        state=SimpleNamespace(max_round_per_stage=4),
        check_runner=LegacyCheckRunner(),
        _remaining_stage_budget_sec=lambda **kwargs: 1_200,
    )
    result = asyncio.run(
        _run_stage_checks(
            flow,
            worker="worker_b",
            stage=stage,
            workspace="/tmp/worker_b",
            gate_tier="full_regression",
            round_index=1,
            stage_deadline_monotonic=9.0,
        )
    )

    assert isinstance(result, AutoCheckResult)
    assert captured == [("worker_b", "full_regression")]


def test_run_stage_checks_writes_remote_heartbeat_when_supported(tmp_path) -> None:
    stage = StageSpec(
        name="stage3",
        objective="implement stage3",
        acceptance_criteria=["done"],
        invariants=["keep api stable"],
    )
    writes: list[tuple[object, object]] = []

    class ArtifactStore:
        def write_json(self, path, payload) -> None:
            writes.append((path, payload))

    class FakeCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
            heartbeat_sink: object | None = None,
        ) -> AutoCheckResult:
            del stage_obj, workspace, gate_tier
            assert callable(heartbeat_sink)
            heartbeat_sink(
                {
                    "event": "start",
                    "status": "running",
                    "heartbeat_id": "hb-1",
                    "remote_host": "node0",
                    "remote_workdir": "/tmp/worker_a",
                    "command": "echo ok",
                    "command_index": 1,
                    "command_total": 1,
                    "timeout_sec": 30,
                    "elapsed_sec": 1,
                    "started_at": "2026-04-01T00:00:00+00:00",
                    "last_progress_at": "2026-04-01T00:00:01+00:00",
                    "last_output_at": "",
                }
            )
            return AutoCheckResult(worker=worker, stage_name=stage.name)

    flow = SimpleNamespace(
        state=SimpleNamespace(max_round_per_stage=4),
        check_runner=FakeCheckRunner(),
        artifact_store=ArtifactStore(),
        _remaining_stage_budget_sec=lambda **kwargs: 1_200,
        _artifact_path=lambda scope, filename: tmp_path / f"{scope}_{filename}",
    )
    result = asyncio.run(
        _run_stage_checks(
            flow,
            worker="worker_a",
            stage=stage,
            workspace="/tmp/worker_a",
            gate_tier="fast_round",
            round_index=1,
            stage_deadline_monotonic=10.0,
        )
    )

    assert isinstance(result, AutoCheckResult)
    assert writes
    heartbeat_payload = next(
        payload
        for path, payload in writes
        if str(path).endswith("runtime_remote_check_heartbeats.json")
    )
    summary_payload = next(
        payload
        for path, payload in writes
        if str(path).endswith("runtime_timeout_recovery_summary.json")
    )
    payload = heartbeat_payload
    assert payload["active"]
    assert payload["active"][0]["stage_name"] == "stage3"
    assert payload["active"][0]["worker"] == "worker_a"
    assert summary_payload["totals"]["timeout_recovery_attempted"] == 0


def test_run_stage_checks_moves_stale_heartbeat_from_active_to_recent(tmp_path) -> None:
    stage = StageSpec(
        name="stage3",
        objective="implement stage3",
        acceptance_criteria=["done"],
        invariants=["keep api stable"],
    )
    heartbeat_path = tmp_path / "runtime_remote_check_heartbeats.json"
    recovery_summary_path = tmp_path / "runtime_timeout_recovery_summary.json"
    heartbeat_path.write_text(
        json.dumps(
            {
                "updated_at_epoch_sec": 2_000,
                "active": [
                    {
                        "heartbeat_id": "hb-stale",
                        "stage_name": "stage3",
                        "round_index": 1,
                        "worker": "worker_b",
                        "gate_tier": "fast_round",
                        "remote_host": "node1",
                        "remote_workdir": "/tmp/worker_b",
                        "command": "echo stale",
                        "command_index": 1,
                        "command_total": 1,
                        "timeout_sec": 10,
                        "elapsed_sec": 10,
                        "started_at": "2026-04-01T00:00:00+00:00",
                        "last_progress_at": "2026-04-01T00:00:10+00:00",
                        "last_output_at": "",
                        "status": "running",
                        "updated_at_epoch_sec": 1,
                    }
                ],
                "recent": [],
            }
        ),
        encoding="utf-8",
    )

    class ArtifactStore:
        def write_json(self, path, payload) -> None:
            path.write_text(json.dumps(payload), encoding="utf-8")

    class FakeCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
            heartbeat_sink: object | None = None,
        ) -> AutoCheckResult:
            del stage_obj, workspace, gate_tier
            assert callable(heartbeat_sink)
            heartbeat_sink(
                {
                    "event": "start",
                    "status": "running",
                    "heartbeat_id": "hb-fresh",
                    "remote_host": "node0",
                    "remote_workdir": "/tmp/worker_a",
                    "command": "echo fresh",
                    "command_index": 1,
                    "command_total": 1,
                    "timeout_sec": 30,
                    "elapsed_sec": 1,
                    "started_at": "2026-04-01T00:00:00+00:00",
                    "last_progress_at": "2026-04-01T00:00:01+00:00",
                    "last_output_at": "",
                }
            )
            return AutoCheckResult(worker=worker, stage_name=stage.name)

    flow = SimpleNamespace(
        state=SimpleNamespace(max_round_per_stage=4),
        check_runner=FakeCheckRunner(),
        artifact_store=ArtifactStore(),
        _remaining_stage_budget_sec=lambda **kwargs: 1_200,
        _artifact_path=lambda scope, filename: heartbeat_path if filename == "remote_check_heartbeats.json" else recovery_summary_path,
        _read_positive_env_int=lambda key, default: 1 if key == "MULTI_CODEX_REMOTE_HEARTBEAT_STALE_TTL_SEC" else default,
    )

    result = asyncio.run(
        _run_stage_checks(
            flow,
            worker="worker_a",
            stage=stage,
            workspace="/tmp/worker_a",
            gate_tier="fast_round",
            round_index=1,
            stage_deadline_monotonic=10.0,
        )
    )

    assert isinstance(result, AutoCheckResult)
    payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    active_ids = [item["heartbeat_id"] for item in payload["active"]]
    assert active_ids == ["hb-fresh"]
    recent_ids = [item["heartbeat_id"] for item in payload["recent"]]
    assert "hb-stale" in recent_ids
    stale_entry = next(item for item in payload["recent"] if item["heartbeat_id"] == "hb-stale")
    assert stale_entry["status"] == "stale"
    assert stale_entry["event"] == "stale_timeout"
    summary = json.loads(recovery_summary_path.read_text(encoding="utf-8"))
    assert summary["totals"]["stale_recycled"] >= 1


def test_run_stage_checks_updates_timeout_recovery_summary_artifact(tmp_path) -> None:
    stage = StageSpec(
        name="stage3",
        objective="implement stage3",
        acceptance_criteria=["done"],
        invariants=["keep api stable"],
    )
    heartbeat_path = tmp_path / "runtime_remote_check_heartbeats.json"
    recovery_summary_path = tmp_path / "runtime_timeout_recovery_summary.json"

    class ArtifactStore:
        def write_json(self, path, payload) -> None:
            path.write_text(json.dumps(payload), encoding="utf-8")

    class FakeCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
            heartbeat_sink: object | None = None,
        ) -> AutoCheckResult:
            del stage_obj, workspace, gate_tier
            assert callable(heartbeat_sink)
            heartbeat_sink(
                {
                    "event": "timeout_recovery",
                    "status": "recovered",
                    "heartbeat_id": "hb-timeout",
                    "remote_host": "node0",
                    "remote_workdir": "/tmp/worker_a",
                    "command": "python3 tests/smoke_suite.py --scenario all",
                    "command_index": 1,
                    "command_total": 1,
                    "timeout_sec": 300,
                    "elapsed_sec": 300,
                    "started_at": "2026-04-01T00:00:00+00:00",
                    "last_progress_at": "2026-04-01T00:05:00+00:00",
                    "last_output_at": "",
                    "recovery": {"recovered": True, "summary": "remote timeout recovery succeeded"},
                }
            )
            return AutoCheckResult(worker=worker, stage_name=stage.name)

    flow = SimpleNamespace(
        state=SimpleNamespace(max_round_per_stage=4),
        check_runner=FakeCheckRunner(),
        artifact_store=ArtifactStore(),
        _remaining_stage_budget_sec=lambda **kwargs: 1_200,
        _artifact_path=lambda scope, filename: heartbeat_path if filename == "remote_check_heartbeats.json" else recovery_summary_path,
    )

    result = asyncio.run(
        _run_stage_checks(
            flow,
            worker="worker_a",
            stage=stage,
            workspace="/tmp/worker_a",
            gate_tier="pre_promotion",
            round_index=1,
            stage_deadline_monotonic=10.0,
        )
    )

    assert isinstance(result, AutoCheckResult)
    summary = json.loads(recovery_summary_path.read_text(encoding="utf-8"))
    assert summary["totals"]["timeout_recovery_attempted"] == 1
    assert summary["totals"]["timeout_recovery_recovered"] == 1
    assert summary["totals"]["timeout_recovery_failed"] == 0
    assert summary["by_stage"]["stage3"]["timeout_recovery_attempted"] == 1


def test_run_stage_checks_timeout_recovery_windows_are_configurable(tmp_path) -> None:
    stage = StageSpec(
        name="stage3",
        objective="implement stage3",
        acceptance_criteria=["done"],
        invariants=["keep api stable"],
    )
    heartbeat_path = tmp_path / "runtime_remote_check_heartbeats.json"
    recovery_summary_path = tmp_path / "runtime_timeout_recovery_summary.json"

    class ArtifactStore:
        def write_json(self, path, payload) -> None:
            path.write_text(json.dumps(payload), encoding="utf-8")

    class FakeCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
            heartbeat_sink: object | None = None,
        ) -> AutoCheckResult:
            del stage_obj, workspace, gate_tier
            assert callable(heartbeat_sink)
            heartbeat_sink(
                {
                    "event": "timeout_recovery",
                    "status": "recovered",
                    "heartbeat_id": "hb-timeout-1",
                    "remote_host": "node0",
                    "remote_workdir": "/tmp/worker_a",
                    "command": "cmd-1",
                    "command_index": 1,
                    "command_total": 2,
                    "timeout_sec": 60,
                    "elapsed_sec": 60,
                    "started_at": "2026-04-01T00:00:00+00:00",
                    "last_progress_at": "2026-04-01T00:01:00+00:00",
                    "last_output_at": "",
                    "recovery": {"recovered": True, "summary": "ok-1"},
                }
            )
            heartbeat_sink(
                {
                    "event": "timeout_recovery",
                    "status": "cleanup_failed",
                    "heartbeat_id": "hb-timeout-2",
                    "remote_host": "node0",
                    "remote_workdir": "/tmp/worker_a",
                    "command": "cmd-2",
                    "command_index": 2,
                    "command_total": 2,
                    "timeout_sec": 60,
                    "elapsed_sec": 60,
                    "started_at": "2026-04-01T00:00:00+00:00",
                    "last_progress_at": "2026-04-01T00:01:00+00:00",
                    "last_output_at": "",
                    "recovery": {"recovered": False, "summary": "ok-2"},
                }
            )
            return AutoCheckResult(worker=worker, stage_name=stage.name)

    def _read_positive_env_int(key: str, default: int) -> int:
        if key == "MULTI_CODEX_TIMEOUT_RECOVERY_DEDUPE_IDS_LIMIT":
            return 1
        if key == "MULTI_CODEX_TIMEOUT_RECOVERY_RECENT_EVENTS_LIMIT":
            return 1
        return default

    flow = SimpleNamespace(
        state=SimpleNamespace(max_round_per_stage=4),
        check_runner=FakeCheckRunner(),
        artifact_store=ArtifactStore(),
        _remaining_stage_budget_sec=lambda **kwargs: 1_200,
        _artifact_path=lambda scope, filename: heartbeat_path if filename == "remote_check_heartbeats.json" else recovery_summary_path,
        _read_positive_env_int=_read_positive_env_int,
    )

    result = asyncio.run(
        _run_stage_checks(
            flow,
            worker="worker_a",
            stage=stage,
            workspace="/tmp/worker_a",
            gate_tier="pre_promotion",
            round_index=1,
            stage_deadline_monotonic=10.0,
        )
    )

    assert isinstance(result, AutoCheckResult)
    summary = json.loads(recovery_summary_path.read_text(encoding="utf-8"))
    assert summary["totals"]["timeout_recovery_attempted"] == 2
    assert len(summary["processed_event_ids"]) == 1
    assert len(summary["recent_events"]) == 1


def test_apply_round_outcome_runs_pre_promotion_gate_before_promotion() -> None:
    stage = StageSpec(
        name="stage3",
        objective="implement stage3",
        acceptance_criteria=["done"],
        invariants=["keep api stable"],
    )
    final_gate = JudgeGateReview(
        stage_name=stage.name,
        round_index=1,
        pass_gate=True,
        rationale="ready",
    )

    class FakeCheckRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
        ) -> AutoCheckResult:
            del stage_obj, workspace
            self.calls.append((worker, gate_tier))
            if worker == "worker_a":
                return AutoCheckResult(
                    worker=worker,
                    stage_name=stage.name,
                    all_tests_passed=False,
                    all_lint_passed=True,
                    all_perf_passed=True,
                    all_harness_passed=True,
                )
            return AutoCheckResult(
                worker=worker,
                stage_name=stage.name,
                all_tests_passed=True,
                all_lint_passed=True,
                all_perf_passed=True,
                all_harness_passed=True,
            )

    class FakeFlow:
        def __init__(self) -> None:
            self.state = SimpleNamespace(target_repo="/tmp/repo")
            self.agents = SimpleNamespace(worker_a_workspace="/tmp/worker_a", worker_b_workspace="/tmp/worker_b")
            self.workspace_port = SimpleNamespace(
                capture_snapshot=lambda workspace: f"snapshot:{workspace}",
            )
            self.check_runner = FakeCheckRunner()
            self.check_artifacts: list[CheckSummaryArtifact] = []
            self.failure_events: list[object] = []

        def _build_check_summary_artifact(
            self,
            *,
            worker: str,
            stage_name: str,
            round_index: int,
            phase: str,
            checks: AutoCheckResult,
            raw_summary: str,
        ) -> CheckSummaryArtifact:
            del checks
            return CheckSummaryArtifact(
                worker=worker,
                stage_name=stage_name,
                round_index=round_index,
                phase=phase,
                raw_summary=raw_summary,
            )

        def _persist_check_summary_artifact(self, artifact: CheckSummaryArtifact) -> None:
            self.check_artifacts.append(artifact)

        def _persist_failure_event(self, artifact: object) -> None:
            self.failure_events.append(artifact)

        def _stage_artifact_ref(self, stage_name: str, suffix: str) -> str:
            return f"{stage_name}:{suffix}"

    flow = FakeFlow()
    outcome = asyncio.run(
        apply_round_outcome(
            flow,
            stage=stage,
            round_index=1,
            round_logs=[],
            review_memory=[],
            final_gate=final_gate,
            auto_checks_a=AutoCheckResult(worker="worker_a", stage_name=stage.name),
            auto_checks_b=AutoCheckResult(worker="worker_b", stage_name=stage.name),
            check_summary_a="prev-a",
            check_summary_b="prev-b",
            review_baseline_a=object(),
            review_baseline_b=object(),
        )
    )

    assert isinstance(outcome, RoundContinueResult)
    assert sorted(flow.check_runner.calls) == [
        ("worker_a", "pre_promotion"),
        ("worker_b", "pre_promotion"),
    ]
    assert final_gate.pass_gate is False
    assert "pre_promotion gate failed for worker_a" in final_gate.required_actions
    assert len(flow.check_artifacts) == 2
    assert {artifact.phase for artifact in flow.check_artifacts} == {"pre_promotion"}
    assert "Tests passed: False" in outcome.prev_check_summary_a
    assert "Tests passed: True" in outcome.prev_check_summary_b
    assert flow.failure_events
    failure = flow.failure_events[-1]
    assert isinstance(failure.classification, FailureClassification)
    assert failure.classification.code == "pre_promotion_checks_failed"


def test_apply_round_outcome_runs_full_regression_when_declared() -> None:
    stage = StageSpec(
        name="stage3",
        objective="implement stage3",
        acceptance_criteria=["done"],
        invariants=["keep api stable"],
        gate_commands_remote_tiered=[
            TieredGateCommand(command="echo fast", tier="fast_round"),
            TieredGateCommand(command="echo full", tier="full_regression"),
        ],
    )
    final_gate = JudgeGateReview(
        stage_name=stage.name,
        round_index=1,
        pass_gate=True,
        rationale="ready",
    )

    class FakeCheckRunner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
        ) -> AutoCheckResult:
            del stage_obj, workspace
            self.calls.append((worker, gate_tier))
            if gate_tier == "full_regression" and worker == "worker_b":
                return AutoCheckResult(
                    worker=worker,
                    stage_name=stage.name,
                    all_tests_passed=False,
                    all_lint_passed=True,
                    all_perf_passed=True,
                    all_harness_passed=True,
                )
            return AutoCheckResult(
                worker=worker,
                stage_name=stage.name,
                all_tests_passed=True,
                all_lint_passed=True,
                all_perf_passed=True,
                all_harness_passed=True,
            )

    class FakeFlow:
        def __init__(self) -> None:
            self.state = SimpleNamespace(target_repo="/tmp/repo")
            self.agents = SimpleNamespace(worker_a_workspace="/tmp/worker_a", worker_b_workspace="/tmp/worker_b")
            self.workspace_port = SimpleNamespace(
                capture_snapshot=lambda workspace: f"snapshot:{workspace}",
            )
            self.check_runner = FakeCheckRunner()
            self.check_artifacts: list[CheckSummaryArtifact] = []
            self.failure_events: list[object] = []

        def _build_check_summary_artifact(
            self,
            *,
            worker: str,
            stage_name: str,
            round_index: int,
            phase: str,
            checks: AutoCheckResult,
            raw_summary: str,
        ) -> CheckSummaryArtifact:
            del checks
            return CheckSummaryArtifact(
                worker=worker,
                stage_name=stage_name,
                round_index=round_index,
                phase=phase,
                raw_summary=raw_summary,
            )

        def _persist_check_summary_artifact(self, artifact: CheckSummaryArtifact) -> None:
            self.check_artifacts.append(artifact)

        def _persist_failure_event(self, artifact: object) -> None:
            self.failure_events.append(artifact)

        def _stage_artifact_ref(self, stage_name: str, suffix: str) -> str:
            return f"{stage_name}:{suffix}"

    flow = FakeFlow()
    outcome = asyncio.run(
        apply_round_outcome(
            flow,
            stage=stage,
            round_index=1,
            round_logs=[],
            review_memory=[],
            final_gate=final_gate,
            auto_checks_a=AutoCheckResult(worker="worker_a", stage_name=stage.name),
            auto_checks_b=AutoCheckResult(worker="worker_b", stage_name=stage.name),
            check_summary_a="prev-a",
            check_summary_b="prev-b",
            review_baseline_a=object(),
            review_baseline_b=object(),
        )
    )

    assert isinstance(outcome, RoundContinueResult)
    assert sorted(flow.check_runner.calls) == [
        ("worker_a", "full_regression"),
        ("worker_a", "pre_promotion"),
        ("worker_b", "full_regression"),
        ("worker_b", "pre_promotion"),
    ]
    assert final_gate.pass_gate is False
    assert "full_regression gate failed for worker_b" in final_gate.required_actions
    assert "Tests passed: True" in outcome.prev_check_summary_a
    assert "Tests passed: False" in outcome.prev_check_summary_b
    assert {artifact.phase for artifact in flow.check_artifacts} == {
        "pre_promotion",
        "full_regression",
    }
    assert flow.failure_events
    failure = flow.failure_events[-1]
    assert isinstance(failure.classification, FailureClassification)
    assert failure.classification.code == "full_regression_checks_failed"


def test_apply_round_outcome_blocks_when_timeout_recovery_failures_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MULTI_CODEX_TIMEOUT_RECOVERY_BLOCK_CONSECUTIVE_FAILURES", "2")
    stage = StageSpec(
        name="stage3",
        objective="implement stage3",
        acceptance_criteria=["done"],
        invariants=["keep api stable"],
        gate_commands_remote_tiered=[
            TieredGateCommand(command="echo pre", tier="pre_promotion"),
        ],
    )
    final_gate = JudgeGateReview(
        stage_name=stage.name,
        round_index=2,
        pass_gate=True,
        rationale="ready",
    )
    summary_path = tmp_path / "runtime_timeout_recovery_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "by_stage_worker_gate_tier": {
                    "stage3:worker_a:pre_promotion": {
                        "timeout_recovery_attempted": 2,
                        "timeout_recovery_recovered": 0,
                        "timeout_recovery_failed": 2,
                        "stale_recycled": 0,
                        "consecutive_failed_recoveries": 2,
                        "max_consecutive_failed_recoveries": 2,
                        "last_event_status": "cleanup_failed",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class FakeCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
        ) -> AutoCheckResult:
            del stage_obj, workspace, gate_tier
            return AutoCheckResult(
                worker=worker,
                stage_name=stage.name,
                all_tests_passed=(worker == "worker_b"),
                all_lint_passed=True,
                all_perf_passed=True,
                all_harness_passed=True,
            )

    class FakeFlow:
        def __init__(self) -> None:
            self.state = SimpleNamespace(target_repo="/tmp/repo")
            self.agents = SimpleNamespace(worker_a_workspace="/tmp/worker_a", worker_b_workspace="/tmp/worker_b")
            self.workspace_port = SimpleNamespace(
                capture_snapshot=lambda workspace: f"snapshot:{workspace}",
            )
            self.check_runner = FakeCheckRunner()
            self.check_artifacts: list[CheckSummaryArtifact] = []
            self.failure_events: list[object] = []
            self.runtime_statuses: list[object] = []
            self.handoffs: list[object] = []
            self.ledgers: list[object] = []
            self.repo_progress_notes: list[object] = []
            self.stage_dashboards: list[object] = []

        def _build_check_summary_artifact(
            self,
            *,
            worker: str,
            stage_name: str,
            round_index: int,
            phase: str,
            checks: AutoCheckResult,
            raw_summary: str,
        ) -> CheckSummaryArtifact:
            del checks
            return CheckSummaryArtifact(
                worker=worker,
                stage_name=stage_name,
                round_index=round_index,
                phase=phase,
                raw_summary=raw_summary,
            )

        def _persist_check_summary_artifact(self, artifact: CheckSummaryArtifact) -> None:
            self.check_artifacts.append(artifact)

        def _persist_failure_event(self, artifact: object) -> None:
            self.failure_events.append(artifact)

        def _stage_artifact_ref(self, stage_name: str, suffix: str) -> str:
            return f"{stage_name}:{suffix}"

        def _artifact_path(self, scope: str, filename: str) -> Path:
            del scope
            assert filename == "timeout_recovery_summary.json"
            return summary_path

        def _read_positive_env_int(self, key: str, default: int) -> int:
            del default
            return int(
                {
                    "MULTI_CODEX_TIMEOUT_RECOVERY_BLOCK_CONSECUTIVE_FAILURES": 2,
                }.get(key, 2)
            )

        def _build_terminal_handoff_packet(
            self,
            *,
            worker: str,
            stage: StageSpec,
            round_index: int,
            final_gate: JudgeGateReview,
            trigger: str = "round_end",
        ) -> object:
            return SimpleNamespace(
                worker=worker,
                stage_name=stage.name,
                round_index=round_index,
                final_gate=final_gate,
                trigger=trigger,
            )

        def _persist_task_handoff_packet(self, packet: object) -> None:
            self.handoffs.append(packet)

        def _persist_runtime_status(self, status: object) -> None:
            self.runtime_statuses.append(status)

        def _build_stage_progress_ledger(self, **kwargs: object) -> object:
            return SimpleNamespace(**kwargs)

        def _persist_stage_progress_ledger(self, stage: StageSpec, ledger: object) -> None:
            del stage
            self.ledgers.append(ledger)

        def _persist_repo_progress_note(self, **kwargs: object) -> None:
            self.repo_progress_notes.append(kwargs)

        def _build_stage_dashboard_artifact(self, **kwargs: object) -> object:
            return SimpleNamespace(**kwargs)

        def _persist_stage_dashboard_artifact(self, artifact: object) -> None:
            self.stage_dashboards.append(artifact)

    flow = FakeFlow()
    outcome = asyncio.run(
        apply_round_outcome(
            flow,
            stage=stage,
            round_index=2,
            round_logs=[],
            review_memory=[],
            final_gate=final_gate,
            auto_checks_a=AutoCheckResult(worker="worker_a", stage_name=stage.name),
            auto_checks_b=AutoCheckResult(worker="worker_b", stage_name=stage.name),
            check_summary_a="prev-a",
            check_summary_b="prev-b",
            review_baseline_a=object(),
            review_baseline_b=object(),
        )
    )

    assert isinstance(outcome, RoundPassResult)
    assert outcome.stage_result.passed is False
    assert final_gate.pass_gate is False
    assert any("exhausted automatic recovery budget" in item for item in final_gate.required_actions)
    assert flow.runtime_statuses[-1].overall_state == "blocked"
    assert flow.ledgers[-1].status == "blocked"
    assert flow.handoffs[-1].trigger == "timeout_recovery"
    assert isinstance(flow.failure_events[-1].classification, FailureClassification)
    assert flow.failure_events[-1].classification.code == "pre_promotion_timeout_recovery_exhausted"


def test_apply_round_outcome_pass_records_heavy_gate_layers_in_ledger_and_dashboard() -> None:
    stage = StageSpec(
        name="stage3",
        objective="implement stage3",
        acceptance_criteria=["done"],
        invariants=["keep api stable"],
        gate_commands_remote_tiered=[
            TieredGateCommand(command="echo pre", tier="pre_promotion"),
            TieredGateCommand(command="echo full", tier="full_regression"),
        ],
    )
    final_gate = JudgeGateReview(
        stage_name=stage.name,
        round_index=1,
        pass_gate=True,
        rationale="ready",
    )

    class _Ready:
        ready = True
        unresolved_blockers: list[str] = []

        @staticmethod
        def model_dump() -> dict[str, object]:
            return {"ready": True}

    class FakeCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
        ) -> AutoCheckResult:
            del stage_obj, workspace, gate_tier
            return AutoCheckResult(
                worker=worker,
                stage_name=stage.name,
                all_tests_passed=True,
                all_lint_passed=True,
                all_perf_passed=True,
                all_harness_passed=True,
            )

    class FakeFlow:
        def __init__(self) -> None:
            self.state = SimpleNamespace(target_repo="/tmp/repo")
            self.agents = SimpleNamespace(worker_a_workspace="/tmp/worker_a", worker_b_workspace="/tmp/worker_b")
            self.workspace_port = SimpleNamespace()
            self.check_runner = FakeCheckRunner()
            self.ledger: dict[str, object] = {}
            self.dashboard: dict[str, object] = {}
            self.repo_note: dict[str, object] = {}

        def _build_check_summary_artifact(
            self,
            *,
            worker: str,
            stage_name: str,
            round_index: int,
            phase: str,
            checks: AutoCheckResult,
            raw_summary: str,
        ) -> CheckSummaryArtifact:
            del checks
            return CheckSummaryArtifact(
                worker=worker,
                stage_name=stage_name,
                round_index=round_index,
                phase=phase,
                raw_summary=raw_summary,
            )

        def _persist_check_summary_artifact(self, artifact: CheckSummaryArtifact) -> None:
            del artifact

        def _persist_failure_event(self, artifact: object) -> None:
            raise AssertionError(f"unexpected failure_event: {artifact}")

        def _build_promotion_readiness_artifact(self, **kwargs: object) -> _Ready:
            del kwargs
            return _Ready()

        def _persist_promotion_readiness_artifact(self, artifact: object) -> None:
            del artifact

        def _validate_stage_outputs(self, stage_obj: StageSpec, *, base_dir: str) -> list[str]:
            del stage_obj, base_dir
            return []

        def _owner_workspace_path(self) -> str:
            return "/tmp/owner"

        def _promote_owner_workspace(self, stage_obj: StageSpec) -> str:
            del stage_obj
            return ""

        def _persist_feature_checklist(self, artifact: object) -> None:
            del artifact

        def _build_stage_progress_ledger(self, **kwargs: object) -> dict[str, object]:
            return dict(kwargs)

        def _persist_stage_progress_ledger(self, stage_obj: StageSpec, ledger: dict[str, object]) -> None:
            del stage_obj
            self.ledger = dict(ledger)

        def _persist_repo_progress_note(self, **kwargs: object) -> None:
            self.repo_note = dict(kwargs)

        def _persist_runtime_status(self, status: object) -> None:
            del status

        def _build_stage_dashboard_artifact(self, **kwargs: object) -> dict[str, object]:
            return dict(kwargs)

        def _persist_stage_dashboard_artifact(self, artifact: dict[str, object]) -> None:
            self.dashboard = dict(artifact)

        def _stage_artifact_ref(self, stage_name: str, suffix: str) -> str:
            return f"{stage_name}:{suffix}"

    flow = FakeFlow()
    outcome = asyncio.run(
        apply_round_outcome(
            flow,
            stage=stage,
            round_index=1,
            round_logs=[],
            review_memory=[],
            final_gate=final_gate,
            auto_checks_a=AutoCheckResult(worker="worker_a", stage_name=stage.name),
            auto_checks_b=AutoCheckResult(worker="worker_b", stage_name=stage.name),
            check_summary_a="prev-a",
            check_summary_b="prev-b",
            review_baseline_a=object(),
            review_baseline_b=object(),
        )
    )

    assert isinstance(outcome, RoundPassResult)
    assert flow.ledger["status"] == "passed"
    assert flow.ledger["passed_gates"] == [
        "stage_initialized",
        "remote_preflight",
        "stage_gate",
        "plan_gate",
        "pre_promotion",
        "full_regression",
        "promotion_readiness",
        "promotion",
    ]
    assert flow.dashboard["status"] == "passed"
    assert flow.dashboard["latest_artifacts"] == flow.ledger["latest_artifacts"]
    assert "stage3:round1_worker_a_pre_promotion_checks.json" in flow.ledger["latest_artifacts"]
    assert "stage3:round1_worker_b_pre_promotion_checks.json" in flow.ledger["latest_artifacts"]
    assert "stage3:round1_worker_a_full_regression_checks.json" in flow.ledger["latest_artifacts"]
    assert "stage3:round1_worker_b_full_regression_checks.json" in flow.ledger["latest_artifacts"]
    assert "stage3:round1_promotion_readiness.json" in flow.ledger["latest_artifacts"]


def test_run_stage_checks_pair_serializes_make_tier(monkeypatch) -> None:
    monkeypatch.delenv("MULTI_CODEX_SERIALIZE_WORKER_REMOTE_CHECKS", raising=False)
    stage = StageSpec(
        name="stage-serialize-fast-round",
        objective="serialize compile gates",
        acceptance_criteria=["done"],
        invariants=["safe"],
        requires_remote=True,
        gate_commands_remote_tiered=[
            TieredGateCommand(command="make src.build", tier="fast_round"),
            TieredGateCommand(command="python3 tests/smoke_suite.py --scenario all", tier="pre_promotion"),
        ],
    )
    marks: dict[str, float] = {}

    class FakeCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
        ) -> AutoCheckResult:
            del stage_obj, workspace, gate_tier
            marks[f"{worker}_start"] = time.monotonic()
            await asyncio.sleep(0.03)
            marks[f"{worker}_end"] = time.monotonic()
            return AutoCheckResult(
                worker=worker,
                stage_name=stage.name,
                all_tests_passed=True,
                all_lint_passed=True,
                all_perf_passed=True,
                all_harness_passed=True,
            )

    flow = SimpleNamespace(
        check_runner=FakeCheckRunner(),
        agents=SimpleNamespace(
            worker_a_workspace="/tmp/worker_a",
            worker_b_workspace="/tmp/worker_b",
        ),
    )

    asyncio.run(
        _run_stage_checks_pair(
            flow,
            stage=stage,
            gate_tier="fast_round",
            round_index=1,
            stage_deadline_monotonic=None,
        )
    )

    assert marks["worker_b_start"] >= marks["worker_a_end"]


def test_run_stage_checks_pair_keeps_non_make_tier_parallel(monkeypatch) -> None:
    monkeypatch.delenv("MULTI_CODEX_SERIALIZE_WORKER_REMOTE_CHECKS", raising=False)
    stage = StageSpec(
        name="stage-parallel-heavy",
        objective="parallel non-compile gates",
        acceptance_criteria=["done"],
        invariants=["safe"],
        requires_remote=True,
        gate_commands_remote_tiered=[
            TieredGateCommand(command="make src.build", tier="fast_round"),
            TieredGateCommand(command="python3 tests/smoke_suite.py --scenario all", tier="pre_promotion"),
        ],
    )
    marks: dict[str, float] = {}

    class FakeCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
        ) -> AutoCheckResult:
            del stage_obj, workspace, gate_tier
            marks[f"{worker}_start"] = time.monotonic()
            await asyncio.sleep(0.05)
            marks[f"{worker}_end"] = time.monotonic()
            return AutoCheckResult(
                worker=worker,
                stage_name=stage.name,
                all_tests_passed=True,
                all_lint_passed=True,
                all_perf_passed=True,
                all_harness_passed=True,
            )

    flow = SimpleNamespace(
        check_runner=FakeCheckRunner(),
        agents=SimpleNamespace(
            worker_a_workspace="/tmp/worker_a",
            worker_b_workspace="/tmp/worker_b",
        ),
    )

    asyncio.run(
        _run_stage_checks_pair(
            flow,
            stage=stage,
            gate_tier="pre_promotion",
            round_index=1,
            stage_deadline_monotonic=None,
        )
    )

    assert marks["worker_b_start"] < marks["worker_a_end"]


def test_run_stage_checks_pair_uses_build_strategy_patterns(monkeypatch) -> None:
    monkeypatch.delenv("MULTI_CODEX_SERIALIZE_WORKER_REMOTE_CHECKS", raising=False)
    stage = StageSpec(
        name="stage-patterns",
        objective="serialize via explicit patterns",
        acceptance_criteria=["done"],
        invariants=["safe"],
        requires_remote=True,
        build_strategy=BuildStrategyProfile(
            serialize_remote_checks="auto",
            serialize_command_patterns=[r"^ninja\b"],
        ),
        gate_commands_remote_tiered=[
            TieredGateCommand(command="ninja -C build app", tier="fast_round"),
        ],
    )
    marks: dict[str, float] = {}

    class FakeCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
        ) -> AutoCheckResult:
            del stage_obj, workspace, gate_tier
            marks[f"{worker}_start"] = time.monotonic()
            await asyncio.sleep(0.02)
            marks[f"{worker}_end"] = time.monotonic()
            return AutoCheckResult(worker=worker, stage_name=stage.name)

    flow = SimpleNamespace(
        check_runner=FakeCheckRunner(),
        agents=SimpleNamespace(
            worker_a_workspace="/tmp/worker_a",
            worker_b_workspace="/tmp/worker_b",
        ),
    )

    asyncio.run(
        _run_stage_checks_pair(
            flow,
            stage=stage,
            gate_tier="fast_round",
            round_index=1,
            stage_deadline_monotonic=None,
        )
    )

    assert marks["worker_b_start"] >= marks["worker_a_end"]


def test_run_stage_checks_pair_respects_build_strategy_never(monkeypatch) -> None:
    monkeypatch.delenv("MULTI_CODEX_SERIALIZE_WORKER_REMOTE_CHECKS", raising=False)
    stage = StageSpec(
        name="stage-never-serialize",
        objective="allow explicit parallel mode",
        acceptance_criteria=["done"],
        invariants=["safe"],
        requires_remote=True,
        build_strategy=BuildStrategyProfile(serialize_remote_checks="never"),
        gate_commands_remote_tiered=[
            TieredGateCommand(command="make src.build", tier="fast_round"),
        ],
    )
    marks: dict[str, float] = {}

    class FakeCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
        ) -> AutoCheckResult:
            del stage_obj, workspace, gate_tier
            marks[f"{worker}_start"] = time.monotonic()
            await asyncio.sleep(0.04)
            marks[f"{worker}_end"] = time.monotonic()
            return AutoCheckResult(worker=worker, stage_name=stage.name)

    flow = SimpleNamespace(
        check_runner=FakeCheckRunner(),
        agents=SimpleNamespace(
            worker_a_workspace="/tmp/worker_a",
            worker_b_workspace="/tmp/worker_b",
        ),
    )

    asyncio.run(
        _run_stage_checks_pair(
            flow,
            stage=stage,
            gate_tier="fast_round",
            round_index=1,
            stage_deadline_monotonic=None,
        )
    )

    assert marks["worker_b_start"] < marks["worker_a_end"]


def test_run_stage_checks_pair_respects_build_strategy_always(monkeypatch) -> None:
    monkeypatch.delenv("MULTI_CODEX_SERIALIZE_WORKER_REMOTE_CHECKS", raising=False)
    stage = StageSpec(
        name="stage-always-serialize",
        objective="force explicit serialized mode",
        acceptance_criteria=["done"],
        invariants=["safe"],
        requires_remote=True,
        build_strategy=BuildStrategyProfile(serialize_remote_checks="always"),
        gate_commands_remote_tiered=[
            TieredGateCommand(
                command="python3 tests/smoke_suite.py --scenario all",
                tier="pre_promotion",
            ),
        ],
    )
    marks: dict[str, float] = {}

    class FakeCheckRunner:
        async def run_stage_checks(
            self,
            worker: str,
            stage_obj: StageSpec,
            workspace: str,
            *,
            gate_tier: str = "fast_round",
        ) -> AutoCheckResult:
            del stage_obj, workspace, gate_tier
            marks[f"{worker}_start"] = time.monotonic()
            await asyncio.sleep(0.03)
            marks[f"{worker}_end"] = time.monotonic()
            return AutoCheckResult(worker=worker, stage_name=stage.name)

    flow = SimpleNamespace(
        check_runner=FakeCheckRunner(),
        agents=SimpleNamespace(
            worker_a_workspace="/tmp/worker_a",
            worker_b_workspace="/tmp/worker_b",
        ),
    )

    asyncio.run(
        _run_stage_checks_pair(
            flow,
            stage=stage,
            gate_tier="pre_promotion",
            round_index=1,
            stage_deadline_monotonic=None,
        )
    )

    assert marks["worker_b_start"] >= marks["worker_a_end"]
