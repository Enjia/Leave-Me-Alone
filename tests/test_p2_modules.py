"""Tests for P2 modules: config, concurrency, observability.

Covers:
- config/env_registry: EnvVar, register, read_env_*, registered_env_vars
- concurrency: gather_with_cancel, wait_first_exception, ConcurrencyLimiter
- observability/trace_context: TraceContext, new_trace, child_span, with_trace
- observability/structured_logging: StructuredLogger, structured_log
"""
from __future__ import annotations

import asyncio
import logging
import os
from unittest.mock import patch

import pytest

# ==================================================================
# P2-1: config/env_registry
# ==================================================================

from config.env_registry import (
    EnvVar,
    read_env,
    read_env_bool,
    read_env_int,
    read_env_str,
    read_non_negative_int,
    read_positive_int,
    register,
    registered_env_vars,
)


class TestEnvVar:
    def test_descriptor_fields(self):
        var = EnvVar(
            name="TEST_VAR",
            var_type="positive_int",
            default="42",
            description="A test variable",
            sensitive=True,
            tags=("test",),
        )
        assert var.name == "TEST_VAR"
        assert var.var_type == "positive_int"
        assert var.default == "42"
        assert var.sensitive is True
        assert var.tags == ("test",)

    def test_frozen(self):
        var = EnvVar(name="X")
        with pytest.raises(AttributeError):
            var.name = "Y"  # type: ignore[misc]


class TestReadEnvStr:
    def test_from_env(self):
        with patch.dict(os.environ, {"MY_STR_VAR": "  hello  "}):
            assert read_env_str("MY_STR_VAR") == "hello"

    def test_default(self):
        result = read_env_str("NONEXISTENT_STR_VAR_12345", "fallback")
        assert result == "fallback"

    def test_overrides(self):
        result = read_env_str("ANY_VAR", overrides={"ANY_VAR": " overridden "})
        assert result == "overridden"


class TestReadEnvInt:
    def test_from_env(self):
        with patch.dict(os.environ, {"MY_INT_VAR": "42"}):
            assert read_env_int("MY_INT_VAR", 0) == 42

    def test_invalid_returns_default(self):
        with patch.dict(os.environ, {"MY_INT_VAR": "not_a_number"}):
            assert read_env_int("MY_INT_VAR", 99) == 99

    def test_minimum_enforcement(self):
        with patch.dict(os.environ, {"MY_INT_VAR": "-5"}):
            assert read_env_int("MY_INT_VAR", 10, minimum=0) == 10

    def test_overrides(self):
        result = read_env_int("X", 0, overrides={"X": "77"})
        assert result == 77


class TestReadPositiveInt:
    def test_positive_value(self):
        with patch.dict(os.environ, {"POS_VAR": "5"}):
            assert read_positive_int("POS_VAR", 1) == 5

    def test_zero_returns_default(self):
        with patch.dict(os.environ, {"POS_VAR": "0"}):
            assert read_positive_int("POS_VAR", 10) == 10

    def test_negative_returns_default(self):
        with patch.dict(os.environ, {"POS_VAR": "-3"}):
            assert read_positive_int("POS_VAR", 10) == 10


class TestReadNonNegativeInt:
    def test_zero_allowed(self):
        with patch.dict(os.environ, {"NN_VAR": "0"}):
            assert read_non_negative_int("NN_VAR", 5) == 0

    def test_negative_returns_default(self):
        with patch.dict(os.environ, {"NN_VAR": "-1"}):
            assert read_non_negative_int("NN_VAR", 5) == 5


class TestReadEnvBool:
    @pytest.mark.parametrize("raw,expected", [
        ("1", True), ("true", True), ("True", True), ("YES", True),
        ("0", False), ("false", False), ("no", False), ("", False),
    ])
    def test_truthy_falsy(self, raw: str, expected: bool):
        with patch.dict(os.environ, {"BOOL_VAR": raw}):
            assert read_env_bool("BOOL_VAR") is expected

    def test_default_false(self):
        assert read_env_bool("NONEXISTENT_BOOL_VAR_12345") is False

    def test_overrides(self):
        assert read_env_bool("X", overrides={"X": "yes"}) is True


class TestReadEnv:
    def test_registered_var(self):
        register(EnvVar(name="TEST_REG_VAR_99", default="default_val"))
        result = read_env("TEST_REG_VAR_99")
        assert result == "default_val"

    def test_unregistered_var(self):
        result = read_env("TOTALLY_UNKNOWN_VAR_99")
        assert result == ""


class TestRegisteredEnvVars:
    def test_returns_list(self):
        all_vars = registered_env_vars()
        assert isinstance(all_vars, list)
        assert len(all_vars) > 0
        assert all(isinstance(v, EnvVar) for v in all_vars)

    def test_contains_standard_vars(self):
        names = {v.name for v in registered_env_vars()}
        assert "MULTI_CODEX_AGENT_TIMEOUT_SEC" in names
        assert "MULTI_CODEX_REMOTE_SSH_USER" in names


# ==================================================================
# P2-2: concurrency
# ==================================================================

from core.concurrency import (
    ConcurrencyLimiter,
    gather_with_cancel,
    wait_first_exception,
)


class TestGatherWithCancel:
    @pytest.mark.asyncio
    async def test_all_succeed(self):
        async def ok(value: int) -> int:
            return value

        results = await gather_with_cancel(ok(1), ok(2), ok(3))
        assert results == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_empty(self):
        results = await gather_with_cancel()
        assert results == []

    @pytest.mark.asyncio
    async def test_first_exception_cancels_others(self):
        cancelled = False

        async def slow() -> int:
            nonlocal cancelled
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled = True
                raise
            return 0

        async def fail() -> int:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await gather_with_cancel(slow(), fail())

        assert cancelled is True

    @pytest.mark.asyncio
    async def test_return_exceptions(self):
        async def ok() -> int:
            return 1

        async def fail() -> int:
            raise ValueError("err")

        results = await gather_with_cancel(ok(), fail(), return_exceptions=True)
        assert results[0] == 1
        assert isinstance(results[1], ValueError)


class TestWaitFirstException:
    @pytest.mark.asyncio
    async def test_all_succeed(self):
        async def ok(value: int) -> int:
            return value

        results = await wait_first_exception(ok(10), ok(20))
        assert results == [10, 20]

    @pytest.mark.asyncio
    async def test_empty(self):
        results = await wait_first_exception()
        assert results == []

    @pytest.mark.asyncio
    async def test_exception_propagates(self):
        async def fail() -> int:
            raise RuntimeError("fail")

        async def slow() -> int:
            await asyncio.sleep(10)
            return 0

        with pytest.raises(RuntimeError, match="fail"):
            await wait_first_exception(slow(), fail())


class TestConcurrencyLimiter:
    def test_invalid_max(self):
        with pytest.raises(ValueError):
            ConcurrencyLimiter(max_concurrent=0)

    @pytest.mark.asyncio
    async def test_context_manager(self):
        limiter = ConcurrencyLimiter(max_concurrent=2)
        assert limiter.active_count == 0

        async with limiter:
            assert limiter.active_count == 1

        assert limiter.active_count == 0

    @pytest.mark.asyncio
    async def test_run(self):
        limiter = ConcurrencyLimiter(max_concurrent=2)

        async def work() -> str:
            return "done"

        result = await limiter.run(work())
        assert result == "done"

    @pytest.mark.asyncio
    async def test_run_with_timeout(self):
        limiter = ConcurrencyLimiter(max_concurrent=1)

        async def slow() -> str:
            await asyncio.sleep(10)
            return "done"

        with pytest.raises(asyncio.TimeoutError):
            await limiter.run(slow(), timeout_sec=0.1)

    @pytest.mark.asyncio
    async def test_run_many(self):
        limiter = ConcurrencyLimiter(max_concurrent=2)
        max_active = 0

        async def track() -> int:
            nonlocal max_active
            max_active = max(max_active, limiter.active_count)
            await asyncio.sleep(0.05)
            return limiter.active_count

        results = await limiter.run_many([track() for _ in range(5)])
        assert len(results) == 5
        assert max_active <= 2

    @pytest.mark.asyncio
    async def test_properties(self):
        limiter = ConcurrencyLimiter(max_concurrent=3)
        assert limiter.max_concurrent == 3
        assert limiter.active_count == 0


# ==================================================================
# P2-3: observability/trace_context
# ==================================================================

from observability.trace_context import (
    TraceContext,
    child_span,
    current_trace,
    new_trace,
    with_trace,
)


class TestTraceContext:
    def test_new_trace(self):
        ctx = new_trace(stage_name="s1", round_index=2, worker="worker_a")
        assert len(ctx.trace_id) == 12
        assert len(ctx.span_id) == 12
        assert ctx.parent_span_id == ""
        assert ctx.stage_name == "s1"
        assert ctx.round_index == 2
        assert ctx.worker == "worker_a"

    def test_frozen(self):
        ctx = new_trace()
        with pytest.raises(AttributeError):
            ctx.trace_id = "new"  # type: ignore[misc]

    def test_custom_trace_id(self):
        ctx = new_trace(trace_id="custom123456")
        assert ctx.trace_id == "custom123456"


class TestChildSpan:
    def test_inherits_trace_id(self):
        parent = new_trace(stage_name="s1")
        with with_trace(parent):
            child = child_span()
            assert child.trace_id == parent.trace_id
            assert child.parent_span_id == parent.span_id
            assert child.span_id != parent.span_id
            assert child.stage_name == "s1"

    def test_override_fields(self):
        parent = new_trace(stage_name="s1", worker="worker_a")
        with with_trace(parent):
            child = child_span(stage_name="s2", worker="worker_b")
            assert child.stage_name == "s2"
            assert child.worker == "worker_b"
            assert child.trace_id == parent.trace_id

    def test_no_parent_creates_root(self):
        child = child_span(stage_name="orphan")
        assert child.parent_span_id == ""
        assert child.stage_name == "orphan"


class TestWithTrace:
    def test_sets_and_resets(self):
        assert current_trace() is None

        ctx = new_trace()
        with with_trace(ctx):
            assert current_trace() is ctx

        assert current_trace() is None

    def test_nested(self):
        parent = new_trace()
        with with_trace(parent):
            assert current_trace() is parent
            child = child_span()
            with with_trace(child):
                assert current_trace() is child
            assert current_trace() is parent

        assert current_trace() is None


# ==================================================================
# P2-3: observability/structured_logging
# ==================================================================

from observability.structured_logging import (
    StructuredLogger,
    get_structured_logger,
    structured_log,
)


class TestStructuredLog:
    def test_emits_log_with_trace(self, caplog):
        ctx = new_trace(stage_name="test-stage", round_index=1)
        logger = logging.getLogger("test.structured")
        with with_trace(ctx):
            with caplog.at_level(logging.INFO, logger="test.structured"):
                structured_log(logger, logging.INFO, "hello", key="value")

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "hello" in record.message
        assert hasattr(record, "structured")
        fields = record.structured
        assert fields["trace_id"] == ctx.trace_id
        assert fields["stage"] == "test-stage"
        assert fields["key"] == "value"

    def test_emits_without_trace(self, caplog):
        logger = logging.getLogger("test.no_trace")
        with caplog.at_level(logging.INFO, logger="test.no_trace"):
            structured_log(logger, logging.INFO, "no trace")

        assert len(caplog.records) == 1
        fields = caplog.records[0].structured
        assert "trace_id" not in fields


class TestStructuredLogger:
    def test_all_levels(self, caplog):
        slogger = get_structured_logger("test.levels")
        with caplog.at_level(logging.DEBUG, logger="test.levels"):
            slogger.debug("d")
            slogger.info("i")
            slogger.warning("w")
            slogger.error("e")

        assert len(caplog.records) == 4

    def test_name(self):
        slogger = get_structured_logger("my.module")
        assert slogger.name == "my.module"

    def test_extra_fields(self, caplog):
        slogger = get_structured_logger("test.extra")
        with caplog.at_level(logging.INFO, logger="test.extra"):
            slogger.info("check done", plugin="lint", duration=1.5)

        fields = caplog.records[0].structured
        assert fields["plugin"] == "lint"
        assert fields["duration"] == 1.5
