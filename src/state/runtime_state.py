from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RuntimePhaseState:
    stage_name: str
    round_index: int = 0
    phase: str = ""
    worker_states: dict[str, str] = field(default_factory=dict)
    judge_state: str = ""


@dataclass
class StageLoopState:
    stage_name: str
    max_round: int
    judge_feedback: list[str] = field(default_factory=list)
    prev_check_summary_a: str = ""
    prev_check_summary_b: str = ""
    no_progress_rounds: int = 0
    repeated_failure_rounds: int = 0
    prev_failure_signature: str = ""
