from __future__ import annotations

import json
import os
import base64
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from agents.codex_exec_agent import CodexExecAgent, CodexExecAgentConfig


SESSION_SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _intake_root(runtime_dir: Path) -> Path:
    return runtime_dir / "intake"


def _sessions_dir(runtime_dir: Path) -> Path:
    return _intake_root(runtime_dir) / "sessions"


def _uploads_dir(runtime_dir: Path, session_id: str) -> Path:
    return _intake_root(runtime_dir) / "uploads" / session_id


def _generated_dir(runtime_dir: Path) -> Path:
    return _intake_root(runtime_dir) / "generated"


def _run_logs_dir(runtime_dir: Path) -> Path:
    return _intake_root(runtime_dir) / "runs"


def _safe_session_id(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", (raw or "").strip())
    if not cleaned:
        raise ValueError("session_id is required")
    return cleaned


def _session_path(runtime_dir: Path, session_id: str) -> Path:
    return _sessions_dir(runtime_dir) / f"{_safe_session_id(session_id)}.json"


def ensure_intake_dirs(runtime_dir: Path) -> None:
    for path in (_sessions_dir(runtime_dir), _generated_dir(runtime_dir), _run_logs_dir(runtime_dir)):
        path.mkdir(parents=True, exist_ok=True)


def _normalize_upload_rel_path(raw: str, fallback_name: str = "upload.bin") -> str:
    candidate = (raw or "").replace("\\", "/").strip().lstrip("/")
    if not candidate:
        candidate = fallback_name
    parts: list[str] = []
    for segment in candidate.split("/"):
        seg = segment.strip()
        if not seg or seg in {".", ".."}:
            continue
        seg = re.sub(r"[^A-Za-z0-9._-]", "_", seg)
        if seg:
            parts.append(seg)
    if not parts:
        parts = [fallback_name]
    return "/".join(parts)


class IntakeStageDraft(BaseModel):
    stage_id: str
    objective: str
    scope_hint: list[str] = Field(default_factory=list)
    test_cases: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class IntakeDraftResponse(BaseModel):
    stages: list[IntakeStageDraft] = Field(default_factory=list)
    rationale: str = ""
    assumptions: list[str] = Field(default_factory=list)


def _normalize_target_repos(
    payload: object,
    *,
    fallback_target_repo: str = "",
) -> list[str]:
    repos: list[str] = []
    seen: set[str] = set()
    if isinstance(payload, list):
        for item in payload:
            candidate = str(item).strip()
            if not candidate or candidate in seen:
                continue
            repos.append(candidate)
            seen.add(candidate)
    if repos:
        return repos[:8]
    fallback = str(fallback_target_repo).strip()
    return [fallback] if fallback else []


def _primary_target_repo_from_session(session: dict[str, Any]) -> str:
    repos = _normalize_target_repos(
        session.get("target_repos"),
        fallback_target_repo=str(session.get("target_repo", "")),
    )
    return repos[0] if repos else ""


def _normalize_remote_validation(payload: object) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    enabled = bool(data.get("enabled", False))
    servers_raw = data.get("servers") if isinstance(data.get("servers"), list) else []

    def _parse_server(raw: object, *, default_label: str) -> tuple[dict[str, str], bool]:
        if not isinstance(raw, dict):
            return {}, False
        host = str(raw.get("host", "")).strip()
        user_raw = str(raw.get("user", "")).strip()
        user = user_raw or "root"
        password = str(raw.get("password", "")).strip()
        workdir = str(raw.get("workdir", "")).strip()
        label_raw = str(raw.get("label", "")).strip()
        label = label_raw or default_label
        has_any_input = any([host, user_raw, password, workdir, label_raw])
        payload_item = {
            "label": label,
            "host": host,
            "user": user,
            "password": password,
            "workdir": workdir,
        }
        return payload_item, has_any_input

    primary_raw = servers_raw[0] if len(servers_raw) > 0 else {}
    secondary_raw = servers_raw[1] if len(servers_raw) > 1 else {}

    primary_server, primary_has_any = _parse_server(primary_raw, default_label="server_1")
    secondary_server, secondary_has_any = _parse_server(secondary_raw, default_label="server_2")

    if enabled and (
        not primary_server
        or not primary_server.get("host")
        or not primary_server.get("workdir")
    ):
        raise ValueError(
            "remote_validation enabled but server_1 is incomplete: host and workdir are required"
        )

    if enabled and secondary_has_any and (
        not secondary_server.get("host") or not secondary_server.get("workdir")
    ):
        raise ValueError(
            "remote_validation server_2 is incomplete: host and workdir are required when provided"
        )

    servers: list[dict[str, str]] = []
    if primary_server.get("host") and primary_server.get("workdir"):
        servers.append(primary_server)
    elif primary_has_any and enabled:
        raise ValueError(
            "remote_validation enabled but server_1 is incomplete: host and workdir are required"
        )

    if secondary_server.get("host") and secondary_server.get("workdir"):
        servers.append(secondary_server)
    elif secondary_has_any and enabled:
        raise ValueError(
            "remote_validation server_2 is incomplete: host and workdir are required when provided"
        )

    return {"enabled": enabled, "servers": servers[:2]}


def create_session(
    runtime_dir: Path,
    *,
    goal: str,
    target_repo: str,
    target_repos: list[str] | None = None,
    model: str = "gpt-5.3-codex",
    sandbox_mode: str = "workspace-write",
    max_round_per_stage: int = 2,
    remote_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_intake_dirs(runtime_dir)
    session_id = f"s-{uuid4().hex[:10]}"
    normalized_target_repos = _normalize_target_repos(
        target_repos,
        fallback_target_repo=target_repo,
    )
    payload: dict[str, Any] = {
        "schema_version": SESSION_SCHEMA_VERSION,
        "session_id": session_id,
        "status": "drafting",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "goal": goal.strip(),
        "target_repo": normalized_target_repos[0] if normalized_target_repos else "",
        "target_repos": normalized_target_repos,
        "model": model.strip() or "gpt-5.3-codex",
        "sandbox_mode": sandbox_mode.strip() or "workspace-write",
        "max_round_per_stage": max(1, int(max_round_per_stage)),
        "remote_validation": _normalize_remote_validation(remote_validation),
        "uploaded_files": [],
        "feedback_history": [],
        "draft": {},
        "generated_stages_file": "",
        "run": {
            "status": "idle",
            "pid": 0,
            "command": [],
            "log_file": "",
            "started_at": "",
        },
    }
    save_session(runtime_dir, payload)
    return payload


def update_session(
    runtime_dir: Path,
    *,
    session_id: str,
    goal: str | None = None,
    target_repo: str | None = None,
    target_repos: list[str] | None = None,
    model: str | None = None,
    sandbox_mode: str | None = None,
    max_round_per_stage: int | None = None,
    remote_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session = load_session(runtime_dir, session_id)

    if goal is not None:
        goal_clean = goal.strip()
        if goal_clean:
            session["goal"] = goal_clean

    normalized_target_repos = _normalize_target_repos(
        session.get("target_repos"),
        fallback_target_repo=str(session.get("target_repo", "")),
    )
    if target_repos is not None:
        normalized_target_repos = _normalize_target_repos(target_repos)
    if target_repo is not None:
        target_repo_clean = target_repo.strip()
        if target_repo_clean:
            normalized_target_repos = [
                target_repo_clean,
                *[repo for repo in normalized_target_repos if repo != target_repo_clean],
            ][:8]
    session["target_repos"] = normalized_target_repos
    session["target_repo"] = normalized_target_repos[0] if normalized_target_repos else ""

    if model is not None:
        model_clean = model.strip()
        if model_clean:
            session["model"] = model_clean

    if sandbox_mode is not None:
        sandbox_mode_clean = sandbox_mode.strip()
        if sandbox_mode_clean:
            session["sandbox_mode"] = sandbox_mode_clean

    if max_round_per_stage is not None:
        session["max_round_per_stage"] = max(1, int(max_round_per_stage))

    if remote_validation is not None:
        session["remote_validation"] = _normalize_remote_validation(remote_validation)

    save_session(runtime_dir, session)
    return session


def load_session(runtime_dir: Path, session_id: str) -> dict[str, Any]:
    payload = json.loads(_session_path(runtime_dir, session_id).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid session payload")
    _refresh_run_status(payload)
    return payload


def save_session(runtime_dir: Path, session: dict[str, Any]) -> None:
    ensure_intake_dirs(runtime_dir)
    session = dict(session)
    normalized_target_repos = _normalize_target_repos(
        session.get("target_repos"),
        fallback_target_repo=str(session.get("target_repo", "")),
    )
    session["target_repos"] = normalized_target_repos
    session["target_repo"] = normalized_target_repos[0] if normalized_target_repos else ""
    session["schema_version"] = SESSION_SCHEMA_VERSION
    session["updated_at"] = _now_iso()
    path = _session_path(runtime_dir, str(session.get("session_id", "")))
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def list_sessions(runtime_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    ensure_intake_dirs(runtime_dir)
    sessions: list[dict[str, Any]] = []
    for path in sorted(_sessions_dir(runtime_dir).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        _refresh_run_status(payload)
        sessions.append(payload)
        if len(sessions) >= max(1, int(limit)):
            break
    return sessions


def register_uploaded_file(
    runtime_dir: Path,
    *,
    session_id: str,
    original_name: str,
    payload: bytes,
) -> str:
    rel_path = _normalize_upload_rel_path(original_name, fallback_name="upload.bin")
    save_path = _uploads_dir(runtime_dir, session_id) / rel_path
    save_path.parent.mkdir(parents=True, exist_ok=True)
    save_path.write_bytes(payload)

    session = load_session(runtime_dir, session_id)
    uploaded_files = [str(item) for item in session.get("uploaded_files", [])]
    if rel_path not in uploaded_files:
        uploaded_files.append(rel_path)
    session["uploaded_files"] = sorted(uploaded_files)
    save_session(runtime_dir, session)
    return rel_path


def _infer_scope_hints(uploaded_files: list[str]) -> list[str]:
    hints: list[str] = []
    for rel in uploaded_files:
        parts = [segment for segment in rel.split("/") if segment]
        if not parts:
            continue
        if len(parts) > 1:
            candidate = parts[0]
        else:
            candidate = "."
        if candidate not in hints:
            hints.append(candidate)
    return hints[:8]


def _goal_keywords(goal: str) -> set[str]:
    lowered = goal.lower()
    keywords = {
        "api": "api" in lowered,
        "auth": "auth" in lowered or "login" in lowered or "token" in lowered,
        "ui": "ui" in lowered or "frontend" in lowered or "page" in lowered,
        "perf": "performance" in lowered or "perf" in lowered,
    }
    return {key for key, enabled in keywords.items() if enabled}


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _looks_binary_bytes(payload: bytes) -> bool:
    if not payload:
        return False
    if b"\x00" in payload:
        return True
    sample = payload[:1024]
    non_text = 0
    for byte in sample:
        if byte in (9, 10, 13) or 32 <= byte <= 126:
            continue
        non_text += 1
    return (non_text / max(1, len(sample))) > 0.20


def _decode_utf8_text(payload: bytes) -> str | None:
    if _looks_binary_bytes(payload):
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _build_uploaded_context_section(
    runtime_dir: Path,
    *,
    session_id: str,
    uploaded_files: list[str],
) -> tuple[str, str]:
    uploads_root = _uploads_dir(runtime_dir, session_id)
    if not uploaded_files:
        return "file_list_only", "No uploaded attachments."

    indexed_files: list[tuple[str, Path, int]] = []
    total_bytes = 0
    for rel in uploaded_files:
        path = uploads_root / rel
        if not path.exists() or not path.is_file():
            continue
        try:
            size = int(path.stat().st_size)
        except OSError:
            continue
        indexed_files.append((rel, path, size))
        total_bytes += size

    if not indexed_files:
        return "file_list_only", "Uploaded file records exist but files are unreadable or missing on disk."

    mode = "full_content_mandatory"
    sections: list[str] = [
        (
            f"Attachment context policy: {mode}. "
            f"indexed_files={len(indexed_files)}, total_bytes={total_bytes}."
        )
    ]

    for rel, path, size in indexed_files:
        try:
            raw_payload = path.read_bytes()
        except OSError:
            sections.append(
                f"\n### FILE: {rel}\n"
                f"- size_bytes: {size}\n"
                "- status: unreadable"
            )
            continue

        decoded = _decode_utf8_text(raw_payload)
        if decoded is None:
            encoded = base64.b64encode(raw_payload).decode("ascii")
            sections.append(
                f"\n### FILE: {rel}\n"
                f"- size_bytes: {size}\n"
                "- type: binary_or_non_utf8\n"
                "```base64\n"
                f"{encoded}\n"
                "```"
            )
            continue

        sampled_text = decoded
        truncated = False
        sections.append(
            f"\n### FILE: {rel}\n"
            f"- size_bytes: {size}\n"
            f"- sampled_mode: {mode}\n"
            f"- truncated: {'yes' if truncated else 'no'}\n"
            "```text\n"
            f"{sampled_text}\n"
            "```"
        )

    context = "\n".join(sections)
    return mode, context


def _build_llm_prompt(
    *,
    goal: str,
    target_repos: list[str],
    scope_hints: list[str],
    uploaded_files: list[str],
    uploaded_context: str,
    context_policy: str,
    feedback: str,
) -> str:
    target_repo_text = "\n".join(f"- {item}" for item in target_repos[:8]) or "- (none)"
    uploaded_preview = "\n".join(f"- {item}" for item in uploaded_files[:40]) or "- (none)"
    scope_text = ", ".join(scope_hints) if scope_hints else "."
    feedback_text = feedback.strip() or "(none)"
    return (
        "You are a software delivery planner.\n"
        "Generate a staged implementation draft from user goal and uploaded context.\n\n"
        f"User goal:\n{goal or '(empty)'}\n\n"
        f"Target repositories (first path is primary execution repo):\n{target_repo_text}\n\n"
        f"Preferred scope hints:\n{scope_text}\n\n"
        f"Uploaded files (relative list):\n{uploaded_preview}\n\n"
        f"Uploaded attachment context ({context_policy}):\n{uploaded_context}\n\n"
        f"Latest user feedback:\n{feedback_text}\n\n"
        "Rules:\n"
        "1) Produce 2 to 5 stages.\n"
        "2) Keep each stage objective concrete and verifiable.\n"
        "3) test_cases must be textual acceptance checks, not executable command lines.\n"
        "4) Keep scope_hint realistic and narrow.\n"
        "5) Do not output code.\n"
        "6) Do not invent unavailable external systems.\n"
        "7) Prefer preserving existing behavior while adding requested capability.\n"
        "8) You must analyze every uploaded file in full before proposing the stage plan.\n"
    )


def _normalize_model_draft(
    *,
    model_output: IntakeDraftResponse,
    goal: str,
    scope_hints: list[str],
    uploaded_files: list[str],
    feedback: str,
) -> dict[str, Any]:
    stages: list[dict[str, Any]] = []
    for index, stage in enumerate(model_output.stages[:5], start=1):
        stage_id = _sanitize_stage_name(stage.stage_id, default=f"stage-{index}")
        objective = stage.objective.strip() or f"Complete stage {index} objective"
        normalized_scope = [item.strip() for item in stage.scope_hint if item.strip()][:8]
        if not normalized_scope:
            normalized_scope = list(scope_hints)
        test_cases = [item.strip() for item in stage.test_cases if item.strip()][:8]
        if not test_cases:
            test_cases = [f"Primary objective achieved: {objective}"]
        notes = [item.strip() for item in stage.notes if item.strip()][:8]
        stages.append(
            {
                "stage_id": stage_id,
                "objective": objective,
                "scope_hint": normalized_scope,
                "test_cases": test_cases,
                "notes": notes,
            }
        )
    if not stages:
        raise ValueError("model returned empty stages")

    return {
        "draft_version": 1,
        "generated_at": _now_iso(),
        "goal": goal,
        "feedback": feedback.strip(),
        "scope_hints": scope_hints,
        "uploaded_files": uploaded_files,
        "stages": stages,
        "generator": "model",
        "planner_rationale": model_output.rationale.strip(),
        "planner_assumptions": [item.strip() for item in model_output.assumptions if item.strip()],
    }


def _generate_stage_draft_with_model(
    runtime_dir: Path,
    *,
    session_id: str,
    goal: str,
    target_repos: list[str],
    scope_hints: list[str],
    uploaded_files: list[str],
    feedback: str,
    target_repo: Path,
    model_name: str,
    sandbox_mode: str,
) -> tuple[dict[str, Any] | None, str]:
    if not _env_flag("MULTI_CODEX_INTAKE_ENABLE_MODEL", True):
        return None, "model_generation_disabled_by_env"
    if shutil.which("codex") is None:
        return None, "codex_cli_not_found"

    workspace = target_repo if target_repo.exists() else runtime_dir
    add_dirs: list[str] = []
    uploads_dir = _uploads_dir(runtime_dir, session_id)
    if uploads_dir.exists():
        add_dirs.append(str(uploads_dir.resolve()))
    for repo in target_repos[1:8]:
        repo_path = Path(repo).expanduser().resolve()
        if repo_path.exists():
            add_dirs.append(str(repo_path))
    context_policy, uploaded_context = _build_uploaded_context_section(
        runtime_dir,
        session_id=session_id,
        uploaded_files=uploaded_files,
    )

    timeout_sec = 180
    try:
        timeout_sec = max(30, int(os.getenv("MULTI_CODEX_INTAKE_TIMEOUT_SEC", "180")))
    except ValueError:
        timeout_sec = 180

    agent = CodexExecAgent(
        config=CodexExecAgentConfig(
            role="intake_planner",
            model=model_name or "gpt-5.3-codex",
            workspace=workspace.resolve(),
            sandbox_mode=sandbox_mode or "workspace-write",
            add_dirs=add_dirs,
            goal="Generate stage objective and textual test-case drafts.",
            backstory="Planner for intake stage drafting in monitor interactive mode.",
            timeout=timeout_sec,
            idle_timeout=min(timeout_sec, 120),
            extra_args=[],
        )
    )
    prompt = _build_llm_prompt(
        goal=goal,
        target_repos=target_repos,
        scope_hints=scope_hints,
        uploaded_files=uploaded_files,
        uploaded_context=uploaded_context,
        context_policy=context_policy,
        feedback=feedback,
    )
    try:
        result = agent.kickoff(prompt, response_format=IntakeDraftResponse)
    except Exception as exc:
        return None, f"model_invocation_failed: {exc}"

    try:
        if result.pydantic is not None:
            parsed = IntakeDraftResponse.model_validate(result.pydantic.model_dump())
        else:
            parsed = IntakeDraftResponse.model_validate_json(result.raw)
    except ValidationError as exc:
        return None, f"model_output_invalid: {exc}"
    except Exception as exc:
        return None, f"model_output_parse_failed: {exc}"

    try:
        draft = _normalize_model_draft(
            model_output=parsed,
            goal=goal,
            scope_hints=scope_hints,
            uploaded_files=uploaded_files,
            feedback=feedback,
        )
    except Exception as exc:
        return None, f"model_output_normalize_failed: {exc}"
    return draft, ""


def _generate_stage_draft_heuristic(
    *,
    goal: str,
    scope_hints: list[str],
    uploaded_files: list[str],
    feedback: str,
) -> dict[str, Any]:
    keywords = _goal_keywords(goal)

    base_tests = [
        "Happy path succeeds with valid inputs and expected outputs.",
        "Failure path is controlled with explicit errors and no corrupted state.",
        "Regression guard keeps existing critical behavior compatible.",
    ]
    if "api" in keywords:
        base_tests.append("API contract: status codes, payload fields, and error semantics stay correct.")
    if "auth" in keywords:
        base_tests.append("Auth boundary: unauthorized requests are denied and authorized requests pass.")
    if "ui" in keywords:
        base_tests.append("UI usability: key flows work on both desktop and mobile viewports.")
    if "perf" in keywords:
        base_tests.append("Performance baseline: key scenarios show no significant latency/resource regression.")

    stage_1_objective = (
        f"Deliver the minimum viable main path for the goal: {goal}"
        if goal
        else "Deliver the minimum viable main path with key dependencies integrated."
    )
    stage_2_objective = "Harden edge cases, error handling, and regression coverage for verifiable delivery."

    stages = [
        {
            "stage_id": "stage-1-core-delivery",
            "objective": stage_1_objective,
            "scope_hint": scope_hints,
            "test_cases": base_tests[:3],
            "notes": [
                "Prioritize core path coverage before non-critical expansion.",
                "Produce verifiable behavior evidence for gate review.",
            ],
        },
        {
            "stage_id": "stage-2-hardening-and-regression",
            "objective": stage_2_objective,
            "scope_hint": scope_hints,
            "test_cases": base_tests[1:],
            "notes": [
                "Close risk on error paths and compatibility boundaries.",
                "Complete regression validation within the same goal constraints.",
            ],
        },
    ]

    return {
        "draft_version": 1,
        "generated_at": _now_iso(),
        "goal": goal,
        "feedback": feedback.strip(),
        "scope_hints": scope_hints,
        "uploaded_files": uploaded_files,
        "stages": stages,
        "generator": "heuristic",
    }


def generate_stage_draft(
    runtime_dir: Path,
    *,
    session_id: str,
    feedback: str = "",
) -> dict[str, Any]:
    session = load_session(runtime_dir, session_id)
    goal = str(session.get("goal", "")).strip()
    uploaded_files = [str(item) for item in session.get("uploaded_files", [])]
    scope_hints = _infer_scope_hints(uploaded_files)
    target_repos = _normalize_target_repos(
        session.get("target_repos"),
        fallback_target_repo=str(session.get("target_repo", "")),
    )
    target_repo_raw = target_repos[0] if target_repos else ""
    target_repo = Path(target_repo_raw).expanduser().resolve() if target_repo_raw else runtime_dir

    draft: dict[str, Any]
    model_draft, model_error = _generate_stage_draft_with_model(
        runtime_dir,
        session_id=session_id,
        goal=goal,
        target_repos=target_repos,
        scope_hints=scope_hints,
        uploaded_files=uploaded_files,
        feedback=feedback,
        target_repo=target_repo,
        model_name=str(session.get("model") or "gpt-5.3-codex"),
        sandbox_mode=str(session.get("sandbox_mode") or "workspace-write"),
    )
    if model_draft is not None:
        draft = model_draft
    else:
        draft = _generate_stage_draft_heuristic(
            goal=goal,
            scope_hints=scope_hints,
            uploaded_files=uploaded_files,
            feedback=feedback,
        )
        if model_error:
            draft["model_error"] = model_error

    feedback_clean = feedback.strip()
    if feedback_clean:
        feedback_history = [str(item) for item in session.get("feedback_history", [])]
        feedback_history.append(feedback_clean)
        session["feedback_history"] = feedback_history[-20:]

    session["draft"] = draft
    session["status"] = "draft_ready"
    save_session(runtime_dir, session)
    return draft


def _sanitize_stage_name(value: str, *, default: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip().lower()).strip("-")
    return cleaned or default


def build_stage_specs_from_draft(
    *,
    draft: dict[str, Any],
    target_repo: Path,
) -> list[dict[str, Any]]:
    stages_payload = draft.get("stages")
    if not isinstance(stages_payload, list) or not stages_payload:
        raise ValueError("draft.stages is empty")

    specs: list[dict[str, Any]] = []
    previous_name = ""
    for index, stage_raw in enumerate(stages_payload, start=1):
        stage = stage_raw if isinstance(stage_raw, dict) else {}
        raw_name = str(stage.get("stage_id", "")).strip()
        stage_name = _sanitize_stage_name(raw_name, default=f"stage-{index}")

        objective = str(stage.get("objective", "")).strip() or f"Complete stage {index} objective"
        test_cases = [
            str(item).strip()
            for item in (stage.get("test_cases") or [])
            if str(item).strip()
        ]
        notes = [
            str(item).strip()
            for item in (stage.get("notes") or [])
            if str(item).strip()
        ]

        scope_hint: list[str] = []
        for raw_scope in (stage.get("scope_hint") or []):
            candidate = str(raw_scope).strip()
            if not candidate:
                continue
            if candidate == ".":
                scope_hint.append(".")
                continue
            candidate_path = (target_repo / candidate).resolve()
            if candidate_path.exists() or candidate_path.parent.exists():
                scope_hint.append(candidate)
        scope_hint = scope_hint[:8]

        acceptance_criteria = [f"Satisfy test case: {item}" for item in test_cases]
        acceptance_criteria.extend(f"Implementation note: {item}" for item in notes)
        if not acceptance_criteria:
            acceptance_criteria = [f"Satisfy stage objective: {objective}"]

        invariants = [
            "Changes must remain within confirmed user goal boundaries.",
            "Do not break existing core behavior while implementing this stage.",
        ]

        spec: dict[str, Any] = {
            "name": stage_name,
            "stage_id": stage_name,
            "objective": objective,
            "depends_on_stages": [previous_name] if previous_name else [],
            "scope_hint": scope_hint,
            "test_commands": [],
            "lint_commands": [],
            "perf_checks": [],
            "gate_commands_remote": [],
            "remote_gate_contracts": [],
            "execution_env": "local_only",
            "requires_remote": False,
            "sync_strategy": "local_only",
            "required_inputs": [],
            "produces_artifacts": [f"{stage_name}_ready"],
            "expected_artifact_paths": [],
            "blocking_decisions": [],
            "rollback_requirements": [],
            "manual_checklist": test_cases,
            "harness_constraints": [
                "Generated from monitor intake confirmation.",
                "Objective and acceptance constraints are user-confirmed.",
            ],
            "artifact_contracts": [],
            "acceptance_criteria": acceptance_criteria,
            "invariants": invariants,
        }
        specs.append(spec)
        previous_name = stage_name

    return specs


def confirm_draft_to_stages_file(runtime_dir: Path, *, session_id: str) -> Path:
    session = load_session(runtime_dir, session_id)
    draft = session.get("draft")
    if not isinstance(draft, dict) or not draft:
        raise ValueError("draft is empty, run analyze first")

    target_repo_raw = _primary_target_repo_from_session(session)
    if not target_repo_raw:
        raise ValueError("target_repo is required before confirm")
    target_repo = Path(target_repo_raw).expanduser().resolve()

    stage_specs = build_stage_specs_from_draft(draft=draft, target_repo=target_repo)
    out_path = _generated_dir(runtime_dir) / f"{_safe_session_id(session_id)}.stages.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(stage_specs, ensure_ascii=False, indent=2), encoding="utf-8")

    session["generated_stages_file"] = str(out_path)
    session["status"] = "confirmed"
    save_session(runtime_dir, session)
    return out_path


def _resolve_run_command() -> list[str]:
    cli = shutil.which("leave-me-alone")
    if cli:
        return [cli]
    # Fallback when running from source in editable mode.
    return [os.environ.get("PYTHON", "python3"), "-m", "app.main"]


def _compose_remote_host(user: str, host: str) -> str:
    normalized_user = user.strip()
    normalized_host = host.strip()
    if not normalized_host:
        return ""
    if "@" in normalized_host:
        return normalized_host
    return f"{normalized_user}@{normalized_host}" if normalized_user else normalized_host


def _build_remote_runtime_overrides(session: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    remote = _normalize_remote_validation(session.get("remote_validation"))
    if not remote.get("enabled"):
        return [], {}
    servers = remote.get("servers")
    if not isinstance(servers, list) or not servers:
        return [], {}

    primary = servers[0] if isinstance(servers[0], dict) else {}
    primary_host = _compose_remote_host(
        str(primary.get("user", "root")),
        str(primary.get("host", "")),
    )
    primary_workdir = str(primary.get("workdir", "")).strip()
    if not primary_host or not primary_workdir:
        return [], {}

    args: list[str] = [
        "--remote-host",
        primary_host,
        "--remote-workdir",
        primary_workdir,
    ]
    env_updates: dict[str, str] = {}

    password_map: dict[str, str] = {}
    primary_password = str(primary.get("password", "")).strip()
    primary_raw_host = str(primary.get("host", "")).strip()
    if primary_password:
        env_updates["MULTI_CODEX_REMOTE_SSH_PASSWORD"] = primary_password
        password_map[primary_host] = primary_password
        if primary_raw_host:
            password_map[primary_raw_host] = primary_password

    if len(servers) > 1 and isinstance(servers[1], dict):
        secondary = servers[1]
        secondary_host = _compose_remote_host(
            str(secondary.get("user", "root")),
            str(secondary.get("host", "")),
        )
        secondary_workdir = str(secondary.get("workdir", "")).strip() or primary_workdir
        if secondary_host:
            args.extend(
                [
                    "--remote-host-secondary",
                    secondary_host,
                    "--remote-workdir-secondary",
                    secondary_workdir,
                ]
            )
            secondary_password = str(secondary.get("password", "")).strip()
            secondary_raw_host = str(secondary.get("host", "")).strip()
            if secondary_password:
                password_map[secondary_host] = secondary_password
                if secondary_raw_host:
                    password_map[secondary_raw_host] = secondary_password

    if password_map:
        env_updates["MULTI_CODEX_REMOTE_SSH_PASSWORDS_JSON"] = json.dumps(password_map)
    return args, env_updates


def launch_confirmed_run(runtime_dir: Path, *, session_id: str) -> dict[str, Any]:
    session = load_session(runtime_dir, session_id)
    target_repo = _primary_target_repo_from_session(session)
    stages_file = str(session.get("generated_stages_file", "")).strip()
    if not target_repo:
        raise ValueError("target_repo is required")
    if not stages_file:
        raise ValueError("generated_stages_file is missing, confirm draft first")

    runtime_dir = runtime_dir.expanduser().resolve()
    run_log = _run_logs_dir(runtime_dir) / f"{_safe_session_id(session_id)}.log"
    run_log.parent.mkdir(parents=True, exist_ok=True)

    cmd = _resolve_run_command() + [
        "--target-repo",
        str(Path(target_repo).expanduser().resolve()),
        "--stages-file",
        str(Path(stages_file).expanduser().resolve()),
        "--runtime-dir",
        str(runtime_dir),
        "--model",
        str(session.get("model") or "gpt-5.3-codex"),
        "--sandbox-mode",
        str(session.get("sandbox_mode") or "workspace-write"),
        "--max-round-per-stage",
        str(max(1, int(session.get("max_round_per_stage") or 2))),
    ]
    remote_args, remote_env_updates = _build_remote_runtime_overrides(session)
    cmd.extend(remote_args)
    process_env = os.environ.copy()
    process_env.update(remote_env_updates)

    with run_log.open("ab") as fp:
        process = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=fp,
            stderr=subprocess.STDOUT,
            cwd=str(Path(target_repo).expanduser().resolve()),
            env=process_env,
        )

    run_info = {
        "status": "running",
        "pid": int(process.pid),
        "command": cmd,
        "log_file": str(run_log),
        "started_at": _now_iso(),
    }
    session["run"] = run_info
    session["status"] = "running"
    save_session(runtime_dir, session)
    return run_info


def _refresh_run_status(session: dict[str, Any]) -> None:
    run = session.get("run")
    if not isinstance(run, dict):
        return
    if str(run.get("status", "")) != "running":
        return
    pid = int(run.get("pid") or 0)
    if pid <= 0:
        return
    try:
        os.kill(pid, 0)
    except OSError:
        run["status"] = "finished"
        session["status"] = "finished"
