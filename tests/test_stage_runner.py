from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from core.models import (
    ActiveConstraintsArtifact,
    FailureEventArtifact,
    FeatureChecklistArtifact,
    InitializerReportArtifact,
    JudgeGateReview,
    ReviewFlowState,
    RuntimeStatusSnapshot,
    StageExecutionPlan,
    StageGate,
    StageGateDriftArtifact,
    StageResult,
    StageSpec,
    TaskHandoffPacket,
)
from orchestrator import stage_runner as stage_runner_module


@dataclass
class _PreflightResult:
    passed: bool
    command: str
    stdout: str = ""
    stderr: str = ""

    def model_dump(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "command": self.command,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


@dataclass
class _CheckRunner:
    worker_a_results: list[_PreflightResult] = field(default_factory=list)
    worker_b_results: list[_PreflightResult] = field(default_factory=list)

    async def run_remote_preflight(self, worker: str, stage: object, workspace: object) -> list[_PreflightResult]:
        del stage, workspace
        return self.worker_a_results if worker == "worker_a" else self.worker_b_results


@dataclass
class _Agents:
    judge: object = object()
    worker_a_workspace: str = "/tmp/worker_a"
    worker_b_workspace: str = "/tmp/worker_b"


@dataclass
class _Config:
    seed_artifacts_dir: object = None


@dataclass
class StageRunnerFlow:
    state: ReviewFlowState = field(default_factory=lambda: ReviewFlowState(target_repo="/tmp/repo", max_round_per_stage=3))
    cfg: object = field(default_factory=_Config)
    agents: _Agents = field(default_factory=_Agents)
    check_runner: _CheckRunner = field(default_factory=_CheckRunner)
    failure_events: list[FailureEventArtifact] = field(default_factory=list)
    task_handoffs: list[TaskHandoffPacket] = field(default_factory=list)
    runtime_statuses: list[RuntimeStatusSnapshot] = field(default_factory=list)
    dashboards: list[object] = field(default_factory=list)
    preflight_records: list[tuple[str, str, list[_PreflightResult]]] = field(default_factory=list)
    metrics: dict[str, int] = field(default_factory=dict)
    stage_execution_plans: list[object] = field(default_factory=list)
    ledgers: list[tuple[str, object]] = field(default_factory=list)
    repo_notes: list[dict[str, object]] = field(default_factory=list)
    initializer_payloads: list[dict[str, object]] = field(default_factory=list)
    after_preflight_calls: list[dict[str, object]] = field(default_factory=list)

    async def _invoke_agent_structured(self, *args: object, **kwargs: object) -> StageGate:
        del args, kwargs
        raise RuntimeError("judge unavailable")

    def _bump_metric(self, key: str) -> None:
        self.metrics[key] = self.metrics.get(key, 0) + 1

    def _merge_stage_gate_with_stage_spec(self, stage: StageSpec, stage_gate: StageGate) -> None:
        stage_gate.stage_name = stage.name
        stage_gate.objective = stage.objective

    def _build_stage_gate_drift_artifact(self, **kwargs: object) -> StageGateDriftArtifact:
        stage = kwargs["stage"]
        return StageGateDriftArtifact(stage_name=stage.name)

    def _persist_stage_gate_drift_artifact(self, artifact: StageGateDriftArtifact) -> None:
        del artifact

    def _persist_failure_event(self, artifact: FailureEventArtifact) -> None:
        self.failure_events.append(artifact)

    def _summarize_check_failure(self, stdout: str, stderr: str) -> str:
        return (stderr or stdout).strip() or "unknown"

    def _persist_remote_preflight_results(
        self,
        stage: StageSpec,
        worker: str,
        results: list[_PreflightResult],
    ) -> str:
        self.preflight_records.append((stage.name, worker, results))
        return f"artifacts/{stage.name}_{worker}_remote_preflight.json"

    def _build_terminal_handoff_packet(
        self,
        *,
        worker: str,
        stage: StageSpec,
        round_index: int,
        final_gate: JudgeGateReview,
        trigger: str = "stage_fail",
    ) -> TaskHandoffPacket:
        return TaskHandoffPacket(
            stage_name=stage.name,
            round_index=round_index,
            worker=worker,
            trigger=trigger,
            objective=stage.objective,
            completed_facts=[],
            current_status=[final_gate.rationale],
            changed_files=[],
            evidence_artifacts=[],
            open_blockers=list(final_gate.required_actions),
            immutable_requirements=[],
            related_report_ids=[],
        )

    def _persist_task_handoff_packet(self, packet: TaskHandoffPacket) -> None:
        self.task_handoffs.append(packet)

    def _persist_runtime_status(self, snapshot: RuntimeStatusSnapshot) -> None:
        self.runtime_statuses.append(snapshot)

    def _build_stage_dashboard_artifact(self, **kwargs: object) -> object:
        return kwargs

    def _persist_stage_dashboard_artifact(self, artifact: object) -> None:
        self.dashboards.append(artifact)

    def _stage_artifact_ref(self, stage_name: str, suffix: str) -> str:
        return f"artifacts/{stage_name}_{suffix}"

    def _select_active_subgoal(self, **kwargs: object) -> tuple[str, str, object]:
        del kwargs
        return "sg1", "Subgoal 1", None

    def _persist_initializer_artifacts(
        self,
        *,
        stage: StageSpec,
        report: InitializerReportArtifact,
        constraints: ActiveConstraintsArtifact,
        checklist: FeatureChecklistArtifact,
    ) -> None:
        self.initializer_payloads.append(
            {
                "stage": stage.name,
                "report": report,
                "constraints": constraints,
                "checklist": checklist,
            }
        )

    def _build_stage_progress_ledger(self, **kwargs: object) -> object:
        return kwargs

    def _persist_stage_progress_ledger(self, stage: StageSpec, ledger: object) -> None:
        self.ledgers.append((stage.name, ledger))

    def _persist_repo_progress_note(self, **kwargs: object) -> None:
        self.repo_notes.append(dict(kwargs))

    def _persist_stage_execution_plan(self, plan: object) -> None:
        self.stage_execution_plans.append(plan)

    def _resolve_stage_timeout_sec(self) -> int:
        return 60

    def _read_positive_env_int(self, key: str, default: int) -> int:
        del key
        return default

    async def _run_single_stage_impl_after_preflight(self, **kwargs: object) -> StageResult:
        self.after_preflight_calls.append(dict(kwargs))
        return StageResult(
            stage_name=str(kwargs["stage"].name),
            passed=True,
            rounds_used=0,
            gate=JudgeGateReview(
                stage_name=str(kwargs["stage"].name),
                round_index=0,
                pass_gate=True,
                rationale="after_preflight",
            ),
            round_logs=[],
        )


def _stage(name: str = "stage-a") -> StageSpec:
    return StageSpec(
        name=name,
        objective=f"objective-{name}",
        acceptance_criteria=["done"],
        invariants=["safe"],
        expected_artifact_paths=["report.json"],
        harness_constraints=["no-opencode"],
        trust_sources=["stage_spec"],
    )


def test_prepare_stage_gate_falls_back_when_judge_fails() -> None:
    flow = StageRunnerFlow()
    stage = _stage()

    gate, max_round = asyncio.run(
        stage_runner_module.prepare_stage_gate(
            flow,
            stage=stage,
            stage_deadline_monotonic=1e9,
        )
    )

    assert gate.stage_name == stage.name
    assert gate.objective == stage.objective
    assert gate.test_commands == list(stage.test_commands)
    assert any("Expected evidence artifact: report.json" == item for item in gate.interface_contracts)
    assert any("Invariant: safe" == item for item in gate.interface_contracts)
    assert any("Trusted input/source: stage_spec" == item for item in gate.interface_contracts)
    assert max_round == flow.state.max_round_per_stage
    assert flow.metrics["judge_stage_gate_fallback_count"] == 1
    assert flow.failure_events[-1].classification.code == "judge_stage_gate_fallback"


def test_run_remote_preflight_fails_closed_and_persists_handoffs() -> None:
    flow = StageRunnerFlow(
        check_runner=_CheckRunner(
            worker_a_results=[_PreflightResult(passed=False, command="ssh a", stderr="connection refused")],
            worker_b_results=[_PreflightResult(passed=True, command="ssh b")],
        )
    )
    stage = _stage()

    result = asyncio.run(stage_runner_module.run_remote_preflight(flow, stage))

    assert isinstance(result, StageResult)
    assert result.passed is False
    assert result.gate.pass_gate is False
    assert result.gate.round_index == 0
    assert len(result.gate.required_actions) == 1
    assert "worker_a" in result.gate.required_actions[0]
    assert len(flow.task_handoffs) == 2
    assert {packet.worker for packet in flow.task_handoffs} == {"worker_a", "worker_b"}
    assert flow.runtime_statuses[-1].overall_state == "blocked"
    assert flow.failure_events[-1].classification.code == "remote_preflight_failed"


def test_prepare_stage_gate_raises_when_gate_drift_has_policy_blockers() -> None:
    class DriftBlockedFlow(StageRunnerFlow):
        def _build_stage_gate_drift_artifact(self, **kwargs: object) -> StageGateDriftArtifact:
            stage = kwargs["stage"]
            return StageGateDriftArtifact(
                stage_name=stage.name,
                policy_blockers=["judge-added command outside contract"],
            )

    flow = DriftBlockedFlow()
    stage = _stage()

    try:
        asyncio.run(
            stage_runner_module.prepare_stage_gate(
                flow,
                stage=stage,
                stage_deadline_monotonic=1e9,
            )
        )
    except ValueError as exc:
        assert "gate drift violated policy" in str(exc)
    else:
        raise AssertionError("expected ValueError for policy-blocked gate drift")


def test_initialize_stage_fails_when_execution_plan_invalid(monkeypatch) -> None:
    flow = StageRunnerFlow()
    stage = _stage()

    def _invalid_plan(stage_obj: StageSpec) -> StageExecutionPlan:
        return StageExecutionPlan(
            stage_name=stage_obj.name,
            objective=stage_obj.objective,
            nodes=[],
            validation_errors=["missing dependency edge"],
        )

    monkeypatch.setattr(stage_runner_module, "build_stage_execution_plan", _invalid_plan)

    try:
        asyncio.run(stage_runner_module.initialize_stage(flow, stage))
    except ValueError as exc:
        assert "execution plan failed validation" in str(exc)
    else:
        raise AssertionError("expected ValueError for invalid execution plan")

    assert flow.stage_execution_plans
    assert flow.failure_events[-1].classification.code == "stage_execution_plan_invalid"


def test_initialize_stage_sets_stage_idle_timeout_hint() -> None:
    flow = StageRunnerFlow()
    stage = _stage()

    bootstrap = asyncio.run(stage_runner_module.initialize_stage(flow, stage))

    assert bootstrap.idle_timeout_sec == 600
    assert getattr(flow, "_current_stage_idle_timeout_sec", None) == 600


def test_finalize_failed_stage_persists_terminal_state() -> None:
    flow = StageRunnerFlow()
    stage = _stage()
    gate = JudgeGateReview(
        stage_name=stage.name,
        round_index=2,
        pass_gate=False,
        rationale="blocked by judge",
        required_actions=["fix failing check"],
    )

    result = stage_runner_module.finalize_failed_stage(
        flow,
        stage=stage,
        max_round=2,
        final_gate=gate,
        round_logs=[],
    )

    assert result.passed is False
    assert result.rounds_used == 2
    assert len(flow.task_handoffs) == 2
    assert flow.runtime_statuses[-1].phase == "stage_failed"
    assert flow.runtime_statuses[-1].overall_state == "failed"
    assert flow.ledgers[-1][1]["status"] == "failed"
    assert flow.repo_notes[-1]["verified_facts"] == ["Stage failed closed."]


def test_run_single_stage_short_circuits_on_preflight_failure(monkeypatch) -> None:
    flow = StageRunnerFlow()
    stage = _stage()
    blocked = StageResult(
        stage_name=stage.name,
        passed=False,
        rounds_used=0,
        gate=JudgeGateReview(
            stage_name=stage.name,
            round_index=0,
            pass_gate=False,
            rationale="blocked",
        ),
        round_logs=[],
    )

    async def _bootstrap(flow_obj: object, stage_obj: StageSpec):
        del flow_obj, stage_obj
        return stage_runner_module.StageBootstrap(
            stage_plan=StageExecutionPlan(stage_name=stage.name, objective=stage.objective),
            stage_deadline_monotonic=123.0,
            idle_timeout_sec=600,
            stage_gate=StageGate(
                stage_name=stage.name,
                objective=stage.objective,
                test_commands=[],
                lint_commands=[],
                perf_checks=[],
                interface_contracts=[],
                pass_criteria=[],
                max_round_per_stage=2,
            ),
            max_round=2,
        )

    async def _preflight(flow_obj: object, stage_obj: StageSpec):
        del flow_obj, stage_obj
        return blocked

    monkeypatch.setattr(stage_runner_module, "initialize_stage", _bootstrap)
    monkeypatch.setattr(stage_runner_module, "run_remote_preflight", _preflight)

    result = asyncio.run(stage_runner_module.run_single_stage(flow, stage))

    assert result is blocked
    assert flow.after_preflight_calls == []


def test_run_single_stage_calls_after_preflight_with_bootstrap(monkeypatch) -> None:
    flow = StageRunnerFlow()
    stage = _stage()
    bootstrap = stage_runner_module.StageBootstrap(
        stage_plan=StageExecutionPlan(stage_name=stage.name, objective=stage.objective),
        stage_deadline_monotonic=456.0,
        idle_timeout_sec=300,
        stage_gate=StageGate(
            stage_name=stage.name,
            objective=stage.objective,
            test_commands=[],
            lint_commands=[],
            perf_checks=[],
            interface_contracts=[],
            pass_criteria=[],
            max_round_per_stage=2,
        ),
        max_round=2,
    )

    async def _bootstrap(flow_obj: object, stage_obj: StageSpec):
        del flow_obj, stage_obj
        return bootstrap

    async def _preflight(flow_obj: object, stage_obj: StageSpec):
        del flow_obj, stage_obj
        return None

    monkeypatch.setattr(stage_runner_module, "initialize_stage", _bootstrap)
    monkeypatch.setattr(stage_runner_module, "run_remote_preflight", _preflight)

    result = asyncio.run(stage_runner_module.run_single_stage(flow, stage))

    assert result.passed is True
    assert len(flow.after_preflight_calls) == 1
    call = flow.after_preflight_calls[0]
    assert call["stage"] is stage
    assert call["stage_plan"] == bootstrap.stage_plan
    assert call["stage_deadline_monotonic"] == 456.0
    assert call["idle_timeout_sec"] == 300
    assert call["stage_gate"] == bootstrap.stage_gate
    assert call["max_round"] == 2


def test_run_single_stage_cancels_preflight_when_initialize_fails(monkeypatch) -> None:
    flow = StageRunnerFlow()
    stage = _stage()
    preflight_cancelled = asyncio.Event()

    async def _bootstrap(flow_obj: object, stage_obj: StageSpec):
        del flow_obj, stage_obj
        raise RuntimeError("bootstrap failed")

    async def _preflight(flow_obj: object, stage_obj: StageSpec):
        del flow_obj, stage_obj
        try:
            await asyncio.sleep(1)
            raise AssertionError("preflight should have been cancelled")
        except asyncio.CancelledError:
            preflight_cancelled.set()
            raise

    monkeypatch.setattr(stage_runner_module, "initialize_stage", _bootstrap)
    monkeypatch.setattr(stage_runner_module, "run_remote_preflight", _preflight)

    with pytest.raises(RuntimeError, match="bootstrap failed"):
        asyncio.run(stage_runner_module.run_single_stage(flow, stage))

    assert preflight_cancelled.is_set()


def test_run_single_stage_cancels_initialize_when_preflight_returns_blocked(monkeypatch) -> None:
    flow = StageRunnerFlow()
    stage = _stage()
    initialize_cancelled = asyncio.Event()
    blocked = StageResult(
        stage_name=stage.name,
        passed=False,
        rounds_used=0,
        gate=JudgeGateReview(
            stage_name=stage.name,
            round_index=0,
            pass_gate=False,
            rationale="blocked",
        ),
        round_logs=[],
    )

    async def _bootstrap(flow_obj: object, stage_obj: StageSpec):
        del flow_obj, stage_obj
        try:
            await asyncio.sleep(1)
            raise AssertionError("initialize should have been cancelled")
        except asyncio.CancelledError:
            initialize_cancelled.set()
            raise

    async def _preflight(flow_obj: object, stage_obj: StageSpec):
        del flow_obj, stage_obj
        return blocked

    monkeypatch.setattr(stage_runner_module, "initialize_stage", _bootstrap)
    monkeypatch.setattr(stage_runner_module, "run_remote_preflight", _preflight)

    result = asyncio.run(stage_runner_module.run_single_stage(flow, stage))

    assert result is blocked
    assert initialize_cancelled.is_set()


def test_run_single_stage_prefers_initialize_exception_over_same_tick_preflight_block(monkeypatch) -> None:
    flow = StageRunnerFlow()
    stage = _stage()
    blocked = StageResult(
        stage_name=stage.name,
        passed=False,
        rounds_used=0,
        gate=JudgeGateReview(
            stage_name=stage.name,
            round_index=0,
            pass_gate=False,
            rationale="blocked",
        ),
        round_logs=[],
    )

    async def _bootstrap(flow_obj: object, stage_obj: StageSpec):
        del flow_obj, stage_obj
        await asyncio.sleep(0)
        raise RuntimeError("bootstrap failed after same-tick completion")

    async def _preflight(flow_obj: object, stage_obj: StageSpec):
        del flow_obj, stage_obj
        await asyncio.sleep(0)
        return blocked

    monkeypatch.setattr(stage_runner_module, "initialize_stage", _bootstrap)
    monkeypatch.setattr(stage_runner_module, "run_remote_preflight", _preflight)

    with pytest.raises(RuntimeError, match="bootstrap failed after same-tick completion"):
        asyncio.run(stage_runner_module.run_single_stage(flow, stage))
