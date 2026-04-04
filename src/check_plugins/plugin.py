"""CheckPlugin Protocol and supporting data classes."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from core.models import GateTier


@dataclass(frozen=True)
class CheckContext:
    """Immutable context passed to every check plugin invocation."""

    worker: str
    stage_name: str
    workspace: Path
    gate_tier: GateTier = "fast_round"
    remote_host: str = ""
    remote_workdir: str = ""
    remote_host_node1: str = ""
    remote_workdir_node1: str = ""
    stage_budget_sec: int | None = None
    round_budget_sec: int | None = None
    phase_timeout_cap_sec: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckResult:
    """Standardized result returned by every check plugin.

    Plugins populate ``passed``, ``commands``, and optionally ``evidence``
    and ``error_category``.  The orchestrator uses these fields to build
    ``CheckSummaryArtifact`` and ``FailureClassification`` without knowing
    the plugin internals.
    """

    plugin_name: str
    passed: bool
    commands: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    error_category: str = ""
    skipped: bool = False
    skip_reason: str = ""
    duration_sec: float = 0.0


@runtime_checkable
class CheckPlugin(Protocol):
    """Protocol that every check plugin must satisfy.

    A plugin is identified by its ``name`` property and executed via
    ``run()``.  The registry discovers plugins by name and invokes them
    in the order specified by the stage profile.
    """

    @property
    def name(self) -> str:
        """Unique plugin identifier (e.g. ``"lint"``, ``"remote_gate"``)."""
        ...

    async def run(self, context: CheckContext, commands: list[str]) -> CheckResult:
        """Execute the check and return a standardized result.

        Parameters
        ----------
        context:
            Immutable execution context (worker, workspace, remote endpoints, …).
        commands:
            The list of shell commands to execute for this check type.
            May be empty if the plugin generates its own commands.

        Returns
        -------
        CheckResult
            Standardized result with pass/fail, command details, and evidence.
        """
        ...
