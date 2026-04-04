from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from .opencode_a2a_adapter import OpenCodeA2AAdapter
from ports.agent import AgentPort


logger = logging.getLogger(__name__)


@dataclass
class AgentBundle:
    judge: AgentPort
    verifier: AgentPort
    worker_a: AgentPort
    worker_b: AgentPort
    worker_a_workspace: Path
    worker_b_workspace: Path
    a2a_adapters: list[OpenCodeA2AAdapter] | None = None

    def start_a2a(self) -> None:
        if not self.a2a_adapters:
            return

        started: list[OpenCodeA2AAdapter] = []
        try:
            for adapter in self.a2a_adapters:
                adapter.start()
                started.append(adapter)
        except Exception:
            logger.exception("Failed to start all A2A adapters; rolling back started adapters.")
            for adapter in reversed(started):
                try:
                    adapter.shutdown()
                except Exception:
                    logger.exception("Failed to shutdown partially started A2A adapter during rollback.")
            raise

    def shutdown_a2a(self) -> None:
        if not self.a2a_adapters:
            return
        for adapter in self.a2a_adapters:
            adapter.shutdown()
