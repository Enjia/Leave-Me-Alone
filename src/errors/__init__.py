"""Unified error taxonomy and recovery strategy module.

Public API
----------
- ``classify_transient_error``  – detect transient (retriable) errors
- ``classify_check_failure``    – classify a check command failure
- ``classify_budget_exhausted`` – classify budget-exhaustion events
- ``classify_human_decision_required`` – classify blocking human-decision events
- ``recommended_recovery``      – map a FailureClassification to a recovery action
- ``compute_backoff_delay``     – compute exponential backoff delay
"""

from .recovery import compute_backoff_delay, recommended_recovery
from .taxonomy import (
    classify_budget_exhausted,
    classify_check_failure,
    classify_human_decision_required,
    classify_transient_error,
)

__all__ = [
    "classify_budget_exhausted",
    "classify_check_failure",
    "classify_human_decision_required",
    "classify_transient_error",
    "compute_backoff_delay",
    "recommended_recovery",
]
