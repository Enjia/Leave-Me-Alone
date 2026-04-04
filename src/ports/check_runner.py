from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from typing import Any, Protocol

from core.models import GateTier


class CheckRunnerPort(Protocol):
    async def run_stage_checks(
        self,
        worker: str,
        stage: object,
        workspace: Path,
        *,
        gate_tier: GateTier = "fast_round",
        stage_budget_sec: int | None = None,
        round_budget_sec: int | None = None,
        phase_timeout_cap_sec: int | None = None,
        heartbeat_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> object:
        ...

    async def run_remote_preflight(self, worker: str, stage: object, workspace: Path) -> object:
        ...
