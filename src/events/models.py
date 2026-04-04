"""Typed runtime event models.

Six event categories are defined, each as a Pydantic model with a
discriminator field ``event_type``.  All events share a common base that
carries a monotonic timestamp and an optional correlation id.

Usage::

    from events.models import StageStartedEvent, RuntimeEvent
    event = StageStartedEvent(stage_name="s1", target_repo="/tmp/repo")
    payload = event.model_dump_json()
"""
from __future__ import annotations

import time
from typing import Annotated, Literal, Union

from pydantic import Field

from core.models import StrictBaseModel


class _BaseEvent(StrictBaseModel):
    """Common fields shared by all runtime events."""
    timestamp_monotonic: float = Field(default_factory=time.monotonic)
    timestamp_iso: str = ""
    trace_id: str = ""
    span_id: str = ""


# ---------------------------------------------------------------------------
# Stage events
# ---------------------------------------------------------------------------

class StageStartedEvent(_BaseEvent):
    event_type: Literal["stage_started"] = "stage_started"
    stage_name: str
    target_repo: str = ""
    round_budget: int = 0


class StageFinishedEvent(_BaseEvent):
    event_type: Literal["stage_finished"] = "stage_finished"
    stage_name: str
    passed: bool
    rounds_used: int = 0
    overall_state: str = ""


# ---------------------------------------------------------------------------
# Round events
# ---------------------------------------------------------------------------

class RoundStartedEvent(_BaseEvent):
    event_type: Literal["round_started"] = "round_started"
    stage_name: str
    round_index: int
    phase: str = ""


class RoundFinishedEvent(_BaseEvent):
    event_type: Literal["round_finished"] = "round_finished"
    stage_name: str
    round_index: int
    gate_passed: bool = False
    phase: str = ""
    worker_states: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Agent call events
# ---------------------------------------------------------------------------

class AgentCallEvent(_BaseEvent):
    event_type: Literal["agent_call"] = "agent_call"
    stage_name: str
    round_index: int
    agent_role: str
    phase: str = ""
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


# ---------------------------------------------------------------------------
# Check result events
# ---------------------------------------------------------------------------

class CheckResultEvent(_BaseEvent):
    event_type: Literal["check_result"] = "check_result"
    stage_name: str
    round_index: int
    worker: str = ""
    check_kind: str = ""
    passed: bool = False
    failed_count: int = 0
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Policy events (budget / compression)
# ---------------------------------------------------------------------------

class PolicyEvent(_BaseEvent):
    event_type: Literal["policy"] = "policy"
    stage_name: str
    round_index: int = 0
    policy_kind: str = ""
    triggered: bool = False
    details: str = ""


# ---------------------------------------------------------------------------
# Cost events
# ---------------------------------------------------------------------------

class CostEvent(_BaseEvent):
    event_type: Literal["cost"] = "cost"
    stage_name: str = ""
    round_index: int = 0
    delta_usd: float = 0.0
    cumulative_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


# ---------------------------------------------------------------------------
# Discriminated union
# ---------------------------------------------------------------------------

RuntimeEvent = Annotated[
    Union[
        StageStartedEvent,
        StageFinishedEvent,
        RoundStartedEvent,
        RoundFinishedEvent,
        AgentCallEvent,
        CheckResultEvent,
        PolicyEvent,
        CostEvent,
    ],
    Field(discriminator="event_type"),
]
