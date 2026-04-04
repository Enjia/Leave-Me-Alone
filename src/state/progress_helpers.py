from __future__ import annotations

import re
from pathlib import Path

from core.models import StageProgressLedger, StageSpec


def artifact_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    slug = re.sub(r"_+", "_", slug).strip("._")
    return slug or "stage"


def repo_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip()).strip("_")
    return slug or "stage"


def repo_progress_dir(flow: object) -> Path:
    directory = Path(flow.state.target_repo) / "docs" / "stage-progress"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def repo_progress_path(flow: object, stage: StageSpec, suffix: str) -> Path:
    base = stage.stage_id or stage.name
    return repo_progress_dir(flow) / f"{repo_slug(base)}_{suffix}"


def latest_stage_progress_ledger(flow: object, stage_name: str) -> StageProgressLedger | None:
    items = flow.state.stage_progress_ledgers.get(stage_name, [])
    return items[-1] if items else None


def select_active_subgoal(
    flow: object,
    *,
    stage: StageSpec,
    round_index: int,
) -> tuple[str, str, str]:
    if not stage.subgoals:
        return ("", "", "")
    latest = latest_stage_progress_ledger(flow, stage.name)
    if latest and latest.active_subgoal_id:
        for subgoal in stage.subgoals:
            if subgoal.subgoal_id == latest.active_subgoal_id and latest.status not in {"passed", "failed"}:
                return (subgoal.subgoal_id, subgoal.title, subgoal.description)
    index = min(max(round_index - 1, 0), len(stage.subgoals) - 1)
    selected = stage.subgoals[index]
    return (selected.subgoal_id, selected.title, selected.description)
