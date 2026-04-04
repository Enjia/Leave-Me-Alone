from __future__ import annotations

from pathlib import Path
from typing import Protocol


class AgentPort(Protocol):
    role: str

    def kickoff(self, prompt: str, *, response_format: object, timeout_override_sec: int | None = None) -> object:
        ...

    @property
    def workspace(self) -> Path:
        ...
