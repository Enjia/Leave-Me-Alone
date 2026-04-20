from __future__ import annotations

from email.parser import BytesParser
from email.policy import default as email_policy_default
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.intake import (
    confirm_draft_to_stages_file,
    create_session,
    generate_stage_draft,
    launch_confirmed_run,
    list_sessions,
    load_session,
    register_uploaded_file,
    update_session,
)


def _load_json(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _artifact_version_key(path: Path) -> tuple[int, str]:
    match = re.search(r"(?:^|_)round(\d+)(?:_|\.|$)", path.name)
    round_index = int(match.group(1)) if match else -1
    return round_index, path.name


def _collect_stage_dashboards(artifacts_dir: Path) -> list[dict[str, Any]]:
    dashboards: list[dict[str, Any]] = []
    for path in sorted(artifacts_dir.glob("*_dashboard.json")):
        payload = _load_json(path)
        if isinstance(payload, dict):
            dashboards.append(payload)
    status_order = {"running": 0, "blocked": 1, "failed": 2, "passed": 3, "pending": 4}
    dashboards.sort(key=lambda item: (status_order.get(str(item.get("status")), 99), str(item.get("stage_name", ""))))
    return dashboards


def _collect_latest_stage_artifacts(
    artifacts_dir: Path,
    pattern: str,
) -> dict[str, dict[str, Any]]:
    results: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in sorted(artifacts_dir.glob(pattern)):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        stage_name = str(payload.get("stage_name", ""))
        if not stage_name:
            continue
        previous = results.get(stage_name)
        if previous is None or _artifact_version_key(path) > _artifact_version_key(previous[0]):
            results[stage_name] = (path, payload)
    return {stage_name: payload for stage_name, (_, payload) in results.items()}


def _collect_latest_stage_worker_artifacts(
    artifacts_dir: Path,
    pattern: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    results: dict[tuple[str, str], tuple[Path, dict[str, Any]]] = {}
    for path in sorted(artifacts_dir.glob(pattern)):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        stage_name = str(payload.get("stage_name", ""))
        worker = str(payload.get("worker", ""))
        if not stage_name or not worker:
            continue
        key = (stage_name, worker)
        previous = results.get(key)
        if previous is None or _artifact_version_key(path) > _artifact_version_key(previous[0]):
            results[key] = (path, payload)

    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for (stage_name, worker), (_, payload) in results.items():
        grouped.setdefault(stage_name, {})[worker] = payload
    return grouped


def _collect_stage_failure_events(artifacts_dir: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(artifacts_dir.glob("*_failure_event.json")):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        stage_name = str(payload.get("stage_name", ""))
        if not stage_name:
            continue
        grouped.setdefault(stage_name, []).append(payload)
    return grouped


def _aggregate_failure_categories(
    failure_events: dict[str, list[dict[str, Any]]],
) -> dict[str, int]:
    """Aggregate failure events by error category across all stages.

    Returns a dict mapping ``category`` → count, enabling the monitor to
    display error-type distribution at a glance.
    """
    counts: dict[str, int] = {}
    for stage_events in failure_events.values():
        for event in stage_events:
            classification = event.get("classification")
            if not isinstance(classification, dict):
                continue
            category = str(classification.get("category", "unknown"))
            counts[category] = counts.get(category, 0) + 1
    return counts


def _collect_stage_progress_ledgers(target_repo: str) -> dict[str, dict[str, Any]]:
    repo = Path(target_repo).expanduser() if target_repo else None
    if repo is None:
        return {}
    progress_dir = repo / "docs" / "stage-progress"
    if not progress_dir.exists():
        return {}
    ledgers: dict[str, dict[str, Any]] = {}
    for path in sorted(progress_dir.glob("*_ledger.json")):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        stage_name = str(payload.get("stage_name", "")).strip()
        if not stage_name:
            continue
        ledgers[stage_name] = payload
    return ledgers


def _read_positive_env_int(key: str, default: int) -> int:
    raw = os.getenv(key, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _normalize_status(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"passed", "pass", "ok", "success"}:
        return "passed"
    if normalized in {"failed", "fail", "error"}:
        return "failed"
    if normalized in {"running", "in_progress"}:
        return "running"
    if normalized in {"partial"}:
        return "partial"
    return "pending"


def _combine_worker_signal(states: dict[str, str]) -> str:
    normalized = {worker: _normalize_status(status) for worker, status in states.items()}
    if not normalized:
        return "pending"
    values = set(normalized.values())
    if "failed" in values:
        return "failed"
    if values == {"passed"}:
        return "passed"
    if "running" in values:
        return "running"
    if "passed" in values:
        return "partial"
    return "pending"


def _command_entries_from_check(payload: dict[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for command in payload.get("passed_commands", []) or []:
        entries.append({"command": str(command), "status": "passed", "summary": ""})
    for failure in payload.get("failed_checks", []) or []:
        if not isinstance(failure, dict):
            continue
        entries.append(
            {
                "command": str(failure.get("command", "")),
                "status": "failed",
                "summary": str(failure.get("summary", "")),
            }
        )
    return entries


def _is_sync_command(command: str) -> bool:
    return (
        command.startswith("prepare remote dir ")
        or command.startswith("rsync to ")
        or command.startswith("cleanup remote path-sensitive metadata ")
        or command.startswith("remote-preflight-sync:")
        or command.startswith("remote-preflight-cleanup:")
    )


def _is_remote_gate_command(command: str) -> bool:
    if command.startswith("remote-exec:") or command.startswith("remote-contract:") or command == "remote-gate":
        return True
    if not command.startswith("[remote:"):
        return False
    if " preflight " in command:
        return False
    if command.endswith("writable-workdir"):
        return False
    return True


def _build_signal_payload(
    *,
    status_by_worker: dict[str, str],
    details: list[str],
) -> dict[str, Any]:
    return {
        "status": _combine_worker_signal(status_by_worker),
        "by_worker": status_by_worker,
        "details": details[:6],
    }


def _build_stage_runtime_signals(
    stage: dict[str, Any],
    *,
    preflight_by_worker: dict[str, dict[str, Any]] | None,
    checks_by_worker: dict[str, dict[str, Any]] | None,
    failure_events: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    preflight_by_worker = preflight_by_worker or {}
    checks_by_worker = checks_by_worker or {}
    failure_events = failure_events or []

    preflight_states: dict[str, str] = {}
    preflight_details: list[str] = []
    for worker, payload in sorted(preflight_by_worker.items()):
        worker_status = _normalize_status(str(payload.get("status", "pending")))
        preflight_states[worker] = worker_status
        if worker_status == "failed":
            failures = [
                str(item.get("command", ""))
                for item in (payload.get("results", []) or [])
                if isinstance(item, dict) and not item.get("passed", False)
            ]
            preflight_details.append(f"{worker}: {', '.join(failures[:2]) or 'preflight failed'}")
        else:
            preflight_details.append(f"{worker}: passed")

    sync_states: dict[str, str] = {}
    sync_details: list[str] = []
    remote_gate_states: dict[str, str] = {}
    remote_gate_details: list[str] = []

    for worker, payload in sorted(checks_by_worker.items()):
        entries = _command_entries_from_check(payload)
        sync_entries = [item for item in entries if _is_sync_command(item["command"])]
        remote_entries = [item for item in entries if _is_remote_gate_command(item["command"])]

        if sync_entries:
            sync_states[worker] = "failed" if any(item["status"] == "failed" for item in sync_entries) else "passed"
            last_sync = sync_entries[-1]
            sync_details.append(f"{worker}: {last_sync['status']} {last_sync['command']}")

        if remote_entries:
            remote_gate_states[worker] = (
                "failed" if any(item["status"] == "failed" for item in remote_entries) else "passed"
            )
            last_remote = remote_entries[-1]
            error_class = str(payload.get("normalized_error_class", "")).strip()
            subsystem = str(payload.get("likely_subsystem", "")).strip()
            suffix = ""
            if error_class or subsystem:
                suffix = f" [{subsystem or 'unknown'}/{error_class or 'unknown'}]"
            remote_gate_details.append(
                f"{worker}: {last_remote['status']} {last_remote['command']}{suffix}"
            )

    for worker, payload in sorted(preflight_by_worker.items()):
        if worker in sync_states:
            continue
        preflight_entries = [
            item
            for item in (payload.get("results", []) or [])
            if isinstance(item, dict) and _is_sync_command(str(item.get("command", "")))
        ]
        if not preflight_entries:
            continue
        sync_states[worker] = (
            "failed" if any(not item.get("passed", False) for item in preflight_entries) else "passed"
        )
        last_sync = preflight_entries[-1]
        sync_details.append(f"{worker}: {_normalize_status('passed' if last_sync.get('passed', False) else 'failed')} {last_sync.get('command', '')}")

    artifact_contract_failures: list[str] = []
    for event in failure_events:
        classification = event.get("classification", {}) if isinstance(event, dict) else {}
        if not isinstance(classification, dict):
            continue
        code = str(classification.get("code", ""))
        category = str(classification.get("category", ""))
        if category == "artifact_contract" or code == "stage_output_validation_failed":
            artifact_contract_failures.append(code or "artifact_contract_failure")

    if artifact_contract_failures:
        artifact_contract = {
            "status": "failed",
            "by_worker": {},
            "details": artifact_contract_failures[:6],
        }
    elif _normalize_status(str(stage.get("status", ""))) == "passed":
        artifact_contract = {
            "status": "passed",
            "by_worker": {},
            "details": ["stage passed without recorded artifact contract failure"],
        }
    else:
        artifact_contract = {
            "status": "pending",
            "by_worker": {},
            "details": ["awaiting end-of-stage artifact validation"],
        }

    return {
        "remote_preflight": _build_signal_payload(
            status_by_worker=preflight_states,
            details=preflight_details or ["no remote preflight artifact yet"],
        ),
        "sync": _build_signal_payload(
            status_by_worker=sync_states,
            details=sync_details or ["no sync artifact yet"],
        ),
        "remote_gate": _build_signal_payload(
            status_by_worker=remote_gate_states,
            details=remote_gate_details or ["no remote gate artifact yet"],
        ),
        "artifact_contract": artifact_contract,
    }


def _read_event_stream(runtime_dir: Path, since_offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Read typed runtime events from events.jsonl (incremental, fail-silent).

    Returns ``(events_as_dicts, new_byte_offset)``.  Pass the returned offset
    back on the next call for incremental monitor refreshes.  Falls back to an
    empty list when the file is absent or malformed.
    """
    events_path = runtime_dir / "events.jsonl"
    if not events_path.exists():
        return [], 0

    events: list[dict[str, Any]] = []
    new_offset = since_offset

    try:
        with events_path.open("rb") as file_handle:
            file_handle.seek(since_offset)
            for raw_line in file_handle:
                stripped = raw_line.strip()
                new_offset += len(raw_line)
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                    if isinstance(payload, dict):
                        events.append(payload)
                except Exception:
                    pass
    except Exception:
        pass

    return events, new_offset


def _summarize_event_stream(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive a lightweight summary from the event stream for the monitor view."""
    stage_events: dict[str, list[dict[str, Any]]] = {}
    latest_cost: dict[str, Any] = {}
    round_counts: dict[str, int] = {}

    for event in events:
        event_type = str(event.get("event_type", ""))
        stage_name = str(event.get("stage_name", ""))

        if stage_name:
            stage_events.setdefault(stage_name, []).append(event)

        if event_type == "cost":
            latest_cost = event

        if event_type in ("round_started", "round_finished") and stage_name:
            round_index = int(event.get("round_index", 0))
            round_counts[stage_name] = max(round_counts.get(stage_name, 0), round_index)

    stage_summaries: dict[str, dict[str, Any]] = {}
    for stage_name, stage_evts in stage_events.items():
        last_event = stage_evts[-1]
        stage_summaries[stage_name] = {
            "event_count": len(stage_evts),
            "last_event_type": last_event.get("event_type", ""),
            "last_phase": last_event.get("phase", ""),
            "rounds_seen": round_counts.get(stage_name, 0),
        }

    return {
        "total_events": len(events),
        "stage_summaries": stage_summaries,
        "latest_cost_event": latest_cost,
    }


def build_monitor_payload(
    runtime_dir: Path,
    *,
    event_stream_since_offset: int = 0,
) -> dict[str, Any]:
    artifacts_dir = runtime_dir / "artifacts"
    runtime_status = _load_json(artifacts_dir / "runtime_status.json") or {}
    harness_spec = _load_json(artifacts_dir / "harness_spec.json") or {}
    summary = _load_json(runtime_dir / "review-summary.json")
    dashboards = _collect_stage_dashboards(artifacts_dir)
    metrics = _load_json(artifacts_dir / "harness_metrics.json") or {}
    governance_policy = _load_json(artifacts_dir / "governance_policy.json") or {}
    remote_check_heartbeats = (
        _load_json(artifacts_dir / "runtime_remote_check_heartbeats.json") or {}
    )
    timeout_recovery_summary = (
        _load_json(artifacts_dir / "runtime_timeout_recovery_summary.json") or {}
    )
    triage_audits = _collect_latest_stage_artifacts(artifacts_dir, "*_triage_audit.json")
    promotion_readiness = _collect_latest_stage_artifacts(
        artifacts_dir, "*_promotion_readiness.json"
    )
    drift_artifacts = _collect_latest_stage_artifacts(artifacts_dir, "*_stage_gate_drift.json")
    preflight_artifacts = _collect_latest_stage_worker_artifacts(
        artifacts_dir, "*_remote_preflight.json"
    )
    check_artifacts = _collect_latest_stage_worker_artifacts(
        artifacts_dir, "*_checks.json"
    )
    failure_events = _collect_stage_failure_events(artifacts_dir)
    failure_category_counts = _aggregate_failure_categories(failure_events)
    target_repo = str(runtime_status.get("target_repo", "")).strip()
    stage_progress_ledgers = _collect_stage_progress_ledgers(target_repo)
    stage_runtime_signals = {
        str(stage.get("stage_name", "")): _build_stage_runtime_signals(
            stage,
            preflight_by_worker=preflight_artifacts.get(str(stage.get("stage_name", ""))),
            checks_by_worker=check_artifacts.get(str(stage.get("stage_name", ""))),
            failure_events=failure_events.get(str(stage.get("stage_name", ""))),
        )
        for stage in dashboards
        if str(stage.get("stage_name", ""))
    }
    stage_dag_plan = _load_json(artifacts_dir / "stage_dag_plan.json") or {}
    cost_ledger = _load_json(artifacts_dir / "cost_ledger.json") or {}
    # Event stream: prioritize events.jsonl over artifact scan for live view.
    event_stream_events, event_stream_offset = _read_event_stream(
        runtime_dir, since_offset=event_stream_since_offset,
    )
    event_stream_summary = _summarize_event_stream(event_stream_events)
    event_stream_summary["byte_offset"] = event_stream_offset
    return {
        "runtime_dir": str(runtime_dir),
        "runtime_status": runtime_status,
        "harness_spec": harness_spec,
        "summary": summary,
        "metrics": metrics,
        "governance_policy": governance_policy,
        "remote_check_heartbeats": remote_check_heartbeats,
        "timeout_recovery_summary": timeout_recovery_summary,
        "stage_dashboards": dashboards,
        "triage_audits": triage_audits,
        "promotion_readiness": promotion_readiness,
        "drift_artifacts": drift_artifacts,
        "stage_runtime_signals": stage_runtime_signals,
        "stage_dag_plan": stage_dag_plan,
        "stage_progress_ledgers": stage_progress_ledgers,
        "cost_ledger": cost_ledger,
        "event_stream_summary": event_stream_summary,
        "failure_category_counts": failure_category_counts,
    }


def _render_simple_list(items: list[Any]) -> str:
    return "".join(f"<li>{html.escape(str(item))}</li>" for item in items) or "<li>none</li>"


def _render_stage_profiles(stage_profiles: list[Any]) -> str:
    cards: list[str] = []
    for item in stage_profiles:
        if not isinstance(item, dict):
            continue
        profile_id = html.escape(str(item.get("profile_id", "")))
        stage_types = ", ".join(str(v) for v in (item.get("stage_types") or [])) or "none"
        envs = ", ".join(str(v) for v in (item.get("execution_envs") or [])) or "none"
        retry_bias = html.escape(str(item.get("retry_bias", "n/a")))
        required_artifacts = _render_simple_list(item.get("required_artifacts", []) or [])
        preferred_gates = _render_simple_list(item.get("preferred_validation_gates", []) or [])
        stop_conditions = _render_simple_list(item.get("stop_conditions", []) or [])
        notes = _render_simple_list(item.get("notes", []) or [])
        cards.append(
            f"""
            <div class="signal-card">
              <h3>{profile_id or 'unnamed-profile'}</h3>
              <ul>
                <li>stage_types={html.escape(stage_types)}</li>
                <li>execution_envs={html.escape(envs)}</li>
                <li>retry_bias={retry_bias}</li>
              </ul>
              <h4>Required Artifacts</h4>
              <ul>{required_artifacts}</ul>
              <h4>Preferred Gates</h4>
              <ul>{preferred_gates}</ul>
              <h4>Stop Conditions</h4>
              <ul>{stop_conditions}</ul>
              <h4>Notes</h4>
              <ul>{notes}</ul>
            </div>
            """
        )
    return "".join(cards) or "<div class=\"signal-card\"><p>none</p></div>"


def _render_governance_section(
    *,
    triage: dict[str, Any] | None,
    promotion: dict[str, Any] | None,
    drift: dict[str, Any] | None,
) -> str:
    triage = triage or {}
    promotion = promotion or {}
    drift = drift or {}
    return f"""
    <div class="grid governance-grid">
      <div>
        <h3>Judge Gate</h3>
        <ul>
          <li>passed={html.escape(str(triage.get("passed", "n/a")))}</li>
          <li>disputed_items={html.escape(str(len(triage.get("invalid_rejections", []) or [])))}</li>
          <li>high_severity_open={html.escape(str(len(triage.get("fact_high_severity_rejections", []) or [])))}</li>
        </ul>
        <ul>{_render_simple_list(triage.get("policy_blockers", []) or [])}</ul>
      </div>
      <div>
        <h3>Promotion</h3>
        <ul>
          <li>ready={html.escape(str(promotion.get("ready", "n/a")))}</li>
          <li>final_gate_passed={html.escape(str(promotion.get("final_gate_passed", "n/a")))}</li>
          <li>all_checks_passed={html.escape(str(promotion.get("all_checks_passed", "n/a")))}</li>
        </ul>
        <ul>{_render_simple_list(promotion.get("unresolved_blockers", []) or [])}</ul>
      </div>
      <div>
        <h3>Drift</h3>
        <ul>
          <li>policy_blockers={html.escape(str(len(drift.get("policy_blockers", []) or [])))}</li>
          <li>policy_warnings={html.escape(str(len(drift.get("policy_warnings", []) or [])))}</li>
          <li>extra_commands={html.escape(str(len((drift.get("extra_test_commands", []) or []) + (drift.get("extra_lint_commands", []) or []) + (drift.get("extra_perf_checks", []) or []))))}</li>
        </ul>
        <ul>{_render_simple_list((drift.get("policy_blockers", []) or []) + (drift.get("policy_warnings", []) or []))}</ul>
      </div>
    </div>
    """


def _render_runtime_signal(name: str, payload: dict[str, Any] | None) -> str:
    payload = payload or {}
    status = html.escape(str(payload.get("status", "pending")))
    worker_lines = "".join(
        f"<li><strong>{html.escape(worker)}</strong>: {html.escape(str(state))}</li>"
        for worker, state in sorted((payload.get("by_worker") or {}).items())
    ) or "<li>none</li>"
    detail_lines = "".join(
        f"<li>{html.escape(str(item))}</li>"
        for item in (payload.get("details") or [])
    ) or "<li>none</li>"
    return f"""
    <div class="signal-card signal-{status}">
      <h3>{html.escape(name)}</h3>
      <p class="muted">status={status}</p>
      <ul>{worker_lines}</ul>
      <ul>{detail_lines}</ul>
    </div>
    """


def _render_stage_card(
    stage: dict[str, Any],
    *,
    triage: dict[str, Any] | None,
    promotion: dict[str, Any] | None,
    drift: dict[str, Any] | None,
    runtime_signals: dict[str, dict[str, Any]] | None,
) -> str:
    stage_name = html.escape(str(stage.get("stage_name", "")))
    stage_id = html.escape(str(stage.get("stage_id", "")))
    objective = html.escape(str(stage.get("objective", "")))
    status = html.escape(str(stage.get("status", "")))
    current_round = html.escape(str(stage.get("current_round", 0)))
    max_rounds = html.escape(str(stage.get("max_rounds", 0)))
    judge_state = html.escape(str(stage.get("judge_state", "")))
    convergence = html.escape(str(stage.get("latest_convergence_action", "")))

    worker_lines = "".join(
        f"<li><strong>{html.escape(worker)}</strong>: {html.escape(str(state))}</li>"
        for worker, state in sorted((stage.get("worker_states") or {}).items())
    ) or "<li>none</li>"
    check_lines = "".join(
        f"<li><strong>{html.escape(worker)}</strong>: {html.escape(str(state))}</li>"
        for worker, state in sorted((stage.get('latest_check_overview') or {}).items())
    ) or "<li>none</li>"
    plan_lines = "".join(
        f"<li><strong>{html.escape(worker)}</strong>: {html.escape(str(state))}</li>"
        for worker, state in sorted((stage.get('latest_plan_overview') or {}).items())
    ) or "<li>none</li>"
    action_lines = "".join(
        f"<li>{html.escape(str(item))}</li>"
        for item in (stage.get("unresolved_actions") or [])
    ) or "<li>none</li>"
    nudge_lines = "".join(
        f"<li>{html.escape(str(item))}</li>"
        for item in (stage.get("latest_nudges") or [])
    ) or "<li>none</li>"
    failure_lines = "".join(
        f"<li>{html.escape(str(item))}</li>"
        for item in (stage.get("latest_failure_codes") or [])
    ) or "<li>none</li>"
    artifact_lines = "".join(
        f"<li>{html.escape(str(item))}</li>"
        for item in (stage.get("latest_artifacts") or [])
    ) or "<li>none</li>"
    report_lines = "".join(
        f"<li>{html.escape(str(item))}</li>"
        for item in (stage.get("open_fact_high_severity_reports") or [])
    ) or "<li>none</li>"
    runtime_signals = runtime_signals or {}
    signal_cards = "".join(
        _render_runtime_signal(title, runtime_signals.get(key))
        for key, title in (
            ("remote_preflight", "Remote Preflight"),
            ("sync", "Sync"),
            ("remote_gate", "Remote Gate"),
            ("artifact_contract", "Artifact Contract"),
        )
    )

    return f"""
    <section class="card stage-card status-{status}">
      <h2>{stage_name}</h2>
      <p class="muted">stage_id={stage_id or 'N/A'} · round {current_round}/{max_rounds} · status={status}</p>
      <p>{objective}</p>
      <div class="grid">
        <div><h3>Workers</h3><ul>{worker_lines}</ul></div>
        <div><h3>Judge</h3><ul><li>{judge_state or 'none'}</li><li>convergence={convergence or 'none'}</li></ul></div>
        <div><h3>Plans</h3><ul>{plan_lines}</ul></div>
        <div><h3>Checks</h3><ul>{check_lines}</ul></div>
        <div><h3>Nudges</h3><ul>{nudge_lines}</ul></div>
        <div><h3>Open Actions</h3><ul>{action_lines}</ul></div>
        <div><h3>Open Fact S0/S1</h3><ul>{report_lines}</ul></div>
        <div><h3>Latest Failures</h3><ul>{failure_lines}</ul></div>
      </div>
      <h3>Runtime Gates</h3>
      <div class="grid governance-grid">{signal_cards}</div>
      <h3>Governance</h3>
      {_render_governance_section(triage=triage, promotion=promotion, drift=drift)}
      <h3>Artifacts</h3>
      <ul>{artifact_lines}</ul>
    </section>
    """


_PHASE_META: dict[str, tuple[str, str, int]] = {
    "not_started": ("尚未启动", "等待开始执行", 0),
    "stage_start": ("阶段初始化", "准备执行计划、约束和上下文", 10),
    "remote_preflight": ("远端预检查", "确认 worker 远端环境就绪", 20),
    "remote_preflight_failed": ("远端预检查失败", "先修复环境阻断后再继续", 20),
    "round_start": ("本轮规划", "Worker 产出实现方案，双 Judge 审批", 40),
    "repair_round": ("修复回合", "根据上一轮失败证据收敛修复并补齐验证", 64),
    "plan_gate_review": ("计划闸门", "Judge 审核计划是否可执行", 50),
    "implementation": ("编码实现", "Worker 实现已批准方案", 62),
    "post_impl_checks": ("实现后检查", "自动检查 + 自检 + 双 Judge 独立审查", 72),
    "verifier_review": ("Verifier 复核", "验证证据与收敛质量", 88),
    "stage_passed": ("阶段完成", "当前 stage 已通过并可推进", 100),
    "stage_failed": ("阶段失败", "当前 stage 失败收敛", 100),
    "spec_gap": ("规格缺口", "先补 StageSpec/输入契约", 15),
    "pre_promotion_timeout_blocked": ("超时恢复阻断", "pre_promotion 连续超时恢复失败，需人工介入", 78),
    "full_regression_timeout_blocked": ("超时恢复阻断", "full_regression 连续超时恢复失败，需人工介入", 84),
}

_WORKER_PROGRESS: dict[str, int] = {
    "idle": 0,
    "planning": 20,
    "replan_required": 28,
    "implementing": 55,
    "self_review": 72,
    "judged": 88,
    "passed": 100,
    "done": 100,
}

_WORKER_LABEL: dict[str, str] = {
    "idle": "空闲，等待任务分配",
    "planning": "制定实现方案",
    "replan_required": "方案被驳回，需要重新规划",
    "implementing": "正在编写代码",
    "self_review": "自检代码质量",
    "judged": "双 Judge 审查完成，等待合并裁决",
    "passed": "已通过审核",
    "done": "任务完成",
}

_JUDGE_PROGRESS: dict[str, int] = {
    "planning_stage_gate": 15,
    "waiting_for_worker_plan": 36,
    "plan_approved": 52,
    "plan_rejected": 45,
    "waiting_for_reviews": 78,
    "waiting_for_verifier": 86,
    "rejected": 100,
    "approved": 100,
    "not_started": 0,
}

_JUDGE_LABEL: dict[str, str] = {
    "planning_stage_gate": "发布阶段约束",
    "waiting_for_worker_plan": "等待 Worker 提交方案",
    "plan_approved": "方案已批准，开始实现",
    "plan_rejected": "方案被驳回，要求修改",
    "waiting_for_reviews": "等待代码审查结果",
    "waiting_for_verifier": "等待独立验证者复核",
    "rejected": "本轮未通过，需要修复",
    "approved": "本轮已通过",
    "not_started": "尚未启动",
}

_STATUS_LABEL: dict[str, str] = {
    "passed": "已通过",
    "running": "运行中",
    "failed": "已失败",
    "blocked": "已阻断",
    "pending": "等待中",
}


def _normalize_stage_status(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"passed", "pass", "ok", "success"}:
        return "passed"
    if normalized in {"failed", "fail", "error"}:
        return "failed"
    if normalized in {"blocked"}:
        return "blocked"
    if normalized in {"running", "in_progress"}:
        return "running"
    return "pending"


_PHASE_PROGRESS_ORDER: dict[str, int] = {
    "not_started": 0,
    "stage_start": 10,
    "remote_preflight": 20,
    "remote_preflight_failed": 20,
    "round_start": 40,
    "repair_round": 45,
    "plan_gate_review": 50,
    "implementation": 62,
    "post_impl_checks": 72,
    "verifier_review": 88,
    "judge_gate_review": 92,
    "pre_promotion": 95,
    "full_regression": 97,
    "stage_passed": 100,
    "stage_failed": 100,
    "spec_gap": 15,
    "pre_promotion_timeout_blocked": 78,
    "full_regression_timeout_blocked": 84,
}


def _phase_triplet(phase: str) -> tuple[str, str, int]:
    return _PHASE_META.get(phase, ("执行中", "正在推进当前阶段流程", 35))


def _describe_phase(phase: str, *, round_index: int) -> tuple[str, str, int]:
    label, goal, progress = _phase_triplet(phase)
    if phase == "repair_round" and round_index > 1:
        label = f"第 {round_index} 轮修复"
    return label, goal, progress


def _infer_phase_from_states(
    raw_phase: str,
    *,
    worker_states: dict[str, Any] | None,
    judge_state: str,
    round_index: int = 0,
    latest_check_overview: dict[str, Any] | None = None,
    latest_nudges: list[Any] | None = None,
    unresolved_actions: list[Any] | None = None,
    latest_convergence_action: str = "",
) -> str:
    phase = (raw_phase or "").strip()
    states = {str(value) for value in (worker_states or {}).values()}
    inferred = ""
    if "planning" in states or judge_state == "waiting_for_worker_plan":
        inferred = "round_start"
    elif "implementing" in states or judge_state == "plan_approved":
        inferred = "implementation"
    elif "replan_required" in states or judge_state == "plan_rejected":
        inferred = "plan_gate_review"
    elif "self_review" in states or "judged" in states or judge_state == "waiting_for_reviews":
        inferred = "post_impl_checks"
    elif judge_state == "waiting_for_verifier":
        inferred = "verifier_review"
    has_repair_context = bool(latest_check_overview) or bool(latest_nudges) or bool(unresolved_actions) or bool(
        latest_convergence_action
    )
    if round_index > 1 and has_repair_context:
        if phase == "round_start" or inferred == "round_start":
            return "repair_round"
    if phase and phase not in {"running", "in_progress"}:
        phase_score = _PHASE_PROGRESS_ORDER.get(phase, -1)
        inferred_score = _PHASE_PROGRESS_ORDER.get(inferred, -1) if inferred else -1
        if inferred and inferred_score > phase_score:
            return inferred
        return phase
    if inferred:
        return inferred
    return phase or "not_started"


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


_PIPELINE_NODES: list[dict[str, str]] = [
    {"id": "stage_gate", "label": "阶段约束", "icon": "📋"},
    {"id": "planner", "label": "制定方案", "icon": "📝"},
    {"id": "plan_gate", "label": "方案审批", "icon": "🔍"},
    {"id": "implementation", "label": "编码实现", "icon": "⚙️"},
    {"id": "checks", "label": "自动检查", "icon": "🧪"},
    {"id": "verifier", "label": "独立验证", "icon": "🔎"},
    {"id": "judge_gate", "label": "最终裁决", "icon": "⚖️"},
    {"id": "promotion", "label": "推进确认", "icon": "🚀"},
]

_PHASE_TO_NODE: dict[str, str] = {
    "stage_start": "stage_gate",
    "remote_preflight": "stage_gate",
    "remote_preflight_failed": "stage_gate",
    "round_start": "planner",
    "repair_round": "implementation",
    "plan_gate_review": "plan_gate",
    "implementation": "implementation",
    "post_impl_checks": "checks",
    "verifier_review": "verifier",
    "pre_promotion_timeout_blocked": "checks",
    "full_regression_timeout_blocked": "checks",
    "stage_passed": "promotion",
    "stage_failed": "promotion",
    "spec_gap": "stage_gate",
}

def _compute_pipeline_node_states(phase: str) -> dict[str, str]:
    if phase in {"", "not_started", "pending"}:
        return {node["id"]: "pending" for node in _PIPELINE_NODES}

    active_node = _PHASE_TO_NODE.get(phase, "implementation")
    states: dict[str, str] = {}
    found_active = False
    for node in _PIPELINE_NODES:
        node_id = node["id"]
        if node_id == active_node:
            states[node_id] = "active"
            found_active = True
        elif not found_active:
            states[node_id] = "done"
        else:
            states[node_id] = "pending"
    if phase in {"stage_passed"}:
        for node_id in states:
            states[node_id] = "done"
    elif phase in {"stage_failed"}:
        states[active_node] = "failed"
    return states

def _summarize_text(text: str, max_length: int = 60) -> str:
    text = text.strip()
    if len(text) <= max_length:
        return text
    return text[:max_length].rstrip() + "…"


def _actor_label(value: str) -> str:
    return {
        "worker": "Worker",
        "shared": "当前阶段",
        "judge": "Judge",
        "verifier": "Verifier",
    }.get(value, value or "当前阶段")


def _parse_focus_nudge(text: str) -> dict[str, str]:
    raw = text.strip()
    head, sep, recommendation = raw.partition(" -> ")
    parts = head.split(":", 3)
    if len(parts) == 4:
        target, category, severity, message = parts
        return {
            "target": target.strip(),
            "category": category.strip(),
            "severity": severity.strip() or "info",
            "message": message.strip(),
            "recommendation": recommendation.strip() if sep else "",
            "raw": raw,
        }
    return {
        "target": "shared",
        "category": "note",
        "severity": "info",
        "message": raw,
        "recommendation": "",
        "raw": raw,
    }


def _summarize_nudge(text: str) -> tuple[str, str, str, str]:
    parsed = _parse_focus_nudge(text)
    actor = _actor_label(parsed["target"])
    category = parsed["category"]
    severity = parsed["severity"] or "info"
    if category == "todo_enforcement":
        summary = f"{actor} 偏离已批准方案，需要先回到既定修复范围"
        icon = "💡"
    elif category == "artifact_missing":
        summary = "需要补齐证据产物并满足产物契约后再申请通过"
        icon = "📦"
    elif category == "no_progress":
        summary = "本轮没有新的有效改动，需要收敛阻塞点或切换修复策略"
        icon = "🧭"
    elif category == "error_recovery":
        summary = f"{actor} 仍卡在远端/运行阻断，先定位具体失败子系统"
        icon = "🛠️"
    elif category == "repeated_failure":
        summary = "同类失败重复出现，需要更换修复路径"
        icon = "♻️"
    else:
        summary = _summarize_text(parsed["message"], max_length=42)
        icon = "💬"
    detail = parsed["raw"]
    if parsed["recommendation"]:
        detail = f"{detail}\n建议动作: {parsed['recommendation']}"
    return summary, detail, severity, icon


def _summarize_action(text: str) -> str:
    lowered = text.lower()
    if lowered.startswith("fix all compile/runtime blockers"):
        return "先修复编译/运行阻断，恢复核心远端命令通过"
    if lowered.startswith("for worker, refactor core module cleanup/control flow"):
        return "Worker 需要修复核心模块的清理/控制流问题"
    if lowered.startswith("for worker, resolve exported symbol/api regression"):
        return "Worker 需要修复导出符号/API 回归"
    if "type-visibility regression in include headers" in lowered:
        return "需要确认 include 头文件的类型可见性回归已修复"
    if lowered.startswith("produce and validate `docs/p2_window_report.json`"):
        return "补齐并验证 `docs/p2_window_report.json` 证据产物"
    if lowered.startswith("re-run full post-triage harness"):
        return "重新执行完整验证并附带干净证据"
    if "automated checks still failing" in lowered:
        if lowered.startswith("worker:"):
            return "Worker 自动检查仍失败，需要继续修复"
        pass  # legacy worker_b compat removed
        return "自动检查仍失败，需要先修复后再推进"
    if "exhausted automatic recovery budget" in lowered:
        return "远端超时恢复预算耗尽，已进入阻断态，需要人工处理远端进程/环境"
    return _summarize_text(text, max_length=46)


def _summarize_failure_code(code: str) -> str:
    normalized = str(code or "").strip()
    if normalized == "pre_promotion_timeout_recovery_exhausted":
        return "Pre-promotion 超时恢复预算耗尽，阶段已阻断"
    if normalized == "full_regression_timeout_recovery_exhausted":
        return "Full-regression 超时恢复预算耗尽，阶段已阻断"
    return f"失败分类: {normalized}"


def _build_nudge_focus_item(text: str) -> dict[str, Any]:
    summary, detail, severity, icon = _summarize_nudge(text)
    return {
        "type": "nudge",
        "icon": icon,
        "severity": severity,
        "summary": summary,
        "detail": detail,
    }


def _build_action_focus_item(text: str) -> dict[str, Any]:
    return {
        "type": "action",
        "icon": "⚠️",
        "severity": "warning",
        "summary": _summarize_action(text),
        "detail": text,
    }


def _stage_highlight(stage: dict[str, Any]) -> str:
    actions = stage.get("unresolved_actions") or []
    failures = stage.get("latest_failure_codes") or []
    nudges = stage.get("latest_nudges") or []
    status = _normalize_stage_status(str(stage.get("status", "")))
    if nudges:
        summary, _, _, _ = _summarize_nudge(str(nudges[0]))
        return summary
    if actions:
        return _summarize_action(str(actions[0]))
    if failures:
        return _summarize_failure_code(str(failures[0]))
    if status == "passed":
        return "阶段已通过，可进入下一阶段"
    if status == "blocked":
        return "阶段阻断，需先解除关键前置问题"
    if status == "pending":
        return "等待前序阶段完成后开始"
    return "阶段正在推进中"


def _build_activity_feed(payload: dict[str, Any], current_stage_name: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    dashboards = payload.get("stage_dashboards") or []
    for stage in dashboards:
        stage_name = str(stage.get("stage_name", ""))
        for nudge_text in (stage.get("latest_nudges") or [])[:3]:
            events.append({**_build_nudge_focus_item(str(nudge_text)), "stage": stage_name})
        for failure_code in (stage.get("latest_failure_codes") or [])[:3]:
            full = _summarize_failure_code(str(failure_code))
            events.append({
                "type": "failure",
                "icon": "🔴",
                "stage": stage_name,
                "summary": _summarize_text(full),
                "detail": full if len(full) > 60 else "",
            })
        for action in (stage.get("unresolved_actions") or [])[:2]:
            events.append({**_build_action_focus_item(str(action)), "stage": stage_name})
    runtime_signals = payload.get("stage_runtime_signals") or {}
    for stage_name, signals in runtime_signals.items():
        if not isinstance(signals, dict):
            continue
        for signal_key in ("remote_preflight", "sync", "remote_gate", "artifact_contract"):
            signal = signals.get(signal_key)
            if not isinstance(signal, dict):
                continue
            signal_status = str(signal.get("status", ""))
            if signal_status == "failed":
                details = signal.get("details") or []
                detail_text = str(details[0]) if details else signal_key
                full = f"{signal_key} 失败: {detail_text}"
                events.append({
                    "type": "signal",
                    "icon": "🚨",
                    "stage": stage_name,
                    "summary": _summarize_text(full),
                    "detail": full if len(full) > 60 else "",
                })
    heartbeat_payload = payload.get("remote_check_heartbeats") or {}
    recent_raw = heartbeat_payload.get("recent") if isinstance(heartbeat_payload, dict) else []
    recent_items = [item for item in recent_raw if isinstance(item, dict)] if isinstance(recent_raw, list) else []
    for item in recent_items[:6]:
        event = str(item.get("event", "")).strip().lower()
        if event not in {"timeout_recovery", "stale_timeout"}:
            continue
        stage_name = str(item.get("stage_name", "")) or current_stage_name
        worker = str(item.get("worker", "worker"))
        gate_tier = str(item.get("gate_tier", ""))
        command = str(item.get("command", ""))
        if event == "timeout_recovery":
            recovery = item.get("recovery") if isinstance(item.get("recovery"), dict) else {}
            recovered = bool(recovery.get("recovered"))
            summary = (
                f"{worker} 远端超时后已完成进程清理 ({gate_tier})"
                if recovered
                else f"{worker} 远端超时后进程清理未确认成功 ({gate_tier})"
            )
            detail = str(recovery.get("summary", "")).strip() or command
            events.append(
                {
                    "type": "timeout_recovery",
                    "icon": "🧯" if recovered else "⚠️",
                    "stage": stage_name,
                    "summary": summary,
                    "detail": detail,
                }
            )
        elif event == "stale_timeout":
            events.append(
                {
                    "type": "heartbeat_stale",
                    "icon": "⌛",
                    "stage": stage_name,
                    "summary": f"{worker} 心跳陈旧已自动回收 ({gate_tier})",
                    "detail": command,
                }
            )
    return events[:12]

def _build_worker_details(
    current_stage: dict[str, Any],
    runtime_signals: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    current_stage = current_stage or {}
    check_overview = current_stage.get("latest_check_overview") or {}
    plan_overview = current_stage.get("latest_plan_overview") or {}
    worker_states = current_stage.get("worker_states") or {}
    details: dict[str, dict[str, Any]] = {}
    for worker_key in ("worker",):
        state = str(worker_states.get(worker_key, "idle"))
        details[worker_key] = {
            "state": state,
            "label": _WORKER_LABEL.get(state, ""),
            "progress": _WORKER_PROGRESS.get(state, 10),
            "check_status": str(check_overview.get(worker_key, "—")),
            "plan_status": str(plan_overview.get(worker_key, "—")),
        }
    return details

def _build_stage_roadmap(payload: dict[str, Any], current_stage_name: str) -> list[dict[str, Any]]:
    dag_plan = payload.get("stage_dag_plan") or {}
    dag_nodes = dag_plan.get("nodes") or []
    execution_order = dag_plan.get("serial_execution_order") or []
    dashboards = payload.get("stage_dashboards") or []
    dashboard_by_name: dict[str, dict[str, Any]] = {
        str(item.get("stage_name", "")): item
        for item in dashboards
        if isinstance(item, dict) and str(item.get("stage_name", ""))
    }
    dag_by_name: dict[str, dict[str, Any]] = {
        str(node.get("stage_name", "")): node
        for node in dag_nodes
        if isinstance(node, dict) and str(node.get("stage_name", ""))
    }
    ordered_names = list(execution_order) if execution_order else [str(n.get("stage_name", "")) for n in dag_nodes]
    if not ordered_names:
        ordered_names = [str(item.get("stage_name", "")) for item in dashboards if isinstance(item, dict)]
    for stage_name in dashboard_by_name:
        if stage_name and stage_name not in ordered_names:
            ordered_names.append(stage_name)
    if current_stage_name and current_stage_name not in ordered_names:
        ordered_names.append(current_stage_name)
    roadmap: list[dict[str, Any]] = []
    for index, stage_name in enumerate(ordered_names, start=1):
        if not stage_name:
            continue
        dashboard = dashboard_by_name.get(stage_name, {})
        dag_node = dag_by_name.get(stage_name, {})
        status = _normalize_stage_status(str(dashboard.get("status", "")))
        is_current = stage_name == current_stage_name
        roadmap.append({
            "index": index,
            "name": stage_name,
            "objective": str(dashboard.get("objective", "") or dag_node.get("objective", "")),
            "status": status,
            "status_label": _STATUS_LABEL.get(status, ""),
            "is_current": is_current,
            "round": _coerce_int(dashboard.get("current_round"), 0),
            "max_round": max(1, _coerce_int(dashboard.get("max_rounds"), 1)) if dashboard else 1,
        })
    return roadmap

def _build_focus_items(current_stage: dict[str, Any]) -> list[dict[str, Any]]:
    current_stage = current_stage or {}
    items: list[dict[str, Any]] = []
    for nudge_text in (current_stage.get("latest_nudges") or [])[:3]:
        items.append(_build_nudge_focus_item(str(nudge_text)))
    for failure_code in (current_stage.get("latest_failure_codes") or [])[:3]:
        full = _summarize_failure_code(str(failure_code))
        items.append({"type": "failure", "icon": "🔴", "severity": "error", "summary": _summarize_text(full), "detail": full if len(full) > 60 else ""})
    for report in (current_stage.get("open_fact_high_severity_reports") or [])[:2]:
        full = str(report)
        items.append({"type": "severity", "icon": "🚨", "severity": "critical", "summary": _summarize_text(full), "detail": full if len(full) > 60 else ""})
    for action in (current_stage.get("unresolved_actions") or [])[:3]:
        items.append(_build_action_focus_item(str(action)))
    return items[:8]

def _build_active_remote_checks(
    payload: dict[str, Any],
    *,
    current_stage_name: str,
) -> list[dict[str, Any]]:
    heartbeat_payload = payload.get("remote_check_heartbeats") or {}
    active_raw = heartbeat_payload.get("active") if isinstance(heartbeat_payload, dict) else []
    active_items = [item for item in active_raw if isinstance(item, dict)] if isinstance(active_raw, list) else []
    stale_ttl_sec = _read_positive_env_int("MULTI_CODEX_REMOTE_HEARTBEAT_STALE_TTL_SEC", 30)
    now_epoch_sec = int(time.time())
    checks: list[dict[str, Any]] = []
    for item in active_items:
        stage_name = str(item.get("stage_name", "")).strip()
        if current_stage_name and stage_name and stage_name != current_stage_name:
            continue
        updated_at_epoch_sec = _coerce_int(item.get("updated_at_epoch_sec"), 0)
        if updated_at_epoch_sec > 0 and now_epoch_sec - updated_at_epoch_sec > stale_ttl_sec:
            continue
        timeout_sec = max(1, _coerce_int(item.get("timeout_sec"), 1))
        elapsed_sec = max(0, _coerce_int(item.get("elapsed_sec"), 0))
        checks.append(
            {
                "worker": str(item.get("worker", "")),
                "stage_name": stage_name,
                "round_index": _coerce_int(item.get("round_index"), 0),
                "gate_tier": str(item.get("gate_tier", "")),
                "remote_host": str(item.get("remote_host", "")),
                "command": str(item.get("command", "")),
                "command_index": _coerce_int(item.get("command_index"), 0),
                "command_total": _coerce_int(item.get("command_total"), 0),
                "status": str(item.get("status", "running")),
                "started_at": str(item.get("started_at", "")),
                "last_progress_at": str(item.get("last_progress_at", "")),
                "last_output_at": str(item.get("last_output_at", "")),
                "elapsed_sec": elapsed_sec,
                "timeout_sec": timeout_sec,
                "progress": min(100, max(0, int(elapsed_sec / timeout_sec * 100))),
            }
        )
    checks.sort(
        key=lambda entry: (
            int(entry.get("elapsed_sec", 0) or 0),
            str(entry.get("worker", "")),
            str(entry.get("command", "")),
        ),
        reverse=True,
    )
    return checks[:6]


def _build_timeout_recovery_overview(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("timeout_recovery_summary") or {}
    totals_raw = summary.get("totals") if isinstance(summary, dict) else {}
    totals = dict(totals_raw) if isinstance(totals_raw, dict) else {}
    recent_raw = summary.get("recent_events") if isinstance(summary, dict) else []
    recent_items = [item for item in recent_raw if isinstance(item, dict)] if isinstance(recent_raw, list) else []
    attempted = _coerce_int(totals.get("timeout_recovery_attempted"), 0)
    recovered = _coerce_int(totals.get("timeout_recovery_recovered"), 0)
    failed = _coerce_int(totals.get("timeout_recovery_failed"), 0)
    stale_recycled = _coerce_int(totals.get("stale_recycled"), 0)
    failure_rate_pct = int(failed * 100 / attempted) if attempted > 0 else 0

    min_attempts_for_rate = _read_positive_env_int(
        "MULTI_CODEX_TIMEOUT_RECOVERY_ALERT_MIN_ATTEMPTS",
        3,
    )
    failure_rate_threshold_pct = _read_positive_env_int(
        "MULTI_CODEX_TIMEOUT_RECOVERY_ALERT_FAILURE_RATE_PCT",
        50,
    )
    consecutive_failures_threshold = _read_positive_env_int(
        "MULTI_CODEX_TIMEOUT_RECOVERY_ALERT_CONSECUTIVE_FAILURES",
        2,
    )
    stale_recycled_threshold = _read_positive_env_int(
        "MULTI_CODEX_TIMEOUT_RECOVERY_ALERT_STALE_RECYCLED",
        10,
    )

    consecutive_failed_recoveries = 0
    for item in recent_items:
        if str(item.get("event", "")).strip().lower() != "timeout_recovery":
            continue
        status = str(item.get("status", "")).strip().lower()
        if status in {"cleanup_failed", "failed", "error"}:
            consecutive_failed_recoveries += 1
            continue
        if status in {"recovered", "passed", "success"}:
            break
        break

    alerts: list[dict[str, Any]] = []
    if attempted >= min_attempts_for_rate and failure_rate_pct >= failure_rate_threshold_pct:
        alerts.append(
            {
                "code": "timeout_recovery_failure_rate",
                "level": "error",
                "message": (
                    f"恢复失败率偏高：{failure_rate_pct}% "
                    f"(失败 {failed} / 尝试 {attempted})"
                ),
            }
        )
    if consecutive_failed_recoveries >= consecutive_failures_threshold:
        alerts.append(
            {
                "code": "timeout_recovery_consecutive_failures",
                "level": "error",
                "message": (
                    f"连续恢复失败 {consecutive_failed_recoveries} 次，"
                    "建议立即人工介入远端进程清理。"
                ),
            }
        )
    if stale_recycled >= stale_recycled_threshold:
        alerts.append(
            {
                "code": "timeout_recovery_stale_recycled_high",
                "level": "warning",
                "message": (
                    f"心跳陈旧回收次数偏高：{stale_recycled}，"
                    "建议检查心跳链路与远端执行稳定性。"
                ),
            }
        )

    return {
        "updated_at_epoch_sec": _coerce_int(summary.get("updated_at_epoch_sec"), 0) if isinstance(summary, dict) else 0,
        "attempted": attempted,
        "recovered": recovered,
        "failed": failed,
        "stale_recycled": stale_recycled,
        "failure_rate_pct": failure_rate_pct,
        "thresholds": {
            "min_attempts_for_rate": min_attempts_for_rate,
            "failure_rate_pct": failure_rate_threshold_pct,
            "consecutive_failures": consecutive_failures_threshold,
            "stale_recycled": stale_recycled_threshold,
        },
        "consecutive_failed_recoveries": consecutive_failed_recoveries,
        "is_alerting": bool(alerts),
        "alerts": alerts,
        "recent_events": [
            {
                "event": str(item.get("event", "")),
                "stage_name": str(item.get("stage_name", "")),
                "worker": str(item.get("worker", "")),
                "gate_tier": str(item.get("gate_tier", "")),
                "status": str(item.get("status", "")),
                "detail": str(item.get("detail", "")),
                "updated_at_epoch_sec": _coerce_int(item.get("updated_at_epoch_sec"), 0),
            }
            for item in recent_items[:6]
        ],
    }


def _build_compression_view(runtime_dir: str) -> list[dict[str, Any]]:
    """Load compression events from the JSONL artifact file."""
    if not runtime_dir:
        return []
    artifact_path = Path(runtime_dir) / "artifacts" / "context_compression.jsonl"
    if not artifact_path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        for line in artifact_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return events[-20:]


def _build_cost_view(cost_ledger: dict[str, Any], *, runtime_dir: str = "") -> dict[str, Any]:
    snapshot = cost_ledger.get("snapshot") or {}
    by_stage = snapshot.get("by_stage") or {}
    by_worker = snapshot.get("by_worker") or {}
    by_model = snapshot.get("by_model") or {}
    compression_events = _build_compression_view(runtime_dir)
    total_saved = sum(
        max(0, evt.get("original_chars", 0) - evt.get("compressed_chars", 0))
        for evt in compression_events
    )
    return {
        "total_tokens": int(snapshot.get("total_tokens", 0)),
        "total_input_tokens": int(snapshot.get("total_input_tokens", 0)),
        "total_output_tokens": int(snapshot.get("total_output_tokens", 0)),
        "estimated_cost_usd": float(snapshot.get("estimated_cost_usd", 0.0)),
        "invocation_count": int(snapshot.get("invocation_count", 0)),
        "budget_warn_triggered": bool(snapshot.get("budget_warn_triggered", False)),
        "budget_hard_triggered": bool(snapshot.get("budget_hard_triggered", False)),
        "by_stage": [
            {
                "name": str(stage_name),
                "tokens": int(entry.get("total_tokens", 0)),
                "cost_usd": float(entry.get("estimated_cost_usd", 0.0)),
                "invocations": int(entry.get("invocation_count", 0)),
            }
            for stage_name, entry in by_stage.items()
            if isinstance(entry, dict)
        ],
        "by_worker": [
            {
                "name": str(worker_name),
                "tokens": int(entry.get("total_tokens", 0)),
                "cost_usd": float(entry.get("estimated_cost_usd", 0.0)),
                "invocations": int(entry.get("invocation_count", 0)),
            }
            for worker_name, entry in by_worker.items()
            if isinstance(entry, dict)
        ],
        "by_model": [
            {
                "name": str(model_name),
                "tokens": int(entry.get("total_tokens", 0)),
                "cost_usd": float(entry.get("estimated_cost_usd", 0.0)),
                "invocations": int(entry.get("invocation_count", 0)),
            }
            for model_name, entry in by_model.items()
            if isinstance(entry, dict)
        ],
        "compression_events": compression_events,
        "compression_saved_chars": total_saved,
    }


def _to_finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _build_sli_view(runtime_status: dict[str, Any], *, runtime_dir: str = "") -> dict[str, Any]:
    raw_metrics = runtime_status.get("sli_metrics")
    raw_alerts = runtime_status.get("sli_alerts")
    metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
    alerts = [str(item) for item in raw_alerts] if isinstance(raw_alerts, list) else []
    run_elapsed_sec = _to_finite_float(metrics.get("run_elapsed_sec"), 0.0)
    if run_elapsed_sec <= 0.0:
        start_time = _to_finite_float(runtime_status.get("start_time"), 0.0)
        if start_time > 0.0:
            run_elapsed_sec = max(0.0, time.time() - start_time)

    compression_rate_raw = metrics.get("compression_rate")
    compression_rate = _to_finite_float(compression_rate_raw, -1.0)
    if compression_rate < 0.0:
        compression_events = _build_compression_view(runtime_dir)
        original_total = sum(int(item.get("original_chars", 0) or 0) for item in compression_events)
        compressed_total = sum(int(item.get("compressed_chars", 0) or 0) for item in compression_events)
        if original_total > 0:
            compression_rate = max(0.0, min(1.0, (original_total - compressed_total) / float(original_total)))
        else:
            compression_rate = 0.0

    return {
        "metrics": {
            "run_elapsed_sec": run_elapsed_sec,
            "avg_stage_rounds": _to_finite_float(metrics.get("avg_stage_rounds"), 0.0),
            "retry_rate_per_stage": _to_finite_float(metrics.get("retry_rate_per_stage"), 0.0),
            "cost_burn_rate_usd_per_min": _to_finite_float(metrics.get("cost_burn_rate_usd_per_min"), 0.0),
            "compression_rate": compression_rate,
        },
        "alerts": alerts,
        "is_alerting": bool(alerts),
    }


def build_monitor_view(payload: dict[str, Any]) -> dict[str, Any]:
    runtime_status = payload.get("runtime_status") or {}
    summary = payload.get("summary") or {}
    dashboards_raw = payload.get("stage_dashboards") or []
    dashboards = [item for item in dashboards_raw if isinstance(item, dict)]
    ledgers_raw = payload.get("stage_progress_ledgers") or {}
    stage_progress_ledgers = {
        str(stage_name): ledger
        for stage_name, ledger in ledgers_raw.items()
        if isinstance(stage_name, str) and isinstance(ledger, dict)
    } if isinstance(ledgers_raw, dict) else {}
    stage_by_name = {
        str(item.get("stage_name", "")): item
        for item in dashboards
        if str(item.get("stage_name", ""))
    }

    current_stage_name = str(runtime_status.get("current_stage", ""))
    current_stage = stage_by_name.get(current_stage_name)
    if current_stage is None:
        current_stage = next(
            (item for item in dashboards if _normalize_stage_status(str(item.get("status", ""))) == "running"),
            dashboards[0] if dashboards else {},
        )
        current_stage_name = str(current_stage.get("stage_name", ""))

    stage_cards: list[dict[str, Any]] = []
    status_counts = {"passed": 0, "running": 0, "failed": 0, "blocked": 0, "pending": 0}

    current_worker_states = runtime_status.get("worker_states")
    if not isinstance(current_worker_states, dict):
        current_worker_states = current_stage.get("worker_states") if isinstance(current_stage, dict) else {}
    if not isinstance(current_worker_states, dict):
        current_worker_states = {}
    judge_state = str(runtime_status.get("judge_state", "")) or str(current_stage.get("judge_state", ""))
    current_round = _coerce_int(runtime_status.get("current_round"), _coerce_int((current_stage or {}).get("current_round"), 0))
    current_phase = _infer_phase_from_states(
        str(runtime_status.get("phase", "")) or str(current_stage.get("phase", "")),
        worker_states=current_worker_states,
        judge_state=judge_state,
        round_index=current_round,
        latest_check_overview=(current_stage or {}).get("latest_check_overview"),
        latest_nudges=(current_stage or {}).get("latest_nudges"),
        unresolved_actions=(current_stage or {}).get("unresolved_actions"),
        latest_convergence_action=str((current_stage or {}).get("latest_convergence_action", "")),
    )
    phase_label, phase_goal, phase_progress = _describe_phase(current_phase, round_index=current_round)

    for stage in dashboards:
        status = _normalize_stage_status(str(stage.get("status", "")))
        status_counts[status] = status_counts.get(status, 0) + 1
        stage_name = str(stage.get("stage_name", ""))
        ledger = stage_progress_ledgers.get(stage_name, {})
        stage_passed_gates_raw = stage.get("passed_gates")
        if not isinstance(stage_passed_gates_raw, list):
            stage_passed_gates_raw = ledger.get("passed_gates", []) if isinstance(ledger, dict) else []
        stage_passed_gates = [
            str(item) for item in stage_passed_gates_raw
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
        stage_worker_states = stage.get("worker_states") if isinstance(stage.get("worker_states"), dict) else {}
        stage_judge_state = str(stage.get("judge_state", ""))
        stage_phase = _infer_phase_from_states(
            str(stage.get("phase", "")) if stage.get("phase") else "",
            worker_states=stage_worker_states,
            judge_state=stage_judge_state,
            round_index=_coerce_int(stage.get("current_round"), _coerce_int(runtime_status.get("current_round"), 0)),
            latest_check_overview=stage.get("latest_check_overview"),
            latest_nudges=stage.get("latest_nudges"),
            unresolved_actions=stage.get("unresolved_actions"),
            latest_convergence_action=str(stage.get("latest_convergence_action", "")),
        )
        round_index = _coerce_int(stage.get("current_round"), _coerce_int(runtime_status.get("current_round"), 0))
        phase_name, _, default_progress = _describe_phase(stage_phase or current_phase, round_index=round_index)
        max_round = max(1, _coerce_int(stage.get("max_rounds"), 1))
        round_ratio = min(100, int(round_index / max_round * 100))
        stage_progress = 100 if status in {"passed", "failed", "blocked"} else max(default_progress, round_ratio)
        depends_on = list(stage.get("depends_on_stages", []) or []) if isinstance(stage.get("depends_on_stages"), list) else []
        stage_cards.append(
            {
                "name": stage_name,
                "objective": str(stage.get("objective", "")),
                "status": status,
                "status_label": _STATUS_LABEL.get(status, ""),
                "phase_label": phase_name,
                "phase_goal": _describe_phase(stage_phase or current_phase, round_index=round_index)[1],
                "progress": stage_progress,
                "round": round_index,
                "max_round": max_round,
                "depends_on": depends_on,
                "is_current": stage_name == current_stage_name,
                "highlight": _stage_highlight(stage),
                "passed_gates": stage_passed_gates,
                "worker_states": {
                    "worker": str(stage_worker_states.get("worker", "idle")),
                },
                "judge_state": stage_judge_state or "not_started",
            }
        )

    total_stages = max(1, len(stage_cards))
    completed = status_counts["passed"] + status_counts["failed"] + status_counts["blocked"]
    pipeline_progress = min(100, int(completed / total_stages * 100))
    if status_counts["running"] > 0:
        pipeline_progress = min(99, max(pipeline_progress, int((completed + 0.5) / total_stages * 100)))
    current_status = _normalize_stage_status(str((current_stage or {}).get("status", "")))
    overall_state = str(runtime_status.get("overall_state", "")).strip()
    if not overall_state or overall_state == "unknown":
        if status_counts["running"] > 0:
            overall_state = "running"
        elif status_counts["failed"] > 0:
            overall_state = "failed"
        elif status_counts["blocked"] > 0:
            overall_state = "blocked"
        elif status_counts["passed"] == len(stage_cards) and stage_cards:
            overall_state = "passed"
        else:
            overall_state = "pending"

    pipeline_node_states = _compute_pipeline_node_states(current_phase)
    pipeline_nodes_view = [
        {**node, "state": pipeline_node_states.get(node["id"], "pending")}
        for node in _PIPELINE_NODES
    ]

    stage_runtime_signals = payload.get("stage_runtime_signals") or {}
    worker_details = _build_worker_details(
        current_stage,
        stage_runtime_signals.get(current_stage_name),
    )
    focus_items = _build_focus_items(current_stage)
    activity_feed = _build_activity_feed(payload, current_stage_name)
    roadmap = _build_stage_roadmap(payload, current_stage_name)
    active_remote_checks = _build_active_remote_checks(payload, current_stage_name=current_stage_name)
    timeout_recovery = _build_timeout_recovery_overview(payload)
    current_stage_index = next((item["index"] for item in roadmap if item.get("is_current")), 0)
    current_stage_card = next(
        (item for item in stage_cards if item.get("name") == current_stage_name),
        {},
    )

    return {
        "repo": str(runtime_status.get("target_repo", payload.get("runtime_dir", ""))),
        "overall_state": overall_state or "unknown",
        "overall_passed": summary.get("overall_passed"),
        "overall_state_label": _STATUS_LABEL.get(overall_state, ""),
        "pipeline": {
            "total": len(stage_cards),
            "progress": pipeline_progress,
            "counts": status_counts,
        },
        "current": {
            "stage_name": current_stage_name,
            "stage_status": current_status,
            "phase": current_phase or "not_started",
            "phase_label": phase_label,
            "phase_goal": phase_goal,
            "phase_progress": (
                phase_progress
                if current_status == "running"
                else (100 if current_status in {"passed", "failed", "blocked"} else 0)
            ),
            "round": current_round,
            "max_round": max(1, _coerce_int((current_stage or {}).get("max_rounds"), 1)),
            "objective": str((current_stage or {}).get("objective", "")),
            "unresolved_actions": list((current_stage or {}).get("unresolved_actions", []) or [])[:3],
            "passed_gates": list((current_stage_card or {}).get("passed_gates", []) or [])[:10],
            "stage_index": current_stage_index,
            "stage_total": len(roadmap),
        },
        "agents": {
            "worker": {
                "state": str(current_worker_states.get("worker", "idle")),
                "label": _WORKER_LABEL.get(str(current_worker_states.get("worker", "idle")), ""),
                "progress": _WORKER_PROGRESS.get(str(current_worker_states.get("worker", "idle")), 10),
            },
            "judge": {
                "state": judge_state or "not_started",
                "label": _JUDGE_LABEL.get(judge_state or "not_started", ""),
                "progress": _JUDGE_PROGRESS.get(judge_state or "not_started", 10),
            },
        },
        "pipeline_nodes": pipeline_nodes_view,
        "worker_details": worker_details,
        "active_remote_checks": active_remote_checks,
        "timeout_recovery": timeout_recovery,
        "focus_items": focus_items,
        "activity_feed": activity_feed,
        "stages": stage_cards,
        "stage_roadmap": roadmap,
        "cost": _build_cost_view(
            payload.get("cost_ledger") or {},
            runtime_dir=str(payload.get("runtime_dir", "")),
        ),
        "sli": _build_sli_view(runtime_status, runtime_dir=str(payload.get("runtime_dir", ""))),
    }


def render_monitor_html(
    payload: dict[str, Any],
    *,
    auto_refresh_sec: float = 10.0,
    sse_url: str | None = None,
) -> str:
    refresh_seconds = max(0.2, float(auto_refresh_sec))
    view = build_monitor_view(payload)
    view_json = json.dumps(view, ensure_ascii=False)
    refresh_meta = (
        ""
        if sse_url
        else f'<meta http-equiv="refresh" content="{html.escape(str(refresh_seconds))}">'
    )
    sse_json = json.dumps(sse_url) if sse_url else "null"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>Multi Codex 实时监控</title>
  <style>
    :root {{
      --bg: #0b1020;
      --panel: #141b2d;
      --panel-alt: #1a2238;
      --line: #2a3550;
      --text: #e7ecf4;
      --muted: #9fb0c7;
      --ok: #10b981;
      --run: #3b82f6;
      --warn: #f59e0b;
      --bad: #ef4444;
      --impl: #f97316;
      --purple: #a78bfa;
    }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 12px 16px; background: var(--bg); color: var(--text); }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; margin-bottom: 10px; transition: border-color .3s; }}
    .card.highlight {{ animation: pulse-border .6s ease; }}
    @keyframes pulse-border {{ 0%,100% {{ border-color: var(--line); }} 50% {{ border-color: var(--run); box-shadow: 0 0 12px rgba(59,130,246,.3); }} }}
    .card-accent-running {{ border-left: 3px solid var(--run); }}
    .card-accent-passed {{ border-left: 3px solid var(--ok); }}
    .card-accent-failed {{ border-left: 3px solid var(--bad); }}
    .card-accent-blocked {{ border-left: 3px solid var(--warn); }}
    .card-accent-pending {{ border-left: 3px solid var(--line); }}

    /* P6: Compact header */
    .header-bar {{ display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }}
    .header-left {{ display: flex; align-items: center; gap: 12px; }}
    .header-title {{ font-size: 18px; font-weight: 700; margin: 0; white-space: nowrap; }}
    .header-meta {{ display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }}
    .header-meta-item {{ font-size: 13px; color: var(--muted); }}
    .header-meta-item strong {{ color: var(--text); }}
    #live-dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--muted); margin-right:4px; vertical-align: middle; }}
    #live-dot.live {{ background: var(--ok); box-shadow: 0 0 8px rgba(16,185,129,.8); }}

    /* Compact pipeline summary */
    .pipeline-summary {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
    .pipeline-progress-wrap {{ flex: 1; min-width: 200px; }}
    .pipeline-stats {{ display: flex; gap: 12px; font-size: 13px; }}
    .pipeline-stats .stat {{ display: flex; align-items: center; gap: 4px; }}
    .stat-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}

    .muted {{ color: var(--muted); font-size: 13px; }}
    .label {{ font-size: 12px; color: var(--muted); margin-bottom: 4px; text-transform: uppercase; letter-spacing: .5px; }}
    .value {{ font-size: 16px; font-weight: 600; margin-bottom: 2px; }}
    .small {{ font-size: 12px; color: var(--muted); }}

    /* P4: Semantic progress bars */
    .bar {{ width: 100%; height: 8px; background: #0f1629; border-radius: 999px; overflow: hidden; border: 1px solid #1e2d4a; position: relative; }}
    .bar > span {{ display: block; height: 100%; width: 0%; transition: width .4s ease, background .3s; border-radius: 999px; }}
    .bar-label {{ position: relative; }}
    .bar-pct {{ position: absolute; right: 0; top: -16px; font-size: 11px; color: var(--muted); }}
    .bar-color-planning span {{ background: var(--run); }}
    .bar-color-implementing span {{ background: var(--impl); }}
    .bar-color-passed span {{ background: var(--ok); }}
    .bar-color-failed span {{ background: var(--bad); }}
    .bar-color-blocked span {{ background: var(--warn); }}
    .bar-color-idle span {{ background: var(--muted); }}
    .bar-color-default span {{ background: var(--run); }}

    .pill {{ display: inline-block; border-radius: 999px; padding: 2px 9px; font-size: 11px; border: 1px solid var(--line); }}
    .status-passed {{ color: var(--ok); border-color: var(--ok); }}
    .status-running {{ color: var(--run); border-color: var(--run); }}
    .status-blocked {{ color: var(--warn); border-color: var(--warn); }}
    .status-failed {{ color: var(--bad); border-color: var(--bad); }}
    .status-pending {{ color: var(--muted); border-color: var(--line); }}

    /* P2: Pipeline nodes */
    .pipeline-nodes {{ display: flex; align-items: center; gap: 0; overflow-x: auto; padding: 8px 0; }}
    .pn {{ display: flex; flex-direction: column; align-items: center; min-width: 72px; position: relative; }}
    .pn-icon {{ font-size: 20px; margin-bottom: 4px; }}
    .pn-label {{ font-size: 11px; color: var(--muted); text-align: center; white-space: nowrap; }}
    .pn-dot {{ width: 10px; height: 10px; border-radius: 50%; margin-bottom: 4px; border: 2px solid var(--line); background: transparent; }}
    .pn-dot.done {{ background: var(--ok); border-color: var(--ok); }}
    .pn-dot.active {{ background: var(--run); border-color: var(--run); box-shadow: 0 0 8px rgba(59,130,246,.6); animation: dot-pulse 1.5s infinite; }}
    .pn-dot.failed {{ background: var(--bad); border-color: var(--bad); }}
    @keyframes dot-pulse {{ 0%,100% {{ box-shadow: 0 0 4px rgba(59,130,246,.4); }} 50% {{ box-shadow: 0 0 12px rgba(59,130,246,.8); }} }}
    .pn-arrow {{ color: var(--line); font-size: 16px; margin: 0 2px; align-self: center; padding-bottom: 16px; }}
    .pn-arrow.done {{ color: var(--ok); }}

    /* Agent row */
    .agent-row {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }}
    @media (max-width: 700px) {{ .agent-row {{ grid-template-columns: 1fr; }} }}
    .agent-card {{ background: var(--panel-alt); border: 1px solid var(--line); border-radius: 10px; padding: 12px; }}
    .agent-name {{ font-size: 13px; font-weight: 600; margin-bottom: 6px; }}
    .agent-state {{ font-size: 14px; margin-bottom: 4px; }}
    .agent-detail {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}

    .remote-check-list {{ display: flex; flex-direction: column; gap: 8px; }}
    .remote-check-item {{ background: var(--panel-alt); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }}
    .remote-check-head {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; }}
    .remote-check-title {{ font-weight: 600; color: var(--fg); }}
    .remote-check-meta {{ color: var(--muted); font-size: 11px; margin-top: 4px; word-break: break-all; }}
    .remote-check-empty {{ font-size: 12px; color: var(--muted); }}
    .recovery-summary-grid {{ display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 8px; }}
    @media (max-width: 700px) {{ .recovery-summary-grid {{ grid-template-columns: 1fr 1fr; }} }}
    .recovery-stat {{ background: var(--panel-alt); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }}
    .recovery-stat-label {{ font-size: 11px; color: var(--muted); }}
    .recovery-stat-value {{ font-size: 18px; font-weight: 700; margin-top: 2px; }}
    #timeout-recovery-section.timeout-recovery-alerting {{ border-color: rgba(239,68,68,.8); box-shadow: 0 0 0 1px rgba(239,68,68,.35) inset; }}
    .recovery-alert-list {{ margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }}
    .recovery-alert-item {{ border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; font-size: 12px; background: var(--panel-alt); }}
    .recovery-alert-item.level-error {{ border-color: rgba(239,68,68,.7); background: rgba(239,68,68,.1); }}
    .recovery-alert-item.level-warning {{ border-color: rgba(245,158,11,.7); background: rgba(245,158,11,.1); }}
    .recovery-threshold-note {{ margin-top: 8px; font-size: 11px; color: var(--muted); }}
    .recovery-recent-list {{ margin-top: 10px; display: flex; flex-direction: column; gap: 6px; }}
    .recovery-recent-item {{ background: var(--panel-alt); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; font-size: 12px; }}
    .recovery-recent-meta {{ color: var(--muted); font-size: 11px; margin-top: 3px; }}

    /* P5: Worker compare (collapsible) */
    .collapse-toggle {{ cursor: pointer; user-select: none; display: flex; align-items: center; gap: 6px; }}
    .collapse-toggle .arrow {{ transition: transform .2s; font-size: 12px; }}
    .collapse-toggle.open .arrow {{ transform: rotate(90deg); }}
    .collapse-body {{ max-height: 0; overflow: hidden; transition: max-height .3s ease; }}
    .collapse-body.open {{ max-height: 600px; }}
    .compare-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 8px; }}
    @media (max-width: 700px) {{ .compare-grid {{ grid-template-columns: 1fr; }} }}
    .compare-card {{ background: var(--panel-alt); border: 1px solid var(--line); border-radius: 8px; padding: 10px; }}
    .compare-card h4 {{ margin: 0 0 6px 0; font-size: 13px; }}
    .compare-row {{ display: flex; justify-content: space-between; font-size: 12px; padding: 3px 0; border-bottom: 1px solid #1e2d4a; }}
    .compare-row:last-child {{ border-bottom: none; }}

    /* P3: Focus items (summary + expandable detail) */
    .focus-item {{ display: flex; align-items: flex-start; gap: 8px; padding: 6px 8px; border-radius: 6px; margin-bottom: 4px; font-size: 13px; cursor: default; }}
    .focus-item.has-detail {{ cursor: pointer; }}
    .focus-item.sev-error {{ background: rgba(239,68,68,.1); border-left: 3px solid var(--bad); }}
    .focus-item.sev-critical {{ background: rgba(239,68,68,.15); border-left: 3px solid var(--bad); }}
    .focus-item.sev-warning {{ background: rgba(245,158,11,.1); border-left: 3px solid var(--warn); }}
    .focus-item.sev-info {{ background: rgba(59,130,246,.08); border-left: 3px solid var(--run); }}
    .focus-icon {{ font-size: 14px; flex-shrink: 0; margin-top: 1px; }}
    .focus-summary {{ flex: 1; word-break: break-word; }}
    .focus-detail {{ display: none; font-size: 11px; color: var(--muted); margin-top: 4px; word-break: break-word; line-height: 1.5; white-space: pre-line; }}
    .focus-item.expanded .focus-detail {{ display: block; }}
    .focus-expand-hint {{ font-size: 10px; color: var(--muted); margin-left: 4px; flex-shrink: 0; }}

    /* P1: Activity feed (summary + expandable detail) */
    .feed-item {{ display: flex; align-items: flex-start; gap: 8px; padding: 5px 0; border-bottom: 1px solid #1a2540; font-size: 12px; cursor: default; }}
    .feed-item.has-detail {{ cursor: pointer; }}
    .feed-item:last-child {{ border-bottom: none; }}
    .feed-icon {{ font-size: 13px; flex-shrink: 0; }}
    .feed-stage {{ color: var(--purple); font-weight: 500; white-space: nowrap; }}
    .feed-summary {{ color: var(--muted); flex: 1; word-break: break-word; }}
    .feed-detail {{ display: none; font-size: 11px; color: var(--muted); margin-top: 3px; word-break: break-word; line-height: 1.4; white-space: pre-line; }}
    .feed-item.expanded .feed-detail {{ display: block; }}

    /* Stage roadmap */
    .roadmap {{ display: grid; grid-template-columns: 1fr; gap: 8px; padding: 8px 0; }}
    .roadmap-node {{ display: grid; grid-template-columns: auto 1fr auto; align-items: flex-start; gap: 10px; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; background: var(--panel-alt); }}
    .roadmap-node.is-current {{ border-color: var(--run); box-shadow: 0 0 0 1px rgba(59,130,246,.25) inset; }}
    .roadmap-order {{ width: 26px; height: 26px; border-radius: 999px; border: 1px solid var(--line); display: flex; align-items: center; justify-content: center; font-size: 11px; color: var(--muted); }}
    .roadmap-main {{ min-width: 0; }}
    .roadmap-dot {{ width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--line); background: transparent; margin-top: 6px; }}
    .roadmap-dot.rm-done {{ background: var(--ok); border-color: var(--ok); }}
    .roadmap-dot.rm-current {{ background: var(--run); border-color: var(--run); box-shadow: 0 0 10px rgba(59,130,246,.6); animation: dot-pulse 1.5s infinite; }}
    .roadmap-dot.rm-failed {{ background: var(--bad); border-color: var(--bad); }}
    .roadmap-name {{ font-size: 13px; font-weight: 600; line-height: 1.3; }}
    .roadmap-name.rm-current {{ color: var(--run); font-weight: 700; }}
    .roadmap-name.rm-done {{ color: var(--ok); }}
    .roadmap-obj {{ font-size: 11px; color: var(--muted); margin-top: 3px; line-height: 1.45; }}
    .roadmap-meta {{ display: flex; align-items: center; gap: 6px; white-space: nowrap; padding-left: 8px; }}

    /* Stage detail list */
    .stage-list {{ display: flex; flex-direction: column; gap: 6px; padding: 4px 0; }}
    .stage-list-item {{ display: flex; gap: 10px; align-items: flex-start; padding: 8px 10px; border-radius: 8px; background: var(--panel-alt); border: 1px solid var(--line); }}
    .stage-list-item.is-current {{ border-color: var(--run); background: rgba(59,130,246,.04); }}
    .stage-list-left {{ flex-shrink: 0; padding-top: 4px; }}
    .stage-list-dot {{ display: block; width: 10px; height: 10px; border-radius: 50%; border: 2px solid var(--line); }}
    .st-dot-passed {{ background: var(--ok); border-color: var(--ok); }}
    .st-dot-running {{ background: var(--run); border-color: var(--run); box-shadow: 0 0 6px rgba(59,130,246,.5); animation: dot-pulse 1.5s infinite; }}
    .st-dot-failed {{ background: var(--bad); border-color: var(--bad); }}
    .st-dot-blocked {{ background: var(--warn); border-color: var(--warn); }}
    .stage-list-body {{ flex: 1; min-width: 0; }}
    .stage-list-head {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
    .stage-list-name {{ font-size: 13px; font-weight: 600; }}
    .stage-list-info {{ display: flex; flex-wrap: wrap; gap: 4px 10px; font-size: 11px; color: var(--muted); margin-top: 3px; }}
    .stage-list-obj {{ font-size: 12px; color: var(--muted); margin-top: 3px; line-height: 1.4; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }}
    .stage-list-gates {{ font-size: 11px; color: var(--muted); margin-top: 2px; line-height: 1.4; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }}
    .stage-dep-tag {{ color: var(--purple); }}
    .current-gates {{ margin-top: 4px; }}

    .section-title {{ font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 8px; text-transform: uppercase; letter-spacing: .5px; }}

    /* Interactive intake panel */
    .intake-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    .intake-field {{ display: flex; flex-direction: column; gap: 6px; }}
    .intake-field label {{ font-size: 12px; color: var(--muted); }}
    .intake-field input:not([type="checkbox"]),
    .intake-field textarea {{ width: 100%; border-radius: 8px; border: 1px solid var(--line); background: var(--panel-alt); color: var(--text); padding: 8px 10px; font-size: 13px; }}
    .intake-field textarea {{ min-height: 72px; resize: vertical; }}
    .target-repo-list {{ display: flex; flex-direction: column; gap: 8px; }}
    .target-repo-input {{ width: 100%; }}
    .target-repo-actions {{ display: flex; align-items: center; gap: 8px; margin-top: 4px; }}
    .target-repo-actions button {{ width: 28px; height: 28px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel-alt); color: var(--text); cursor: pointer; font-size: 16px; line-height: 1; }}
    .target-repo-actions button:hover {{ border-color: var(--run); }}
    .intake-checkbox {{ align-items: flex-end; }}
    .intake-checkbox label {{ display: inline-flex; align-items: center; gap: 8px; color: var(--text); font-size: 14px; line-height: 1.2; white-space: nowrap; margin-left: auto; justify-content: flex-end; }}
    .intake-checkbox input[type="checkbox"] {{ width: auto; margin: 0; padding: 0; flex: 0 0 auto; accent-color: var(--run); }}
    .intake-actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
    .intake-actions button {{ border-radius: 8px; border: 1px solid var(--line); background: var(--panel-alt); color: var(--text); padding: 6px 10px; font-size: 12px; cursor: pointer; }}
    .intake-actions button:hover {{ border-color: var(--run); }}
    .intake-status {{ margin-top: 8px; font-size: 12px; color: var(--muted); white-space: pre-wrap; }}
    .intake-status.error {{ color: var(--bad); }}
    .intake-status.ok {{ color: var(--ok); }}
    .intake-draft {{ margin-top: 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-alt); padding: 10px; font-size: 12px; line-height: 1.45; max-height: 300px; overflow: auto; white-space: pre-wrap; word-break: break-word; }}
    .remote-validation-panel {{ margin-top: 10px; padding: 10px; border: 1px solid var(--line); border-radius: 8px; background: #101a2d; display: none; }}
    .remote-server-grid {{ display: grid; grid-template-columns: repeat(2, minmax(180px, 1fr)); gap: 8px; }}
    .password-row {{ display: flex; gap: 6px; }}
    .password-row input {{ flex: 1; }}
    .password-row button {{ border-radius: 8px; border: 1px solid var(--line); background: var(--panel-alt); color: var(--text); padding: 0 8px; font-size: 12px; cursor: pointer; }}
    @media (max-width: 700px) {{ .intake-grid {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 700px) {{ .remote-server-grid {{ grid-template-columns: 1fr; }} }}

    ul {{ margin: 6px 0 0 16px; padding: 0; }}
  </style>
</head>
<body>

  <!-- P6: Compact header -->
  <section class="card" style="padding:10px 16px;">
    <div class="header-bar">
      <div class="header-left">
        <h1 class="header-title">Multi Codex 运行监控</h1>
        <span class="muted"><span id="live-dot"></span><span id="live-label">等待数据</span></span>
      </div>
      <div class="header-meta">
        <span class="header-meta-item" id="header-repo"></span>
        <span class="header-meta-item" id="header-stage"></span>
        <span class="header-meta-item" id="header-round"></span>
      </div>
    </div>
    <div class="pipeline-summary" style="margin-top:8px;">
      <div class="pipeline-progress-wrap">
        <div class="bar-label">
          <span class="bar-pct" id="pipeline-pct"></span>
          <div class="bar bar-color-default"><span id="pipeline-bar"></span></div>
        </div>
      </div>
      <div class="pipeline-stats" id="pipeline-stats"></div>
    </div>
    <!-- Cost summary inline -->
    <div id="cost-summary" style="margin-top:6px; font-size:13px; color:var(--muted); display:none;"></div>
    <div id="sli-summary" style="margin-top:4px; font-size:13px; color:var(--muted); display:none;"></div>
  </section>

  <section class="card">
    <div class="section-title">交互式需求分析（Intake）</div>
    <div class="intake-grid">
      <div class="intake-field" style="grid-column: 1 / -1;">
        <label>目标仓库绝对路径（支持多个）</label>
        <div id="intake-target-repo-list" class="target-repo-list"></div>
        <div class="target-repo-actions">
          <button type="button" onclick="addTargetRepoInput()" title="新增目标仓库路径">+</button>
          <button type="button" onclick="removeTargetRepoInput()" title="删除最后一个目标仓库路径">-</button>
        </div>
      </div>
      <div class="intake-field" style="grid-column: 1 / -1;">
        <label for="intake-goal">目标描述</label>
        <textarea id="intake-goal" placeholder="描述你希望系统最终实现的功能目标"></textarea>
      </div>
      <div class="intake-field intake-checkbox" style="grid-column: 1 / -1;">
        <label for="intake-need-remote">
          <input id="intake-need-remote" type="checkbox" onchange="toggleRemoteValidationInputs()">
          <span>需要服务器环境验证</span>
        </label>
      </div>
      <div class="intake-field">
        <label for="intake-files">上传文件或目录（可多选）</label>
        <input id="intake-files" type="file" multiple webkitdirectory directory>
      </div>
      <div class="intake-field">
        <label for="intake-feedback">反馈（用于重生草案）</label>
        <textarea id="intake-feedback" placeholder="例如：请拆成3个阶段，先做API契约，再做实现，再做回归"></textarea>
      </div>
    </div>

    <div class="remote-validation-panel" id="remote-validation-panel">
      <div class="section-title" style="margin-bottom:8px;">服务器连接信息</div>
      <div class="small" style="margin-bottom:8px;">仅在勾选“需要服务器环境验证”后使用。</div>
      <div class="remote-server-grid">
        <div class="intake-field">
          <label for="remote-primary-host">服务器 1 IP/Host</label>
          <input id="remote-primary-host" type="text" placeholder="10.0.0.1">
        </div>
        <div class="intake-field">
          <label for="remote-primary-user">服务器 1 登录名</label>
          <input id="remote-primary-user" type="text" placeholder="root" value="root">
        </div>
        <div class="intake-field">
          <label for="remote-primary-password">服务器 1 密码</label>
          <div class="password-row">
            <input id="remote-primary-password" type="password" placeholder="password">
            <button type="button" onclick="togglePasswordVisibility('remote-primary-password', this)">显示</button>
          </div>
        </div>
        <div class="intake-field">
          <label for="remote-primary-workdir">服务器 1 工作目录</label>
          <input id="remote-primary-workdir" type="text" placeholder="/workspace/project">
        </div>
      </div>
      <div class="remote-server-grid" style="margin-top:8px;">
        <div class="intake-field">
          <label for="remote-secondary-host">服务器 2 IP/Host（可选）</label>
          <input id="remote-secondary-host" type="text" placeholder="10.0.0.2">
        </div>
        <div class="intake-field">
          <label for="remote-secondary-user">服务器 2 登录名</label>
          <input id="remote-secondary-user" type="text" placeholder="root" value="root">
        </div>
        <div class="intake-field">
          <label for="remote-secondary-password">服务器 2 密码</label>
          <div class="password-row">
            <input id="remote-secondary-password" type="password" placeholder="password">
            <button type="button" onclick="togglePasswordVisibility('remote-secondary-password', this)">显示</button>
          </div>
        </div>
        <div class="intake-field">
          <label for="remote-secondary-workdir">服务器 2 工作目录</label>
          <input id="remote-secondary-workdir" type="text" placeholder="/workspace/project">
        </div>
      </div>
    </div>

    <div class="intake-actions">
      <button type="button" onclick="startIntakeSession()">1. 创建会话</button>
      <button type="button" onclick="saveIntakeSessionEdits()">2. 保存会话配置</button>
      <button type="button" onclick="uploadIntakeFiles()">3. 上传附件</button>
      <button type="button" onclick="analyzeIntake()">4. Analyze</button>
      <button type="button" onclick="regenerateIntake()">5. 反馈重生</button>
      <button type="button" onclick="confirmIntake()">6. 确认并启动</button>
    </div>
    <div class="intake-status" id="intake-status">尚未创建 intake 会话。</div>
    <div class="intake-draft" id="intake-draft">Analyze 后会在这里显示 stage objective 与 test case 草案。</div>
  </section>

  <!-- Stage roadmap -->
  <section class="card" id="roadmap-section" style="padding:10px 16px; display:none;">
    <div class="section-title">🗺️ 项目阶段总览</div>
    <div class="roadmap" id="roadmap-container"></div>
  </section>

  <!-- P2: Execution pipeline nodes -->
  <section class="card" style="padding:10px 16px;">
    <div class="section-title">执行流水线（当前阶段内部节点）</div>
    <div class="pipeline-nodes" id="pipeline-nodes"></div>
    <div class="small" id="phase-desc" style="margin-top:4px;"></div>
    <div class="small current-gates" id="current-gates"></div>
  </section>

  <!-- Agent status with semantic bars (P4) -->
  <section class="card">
    <div class="section-title">智能体状态</div>
    <div class="agent-row" id="agent-row"></div>
  </section>

  <section class="card" id="remote-checks-section" style="display:none;">
    <div class="section-title">远端检查实时心跳</div>
    <div class="remote-check-list" id="remote-check-list"></div>
  </section>

  <section class="card" id="timeout-recovery-section" style="display:none;">
    <div class="section-title">远端超时恢复统计</div>
    <div class="recovery-summary-grid" id="timeout-recovery-stats"></div>
    <div class="recovery-threshold-note" id="timeout-recovery-thresholds"></div>
    <div class="recovery-alert-list" id="timeout-recovery-alerts"></div>
    <div class="recovery-recent-list" id="timeout-recovery-recent"></div>
  </section>

  <!-- P5: Worker compare panel (collapsible) -->
  <section class="card">
    <div class="collapse-toggle" id="compare-toggle" onclick="toggleCompare()">
      <span class="arrow">▶</span>
      <span class="section-title" style="margin-bottom:0;">Worker 对比详情</span>
    </div>
    <div class="collapse-body" id="compare-body">
      <div class="compare-grid" id="compare-grid"></div>
    </div>
  </section>

  <!-- P3: Focus items -->
  <section class="card" id="focus-section" style="display:none;">
    <div class="section-title">📋 当前关注点</div>
    <div id="focus-list"></div>
  </section>

  <!-- Stage detail list -->
  <section class="card">
    <div class="section-title">阶段状态详情</div>
    <div class="stage-list" id="stage-list"></div>
  </section>

  <!-- P1: Activity feed -->
  <section class="card" id="feed-section" style="display:none;">
    <div class="section-title">📜 实时事件流</div>
    <div id="feed-list"></div>
  </section>

  <script>
    const initialView = {view_json};
    const sseUrl = {sse_json};
    const refreshSec = {refresh_seconds};

    function esc(text) {{
      return String(text ?? "").replace(/[&<>"']/g, (c) => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]));
    }}
    function safeNumber(value, fallback = 0) {{
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : fallback;
    }}
    function fixedNumber(value, digits, fallback = 0) {{
      return safeNumber(value, fallback).toFixed(digits);
    }}
    function statusClass(s) {{
      s = String(s||"pending").toLowerCase();
      if (["passed","success","ok"].includes(s)) return "status-passed";
      if (["running","in_progress"].includes(s)) return "status-running";
      if (["failed","error"].includes(s)) return "status-failed";
      if (s==="blocked") return "status-blocked";
      return "status-pending";
    }}
    function barColor(state) {{
      const s = String(state||"").toLowerCase();
      if (["planning","replan_required","waiting_for_worker_plan","planning_stage_gate"].includes(s)) return "bar-color-planning";
      if (["implementing","plan_approved"].includes(s)) return "bar-color-implementing";
      if (["passed","done","approved"].includes(s)) return "bar-color-passed";
      if (["failed","rejected"].includes(s)) return "bar-color-failed";
      if (s==="blocked") return "bar-color-blocked";
      if (s==="idle"||s==="not_started") return "bar-color-idle";
      return "bar-color-default";
    }}
    function setBar(el, value, colorClass) {{
      if (!el) return;
      const span = el.querySelector("span");
      if (span) span.style.width = `${{Math.max(0,Math.min(100,Number(value||0)))}}%`;
      if (colorClass) {{
        el.className = el.className.replace(/bar-color-\\S+/g, "").trim() + " " + colorClass;
      }}
    }}
    function agentLabel(agent) {{
      const state = (agent||{{}}).state||"-";
      const label = (agent||{{}}).label||"";
      return label ? `${{state}}（${{label}}）` : state;
    }}
    function formatDuration(sec) {{
      const value = Math.max(0, Number(sec || 0));
      const mins = Math.floor(value / 60);
      const rem = Math.floor(value % 60);
      if (mins <= 0) return `${{rem}}s`;
      return `${{mins}}m ${{rem}}s`;
    }}
    function summarizeActionText(text) {{
      const raw = String(text || "");
      const lowered = raw.toLowerCase();
      if (lowered.startsWith("fix all compile/runtime blockers")) return "先修复编译/运行阻断，恢复核心远端命令通过";
      if (lowered.startsWith("for worker, refactor core module cleanup/control flow")) return "Worker 需要修复核心模块的清理/控制流问题";
      if (lowered.startsWith("for worker, resolve exported symbol/api regression")) return "Worker 需要修复导出符号/API 回归";
      if (lowered.includes("type-visibility regression in include headers")) return "需要确认 include 头文件的类型可见性回归已修复";
      if (lowered.startsWith("produce and validate `docs/p2_window_report.json`")) return "补齐并验证 `docs/p2_window_report.json` 证据产物";
      if (lowered.startsWith("re-run full post-triage harness")) return "重新执行完整验证并附带干净证据";
      if (lowered.includes("automated checks still failing")) {{
        if (lowered.startsWith("worker:")) return "Worker 自动检查仍失败，需要继续修复";

        return "自动检查仍失败，需要先修复后再推进";
      }}
      return raw;
    }}
    function simplifySignalSummary(text) {{
      const raw = String(text || "");
      if (!raw) return {{summary: raw, detail: ""}};
      const arrowIndex = raw.indexOf(" -> ");
      const head = arrowIndex >= 0 ? raw.slice(0, arrowIndex) : raw;
      const parts = head.split(":", 4);
      if (parts.length === 4) {{
        const actor = parts[0];
        const category = parts[1];
        const actorLabel = actor === "worker" ? "Worker" : "当前阶段";
        if (category === "todo_enforcement") return {{summary: `${{actorLabel}} 偏离已批准方案，需要先回到既定修复范围`, detail: raw}};
        if (category === "artifact_missing") return {{summary: "需要补齐证据产物并满足产物契约后再申请通过", detail: raw}};
        if (category === "no_progress") return {{summary: "本轮没有新的有效改动，需要收敛阻塞点或切换修复策略", detail: raw}};
        if (category === "error_recovery") return {{summary: `${{actorLabel}} 仍卡在远端/运行阻断，先定位具体失败子系统`, detail: raw}};
      }}
      if (raw.startsWith("Fix all compile/runtime blockers") || raw.startsWith("For worker_") || raw.startsWith("Re-run full post-triage harness")) {{
        return {{summary: summarizeActionText(raw), detail: raw}};
      }}
      return {{summary: raw, detail: ""}};
    }}
    function normalizeUiItem(item) {{
      const rawSummary = String(item?.summary || item?.text || item?.message || "");
      const result = simplifySignalSummary(rawSummary);
      const detail = String(item?.detail || "");
      return {{
        ...item,
        summary: result.summary || rawSummary,
        detail: detail || result.detail || "",
      }};
    }}

    let intakeSessionId = "";
    function setIntakeStatus(message, isError = false) {{
      const el = document.getElementById("intake-status");
      if (!el) return;
      el.textContent = message || "";
      el.classList.remove("error", "ok");
      el.classList.add(isError ? "error" : "ok");
    }}
    function addTargetRepoInput(value = "") {{
      const container = document.getElementById("intake-target-repo-list");
      if (!container) return;
      const input = document.createElement("input");
      input.type = "text";
      input.className = "target-repo-input";
      input.placeholder = "/abs/path/to/repo";
      input.value = String(value || "");
      container.appendChild(input);
    }}
    function ensureTargetRepoInputs() {{
      const container = document.getElementById("intake-target-repo-list");
      if (!container) return;
      if ((container.querySelectorAll(".target-repo-input") || []).length === 0) {{
        addTargetRepoInput("");
      }}
    }}
    function removeTargetRepoInput() {{
      const container = document.getElementById("intake-target-repo-list");
      if (!container) return;
      const inputs = Array.from(container.querySelectorAll(".target-repo-input"));
      if (inputs.length <= 1) {{
        if (inputs.length === 1) inputs[0].value = "";
        return;
      }}
      const last = inputs[inputs.length - 1];
      if (last) last.remove();
    }}
    function setTargetRepoInputs(values) {{
      const container = document.getElementById("intake-target-repo-list");
      if (!container) return;
      const repos = Array.isArray(values) ? values : [];
      container.innerHTML = "";
      if (repos.length === 0) {{
        addTargetRepoInput("");
        return;
      }}
      repos.forEach((repo) => addTargetRepoInput(String(repo || "")));
    }}
    function collectTargetRepos() {{
      const container = document.getElementById("intake-target-repo-list");
      if (!container) return [];
      const seen = new Set();
      return Array.from(container.querySelectorAll(".target-repo-input"))
        .map((el) => String(el?.value || "").trim())
        .filter((repo) => {{
          if (!repo || seen.has(repo)) return false;
          seen.add(repo);
          return true;
        }});
    }}
    function renderIntakeDraft(draft, session) {{
      const draftEl = document.getElementById("intake-draft");
      if (draftEl) {{
        draftEl.textContent = JSON.stringify(draft || {{}}, null, 2);
      }}
      if (session && session.session_id) {{
        intakeSessionId = String(session.session_id);
      }}
      const sessionTargetRepos = Array.isArray(session?.target_repos)
        ? session.target_repos
        : (session?.target_repo ? [session.target_repo] : []);
      setTargetRepoInputs(sessionTargetRepos);
      const remote = session?.remote_validation || {{}};
      const enabled = Boolean(remote.enabled);
      const checkbox = document.getElementById("intake-need-remote");
      if (checkbox) checkbox.checked = enabled;
      const servers = Array.isArray(remote.servers) ? remote.servers : [];
      const primary = servers[0] || {{}};
      const secondary = servers[1] || {{}};
      const assignValue = (id, value) => {{
        const el = document.getElementById(id);
        if (el) el.value = String(value || "");
      }};
      assignValue("remote-primary-host", primary.host || "");
      assignValue("remote-primary-user", primary.user || "root");
      assignValue("remote-primary-password", primary.password || "");
      assignValue("remote-primary-workdir", primary.workdir || "");
      assignValue("remote-secondary-host", secondary.host || "");
      assignValue("remote-secondary-user", secondary.user || "root");
      assignValue("remote-secondary-password", secondary.password || "");
      assignValue("remote-secondary-workdir", secondary.workdir || "");
      toggleRemoteValidationInputs();
    }}
    function togglePasswordVisibility(inputId, buttonEl) {{
      const input = document.getElementById(inputId);
      if (!input) return;
      const reveal = input.type === "password";
      input.type = reveal ? "text" : "password";
      if (buttonEl) buttonEl.textContent = reveal ? "隐藏" : "显示";
    }}
    function toggleRemoteValidationInputs() {{
      const checkbox = document.getElementById("intake-need-remote");
      const panel = document.getElementById("remote-validation-panel");
      if (!panel) return;
      panel.style.display = checkbox && checkbox.checked ? "block" : "none";
    }}
    function collectRemoteValidationPayload() {{
      const needRemote = Boolean(document.getElementById("intake-need-remote")?.checked);
      if (!needRemote) {{
        return {{ enabled: false, servers: [] }};
      }}
      const value = (id) => String(document.getElementById(id)?.value || "").trim();
      const primaryHost = value("remote-primary-host");
      const primaryWorkdir = value("remote-primary-workdir");
      const primaryUser = value("remote-primary-user") || "root";
      const primaryPassword = value("remote-primary-password");
      const servers = [{{
        label: "server_1",
        host: primaryHost,
        user: primaryUser,
        password: primaryPassword,
        workdir: primaryWorkdir,
      }}];
      const secondaryHost = value("remote-secondary-host");
      const secondaryUserRaw = value("remote-secondary-user");
      const secondaryPassword = value("remote-secondary-password");
      const secondaryWorkdir = value("remote-secondary-workdir");
      const secondaryHasAny = Boolean(
        secondaryHost
        || secondaryPassword
        || secondaryWorkdir
        || (secondaryUserRaw && secondaryUserRaw !== "root")
      );
      if (secondaryHasAny) {{
        servers.push({{
          label: "server_2",
          host: secondaryHost,
          user: secondaryUserRaw || "root",
          password: secondaryPassword,
          workdir: secondaryWorkdir,
        }});
      }}
      return {{ enabled: needRemote, servers: servers }};
    }}
    function validateRemoteValidationPayload(remoteValidation) {{
      if (!remoteValidation || !remoteValidation.enabled) {{
        return "";
      }}
      if (!Array.isArray(remoteValidation.servers) || remoteValidation.servers.length === 0) {{
        return "勾选了服务器环境验证，但服务器 1 的 IP/工作目录未填写完整。";
      }}
      const primary = remoteValidation.servers[0] || {{}};
      const primaryHost = String(primary.host || "").trim();
      const primaryWorkdir = String(primary.workdir || "").trim();
      if (!primaryHost || !primaryWorkdir) {{
        return "服务器 1 必须同时填写 IP 和工作目录。";
      }}
      const secondary = remoteValidation.servers[1];
      if (secondary) {{
        const secondaryHost = String(secondary.host || "").trim();
        const secondaryWorkdir = String(secondary.workdir || "").trim();
        if (!secondaryHost) {{
          return "服务器 2 已填写信息但缺少 IP。";
        }}
        if (!secondaryWorkdir) {{
          return "服务器 2 已填写信息但缺少工作目录。";
        }}
      }}
      return "";
    }}
    async function saveIntakeSessionEdits(options = {{}}) {{
      const requireSession = options.requireSession !== false;
      const requireGoal = options.requireGoal === true;
      const quiet = options.quiet === true;
      if (!intakeSessionId) {{
        if (requireSession && !quiet) {{
          setIntakeStatus("请先创建会话。", true);
        }}
        return false;
      }}
      const goalEl = document.getElementById("intake-goal");
      const goal = String(goalEl?.value || "").trim();
      const targetRepos = collectTargetRepos();
      const targetRepo = targetRepos[0] || "";
      if (requireGoal && !goal) {{
        if (!quiet) {{
          setIntakeStatus("请先填写目标描述。", true);
        }}
        return false;
      }}
      const remoteValidation = collectRemoteValidationPayload();
      const remoteError = validateRemoteValidationPayload(remoteValidation);
      if (remoteError) {{
        if (!quiet) {{
          setIntakeStatus(remoteError, true);
        }}
        return false;
      }}
      try {{
        const data = await postJson("./api/intake/session/update", {{
          session_id: intakeSessionId,
          goal: goal,
          target_repo: targetRepo,
          target_repos: targetRepos,
          remote_validation: remoteValidation,
        }});
        renderIntakeDraft(data.session?.draft || {{}}, data.session || null);
        if (!quiet) {{
          setIntakeStatus(`会话配置已保存：${{intakeSessionId}}`);
        }}
        return true;
      }} catch (error) {{
        if (!quiet) {{
          setIntakeStatus(`保存会话配置失败：${{error.message || error}}`, true);
        }}
        return false;
      }}
    }}
    async function postJson(url, body) {{
      const response = await fetch(url, {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify(body || {{}}),
      }});
      const data = await response.json().catch(() => ({{ok: false, error: "invalid_json"}}));
      if (!response.ok || data.ok === false) {{
        throw new Error(String(data.error || `http_${{response.status}}`));
      }}
      return data;
    }}
    async function startIntakeSession() {{
      const goalEl = document.getElementById("intake-goal");
      const goal = String(goalEl?.value || "").trim();
      const targetRepos = collectTargetRepos();
      const targetRepo = targetRepos[0] || "";
      if (!goal) {{
        setIntakeStatus("请先填写目标描述。", true);
        return;
      }}
      try {{
        const remoteValidation = collectRemoteValidationPayload();
        const remoteError = validateRemoteValidationPayload(remoteValidation);
        if (remoteError) {{
          setIntakeStatus(remoteError, true);
          return;
        }}
        const data = await postJson("./api/intake/session", {{
          goal: goal,
          target_repo: targetRepo,
          target_repos: targetRepos,
          remote_validation: remoteValidation,
        }});
        intakeSessionId = String(data.session?.session_id || "");
        renderIntakeDraft(data.session?.draft || {{}}, data.session || null);
        setIntakeStatus(`会话已创建：${{intakeSessionId}}`);
      }} catch (error) {{
        setIntakeStatus(`创建会话失败：${{error.message || error}}`, true);
      }}
    }}
    async function uploadIntakeFiles() {{
      if (!intakeSessionId) {{
        setIntakeStatus("请先创建会话。", true);
        return;
      }}
      const fileEl = document.getElementById("intake-files");
      const files = fileEl?.files ? Array.from(fileEl.files) : [];
      if (files.length === 0) {{
        setIntakeStatus("请选择至少一个文件。", true);
        return;
      }}
      const formData = new FormData();
      formData.append("session_id", intakeSessionId);
      files.forEach((file) => {{
        const rel = file.webkitRelativePath || file.name;
        formData.append("files", file, rel);
      }});
      try {{
        const response = await fetch("./api/intake/upload", {{
          method: "POST",
          body: formData,
        }});
        const data = await response.json().catch(() => ({{ok: false, error: "invalid_json"}}));
        if (!response.ok || data.ok === false) {{
          throw new Error(String(data.error || `http_${{response.status}}`));
        }}
        renderIntakeDraft(data.session?.draft || {{}}, data.session || null);
        setIntakeStatus(`上传完成：${{Number(data.uploaded_count || 0)}} 个文件。`);
      }} catch (error) {{
        setIntakeStatus(`上传失败：${{error.message || error}}`, true);
      }}
    }}
    async function analyzeIntake() {{
      if (!intakeSessionId) {{
        setIntakeStatus("请先创建会话。", true);
        return;
      }}
      const synced = await saveIntakeSessionEdits({{ quiet: true, requireGoal: true }});
      if (!synced) {{
        setIntakeStatus("Analyze 前同步会话配置失败，请先保存会话配置。", true);
        return;
      }}
      try {{
        const data = await postJson("./api/intake/analyze", {{
          session_id: intakeSessionId,
          feedback: "",
        }});
        renderIntakeDraft(data.draft || {{}}, data.session || null);
        setIntakeStatus("Analyze 完成，请确认 stage objective 与 test case。");
      }} catch (error) {{
        setIntakeStatus(`Analyze 失败：${{error.message || error}}`, true);
      }}
    }}
    async function regenerateIntake() {{
      if (!intakeSessionId) {{
        setIntakeStatus("请先创建会话。", true);
        return;
      }}
      const synced = await saveIntakeSessionEdits({{ quiet: true, requireGoal: true }});
      if (!synced) {{
        setIntakeStatus("重生前同步会话配置失败，请先保存会话配置。", true);
        return;
      }}
      const feedbackEl = document.getElementById("intake-feedback");
      const feedback = String(feedbackEl?.value || "").trim();
      if (!feedback) {{
        setIntakeStatus("请输入反馈后再重生。", true);
        return;
      }}
      try {{
        const data = await postJson("./api/intake/analyze", {{
          session_id: intakeSessionId,
          feedback: feedback,
        }});
        renderIntakeDraft(data.draft || {{}}, data.session || null);
        setIntakeStatus("已根据反馈重生草案。");
      }} catch (error) {{
        setIntakeStatus(`重生失败：${{error.message || error}}`, true);
      }}
    }}
    async function confirmIntake() {{
      if (!intakeSessionId) {{
        setIntakeStatus("请先创建会话并 Analyze。", true);
        return;
      }}
      const synced = await saveIntakeSessionEdits({{ quiet: true, requireGoal: true }});
      if (!synced) {{
        setIntakeStatus("确认前同步会话配置失败，请先保存会话配置。", true);
        return;
      }}
      try {{
        const data = await postJson("./api/intake/confirm", {{
          session_id: intakeSessionId,
          auto_run: true,
        }});
        renderIntakeDraft(data.session?.draft || {{}}, data.session || null);
        const pid = Number(data.run?.pid || 0);
        const stagesFile = String(data.generated_stages_file || "");
        const runHint = pid > 0 ? `后台进程 PID=${{pid}}` : "后台任务已触发";
        setIntakeStatus(`已确认并启动执行。stages 文件：${{stagesFile}}；${{runHint}}`);
      }} catch (error) {{
        setIntakeStatus(`确认启动失败：${{error.message || error}}`, true);
      }}
    }}

    let compareOpen = false;
    const focusExpandedState = Object.create(null);
    function toggleCompare() {{
      compareOpen = !compareOpen;
      document.getElementById("compare-toggle").classList.toggle("open", compareOpen);
      document.getElementById("compare-body").classList.toggle("open", compareOpen);
    }}

    function _focusItemKey(item) {{
      const type = String(item?.type || "");
      const summary = String(item?.summary || item?.text || item?.message || "");
      const detail = String(item?.detail || "");
      return encodeURIComponent(`${{type}}|${{summary}}|${{detail}}`);
    }}

    function toggleFocusItem(el) {{
      if (!el) return;
      const key = String(el.getAttribute("data-focus-key") || "");
      const expanded = el.classList.toggle("expanded");
      const hint = el.querySelector(".focus-expand-hint");
      if (hint) hint.textContent = expanded ? "▾ 收起" : "▸ 展开";
      if (!key) return;
      if (expanded) {{
        focusExpandedState[key] = true;
      }} else {{
        delete focusExpandedState[key];
      }}
    }}

    function render(view) {{
      // --- P6: Compact header ---
      document.getElementById("header-repo").textContent = view.repo || "";
      const current = view.current || {{}};
      const stageStatus = current.stage_status || "pending";
      document.getElementById("header-stage").innerHTML =
        `<strong>${{esc(current.stage_name||"N/A")}}</strong> <span class="pill ${{statusClass(stageStatus)}}">${{esc(stageStatus)}}</span>`;
      document.getElementById("header-round").textContent =
        `Stage ${{current.stage_index||0}}/${{current.stage_total||0}} · 第 ${{current.round||0}} 轮 / 共 ${{current.max_round||1}} 轮`;

      const pipeline = view.pipeline || {{}};
      const counts = pipeline.counts || {{}};
      const pipelinePct = pipeline.progress || 0;
      document.getElementById("pipeline-pct").textContent = `${{pipelinePct}}%`;
      const pipelineBar = document.getElementById("pipeline-bar").parentElement;
      setBar(pipelineBar, pipelinePct);

      const statsEl = document.getElementById("pipeline-stats");
      const statItems = [
        {{label:"通过",count:counts.passed||0,color:"var(--ok)"}},
        {{label:"运行",count:counts.running||0,color:"var(--run)"}},
        {{label:"阻断",count:counts.blocked||0,color:"var(--warn)"}},
        {{label:"失败",count:counts.failed||0,color:"var(--bad)"}},
      ];
      statsEl.innerHTML = statItems.map(s =>
        `<span class="stat"><span class="stat-dot" style="background:${{s.color}}"></span>${{s.label}}: ${{s.count}}</span>`
      ).join("");

      // --- Cost summary ---
      const cost = view.cost || {{}};
      const costEl = document.getElementById("cost-summary");
      if (cost.invocation_count > 0) {{
        costEl.style.display = "block";
        const warnFlag = cost.budget_warn_triggered ? ' <span style="color:var(--warn)">⚠ 预算告警</span>' : "";
        const hardFlag = cost.budget_hard_triggered ? ' <span style="color:var(--bad)">🛑 预算阻断</span>' : "";
        const costUsd = fixedNumber(cost.estimated_cost_usd, 4);
        const totalTok = safeNumber(cost.total_tokens, 0).toLocaleString();
        const invocations = safeNumber(cost.invocation_count, 0);
        let stageBreakdown = "";
        if (Array.isArray(cost.by_stage) && cost.by_stage.length > 0) {{
          stageBreakdown = " · " + cost.by_stage.map(s =>
            `${{esc(s.name)}}: ${{fixedNumber(s?.cost_usd, 4)}}`
          ).join(", ");
        }}
        const savedCharsValue = safeNumber(cost.compression_saved_chars, 0);
        const savedChars = savedCharsValue.toLocaleString();
        const compressionInfo = savedCharsValue > 0
          ? ` · 📦 压缩节省 ${{savedChars}} 字符`
          : "";
        costEl.innerHTML = `💰 成本: <strong>${{costUsd}}</strong> · ${{totalTok}} tokens · ${{invocations}} 次调用${{stageBreakdown}}${{compressionInfo}}${{warnFlag}}${{hardFlag}}`;
      }} else {{
        costEl.style.display = "none";
      }}

      // --- SLI summary ---
      const sli = view.sli || {{}};
      const sliMetrics = sli.metrics || {{}};
      const sliEl = document.getElementById("sli-summary");
      const retryRate = safeNumber(sliMetrics.retry_rate_per_stage, 0);
      const burnRate = safeNumber(sliMetrics.cost_burn_rate_usd_per_min, 0);
      const compressionRate = safeNumber(sliMetrics.compression_rate, 0);
      const elapsedSec = safeNumber(sliMetrics.run_elapsed_sec, 0);
      const avgRounds = safeNumber(sliMetrics.avg_stage_rounds, 0);
      const sliAlerts = Array.isArray(sli.alerts) ? sli.alerts : [];
      if (elapsedSec > 0 || avgRounds > 0 || retryRate > 0 || burnRate > 0 || compressionRate > 0 || sliAlerts.length > 0) {{
        sliEl.style.display = "block";
        const alertPart = sliAlerts.length > 0
          ? ` · <span style="color:var(--warn)">告警: ${{sliAlerts.map(esc).join(", ")}}</span>`
          : "";
        sliEl.innerHTML =
          `📈 SLI: 运行 ${{formatDuration(elapsedSec)}} · 平均轮次 ${{fixedNumber(avgRounds, 2)}} · 重试率 ${{fixedNumber(retryRate*100, 1)}}%/stage · 成本燃速 ${{fixedNumber(burnRate, 4)}} USD/min · 压缩率 ${{fixedNumber(compressionRate*100, 1)}}%${{alertPart}}`;
      }} else {{
        sliEl.style.display = "none";
      }}

      // --- P2: Pipeline nodes ---
      const nodes = view.pipeline_nodes || [];
      const nodesEl = document.getElementById("pipeline-nodes");
      nodesEl.innerHTML = nodes.map((n, i) => {{
        const dotClass = n.state === "done" ? "done" : n.state === "active" ? "active" : n.state === "failed" ? "failed" : "";
        const arrowClass = n.state === "done" ? "done" : "";
        const arrow = i < nodes.length - 1 ? `<span class="pn-arrow ${{arrowClass}}">→</span>` : "";
        return `<div class="pn"><span class="pn-icon">${{n.icon||""}}</span><span class="pn-dot ${{dotClass}}"></span><span class="pn-label">${{esc(n.label)}}</span></div>${{arrow}}`;
      }}).join("");
      document.getElementById("phase-desc").textContent =
        `${{current.phase_label||""}} — ${{current.phase_goal||""}}`;
      const currentGates = Array.isArray(current.passed_gates) ? current.passed_gates : [];
      document.getElementById("current-gates").textContent = currentGates.length
        ? `已通过 Gate：${{currentGates.join(" → ")}}`
        : "已通过 Gate：暂无记录";

      // --- P4: Agent cards with semantic bars ---
      const agents = view.agents || {{}};
      const agentRow = document.getElementById("agent-row");
      const agentConfigs = [
        {{key:"worker", name:"Worker（实现者）", data: agents.worker}},
        {{key:"judge", name:"Judge / Verifier（裁判 / 验证者）", data: agents.judge}},
      ];
      agentRow.innerHTML = agentConfigs.map(ac => {{
        const d = ac.data || {{}};
        const pct = d.progress || 0;
        const state = d.state || "idle";
        const bc = barColor(state);
        return `<div class="agent-card">
          <div class="agent-name">${{esc(ac.name)}}</div>
          <div class="agent-state">${{esc(agentLabel(d))}}</div>
          <div class="bar-label"><span class="bar-pct">${{pct}}%</span><div class="bar ${{bc}}"><span style="width:${{pct}}%"></span></div></div>
        </div>`;
      }}).join("");

      const activeRemoteChecks = Array.isArray(view.active_remote_checks) ? view.active_remote_checks : [];
      const remoteChecksSection = document.getElementById("remote-checks-section");
      const remoteCheckList = document.getElementById("remote-check-list");
      if (activeRemoteChecks.length === 0) {{
        remoteChecksSection.style.display = "none";
      }} else {{
        remoteChecksSection.style.display = "block";
        remoteCheckList.innerHTML = activeRemoteChecks.map((item) => {{
          const pct = Number(item.progress || 0);
          const worker = String(item.worker || "worker");
          const tier = String(item.gate_tier || "fast_round");
          const host = String(item.remote_host || "remote");
          const command = String(item.command || "");
          const elapsed = formatDuration(item.elapsed_sec || 0);
          const timeout = formatDuration(item.timeout_sec || 0);
          const index = Number(item.command_index || 0);
          const total = Number(item.command_total || 0);
          const seq = index > 0 && total > 0 ? `${{index}}/${{total}}` : "1/1";
          return `<div class="remote-check-item">
            <div class="remote-check-head">
              <span class="remote-check-title">${{esc(worker)}} · ${{esc(tier)}} · ${{esc(host)}}</span>
              <span class="pill status-running">运行中</span>
            </div>
            <div class="remote-check-meta">命令 ${{seq}}：${{esc(command)}}</div>
            <div class="remote-check-meta">已运行 ${{elapsed}} / 超时上限 ${{timeout}}</div>
            <div class="bar-label"><span class="bar-pct">${{pct}}%</span><div class="bar bar-color-implementing"><span style="width:${{pct}}%"></span></div></div>
          </div>`;
        }}).join("");
      }}

      const timeoutRecovery = view.timeout_recovery || {{}};
      const recoverySection = document.getElementById("timeout-recovery-section");
      const recoveryStats = document.getElementById("timeout-recovery-stats");
      const recoveryThresholds = document.getElementById("timeout-recovery-thresholds");
      const recoveryAlerts = document.getElementById("timeout-recovery-alerts");
      const recoveryRecent = document.getElementById("timeout-recovery-recent");
      const attempted = Number(timeoutRecovery.attempted || 0);
      const recovered = Number(timeoutRecovery.recovered || 0);
      const failed = Number(timeoutRecovery.failed || 0);
      const stale = Number(timeoutRecovery.stale_recycled || 0);
      const failureRatePct = Number(timeoutRecovery.failure_rate_pct || 0);
      const thresholds = timeoutRecovery.thresholds || {{}};
      const isAlerting = Boolean(timeoutRecovery.is_alerting);
      const alerts = Array.isArray(timeoutRecovery.alerts) ? timeoutRecovery.alerts : [];
      const recentRecovery = Array.isArray(timeoutRecovery.recent_events) ? timeoutRecovery.recent_events : [];
      if (attempted === 0 && stale === 0 && recentRecovery.length === 0) {{
        recoverySection.style.display = "none";
        recoverySection.classList.remove("timeout-recovery-alerting");
      }} else {{
        recoverySection.style.display = "block";
        recoverySection.classList.toggle("timeout-recovery-alerting", isAlerting);
        recoveryStats.innerHTML = [
          {{label: "恢复尝试", value: attempted}},
          {{label: "恢复成功", value: recovered}},
          {{label: "恢复失败", value: failed}},
          {{label: "陈旧回收", value: stale}},
          {{label: "失败率", value: `${{failureRatePct}}%`}},
        ].map((item) =>
          `<div class="recovery-stat"><div class="recovery-stat-label">${{item.label}}</div><div class="recovery-stat-value">${{item.value}}</div></div>`
        ).join("");
        recoveryThresholds.textContent =
          `告警阈值：尝试≥${{Number(thresholds.min_attempts_for_rate || 0)}} 且失败率≥${{Number(thresholds.failure_rate_pct || 0)}}%，连续失败≥${{Number(thresholds.consecutive_failures || 0)}}，陈旧回收≥${{Number(thresholds.stale_recycled || 0)}}`;
        recoveryAlerts.innerHTML = alerts.map((item) => {{
          const level = String(item.level || "warning").toLowerCase();
          const message = String(item.message || "");
          return `<div class="recovery-alert-item level-${{esc(level)}}">${{esc(message)}}</div>`;
        }}).join("") || '<div class="remote-check-empty">当前无恢复告警</div>';
        recoveryRecent.innerHTML = recentRecovery.map((item) => {{
          const event = String(item.event || "");
          const label = event === "timeout_recovery" ? "超时恢复" : (event === "stale_timeout" ? "陈旧回收" : event || "事件");
          const summary = String(item.detail || "");
          return `<div class="recovery-recent-item">
            <div><strong>${{esc(label)}}</strong> · ${{esc(item.worker||"worker")}} · ${{esc(item.gate_tier||"-")}}</div>
            <div class="recovery-recent-meta">${{esc(item.stage_name||"-")}} · ${{esc(summary || "无附加信息")}}</div>
          </div>`;
        }}).join("") || '<div class="remote-check-empty">暂无恢复事件</div>';
      }}

      // --- P5: Worker compare ---
      const wd = view.worker_details || {{}};
      const compareGrid = document.getElementById("compare-grid");
      const workerConfigs = [
        {{key:"worker", name:"Worker（实现者）"}},
      ];
      compareGrid.innerHTML = workerConfigs.map(wc => {{
        const w = wd[wc.key] || {{}};
        return `<div class="compare-card">
          <h4>${{esc(wc.name)}}</h4>
          <div class="compare-row"><span>状态</span><span>${{esc(agentLabel(w))}}</span></div>
          <div class="compare-row"><span>检查结果</span><span>${{esc(w.check_status||"—")}}</span></div>
          <div class="compare-row"><span>方案状态</span><span>${{esc(w.plan_status||"—")}}</span></div>
          <div class="compare-row"><span>进度</span><span>${{w.progress||0}}%</span></div>
        </div>`;
      }}).join("");

      // --- Stage roadmap ---
      const roadmap = view.stage_roadmap || [];
      const roadmapSection = document.getElementById("roadmap-section");
      const roadmapContainer = document.getElementById("roadmap-container");
      if (roadmap.length === 0) {{
        roadmapSection.style.display = "none";
      }} else {{
        roadmapSection.style.display = "block";
        roadmapContainer.innerHTML = roadmap.map((r) => {{
          const dotClass = r.is_current ? "rm-current" : r.status === "passed" ? "rm-done" : r.status === "failed" ? "rm-failed" : "";
          const nameClass = r.is_current ? "rm-current" : r.status === "passed" ? "rm-done" : "";
          const currentBadge = r.is_current ? `<span class="pill status-running">当前</span>` : "";
          const roundMeta = r.round ? ` · 第 ${{r.round}} / ${{r.max_round||1}} 轮` : "";
          return `<div class="roadmap-node ${{r.is_current ? "is-current" : ""}}">
            <div class="roadmap-order">${{r.index||"•"}}</div>
            <div class="roadmap-main">
              <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <span class="roadmap-dot ${{dotClass}}"></span>
                <span class="roadmap-name ${{nameClass}}">${{esc(r.name)}}</span>
                ${{currentBadge}}
              </div>
              <div class="roadmap-obj" title="${{esc(r.objective||"")}}">${{esc(r.objective||"")}}</div>
            </div>
            <div class="roadmap-meta">
              <span class="pill ${{statusClass(r.status)}}">${{esc(r.status_label||r.status||"pending")}}</span>
              <span class="small">${{roundMeta}}</span>
            </div>
          </div>`;
        }}).join("");
      }}

      // --- P3: Focus items (summary + expandable) ---
      const focusItems = view.focus_items || [];
      const focusSection = document.getElementById("focus-section");
      const focusList = document.getElementById("focus-list");
      if (focusItems.length === 0) {{
        focusSection.style.display = "none";
      }} else {{
        focusSection.style.display = "block";
        const nextFocusKeys = Object.create(null);
        focusList.innerHTML = focusItems.map((it, idx) => {{
          const f = normalizeUiItem(it);
          const sevClass = f.severity === "error" || f.severity === "critical" ? "sev-error" : f.severity === "warning" ? "sev-warning" : "sev-info";
          const hasDetail = f.detail ? "has-detail" : "";
          const focusKey = _focusItemKey(f);
          nextFocusKeys[focusKey] = true;
          const expanded = Boolean(f.detail && focusExpandedState[focusKey]);
          const expandHint = f.detail
            ? `<span class="focus-expand-hint">${{expanded ? "▾ 收起" : "▸ 展开"}}</span>`
            : "";
          const detailBlock = f.detail ? `<div class="focus-detail">${{esc(f.detail)}}</div>` : "";
          return `<div class="focus-item ${{sevClass}} ${{hasDetail}} ${{expanded ? "expanded" : ""}}" data-focus-key="${{focusKey}}" onclick="toggleFocusItem(this)"><span class="focus-icon">${{f.icon||""}}</span><div style="flex:1"><span class="focus-summary">${{esc(f.summary||f.text||"")}}</span>${{expandHint}}${{detailBlock}}</div></div>`;
        }}).join("");
        Object.keys(focusExpandedState).forEach((key) => {{
          if (!nextFocusKeys[key]) delete focusExpandedState[key];
        }});
      }}

      // --- Stage detail list ---
      const stages = view.stages || [];
      const stageListEl = document.getElementById("stage-list");
      if (stages.length === 0) {{
        stageListEl.innerHTML = '<div class="small">暂无阶段数据</div>';
      }} else {{
        stageListEl.innerHTML = stages.map(s => {{
          const deps = (s.depends_on || []).length ? (s.depends_on || []).map(d => esc(d)).join("，") : "";
          const currentBadge = s.is_current ? `<span class="pill status-running" style="font-size:10px;">当前</span>` : "";
          const depsLine = deps ? `<span class="stage-dep-tag">↳ ${{deps}}</span>` : "";
          return `<div class="stage-list-item ${{s.is_current ? "is-current" : ""}}">
            <div class="stage-list-left">
              <span class="stage-list-dot st-dot-${{s.status}}"></span>
            </div>
            <div class="stage-list-body">
              <div class="stage-list-head">
                <span class="stage-list-name">${{esc(s.name)}}</span>
                ${{currentBadge}}
                <span class="pill ${{statusClass(s.status)}}" style="font-size:10px;">${{esc(s.status_label||s.status)}}</span>
              </div>
              <div class="stage-list-info">
                <span>${{esc(s.phase_label||"-")}}</span>
                <span>第 ${{s.round||0}} / ${{s.max_round||1}} 轮</span>
                ${{depsLine}}
              </div>
              <div class="stage-list-gates">${{(Array.isArray(s.passed_gates) && s.passed_gates.length) ? esc("已通过 Gate：" + s.passed_gates.join(" → ")) : "已通过 Gate：暂无记录"}}</div>
              <div class="stage-list-obj">${{esc(s.highlight || s.objective || "—")}}</div>
            </div>
          </div>`;
        }}).join("");
      }}

      // --- P1: Activity feed (summary + expandable) ---
      const feed = view.activity_feed || [];
      const feedSection = document.getElementById("feed-section");
      const feedList = document.getElementById("feed-list");
      if (feed.length === 0) {{
        feedSection.style.display = "none";
      }} else {{
        feedSection.style.display = "block";
        feedList.innerHTML = feed.map((it) => {{
          const f = normalizeUiItem(it);
          const hasDetail = f.detail ? "has-detail" : "";
          const detailBlock = f.detail ? `<div class="feed-detail">${{esc(f.detail)}}</div>` : "";
          return `<div class="feed-item ${{hasDetail}}" onclick="this.classList.toggle('expanded')"><span class="feed-icon">${{f.icon||""}}</span><span class="feed-stage">${{esc(f.stage||"")}}</span><div style="flex:1"><span class="feed-summary">${{esc(f.summary||f.message||"")}}</span>${{detailBlock}}</div></div>`;
        }}).join("");
      }}
    }}

    function markLive(ok, note) {{
      const dot = document.getElementById("live-dot");
      const label = document.getElementById("live-label");
      if (dot) dot.classList.toggle("live", !!ok);
      if (label) label.textContent = note;
    }}

    ensureTargetRepoInputs();
    render(initialView);

    const isFileProtocol = window.location.protocol === "file:";
    if (sseUrl) {{
      markLive(false, "正在连接实时流...");
      const source = new EventSource(sseUrl);
      source.onopen = () => markLive(true, "● 实时连接已建立");
      source.onmessage = (event) => {{
        try {{
          const view = JSON.parse(event.data || "{{}}");
          render(view);
          markLive(true, "● 实时更新中");
        }} catch (error) {{
          markLive(false, "实时消息解析失败");
        }}
      }};
      source.onerror = () => {{
        markLive(false, "连接中断，等待自动重连...");
      }};
    }} else if (isFileProtocol) {{
      markLive(false, "📄 静态快照（如需最新数据请重新生成或启动 monitor server）");
    }} else {{
      markLive(true, "● 实时更新中");
      setInterval(() => {{
        fetch("./api/view")
          .then((resp) => resp.ok ? resp.json() : null)
          .then((view) => {{
            if (view) render(view);
          }})
          .catch(() => null);
      }}, Math.max(1000, refreshSec * 1000));
    }}
  </script>
</body>
</html>
"""


def write_monitor_html(runtime_dir: Path, output_html: Path | None = None) -> Path:
    runtime_dir = runtime_dir.expanduser().resolve()
    payload = build_monitor_payload(runtime_dir)
    output_path = output_html.expanduser().resolve() if output_html else runtime_dir / "monitor.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_monitor_html(payload), encoding="utf-8")
    return output_path


def _build_monitor_http_handler(runtime_dir: Path, refresh_sec: float) -> type[BaseHTTPRequestHandler]:
    class _MonitorHttpHandler(BaseHTTPRequestHandler):
        def _split_path(self) -> tuple[str, dict[str, list[str]]]:
            parsed = urlparse(self.path)
            return parsed.path, parse_qs(parsed.query)

        def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "0").strip() or "0"
            length = max(0, int(raw_length))
            if length <= 0:
                return {}
            payload = self.rfile.read(length)
            if not payload:
                return {}
            parsed = json.loads(payload.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON body must be an object")
            return parsed

        def do_GET(self) -> None:
            route, query = self._split_path()
            if route in ("/", "/index.html"):
                payload = build_monitor_payload(runtime_dir)
                body = render_monitor_html(
                    payload,
                    auto_refresh_sec=refresh_sec,
                    sse_url="/events",
                ).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if route == "/api/payload":
                payload = build_monitor_payload(runtime_dir)
                body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if route == "/api/view":
                view = build_monitor_view(build_monitor_payload(runtime_dir))
                body = json.dumps(view, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if route == "/api/intake/session":
                session_id = (query.get("session_id") or [""])[0].strip()
                if not session_id:
                    self._send_json(
                        {"ok": False, "error": "session_id is required"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                try:
                    session = load_session(runtime_dir, session_id)
                except FileNotFoundError:
                    self._send_json(
                        {"ok": False, "error": f"session not found: {session_id}"},
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                self._send_json({"ok": True, "session": session})
                return
            if route == "/api/intake/sessions":
                limit = 20
                if query.get("limit"):
                    try:
                        limit = max(1, min(200, int((query.get("limit") or ["20"])[0])))
                    except ValueError:
                        limit = 20
                sessions = list_sessions(runtime_dir, limit=limit)
                self._send_json({"ok": True, "sessions": sessions})
                return
            if route == "/events":
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                interval = max(0.2, float(refresh_sec))
                sse_event_offset = 0
                try:
                    while True:
                        payload = build_monitor_payload(
                            runtime_dir,
                            event_stream_since_offset=sse_event_offset,
                        )
                        sse_event_offset = int(
                            payload.get("event_stream_summary", {}).get("byte_offset", 0)
                        )
                        view = build_monitor_view(payload)
                        data = json.dumps(view, ensure_ascii=False)
                        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                        self.wfile.flush()
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        import time
                        time.sleep(interval)
                except (BrokenPipeError, ConnectionResetError):
                    return
            if route == "/healthz":
                body = b"ok\n"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            route, query = self._split_path()
            try:
                if route == "/api/intake/session":
                    payload = self._read_json_body()
                    goal = str(payload.get("goal", "")).strip()
                    target_repo = str(payload.get("target_repo", "")).strip()
                    target_repos_payload: list[str] | None = None
                    if payload.get("target_repos") is not None:
                        if not isinstance(payload.get("target_repos"), list):
                            self._send_json(
                                {"ok": False, "error": "target_repos must be an array"},
                                status=HTTPStatus.BAD_REQUEST,
                            )
                            return
                        target_repos_payload = [
                            str(item)
                            for item in payload.get("target_repos")
                        ]
                    if not goal:
                        self._send_json(
                            {"ok": False, "error": "goal is required"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    session = create_session(
                        runtime_dir,
                        goal=goal,
                        target_repo=target_repo,
                        target_repos=target_repos_payload,
                        model=str(payload.get("model", "gpt-5.3-codex")),
                        sandbox_mode=str(payload.get("sandbox_mode", "workspace-write")),
                        max_round_per_stage=int(payload.get("max_round_per_stage", 2) or 2),
                        remote_validation=(
                            payload.get("remote_validation")
                            if isinstance(payload.get("remote_validation"), dict)
                            else None
                        ),
                    )
                    self._send_json({"ok": True, "session": session}, status=HTTPStatus.CREATED)
                    return

                if route == "/api/intake/session/update":
                    payload = self._read_json_body()
                    session_id = str(payload.get("session_id", "")).strip()
                    if not session_id:
                        self._send_json(
                            {"ok": False, "error": "session_id is required"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    remote_validation_payload: dict[str, Any] | None = None
                    if payload.get("remote_validation") is not None:
                        if not isinstance(payload.get("remote_validation"), dict):
                            self._send_json(
                                {"ok": False, "error": "remote_validation must be an object"},
                                status=HTTPStatus.BAD_REQUEST,
                            )
                            return
                        remote_validation_payload = payload.get("remote_validation")
                    target_repos_payload: list[str] | None = None
                    if payload.get("target_repos") is not None:
                        if not isinstance(payload.get("target_repos"), list):
                            self._send_json(
                                {"ok": False, "error": "target_repos must be an array"},
                                status=HTTPStatus.BAD_REQUEST,
                            )
                            return
                        target_repos_payload = [
                            str(item)
                            for item in payload.get("target_repos")
                        ]
                    session = update_session(
                        runtime_dir,
                        session_id=session_id,
                        goal=(
                            str(payload.get("goal"))
                            if payload.get("goal") is not None
                            else None
                        ),
                        target_repo=(
                            str(payload.get("target_repo"))
                            if payload.get("target_repo") is not None
                            else None
                        ),
                        target_repos=target_repos_payload,
                        model=(
                            str(payload.get("model"))
                            if payload.get("model") is not None
                            else None
                        ),
                        sandbox_mode=(
                            str(payload.get("sandbox_mode"))
                            if payload.get("sandbox_mode") is not None
                            else None
                        ),
                        max_round_per_stage=(
                            int(payload.get("max_round_per_stage"))
                            if payload.get("max_round_per_stage") is not None
                            else None
                        ),
                        remote_validation=remote_validation_payload,
                    )
                    self._send_json({"ok": True, "session": session})
                    return

                if route == "/api/intake/upload":
                    content_type = self.headers.get("Content-Type", "")
                    if "multipart/form-data" not in content_type:
                        self._send_json(
                            {"ok": False, "error": "multipart/form-data is required"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    content_length = max(0, int(self.headers.get("Content-Length", "0") or 0))
                    raw_payload = self.rfile.read(content_length) if content_length > 0 else b""
                    mime_bytes = (
                        f"Content-Type: {content_type}\r\n"
                        "MIME-Version: 1.0\r\n\r\n"
                    ).encode("utf-8") + raw_payload
                    multipart = BytesParser(policy=email_policy_default).parsebytes(mime_bytes)

                    session_id = ""
                    file_parts: list[tuple[str, bytes]] = []
                    for part in multipart.iter_parts():
                        if part.get_content_disposition() != "form-data":
                            continue
                        field_name = str(part.get_param("name", header="content-disposition") or "")
                        filename = part.get_filename()
                        data = part.get_payload(decode=True) or b""
                        if filename:
                            file_parts.append((str(filename), data))
                            continue
                        if field_name == "session_id":
                            session_id = data.decode("utf-8", errors="ignore").strip()

                    if not session_id:
                        session_id = (query.get("session_id") or [""])[0].strip()
                    if not session_id:
                        self._send_json(
                            {"ok": False, "error": "session_id is required"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return

                    uploaded: list[str] = []
                    for filename, data in file_parts:
                        filename = filename.strip()
                        if not filename:
                            continue
                        if not data:
                            continue
                        rel_path = register_uploaded_file(
                            runtime_dir,
                            session_id=session_id,
                            original_name=filename,
                            payload=data,
                        )
                        uploaded.append(rel_path)

                    session = load_session(runtime_dir, session_id)
                    self._send_json(
                        {
                            "ok": True,
                            "session_id": session_id,
                            "uploaded_count": len(uploaded),
                            "uploaded_files": uploaded,
                            "session": session,
                        }
                    )
                    return

                if route == "/api/intake/analyze":
                    payload = self._read_json_body()
                    session_id = str(payload.get("session_id", "")).strip()
                    if not session_id:
                        self._send_json(
                            {"ok": False, "error": "session_id is required"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    draft = generate_stage_draft(
                        runtime_dir,
                        session_id=session_id,
                        feedback=str(payload.get("feedback", "")),
                    )
                    session = load_session(runtime_dir, session_id)
                    self._send_json({"ok": True, "draft": draft, "session": session})
                    return

                if route == "/api/intake/confirm":
                    payload = self._read_json_body()
                    session_id = str(payload.get("session_id", "")).strip()
                    if not session_id:
                        self._send_json(
                            {"ok": False, "error": "session_id is required"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    stages_file = confirm_draft_to_stages_file(runtime_dir, session_id=session_id)
                    auto_run = bool(payload.get("auto_run", True))
                    run_info: dict[str, Any] | None = None
                    if auto_run:
                        run_info = launch_confirmed_run(runtime_dir, session_id=session_id)
                    session = load_session(runtime_dir, session_id)
                    self._send_json(
                        {
                            "ok": True,
                            "generated_stages_file": str(stages_file),
                            "run": run_info or {},
                            "session": session,
                        }
                    )
                    return

                self.send_error(HTTPStatus.NOT_FOUND)
            except FileNotFoundError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    status=HTTPStatus.NOT_FOUND,
                )
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            except Exception as exc:  # pragma: no cover - fail closed in runtime path
                self._send_json(
                    {"ok": False, "error": f"internal_error: {exc}"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return _MonitorHttpHandler


def serve_monitor(
    *,
    runtime_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    refresh_sec: float = 2.0,
) -> None:
    runtime_dir = runtime_dir.expanduser().resolve()
    handler = _build_monitor_http_handler(runtime_dir, max(0.2, float(refresh_sec)))
    server = ThreadingHTTPServer((host, int(port)), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
