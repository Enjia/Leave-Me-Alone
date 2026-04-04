from __future__ import annotations

import logging
from pathlib import Path

from core.models import StageSpec


logger = logging.getLogger(__name__)


def owner_workspace_path(flow: object) -> Path:
    return (
        flow.agents.worker_a_workspace
        if flow.cfg.owner_worker == "worker_a"
        else flow.agents.worker_b_workspace
    )


def promote_owner_workspace(flow: object, stage: StageSpec) -> str | None:
    del stage
    owner = flow.cfg.owner_worker
    owner_workspace = owner_workspace_path(flow)
    peer_workspace = (
        flow.agents.worker_b_workspace
        if owner == "worker_a"
        else flow.agents.worker_a_workspace
    )
    try:
        flow.workspace_port.promote_owner_workspace(
            owner_workspace=owner_workspace,
            peer_workspace=peer_workspace,
        )
    except Exception as exc:
        logger.exception("Failed promoting owner workspace %s", owner)
        return f"failed to promote owner workspace {owner}: {exc}"
    return None
