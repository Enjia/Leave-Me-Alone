from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class FileArtifactStore:
    def __init__(self, runtime_dir: Path, slugify: callable) -> None:
        self.runtime_dir = runtime_dir
        self._slugify = slugify

    def artifact_path(self, scope: str, filename: str) -> Path:
        artifacts_dir = self.runtime_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        return artifacts_dir / f"{self._slugify(scope)}_{filename}"

    def artifact_ref(self, scope: str, filename: str) -> str:
        return f"artifacts/{self._slugify(scope)}_{filename}"

    def stage_path(self, stage_name: str, suffix: str) -> Path:
        return self.artifact_path(stage_name, suffix)

    def stage_ref(self, stage_name: str, suffix: str) -> str:
        return self.artifact_ref(stage_name, suffix)

    def write_json(self, path: Path, payload: object) -> None:
        self.write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(content)
            temp_name = handle.name
        os.replace(temp_name, path)
