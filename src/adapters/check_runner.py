from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from check_plugins.adapt import check_results_to_auto_check_result
from check_plugins.builtin import create_default_registry
from check_plugins.plugin import CheckContext
from check_plugins.profiles import extract_commands_from_stage, resolve_plugins_for_tier
from check_plugins.registry import CheckPluginRegistry
from core.checks import run_checks_from_stage_spec_async, run_remote_preflight_from_stage_spec_async
from core.models import AutoCheckResult, GateTier, normalize_execution_env

class DefaultCheckRunner:
    def __init__(
        self,
        *,
        remote_host: str,
        remote_workdir: str,
        remote_host_secondary: str,
        remote_workdir_secondary: str,
        split_worker_remote_endpoints: bool = False,
        plugin_registry: CheckPluginRegistry | None = None,
    ) -> None:
        self.remote_host = remote_host
        self.remote_workdir = remote_workdir
        self.remote_host_secondary = remote_host_secondary
        self.remote_workdir_secondary = remote_workdir_secondary
        self.split_worker_remote_endpoints = split_worker_remote_endpoints
        self.plugin_registry = plugin_registry or create_default_registry()
    def _resolve_worker_remote_endpoints(self, worker: str, stage: object) -> tuple[str, str, str, str]:
        stage_execution_env = normalize_execution_env(str(getattr(stage, "execution_env", "")))
        should_split = (
            self.split_worker_remote_endpoints
            and stage_execution_env == "remote_primary"
            and worker == "worker_b"
            and bool(self.remote_host_secondary)
        )
        if should_split:
            return (
                self.remote_host_secondary,
                self.remote_workdir_secondary or self.remote_workdir,
                "",
                "",
            )
        return (
            self.remote_host,
            self.remote_workdir,
            self.remote_host_secondary,
            self.remote_workdir_secondary,
        )

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
        remote_host, remote_workdir, remote_host_secondary, remote_workdir_secondary = (
            self._resolve_worker_remote_endpoints(worker, stage)
        )
        return await run_checks_from_stage_spec_async(
            worker,
            stage,
            workspace,
            remote_host=remote_host,
            remote_workdir=remote_workdir,
            remote_host_secondary=remote_host_secondary,
            remote_workdir_secondary=remote_workdir_secondary,
            gate_tier=gate_tier,
            stage_budget_sec=stage_budget_sec,
            round_budget_sec=round_budget_sec,
            phase_timeout_cap_sec=phase_timeout_cap_sec,
            heartbeat_sink=heartbeat_sink,
        )

    async def run_remote_preflight(self, worker: str, stage: object, workspace: Path) -> object:
        remote_host, remote_workdir, remote_host_secondary, remote_workdir_secondary = (
            self._resolve_worker_remote_endpoints(worker, stage)
        )
        return await run_remote_preflight_from_stage_spec_async(
            worker,
            stage,
            workspace,
            remote_host=remote_host,
            remote_workdir=remote_workdir,
            remote_host_secondary=remote_host_secondary,
            remote_workdir_secondary=remote_workdir_secondary,
        )

    # ------------------------------------------------------------------
    # Plugin-based execution (new API)
    # ------------------------------------------------------------------

    def _build_check_context(
        self,
        worker: str,
        stage: object,
        workspace: Path,
        *,
        gate_tier: GateTier = "fast_round",
        stage_budget_sec: int | None = None,
        round_budget_sec: int | None = None,
        phase_timeout_cap_sec: int | None = None,
    ) -> CheckContext:
        """Build a CheckContext from the runner's configuration and call args."""
        remote_host, remote_workdir, remote_host_secondary, remote_workdir_secondary = (
            self._resolve_worker_remote_endpoints(worker, stage)
        )
        return CheckContext(
            worker=worker,
            stage_name=str(getattr(stage, "name", "")),
            workspace=workspace,
            gate_tier=gate_tier,
            remote_host=remote_host,
            remote_workdir=remote_workdir,
            remote_host_secondary=remote_host_secondary,
            remote_workdir_secondary=remote_workdir_secondary,
            stage_budget_sec=stage_budget_sec,
            round_budget_sec=round_budget_sec,
            phase_timeout_cap_sec=phase_timeout_cap_sec,
            extra={"stage": stage},
        )

    async def run_plugins(
        self,
        worker: str,
        stage: object,
        workspace: Path,
        *,
        plugin_names: list[str] | None = None,
        commands_by_plugin: dict[str, list[str]] | None = None,
        gate_tier: GateTier = "fast_round",
        stage_budget_sec: int | None = None,
        round_budget_sec: int | None = None,
        phase_timeout_cap_sec: int | None = None,
        timeout_sec: int | None = None,
        max_attempts: int = 1,
    ) -> list:
        """Run check plugins via the registry.

        If *plugin_names* is ``None``, all registered plugins are executed.
        This is the new plugin-based API that coexists with the legacy
        ``run_stage_checks`` / ``run_remote_preflight`` methods.
        """
        context = self._build_check_context(
            worker, stage, workspace,
            gate_tier=gate_tier,
            stage_budget_sec=stage_budget_sec,
            round_budget_sec=round_budget_sec,
            phase_timeout_cap_sec=phase_timeout_cap_sec,
        )
        if plugin_names is None:
            return await self.plugin_registry.run_all(
                context,
                commands_by_plugin=commands_by_plugin,
                timeout_sec=timeout_sec,
                max_attempts=max_attempts,
            )
        return await self.plugin_registry.run_plugins(
            plugin_names=plugin_names,
            context=context,
            commands_by_plugin=commands_by_plugin,
            timeout_sec=timeout_sec,
            max_attempts=max_attempts,
        )

    async def run_plugins_for_stage(
        self,
        worker: str,
        stage: object,
        workspace: Path,
        *,
        gate_tier: GateTier = "fast_round",
        stage_budget_sec: int | None = None,
        round_budget_sec: int | None = None,
        phase_timeout_cap_sec: int | None = None,
        timeout_sec: int | None = None,
        heartbeat_sink: Callable[[dict[str, Any]], None] | None = None,
        max_attempts: int = 2,
    ) -> AutoCheckResult:
        """Run check plugins selected by the stage's check profile.

        This is the highest-level plugin API.  It reads
        ``stage.check_profile`` (a ``dict[str, list[str]]`` mapping
        gate tier names to plugin name lists) and falls back to the
        built-in defaults in ``check_plugins.profiles`` when the stage
        does not specify a profile or the requested tier is absent.

        Commands are automatically extracted from the stage's existing
        fields (``lint_commands``, ``test_commands``, etc.).

        Returns an ``AutoCheckResult`` so that downstream code
        (``round_runner``, ``runtime_artifacts``, ``format_check_summary``)
        can consume the result without any adaptation.
        """
        tier_plugins: dict[str, list[str]] | None = getattr(stage, "check_profile", None) or None
        plugin_names = resolve_plugins_for_tier(gate_tier, tier_plugins=tier_plugins)
        commands_by_plugin = extract_commands_from_stage(stage, plugin_names, gate_tier=gate_tier)

        # Store heartbeat_sink in context.extra so plugins can use it.
        stage_name = str(getattr(stage, "name", ""))
        context = self._build_check_context(
            worker, stage, workspace,
            gate_tier=gate_tier,
            stage_budget_sec=stage_budget_sec,
            round_budget_sec=round_budget_sec,
            phase_timeout_cap_sec=phase_timeout_cap_sec,
        )
        if heartbeat_sink is not None:
            # CheckContext is frozen, so we inject via the mutable extra dict.
            context.extra["heartbeat_sink"] = heartbeat_sink

        raw_results = await self.plugin_registry.run_plugins(
            plugin_names=plugin_names,
            context=context,
            commands_by_plugin=commands_by_plugin,
            timeout_sec=timeout_sec,
            max_attempts=max_attempts,
        )

        return check_results_to_auto_check_result(
            raw_results,
            worker=worker,
            stage_name=stage_name,
        )
