from __future__ import annotations

from pathlib import Path

import pytest

from agents.agent_bundle import AgentBundle


class _DummyAgent:
    def __init__(self, role: str) -> None:
        self.role = role


class _Adapter:
    def __init__(self, name: str, *, fail_on_start: bool = False) -> None:
        self.name = name
        self.fail_on_start = fail_on_start
        self.start_calls = 0
        self.shutdown_calls = 0

    def start(self) -> None:
        self.start_calls += 1
        if self.fail_on_start:
            raise RuntimeError(f"{self.name} boom")

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def _bundle(adapters: list[object] | None) -> AgentBundle:
    agent = _DummyAgent("judge")
    worker_a = _DummyAgent("worker_a")
    worker_b = _DummyAgent("worker_b")
    verifier = _DummyAgent("verifier")
    return AgentBundle(
        judge=agent,
        verifier=verifier,
        worker_a=worker_a,
        worker_b=worker_b,
        worker_a_workspace=Path("/tmp/worker_a"),
        worker_b_workspace=Path("/tmp/worker_b"),
        a2a_adapters=adapters,  # type: ignore[arg-type]
    )


def test_agent_bundle_start_a2a_rolls_back_started_adapters() -> None:
    first = _Adapter("first")
    second = _Adapter("second", fail_on_start=True)
    bundle = _bundle([first, second])

    with pytest.raises(RuntimeError, match="second boom"):
        bundle.start_a2a()

    assert first.start_calls == 1
    assert first.shutdown_calls == 1
    assert second.start_calls == 1
    assert second.shutdown_calls == 0


def test_agent_bundle_shutdown_a2a_calls_all_adapters() -> None:
    first = _Adapter("first")
    second = _Adapter("second")
    bundle = _bundle([first, second])

    bundle.shutdown_a2a()

    assert first.shutdown_calls == 1
    assert second.shutdown_calls == 1


def test_agent_bundle_start_a2a_noop_without_adapters() -> None:
    bundle = _bundle(None)
    bundle.start_a2a()
    bundle.shutdown_a2a()
