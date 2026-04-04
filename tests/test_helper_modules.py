from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace

import importlib.util

from adapters.workspace_ops import owner_workspace_path
from core.models import ReviewFlowState, StageSpec
from state.progress_helpers import (
    artifact_slug,
    latest_stage_progress_ledger,
    repo_progress_path,
    select_active_subgoal,
)

from .test_regression_guards import _make_flow


def test_progress_helpers_slug_and_progress_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        runtime = root / "runtime"
        repo.mkdir()
        runtime.mkdir()
        flow = _make_flow(repo, runtime)
        stage = StageSpec(
            name="Stage A/B",
            stage_id="stage.a",
            objective="obj",
            acceptance_criteria=["done"],
            invariants=["safe"],
        )

        assert artifact_slug("foo/bar baz") == "foo_bar_baz"
        assert repo_progress_path(flow, stage, "ledger.json").name == "stage_a_ledger.json"


def test_progress_helpers_select_subgoal_and_owner_workspace() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        runtime = root / "runtime"
        repo.mkdir()
        runtime.mkdir()
        flow = SimpleNamespace(
            state=ReviewFlowState(target_repo=str(repo)),
            cfg=SimpleNamespace(owner_worker="worker_a"),
            agents=SimpleNamespace(
                worker_a_workspace=root / "worker_a",
                worker_b_workspace=root / "worker_b",
            ),
        )
        stage = StageSpec(
            name="Stage S",
            objective="obj",
            acceptance_criteria=["done"],
            invariants=["safe"],
            subgoals=[
                {"subgoal_id": "sg1", "title": "One", "description": "d1"},
                {"subgoal_id": "sg2", "title": "Two", "description": "d2"},
            ],
        )
        subgoal_id, title, description = select_active_subgoal(flow, stage=stage, round_index=2)
        assert (subgoal_id, title, description) == ("sg2", "Two", "d2")
        assert latest_stage_progress_ledger(flow, "missing") is None
        assert owner_workspace_path(flow).name == "worker_a"


def test_orchestrator_modules_do_not_import_concrete_adapter_implementations() -> None:
    orchestrator_dir = Path(__file__).resolve().parents[1] / "src" / "orchestrator"
    forbidden_fragments = (
        "from adapters.",
        "from core.workspace_manager import",
        "from core.checks import run_",
        "from core.checks import run",
    )
    allowed_lines = {
        "from core.checks import format_check_summary",
    }

    violations: list[str] = []
    for path in sorted(orchestrator_dir.glob("*.py")):
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw_line.strip()
            if not line or line in allowed_lines:
                continue
            if any(fragment in line for fragment in forbidden_fragments):
                violations.append(f"{path.name}:{line_number}: {line}")

    assert violations == []


def test_runtime_wiring_modules_do_not_type_against_workspacemanager() -> None:
    module_paths = [
        Path(__file__).resolve().parents[1] / "src" / "engine" / "flow.py",
        Path(__file__).resolve().parents[1] / "src" / "engine" / "flow_dependencies.py",
        Path(__file__).resolve().parents[1] / "src" / "agents" / "agent_bundle_factory.py",
        Path(__file__).resolve().parents[1] / "src" / "agents" / "agents.py",
    ]

    violations: list[str] = []
    for path in module_paths:
        for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw_line.strip()
            if "from core.workspace_manager import WorkspaceManager" in line:
                violations.append(f"{path.name}:{line_number}: {line}")

    assert violations == []


def test_flow_persistence_facade_uses_service_boundary() -> None:
    path = Path(__file__).resolve().parents[1] / "src" / "engine" / "flow_persistence_facade.py"
    source = path.read_text(encoding="utf-8")

    assert "from persistence.runtime_persistence import" not in source
    assert "from persistence.stage_artifacts import" not in source
    assert "self.persistence_service." in source


def test_refactor_target_module_sizes_remain_bounded() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    limits = {
        root / "engine" / "flow.py": 600,
        root / "engine" / "flow_facade_mixins.py": 120,
        root / "engine" / "flow_persistence_facade.py": 280,
        root / "engine" / "flow_runtime_facade.py": 260,
        root / "engine" / "flow_review_facade.py": 220,
        root / "persistence" / "stage_artifacts.py": 480,
        root / "persistence" / "stage_artifact_builders.py": 220,
        root / "persistence" / "service.py": 260,
    }

    violations: list[str] = []
    for path, limit in limits.items():
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > limit:
            violations.append(f"{path.name}: {lines}>{limit}")

    assert violations == []


def test_refactor_status_doc_exists_and_mentions_new_boundaries() -> None:
    path = Path(__file__).resolve().parents[1] / "docs" / "refactor-status.md"
    source = path.read_text(encoding="utf-8")

    assert "flow_dependencies.py" in source
    assert "persistence/service.py" in source
    assert "flow_runtime_facade.py" in source
    assert "flow_review_facade.py" in source


def test_flow_artifact_helpers_support_injected_artifact_store() -> None:
    class _StubArtifactStore:
        def __init__(self, base: Path) -> None:
            self.base = base
            self.calls: list[tuple[str, str]] = []

        def artifact_path(self, scope: str, filename: str) -> Path:
            self.calls.append((scope, filename))
            return self.base / f"{scope}__{filename}"

        def artifact_ref(self, scope: str, filename: str) -> str:
            self.calls.append((scope, filename))
            return f"stub://{scope}/{filename}"

        def stage_path(self, stage_name: str, suffix: str) -> Path:
            self.calls.append((stage_name, suffix))
            return self.base / f"{stage_name}__{suffix}"

        def stage_ref(self, stage_name: str, suffix: str) -> str:
            self.calls.append((stage_name, suffix))
            return f"stub://{stage_name}/{suffix}"

        def write_json(self, path: Path, payload: object) -> None:
            del payload
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")

        def write_text(self, path: Path, content: str) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        runtime = root / "runtime"
        repo.mkdir()
        runtime.mkdir()
        flow = _make_flow(repo, runtime)
        store = _StubArtifactStore(runtime / "custom-artifacts")
        flow.artifact_store = store

        path = flow._stage_artifact_path("stage-a", "report.json")
        ref = flow._stage_artifact_ref("stage-a", "report.json")
        global_path = flow._artifact_path("runtime", "runtime_status.json")
        global_ref = flow._artifact_ref("runtime", "runtime_status.json")

        assert path == runtime / "custom-artifacts" / "stage-a__report.json"
        assert ref == "stub://stage-a/report.json"
        assert global_path == runtime / "custom-artifacts" / "runtime__runtime_status.json"
        assert global_ref == "stub://runtime/runtime_status.json"
        assert store.calls == [
            ("stage-a", "report.json"),
            ("stage-a", "report.json"),
            ("runtime", "runtime_status.json"),
            ("runtime", "runtime_status.json"),
        ]
