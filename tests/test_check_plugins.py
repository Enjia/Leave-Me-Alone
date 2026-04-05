"""Tests for the check_plugins subsystem.

Covers:
- CheckContext construction
- CheckResult defaults
- CheckPluginRegistry register / get / has / run_plugins / run_all
- resolve_plugins_for_tier (default + custom profile)
- extract_commands_from_stage
- DefaultCheckRunner._build_check_context
- DefaultCheckRunner.run_plugins (mock registry)
- DefaultCheckRunner.run_plugins_for_stage (profile-driven)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from check_plugins.plugin import CheckContext, CheckResult
from check_plugins.profiles import (
    DEFAULT_TIER_PLUGINS,
    extract_commands_from_stage,
    resolve_plugins_for_tier,
)
from check_plugins.registry import CheckPluginRegistry


# ------------------------------------------------------------------
# Helpers / Fixtures
# ------------------------------------------------------------------

class FakePlugin:
    """Minimal CheckPlugin implementation for testing."""

    def __init__(self, plugin_name: str, *, should_pass: bool = True, delay: float = 0.0):
        self._name = plugin_name
        self._should_pass = should_pass
        self._delay = delay
        self.call_count = 0
        self.last_context: CheckContext | None = None
        self.last_commands: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    async def run(self, context: CheckContext, commands: list[str]) -> CheckResult:
        self.call_count += 1
        self.last_context = context
        self.last_commands = list(commands)
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return CheckResult(
            plugin_name=self._name,
            passed=self._should_pass,
            commands=[{"command": cmd, "exit_code": 0, "passed": self._should_pass} for cmd in commands],
            evidence=[] if self._should_pass else [f"{self._name} failed"],
            error_category="" if self._should_pass else "automated_checks",
        )


class ErrorPlugin:
    """Plugin that raises an exception."""

    @property
    def name(self) -> str:
        return "error_plugin"

    async def run(self, context: CheckContext, commands: list[str]) -> CheckResult:
        raise RuntimeError("boom")


@dataclass
class FakeStage:
    """Minimal stage object for testing."""
    name: str = "test-stage"
    execution_env: str = "local_only"
    sync_strategy: str = "local_only"
    lint_commands: list[str] = field(default_factory=lambda: ["ruff check ."])
    test_commands: list[str] = field(default_factory=lambda: ["pytest -x"])
    perf_checks: list[str] = field(default_factory=list)
    gate_commands_remote: list[str] = field(default_factory=list)
    remote_gate_contracts: list[Any] = field(default_factory=list)
    check_profile: dict[str, list[str]] = field(default_factory=dict)


def _make_context(**overrides: Any) -> CheckContext:
    defaults: dict[str, Any] = {
        "worker": "worker_a",
        "stage_name": "stage-1",
        "workspace": Path("/tmp/ws"),
    }
    defaults.update(overrides)
    return CheckContext(**defaults)


# ==================================================================
# CheckContext
# ==================================================================

class TestCheckContext:
    def test_defaults(self):
        ctx = _make_context()
        assert ctx.worker == "worker_a"
        assert ctx.gate_tier == "fast_round"
        assert ctx.remote_host == ""
        assert ctx.extra == {}

    def test_frozen(self):
        ctx = _make_context()
        with pytest.raises(AttributeError):
            ctx.worker = "worker_b"  # type: ignore[misc]

    def test_custom_fields(self):
        ctx = _make_context(
            gate_tier="pre_promotion",
            remote_host="10.0.0.1",
            stage_budget_sec=300,
            extra={"stage": "obj"},
        )
        assert ctx.gate_tier == "pre_promotion"
        assert ctx.remote_host == "10.0.0.1"
        assert ctx.stage_budget_sec == 300
        assert ctx.extra["stage"] == "obj"


# ==================================================================
# CheckResult
# ==================================================================

class TestCheckResult:
    def test_defaults(self):
        result = CheckResult(plugin_name="lint", passed=True)
        assert result.commands == []
        assert result.evidence == []
        assert result.error_category == ""
        assert result.skipped is False
        assert result.duration_sec == 0.0

    def test_skipped(self):
        result = CheckResult(plugin_name="perf", passed=True, skipped=True, skip_reason="No commands")
        assert result.skipped is True
        assert result.skip_reason == "No commands"


# ==================================================================
# CheckPluginRegistry
# ==================================================================

class TestCheckPluginRegistry:
    def test_register_and_lookup(self):
        registry = CheckPluginRegistry()
        plugin = FakePlugin("lint")
        registry.register(plugin)

        assert registry.has_plugin("lint")
        assert registry.get_plugin("lint") is plugin
        assert not registry.has_plugin("test")
        assert registry.get_plugin("test") is None

    def test_registered_names(self):
        registry = CheckPluginRegistry()
        registry.register(FakePlugin("lint"))
        registry.register(FakePlugin("test"))
        assert registry.registered_names == ["lint", "test"]

    def test_overwrite_existing(self):
        registry = CheckPluginRegistry()
        original = FakePlugin("lint")
        replacement = FakePlugin("lint", should_pass=False)
        registry.register(original)
        registry.register(replacement)
        assert registry.get_plugin("lint") is replacement

    @pytest.mark.asyncio
    async def test_run_plugins_basic(self):
        registry = CheckPluginRegistry()
        lint_plugin = FakePlugin("lint")
        test_plugin = FakePlugin("test")
        registry.register(lint_plugin)
        registry.register(test_plugin)

        ctx = _make_context()
        results = await registry.run_plugins(
            plugin_names=["lint", "test"],
            context=ctx,
            commands_by_plugin={"lint": ["ruff check ."], "test": ["pytest"]},
        )

        assert len(results) == 2
        assert results[0].plugin_name == "lint"
        assert results[0].passed is True
        assert results[1].plugin_name == "test"
        assert lint_plugin.call_count == 1
        assert lint_plugin.last_commands == ["ruff check ."]

    @pytest.mark.asyncio
    async def test_run_plugins_missing_plugin(self):
        registry = CheckPluginRegistry()
        registry.register(FakePlugin("lint"))

        ctx = _make_context()
        results = await registry.run_plugins(
            plugin_names=["lint", "nonexistent"],
            context=ctx,
        )

        assert len(results) == 2
        assert results[1].plugin_name == "nonexistent"
        assert results[1].skipped is True
        assert results[1].passed is False

    @pytest.mark.asyncio
    async def test_run_plugins_dedup(self):
        registry = CheckPluginRegistry()
        plugin = FakePlugin("lint")
        registry.register(plugin)

        ctx = _make_context()
        results = await registry.run_plugins(
            plugin_names=["lint", "lint", "lint"],
            context=ctx,
            dedup=True,
        )

        assert len(results) == 1
        assert plugin.call_count == 1

    @pytest.mark.asyncio
    async def test_run_plugins_no_dedup(self):
        registry = CheckPluginRegistry()
        plugin = FakePlugin("lint")
        registry.register(plugin)

        ctx = _make_context()
        results = await registry.run_plugins(
            plugin_names=["lint", "lint"],
            context=ctx,
            dedup=False,
        )

        assert len(results) == 2
        assert plugin.call_count == 2

    @pytest.mark.asyncio
    async def test_run_plugins_timeout(self):
        registry = CheckPluginRegistry()
        slow_plugin = FakePlugin("slow", delay=5.0)
        registry.register(slow_plugin)

        ctx = _make_context()
        results = await registry.run_plugins(
            plugin_names=["slow"],
            context=ctx,
            timeout_sec=1,
        )

        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].error_category == "timeout"

    @pytest.mark.asyncio
    async def test_run_plugins_exception(self):
        registry = CheckPluginRegistry()
        registry.register(ErrorPlugin())

        ctx = _make_context()
        results = await registry.run_plugins(
            plugin_names=["error_plugin"],
            context=ctx,
        )

        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].error_category == "unknown"

    @pytest.mark.asyncio
    async def test_run_plugins_retries_timeout_then_succeeds(self):
        registry = CheckPluginRegistry()

        class FlakyTimeoutPlugin:
            def __init__(self) -> None:
                self.calls = 0

            @property
            def name(self) -> str:
                return "flaky_timeout"

            async def run(self, context: CheckContext, commands: list[str]) -> CheckResult:
                del context, commands
                self.calls += 1
                if self.calls == 1:
                    return CheckResult(
                        plugin_name=self.name,
                        passed=False,
                        error_category="timeout",
                        evidence=["request timed out"],
                    )
                return CheckResult(plugin_name=self.name, passed=True)

        plugin = FlakyTimeoutPlugin()
        registry.register(plugin)
        results = await registry.run_plugins(
            plugin_names=["flaky_timeout"],
            context=_make_context(),
            max_attempts=2,
            backoff_base_sec=0.0,
            backoff_max_sec=0.0,
        )
        assert len(results) == 1
        assert results[0].passed is True
        assert plugin.calls == 2

    @pytest.mark.asyncio
    async def test_run_plugins_retries_transient_exception_then_succeeds(self):
        registry = CheckPluginRegistry()

        class FlakyExceptionPlugin:
            def __init__(self) -> None:
                self.calls = 0

            @property
            def name(self) -> str:
                return "flaky_exception"

            async def run(self, context: CheckContext, commands: list[str]) -> CheckResult:
                del context, commands
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("503 Service Unavailable")
                return CheckResult(plugin_name=self.name, passed=True)

        plugin = FlakyExceptionPlugin()
        registry.register(plugin)
        results = await registry.run_plugins(
            plugin_names=["flaky_exception"],
            context=_make_context(),
            max_attempts=2,
            backoff_base_sec=0.0,
            backoff_max_sec=0.0,
        )
        assert len(results) == 1
        assert results[0].passed is True
        assert plugin.calls == 2

    @pytest.mark.asyncio
    async def test_run_all(self):
        registry = CheckPluginRegistry()
        registry.register(FakePlugin("lint"))
        registry.register(FakePlugin("test"))

        ctx = _make_context()
        results = await registry.run_all(ctx)

        assert len(results) == 2
        assert [r.plugin_name for r in results] == ["lint", "test"]

    @pytest.mark.asyncio
    async def test_run_plugins_empty_commands(self):
        registry = CheckPluginRegistry()
        plugin = FakePlugin("lint")
        registry.register(plugin)

        ctx = _make_context()
        results = await registry.run_plugins(
            plugin_names=["lint"],
            context=ctx,
            commands_by_plugin={},
        )

        assert len(results) == 1
        assert plugin.last_commands == []

    @pytest.mark.asyncio
    async def test_duration_recorded(self):
        registry = CheckPluginRegistry()
        registry.register(FakePlugin("lint"))

        ctx = _make_context()
        results = await registry.run_plugins(
            plugin_names=["lint"],
            context=ctx,
        )

        assert results[0].duration_sec >= 0.0


# ==================================================================
# profiles: resolve_plugins_for_tier
# ==================================================================

class TestResolvePluginsForTier:
    def test_default_fast_round(self):
        plugins = resolve_plugins_for_tier("fast_round")
        assert plugins == ["lint", "test"]

    def test_default_pre_promotion(self):
        plugins = resolve_plugins_for_tier("pre_promotion")
        assert plugins == ["lint", "test", "perf", "harness"]

    def test_default_full_regression(self):
        plugins = resolve_plugins_for_tier("full_regression")
        assert plugins == ["lint", "test", "perf", "harness", "remote_preflight"]

    def test_custom_profile_override(self):
        custom = {"fast_round": ["lint"], "pre_promotion": ["lint", "test", "custom_check"]}
        plugins = resolve_plugins_for_tier("fast_round", tier_plugins=custom)
        assert plugins == ["lint"]

        plugins = resolve_plugins_for_tier("pre_promotion", tier_plugins=custom)
        assert plugins == ["lint", "test", "custom_check"]

    def test_custom_profile_fallback_to_default(self):
        custom = {"fast_round": ["lint"]}
        plugins = resolve_plugins_for_tier("full_regression", tier_plugins=custom)
        assert plugins == DEFAULT_TIER_PLUGINS["full_regression"]

    def test_returns_copy(self):
        plugins_a = resolve_plugins_for_tier("fast_round")
        plugins_b = resolve_plugins_for_tier("fast_round")
        assert plugins_a == plugins_b
        assert plugins_a is not plugins_b


# ==================================================================
# profiles: extract_commands_from_stage
# ==================================================================

class TestExtractCommandsFromStage:
    def test_basic_extraction(self):
        stage = FakeStage()
        commands = extract_commands_from_stage(stage, ["lint", "test"])
        assert commands == {"lint": ["ruff check ."], "test": ["pytest -x"]}

    def test_only_requested_plugins(self):
        stage = FakeStage()
        commands = extract_commands_from_stage(stage, ["lint"])
        assert "lint" in commands
        assert "test" not in commands

    def test_empty_commands_omitted(self):
        stage = FakeStage(lint_commands=[], test_commands=[])
        commands = extract_commands_from_stage(stage, ["lint", "test"])
        assert commands == {}

    def test_perf_and_harness(self):
        stage = FakeStage(
            perf_checks=["perf stat ./bench"],
            gate_commands_remote=["./run_remote.sh"],
        )
        commands = extract_commands_from_stage(stage, ["perf", "harness"])
        assert commands == {"perf": ["perf stat ./bench"], "harness": ["./run_remote.sh"]}

    def test_remote_preflight_not_extracted(self):
        stage = FakeStage()
        commands = extract_commands_from_stage(stage, ["remote_preflight"])
        assert commands == {}

    def test_unknown_plugin_ignored(self):
        stage = FakeStage()
        commands = extract_commands_from_stage(stage, ["unknown_plugin"])
        assert commands == {}


# ==================================================================
# DefaultCheckRunner — plugin integration
# ==================================================================

class TestDefaultCheckRunnerPlugins:
    def _make_runner(self, registry: CheckPluginRegistry | None = None):
        from adapters.check_runner import DefaultCheckRunner
        return DefaultCheckRunner(
            remote_host="10.0.0.1",
            remote_workdir="/remote/ws",
            remote_host_secondary="10.0.0.2",
            remote_workdir_secondary="/remote/ws1",
            plugin_registry=registry,
        )

    def test_build_check_context(self):
        runner = self._make_runner()
        stage = FakeStage()
        ctx = runner._build_check_context(
            "worker_a", stage, Path("/tmp/ws"),
            gate_tier="pre_promotion",
            stage_budget_sec=600,
        )
        assert ctx.worker == "worker_a"
        assert ctx.stage_name == "test-stage"
        assert ctx.gate_tier == "pre_promotion"
        assert ctx.remote_host == "10.0.0.1"
        assert ctx.remote_workdir == "/remote/ws"
        assert ctx.stage_budget_sec == 600
        assert ctx.extra["stage"] is stage

    def test_build_check_context_split_worker(self):
        from adapters.check_runner import DefaultCheckRunner
        runner = DefaultCheckRunner(
            remote_host="10.0.0.1",
            remote_workdir="/remote/ws",
            remote_host_secondary="10.0.0.2",
            remote_workdir_secondary="/remote/ws1",
            split_worker_remote_endpoints=True,
        )
        stage = FakeStage(execution_env="remote_primary")
        ctx = runner._build_check_context("worker_b", stage, Path("/tmp/ws"))
        assert ctx.remote_host == "10.0.0.2"
        assert ctx.remote_workdir == "/remote/ws1"

    @pytest.mark.asyncio
    async def test_run_plugins_delegates_to_registry(self):
        registry = CheckPluginRegistry()
        lint_plugin = FakePlugin("lint")
        registry.register(lint_plugin)

        runner = self._make_runner(registry)
        stage = FakeStage()

        results = await runner.run_plugins(
            "worker_a", stage, Path("/tmp/ws"),
            plugin_names=["lint"],
            commands_by_plugin={"lint": ["ruff check ."]},
        )

        assert len(results) == 1
        assert results[0].passed is True
        assert lint_plugin.call_count == 1

    @pytest.mark.asyncio
    async def test_run_plugins_all(self):
        registry = CheckPluginRegistry()
        registry.register(FakePlugin("lint"))
        registry.register(FakePlugin("test"))

        runner = self._make_runner(registry)
        stage = FakeStage()

        results = await runner.run_plugins(
            "worker_a", stage, Path("/tmp/ws"),
            plugin_names=None,
        )

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_run_plugins_for_stage_default_profile(self):
        registry = CheckPluginRegistry()
        lint_plugin = FakePlugin("lint")
        test_plugin = FakePlugin("test")
        registry.register(lint_plugin)
        registry.register(test_plugin)

        runner = self._make_runner(registry)
        stage = FakeStage()

        result = await runner.run_plugins_for_stage(
            "worker_a", stage, Path("/tmp/ws"),
            gate_tier="fast_round",
        )

        # run_plugins_for_stage now returns AutoCheckResult
        from core.models import AutoCheckResult
        assert isinstance(result, AutoCheckResult)
        assert result.worker == "worker_a"
        assert result.stage_name == "test-stage"
        assert result.all_lint_passed is True
        assert result.all_tests_passed is True
        assert lint_plugin.last_commands == ["ruff check ."]
        assert test_plugin.last_commands == ["pytest -x"]

    @pytest.mark.asyncio
    async def test_run_plugins_for_stage_custom_profile(self):
        registry = CheckPluginRegistry()
        lint_plugin = FakePlugin("lint")
        test_plugin = FakePlugin("test")
        registry.register(lint_plugin)
        registry.register(test_plugin)

        runner = self._make_runner(registry)
        stage = FakeStage(check_profile={"fast_round": ["lint"]})

        result = await runner.run_plugins_for_stage(
            "worker_a", stage, Path("/tmp/ws"),
            gate_tier="fast_round",
        )

        from core.models import AutoCheckResult
        assert isinstance(result, AutoCheckResult)
        assert result.all_lint_passed is True
        assert test_plugin.call_count == 0

    @pytest.mark.asyncio
    async def test_run_plugins_for_stage_pre_promotion(self):
        registry = CheckPluginRegistry()
        for plugin_name in ["lint", "test", "perf", "harness"]:
            registry.register(FakePlugin(plugin_name))

        runner = self._make_runner(registry)
        stage = FakeStage(
            perf_checks=["bench run"],
            gate_commands_remote=["./gate.sh"],
        )

        result = await runner.run_plugins_for_stage(
            "worker_a", stage, Path("/tmp/ws"),
            gate_tier="pre_promotion",
        )

        from core.models import AutoCheckResult
        assert isinstance(result, AutoCheckResult)
        assert result.all_lint_passed is True
        assert result.all_tests_passed is True
        assert result.all_perf_passed is True
        assert result.all_harness_passed is True

    @pytest.mark.asyncio
    async def test_run_plugins_for_stage_with_timeout(self):
        registry = CheckPluginRegistry()
        registry.register(FakePlugin("lint", delay=5.0))
        registry.register(FakePlugin("test"))

        runner = self._make_runner(registry)
        stage = FakeStage()

        result = await runner.run_plugins_for_stage(
            "worker_a", stage, Path("/tmp/ws"),
            gate_tier="fast_round",
            timeout_sec=1,
        )

        from core.models import AutoCheckResult
        assert isinstance(result, AutoCheckResult)
        # lint timed out → all_lint_passed should be False
        assert result.all_lint_passed is False

    @pytest.mark.asyncio
    async def test_run_plugins_for_stage_failed_plugin(self):
        registry = CheckPluginRegistry()
        registry.register(FakePlugin("lint", should_pass=False))
        registry.register(FakePlugin("test", should_pass=True))

        runner = self._make_runner(registry)
        stage = FakeStage()

        result = await runner.run_plugins_for_stage(
            "worker_a", stage, Path("/tmp/ws"),
            gate_tier="fast_round",
        )

        from core.models import AutoCheckResult
        assert isinstance(result, AutoCheckResult)
        assert result.all_lint_passed is False
        assert result.all_tests_passed is True
        assert len(result.lint_results) > 0
        assert not result.lint_results[0].passed

    @pytest.mark.asyncio
    async def test_run_plugins_for_stage_heartbeat_sink(self):
        registry = CheckPluginRegistry()
        lint_plugin = FakePlugin("lint")
        registry.register(lint_plugin)

        runner = self._make_runner(registry)
        stage = FakeStage()
        heartbeat_calls: list[dict] = []

        result = await runner.run_plugins_for_stage(
            "worker_a", stage, Path("/tmp/ws"),
            gate_tier="fast_round",
            heartbeat_sink=lambda payload: heartbeat_calls.append(payload),
        )

        from core.models import AutoCheckResult
        assert isinstance(result, AutoCheckResult)
        # Verify heartbeat_sink was stored in context.extra
        assert lint_plugin.last_context is not None
        assert "heartbeat_sink" in lint_plugin.last_context.extra
        assert callable(lint_plugin.last_context.extra["heartbeat_sink"])


# ==================================================================
# adapt: check_results_to_auto_check_result
# ==================================================================

from check_plugins.adapt import check_results_to_auto_check_result
from check_plugins.plugin import CheckResult as PluginCheckResult


class TestCheckResultsToAutoCheckResult:
    def test_empty_results(self):
        from core.models import AutoCheckResult
        result = check_results_to_auto_check_result(
            [], worker="worker_a", stage_name="s1",
        )
        assert isinstance(result, AutoCheckResult)
        assert result.all_tests_passed is True
        assert result.all_lint_passed is True
        assert result.all_perf_passed is True
        assert result.all_harness_passed is True

    def test_all_passed(self):
        results = [
            PluginCheckResult(
                plugin_name="lint", passed=True,
                commands=[{"command": "ruff check .", "exit_code": 0, "passed": True}],
            ),
            PluginCheckResult(
                plugin_name="test", passed=True,
                commands=[{"command": "pytest", "exit_code": 0, "passed": True}],
            ),
        ]
        auto = check_results_to_auto_check_result(
            results, worker="worker_a", stage_name="s1",
        )
        assert auto.all_lint_passed is True
        assert auto.all_tests_passed is True
        assert len(auto.lint_results) == 1
        assert auto.lint_results[0].command == "ruff check ."
        assert auto.lint_results[0].passed is True
        assert len(auto.test_results) == 1

    def test_mixed_pass_fail(self):
        results = [
            PluginCheckResult(plugin_name="lint", passed=True, commands=[]),
            PluginCheckResult(
                plugin_name="test", passed=False,
                commands=[{"command": "pytest", "exit_code": 1, "passed": False, "stderr": "FAILED"}],
            ),
        ]
        auto = check_results_to_auto_check_result(
            results, worker="worker_a", stage_name="s1",
        )
        assert auto.all_lint_passed is True
        assert auto.all_tests_passed is False
        assert len(auto.test_results) == 1
        assert auto.test_results[0].passed is False

    def test_skipped_results_ignored(self):
        results = [
            PluginCheckResult(plugin_name="perf", passed=True, skipped=True, skip_reason="No commands"),
        ]
        auto = check_results_to_auto_check_result(
            results, worker="worker_a", stage_name="s1",
        )
        assert auto.all_perf_passed is True
        assert len(auto.perf_results) == 0

    def test_skipped_not_passed_is_fail_closed(self):
        """Plugin not registered → skipped=True, passed=False → must fail."""
        results = [
            PluginCheckResult(
                plugin_name="missing_plugin", passed=False, skipped=True,
                skip_reason="Plugin 'missing_plugin' not registered.",
            ),
        ]
        auto = check_results_to_auto_check_result(
            results, worker="worker_a", stage_name="s1",
        )
        # Should surface as a harness failure, not silently pass
        assert auto.all_harness_passed is False
        assert len(auto.harness_results) == 1
        assert auto.harness_results[0].passed is False
        assert "missing_plugin" in auto.harness_results[0].command
        assert "not registered" in auto.harness_results[0].stderr

    def test_skipped_not_passed_mixed_with_real_results(self):
        """Mix of real results and a missing plugin → overall should fail."""
        results = [
            PluginCheckResult(plugin_name="lint", passed=True, commands=[]),
            PluginCheckResult(plugin_name="test", passed=True, commands=[]),
            PluginCheckResult(
                plugin_name="custom_gate", passed=False, skipped=True,
                skip_reason="Plugin 'custom_gate' not registered.",
            ),
        ]
        auto = check_results_to_auto_check_result(
            results, worker="worker_a", stage_name="s1",
        )
        assert auto.all_lint_passed is True
        assert auto.all_tests_passed is True
        # Missing plugin should pull down harness
        assert auto.all_harness_passed is False
        assert len(auto.harness_results) == 1

    def test_harness_and_remote_preflight(self):
        results = [
            PluginCheckResult(
                plugin_name="harness", passed=True,
                commands=[{"command": "./gate.sh", "exit_code": 0, "passed": True}],
            ),
            PluginCheckResult(
                plugin_name="remote_preflight", passed=False,
                commands=[{"command": "preflight", "exit_code": 1, "passed": False}],
            ),
        ]
        auto = check_results_to_auto_check_result(
            results, worker="worker_b", stage_name="s2",
        )
        # Both map to harness_results
        assert len(auto.harness_results) == 2
        # remote_preflight failed → all_harness_passed should be False
        assert auto.all_harness_passed is False

    def test_unknown_plugin_maps_to_harness(self):
        results = [
            PluginCheckResult(plugin_name="custom_check", passed=True, commands=[]),
        ]
        auto = check_results_to_auto_check_result(
            results, worker="worker_a", stage_name="s1",
        )
        assert auto.all_harness_passed is True

    def test_synthesized_entry_when_no_commands(self):
        results = [
            PluginCheckResult(plugin_name="lint", passed=False, commands=[]),
        ]
        auto = check_results_to_auto_check_result(
            results, worker="worker_a", stage_name="s1",
        )
        assert auto.all_lint_passed is False
        assert len(auto.lint_results) == 1
        assert auto.lint_results[0].command == "[lint]"
        assert auto.lint_results[0].passed is False

    def test_downstream_field_access_compatible(self):
        """Verify the result is fully compatible with downstream code patterns."""
        results = [
            PluginCheckResult(
                plugin_name="lint", passed=True,
                commands=[{"command": "ruff", "exit_code": 0, "passed": True, "stdout": "ok", "stderr": ""}],
            ),
            PluginCheckResult(
                plugin_name="test", passed=True,
                commands=[{"command": "pytest", "exit_code": 0, "passed": True, "stdout": "ok", "stderr": ""}],
            ),
            PluginCheckResult(plugin_name="perf", passed=True, skipped=True),
            PluginCheckResult(plugin_name="harness", passed=True, skipped=True),
        ]
        auto = check_results_to_auto_check_result(
            results, worker="worker_a", stage_name="stage-1",
        )

        # Simulate downstream access patterns from round_runner.py
        all_checks_passed = (
            auto.all_tests_passed
            and auto.all_lint_passed
            and auto.all_perf_passed
            and auto.all_harness_passed
        )
        assert all_checks_passed is True

        # Simulate runtime_artifacts.py iteration
        for check_type, category_results in (
            ("test", auto.test_results),
            ("lint", auto.lint_results),
            ("perf", auto.perf_results),
            ("harness", auto.harness_results),
        ):
            for cmd_result in category_results:
                assert hasattr(cmd_result, "command")
                assert hasattr(cmd_result, "exit_code")
                assert hasattr(cmd_result, "passed")
                assert hasattr(cmd_result, "stdout")
                assert hasattr(cmd_result, "stderr")


# ==================================================================
# Round-level regression: _run_stage_checks with plugin path
# ==================================================================

class TestRunStageChecksPluginPath:
    """Verify that _run_stage_checks prefers run_plugins_for_stage
    and that the returned AutoCheckResult is compatible with downstream."""

    @pytest.mark.asyncio
    async def test_plugin_path_preferred_over_legacy(self):
        from types import SimpleNamespace
        from core.models import AutoCheckResult, StageSpec
        from orchestrator.round_runner import _run_stage_checks

        stage = StageSpec(
            name="stage-plugin",
            objective="test plugin path",
            acceptance_criteria=["done"],
            invariants=["stable"],
        )
        plugin_called = False
        legacy_called = False

        class PluginCheckRunner:
            async def run_plugins_for_stage(
                self, worker, stage_obj, workspace, *, gate_tier="fast_round", heartbeat_sink=None, **kwargs
            ) -> AutoCheckResult:
                nonlocal plugin_called
                plugin_called = True
                return AutoCheckResult(
                    worker=worker,
                    stage_name=stage_obj.name,
                    all_tests_passed=True,
                    all_lint_passed=True,
                    all_perf_passed=True,
                    all_harness_passed=True,
                )

            async def run_stage_checks(self, worker, stage_obj, workspace, **kwargs) -> AutoCheckResult:
                nonlocal legacy_called
                legacy_called = True
                return AutoCheckResult(worker=worker, stage_name=stage_obj.name)

        flow = SimpleNamespace(
            state=SimpleNamespace(max_round_per_stage=4),
            check_runner=PluginCheckRunner(),
            _remaining_stage_budget_sec=lambda **kwargs: 1200,
        )

        result = await _run_stage_checks(
            flow,
            worker="worker_a",
            stage=stage,
            workspace="/tmp/ws",
            gate_tier="fast_round",
            round_index=1,
            stage_deadline_monotonic=99999.0,
        )

        assert plugin_called is True
        assert legacy_called is False
        assert isinstance(result, AutoCheckResult)
        assert result.all_tests_passed is True

    @pytest.mark.asyncio
    async def test_legacy_fallback_when_no_plugin_method(self):
        from types import SimpleNamespace
        from core.models import AutoCheckResult, StageSpec
        from orchestrator.round_runner import _run_stage_checks

        stage = StageSpec(
            name="stage-legacy",
            objective="test legacy fallback",
            acceptance_criteria=["done"],
            invariants=["stable"],
        )
        legacy_called = False

        class LegacyOnlyCheckRunner:
            async def run_stage_checks(
                self, worker, stage_obj, workspace, *, gate_tier="fast_round", **kwargs
            ) -> AutoCheckResult:
                nonlocal legacy_called
                legacy_called = True
                return AutoCheckResult(worker=worker, stage_name=stage_obj.name)

        flow = SimpleNamespace(
            state=SimpleNamespace(max_round_per_stage=4),
            check_runner=LegacyOnlyCheckRunner(),
            _remaining_stage_budget_sec=lambda **kwargs: 1200,
        )

        result = await _run_stage_checks(
            flow,
            worker="worker_a",
            stage=stage,
            workspace="/tmp/ws",
            gate_tier="fast_round",
            round_index=1,
            stage_deadline_monotonic=99999.0,
        )

        assert legacy_called is True
        assert isinstance(result, AutoCheckResult)

    @pytest.mark.asyncio
    async def test_plugin_path_passes_heartbeat_sink(self):
        from types import SimpleNamespace
        from core.models import AutoCheckResult, StageSpec
        from orchestrator.round_runner import _run_stage_checks

        stage = StageSpec(
            name="stage-hb",
            objective="test heartbeat",
            acceptance_criteria=["done"],
            invariants=["stable"],
        )
        received_heartbeat_sink = "NOT_SET"  # sentinel to detect if param was forwarded
        plugin_called = False

        class PluginCheckRunner:
            async def run_plugins_for_stage(
                self, worker, stage_obj, workspace, *, gate_tier="fast_round", heartbeat_sink=None, **kwargs
            ) -> AutoCheckResult:
                nonlocal received_heartbeat_sink, plugin_called
                plugin_called = True
                received_heartbeat_sink = heartbeat_sink
                return AutoCheckResult(
                    worker=worker, stage_name=stage_obj.name,
                    all_tests_passed=True, all_lint_passed=True,
                    all_perf_passed=True, all_harness_passed=True,
                )

        flow = SimpleNamespace(
            state=SimpleNamespace(max_round_per_stage=4),
            check_runner=PluginCheckRunner(),
            _remaining_stage_budget_sec=lambda **kwargs: 1200,
        )

        await _run_stage_checks(
            flow,
            worker="worker_a",
            stage=stage,
            workspace="/tmp/ws",
            gate_tier="fast_round",
            round_index=1,
            stage_deadline_monotonic=99999.0,
        )

        # Verify the plugin path was actually called and heartbeat_sink was forwarded.
        assert plugin_called is True
        # heartbeat_sink is built by _build_remote_check_heartbeat_sink;
        # in this minimal test flow it resolves to a callable (or None).
        # The key point: the sentinel was overwritten, proving the param was forwarded.
        assert received_heartbeat_sink != "NOT_SET"


# ==================================================================
# HarnessCheckPlugin — remote execution plane
# ==================================================================

class TestHarnessCheckPluginRemotePath:
    """Verify HarnessCheckPlugin delegates to the full remote execution
    plane when context.remote_host is set and stage.execution_env
    indicates remote execution."""

    @pytest.mark.asyncio
    async def test_remote_path_triggered_when_remote_host_set(self, monkeypatch):
        """When remote_host is set and execution_env is remote_primary,
        the plugin should call remote primitives instead of local _run_command_list."""
        from check_plugins.builtin import HarnessCheckPlugin
        from core.models import CheckCommandResult

        sync_calls: list[tuple] = []
        remote_exec_calls: list[tuple] = []
        heartbeat_calls: list[dict] = []

        def fake_validate_preconditions(stage, *args, **kwargs):
            return []  # no errors

        def fake_resolve_sync_targets(stage, *args, **kwargs):
            return [("10.0.0.1", "/remote/ws/worker_a")]

        def fake_sync_to_remote(workspace, host, workdir, **kwargs):
            sync_calls.append((host, workdir))
            return CheckCommandResult(command=f"rsync to {host}", exit_code=0, passed=True)

        def fake_cleanup(host, workdir):
            return CheckCommandResult(command=f"cleanup {host}", exit_code=0, passed=True)

        def fake_resolve_remote_targets(stage, *args, **kwargs):
            return [("10.0.0.1", "/remote/ws/worker_a")]

        def fake_check_workdir(host, workdir, timeout=30):
            return CheckCommandResult(command=f"test -d {workdir}", exit_code=0, passed=True)

        def fake_run_remote_command_list(commands, host, workdir, **kwargs):
            remote_exec_calls.append((commands, host, workdir, kwargs.get("heartbeat_sink")))
            return [
                CheckCommandResult(command=cmd, exit_code=0, passed=True, stdout="ok")
                for cmd in commands
            ]

        def fake_worker_scoped(base, worker):
            return f"{base}/{worker}" if base else ""

        def fake_should_preserve(stage, tier):
            return False

        def fake_cache_paths(stage):
            return []

        def fake_timeout(**kwargs):
            return 600

        checks_mod = "check_plugins.builtin"
        monkeypatch.setattr(f"{checks_mod}._validate_remote_preconditions", fake_validate_preconditions)
        monkeypatch.setattr(f"{checks_mod}._resolve_sync_targets", fake_resolve_sync_targets)
        monkeypatch.setattr(f"{checks_mod}._sync_to_remote", fake_sync_to_remote)
        monkeypatch.setattr(f"{checks_mod}._cleanup_remote_path_sensitive_metadata", fake_cleanup)
        monkeypatch.setattr(f"{checks_mod}._resolve_remote_targets", fake_resolve_remote_targets)
        monkeypatch.setattr(f"{checks_mod}._check_remote_workdir_exists", fake_check_workdir)
        monkeypatch.setattr(f"{checks_mod}._run_remote_command_list", fake_run_remote_command_list)
        monkeypatch.setattr(f"{checks_mod}._worker_scoped_remote_workdir", fake_worker_scoped)
        monkeypatch.setattr(f"{checks_mod}._should_preserve_remote_cache", fake_should_preserve)
        monkeypatch.setattr(f"{checks_mod}._resolve_remote_cache_paths", fake_cache_paths)
        monkeypatch.setattr(f"{checks_mod}._resolve_effective_remote_command_timeout_sec", fake_timeout)

        stage = FakeStage(
            execution_env="remote_primary",
            sync_strategy="sync_to_remote_primary",
        )
        heartbeat_sink = lambda payload: heartbeat_calls.append(payload)
        context = CheckContext(
            worker="worker_a",
            stage_name="stage-remote",
            workspace=Path("/tmp/ws"),
            remote_host="10.0.0.1",
            remote_workdir="/remote/ws",
            extra={"stage": stage, "heartbeat_sink": heartbeat_sink},
        )

        plugin = HarnessCheckPlugin()
        result = await plugin.run(context, ["./run_gate.sh"])

        assert result.passed is True
        assert len(sync_calls) == 1
        assert sync_calls[0][0] == "10.0.0.1"
        assert len(remote_exec_calls) == 1
        assert remote_exec_calls[0][0] == ["./run_gate.sh"]
        # Verify heartbeat_sink was forwarded to remote execution
        assert remote_exec_calls[0][3] is heartbeat_sink

    @pytest.mark.asyncio
    async def test_local_fallback_when_no_remote_host(self, monkeypatch):
        """When remote_host is empty, the plugin should use local _run_command_list."""
        from check_plugins.builtin import HarnessCheckPlugin
        from core.models import CheckCommandResult

        local_calls: list[tuple] = []

        def fake_run_command_list(commands, workspace):
            local_calls.append((commands, workspace))
            return [
                CheckCommandResult(command=cmd, exit_code=0, passed=True)
                for cmd in commands
            ]

        monkeypatch.setattr(
            "check_plugins.builtin._run_command_list",
            fake_run_command_list,
        )

        stage = FakeStage(execution_env="local_only")
        context = CheckContext(
            worker="worker_a",
            stage_name="stage-local",
            workspace=Path("/tmp/ws"),
            extra={"stage": stage},
        )

        plugin = HarnessCheckPlugin()
        result = await plugin.run(context, ["make check"])

        assert result.passed is True
        assert len(local_calls) == 1

    @pytest.mark.asyncio
    async def test_local_fallback_when_execution_env_is_local(self, monkeypatch):
        """Even with remote_host set, if execution_env is local_only, use local path."""
        from check_plugins.builtin import HarnessCheckPlugin
        from core.models import CheckCommandResult

        local_calls: list[tuple] = []

        def fake_run_command_list(commands, workspace):
            local_calls.append((commands, workspace))
            return [
                CheckCommandResult(command=cmd, exit_code=0, passed=True)
                for cmd in commands
            ]

        monkeypatch.setattr(
            "check_plugins.builtin._run_command_list",
            fake_run_command_list,
        )

        stage = FakeStage(execution_env="local_only")
        context = CheckContext(
            worker="worker_a",
            stage_name="stage-local",
            workspace=Path("/tmp/ws"),
            remote_host="10.0.0.1",
            remote_workdir="/remote/ws",
            extra={"stage": stage},
        )

        plugin = HarnessCheckPlugin()
        result = await plugin.run(context, ["make check"])

        assert result.passed is True
        assert len(local_calls) == 1

    @pytest.mark.asyncio
    async def test_precondition_failure_returns_failed_result(self, monkeypatch):
        """When remote preconditions fail, the plugin should return a failed result."""
        from check_plugins.builtin import HarnessCheckPlugin
        from core.models import CheckCommandResult

        def fake_validate_preconditions(stage, *args, **kwargs):
            return [CheckCommandResult(
                command="remote-precondition:node0",
                exit_code=1,
                passed=False,
                stderr="--remote-host was not provided.",
            )]

        def fake_worker_scoped(base, worker):
            return f"{base}/{worker}" if base else ""

        checks_mod = "check_plugins.builtin"
        monkeypatch.setattr(f"{checks_mod}._validate_remote_preconditions", fake_validate_preconditions)
        monkeypatch.setattr(f"{checks_mod}._worker_scoped_remote_workdir", fake_worker_scoped)

        stage = FakeStage(execution_env="remote_primary")
        context = CheckContext(
            worker="worker_a",
            stage_name="stage-precond",
            workspace=Path("/tmp/ws"),
            remote_host="10.0.0.1",
            remote_workdir="/remote/ws",
            extra={"stage": stage},
        )

        plugin = HarnessCheckPlugin()
        result = await plugin.run(context, ["./gate.sh"])

        assert result.passed is False
        assert result.error_category == "remote_gate"

    @pytest.mark.asyncio
    async def test_sync_failure_skips_remote_exec(self, monkeypatch):
        """When sync fails, remote commands should be skipped for that target."""
        from check_plugins.builtin import HarnessCheckPlugin
        from core.models import CheckCommandResult

        remote_exec_calls: list[tuple] = []

        def fake_validate_preconditions(stage, *args, **kwargs):
            return []

        def fake_resolve_sync_targets(stage, *args, **kwargs):
            return [("10.0.0.1", "/remote/ws/worker_a")]

        def fake_sync_to_remote(workspace, host, workdir, **kwargs):
            return CheckCommandResult(command=f"rsync to {host}", exit_code=1, passed=False, stderr="rsync failed")

        def fake_resolve_remote_targets(stage, *args, **kwargs):
            return [("10.0.0.1", "/remote/ws/worker_a")]

        def fake_run_remote_command_list(commands, host, workdir, **kwargs):
            remote_exec_calls.append((commands, host))
            return []

        def fake_worker_scoped(base, worker):
            return f"{base}/{worker}" if base else ""

        def fake_should_preserve(stage, tier):
            return False

        def fake_cache_paths(stage):
            return []

        def fake_timeout(**kwargs):
            return 600

        checks_mod = "check_plugins.builtin"
        monkeypatch.setattr(f"{checks_mod}._validate_remote_preconditions", fake_validate_preconditions)
        monkeypatch.setattr(f"{checks_mod}._resolve_sync_targets", fake_resolve_sync_targets)
        monkeypatch.setattr(f"{checks_mod}._sync_to_remote", fake_sync_to_remote)
        monkeypatch.setattr(f"{checks_mod}._resolve_remote_targets", fake_resolve_remote_targets)
        monkeypatch.setattr(f"{checks_mod}._run_remote_command_list", fake_run_remote_command_list)
        monkeypatch.setattr(f"{checks_mod}._worker_scoped_remote_workdir", fake_worker_scoped)
        monkeypatch.setattr(f"{checks_mod}._should_preserve_remote_cache", fake_should_preserve)
        monkeypatch.setattr(f"{checks_mod}._resolve_remote_cache_paths", fake_cache_paths)
        monkeypatch.setattr(f"{checks_mod}._resolve_effective_remote_command_timeout_sec", fake_timeout)

        stage = FakeStage(
            execution_env="remote_primary",
            sync_strategy="sync_to_remote_primary",
        )
        context = CheckContext(
            worker="worker_a",
            stage_name="stage-sync-fail",
            workspace=Path("/tmp/ws"),
            remote_host="10.0.0.1",
            remote_workdir="/remote/ws",
            extra={"stage": stage},
        )

        plugin = HarnessCheckPlugin()
        result = await plugin.run(context, ["./gate.sh"])

        assert result.passed is False
        # Remote exec should NOT have been called since sync failed
        assert len(remote_exec_calls) == 0

    @pytest.mark.asyncio
    async def test_remote_path_triggered_for_node1_only_endpoint(self, monkeypatch):
        """remote_secondary should use remote path when only remote_host_secondary is set."""
        from check_plugins.builtin import HarnessCheckPlugin
        from core.models import CheckCommandResult

        remote_exec_calls: list[tuple] = []

        def fail_local_run(*args, **kwargs):
            raise AssertionError("local _run_command_list should not be used for remote_secondary remote path")

        def fake_validate_preconditions(stage, *args, **kwargs):
            return []

        def fake_resolve_sync_targets(stage, *args, **kwargs):
            return [("10.0.0.2", "/remote/ws1/worker_a")]

        def fake_sync_to_remote(workspace, host, workdir, **kwargs):
            return CheckCommandResult(command=f"rsync to {host}", exit_code=0, passed=True)

        def fake_cleanup(host, workdir):
            return CheckCommandResult(command=f"cleanup {host}", exit_code=0, passed=True)

        def fake_resolve_remote_targets(stage, *args, **kwargs):
            return [("10.0.0.2", "/remote/ws1/worker_a")]

        def fake_check_workdir(host, workdir, timeout=30):
            return CheckCommandResult(command=f"test -d {workdir}", exit_code=0, passed=True)

        def fake_run_remote_command_list(commands, host, workdir, **kwargs):
            remote_exec_calls.append((commands, host, workdir))
            return [CheckCommandResult(command=commands[0], exit_code=0, passed=True)]

        def fake_worker_scoped(base, worker):
            return f"{base}/{worker}" if base else ""

        checks_mod = "check_plugins.builtin"
        monkeypatch.setattr(f"{checks_mod}._run_command_list", fail_local_run)
        monkeypatch.setattr(f"{checks_mod}._validate_remote_preconditions", fake_validate_preconditions)
        monkeypatch.setattr(f"{checks_mod}._resolve_sync_targets", fake_resolve_sync_targets)
        monkeypatch.setattr(f"{checks_mod}._sync_to_remote", fake_sync_to_remote)
        monkeypatch.setattr(f"{checks_mod}._cleanup_remote_path_sensitive_metadata", fake_cleanup)
        monkeypatch.setattr(f"{checks_mod}._resolve_remote_targets", fake_resolve_remote_targets)
        monkeypatch.setattr(f"{checks_mod}._check_remote_workdir_exists", fake_check_workdir)
        monkeypatch.setattr(f"{checks_mod}._run_remote_command_list", fake_run_remote_command_list)
        monkeypatch.setattr(f"{checks_mod}._worker_scoped_remote_workdir", fake_worker_scoped)
        monkeypatch.setattr(f"{checks_mod}._should_preserve_remote_cache", lambda *args, **kwargs: False)
        monkeypatch.setattr(f"{checks_mod}._resolve_remote_cache_paths", lambda *args, **kwargs: [])
        monkeypatch.setattr(f"{checks_mod}._resolve_effective_remote_command_timeout_sec", lambda **kwargs: 600)

        stage = FakeStage(execution_env="remote_secondary", sync_strategy="sync_to_remote_secondary")
        context = CheckContext(
            worker="worker_a",
            stage_name="stage-node1",
            workspace=Path("/tmp/ws"),
            remote_host="",
            remote_workdir="/remote/ws",
            remote_host_secondary="10.0.0.2",
            remote_workdir_secondary="/remote/ws1",
            extra={"stage": stage},
        )

        plugin = HarnessCheckPlugin()
        result = await plugin.run(context, ["./gate.sh"])

        assert result.passed is True
        assert len(remote_exec_calls) == 1
        assert remote_exec_calls[0][1] == "10.0.0.2"

    @pytest.mark.asyncio
    async def test_remote_result_contract_validation_is_applied(self, monkeypatch):
        """Harness remote path should invoke _validate_remote_results to keep legacy semantics."""
        from check_plugins.builtin import HarnessCheckPlugin
        from core.models import CheckCommandResult

        called: dict[str, object] = {}

        def fake_validate_preconditions(stage, *args, **kwargs):
            return []

        def fake_resolve_sync_targets(stage, *args, **kwargs):
            return []

        def fake_resolve_remote_targets(stage, *args, **kwargs):
            return []

        def fake_validate_remote_results(stage, remote_results, *, gate_tier, selected_remote_commands):
            called["gate_tier"] = gate_tier
            called["selected_remote_commands"] = list(selected_remote_commands)
            called["remote_results_count"] = len(remote_results)
            return [CheckCommandResult(
                command="remote-gate",
                exit_code=1,
                passed=False,
                stderr="no remote commands were executed",
            )]

        checks_mod = "check_plugins.builtin"
        monkeypatch.setattr(f"{checks_mod}._validate_remote_preconditions", fake_validate_preconditions)
        monkeypatch.setattr(f"{checks_mod}._resolve_sync_targets", fake_resolve_sync_targets)
        monkeypatch.setattr(f"{checks_mod}._resolve_remote_targets", fake_resolve_remote_targets)
        monkeypatch.setattr(f"{checks_mod}._validate_remote_results", fake_validate_remote_results)
        monkeypatch.setattr(f"{checks_mod}._worker_scoped_remote_workdir", lambda base, worker: f"{base}/{worker}" if base else "")
        monkeypatch.setattr(f"{checks_mod}._should_preserve_remote_cache", lambda *args, **kwargs: False)
        monkeypatch.setattr(f"{checks_mod}._resolve_remote_cache_paths", lambda *args, **kwargs: [])
        monkeypatch.setattr(f"{checks_mod}._resolve_effective_remote_command_timeout_sec", lambda **kwargs: 600)

        stage = FakeStage(execution_env="remote_primary", sync_strategy="sync_to_remote_primary")
        context = CheckContext(
            worker="worker_a",
            stage_name="stage-contract",
            workspace=Path("/tmp/ws"),
            remote_host="10.0.0.1",
            remote_workdir="/remote/ws",
            extra={"stage": stage},
        )

        plugin = HarnessCheckPlugin()
        result = await plugin.run(context, ["./gate.sh"])

        assert result.passed is False
        assert result.error_category == "remote_gate"
        assert called["selected_remote_commands"] == ["./gate.sh"]
        assert called["gate_tier"] == "fast_round"
        assert called["remote_results_count"] == 0
