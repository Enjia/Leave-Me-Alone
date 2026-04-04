from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ArtifactStorePort(Protocol):
    def artifact_path(self, scope: str, filename: str) -> Path:
        ...

    def artifact_ref(self, scope: str, filename: str) -> str:
        ...

    def stage_path(self, stage_name: str, suffix: str) -> Path:
        ...

    def stage_ref(self, stage_name: str, suffix: str) -> str:
        ...

    def write_json(self, path: Path, payload: object) -> None:
        ...

    def write_text(self, path: Path, content: str) -> None:
        ...
