from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from adapters.check_runner import DefaultCheckRunner
from adapters.workspace_git import WorkspaceManagerAdapter
from persistence.artifact_store import FileArtifactStore
from ports.artifact_store import ArtifactStorePort
from ports.check_runner import CheckRunnerPort
from ports.workspace import WorkspacePort
from app.runtime_config import RuntimeConfig


@dataclass(frozen=True)
class FlowDependencies:
    workspace_port: WorkspacePort
    check_runner: CheckRunnerPort
    artifact_store: ArtifactStorePort


def build_default_flow_dependencies(
    *,
    cfg: RuntimeConfig,
    workspace_manager: WorkspacePort,
    slugify: Callable[[str], str],
) -> FlowDependencies:
    return FlowDependencies(
        workspace_port=WorkspaceManagerAdapter(workspace_manager),
        check_runner=DefaultCheckRunner(
            remote_host=cfg.remote_host,
            remote_workdir=cfg.remote_workdir,
            remote_host_secondary=cfg.remote_host_secondary,
            remote_workdir_secondary=cfg.remote_workdir_secondary,
            split_worker_remote_endpoints=cfg.split_worker_remote_endpoints,
        ),
        artifact_store=FileArtifactStore(
            runtime_dir=cfg.runtime_dir,
            slugify=slugify,
        ),
    )
