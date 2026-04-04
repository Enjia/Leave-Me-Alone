from __future__ import annotations

from dataclasses import dataclass

from config.env_registry import read_non_negative_int, read_positive_int

@dataclass(frozen=True)
class AgentRuntimeEnv:
    timeout_sec: int
    idle_timeout_sec: int

def load_agent_runtime_env() -> AgentRuntimeEnv:
    return AgentRuntimeEnv(
        timeout_sec=read_positive_int("MULTI_CODEX_AGENT_TIMEOUT_SEC", 10_800),
        idle_timeout_sec=read_non_negative_int("MULTI_CODEX_AGENT_IDLE_TIMEOUT_SEC", 600),
    )