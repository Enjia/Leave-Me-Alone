"""Unified recovery-strategy module.

Maps a ``FailureClassification`` to a concrete recovery action that the
orchestrator can execute without ad-hoc branching.  This replaces the
scattered retry/backoff/degrade logic that was previously hardcoded in
``round_runner.py`` and ``stage_runner.py``.

The module is intentionally pure-functional: every function takes a
classification and returns a decision dict.  No I/O, no flow object.
"""
from __future__ import annotations

from core.models import FailureClassification

# ---------------------------------------------------------------------------
# Recovery action constants
# ---------------------------------------------------------------------------

ACTION_RETRY_IMMEDIATE = "retry_immediate"
ACTION_RETRY_WITH_BACKOFF = "retry_with_backoff"
ACTION_RETRY_NEXT_ROUND = "retry_next_round"
ACTION_REPAIR_REQUIRED = "repair_required"
ACTION_REPLAN = "replan"
ACTION_DEGRADE = "degrade"
ACTION_BLOCKED = "blocked"
ACTION_ROLLBACK_TO_SPEC = "rollback_to_spec"
ACTION_HOLD_PROMOTION = "hold_promotion"
ACTION_STOP_AND_REPLAN = "stop_and_replan"
ACTION_FIX_CONTRACT = "fix_contract_before_run"
ACTION_HUMAN_DECISION = "human_decision_required"

# ---------------------------------------------------------------------------
# Backoff parameters by category
# ---------------------------------------------------------------------------

_BACKOFF_PARAMS: dict[str, dict[str, int | float]] = {
    "rate_limited": {"base_delay_sec": 5, "max_delay_sec": 60, "max_attempts": 5},
    "request_timeout": {"base_delay_sec": 2, "max_delay_sec": 30, "max_attempts": 3},
    "connection_error": {"base_delay_sec": 3, "max_delay_sec": 30, "max_attempts": 3},
    "service_unavailable": {"base_delay_sec": 3, "max_delay_sec": 60, "max_attempts": 4},
    "transient_unknown": {"base_delay_sec": 2, "max_delay_sec": 30, "max_attempts": 3},
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def recommended_recovery(classification: FailureClassification) -> dict[str, object]:
    """Return a recovery decision for the given failure classification.

    The returned dict always contains:
      - ``action``: one of the ACTION_* constants
      - ``category``: the failure category
      - ``disposition``: the failure disposition
      - ``retryable``: whether the failure is retryable

    For retryable failures it also contains:
      - ``base_delay_sec``: initial backoff delay
      - ``max_delay_sec``: maximum backoff delay
      - ``max_attempts``: maximum retry attempts
    """
    category = classification.category
    code = classification.code

    # --- Transient (retriable with backoff) ---
    if category == "transient":
        backoff = _BACKOFF_PARAMS.get(code, _BACKOFF_PARAMS["transient_unknown"])
        return {
            "action": ACTION_RETRY_WITH_BACKOFF,
            "category": category,
            "disposition": classification.disposition,
            "retryable": True,
            **backoff,
        }

    # --- Timeout ---
    if category == "timeout":
        return {
            "action": ACTION_RETRY_NEXT_ROUND,
            "category": category,
            "disposition": classification.disposition,
            "retryable": True,
            "base_delay_sec": 0,
            "max_delay_sec": 0,
            "max_attempts": 2,
        }

    # --- Policy (budget, governance) ---
    if category == "policy":
        return {
            "action": ACTION_BLOCKED,
            "category": category,
            "disposition": "blocked",
            "retryable": False,
        }

    # --- Human decision required ---
    if category == "human_decision":
        return {
            "action": ACTION_HUMAN_DECISION,
            "category": category,
            "disposition": "blocked",
            "retryable": False,
        }

    # --- Input contract violation ---
    if category == "input_contract":
        return {
            "action": ACTION_FIX_CONTRACT,
            "category": category,
            "disposition": "blocked",
            "retryable": False,
        }

    # --- Spec gap ---
    if category == "spec_gap":
        return {
            "action": ACTION_ROLLBACK_TO_SPEC,
            "category": category,
            "disposition": classification.disposition,
            "retryable": False,
        }

    # --- Structured output parse failure ---
    if category == "structured_output":
        return {
            "action": ACTION_RETRY_IMMEDIATE,
            "category": category,
            "disposition": "retry_same_round",
            "retryable": True,
            "base_delay_sec": 0,
            "max_delay_sec": 0,
            "max_attempts": 3,
        }

    # --- Planner failure ---
    if category == "planner":
        return {
            "action": ACTION_REPLAN,
            "category": category,
            "disposition": classification.disposition,
            "retryable": True,
            "base_delay_sec": 0,
            "max_delay_sec": 0,
            "max_attempts": 2,
        }

    # --- Workspace state corruption ---
    if category == "workspace_state":
        return {
            "action": ACTION_STOP_AND_REPLAN,
            "category": category,
            "disposition": "terminal",
            "retryable": False,
        }

    # --- Promotion failure ---
    if category == "promotion":
        return {
            "action": ACTION_HOLD_PROMOTION,
            "category": category,
            "disposition": classification.disposition,
            "retryable": False,
        }

    # --- Remote gate / artifact contract / automated checks ---
    if category in ("remote_gate", "artifact_contract", "automated_checks"):
        return {
            "action": ACTION_REPAIR_REQUIRED,
            "category": category,
            "disposition": "repair_required",
            "retryable": False,
        }

    # --- Fallback ---
    return {
        "action": ACTION_REPAIR_REQUIRED,
        "category": category,
        "disposition": classification.disposition,
        "retryable": classification.retryable,
    }


def compute_backoff_delay(
    attempt: int,
    base_delay_sec: float = 2.0,
    max_delay_sec: float = 30.0,
) -> float:
    """Compute exponential backoff delay for a given attempt number (1-based)."""
    delay = min(base_delay_sec * (2 ** (attempt - 1)), max_delay_sec)
    return float(delay)
