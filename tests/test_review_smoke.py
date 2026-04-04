from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from engine.flow import build_flow
from core.models import JudgeGateReview, StageResult, StageSpec
from orchestrator import run_loop as run_loop_module
from app.runtime_config import RuntimeConfig
from engine.flow import BlockingDecisionRequired
from core.workspace_manager import WorkspaceManager

from .golden_utils import normalize_artifact_payload


class _DummyAgents:
    def start_a2a(self) -> None:
        return None

    def shutdown_a2a(self) -> None:
        return None


def test_run_review_smoke_persists_core_artifacts(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        runtime_dir = root / "runtime"
        repo.mkdir()
        runtime_dir.mkdir()

        cfg = RuntimeConfig(
            target_repo=repo,
            stages_file=runtime_dir / "stages.json",
            runtime_dir=runtime_dir,
            seed_artifacts_dir=None,
            model="gpt-5",
            sandbox_mode="danger-full-access",
            enable_a2a=False,
            a2a_endpoints={},
            max_round_per_stage=2,
            output_file=runtime_dir / "summary.json",
        )
        stage = StageSpec(
            name="Stage A",
            stage_id="stage.a",
            objective="do the thing",
            acceptance_criteria=["done"],
            invariants=["safe"],
            produces_artifacts=["report"],
        )

        async def _fake_run_single_stage(flow: object, current_stage: StageSpec) -> StageResult:
            del flow
            return StageResult(
                stage_name=current_stage.name,
                passed=True,
                rounds_used=1,
                gate=JudgeGateReview(
                    stage_name=current_stage.name,
                    round_index=1,
                    pass_gate=True,
                    rationale="ok",
                ),
                round_logs=[],
            )

        monkeypatch.setattr(run_loop_module, "run_single_stage", _fake_run_single_stage)

        flow = build_flow(
            cfg=cfg,
            agents=_DummyAgents(),
            workspace_manager=WorkspaceManager(target_repo=repo, runtime_dir=runtime_dir),
            stages=[stage],
        )

        summary = asyncio.run(run_loop_module.run_review_inner(flow))

        assert summary.overall_passed is True
        assert [item.stage_name for item in summary.stage_results] == ["Stage A"]

        artifacts_dir = runtime_dir / "artifacts"
        assert (artifacts_dir / "harness_harness_spec.json").exists()
        assert (artifacts_dir / "governance_governance_policy.json").exists()
        assert (artifacts_dir / "runtime_runtime_status.json").exists()
        assert (artifacts_dir / "stage_dag_plan.json").exists()
        assert (artifacts_dir / "Stage_A_stage_spec_snapshot.json").exists()
        assert (artifacts_dir / "Stage_A_report.json").exists()

        runtime_payload = normalize_artifact_payload(
            json.loads((artifacts_dir / "runtime_runtime_status.json").read_text(encoding="utf-8"))
        )
        harness_payload = normalize_artifact_payload(
            json.loads((artifacts_dir / "harness_harness_spec.json").read_text(encoding="utf-8"))
        )
        dag_payload = normalize_artifact_payload(
            json.loads((artifacts_dir / "stage_dag_plan.json").read_text(encoding="utf-8"))
        )
        stage_spec_payload = normalize_artifact_payload(
            json.loads((artifacts_dir / "Stage_A_stage_spec_snapshot.json").read_text(encoding="utf-8"))
        )
        report_payload = normalize_artifact_payload(
            json.loads((artifacts_dir / "Stage_A_report.json").read_text(encoding="utf-8"))
        )

        assert runtime_payload["overall_state"] == "passed"
        assert runtime_payload["judge_state"] == "done"
        assert "artifacts/harness_metrics.json" in runtime_payload["latest_artifacts"]
        assert harness_payload["provider"] == "codex"
        assert "judge + verifier + worker_a + worker_b" == harness_payload["topology"]
        assert dag_payload["serial_execution_order"] == ["Stage A"]
        assert stage_spec_payload["stage_id"] == "stage.a"
        assert stage_spec_payload["objective"] == "do the thing"
        assert report_payload["stage_name"] == "Stage A"
        assert report_payload["artifact_name"] == "report"
        assert report_payload["data"]["stage_passed"] is True


def test_run_review_smoke_persists_blocking_decision_artifacts() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        runtime_dir = root / "runtime"
        repo.mkdir()
        runtime_dir.mkdir()

        cfg = RuntimeConfig(
            target_repo=repo,
            stages_file=runtime_dir / "stages.json",
            runtime_dir=runtime_dir,
            seed_artifacts_dir=None,
            model="gpt-5",
            sandbox_mode="danger-full-access",
            enable_a2a=False,
            a2a_endpoints={},
            max_round_per_stage=2,
            output_file=runtime_dir / "summary.json",
        )
        stage = StageSpec(
            name="Stage B",
            stage_id="stage.b",
            objective="needs approval",
            acceptance_criteria=["done"],
            invariants=["safe"],
            blocking_decisions=["need_human_ok"],
        )

        flow = build_flow(
            cfg=cfg,
            agents=_DummyAgents(),
            workspace_manager=WorkspaceManager(target_repo=repo, runtime_dir=runtime_dir),
            stages=[stage],
        )
        with pytest.raises(BlockingDecisionRequired):
            asyncio.run(run_loop_module.run_review_inner(flow))

        artifacts_dir = runtime_dir / "artifacts"
        decision_payload = normalize_artifact_payload(
            json.loads((artifacts_dir / "Stage_B_decision_request.json").read_text(encoding="utf-8"))
        )
        failure_payload = normalize_artifact_payload(
            json.loads((artifacts_dir / "Stage_B_round0_blocking_decision_failure_event.json").read_text(encoding="utf-8"))
        )

        assert decision_payload["blocking_decisions"] == ["need_human_ok"]
        assert decision_payload["status"] == "awaiting_approval"
        assert failure_payload["classification"]["code"] == "blocking_decision_required"
        assert failure_payload["classification"]["category"] == "human_decision"


def test_run_review_smoke_persists_missing_input_failure_artifact() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        runtime_dir = root / "runtime"
        repo.mkdir()
        runtime_dir.mkdir()

        cfg = RuntimeConfig(
            target_repo=repo,
            stages_file=runtime_dir / "stages.json",
            runtime_dir=runtime_dir,
            seed_artifacts_dir=None,
            model="gpt-5",
            sandbox_mode="danger-full-access",
            enable_a2a=False,
            a2a_endpoints={},
            max_round_per_stage=2,
            output_file=runtime_dir / "summary.json",
        )
        stage = StageSpec(
            name="Stage C",
            stage_id="stage.c",
            objective="needs artifacts",
            acceptance_criteria=["done"],
            invariants=["safe"],
            required_inputs=["missing_artifact"],
        )

        flow = build_flow(
            cfg=cfg,
            agents=_DummyAgents(),
            workspace_manager=WorkspaceManager(target_repo=repo, runtime_dir=runtime_dir),
            stages=[stage],
        )

        with pytest.raises(ValueError, match="required_inputs remain unsatisfied"):
            asyncio.run(run_loop_module.run_review_inner(flow))

        artifacts_dir = runtime_dir / "artifacts"
        dag_payload = normalize_artifact_payload(
            json.loads((artifacts_dir / "stage_dag_plan.json").read_text(encoding="utf-8"))
        )

        assert dag_payload["validation_errors"]
        assert any(
            "required_inputs remain unsatisfied" in item
            for item in dag_payload["validation_errors"]
        )
