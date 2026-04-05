from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from engine.flow import BlockingDecisionRequired
from core.models import (
    FailureEventArtifact,
    JudgeGateReview,
    ReviewFlowState,
    RunSummary,
    RuntimeStatusSnapshot,
    StageGate,
    StageGateDriftArtifact,
    StageDagPlan,
    StageResult,
    StageSpec,
)
from orchestrator.run_loop import run_review_inner
from orchestrator import stage_runner as stage_runner_module
from orchestrator import stage_loop as stage_loop_module


@dataclass
class FakeFlow:
    state: ReviewFlowState
    stage_result: StageResult | None = None
    failure_events: list[FailureEventArtifact] = field(default_factory=list)
    runtime_statuses: list[RuntimeStatusSnapshot] = field(default_factory=list)
    persisted_stage_artifacts: list[str] = field(default_factory=list)
    persisted_stage_specs: list[str] = field(default_factory=list)
    decision_requests: list[tuple[str, list[str]]] = field(default_factory=list)
    dag_plans: list[StageDagPlan] = field(default_factory=list)
    gc_calls: list[tuple[str, object]] = field(default_factory=list)
    policy_apply_calls: list[str] = field(default_factory=list)
    harness_metrics_persisted: int = 0
    hydrated_stages: list[str] = field(default_factory=list)
    blocking_decision: tuple[str, list[str]] | None = None
    cfg: object = field(
        default_factory=lambda: SimpleConfig(
            seed_artifacts_dir=None,
            remote_host="",
            remote_workdir="",
            remote_host_secondary="",
            remote_workdir_secondary="",
        )
    )
    agents: object = field(default_factory=lambda: SimpleAgents())

    def _persist_harness_spec_snapshot(self) -> None:
        return None

    def _persist_governance_policy_snapshot(self) -> None:
        return None

    def _persist_runtime_status(self, status: RuntimeStatusSnapshot) -> None:
        self.runtime_statuses.append(status)

    def _hydrate_stage_spec_defaults(self, stage: StageSpec) -> None:
        self.hydrated_stages.append(stage.name)

    def _validate_stage_catalog(self, stages: list[StageSpec]) -> list[str]:
        return []

    def _persist_failure_event(self, artifact: FailureEventArtifact) -> None:
        self.failure_events.append(artifact)

    def _persist_stage_dag_plan(self, plan: StageDagPlan) -> None:
        self.dag_plans.append(plan)

    def _persist_stage_spec_snapshot(self, stage: StageSpec) -> None:
        self.persisted_stage_specs.append(stage.name)

    def _validate_stage_definition(self, stage: StageSpec) -> list[str]:
        return []

    def _check_required_inputs(self, stage: StageSpec) -> list[str]:
        return []

    def _check_blocking_decisions(self, stage: StageSpec) -> list[str]:
        if self.blocking_decision and self.blocking_decision[0] == stage.name:
            return list(self.blocking_decision[1])
        return []

    def _persist_decision_request(self, stage: StageSpec, decisions: list[str]) -> None:
        self.decision_requests.append((stage.name, list(decisions)))

    def _raise_blocking_decision_required(self, stage_name: str, decisions: list[str]) -> None:
        raise BlockingDecisionRequired(stage_name=stage_name, decisions=decisions)

    async def _run_single_stage_impl(self, stage: StageSpec) -> StageResult:
        assert self.stage_result is not None
        return self.stage_result

    def _persist_stage_artifacts(self, stage: StageSpec, result: StageResult) -> None:
        self.persisted_stage_artifacts.append(stage.name)

    def _persist_harness_metrics(self) -> None:
        self.harness_metrics_persisted += 1

    async def _invoke_agent_structured(self, *args: object, **kwargs: object) -> StageGate:
        stage = kwargs.get("stage_name", "stage")
        return StageGate(
            stage_name=str(stage),
            objective=f"objective-{stage}",
            test_commands=[],
            lint_commands=[],
            perf_checks=[],
            interface_contracts=[],
            pass_criteria=[],
            max_round_per_stage=self.state.max_round_per_stage or 2,
        )

    def _bump_metric(self, key: str) -> None:
        self.state.harness_metrics[key] = self.state.harness_metrics.get(key, 0) + 1

    def _check_budget_hard_limit(self) -> bool:
        return False

    def _check_stage_budget(self, stage_name: str) -> str | None:
        return None

    def _persist_cost_ledger(self) -> None:
        return None

    def _get_cost_snapshot(self):
        return None

    def _reset_stage_cost(self, stage_name: str) -> None:
        return None

    def _gc_stage_memory(self, stage_name: str, result: object) -> None:
        self.gc_calls.append((stage_name, result))

    def _apply_stage_policy_overrides(self, stage: StageSpec) -> None:
        self.policy_apply_calls.append(stage.name)

    def _check_budget_for_agent_call(self) -> None:
        return None

    def _merge_stage_gate_with_stage_spec(self, stage: StageSpec, stage_gate: StageGate) -> None:
        stage_gate.stage_name = stage.name
        stage_gate.objective = stage.objective

    def _build_stage_gate_drift_artifact(self, **kwargs: object) -> StageGateDriftArtifact:
        stage = kwargs["stage"]
        return StageGateDriftArtifact(stage_name=stage.name)

    def _persist_stage_gate_drift_artifact(self, artifact: StageGateDriftArtifact) -> None:
        return None

    def _stage_artifact_ref(self, stage_name: str, suffix: str) -> str:
        return f"artifacts/{stage_name}_{suffix}"

    def _persist_stage_dashboard_artifact(self, artifact: object) -> None:
        return None

    def _build_stage_dashboard_artifact(self, **kwargs: object) -> object:
        return kwargs

    def _select_active_subgoal(self, **kwargs: object) -> tuple[str, str, object]:
        return "sg1", "Subgoal 1", None

    def _persist_initializer_artifacts(self, **kwargs: object) -> None:
        return None

    def _build_stage_progress_ledger(self, **kwargs: object) -> object:
        return kwargs

    def _persist_stage_progress_ledger(self, stage: StageSpec, ledger: object) -> None:
        return None

    def _persist_repo_progress_note(self, **kwargs: object) -> None:
        return None

    def _persist_stage_execution_plan(self, plan: object) -> None:
        return None

    def _resolve_stage_timeout_sec(self) -> int:
        return 60

    def _read_positive_env_int(self, key: str, default: int) -> int:
        return default

    async def _run_single_stage_impl_after_preflight(self, **kwargs: object) -> StageResult:
        assert self.stage_result is not None
        return self.stage_result


@dataclass
class SimpleConfig:
    seed_artifacts_dir: object
    remote_host: str
    remote_workdir: str
    remote_host_secondary: str
    remote_workdir_secondary: str


@dataclass
class SimpleAgents:
    worker_a_workspace: str = "/tmp/worker_a"
    worker_b_workspace: str = "/tmp/worker_b"


def _stage(name: str, *, produces_artifacts: list[str] | None = None) -> StageSpec:
    return StageSpec(
        name=name,
        objective=f"objective-{name}",
        acceptance_criteria=[f"accept-{name}"],
        invariants=[f"invariant-{name}"],
        produces_artifacts=produces_artifacts or [],
    )


def _result(stage_name: str, passed: bool = True) -> StageResult:
    return StageResult(
        stage_name=stage_name,
        passed=passed,
        rounds_used=1,
        gate=JudgeGateReview(
            stage_name=stage_name,
            round_index=1,
            pass_gate=passed,
            rationale="ok" if passed else "failed",
        ),
    )


def test_run_loop_raises_when_no_stages() -> None:
    flow = FakeFlow(state=ReviewFlowState(target_repo="/tmp/repo"))
    with pytest.raises(ValueError, match="No stages provided"):
        asyncio.run(run_review_inner(flow))


def test_run_loop_persists_summary_and_stage_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    stage = _stage("stage-a", produces_artifacts=["artifact-a"])
    flow = FakeFlow(
        state=ReviewFlowState(target_repo="/tmp/repo", stages=[stage]),
        stage_result=_result("stage-a", True),
    )
    async def _no_preflight(flow_obj: object, stage_obj: StageSpec) -> StageResult | None:
        return None
    monkeypatch.setattr(stage_runner_module, "run_remote_preflight", _no_preflight)

    summary = asyncio.run(run_review_inner(flow))

    assert isinstance(summary, RunSummary)
    assert summary.overall_passed is True
    assert flow.persisted_stage_specs == ["stage-a"]
    assert flow.persisted_stage_artifacts == ["stage-a"]
    assert flow.harness_metrics_persisted == 1
    assert flow.runtime_statuses[-1].overall_state == "passed"
    # GC must be called once per executed stage, with the stage name and result.
    assert len(flow.gc_calls) == 1
    gc_stage_name, gc_result = flow.gc_calls[0]
    assert gc_stage_name == "stage-a"
    assert isinstance(gc_result, StageResult)
    assert gc_result.stage_name == "stage-a"


def test_run_loop_skips_stages_marked_as_resumed_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    stage_a = _stage("stage-a", produces_artifacts=["artifact-a"])
    stage_b = _stage("stage-b", produces_artifacts=["artifact-b"])
    flow = FakeFlow(
        state=ReviewFlowState(
            target_repo="/tmp/repo",
            stages=[stage_a, stage_b],
            resumed_stage_results=[_result("stage-a", True)],
            resume_passed_stage_names=["stage-a"],
        ),
        stage_result=_result("stage-b", True),
    )

    async def _no_preflight(flow_obj: object, stage_obj: StageSpec) -> StageResult | None:
        return None

    monkeypatch.setattr(stage_runner_module, "run_remote_preflight", _no_preflight)
    summary = asyncio.run(run_review_inner(flow))

    assert [item.stage_name for item in summary.stage_results] == ["stage-a", "stage-b"]
    assert flow.persisted_stage_specs == ["stage-a", "stage-b"]
    assert flow.persisted_stage_artifacts == ["stage-b"]
    # Stage policy override hook should still run for every scheduled stage.
    assert flow.policy_apply_calls == ["stage-a", "stage-b"]
    # GC should only be called for actually-executed stages (stage-b), not resumed ones.
    assert len(flow.gc_calls) == 1
    assert flow.gc_calls[0][0] == "stage-b"


def test_run_loop_blocks_on_unapproved_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    stage = _stage("stage-a")
    flow = FakeFlow(
        state=ReviewFlowState(target_repo="/tmp/repo", stages=[stage]),
        stage_result=_result("stage-a", True),
        blocking_decision=("stage-a", ["need_human_ok"]),
    )
    async def _no_preflight(flow_obj: object, stage_obj: StageSpec) -> StageResult | None:
        return None
    monkeypatch.setattr(stage_runner_module, "run_remote_preflight", _no_preflight)

    with pytest.raises(BlockingDecisionRequired):
        asyncio.run(run_review_inner(flow))

    assert flow.decision_requests == [("stage-a", ["need_human_ok"])]
    assert flow.failure_events[-1].classification.code == "blocking_decision_required"


def test_stage_loop_fails_closed_when_all_rounds_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    stage = _stage("stage-a")
    stage_plan = object()
    state = ReviewFlowState(target_repo="/tmp/repo", max_round_per_stage=2)

    class LoopFlow:
        def __init__(self) -> None:
            self.state = state
            self.agents = SimpleAgents()
            self.workspace_port = self
            self._current_stage_gate = None
            self.finalize_calls: list[tuple[JudgeGateReview, list[object]]] = []

        def capture_snapshot(self, workspace: str) -> str:
            return f"snapshot:{workspace}"

    flow = LoopFlow()

    async def _reject(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return stage_loop_module.RoundPlanRejected(judge_feedback=["fix plan output", "narrow scope"])

    def _finalize_failed_stage(
        flow_obj: object,
        *,
        stage: StageSpec,
        max_round: int,
        final_gate: JudgeGateReview,
        round_logs: list[object],
    ) -> StageResult:
        assert flow_obj is flow
        flow.finalize_calls.append((final_gate, round_logs))
        return StageResult(stage_name=stage.name, passed=False, rounds_used=max_round, gate=final_gate, round_logs=[])

    monkeypatch.setattr(stage_loop_module, "run_round_plan_phase", _reject)
    monkeypatch.setattr(stage_loop_module, "finalize_failed_stage", _finalize_failed_stage)

    result = asyncio.run(
        stage_loop_module.run_stage_round_loop(
            flow,
            stage=stage,
            stage_plan=stage_plan,
            stage_deadline_monotonic=0.0,
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
    )

    assert result.passed is False
    assert flow.finalize_calls
    final_gate = flow.finalize_calls[0][0]
    assert final_gate.pass_gate is False
    assert final_gate.round_index == 2
    assert "plan_gate" in final_gate.rationale
    assert final_gate.required_actions == ["fix plan output", "narrow scope"]


def test_run_loop_stage_budget_exceeded_blocks_subsequent_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """When per-stage budget is exceeded after a stage runs, the loop should
    break and record a failure event with a valid FailureClassification."""
    stage_a = _stage("stage-a", produces_artifacts=["artifact-a"])
    stage_b = _stage("stage-b")

    flow = FakeFlow(
        state=ReviewFlowState(target_repo="/tmp/repo", stages=[stage_a, stage_b]),
        stage_result=_result("stage-a", True),
    )

    # Make _check_stage_budget return a reason after stage-a executes.
    original_check = flow._check_stage_budget

    def _fake_check_stage_budget(stage_name: str) -> str | None:
        if stage_name == "stage-a":
            return "Stage 'stage-a' exceeded per-stage budget ($0.50 > $0.10)"
        return original_check(stage_name)

    flow._check_stage_budget = _fake_check_stage_budget  # type: ignore[assignment]

    async def _no_preflight(flow_obj: object, stage_obj: StageSpec) -> StageResult | None:
        return None

    monkeypatch.setattr(stage_runner_module, "run_remote_preflight", _no_preflight)

    summary = asyncio.run(run_review_inner(flow))

    assert summary.overall_passed is False
    # Only stage-a should have been executed.
    assert len(summary.stage_results) == 1
    assert summary.stage_results[0].stage_name == "stage-a"
    assert summary.stage_results[0].passed is True

    # A budget_policy failure event should have been recorded.
    budget_failures = [f for f in flow.failure_events if f.source == "budget_policy"]
    assert len(budget_failures) == 1
    failure = budget_failures[0]
    assert failure.stage_name == "stage-a"
    assert failure.classification.code == "stage_budget_exceeded"
    assert failure.classification.category == "input_contract"
    assert failure.classification.disposition == "blocked"


def test_run_loop_hard_budget_blocks_all_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the hard budget limit is triggered, no stages should execute."""
    stage_a = _stage("stage-a")

    flow = FakeFlow(
        state=ReviewFlowState(target_repo="/tmp/repo", stages=[stage_a]),
        stage_result=_result("stage-a", True),
    )

    # Make _check_budget_hard_limit return True.
    flow._check_budget_hard_limit = lambda: True  # type: ignore[assignment]

    async def _no_preflight(flow_obj: object, stage_obj: StageSpec) -> StageResult | None:
        return None

    monkeypatch.setattr(stage_runner_module, "run_remote_preflight", _no_preflight)

    summary = asyncio.run(run_review_inner(flow))

    assert summary.overall_passed is False
    # Stage should appear as failed (not executed).
    assert len(summary.stage_results) == 1
    assert summary.stage_results[0].passed is False
    assert summary.stage_results[0].rounds_used == 0

    # A budget_policy failure event should have been recorded.
    budget_failures = [f for f in flow.failure_events if f.source == "budget_policy"]
    assert len(budget_failures) == 1
    failure = budget_failures[0]
    assert failure.classification.code == "budget_hard_limit_exceeded"
    assert failure.classification.category == "input_contract"
    assert failure.classification.disposition == "blocked"
