from __future__ import annotations

import tempfile
from pathlib import Path

from engine.flow_dependencies import build_default_flow_dependencies
from app.runtime_config import RuntimeConfig
from core.workspace_manager import WorkspaceManager


def test_build_default_flow_dependencies_creates_default_ports() -> None:
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
            remote_host="node0",
            remote_workdir="/enjia/repo",
            remote_host_node1="node1",
            remote_workdir_node1="/enjia/repo",
        )
        manager = WorkspaceManager(target_repo=repo, runtime_dir=runtime_dir)

        deps = build_default_flow_dependencies(
            cfg=cfg,
            workspace_manager=manager,
            slugify=lambda value: value.replace("/", "_"),
        )

        assert deps.workspace_port.prepare_worker_workspace("worker_a").name == "worker_a"
        assert deps.artifact_store.stage_ref("stage/a", "report.json") == "artifacts/stage_a_report.json"
        assert deps.artifact_store.artifact_ref("runtime", "runtime_status.json") == "artifacts/runtime_runtime_status.json"
        assert deps.check_runner.remote_host == "node0"


def test_flow_module_no_longer_constructs_default_concrete_adapters_directly() -> None:
    flow_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "engine"
        / "flow.py"
    )
    source = flow_path.read_text(encoding="utf-8")

    assert "from adapters.check_runner import DefaultCheckRunner" not in source
    assert "from adapters.workspace_git import WorkspaceManagerAdapter" not in source
    assert "from persistence.artifact_store import FileArtifactStore" not in source


def test_persistence_modules_do_not_use_with_name_for_global_artifacts() -> None:
    module_paths = [
        Path(__file__).resolve().parents[1]
        / "src"
        / "persistence"
        / "runtime_persistence.py",
        Path(__file__).resolve().parents[1]
        / "src"
        / "persistence"
        / "stage_artifacts.py",
    ]
    for path in module_paths:
        source = path.read_text(encoding="utf-8")
        assert ".with_name(" not in source
