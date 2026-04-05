"""Built-in check plugins that wrap the existing checks.py primitives.

Each plugin delegates to the low-level ``_run_command`` / ``_run_command_list``
helpers in ``checks.py``, converting their results into the standardized
``CheckResult`` format.  This keeps the existing execution logic intact while
making it composable and replaceable via the plugin registry.

The ``HarnessCheckPlugin`` additionally supports the full remote execution
plane (sync → precondition → remote exec with heartbeat) so that the plugin
path is semantically equivalent to the legacy ``run_checks_from_stage_spec``.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from core.models import normalize_execution_env
from core.checks import (
    _check_remote_workdir_exists,
    _cleanup_remote_path_sensitive_metadata,
    _resolve_effective_remote_command_timeout_sec,
    _resolve_remote_cache_paths,
    _resolve_remote_targets,
    _resolve_sync_targets,
    _run_command_list,
    _run_remote_command_list,
    _should_preserve_remote_cache,
    _sync_to_remote,
    _validate_remote_preconditions,
    _validate_remote_results,
    _worker_scoped_remote_workdir,
    run_remote_preflight_from_stage_spec,
)
from .plugin import CheckContext, CheckResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _command_results_to_check_result(
    plugin_name: str,
    raw_results: list[Any],
) -> CheckResult:
    """Convert a list of CheckCommandResult objects to a CheckResult."""
    commands: list[dict[str, Any]] = []
    evidence: list[str] = []
    all_passed = True

    for result in raw_results:
        passed = getattr(result, "passed", False)
        command = getattr(result, "command", "")
        exit_code = getattr(result, "exit_code", -1)
        stdout = getattr(result, "stdout", "")
        stderr = getattr(result, "stderr", "")

        commands.append({
            "command": command,
            "exit_code": exit_code,
            "passed": passed,
            "stdout": stdout[:500] if stdout else "",
            "stderr": stderr[:500] if stderr else "",
        })

        if not passed:
            all_passed = False
            snippet = stderr.strip()[:200] or stdout.strip()[:200]
            evidence.append(f"{command} (exit={exit_code}): {snippet}")

    error_category = ""
    if not all_passed:
        for cmd_info in commands:
            if cmd_info["exit_code"] == -2:
                error_category = "input_contract"
                break
            if "timeout" in cmd_info.get("stderr", "").lower():
                error_category = "timeout"
                break
        if not error_category:
            error_category = "automated_checks"

    return CheckResult(
        plugin_name=plugin_name,
        passed=all_passed,
        commands=commands,
        evidence=evidence,
        error_category=error_category,
    )


# ---------------------------------------------------------------------------
# Built-in plugins
# ---------------------------------------------------------------------------

class LintCheckPlugin:
    """Runs lint commands from the stage gate."""

    @property
    def name(self) -> str:
        return "lint"

    async def run(self, context: CheckContext, commands: list[str]) -> CheckResult:
        if not commands:
            return CheckResult(plugin_name=self.name, passed=True, skipped=True, skip_reason="No lint commands configured.")
        results = await asyncio.to_thread(_run_command_list, commands, context.workspace)
        return _command_results_to_check_result(self.name, results)


class TestCheckPlugin:
    """Runs test commands from the stage gate."""

    @property
    def name(self) -> str:
        return "test"

    async def run(self, context: CheckContext, commands: list[str]) -> CheckResult:
        if not commands:
            return CheckResult(plugin_name=self.name, passed=True, skipped=True, skip_reason="No test commands configured.")
        results = await asyncio.to_thread(_run_command_list, commands, context.workspace)
        return _command_results_to_check_result(self.name, results)


class PerfCheckPlugin:
    """Runs performance check commands from the stage gate."""

    @property
    def name(self) -> str:
        return "perf"

    async def run(self, context: CheckContext, commands: list[str]) -> CheckResult:
        if not commands:
            return CheckResult(plugin_name=self.name, passed=True, skipped=True, skip_reason="No perf commands configured.")
        results = await asyncio.to_thread(_run_command_list, commands, context.workspace)
        return _command_results_to_check_result(self.name, results)


class HarnessCheckPlugin:
    """Runs harness-level gate commands (local or remote).

    When the ``CheckContext`` carries a non-empty ``remote_host`` **and**
    the stage's ``execution_env`` indicates remote execution, this plugin
    replicates the full legacy remote execution plane:

    1. Validate remote preconditions (host/workdir configured).
    2. Sync workspace to remote targets (respecting ``sync_strategy``).
    3. Execute commands on each remote target via SSH with heartbeat.

    Otherwise it falls back to local ``_run_command_list``.
    """

    @property
    def name(self) -> str:
        return "harness"

    async def run(self, context: CheckContext, commands: list[str]) -> CheckResult:
        if not commands:
            return CheckResult(plugin_name=self.name, passed=True, skipped=True, skip_reason="No harness commands configured.")

        stage = context.extra.get("stage")
        execution_env = normalize_execution_env(str(getattr(stage, "execution_env", ""))) if stage else ""
        requires_remote = execution_env in (
            "remote_primary", "remote_secondary", "remote_primary_and_secondary",
        )

        if requires_remote and stage is not None:
            return await self._run_remote(context, commands, stage)

        # Local fallback
        results = await asyncio.to_thread(_run_command_list, commands, context.workspace)
        result = _command_results_to_check_result(self.name, results)
        if not result.passed and result.error_category == "automated_checks":
            has_remote = any("remote" in cmd or "dev_env_remote" in cmd for cmd in commands)
            result.error_category = "remote_gate" if has_remote else "artifact_contract"
        return result

    async def _run_remote(
        self,
        context: CheckContext,
        commands: list[str],
        stage: Any,
    ) -> CheckResult:
        """Full remote execution plane matching legacy run_checks_from_stage_spec."""
        heartbeat_sink: Callable[[dict[str, Any]], None] | None = context.extra.get("heartbeat_sink")

        effective_remote_workdir = _worker_scoped_remote_workdir(context.remote_workdir, context.worker)
        secondary_base = context.remote_workdir_secondary or context.remote_workdir
        effective_remote_workdir_secondary = _worker_scoped_remote_workdir(secondary_base, context.worker)

        all_cmd_results: list[Any] = []
        remote_cmd_results: list[Any] = []
        evidence: list[str] = []

        # Step 0: Validate remote preconditions
        precondition_results = _validate_remote_preconditions(
            stage,
            context.remote_host,
            effective_remote_workdir,
            context.remote_host_secondary,
            effective_remote_workdir_secondary,
            remote_commands=commands,
        )
        if precondition_results:
            all_cmd_results.extend(precondition_results)
            for precondition_result in precondition_results:
                evidence.append(f"precondition failed: {getattr(precondition_result, 'stderr', '')}"[:200])
            return _build_harness_result(self.name, all_cmd_results, evidence, error_category="remote_gate")

        # Step 1: Sync workspace to remote targets
        preserve_remote_cache = _should_preserve_remote_cache(stage, context.gate_tier)
        remote_cache_paths = _resolve_remote_cache_paths(stage)
        sync_targets = _resolve_sync_targets(
            stage, context.remote_host, effective_remote_workdir,
            context.remote_host_secondary, effective_remote_workdir_secondary,
        )
        synced_targets: set[tuple[str, str]] = set()
        for host, workdir in sync_targets:
            sync_result = await asyncio.to_thread(
                _sync_to_remote,
                context.workspace, host, workdir,
                preserve_build_cache=preserve_remote_cache,
                cache_paths=remote_cache_paths,
            )
            all_cmd_results.append(sync_result)
            if not sync_result.passed:
                logger.error("Sync to %s:%s failed, skipping remote commands for this target", host, workdir)
                evidence.append(f"sync failed: {host}:{workdir}")
                continue
            cleanup_result = await asyncio.to_thread(
                _cleanup_remote_path_sensitive_metadata, host, workdir,
            )
            all_cmd_results.append(cleanup_result)
            if cleanup_result.passed:
                synced_targets.add((host, workdir))
            else:
                logger.error("Remote metadata cleanup failed for %s:%s", host, workdir)
                evidence.append(f"cleanup failed: {host}:{workdir}")

        # Step 2: Execute commands on remote targets
        exec_targets = _resolve_remote_targets(
            stage, context.remote_host, effective_remote_workdir,
            context.remote_host_secondary, effective_remote_workdir_secondary,
        )
        remote_timeout = _resolve_effective_remote_command_timeout_sec(
            stage_budget_sec=context.stage_budget_sec,
            round_budget_sec=context.round_budget_sec,
            phase_timeout_cap_sec=context.phase_timeout_cap_sec,
        )
        for host, workdir in exec_targets:
            if sync_targets and (host, workdir) not in synced_targets:
                evidence.append(f"skipped {host}:{workdir} (sync failed)")
                continue
            preflight_result = await asyncio.to_thread(_check_remote_workdir_exists, host, workdir)
            all_cmd_results.append(preflight_result)
            if not preflight_result.passed:
                evidence.append(f"workdir missing: {host}:{workdir}")
                continue
            node_results = await asyncio.to_thread(
                _run_remote_command_list,
                commands, host, workdir,
                timeout=remote_timeout,
                worker=context.worker,
                stage_name=context.stage_name,
                gate_tier=context.gate_tier,
                heartbeat_sink=heartbeat_sink,
            )
            all_cmd_results.extend(node_results)
            remote_cmd_results.extend(node_results)

        # Keep legacy semantics: validate remote command execution/contracts.
        all_cmd_results.extend(
            _validate_remote_results(
                stage,
                remote_cmd_results,
                gate_tier=context.gate_tier,
                selected_remote_commands=commands,
            )
        )

        return _build_harness_result(self.name, all_cmd_results, evidence, error_category="remote_gate")


def _build_harness_result(
    plugin_name: str,
    raw_results: list[Any],
    extra_evidence: list[str],
    *,
    error_category: str = "remote_gate",
) -> CheckResult:
    """Build a CheckResult from a mix of sync/preflight/exec results."""
    result = _command_results_to_check_result(plugin_name, raw_results)
    result.evidence.extend(extra_evidence)
    if not result.passed and result.error_category in ("automated_checks", ""):
        result.error_category = error_category
    return result


class RemotePreflightPlugin:
    """Runs remote preflight checks via SSH + container."""

    @property
    def name(self) -> str:
        return "remote_preflight"

    async def run(self, context: CheckContext, commands: list[str]) -> CheckResult:
        # Remote preflight uses the stage spec directly, not individual commands.
        # The 'commands' parameter is ignored; the plugin reads from context.extra.
        stage = context.extra.get("stage")
        if stage is None:
            return CheckResult(
                plugin_name=self.name,
                passed=False,
                error_category="input_contract",
                evidence=["No stage object in context.extra"],
            )

        try:
            raw_result = await asyncio.to_thread(
                run_remote_preflight_from_stage_spec,
                context.worker,
                stage,
                context.workspace,
                remote_host=context.remote_host,
                remote_workdir=context.remote_workdir,
                remote_host_secondary=context.remote_host_secondary,
                remote_workdir_secondary=context.remote_workdir_secondary,
            )
        except Exception as exc:
            return CheckResult(
                plugin_name=self.name,
                passed=False,
                error_category="remote_gate",
                evidence=[f"Remote preflight exception: {exc!s}"[:300]],
            )

        # Convert the raw preflight result to CheckResult.
        status = getattr(raw_result, "status", "unknown")
        passed = str(status).lower() in ("passed", "ok", "success")
        entries = getattr(raw_result, "results", []) or []

        cmd_details: list[dict[str, Any]] = []
        evidence: list[str] = []
        for entry in entries:
            entry_passed = getattr(entry, "passed", False)
            entry_cmd = str(getattr(entry, "command", ""))
            cmd_details.append({
                "command": entry_cmd,
                "passed": entry_passed,
                "exit_code": 0 if entry_passed else 1,
            })
            if not entry_passed:
                evidence.append(f"preflight failed: {entry_cmd}"[:200])

        return CheckResult(
            plugin_name=self.name,
            passed=passed,
            commands=cmd_details,
            evidence=evidence,
            error_category="" if passed else "remote_gate",
        )


# ---------------------------------------------------------------------------
# Factory: create a registry with all built-in plugins
# ---------------------------------------------------------------------------

def create_default_registry() -> "CheckPluginRegistry":
    """Create a CheckPluginRegistry pre-loaded with all built-in plugins."""
    from .registry import CheckPluginRegistry

    registry = CheckPluginRegistry()
    registry.register(LintCheckPlugin())
    registry.register(TestCheckPlugin())
    registry.register(PerfCheckPlugin())
    registry.register(HarnessCheckPlugin())
    registry.register(RemotePreflightPlugin())
    return registry
