"""Tests for the self-contained FlowLite runtime."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from runtime.flow_lite import FlowLite, start


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class SimpleState(BaseModel):
    counter: int = 0


class SyncFlow(FlowLite[SimpleState]):
    @start()
    def run(self) -> str:
        self.state.counter += 1
        return "sync-done"


class AsyncFlow(FlowLite[SimpleState]):
    @start()
    async def run(self) -> str:
        self.state.counter += 10
        return "async-done"


class NoStartFlow(FlowLite[SimpleState]):
    def run(self) -> str:
        return "no-start"


# ---------------------------------------------------------------------------
# State initialisation
# ---------------------------------------------------------------------------

def test_state_auto_initialised() -> None:
    flow = SyncFlow()
    assert isinstance(flow.state, SimpleState)
    assert flow.state.counter == 0


def test_state_is_mutable_before_kickoff() -> None:
    flow = SyncFlow()
    flow.state.counter = 42
    result = flow.kickoff()
    assert result == "sync-done"
    assert flow.state.counter == 43


# ---------------------------------------------------------------------------
# Sync kickoff
# ---------------------------------------------------------------------------

def test_sync_kickoff_returns_result() -> None:
    flow = SyncFlow()
    assert flow.kickoff() == "sync-done"
    assert flow.state.counter == 1


# ---------------------------------------------------------------------------
# Async kickoff (no running loop)
# ---------------------------------------------------------------------------

def test_async_kickoff_returns_result() -> None:
    flow = AsyncFlow()
    assert flow.kickoff() == "async-done"
    assert flow.state.counter == 10


# ---------------------------------------------------------------------------
# Async kickoff inside a running event loop (the High bug)
# ---------------------------------------------------------------------------

def test_async_kickoff_inside_running_loop() -> None:
    """Verify kickoff() works when called from within a running event loop.

    This is the scenario that previously crashed with
    ``RuntimeError: This event loop is already running``.
    """
    flow = AsyncFlow()

    async def _outer() -> str:
        # kickoff() is synchronous but the entry-point is async.
        # FlowLite must handle this without crashing.
        return flow.kickoff()

    result = asyncio.run(_outer())
    assert result == "async-done"
    assert flow.state.counter == 10


# ---------------------------------------------------------------------------
# Missing @start
# ---------------------------------------------------------------------------

def test_no_start_raises_runtime_error() -> None:
    flow = NoStartFlow()
    with pytest.raises(RuntimeError, match="no method decorated with @start"):
        flow.kickoff()


# ---------------------------------------------------------------------------
# Inheritance chain
# ---------------------------------------------------------------------------

class DerivedState(BaseModel):
    value: str = "init"


class BaseFlow(FlowLite[DerivedState]):
    @start()
    async def run(self) -> str:
        self.state.value = "base"
        return self.state.value


class ChildFlow(BaseFlow):
    """Inherits @start from BaseFlow."""
    pass


def test_child_flow_inherits_start_and_state() -> None:
    flow = ChildFlow()
    assert isinstance(flow.state, DerivedState)
    assert flow.kickoff() == "base"
    assert flow.state.value == "base"
