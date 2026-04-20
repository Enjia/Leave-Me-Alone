from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.models import (
    BudgetPolicy,
    CostSnapshot,
    ModelCostEntry,
    StageCostEntry,
    UsageRecord,
    WorkerCostEntry,
)

logger = logging.getLogger(__name__)


class BudgetExhaustedError(RuntimeError):
    """Raised when immediate_abort enforcement detects a hard budget breach."""

# Pricing per 1M tokens (USD).  Extend as needed.
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "o3": (10.0, 40.0),
    "o4-mini": (1.10, 4.40),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
}


def _estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a given model and token counts."""
    normalized = model.strip().lower()
    for key, (input_price, output_price) in _MODEL_PRICING.items():
        if key in normalized:
            return (input_tokens * input_price + output_tokens * output_price) / 1_000_000
    # Unknown model: use a conservative mid-range estimate.
    return (input_tokens * 3.0 + output_tokens * 12.0) / 1_000_000


class CostLedger:
    """Thread-safe accumulator for token usage and cost across a run.

    Records individual ``UsageRecord`` entries and provides aggregated
    snapshots by stage, model, and overall totals.
    """

    def __init__(self, budget_policy: BudgetPolicy | None = None) -> None:
        self._lock = threading.Lock()
        self._records: list[UsageRecord] = []
        self._budget_policy = budget_policy or BudgetPolicy()
        self._budget_warn_triggered = False
        self._budget_hard_triggered = False

    @property
    def budget_policy(self) -> BudgetPolicy:
        return self._budget_policy

    @property
    def budget_hard_triggered(self) -> bool:
        with self._lock:
            return self._budget_hard_triggered

    def record(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int = 0,
        model: str = "",
        agent_role: str = "",
        stage_name: str = "",
        latency_sec: float = 0.0,
    ) -> UsageRecord:
        """Record a single agent invocation's usage and return the entry."""
        if total_tokens <= 0:
            total_tokens = input_tokens + output_tokens

        entry = UsageRecord(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            model=model,
            agent_role=agent_role,
            stage_name=stage_name,
            latency_sec=round(latency_sec, 2),
            timestamp_iso=datetime.now(timezone.utc).isoformat(),
        )

        with self._lock:
            self._records.append(entry)
            self._check_budget_thresholds()

        return entry

    def snapshot(self) -> CostSnapshot:
        """Return a point-in-time aggregated cost snapshot."""
        with self._lock:
            return self._build_snapshot_locked()

    def stage_cost_usd(self, stage_name: str) -> float:
        """Return the estimated cost for a specific stage."""
        with self._lock:
            total = 0.0
            for record in self._records:
                if record.stage_name == stage_name:
                    total += _estimate_cost_usd(
                        record.model, record.input_tokens, record.output_tokens
                    )
            return total

    def check_stage_budget(self, stage_name: str) -> str | None:
        """Return a reason string if the stage has exceeded its budget, else None."""
        per_stage = self._budget_policy.per_stage_budget_usd
        if per_stage <= 0:
            return None
        current = self.stage_cost_usd(stage_name)
        if current >= per_stage:
            return (
                f"Stage '{stage_name}' cost ${current:.4f} exceeds "
                f"per-stage budget ${per_stage:.4f}"
            )
        return None

    def reset_stage(self, stage_name: str) -> None:
        """Remove all records for *stage_name* so per-stage budget restarts."""
        with self._lock:
            self._records = [r for r in self._records if r.stage_name != stage_name]

    def restore_from_json(self, artifact_path: Path) -> None:
        """Load historical records from a persisted cost_ledger.json file."""
        if not artifact_path.exists():
            return
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to restore cost ledger from %s: %s", artifact_path, exc)
            return
        raw_records = payload.get("records") or []
        if not isinstance(raw_records, list):
            return
        restored = 0
        with self._lock:
            for raw in raw_records:
                if not isinstance(raw, dict):
                    continue
                try:
                    entry = UsageRecord(**raw)
                except Exception:
                    continue
                self._records.append(entry)
                restored += 1
            if restored:
                self._check_budget_thresholds()
        if restored:
            logger.info("Restored %d historical usage records from %s", restored, artifact_path)

    def check_budget_for_agent_call(self) -> None:
        """Raise BudgetExhaustedError if hard budget is triggered and enforcement is immediate_abort."""
        if self._budget_policy.hard_budget_enforcement != "immediate_abort":
            return
        if self.budget_hard_triggered:
            snapshot = self.snapshot()
            raise BudgetExhaustedError(
                f"Hard budget ${self._budget_policy.hard_budget_usd:.4f} exceeded "
                f"(current: ${snapshot.estimated_cost_usd:.4f}). "
                f"Aborting agent call (enforcement=immediate_abort)."
            )

    def persist(self, artifact_path: Path) -> None:
        """Persist the current snapshot and raw records to a JSON file."""
        snapshot = self.snapshot()
        with self._lock:
            raw_records = [record.model_dump() for record in self._records]

        payload: dict[str, Any] = {
            "snapshot": snapshot.model_dump(),
            "records": raw_records,
        }
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _build_snapshot_locked(self) -> CostSnapshot:
        total_input = 0
        total_output = 0
        by_stage: dict[str, StageCostEntry] = {}
        by_worker: dict[str, WorkerCostEntry] = {}
        by_model: dict[str, ModelCostEntry] = {}

        for record in self._records:
            total_input += record.input_tokens
            total_output += record.output_tokens
            cost = _estimate_cost_usd(
                record.model, record.input_tokens, record.output_tokens
            )

            # Per-stage aggregation.
            stage_key = record.stage_name or "<global>"
            if stage_key not in by_stage:
                by_stage[stage_key] = StageCostEntry()
            stage_entry = by_stage[stage_key]
            stage_entry.input_tokens += record.input_tokens
            stage_entry.output_tokens += record.output_tokens
            stage_entry.total_tokens += record.total_tokens
            stage_entry.estimated_cost_usd += cost
            stage_entry.invocation_count += 1

            # Per-model aggregation.
            model_key = record.model or "<unknown>"
            if model_key not in by_model:
                by_model[model_key] = ModelCostEntry()
            model_entry = by_model[model_key]
            model_entry.input_tokens += record.input_tokens
            model_entry.output_tokens += record.output_tokens
            model_entry.total_tokens += record.total_tokens
            model_entry.estimated_cost_usd += cost
            model_entry.invocation_count += 1

            # Per-worker(role) aggregation.
            worker_key = self._worker_bucket(record.agent_role)
            if worker_key not in by_worker:
                by_worker[worker_key] = WorkerCostEntry()
            worker_entry = by_worker[worker_key]
            worker_entry.input_tokens += record.input_tokens
            worker_entry.output_tokens += record.output_tokens
            worker_entry.total_tokens += record.total_tokens
            worker_entry.estimated_cost_usd += cost
            worker_entry.invocation_count += 1

        total_tokens = total_input + total_output
        total_cost = sum(entry.estimated_cost_usd for entry in by_stage.values())

        return CostSnapshot(
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_tokens=total_tokens,
            estimated_cost_usd=round(total_cost, 6),
            by_stage=by_stage,
            by_worker=by_worker,
            by_model=by_model,
            invocation_count=len(self._records),
            budget_warn_triggered=self._budget_warn_triggered,
            budget_hard_triggered=self._budget_hard_triggered,
        )

    @staticmethod
    def _worker_bucket(agent_role: str) -> str:
        role = (agent_role or "").strip().lower()
        if not role:
            return "<unknown>"
        if "worker" in role:
            return "worker"
        if "judge" in role:
            return "judge"
        if "planner" in role:
            return "planner"
        return role

    def _check_budget_thresholds(self) -> None:
        """Check budget thresholds after each record (called under lock)."""
        snapshot = self._build_snapshot_locked()
        total_cost = snapshot.estimated_cost_usd

        warn_threshold = self._budget_policy.warn_budget_usd
        hard_threshold = self._budget_policy.hard_budget_usd

        if warn_threshold > 0 and total_cost >= warn_threshold and not self._budget_warn_triggered:
            self._budget_warn_triggered = True
            logger.warning(
                "BUDGET WARNING: Total estimated cost $%.4f reached warn threshold $%.4f",
                total_cost,
                warn_threshold,
            )

        if hard_threshold > 0 and total_cost >= hard_threshold and not self._budget_hard_triggered:
            self._budget_hard_triggered = True
            logger.error(
                "BUDGET HARD LIMIT: Total estimated cost $%.4f reached hard threshold $%.4f. "
                "New stages will be blocked.",
                total_cost,
                hard_threshold,
            )
