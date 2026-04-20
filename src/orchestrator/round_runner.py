from __future__ import annotations

import asyncio
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import threading
import time
from dataclasses import dataclass

from core.checks import format_check_summary
from core.concurrency import wait_first_exception
from ports.workspace import WorkspaceArtifactsLike
from core.models import (
    CheckSummaryArtifact,
    ConvergenceSignal,
    FailureClassification,
    FailureEventArtifact,
    FeatureChecklistArtifact,
    JudgeGateReview,
    PlanGateReview,
    PlanDriftArtifact,
    RuntimeStatusSnapshot,
    SelfReviewResult,
    StageContextPacket,
    StageExecutionPlan,
    StageResult,
    StageRoundLog,
    StageSpec,
    VerifierReport,
    WorkerDelivery,
    WorkerEntryPacket,
    WorkerPlan,
)
from core.prompts import (
    judge_gate_review_prompt,
    judge_independent_review_prompt,
    verifier_review_prompt,
    worker_self_review_prompt,
)


@dataclass(frozen=True)
class RoundPlanApproved:
    context_packet: StageContextPacket
    context_packet_json: str
    worker_entry_packet: WorkerEntryPacket
    worker_plan: WorkerPlan
    plan_gate_review: PlanGateReview

@dataclass(frozen=True)
class RoundPlanRejected:
    judge_feedback: list[str]

@dataclass(frozen=True)
class RoundDeliveryPhaseResult:
    worker_delivery: WorkerDelivery
    drift: PlanDriftArtifact
    check_summary: str
    check_artifact_post_impl: CheckSummaryArtifact
    patch_after_impl: WorkspaceArtifactsLike

@dataclass(frozen=True)
class RoundReviewGatePhaseResult:
    worker_self_review: SelfReviewResult
    judge_a_gate: JudgeGateReview
    judge_b_gate: JudgeGateReview
    verifier_report: VerifierReport
    final_gate: JudgeGateReview
    auto_checks: object
    check_summary: str
    check_artifact_post_review: CheckSummaryArtifact
    patch_final: WorkspaceArtifactsLike
    review_memory: list
    convergence_signal: ConvergenceSignal
    no_progress_rounds: int
    repeated_failure_rounds: int
    prev_failure_signature: str
    round_log: StageRoundLog

@dataclass(frozen=True)
class RoundContinueResult:
    judge_feedback: list[str]
    prev_check_summary: str
    review_baseline: object

@dataclass(frozen=True)
class RoundPassResult:
    stage_result: StageResult


def merge_gate_decisions(
    gate_a: JudgeGateReview,
    gate_b: JudgeGateReview,
    stage_name: str,
    round_index: int,
) -> JudgeGateReview:
    """Deterministic merge of two independent Judge gate decisions.

    Rules:
    - Both pass -> merged pass
    - Either fail -> merged fail
    - required_actions = deduplicated union
    - high_severity_open = deduplicated union
    - disputed_items = deduplicated union
    """
    merged_pass = gate_a.pass_gate and gate_b.pass_gate
    merged_actions = list(dict.fromkeys(
        list(gate_a.required_actions or []) + list(gate_b.required_actions or [])
    ))
    merged_high_severity = list(dict.fromkeys(
        list(gate_a.high_severity_open or []) + list(gate_b.high_severity_open or [])
    ))
    merged_disputed = list(dict.fromkeys(
        list(gate_a.disputed_items or []) + list(gate_b.disputed_items or [])
    ))

    rationale_parts = []
    if gate_a.rationale:
        rationale_parts.append(f"[Judge A] {gate_a.rationale}")
    if gate_b.rationale:
        rationale_parts.append(f"[Judge B] {gate_b.rationale}")
    if not merged_pass and gate_a.pass_gate != gate_b.pass_gate:
        dissenter = "Judge B" if gate_a.pass_gate else "Judge A"
        rationale_parts.append(f"Merged to FAIL because {dissenter} rejected.")

    return JudgeGateReview(
        stage_name=stage_name,
        round_index=round_index,
        pass_gate=merged_pass,
        high_severity_open=merged_high_severity,
        disputed_items=merged_disputed,
        required_actions=merged_actions,
        rationale="\n".join(rationale_parts),
    )

async def _await_worker_delivery(delivery: object) -> WorkerDelivery:
    """Await a single worker delivery coroutine."""
    return await delivery


async def _capture_workspace_artifacts(
    flow: object,
    *,
    baseline: WorkspaceArtifactsLike | object,
) -> WorkspaceArtifactsLike:
    patch = await asyncio.to_thread(
        flow.workspace_port.capture_artifacts,
        flow.agents.worker_workspace,
        baseline_snapshot=baseline,
    )
    return patch

_REMOTE_HEARTBEAT_FILE_LOCK = threading.Lock()


def _default_timeout_recovery_summary(now_epoch_sec: int) -> dict[str, object]:
    return {
        "updated_at_epoch_sec": now_epoch_sec,
        "totals": {
            "timeout_recovery_attempted": 0,
            "timeout_recovery_recovered": 0,
            "timeout_recovery_failed": 0,
            "stale_recycled": 0,
        },
        "by_stage": {},
        "by_worker": {},
        "by_gate_tier": {},
        "by_stage_worker_gate_tier": {},
        "recent_events": [],
        "processed_event_ids": [],
    }


def _update_timeout_recovery_summary(
    summary: dict[str, object],
    *,
    event_entry: dict[str, object],
    dedupe_event_limit: int = 500,
    recent_events_limit: int = 50,
) -> None:
    event = str(event_entry.get("event", "")).strip().lower()
    heartbeat_id = str(event_entry.get("heartbeat_id", "")).strip()
    if event not in {"timeout_recovery", "stale_timeout"} or not heartbeat_id:
        return
    dedupe_limit = max(1, int(dedupe_event_limit))
    recent_limit = max(1, int(recent_events_limit))

    processed_ids_raw = summary.get("processed_event_ids")
    processed_ids = [
        str(item) for item in processed_ids_raw
        if isinstance(item, (str, int, float))
    ] if isinstance(processed_ids_raw, list) else []
    event_id = f"{event}:{heartbeat_id}"
    if event_id in processed_ids:
        return
    processed_ids.insert(0, event_id)
    summary["processed_event_ids"] = processed_ids[:dedupe_limit]

    totals_raw = summary.get("totals")
    totals = dict(totals_raw) if isinstance(totals_raw, dict) else {}
    stage = str(event_entry.get("stage_name", "")).strip() or "unknown_stage"
    worker = str(event_entry.get("worker", "")).strip() or "unknown_worker"
    gate_tier = str(event_entry.get("gate_tier", "")).strip() or "unknown_gate_tier"

    def _bump_group(group_name: str, key: str, metric: str) -> None:
        group_raw = summary.get(group_name)
        group = dict(group_raw) if isinstance(group_raw, dict) else {}
        row_raw = group.get(key)
        row = dict(row_raw) if isinstance(row_raw, dict) else {
            "timeout_recovery_attempted": 0,
            "timeout_recovery_recovered": 0,
            "timeout_recovery_failed": 0,
            "stale_recycled": 0,
            "consecutive_failed_recoveries": 0,
            "max_consecutive_failed_recoveries": 0,
            "last_event_status": "",
        }
        row[metric] = int(row.get(metric, 0) or 0) + 1
        group[key] = row
        summary[group_name] = group

    def _update_scope_row(group_name: str, key: str, *, recovered: bool | None, status: str) -> None:
        group_raw = summary.get(group_name)
        group = dict(group_raw) if isinstance(group_raw, dict) else {}
        row_raw = group.get(key)
        row = dict(row_raw) if isinstance(row_raw, dict) else {
            "timeout_recovery_attempted": 0,
            "timeout_recovery_recovered": 0,
            "timeout_recovery_failed": 0,
            "stale_recycled": 0,
            "consecutive_failed_recoveries": 0,
            "max_consecutive_failed_recoveries": 0,
            "last_event_status": "",
        }
        if recovered is True:
            row["consecutive_failed_recoveries"] = 0
        elif recovered is False:
            row["consecutive_failed_recoveries"] = int(row.get("consecutive_failed_recoveries", 0) or 0) + 1
            row["max_consecutive_failed_recoveries"] = max(
                int(row.get("max_consecutive_failed_recoveries", 0) or 0),
                int(row["consecutive_failed_recoveries"]),
            )
        row["last_event_status"] = status
        group[key] = row
        summary[group_name] = group

    if event == "timeout_recovery":
        recovery_payload = event_entry.get("recovery")
        recovered = bool(recovery_payload.get("recovered")) if isinstance(recovery_payload, dict) else False
        status = str(event_entry.get("status", "")).strip()
        totals["timeout_recovery_attempted"] = int(totals.get("timeout_recovery_attempted", 0) or 0) + 1
        metric = "timeout_recovery_recovered" if recovered else "timeout_recovery_failed"
        totals[metric] = int(totals.get(metric, 0) or 0) + 1
        _bump_group("by_stage", stage, "timeout_recovery_attempted")
        _bump_group("by_stage", stage, metric)
        _bump_group("by_worker", worker, "timeout_recovery_attempted")
        _bump_group("by_worker", worker, metric)
        _bump_group("by_gate_tier", gate_tier, "timeout_recovery_attempted")
        _bump_group("by_gate_tier", gate_tier, metric)
        scope_key = f"{stage}:{worker}:{gate_tier}"
        _bump_group("by_stage_worker_gate_tier", scope_key, "timeout_recovery_attempted")
        _bump_group("by_stage_worker_gate_tier", scope_key, metric)
        _update_scope_row(
            "by_stage_worker_gate_tier",
            scope_key,
            recovered=recovered,
            status=status,
        )
        detail = (
            str(recovery_payload.get("summary", "")).strip()
            if isinstance(recovery_payload, dict)
            else ""
        )
    else:
        totals["stale_recycled"] = int(totals.get("stale_recycled", 0) or 0) + 1
        _bump_group("by_stage", stage, "stale_recycled")
        _bump_group("by_worker", worker, "stale_recycled")
        _bump_group("by_gate_tier", gate_tier, "stale_recycled")
        detail = str(event_entry.get("command", "")).strip()
    summary["totals"] = totals

    recent_raw = summary.get("recent_events")
    recent_events = [
        item for item in recent_raw if isinstance(item, dict)
    ] if isinstance(recent_raw, list) else []
    recent_events = [
        {
            "event": event,
            "heartbeat_id": heartbeat_id,
            "stage_name": stage,
            "worker": worker,
            "gate_tier": gate_tier,
            "status": str(event_entry.get("status", "")),
            "updated_at_epoch_sec": int(event_entry.get("updated_at_epoch_sec", 0) or 0),
            "command": str(event_entry.get("command", "")),
            "detail": detail,
        },
        *recent_events,
    ][:recent_limit]
    summary["recent_events"] = recent_events


def _build_remote_check_heartbeat_sink(
    flow: object,
    *,
    stage_name: str,
    round_index: int,
    worker: str,
    gate_tier: str,
):
    artifact_path_builder = getattr(flow, "_artifact_path", None)
    artifact_store = getattr(flow, "artifact_store", None)
    if not callable(artifact_path_builder):
        return None
    writer = getattr(artifact_store, "write_json", None)
    if not callable(writer):
        return None
    heartbeat_path = artifact_path_builder("runtime", "remote_check_heartbeats.json")
    timeout_recovery_summary_path = artifact_path_builder("runtime", "timeout_recovery_summary.json")

    def _sink(payload: dict[str, object]) -> None:
        heartbeat_id = str(payload.get("heartbeat_id", "")).strip()
        if not heartbeat_id:
            command = str(payload.get("command", "")).strip()
            remote_host = str(payload.get("remote_host", "")).strip()
            command_index = int(payload.get("command_index", 0) or 0)
            heartbeat_id = (
                f"{stage_name}:{round_index}:{worker}:{gate_tier}:{remote_host}:{command_index}:{command}"
            )
        now_epoch_sec = time.time()
        event = str(payload.get("event", "")).strip().lower()
        status = str(payload.get("status", "")).strip().lower() or "running"

        entry = {
            "event": event,
            "heartbeat_id": heartbeat_id,
            "stage_name": stage_name,
            "round_index": round_index,
            "worker": worker,
            "gate_tier": gate_tier,
            "remote_host": str(payload.get("remote_host", "")),
            "remote_workdir": str(payload.get("remote_workdir", "")),
            "command": str(payload.get("command", "")),
            "command_index": int(payload.get("command_index", 0) or 0),
            "command_total": int(payload.get("command_total", 0) or 0),
            "timeout_sec": int(payload.get("timeout_sec", 0) or 0),
            "elapsed_sec": int(payload.get("elapsed_sec", 0) or 0),
            "started_at": str(payload.get("started_at", "")),
            "last_progress_at": str(payload.get("last_progress_at", "")),
            "last_output_at": str(payload.get("last_output_at", "")),
            "exit_code": int(payload.get("exit_code", 0) or 0)
            if payload.get("exit_code") is not None
            else None,
            "status": status,
            "updated_at_epoch_sec": int(now_epoch_sec),
        }
        recovery_payload = payload.get("recovery")
        if isinstance(recovery_payload, dict):
            entry["recovery"] = dict(recovery_payload)

        with _REMOTE_HEARTBEAT_FILE_LOCK:
            existing: dict[str, object] = {}
            try:
                if heartbeat_path.exists():
                    loaded = json.loads(heartbeat_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        existing = loaded
            except Exception:
                existing = {}

            active_raw = existing.get("active")
            recent_raw = existing.get("recent")
            active_by_id = {
                str(item.get("heartbeat_id", "")): item
                for item in active_raw
                if isinstance(item, dict) and str(item.get("heartbeat_id", ""))
            } if isinstance(active_raw, list) else {}
            recent_list = [
                item for item in recent_raw if isinstance(item, dict)
            ] if isinstance(recent_raw, list) else []
            stale_ttl_sec = _read_timeout_env_int(
                flow,
                "MULTI_CODEX_REMOTE_HEARTBEAT_STALE_TTL_SEC",
                30,
            )
            dedupe_event_limit = _read_timeout_env_int(
                flow,
                "MULTI_CODEX_TIMEOUT_RECOVERY_DEDUPE_IDS_LIMIT",
                500,
            )
            recent_events_limit = _read_timeout_env_int(
                flow,
                "MULTI_CODEX_TIMEOUT_RECOVERY_RECENT_EVENTS_LIMIT",
                50,
            )

            stale_entries: list[dict[str, object]] = []
            for active_id, active_entry in list(active_by_id.items()):
                last_update_epoch = int(active_entry.get("updated_at_epoch_sec", 0) or 0)
                if last_update_epoch <= 0:
                    last_update_epoch = int(now_epoch_sec)
                if now_epoch_sec - last_update_epoch > stale_ttl_sec:
                    stale_entry = dict(active_entry)
                    stale_entry["status"] = "stale"
                    stale_entry["event"] = "stale_timeout"
                    stale_entry["updated_at_epoch_sec"] = int(now_epoch_sec)
                    stale_entry["stale_timeout_sec"] = stale_ttl_sec
                    stale_entries.append(stale_entry)
                    active_by_id.pop(active_id, None)

            if event in {"start", "progress"} and status == "running":
                active_by_id[heartbeat_id] = entry
            else:
                active_by_id.pop(heartbeat_id, None)
                recent_list = [entry, *recent_list][:20]
            if stale_entries:
                recent_list = [*stale_entries, *recent_list][:20]

            payload_to_persist = {
                "updated_at_epoch_sec": int(now_epoch_sec),
                "active": sorted(
                    active_by_id.values(),
                    key=lambda item: (
                        int(item.get("elapsed_sec", 0) or 0),
                        str(item.get("heartbeat_id", "")),
                    ),
                    reverse=True,
                ),
                "recent": recent_list,
            }
            writer(heartbeat_path, payload_to_persist)

            summary_existing: dict[str, object] = {}
            try:
                if timeout_recovery_summary_path.exists():
                    loaded = json.loads(timeout_recovery_summary_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        summary_existing = loaded
            except Exception:
                summary_existing = {}
            summary = (
                dict(summary_existing)
                if summary_existing
                else _default_timeout_recovery_summary(int(now_epoch_sec))
            )
            events_to_record: list[dict[str, object]] = []
            if event in {"timeout_recovery", "stale_timeout"}:
                events_to_record.append(entry)
            events_to_record.extend(stale_entries)
            for event_entry in events_to_record:
                _update_timeout_recovery_summary(
                    summary,
                    event_entry=event_entry,
                    dedupe_event_limit=dedupe_event_limit,
                    recent_events_limit=recent_events_limit,
                )
            summary["updated_at_epoch_sec"] = int(now_epoch_sec)
            writer(timeout_recovery_summary_path, summary)

    return _sink


def _read_timeout_env_int(flow: object, key: str, default: int) -> int:
    reader = getattr(flow, "_read_positive_env_int", None)
    if callable(reader):
        value = int(reader(key, default))
        return value if value > 0 else default
    return default


def _load_timeout_recovery_summary(flow: object) -> dict[str, object]:
    artifact_path_builder = getattr(flow, "_artifact_path", None)
    if not callable(artifact_path_builder):
        return {}
    try:
        path = artifact_path_builder("runtime", "timeout_recovery_summary.json")
    except Exception:
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    try:
        loaded = json.loads(raw)
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _timeout_recovery_scope_row(
    flow: object,
    *,
    stage_name: str,
    worker: str,
    gate_tier: str,
) -> dict[str, object]:
    summary = _load_timeout_recovery_summary(flow)
    group_raw = summary.get("by_stage_worker_gate_tier")
    if not isinstance(group_raw, dict):
        return {}
    row = group_raw.get(f"{stage_name}:{worker}:{gate_tier}")
    return dict(row) if isinstance(row, dict) else {}


def _repeated_timeout_recovery_workers(
    flow: object,
    *,
    stage_name: str,
    gate_tier: str,
    workers: tuple[str, ...] = ("worker",),
) -> list[tuple[str, int]]:
    threshold = _read_timeout_env_int(
        flow,
        "MULTI_CODEX_TIMEOUT_RECOVERY_BLOCK_CONSECUTIVE_FAILURES",
        2,
    )
    blocked_workers: list[tuple[str, int]] = []
    for worker in workers:
        row = _timeout_recovery_scope_row(
            flow,
            stage_name=stage_name,
            worker=worker,
            gate_tier=gate_tier,
        )
        consecutive_failed = int(row.get("consecutive_failed_recoveries", 0) or 0)
        if consecutive_failed >= threshold:
            blocked_workers.append((worker, consecutive_failed))
    return blocked_workers


def _terminal_timeout_recovery_block_result(
    flow: object,
    *,
    stage: StageSpec,
    round_index: int,
    round_logs: list[StageRoundLog],
    final_gate: JudgeGateReview,
    gate_tier: str,
    gate_artifact_refs: list[str],
    blocked_workers: list[tuple[str, int]],
    passed_gates: list[str],
) -> RoundPassResult:
    threshold = _read_timeout_env_int(
        flow,
        "MULTI_CODEX_TIMEOUT_RECOVERY_BLOCK_CONSECUTIVE_FAILURES",
        2,
    )
    action_lines = [
        (
            f"{worker}: repeated {gate_tier} remote timeouts exhausted automatic recovery budget "
            f"({count} consecutive failures, threshold={threshold})"
        )
        for worker, count in blocked_workers
    ]
    final_gate.pass_gate = False
    final_gate.required_actions.extend(action_lines)
    final_gate.required_actions = list(dict.fromkeys(final_gate.required_actions))
    final_gate.rationale = (
        f"{final_gate.rationale}\nRepeated remote timeout recovery failed in {gate_tier}; "
        "stage blocked for manual intervention."
    ).strip()
    flow._persist_failure_event(
        FailureEventArtifact(
            stage_name=stage.name,
            round_index=round_index,
            source=f"{gate_tier}_timeout_recovery",
            classification=FailureClassification(
                code=f"{gate_tier}_timeout_recovery_exhausted",
                category="timeout",
                disposition="blocked",
                summary="Repeated remote timeout recovery failures exhausted the automatic retry budget.",
                owner="system",
                retryable=False,
                evidence=list(action_lines),
            ),
            details={
                "gate_tier": gate_tier,
                "blocked_workers": [
                    {"worker": worker, "consecutive_failed_recoveries": count}
                    for worker, count in blocked_workers
                ],
            },
        )
    )
    flow._persist_task_handoff_packet(
        flow._build_terminal_handoff_packet(
            worker="worker",
            stage=stage,
            round_index=round_index,
            final_gate=final_gate,
            trigger="timeout_recovery",
        )
    )
    flow._persist_runtime_status(
        RuntimeStatusSnapshot(
            target_repo=flow.state.target_repo,
            current_stage=stage.name,
            current_round=round_index,
            phase=f"{gate_tier}_timeout_blocked",
            overall_state="blocked",
            worker_states={"worker": "blocked"},
            judge_state="blocked_on_timeout_recovery",
            latest_artifacts=list(gate_artifact_refs),
            notes=list(final_gate.required_actions),
        )
    )
    blocked_ledger = flow._build_stage_progress_ledger(
        stage=stage,
        round_index=round_index,
        status="blocked",
        passed_gates=list(passed_gates),
        latest_artifacts=list(gate_artifact_refs),
        current_blocker=(final_gate.required_actions[0] if final_gate.required_actions else ""),
        current_blocker_category="timeout",
        notes=list(final_gate.required_actions),
    )
    flow._persist_stage_progress_ledger(stage, blocked_ledger)
    flow._persist_repo_progress_note(
        stage=stage,
        ledger=blocked_ledger,
        verified_facts=["Automatic timeout recovery budget was exhausted."],
        repeated_failure_points=list(final_gate.required_actions),
        stable_workarounds=[],
    )
    flow._persist_stage_dashboard_artifact(
        flow._build_stage_dashboard_artifact(
            stage=stage,
            status="blocked",
            current_round=round_index,
            worker_states={"worker": "blocked"},
            judge_state="blocked_on_timeout_recovery",
            unresolved_actions=list(final_gate.required_actions),
            latest_artifacts=list(gate_artifact_refs),
        )
    )
    return RoundPassResult(
        stage_result=StageResult(
            stage_name=stage.name,
            passed=False,
            rounds_used=round_index,
            gate=final_gate,
            round_logs=round_logs,
        )
    )


def _resolve_phase_timeout_cap_sec(flow: object, gate_tier: str) -> int:
    if gate_tier == "fast_round":
        return _read_timeout_env_int(flow, "MULTI_CODEX_PHASE_TIMEOUT_FAST_ROUND_SEC", 900)
    if gate_tier == "pre_promotion":
        return _read_timeout_env_int(flow, "MULTI_CODEX_PHASE_TIMEOUT_PRE_PROMOTION_SEC", 1_800)
    if gate_tier == "full_regression":
        return _read_timeout_env_int(flow, "MULTI_CODEX_PHASE_TIMEOUT_FULL_REGRESSION_SEC", 3_600)
    return _read_timeout_env_int(flow, "MULTI_CODEX_PHASE_TIMEOUT_DEFAULT_SEC", 1_800)


def _resolve_round_budget_sec(flow: object, *, stage_budget_sec: int, round_index: int) -> int:
    max_round = int(getattr(getattr(flow, "state", object()), "max_round_per_stage", 0) or 0)
    if max_round <= 0:
        return stage_budget_sec
    remaining_rounds = max(1, max_round - round_index + 1)
    return max(1, stage_budget_sec // remaining_rounds)


def _build_gate_timeout_budget_kwargs(
    flow: object,
    *,
    stage_name: str,
    round_index: int,
    gate_tier: str,
    stage_deadline_monotonic: float | None,
) -> dict[str, int]:
    phase_timeout_cap_sec = _resolve_phase_timeout_cap_sec(flow, gate_tier)
    if stage_deadline_monotonic is None:
        return {"phase_timeout_cap_sec": phase_timeout_cap_sec}
    stage_budget_sec = flow._remaining_stage_budget_sec(
        stage_name=stage_name,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )
    return {
        "stage_budget_sec": stage_budget_sec,
        "round_budget_sec": _resolve_round_budget_sec(
            flow,
            stage_budget_sec=stage_budget_sec,
            round_index=round_index,
        ),
        "phase_timeout_cap_sec": phase_timeout_cap_sec,
    }


async def _run_stage_checks(
    flow: object,
    *,
    worker: str,
    stage: StageSpec,
    workspace: object,
    gate_tier: str,
    round_index: int,
    stage_deadline_monotonic: float | None,
) -> object:
    heartbeat_sink = _build_remote_check_heartbeat_sink(
        flow,
        stage_name=stage.name,
        round_index=round_index,
        worker=worker,
        gate_tier=gate_tier,
    )
    budget_kwargs = _build_gate_timeout_budget_kwargs(
        flow,
        stage_name=stage.name,
        round_index=round_index,
        gate_tier=gate_tier,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )
    # --- Plugin-based path (preferred) ---
    plugin_call = getattr(flow.check_runner, "run_plugins_for_stage", None)
    if plugin_call is not None:
        return await plugin_call(
            worker, stage, workspace,
            gate_tier=gate_tier,
            heartbeat_sink=heartbeat_sink,
            **budget_kwargs,
        )

    # --- Legacy fallback ---
    desired_kwargs: dict[str, object] = {
        "gate_tier": gate_tier,
        **budget_kwargs,
        "heartbeat_sink": heartbeat_sink,
    }
    call = flow.check_runner.run_stage_checks
    try:
        signature = inspect.signature(call)
    except (TypeError, ValueError):
        signature = None
    if signature is None:
        return await call(worker, stage, workspace, **desired_kwargs)

    has_var_kw = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    kwargs_to_pass = (
        desired_kwargs
        if has_var_kw
        else {
            key: value
            for key, value in desired_kwargs.items()
            if key in signature.parameters
        }
    )
    return await call(worker, stage, workspace, **kwargs_to_pass)


def _stage_gate_commands_for_tier(stage: StageSpec, gate_tier: str) -> list[str]:
    if stage.gate_commands_remote_tiered:
        return [
            item.command.strip()
            for item in stage.gate_commands_remote_tiered
            if item.tier == gate_tier and item.command.strip()
        ]
    if gate_tier == "fast_round":
        return [command.strip() for command in stage.gate_commands_remote if command.strip()]
    return []


def _looks_like_make_command(command: str) -> bool:
    text = (command or "").strip()
    if not text:
        return False
    try:
        tokens = shlex.split(text)
    except ValueError:
        return text.startswith("make ") or text == "make"
    if not tokens:
        return False
    return Path(tokens[0]).name == "make"


def _command_matches_patterns(command: str, patterns: list[str]) -> bool:
    text = (command or "").strip()
    if not text:
        return False
    for pattern in patterns:
        normalized = pattern.strip()
        if not normalized:
            continue
        try:
            if re.search(normalized, text):
                return True
        except re.error:
            if normalized in text:
                return True
    return False


def _read_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _should_serialize_worker_checks(stage: StageSpec, gate_tier: str) -> bool:
    explicit = os.getenv("MULTI_CODEX_SERIALIZE_WORKER_REMOTE_CHECKS")
    if explicit is not None:
        return _read_bool_env("MULTI_CODEX_SERIALIZE_WORKER_REMOTE_CHECKS", False)
    if not stage.requires_remote:
        return False
    commands = _stage_gate_commands_for_tier(stage, gate_tier)
    if not commands:
        return False

    profile = getattr(stage, "build_strategy", None)
    if profile is not None:
        if profile.serialize_remote_checks == "always":
            return True
        if profile.serialize_remote_checks == "never":
            return False
        if profile.serialize_command_patterns and any(
            _command_matches_patterns(command, profile.serialize_command_patterns)
            for command in commands
        ):
            return True

    # Default policy is compile-focused: only make-like gate commands are serialized.
    # Non-compile remote gates remain parallel unless env override or build_strategy says otherwise.
    return any(_looks_like_make_command(command) for command in commands)


async def run_round_plan_phase(
    flow: object,
    *,
    stage: StageSpec,
    stage_plan: StageExecutionPlan,
    round_index: int,
    judge_feedback: list[str],
    prev_check_summary: str,
    review_memory: list,
    stage_deadline_monotonic: float,
) -> RoundPlanApproved | RoundPlanRejected:
    flow._persist_runtime_status(
        RuntimeStatusSnapshot(
            target_repo=flow.state.target_repo,
            current_stage=stage.name,
            current_round=round_index,
            phase="round_start",
            overall_state="running",
            worker_states={"worker": "planning"},
            judge_state="waiting_for_worker_plan",
            latest_artifacts=[
                flow._stage_artifact_ref(stage.name, f"round{round_index}_context_packet.json")
            ],
            notes=[f"Round {round_index} started."],
        )
    )
    flow._persist_stage_dashboard_artifact(
        flow._build_stage_dashboard_artifact(
            stage=stage,
            status="running",
            current_round=round_index,
            worker_states={"worker": "planning"},
            judge_state="waiting_for_worker_plan",
            unresolved_actions=list(judge_feedback),
            latest_artifacts=[
                flow._stage_artifact_ref(stage.name, f"round{round_index}_context_packet.json")
            ],
        )
    )
    flow._remaining_stage_budget_sec(
        stage_name=stage.name,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )
    context_packet = flow._build_stage_context_packet(
        stage=stage,
        stage_gate=flow._current_stage_gate,
        stage_plan=stage_plan,
        round_index=round_index,
        judge_feedback=judge_feedback,
        prev_check_summary_a=prev_check_summary,
        prev_check_summary_b="",
        review_memory=review_memory,
    )
    flow._persist_context_packet(context_packet)
    context_packet_json = json.dumps(
        context_packet.model_dump(),
        ensure_ascii=False,
        indent=2,
    )
    flow._persist_task_handoff_packet(
        flow._build_round_start_handoff_packet(
            worker="worker",
            stage=stage,
            round_index=round_index,
            context_packet=context_packet,
            review_memory=review_memory,
        )
    )
    baseline_status_artifacts = flow._run_stage_baseline_sanity(
        stage=stage,
        round_index=round_index,
    )
    for artifact in baseline_status_artifacts:
        flow._persist_baseline_status_artifact(artifact)
    baseline_failures = [
        failure
        for artifact in baseline_status_artifacts
        for failure in artifact.failures
    ]
    if baseline_failures:
        flow._persist_failure_event(
            FailureEventArtifact(
                stage_name=stage.name,
                round_index=round_index,
                source="baseline_sanity",
                classification=FailureClassification(
                    code="baseline_sanity_failed",
                    category="workspace_state",
                    disposition="blocked",
                    summary="Baseline sanity failed before planner execution.",
                    owner="system",
                    retryable=False,
                    evidence=baseline_failures,
                ),
                details={"failures": baseline_failures},
            )
        )
        raise ValueError(
            f"Baseline sanity failed for stage '{stage.name}': {baseline_failures}"
        )
    current_blocker = judge_feedback[0] if judge_feedback else ""
    current_blocker_category = "planner" if judge_feedback else ""
    worker_entry_packet = flow._build_worker_entry_packet(
        stage=stage,
        round_index=round_index,
        worker="worker",
        context_packet=context_packet,
        passed_gates=["stage_initialized", "remote_preflight", "stage_gate"],
        current_blocker=current_blocker,
        current_blocker_category=current_blocker_category,
        artifact_refs=[
            flow._stage_artifact_ref(stage.name, f"round{round_index}_context_packet.json"),
            flow._stage_artifact_ref(stage.name, "stage_spec_snapshot.json"),
        ],
    )
    flow._persist_worker_entry_packet(worker_entry_packet)
    round_ledger = flow._build_stage_progress_ledger(
        stage=stage,
        round_index=round_index,
        status="running",
        passed_gates=["stage_initialized", "remote_preflight", "stage_gate"],
        latest_artifacts=[
            flow._stage_artifact_ref(stage.name, f"round{round_index}_context_packet.json"),
            flow._stage_artifact_ref(stage.name, f"round{round_index}_worker_entry_packet.json"),
        ],
        current_blocker=current_blocker,
        current_blocker_category=current_blocker_category,
        notes=[f"Round {round_index} started with fixed entry packet."],
    )
    flow._persist_stage_progress_ledger(stage, round_ledger)
    flow._persist_repo_progress_note(
        stage=stage,
        ledger=round_ledger,
        verified_facts=["Context packet persisted.", "Baseline sanity passed."],
        repeated_failure_points=[],
        stable_workarounds=[],
    )
    worker_plan = await flow._invoke_worker_plan_for_round(
        worker_name="worker",
        agent=flow.agents.worker,
        workspace=flow.agents.worker_workspace,
        stage=stage,
        round_index=round_index,
        judge_feedback=judge_feedback,
        auto_check_summary=prev_check_summary,
        context_packet_json=context_packet_json,
        worker_entry_packet_json=json.dumps(worker_entry_packet.model_dump(), ensure_ascii=False, indent=2),
        stage_deadline_monotonic=stage_deadline_monotonic,
    )
    plan_gate_review = await flow._invoke_plan_gate_review(
        stage=stage,
        round_index=round_index,
        context_packet_json=context_packet_json,
        worker_plan=worker_plan,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )
    if not plan_gate_review.pass_gate:
        updated_feedback = (
            list(plan_gate_review.worker_required_actions)
            + list(plan_gate_review.blockers)
        )
        flow._persist_failure_event(
            FailureEventArtifact(
                stage_name=stage.name,
                round_index=round_index,
                source="plan_gate",
                classification=FailureClassification(
                    code="plan_gate_rejected",
                    category="planner",
                    disposition="retry_next_round",
                    summary="Judge rejected worker plans before implementation.",
                    owner="judge",
                    retryable=True,
                    evidence=updated_feedback or [plan_gate_review.rationale],
                ),
                details=plan_gate_review.model_dump(),
            )
        )
        flow._persist_runtime_status(
            RuntimeStatusSnapshot(
                target_repo=flow.state.target_repo,
                current_stage=stage.name,
                current_round=round_index,
                phase="plan_gate_review",
                overall_state="running",
                worker_states={"worker": "replan_required"},
                judge_state="plan_rejected",
                latest_artifacts=[
                    flow._stage_artifact_ref(stage.name, f"round{round_index}_worker_plan.json"),
                    flow._stage_artifact_ref(stage.name, f"round{round_index}_plan_gate_review.json"),
                ],
                notes=["Plan gate rejected; retrying next round without implementation."],
            )
        )
        flow._persist_stage_dashboard_artifact(
            flow._build_stage_dashboard_artifact(
                stage=stage,
                status="running",
                current_round=round_index,
                worker_states={"worker": "replan_required"},
                judge_state="plan_rejected",
                unresolved_actions=updated_feedback,
                latest_artifacts=[
                    flow._stage_artifact_ref(stage.name, f"round{round_index}_plan_gate_review.json")
                ],
            )
        )
        return RoundPlanRejected(judge_feedback=updated_feedback)

    flow._persist_runtime_status(
        RuntimeStatusSnapshot(
            target_repo=flow.state.target_repo,
            current_stage=stage.name,
            current_round=round_index,
            phase="plan_gate_review",
            overall_state="running",
            worker_states={"worker": "implementing"},
            judge_state="plan_approved",
            latest_artifacts=[
                flow._stage_artifact_ref(stage.name, f"round{round_index}_worker_plan.json"),
                flow._stage_artifact_ref(stage.name, f"round{round_index}_plan_gate_review.json"),
            ],
            notes=["Worker Plan Mode completed and plan gate approved execution."],
        )
    )
    flow._persist_stage_dashboard_artifact(
        flow._build_stage_dashboard_artifact(
            stage=stage,
            status="running",
            current_round=round_index,
            worker_states={"worker": "implementing"},
            judge_state="plan_approved",
            unresolved_actions=[],
            latest_artifacts=[
                flow._stage_artifact_ref(stage.name, f"round{round_index}_plan_gate_review.json"),
            ],
        )
    )
    return RoundPlanApproved(
        context_packet=context_packet,
        context_packet_json=context_packet_json,
        worker_entry_packet=worker_entry_packet,
        worker_plan=worker_plan,
        plan_gate_review=plan_gate_review,
    )


async def run_round_delivery_phase(
    flow: object,
    *,
    stage: StageSpec,
    stage_gate: object,
    round_index: int,
    judge_feedback: list[str],
    worker_plan: WorkerPlan,
    worker_entry_packet: WorkerEntryPacket,
    prev_check_summary: str,
    context_packet_json: str,
    review_baseline: WorkspaceArtifactsLike | object,
    stage_deadline_monotonic: float,
) -> RoundDeliveryPhaseResult:
    worker_delivery = await _await_worker_delivery(
        flow._invoke_worker_delivery_for_round(
            worker_name="worker",
            agent=flow.agents.worker,
            workspace=flow.agents.worker_workspace,
            stage=stage,
            stage_gate=stage_gate,
            round_index=round_index,
            judge_feedback=judge_feedback,
            approved_plan=worker_plan,
            auto_check_summary=prev_check_summary,
            context_packet_json=context_packet_json,
            worker_entry_packet_json=json.dumps(worker_entry_packet.model_dump(), ensure_ascii=False, indent=2),
            stage_deadline_monotonic=stage_deadline_monotonic,
        ),
    )
    flow._persist_worker_delivery(stage_name=stage.name, round_index=round_index, delivery=worker_delivery)

    clean_state = flow._build_clean_state_artifact(
        stage_name=stage.name,
        round_index=round_index,
        worker="worker",
        delivery=worker_delivery,
    )
    flow._persist_clean_state_artifact(clean_state)
    if not clean_state.passed:
        flow._persist_failure_event(
            FailureEventArtifact(
                stage_name=stage.name,
                round_index=round_index,
                source="worker_clean_state",
                classification=FailureClassification(
                    code="session_end_clean_state_failed",
                    category="workspace_state",
                    disposition="repair_required",
                    summary="Worker delivery ended without a clean handoff state.",
                    owner="worker",
                    retryable=True,
                    evidence=list(clean_state.undocumented_blockers),
                ),
                details=clean_state.model_dump(),
            )
        )
    drift = flow._build_plan_drift_artifact(
        stage_name=stage.name,
        round_index=round_index,
        worker="worker",
        plan=worker_plan,
        delivery=worker_delivery,
    )
    flow._persist_plan_drift_artifact(drift)
    if drift.severity == "blocking":
        flow._persist_failure_event(
            FailureEventArtifact(
                stage_name=stage.name,
                round_index=round_index,
                source="worker_plan_drift",
                classification=FailureClassification(
                    code="plan_drift_blocking",
                    category="planner",
                    disposition="repair_required",
                    summary="Implementation drifted outside approved plan scope.",
                    owner="worker",
                    retryable=True,
                    evidence=drift.out_of_plan_files or drift.notes,
                ),
                details=drift.model_dump(),
            )
        )
    for nudge in flow._build_runtime_nudges(
        stage=stage,
        round_index=round_index,
        drift_a=drift,
        drift_b=None,
    ):
        flow._persist_runtime_nudge(nudge)

    flow._remaining_stage_budget_sec(
        stage_name=stage.name,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )
    try:
        auto_checks = await _run_stage_checks(
            flow,
            worker="worker",
            stage=stage,
            workspace=flow.agents.worker_workspace,
            gate_tier="fast_round",
            round_index=round_index,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )
    except Exception as exc:
        error_message = f"post_impl_checks_exception: {type(exc).__name__}: {exc}"
        flow._persist_failure_event(
            FailureEventArtifact(
                stage_name=stage.name,
                round_index=round_index,
                source="post_impl_checks",
                classification=FailureClassification(
                    code="post_impl_checks_exception",
                    category="automated_checks",
                    disposition="blocked",
                    summary="Post-implementation checks crashed before producing structured check artifacts.",
                    owner="system",
                    retryable=False,
                    evidence=[error_message],
                ),
                details={
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                    "phase": "post_impl_checks",
                },
            )
        )
        flow._persist_runtime_status(
            RuntimeStatusSnapshot(
                target_repo=flow.state.target_repo,
                current_stage=stage.name,
                current_round=round_index,
                phase="post_impl_checks",
                overall_state="blocked",
                worker_states={"worker": "blocked"},
                judge_state="waiting_for_checks",
                latest_artifacts=[
                    flow._stage_artifact_ref(stage.name, f"round{round_index}_worker_delivery.json"),
                ],
                notes=[error_message],
            )
        )
        raise

    flow._remaining_stage_budget_sec(
        stage_name=stage.name,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )
    flow._persist_runtime_status(
        RuntimeStatusSnapshot(
            target_repo=flow.state.target_repo,
            current_stage=stage.name,
            current_round=round_index,
            phase="post_impl_checks",
            overall_state="running",
            worker_states={"worker": "self_review"},
            judge_state="waiting_for_reviews",
            latest_artifacts=[],
            notes=["Post-implementation checks completed."],
        )
    )
    flow._persist_stage_dashboard_artifact(
        flow._build_stage_dashboard_artifact(
            stage=stage,
            status="running",
            current_round=round_index,
            worker_states={"worker": "self_review"},
            judge_state="waiting_for_reviews",
            unresolved_actions=[],
            latest_artifacts=[],
        )
    )
    check_summary = format_check_summary(auto_checks)
    check_artifact_post_impl = flow._build_check_summary_artifact(
        worker="worker",
        stage_name=stage.name,
        round_index=round_index,
        phase="post_impl",
        checks=auto_checks,
        raw_summary=check_summary,
    )
    flow._persist_check_summary_artifact(check_artifact_post_impl)

    patch_after_impl = await _capture_workspace_artifacts(
        flow,
        baseline=review_baseline,
    )
    return RoundDeliveryPhaseResult(
        worker_delivery=worker_delivery,
        drift=drift,
        check_summary=check_summary,
        check_artifact_post_impl=check_artifact_post_impl,
        patch_after_impl=patch_after_impl,
    )

async def run_round_review_gate_phase(
    flow: object,
    *,
    stage: StageSpec,
    round_index: int,
    context_packet: StageContextPacket,
    context_packet_json: str,
    review_memory: list,
    review_baseline: WorkspaceArtifactsLike | object,
    delivery_phase_result: RoundDeliveryPhaseResult,
    no_progress_rounds: int,
    repeated_failure_rounds: int,
    prev_failure_signature: str,
    stage_deadline_monotonic: float,
) -> RoundReviewGatePhaseResult:
    # --- Worker self-review ---
    worker_self_review = await flow._invoke_agent_structured(
        flow.agents.worker,
        worker_self_review_prompt(
            "worker",
            stage.name,
            delivery_phase_result.patch_after_impl.review_patch or delivery_phase_result.patch_after_impl.patch,
            auto_check_summary=delivery_phase_result.check_summary,
            context_packet_json=context_packet_json,
            check_artifact_json=json.dumps(
                delivery_phase_result.check_artifact_post_impl.model_dump(),
                ensure_ascii=False,
                indent=2,
            ),
        ),
        SelfReviewResult,
        stage_name=stage.name,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )

    flow._remaining_stage_budget_sec(
        stage_name=stage.name,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )

    # --- Post self-review checks ---
    auto_checks = await _run_stage_checks(
        flow,
        worker="worker",
        stage=stage,
        workspace=flow.agents.worker_workspace,
        gate_tier="fast_round",
        round_index=round_index,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )
    flow._remaining_stage_budget_sec(
        stage_name=stage.name,
        stage_deadline_monotonic=stage_deadline_monotonic,
    )
    check_summary = format_check_summary(auto_checks)
    check_artifact_post_self = flow._build_check_summary_artifact(
        worker="worker",
        stage_name=stage.name,
        round_index=round_index,
        phase="post_self_review",
        checks=auto_checks,
        raw_summary=check_summary,
    )
    flow._persist_check_summary_artifact(check_artifact_post_self)

    patch_after_self = await _capture_workspace_artifacts(
        flow,
        baseline=review_baseline,
    )

    # --- Verifier review ---
    verifier_payload = flow._build_verifier_payload(
        stage=stage,
        stage_name=stage.name,
        patch_a=patch_after_self,
        patch_b=None,
        review_a_on_b=None,
        review_b_on_a=None,
        triage_a=None,
        triage_b=None,
        check_artifact_a=check_artifact_post_self,
        check_artifact_b=None,
        drift_a=delivery_phase_result.drift,
        drift_b=None,
    )
    flow._persist_runtime_status(
        RuntimeStatusSnapshot(
            target_repo=flow.state.target_repo,
            current_stage=stage.name,
            current_round=round_index,
            phase="verifier_review",
            overall_state="running",
            worker_states={"worker": "reviewed"},
            judge_state="waiting_for_verifier",
            latest_artifacts=[
                flow._stage_artifact_ref(stage.name, f"round{round_index}_worker_post_self_review_checks.json"),
            ],
            notes=["Verifier is auditing the stage contract and evidence."],
        )
    )
    flow._persist_stage_dashboard_artifact(
        flow._build_stage_dashboard_artifact(
            stage=stage,
            status="running",
            current_round=round_index,
            worker_states={"worker": "reviewed"},
            judge_state="waiting_for_verifier",
            unresolved_actions=[],
            latest_artifacts=[
                flow._stage_artifact_ref(stage.name, f"round{round_index}_worker_post_self_review_checks.json"),
            ],
        )
    )
    try:
        verifier_report = await flow._invoke_agent_structured(
            flow.agents.verifier,
            verifier_review_prompt(
                stage,
                round_index,
                json.dumps(verifier_payload, ensure_ascii=False, indent=2),
                context_packet_json=context_packet_json,
            ),
            VerifierReport,
            stage_name=stage.name,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )
        verifier_report = verifier_report.model_copy(update={"stage_name": stage.name, "round_index": round_index})
    except Exception as exc:
        flow._bump_metric("verifier_fallback_count")
        verifier_report = VerifierReport(
            stage_name=stage.name,
            round_index=round_index,
            pass_ready=False,
            blocking_gaps=["Verifier structured audit failed; rerun this round to obtain deterministic verification output."],
            criteria_results=[],
            evidence_gaps=[],
            verifier_notes=[f"Fallback verifier used due to structured invocation failure: {str(exc)[:300]}"],
        )
        flow._persist_failure_event(
            FailureEventArtifact(
                stage_name=stage.name,
                round_index=round_index,
                source="verifier_review",
                classification=FailureClassification(
                    code="verifier_fallback",
                    category="structured_output",
                    disposition="retry_next_round",
                    summary="Verifier structured output failed; using fail-closed fallback.",
                    owner="system",
                    retryable=True,
                    evidence=list(verifier_report.blocking_gaps),
                ),
                details=verifier_report.model_dump(),
            )
        )
        flow._persist_task_handoff_packet(
            flow._build_terminal_handoff_packet(
                worker="worker",
                stage=stage,
                round_index=round_index,
                final_gate=JudgeGateReview(
                    stage_name=stage.name,
                    round_index=round_index,
                    pass_gate=False,
                    high_severity_open=[],
                    disputed_items=[],
                    required_actions=list(verifier_report.blocking_gaps),
                    rationale="Verifier fallback timeout recovery handoff.",
                ),
                trigger="timeout_recovery",
            )
        )
    flow._persist_verifier_report(verifier_report)

    final_gate: JudgeGateReview
    judge_a_gate: JudgeGateReview | None = None
    judge_b_gate: JudgeGateReview | None = None

    if verifier_report.spec_gap_detected:
        spec_gap_report = flow._persist_and_build_spec_gap_report(
            stage=stage,
            round_index=round_index,
            verifier_report=verifier_report,
        )
        final_gate = JudgeGateReview(
            stage_name=stage.name,
            round_index=round_index,
            pass_gate=False,
            high_severity_open=[],
            disputed_items=[],
            required_actions=[
                "Spec gap detected: revise stage contract/source-of-truth before rerunning implementation."
            ] + [f"Ambiguous contract: {item}" for item in spec_gap_report.ambiguous_contracts]
              + [f"Clarification needed: {item}" for item in spec_gap_report.requested_clarifications],
            rationale="Judge gate was skipped because verifier flagged a spec gap and the harness failed closed.",
        )
        flow._persist_runtime_status(
            RuntimeStatusSnapshot(
                target_repo=flow.state.target_repo,
                current_stage=stage.name,
                current_round=round_index,
                phase="spec_gap",
                overall_state="blocked",
                worker_states={"worker": "blocked"},
                judge_state="skipped_due_to_spec_gap",
                latest_artifacts=[
                    flow._stage_artifact_ref(stage.name, f"round{round_index}_verifier_report.json"),
                    flow._stage_artifact_ref(stage.name, f"round{round_index}_spec_gap_report.json"),
                ],
                notes=["Verifier detected contract ambiguity; execution rolled back to spec."],
            )
        )
        flow._persist_stage_dashboard_artifact(
            flow._build_stage_dashboard_artifact(
                stage=stage,
                status="blocked",
                current_round=round_index,
                worker_states={"worker": "blocked"},
                judge_state="skipped_due_to_spec_gap",
                unresolved_actions=list(final_gate.required_actions),
                latest_artifacts=[flow._stage_artifact_ref(stage.name, f"round{round_index}_spec_gap_report.json")],
            )
        )
        flow._persist_task_handoff_packet(
            flow._build_terminal_handoff_packet(
                worker="worker",
                stage=stage,
                round_index=round_index,
                final_gate=final_gate,
                trigger="spec_gap",
            )
        )
    else:
        # --- Dual Judge independent review ---
        flow._persist_runtime_status(
            RuntimeStatusSnapshot(
                target_repo=flow.state.target_repo,
                current_stage=stage.name,
                current_round=round_index,
                phase="dual_judge_review",
                overall_state="running",
                worker_states={"worker": "reviewed"},
                judge_state="dual_reviewing",
                latest_artifacts=[
                    flow._stage_artifact_ref(stage.name, f"round{round_index}_worker_post_self_review_checks.json"),
                    flow._stage_artifact_ref(stage.name, f"round{round_index}_verifier_report.json"),
                ],
                notes=["Dual judges are independently reviewing worker output."],
            )
        )
        flow._persist_stage_dashboard_artifact(
            flow._build_stage_dashboard_artifact(
                stage=stage,
                status="running",
                current_round=round_index,
                worker_states={"worker": "reviewed"},
                judge_state="dual_reviewing",
                unresolved_actions=[],
                latest_artifacts=[
                    flow._stage_artifact_ref(stage.name, f"round{round_index}_worker_post_self_review_checks.json"),
                    flow._stage_artifact_ref(stage.name, f"round{round_index}_verifier_report.json"),
                ],
            )
        )
        worker_patch = patch_after_self.review_patch or patch_after_self.patch
        verifier_report_json = json.dumps(verifier_report.model_dump(), ensure_ascii=False, indent=2)
        check_artifact_json = json.dumps(check_artifact_post_self.model_dump(), ensure_ascii=False, indent=2)

        try:
            judge_a_gate, judge_b_gate = await asyncio.gather(
                flow._invoke_agent_structured(
                    flow.agents.judge,
                    judge_independent_review_prompt(
                        "judge",
                        stage.name,
                        round_index,
                        worker_patch,
                        auto_check_summary=check_summary,
                        context_packet_json=context_packet_json,
                        check_artifact_json=check_artifact_json,
                        verifier_report_json=verifier_report_json,
                    ),
                    JudgeGateReview,
                    stage_name=stage.name,
                    stage_deadline_monotonic=stage_deadline_monotonic,
                ),
                flow._invoke_agent_structured(
                    flow.agents.judge_b,
                    judge_independent_review_prompt(
                        "judge_b",
                        stage.name,
                        round_index,
                        worker_patch,
                        auto_check_summary=check_summary,
                        context_packet_json=context_packet_json,
                        check_artifact_json=check_artifact_json,
                        verifier_report_json=verifier_report_json,
                    ),
                    JudgeGateReview,
                    stage_name=stage.name,
                    stage_deadline_monotonic=stage_deadline_monotonic,
                ),
            )
            # Deterministic merge of dual judge decisions
            final_gate = merge_gate_decisions(judge_a_gate, judge_b_gate, stage.name, round_index)
        except Exception as exc:
            flow._bump_metric("judge_final_gate_fallback_count")
            final_gate = JudgeGateReview(
                stage_name=stage.name,
                round_index=round_index,
                pass_gate=False,
                high_severity_open=[],
                disputed_items=[],
                required_actions=["Dual judge structured gate failed; rerun this round to get deterministic gate output."],
                rationale=f"Fallback judge gate used due to structured invocation failure (fail-closed). Error: {str(exc)[:300]}",
            )
            flow._persist_failure_event(
                FailureEventArtifact(
                    stage_name=stage.name,
                    round_index=round_index,
                    source="judge_final_gate",
                    classification=FailureClassification(
                        code="judge_final_gate_fallback",
                        category="structured_output",
                        disposition="retry_next_round",
                        summary="Dual judge failed to produce structured final gate output; fail-closed fallback was used.",
                        owner="judge",
                        retryable=True,
                        evidence=[str(exc)[:600]],
                    ),
                    details={"exception": str(exc)[:1200]},
                )
            )
            flow._persist_task_handoff_packet(
                flow._build_terminal_handoff_packet(
                    worker="worker",
                    stage=stage,
                    round_index=round_index,
                    final_gate=final_gate,
                    trigger="timeout_recovery",
                )
            )
        final_gate.stage_name = stage.name
        final_gate.round_index = round_index

    # --- Automated check enforcement ---
    all_checks_passed = _checks_all_passed(auto_checks)
    if not all_checks_passed:
        final_gate.pass_gate = False
        if not final_gate.required_actions:
            final_gate.required_actions = []
        final_gate.required_actions.append("worker: automated checks still failing — fix before next round")

    # --- Convergence signal ---
    convergence_signal = flow._build_convergence_signal(
        stage=stage,
        stage_name=stage.name,
        round_index=round_index,
        patch_a=patch_after_self,
        patch_b=None,
        check_artifact_a=check_artifact_post_self,
        check_artifact_b=None,
        prev_failure_signature=prev_failure_signature,
        no_progress_rounds=no_progress_rounds,
        repeated_failure_rounds=repeated_failure_rounds,
    )
    current_failure_signature = check_artifact_post_self.signal_hash or ""
    no_progress_rounds = no_progress_rounds + 1 if convergence_signal.no_progress_detected else 0
    repeated_failure_rounds = repeated_failure_rounds + 1 if convergence_signal.repeated_failure_signature else 0
    prev_failure_signature = current_failure_signature
    if convergence_signal.recommended_action == "stop_and_replan":
        final_gate.pass_gate = False
        final_gate.required_actions.append(
            "Convergence guard fired: repeated no-progress / repeated failure signature detected; stop retrying and re-plan."
        )
        flow._persist_failure_event(
            FailureEventArtifact(
                stage_name=stage.name,
                round_index=round_index,
                source="convergence_guard",
                classification=FailureClassification(
                    code="convergence_guard_stop",
                    category="workspace_state",
                    disposition="blocked",
                    summary="Convergence guard stopped retries because the round is not making meaningful progress.",
                    owner="shared",
                    retryable=False,
                    evidence=list(convergence_signal.reasons),
                ),
                details=convergence_signal.model_dump(),
            )
        )
    flow._persist_convergence_signal(convergence_signal)
    for nudge in flow._build_runtime_nudges(
        stage=stage,
        round_index=round_index,
        convergence_signal=convergence_signal,
        check_artifact_a=check_artifact_post_self,
        check_artifact_b=None,
    ):
        flow._persist_runtime_nudge(nudge)

    # --- Handoff ---
    handoff_trigger = "stage_pass" if final_gate.pass_gate else "round_end"
    handoff = flow._build_task_handoff_packet(
        worker="worker",
        trigger=handoff_trigger,
        stage=stage,
        round_index=round_index,
        delivery=delivery_phase_result.worker_delivery,
        patch=patch_after_self,
        check_artifact=check_artifact_post_self,
        context_packet=context_packet,
        judge_feedback=final_gate.required_actions,
        review_memory=review_memory,
    )
    flow._persist_task_handoff_packet(handoff)

    round_log = StageRoundLog(
        round_index=round_index,
        worker_delivery=delivery_phase_result.worker_delivery,
        worker_auto_checks=auto_checks,
        worker_self_review=worker_self_review,
        judge_a_review=judge_a_gate,
        judge_b_review=judge_b_gate,
        verifier_report=verifier_report,
        judge_gate=final_gate,
    )

    return RoundReviewGatePhaseResult(
        worker_self_review=worker_self_review,
        judge_a_gate=judge_a_gate or final_gate,
        judge_b_gate=judge_b_gate or final_gate,
        verifier_report=verifier_report,
        final_gate=final_gate,
        auto_checks=auto_checks,
        check_summary=check_summary,
        check_artifact_post_review=check_artifact_post_self,
        patch_final=patch_after_self,
        review_memory=review_memory,
        convergence_signal=convergence_signal,
        no_progress_rounds=no_progress_rounds,
        repeated_failure_rounds=repeated_failure_rounds,
        prev_failure_signature=prev_failure_signature,
        round_log=round_log,
    )

def _checks_all_passed(checks: object) -> bool:
    return bool(
        getattr(checks, "all_tests_passed", False)
        and getattr(checks, "all_lint_passed", False)
        and getattr(checks, "all_perf_passed", False)
        and getattr(checks, "all_harness_passed", False)
    )


def _stage_declares_gate_tier(stage: StageSpec, tier: str) -> bool:
    if any(item.tier == tier for item in stage.gate_commands_remote_tiered):
        return True
    if any(contract.tier == tier for contract in stage.remote_gate_contracts):
        return True
    return False


async def apply_round_outcome(
    flow: object,
    *,
    stage: StageSpec,
    round_index: int,
    round_logs: list[StageRoundLog],
    review_memory: list,
    final_gate: JudgeGateReview,
    auto_checks: object,
    check_summary: str,
    review_baseline: object,
    stage_deadline_monotonic: float | None = None,
) -> RoundPassResult | RoundContinueResult:
    next_check_summary = check_summary
    declared_pre_promotion = _stage_declares_gate_tier(stage, "pre_promotion")
    declared_full_regression = _stage_declares_gate_tier(stage, "full_regression")
    should_run_pre_promotion_phase = final_gate.pass_gate
    pre_promotion_artifact_refs: list[str] = []
    full_regression_artifact_refs: list[str] = []

    if should_run_pre_promotion_phase:
        try:
            pre_promotion_checks = await _run_stage_checks(
                flow,
                worker="worker",
                stage=stage,
                workspace=flow.agents.worker_workspace,
                gate_tier="pre_promotion",
                round_index=round_index,
                stage_deadline_monotonic=stage_deadline_monotonic,
            )
        except Exception as exc:
            final_gate.pass_gate = False
            pre_promotion_crash = (
                "pre_promotion checks crashed: "
                f"{type(exc).__name__}: {exc}"
            )
            next_check_summary = pre_promotion_crash
            final_gate.required_actions.append(pre_promotion_crash)
            flow._persist_failure_event(
                FailureEventArtifact(
                    stage_name=stage.name,
                    round_index=round_index,
                    source="pre_promotion_checks",
                    classification=FailureClassification(
                        code="pre_promotion_checks_exception",
                        category="automated_checks",
                        disposition="blocked",
                        summary="Pre-promotion checks crashed before producing structured artifacts.",
                        owner="system",
                        retryable=False,
                        evidence=[f"{type(exc).__name__}: {exc}"],
                    ),
                    details={
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                        "phase": "pre_promotion_checks",
                    },
                )
            )
        else:
            pre_promotion_summary = format_check_summary(pre_promotion_checks)
            next_check_summary = pre_promotion_summary
            pre_promotion_artifact = flow._build_check_summary_artifact(
                worker="worker",
                stage_name=stage.name,
                round_index=round_index,
                phase="pre_promotion",
                checks=pre_promotion_checks,
                raw_summary=pre_promotion_summary,
            )
            pre_promotion_artifact_refs = [
                flow._stage_artifact_ref(
                    stage.name,
                    f"round{round_index}_worker_pre_promotion_checks.json",
                ),
            ]
            flow._persist_check_summary_artifact(pre_promotion_artifact)
            if not _checks_all_passed(pre_promotion_checks):
                final_gate.pass_gate = False
                final_gate.required_actions.append("pre_promotion gate failed for worker")
                flow._persist_failure_event(
                    FailureEventArtifact(
                        stage_name=stage.name,
                        round_index=round_index,
                        source="pre_promotion_checks",
                        classification=FailureClassification(
                            code="pre_promotion_checks_failed",
                            category="remote_gate",
                            disposition="repair_required",
                            summary="Pre-promotion checks failed closed.",
                            owner="worker",
                            retryable=False,
                            evidence=pre_promotion_artifact_refs,
                        ),
                        details={
                            "worker_summary": pre_promotion_summary,
                        },
                    )
                )
                blocked_workers = _repeated_timeout_recovery_workers(
                    flow,
                    stage_name=stage.name,
                    gate_tier="pre_promotion",
                )
                if blocked_workers:
                    return _terminal_timeout_recovery_block_result(
                        flow,
                        stage=stage,
                        round_index=round_index,
                        round_logs=round_logs,
                        final_gate=final_gate,
                        gate_tier="pre_promotion",
                        gate_artifact_refs=pre_promotion_artifact_refs,
                        blocked_workers=blocked_workers,
                        passed_gates=[
                            "stage_initialized",
                            "remote_preflight",
                            "stage_gate",
                            "plan_gate",
                        ],
                    )

    if final_gate.pass_gate and declared_full_regression:
        try:
            full_regression_checks = await _run_stage_checks(
                flow,
                worker="worker",
                stage=stage,
                workspace=flow.agents.worker_workspace,
                gate_tier="full_regression",
                round_index=round_index,
                stage_deadline_monotonic=stage_deadline_monotonic,
            )
        except Exception as exc:
            final_gate.pass_gate = False
            full_regression_crash = (
                "full_regression checks crashed: "
                f"{type(exc).__name__}: {exc}"
            )
            next_check_summary = full_regression_crash
            final_gate.required_actions.append(full_regression_crash)
            flow._persist_failure_event(
                FailureEventArtifact(
                    stage_name=stage.name,
                    round_index=round_index,
                    source="full_regression_checks",
                    classification=FailureClassification(
                        code="full_regression_checks_exception",
                        category="automated_checks",
                        disposition="blocked",
                        summary="Full-regression checks crashed before producing structured artifacts.",
                        owner="system",
                        retryable=False,
                        evidence=[f"{type(exc).__name__}: {exc}"],
                    ),
                    details={
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                        "phase": "full_regression_checks",
                    },
                )
            )
        else:
            full_regression_summary = format_check_summary(full_regression_checks)
            next_check_summary = full_regression_summary
            full_regression_artifact = flow._build_check_summary_artifact(
                worker="worker",
                stage_name=stage.name,
                round_index=round_index,
                phase="full_regression",
                checks=full_regression_checks,
                raw_summary=full_regression_summary,
            )
            full_regression_artifact_refs = [
                flow._stage_artifact_ref(
                    stage.name,
                    f"round{round_index}_worker_full_regression_checks.json",
                ),
            ]
            flow._persist_check_summary_artifact(full_regression_artifact)
            if not _checks_all_passed(full_regression_checks):
                final_gate.pass_gate = False
                final_gate.required_actions.append("full_regression gate failed for worker")
                flow._persist_failure_event(
                    FailureEventArtifact(
                        stage_name=stage.name,
                        round_index=round_index,
                        source="full_regression_checks",
                        classification=FailureClassification(
                            code="full_regression_checks_failed",
                            category="remote_gate",
                            disposition="repair_required",
                            summary="Full-regression checks failed closed.",
                            owner="worker",
                            retryable=False,
                            evidence=full_regression_artifact_refs,
                        ),
                        details={
                            "worker_summary": full_regression_summary,
                        },
                    )
                )
                blocked_workers = _repeated_timeout_recovery_workers(
                    flow,
                    stage_name=stage.name,
                    gate_tier="full_regression",
                )
                if blocked_workers:
                    return _terminal_timeout_recovery_block_result(
                        flow,
                        stage=stage,
                        round_index=round_index,
                        round_logs=round_logs,
                        final_gate=final_gate,
                        gate_tier="full_regression",
                        gate_artifact_refs=full_regression_artifact_refs,
                        blocked_workers=blocked_workers,
                        passed_gates=[
                            "stage_initialized",
                            "remote_preflight",
                            "stage_gate",
                            "plan_gate",
                            "pre_promotion",
                        ],
                    )

    if final_gate.pass_gate:
        promotion_readiness = flow._build_promotion_readiness_artifact(
            stage_name=stage.name,
            round_index=round_index,
            final_gate=final_gate,
            auto_checks_a=auto_checks,
            auto_checks_b=None,
            review_memory=review_memory,
        )
        flow._persist_promotion_readiness_artifact(promotion_readiness)
        if not promotion_readiness.ready:
            final_gate.pass_gate = False
            final_gate.required_actions.extend(promotion_readiness.unresolved_blockers)
            flow._persist_failure_event(
                FailureEventArtifact(
                    stage_name=stage.name,
                    round_index=round_index,
                    source="promotion_readiness",
                    classification=FailureClassification(
                        code="promotion_not_ready",
                        category="promotion",
                        disposition="repair_required",
                        summary="Judge gate passed but promotion readiness contract failed.",
                        owner="shared",
                        retryable=False,
                        evidence=promotion_readiness.unresolved_blockers,
                    ),
                    details=promotion_readiness.model_dump(),
                )
            )
    if final_gate.pass_gate:
        output_errors = flow._validate_stage_outputs(stage, base_dir=flow._owner_workspace_path())
        if output_errors:
            final_gate.pass_gate = False
            final_gate.required_actions.extend(output_errors)
            final_gate.required_actions = list(dict.fromkeys(final_gate.required_actions))
            flow._persist_failure_event(
                FailureEventArtifact(
                    stage_name=stage.name,
                    round_index=round_index,
                    source="stage_outputs",
                    classification=FailureClassification(
                        code="stage_outputs_invalid",
                        category="artifact_contract",
                        disposition="repair_required",
                        summary="Stage output validation failed before promotion.",
                        owner="system",
                        retryable=False,
                        evidence=list(output_errors),
                    ),
                    details={"output_errors": output_errors},
                )
            )
    if final_gate.pass_gate:
        promotion_error = flow._promote_owner_workspace(stage)
        if promotion_error:
            final_gate.pass_gate = False
            final_gate.required_actions.append(promotion_error)
            final_gate.rationale = f"{final_gate.rationale}\nOwner workspace promotion failed: {promotion_error}".strip()
            flow._persist_failure_event(
                FailureEventArtifact(
                    stage_name=stage.name,
                    round_index=round_index,
                    source="promotion",
                    classification=FailureClassification(
                        code="promotion_failed",
                        category="promotion",
                        disposition="repair_required",
                        summary="Owner workspace promotion failed after judge pass.",
                        owner="system",
                        retryable=False,
                        evidence=[promotion_error],
                    ),
                    details={"promotion_error": promotion_error},
                )
            )
        else:
            flow._persist_feature_checklist(
                FeatureChecklistArtifact(
                    stage_name=stage.name,
                    stage_id=stage.stage_id,
                    round_index=round_index,
                    items=[item.model_copy(update={"status": "verified"}) for item in stage.feature_checklist],
                )
            )
            pass_gates = [
                "stage_initialized",
                "remote_preflight",
                "stage_gate",
                "plan_gate",
            ]
            if declared_pre_promotion:
                pass_gates.append("pre_promotion")
            if declared_full_regression:
                pass_gates.append("full_regression")
            pass_gates.extend(["promotion_readiness", "promotion"])
            pass_latest_artifacts = (
                pre_promotion_artifact_refs
                + full_regression_artifact_refs
                + [flow._stage_artifact_ref(stage.name, f"round{round_index}_promotion_readiness.json")]
            )
            pass_notes = ["Stage passed and was promoted."]
            if declared_pre_promotion:
                pass_notes.append("Pre-promotion gate passed.")
            if declared_full_regression:
                pass_notes.append("Full-regression gate passed.")
            pass_ledger = flow._build_stage_progress_ledger(
                stage=stage,
                round_index=round_index,
                status="passed",
                passed_gates=pass_gates,
                latest_artifacts=pass_latest_artifacts,
                notes=pass_notes,
            )
            flow._persist_stage_progress_ledger(stage, pass_ledger)
            flow._persist_repo_progress_note(
                stage=stage,
                ledger=pass_ledger,
                verified_facts=[
                    "Judge gate passed.",
                    "Promotion readiness passed.",
                    "Owner workspace promoted.",
                    *(
                        ["Pre-promotion gate passed."]
                        if declared_pre_promotion
                        else []
                    ),
                    *(
                        ["Full-regression gate passed."]
                        if declared_full_regression
                        else []
                    ),
                ],
                repeated_failure_points=[],
                stable_workarounds=[],
            )
            flow._persist_runtime_status(
                RuntimeStatusSnapshot(
                    target_repo=flow.state.target_repo,
                    current_stage=stage.name,
                    current_round=round_index,
                    phase="stage_passed",
                    overall_state="passed",
                    worker_states={"worker": "done"},
                    judge_state="approved",
                    latest_artifacts=[
                        flow._stage_artifact_ref(stage.name, f"round{round_index}_worker_stage_pass_handoff.json"),
                    ],
                    notes=[f"Stage {stage.name} passed and was promoted."],
                )
            )
            flow._persist_stage_dashboard_artifact(
                flow._build_stage_dashboard_artifact(
                    stage=stage,
                    status="passed",
                    current_round=round_index,
                    worker_states={"worker": "done"},
                    judge_state="approved",
                    unresolved_actions=[],
                    latest_artifacts=pass_latest_artifacts,
                )
            )
            return RoundPassResult(
                stage_result=StageResult(
                    stage_name=stage.name,
                    passed=True,
                    rounds_used=round_index,
                    gate=final_gate,
                    round_logs=round_logs,
                )
            )

    return RoundContinueResult(
        judge_feedback=final_gate.required_actions,
        prev_check_summary=next_check_summary,
        review_baseline=flow.workspace_port.capture_snapshot(flow.agents.worker_workspace),
    )
