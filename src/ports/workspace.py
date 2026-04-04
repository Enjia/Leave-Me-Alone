from __future__ import annotations

from pathlib import Path
from typing import Protocol


class WorkspaceArtifactsLike(Protocol):
    changed_files: list[str]
    status_lines: list[str]
    patch: str
    review_patch: str


class WorkspacePort(Protocol):
    def prepare_worker_workspace(self, worker: str) -> Path:
        ...

    def capture_snapshot(self, workspace: Path) -> object:
        ...

    def capture_artifacts(
        self,
        workspace: Path,
        *,
        baseline_snapshot: object | None = None,
    ) -> WorkspaceArtifactsLike:
        ...

    def promote_owner_workspace(self, *, owner_workspace: Path, peer_workspace: Path) -> None:
        ...
