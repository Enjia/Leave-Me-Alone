from __future__ import annotations

import logging
from pathlib import Path

from core.models import StageSpec

logger = logging.getLogger(__name__)

def owner_workspace_path(flow: object) -> Path:
    return flow.agents.worker_workspace

def promote_owner_workspace(flow: object, stage: StageSpec) -> str | None:
    del stage
    owner_workspace = owner_workspace_path(flow)
    try:
        flow.workspace_port.promote_owner_workspace(
            owner_workspace=owner_workspace,
            peer_workspace=None,
        )
    except Exception as exc:
        logger.exception("Failed promoting owner workspace")
        return f"failed to promote owner workspace: {exc}"
    return None
