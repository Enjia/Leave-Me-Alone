from __future__ import annotations

from pathlib import Path
from typing import Any

from .flow_review_facade import FlowReviewFacadeMixin
from .flow_runtime_facade import FlowRuntimeFacadeMixin
from .flow_persistence_facade import FlowPersistenceFacadeMixin
from core.models import (
    StageProgressLedger,
    StageSpec,
)
from policy.runtime_artifacts import (
    build_stage_progress_ledger,
)
from state.progress_helpers import (
    artifact_slug,
    latest_stage_progress_ledger,
    repo_progress_dir,
    repo_progress_path,
    repo_slug,
    select_active_subgoal,
)

class FlowHarnessFacadeMixin(
    FlowPersistenceFacadeMixin,
    FlowRuntimeFacadeMixin,
    FlowReviewFacadeMixin,
):
    @staticmethod
    def _artifact_slug(value: str) -> str:
        return artifact_slug(value)

    def _stage_output_artifact_suffix(self, artifact_name: str) -> str:
        return f"{self._artifact_slug(artifact_name)}.json"

    def _stage_artifact_path(self, stage_name: str, suffix: str) -> Any:
        return self.artifact_store.stage_path(stage_name, suffix)

    def _stage_artifact_ref(self, stage_name: str, suffix: str) -> str:
        return self.artifact_store.stage_ref(stage_name, suffix)

    def _artifact_path(self, scope: str, filename: str) -> Any:
        return self.artifact_store.artifact_path(scope, filename)

    def _artifact_ref(self, scope: str, filename: str) -> str:
        return self.artifact_store.artifact_ref(scope, filename)

    @staticmethod
    def _repo_slug(value: str) -> str:
        return repo_slug(value)

    def _repo_progress_dir(self) -> Path:
        return repo_progress_dir(self)

    def _repo_progress_path(self, stage: StageSpec, suffix: str) -> Path:
        return repo_progress_path(self, stage, suffix)

    def _latest_stage_progress_ledger(self, stage_name: str) -> StageProgressLedger | None:
        return latest_stage_progress_ledger(self, stage_name)

    def _select_active_subgoal(
        self,
        *,
        stage: StageSpec,
        round_index: int,
    ) -> tuple[str, str, str]:
        return select_active_subgoal(self, stage=stage, round_index=round_index)

    def _build_stage_progress_ledger(
        self,
        *,
        stage: StageSpec,
        round_index: int,
        status: str,
        passed_gates: list[str],
        latest_artifacts: list[str],
        current_blocker: str = "",
        current_blocker_category: str = "",
        notes: list[str] | None = None,
    ) -> StageProgressLedger:
        return build_stage_progress_ledger(
            self,
            stage=stage,
            round_index=round_index,
            status=status,
            passed_gates=passed_gates,
            latest_artifacts=latest_artifacts,
            current_blocker=current_blocker,
            current_blocker_category=current_blocker_category,
            notes=notes,
        )
