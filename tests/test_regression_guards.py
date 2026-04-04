from __future__ import annotations

import json
import tempfile
import typing
import asyncio
from dataclasses import dataclass
from pathlib import Path

from engine.flow import MultiCodexReviewFlow, build_flow
from adapters.structured_agents import invoke_agent_structured, invoke_agent_structured_sync
from core.models import JudgeGateReview, ReviewFlowState, StageResult, StageSpec
from core.models import FailureClassification, StageGate
from app.monitor import _collect_latest_stage_artifacts
from app.runtime_config import RuntimeConfig
from policy.governance import (
    merge_stage_gate_with_stage_spec,
    recommended_recovery_for_failure,
)
from adapters.structured_agents import detect_transient_cli_failure
from core.workspace_manager import WorkspaceManager

from .golden_utils import normalize_artifact_payload


class DummyAgents:
    def start_a2a(self) -> None:
        return None

    def shutdown_a2a(self) -> None:
        return None


@dataclass
class _DummyKickoffResult:
    raw: str
    pydantic: object | None = None


class _StructuredDummyAgent:
    role = "dummy"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    @property
    def workspace(self) -> Path:
        return Path("/tmp/dummy")

    def kickoff(
        self,
        prompt: str,
        *,
        response_format: object,
        timeout_override_sec: int | None = None,
    ) -> _DummyKickoffResult:
        self.calls.append(
            {
                "prompt": prompt,
                "response_format": response_format,
                "timeout_override_sec": timeout_override_sec,
            }
        )
        payload = {
            "stage_name": "Stage Gate",
            "objective": "obj",
            "test_commands": [],
            "lint_commands": [],
            "perf_checks": [],
            "interface_contracts": [],
            "pass_criteria": [],
            "max_round_per_stage": 2,
        }
        return _DummyKickoffResult(raw=json.dumps(payload))


class _AsyncStructuredDummyAgent(_StructuredDummyAgent):
    async def kickoff(
        self,
        prompt: str,
        *,
        response_format: object,
        timeout_override_sec: int | None = None,
    ) -> _DummyKickoffResult:
        self.calls.append(
            {
                "prompt": prompt,
                "response_format": response_format,
                "timeout_override_sec": timeout_override_sec,
                "async": True,
            }
        )
        payload = {
            "stage_name": "Stage Gate",
            "objective": "obj",
            "test_commands": [],
            "lint_commands": [],
            "perf_checks": [],
            "interface_contracts": [],
            "pass_criteria": [],
            "max_round_per_stage": 2,
        }
        return _DummyKickoffResult(raw=json.dumps(payload))


def _make_flow(target_repo: Path, runtime_dir: Path) -> MultiCodexReviewFlow:
    cfg = RuntimeConfig(
        target_repo=target_repo,
        stages_file=runtime_dir / "stages.json",
        runtime_dir=runtime_dir,
        seed_artifacts_dir=None,
        model="gpt-5",
        sandbox_mode="danger-full-access",
        enable_a2a=False,
        a2a_endpoints={},
        max_round_per_stage=3,
        output_file=runtime_dir / "summary.json",
    )
    flow = MultiCodexReviewFlow(
        cfg=cfg,
        agents=DummyAgents(),
        workspace_manager=WorkspaceManager(target_repo=target_repo, runtime_dir=runtime_dir),
    )
    flow.state.target_repo = str(target_repo)
    return flow


def test_workspace_refreshes_existing_worker_snapshot() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        runtime_dir = root / "runtime"
        repo.mkdir()
        runtime_dir.mkdir()
        (repo / "file.txt").write_text("v1", encoding="utf-8")

        workspace_manager = WorkspaceManager(target_repo=repo, runtime_dir=runtime_dir)
        workspace = workspace_manager.prepare_worker_workspace("worker_a")
        assert (workspace / "file.txt").read_text(encoding="utf-8") == "v1"

        (repo / "file.txt").write_text("v2", encoding="utf-8")
        refreshed = workspace_manager.prepare_worker_workspace("worker_a")
        assert refreshed == workspace
        assert (refreshed / "file.txt").read_text(encoding="utf-8") == "v2"


def test_stage_artifacts_use_safe_filename_and_keep_semantics() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        runtime_dir = root / "runtime"
        repo.mkdir()
        runtime_dir.mkdir()
        flow = _make_flow(repo, runtime_dir)

        stage = StageSpec(
            name="Stage X",
            objective="obj",
            acceptance_criteria=["done"],
            invariants=["safe"],
            produces_artifacts=["foo/bar"],
        )
        result = StageResult(
            stage_name=stage.name,
            passed=True,
            rounds_used=1,
            gate=JudgeGateReview(
                stage_name=stage.name,
                round_index=1,
                pass_gate=True,
                rationale="ok",
            ),
        )

        flow._persist_stage_artifacts(stage, result)
        artifact_path = runtime_dir / "artifacts" / "Stage_X_foo_bar.json"
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))

        assert artifact_path.exists()
        assert normalize_artifact_payload(payload)["artifact_name"] == "foo/bar"


def test_monitor_prefers_numeric_latest_round() -> None:
    with tempfile.TemporaryDirectory() as td:
        artifacts_dir = Path(td)
        (artifacts_dir / "stage_round9_triage_audit.json").write_text(
            json.dumps({"stage_name": "Stage Z", "round_index": 9}),
            encoding="utf-8",
        )
        (artifacts_dir / "stage_round10_triage_audit.json").write_text(
            json.dumps({"stage_name": "Stage Z", "round_index": 10}),
            encoding="utf-8",
        )

        latest = _collect_latest_stage_artifacts(artifacts_dir, "*_triage_audit.json")
        assert latest["Stage Z"]["round_index"] == 10


def test_validate_stage_outputs_supports_explicit_base_dir() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        runtime_dir = root / "runtime"
        owner_workspace = root / "worker_a"
        repo.mkdir()
        runtime_dir.mkdir()
        owner_workspace.mkdir()
        flow = _make_flow(repo, runtime_dir)

        (owner_workspace / "report.json").write_text(
            json.dumps({"status": "ok", "evidence": ["x"]}),
            encoding="utf-8",
        )

        stage = StageSpec(
            name="Stage Outputs",
            objective="obj",
            acceptance_criteria=["done"],
            invariants=["safe"],
            expected_artifact_paths=["report.json"],
        )

        repo_errors = flow._validate_stage_outputs(stage)
        owner_errors = flow._validate_stage_outputs(stage, base_dir=owner_workspace)

        assert repo_errors == ["missing expected artifact: report.json"]
        assert owner_errors == []


def test_build_flow_accepts_injected_artifact_store() -> None:
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
            max_round_per_stage=3,
            output_file=runtime_dir / "summary.json",
        )

        class _StubArtifactStore:
            def stage_path(self, stage_name: str, suffix: str) -> Path:
                return runtime_dir / "stub" / f"{stage_name}_{suffix}"

            def stage_ref(self, stage_name: str, suffix: str) -> str:
                return f"stub://{stage_name}/{suffix}"

            def write_json(self, path: Path, payload: object) -> None:
                del payload
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")

            def write_text(self, path: Path, content: str) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

        artifact_store = _StubArtifactStore()
        flow = build_flow(
            cfg=cfg,
            agents=DummyAgents(),
            workspace_manager=WorkspaceManager(target_repo=repo, runtime_dir=runtime_dir),
            stages=[],
            artifact_store=artifact_store,
        )

        assert flow.artifact_store is artifact_store


def test_build_flow_accepts_injected_runtime_ports() -> None:
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
            max_round_per_stage=3,
            output_file=runtime_dir / "summary.json",
        )

        class _StubWorkspacePort:
            def prepare_worker_workspace(self, worker: str) -> Path:
                return runtime_dir / worker

            def capture_snapshot(self, workspace: Path) -> object:
                return {"workspace": str(workspace)}

            def capture_artifacts(
                self,
                workspace: Path,
                *,
                baseline_snapshot: object | None = None,
            ) -> object:
                del baseline_snapshot
                return {
                    "workspace": str(workspace),
                    "changed_files": [],
                    "status_lines": [],
                    "patch": "",
                    "review_patch": "",
                }

            def promote_owner_workspace(self, *, owner_workspace: Path, peer_workspace: Path) -> None:
                del owner_workspace, peer_workspace

        class _StubCheckRunner:
            async def run_stage_checks(
                self,
                worker: str,
                stage: object,
                workspace: Path,
                *,
                gate_tier: str = "fast_round",
            ) -> object:
                del gate_tier
                return {"kind": "checks", "worker": worker, "workspace": str(workspace), "stage": stage}

            async def run_remote_preflight(self, worker: str, stage: object, workspace: Path) -> object:
                return {"kind": "preflight", "worker": worker, "workspace": str(workspace), "stage": stage}

        workspace_port = _StubWorkspacePort()
        check_runner = _StubCheckRunner()
        flow = build_flow(
            cfg=cfg,
            agents=DummyAgents(),
            workspace_manager=WorkspaceManager(target_repo=repo, runtime_dir=runtime_dir),
            stages=[],
            workspace_port=workspace_port,
            check_runner=check_runner,
        )

        assert flow.workspace_port is workspace_port
        assert flow.check_runner is check_runner
        assert flow.persistence_service.__class__.__name__ == "PersistenceService"


def test_worker_plan_normalizes_absolute_repo_paths() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        runtime_dir = root / "runtime"
        repo.mkdir()
        runtime_dir.mkdir()
        flow = _make_flow(repo, runtime_dir)

        plan = flow._normalize_worker_plan_payload(
            {
                "worker": "worker_a",
                "stage_name": "Stage Plan",
                "round_index": 1,
                "goal": "implement",
                "relevant_files": [str((repo / "src" / "module.py").resolve())],
                "planned_steps": [
                    {
                        "title": "edit module",
                        "files": [str((repo / "src" / "module.py").resolve())],
                        "action": "modify code",
                    }
                ],
                "verification_steps": [str((repo / "tests" / "test_module.py").resolve())],
                "risks": [],
                "assumptions": [],
                "summary": "ok",
            },
            worker="worker_a",
        )

        assert plan.relevant_files == ["src/module.py"]
        assert plan.planned_steps[0].files == ["src/module.py"]
        assert plan.verification_steps == ["tests/test_module.py"]


def test_merge_stage_gate_preserves_stage_commands_and_invariants() -> None:
    stage = StageSpec(
        name="Stage Gate",
        objective="obj",
        acceptance_criteria=["must-pass"],
        invariants=["stay-safe"],
        test_commands=["pytest -q"],
        lint_commands=["ruff check ."],
        perf_checks=["bench.sh"],
        gate_commands_remote=["remote.sh"],
        expected_artifact_paths=["report.json"],
        harness_constraints=["no-opencode"],
        trust_sources=["stage_spec"],
        non_goals=["no refactor"],
    )
    gate = StageGate(
        stage_name="bad",
        objective="bad",
        test_commands=["echo hacked"],
        lint_commands=[],
        perf_checks=[],
        interface_contracts=["custom"],
        pass_criteria=["judge-added"],
        max_round_per_stage=2,
    )

    merge_stage_gate_with_stage_spec(stage, gate)

    assert gate.test_commands == ["pytest -q"]
    assert gate.lint_commands == ["ruff check ."]
    assert gate.perf_checks == ["bench.sh"]
    assert "Invariant: stay-safe" in gate.interface_contracts
    assert "Must satisfy StageSpec hard requirements for Stage Gate" in gate.pass_criteria


def test_recommended_recovery_and_transient_detection() -> None:
    recovery = recommended_recovery_for_failure(
        FailureClassification(
            code="remote",
            category="remote_gate",
            disposition="repair_required",
            summary="x",
        )
    )
    assert recovery["default_action"] == "rerun_gate_or_fix_environment"

    raw = '{"type":"turn.failed","error":{"message":"429 Too Many Requests"}}'
    assert detect_transient_cli_failure(raw) == "429 Too Many Requests"


def test_non_cli_agent_invocation_respects_agentport_kickoff_signature() -> None:
    class _Flow:
        def _bump_metric(self, key: str) -> None:
            raise AssertionError(f"unexpected metric bump: {key}")

    agent = _StructuredDummyAgent()

    result = invoke_agent_structured_sync(
        _Flow(),
        agent,
        "return a stage gate",
        StageGate,
        timeout_override_sec=17,
    )

    assert isinstance(result, StageGate)
    assert agent.calls
    assert agent.calls[0]["response_format"] is StageGate
    assert agent.calls[0]["timeout_override_sec"] == 17


def test_flow_type_hints_do_not_require_undefined_cli_agent_symbols() -> None:
    hints = typing.get_type_hints(MultiCodexReviewFlow._invoke_opencode_agent)

    assert hints["agent"].__name__ == "AgentPort"


def test_non_cli_agent_async_kickoff_is_resolved() -> None:
    class _Flow:
        def _bump_metric(self, key: str) -> None:
            raise AssertionError(f"unexpected metric bump: {key}")

    agent = _AsyncStructuredDummyAgent()

    result = invoke_agent_structured_sync(
        _Flow(),
        agent,
        "return a stage gate",
        StageGate,
        timeout_override_sec=9,
    )

    assert isinstance(result, StageGate)
    assert agent.calls
    assert agent.calls[0]["response_format"] is StageGate
    assert agent.calls[0]["timeout_override_sec"] == 9
    assert agent.calls[0]["async"] is True


def test_invoke_agent_structured_applies_idle_timeout_hint_cap() -> None:
    class _Flow:
        _current_stage_idle_timeout_sec = 30

        @staticmethod
        def _remaining_stage_budget_sec(*, stage_name: str, stage_deadline_monotonic: float) -> int:
            del stage_name, stage_deadline_monotonic
            return 90

        @staticmethod
        def _resolve_agent_timeout_sec() -> int:
            return 120

        @staticmethod
        def _bump_metric(key: str) -> None:
            raise AssertionError(f"unexpected metric bump: {key}")

    agent = _StructuredDummyAgent()
    result = asyncio.run(
        invoke_agent_structured(
            _Flow(),
            agent,
            "return a stage gate",
            StageGate,
            stage_name="stage-x",
            stage_deadline_monotonic=9999.0,
        )
    )

    assert isinstance(result, StageGate)
    assert agent.calls
    assert agent.calls[0]["timeout_override_sec"] == 30
