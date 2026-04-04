from __future__ import annotations

import time

from config.env_registry import read_positive_int

def bump_metric(flow: object, key: str, amount: int = 1) -> None:
    current = int(flow.state.harness_metrics.get(key, 0))
    flow.state.harness_metrics[key] = current + amount
    flow._persist_harness_metrics()

def read_positive_env_int(key: str, default: int) -> int:
    """Backward-compatible wrapper around the centralized env registry."""
    return read_positive_int(key, default)

def resolve_stage_timeout_sec() -> int:
    return read_positive_int("MULTI_CODEX_STAGE_TIMEOUT_SEC", 10_800)

def resolve_agent_timeout_sec() -> int:
    return read_positive_int("MULTI_CODEX_AGENT_TIMEOUT_SEC", 10_800)

def remaining_stage_budget_sec(*, stage_name: str, stage_deadline_monotonic: float) -> int:
    remaining_float = stage_deadline_monotonic - time.monotonic()
    if remaining_float <= 0:
        raise RuntimeError(f"Stage '{stage_name}' exceeded timeout budget before next step.")
    return max(1, int(remaining_float))

# ---------------------------------------------------------------------------
# Cost-ledger helpers (delegated from FlowRuntimeFacadeMixin)
# ---------------------------------------------------------------------------

def record_usage(flow: object, **kwargs: object) -> None:
    ledger = getattr(flow, "cost_ledger", None)
    if ledger is not None:
        ledger.record(**kwargs)
    _emit_cost_event(flow, **kwargs)


def _emit_cost_event(flow: object, **kwargs: object) -> None:
    """Emit a CostEvent after each usage record (fail-silent)."""
    emit = getattr(flow, "_emit_event", None)
    if emit is None:
        return
    try:
        from events.models import CostEvent

        ledger = getattr(flow, "cost_ledger", None)
        cumulative_usd = 0.0
        if ledger is not None:
            snapshot = ledger.snapshot() if hasattr(ledger, "snapshot") else None
            if snapshot is not None:
                cumulative_usd = float(getattr(snapshot, "estimated_cost_usd", 0.0))

        emit(CostEvent(
            stage_name=str(kwargs.get("stage_name", "")),
            round_index=int(kwargs.get("round_index", 0)),
            delta_usd=float(kwargs.get("cost_usd", 0.0)),
            cumulative_usd=cumulative_usd,
            input_tokens=int(kwargs.get("input_tokens", 0)),
            output_tokens=int(kwargs.get("output_tokens", 0)),
            model=str(kwargs.get("model", "")),
        ))
    except Exception:
        pass


def get_cost_snapshot(flow: object) -> object | None:
    ledger = getattr(flow, "cost_ledger", None)
    return ledger.snapshot() if ledger is not None else None


def check_budget_hard_limit(flow: object) -> bool:
    ledger = getattr(flow, "cost_ledger", None)
    triggered = ledger.budget_hard_triggered if ledger is not None else False
    if triggered:
        _emit_policy_event(flow, policy_kind="budget_hard_limit", triggered=True, details="global hard budget exceeded")
    return triggered

def check_stage_budget(flow: object, stage_name: str) -> str | None:
    ledger = getattr(flow, "cost_ledger", None)
    result = ledger.check_stage_budget(stage_name) if ledger is not None else None
    if result is not None:
        _emit_policy_event(
            flow,
            policy_kind="stage_budget",
            triggered=True,
            stage_name=stage_name,
            details=result,
        )
    return result


def _emit_policy_event(
    flow: object,
    *,
    policy_kind: str,
    triggered: bool,
    stage_name: str = "",
    details: str = "",
) -> None:
    """Emit a PolicyEvent when a budget policy fires (fail-silent)."""
    emit = getattr(flow, "_emit_event", None)
    if emit is None:
        return
    try:
        from events.models import PolicyEvent

        emit(PolicyEvent(
            stage_name=stage_name,
            policy_kind=policy_kind,
            triggered=triggered,
            details=details,
        ))
    except Exception:
        pass

def persist_cost_ledger(flow: object) -> None:
    ledger = getattr(flow, "cost_ledger", None)
    if ledger is not None:
        ledger.persist(flow._artifact_path("harness", "cost_ledger.json"))


def reset_stage_cost(flow: object, stage_name: str) -> None:
    ledger = getattr(flow, "cost_ledger", None)
    if ledger is not None:
        ledger.reset_stage(stage_name)


def check_budget_for_agent_call(flow: object) -> None:
    """Raise BudgetExhaustedError if immediate_abort enforcement is active and budget exceeded."""
    ledger = getattr(flow, "cost_ledger", None)
    if ledger is not None:
        ledger.check_budget_for_agent_call()
