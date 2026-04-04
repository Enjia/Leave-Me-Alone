from __future__ import annotations

from dataclasses import dataclass

from agents.agent_env_config import load_agent_runtime_env
from agents.agent_bundle import AgentBundle
from agents.agents import create_agent_bundle


@dataclass
class _Cfg:
    provider: str = "codex"


@dataclass
class _Manager:
    value: str = "manager"


def test_load_agent_runtime_env_defaults_and_invalid(monkeypatch) -> None:
    monkeypatch.setenv("MULTI_CODEX_AGENT_TIMEOUT_SEC", "bad")
    monkeypatch.setenv("MULTI_CODEX_AGENT_IDLE_TIMEOUT_SEC", "-1")

    env = load_agent_runtime_env()

    assert env.timeout_sec == 10_800
    assert env.idle_timeout_sec == 600


def test_create_agent_bundle_delegates_to_factory(monkeypatch) -> None:
    cfg = _Cfg()
    manager = _Manager()
    sentinel = object()

    def _factory(runtime_cfg, runtime_manager):
        assert runtime_cfg is cfg
        assert runtime_manager is manager
        return sentinel

    monkeypatch.setattr(
        "agents.agent_bundle_factory.create_agent_bundle_from_runtime",
        _factory,
    )

    result = create_agent_bundle(cfg, manager)

    assert result is sentinel


def test_agentbundle_is_still_reexported_via_agents_module() -> None:
    from agents.agents import AgentBundle as agents_bundle

    assert agents_bundle is AgentBundle
