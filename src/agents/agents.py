from __future__ import annotations

from agents.agent_bundle import AgentBundle
from ports.agent import AgentPort
from ports.workspace import WorkspacePort
from app.runtime_config import RuntimeConfig


AgentLike = AgentPort


def create_agent_bundle(cfg: RuntimeConfig, manager: WorkspacePort) -> AgentBundle:
    from .agent_bundle_factory import create_agent_bundle_from_runtime

    return create_agent_bundle_from_runtime(cfg, manager)
