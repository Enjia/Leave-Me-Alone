"""Runtime event bus for leave-me-alone.

Public API
----------
- ``EventBus``      – File-backed JSONL event bus
- ``make_event_bus`` – Factory for creating EventBus instances
- ``RuntimeEvent``  – Discriminated union of all event types
- Event types: ``StageStartedEvent``, ``StageFinishedEvent``,
  ``RoundStartedEvent``, ``RoundFinishedEvent``, ``AgentCallEvent``,
  ``CheckResultEvent``, ``PolicyEvent``, ``CostEvent``
"""

from .event_bus import EventBus, make_event_bus
from .models import (
    AgentCallEvent,
    CheckResultEvent,
    CostEvent,
    PolicyEvent,
    RoundFinishedEvent,
    RoundStartedEvent,
    RuntimeEvent,
    StageFinishedEvent,
    StageStartedEvent,
)

__all__ = [
    "AgentCallEvent",
    "CheckResultEvent",
    "CostEvent",
    "EventBus",
    "PolicyEvent",
    "RoundFinishedEvent",
    "RoundStartedEvent",
    "RuntimeEvent",
    "StageFinishedEvent",
    "StageStartedEvent",
    "make_event_bus",
]
