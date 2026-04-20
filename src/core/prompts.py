from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from core.models import StageGate, StageSpec


SEVERITY_POLICY = """
Severity definitions and handling policy:
- S0 (Critical): Data loss, security vulnerability, crash in production path.
      Action: MUST fix before stage passes. Blocks gate.
- S1 (Major): Incorrect behavior, broken API contract, test failure.
      Action: MUST fix before stage passes. Blocks gate.
- S2 (Moderate): Edge-case bug, performance regression, poor error handling.
      Action: MAY defer to next stage, but must be tracked.
- S3 (Minor): Style, naming, documentation, non-functional nit.
      Action: Record only. Does not block gate.
""".strip()

BUG_REPORT_TEMPLATE = """
Each bug report MUST follow this exact structure:
{
  "report_id": "<unique-id>",
  "severity": "S0|S1|S2|S3",
  "title": "<concise title>",
  "file_path": "<relative file path>",
  "line": <line number or null>,
  "evidence": "<code snippet or log output proving the issue>",
  "reasoning": "<why this is a defect>",
  "reproduction_or_inference": "<steps to reproduce OR logical inference chain>",
  "fix_suggestion": "<concrete fix suggestion>",
  "evidence_semantics": {
    "certainty": "fact|inference|to_verify",
    "facts": ["<verified fact only>"],
    "inferences": ["<reasoned inference>"],
    "to_verify": ["<what still needs runtime or code proof>"]
  }
}
""".strip()

MAX_SOURCE_REQUIREMENTS_CHARS = 24_000
MAX_PROMPT_LIST_ITEMS = 10
MAX_CHECK_SUMMARY_CHARS = 2_500


@dataclass(frozen=True)
class SourceRequirementsReport:
    source_path: str
    source_mode: str
    source_query: str
    source_anchor: str
    extracted_text: str
    extraction_sha256: str
    matched_sections: int
    truncated: bool
    errors: list[str]


def _split_h3_sections(markdown: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in markdown.splitlines():
        if line.startswith("### "):
            if current_heading is not None:
                sections.append((current_heading, "\n".join(current_lines).strip()))
            current_heading = line.strip()
            current_lines = [line]
            continue
        if current_heading is not None:
            current_lines.append(line)

    if current_heading is not None:
        sections.append((current_heading, "\n".join(current_lines).strip()))

    return sections


def _resolve_source_file(stage: StageSpec) -> Path | None:
    raw = (stage.source_file or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    return (Path.cwd() / path).resolve()


def build_stage_source_requirements_report(stage: StageSpec) -> SourceRequirementsReport:
    source_path = _resolve_source_file(stage)
    if source_path is None:
        return SourceRequirementsReport(
            source_path="",
            source_mode=stage.source_mode,
            source_query=stage.source_query or "",
            source_anchor=stage.source_anchor or "",
            extracted_text="",
            extraction_sha256="",
            matched_sections=0,
            truncated=False,
            errors=[],
        )
    errors: list[str] = []
    if not source_path.exists():
        errors.append(f"source_file not found: {source_path}")
        return SourceRequirementsReport(
            source_path=str(source_path),
            source_mode=stage.source_mode,
            source_query=stage.source_query or "",
            source_anchor=stage.source_anchor or "",
            extracted_text="",
            extraction_sha256="",
            matched_sections=0,
            truncated=False,
            errors=errors,
        )

    try:
        markdown = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"failed reading source_file {source_path}: {exc}")
        return SourceRequirementsReport(
            source_path=str(source_path),
            source_mode=stage.source_mode,
            source_query=stage.source_query or "",
            source_anchor=stage.source_anchor or "",
            extracted_text="",
            extraction_sha256="",
            matched_sections=0,
            truncated=False,
            errors=errors,
        )

    mode = stage.source_mode
    extracted = ""
    matched_sections = 0

    if mode == "full_file":
        extracted = markdown
        matched_sections = 1
    else:
        sections = _split_h3_sections(markdown)
        if mode == "anchor_section":
            anchor = (stage.source_anchor or "").strip()
            anchor_norm = anchor.removeprefix("### ").strip().lower()
            for heading, body in sections:
                heading_norm = heading.removeprefix("### ").strip().lower()
                if heading_norm == anchor_norm:
                    extracted = body
                    matched_sections = 1
                    break
            if not extracted:
                errors.append(f"anchor '{anchor}' not found in {source_path}")
        else:
            query = (stage.source_query or "").strip().lower()
            if not query:
                errors.append("source_mode=query_sections but source_query is empty")
            else:
                matched = [body for heading, body in sections if query in heading.lower()]
                if matched:
                    extracted = "\n\n".join(matched)
                    matched_sections = len(matched)
                else:
                    errors.append(
                        f"no headings matched query '{stage.source_query}' in {source_path}"
                    )

    truncated = False
    if len(extracted) > MAX_SOURCE_REQUIREMENTS_CHARS:
        truncated = True
        extracted = extracted[:MAX_SOURCE_REQUIREMENTS_CHARS] + "\n...<SOURCE TRUNCATED>..."

    extraction_sha256 = (
        hashlib.sha256(extracted.encode("utf-8")).hexdigest() if extracted else ""
    )
    return SourceRequirementsReport(
        source_path=str(source_path),
        source_mode=stage.source_mode,
        source_query=stage.source_query or "",
        source_anchor=stage.source_anchor or "",
        extracted_text=extracted,
        extraction_sha256=extraction_sha256,
        matched_sections=matched_sections,
        truncated=truncated,
        errors=errors,
    )


def _extract_stage_source_requirements(stage: StageSpec) -> str:
    report = build_stage_source_requirements_report(stage)
    if not report.source_path:
        return ""
    error_lines = "\n".join(f"- {item}" for item in report.errors) or "- none"
    extracted = report.extracted_text or "(empty)"
    return (
        "UNIQUE SOURCE OF TRUTH (must follow strictly):\n"
        f"- source_file: {report.source_path}\n"
        f"- source_mode: {report.source_mode}\n"
        f"- source_query: {report.source_query or 'N/A'}\n"
        f"- source_anchor: {report.source_anchor or 'N/A'}\n"
        f"- matched_sections: {report.matched_sections}\n"
        f"- source_truncated: {report.truncated}\n"
        f"- extraction_sha256: {report.extraction_sha256 or 'N/A'}\n"
        "Extraction errors:\n"
        f"{error_lines}\n"
        "Extracted stage requirements:\n"
        "<<<SOURCE_REQUIREMENTS_START>>>\n"
        f"{extracted}\n"
        "<<<SOURCE_REQUIREMENTS_END>>>"
    )


def trim_patch(patch: str, max_chars: int = 18_000) -> str:
    if len(patch) <= max_chars:
        return patch
    return patch[:max_chars] + "\n...<PATCH TRUNCATED>..."


def _compact_context_packet(
    payload: dict[str, object],
    *,
    max_facts: int = 8,
    max_requirements: int = 20,
) -> dict[str, object]:
    """Compact a context packet with configurable limits.

    When called from a budget-aware path, *max_facts* and *max_requirements*
    are derived from the zone budget; otherwise the defaults match the
    original static limits for backward compatibility.
    """
    round_index = payload.get("round_index", 0)
    is_early_round = isinstance(round_index, int) and round_index <= 1

    facts_limit = max_facts if is_early_round else max(4, max_facts // 2)
    backlog_limit = max_facts if is_early_round else max(2, max_facts // 2)

    synthesis = payload.get("synthesis")
    compact_synthesis: dict[str, object] = {}
    if isinstance(synthesis, dict):
        compact_synthesis = {
            "confirmed_facts": (synthesis.get("confirmed_facts") or [])[:facts_limit],
            "verification_backlog": (synthesis.get("verification_backlog") or [])[:backlog_limit],
            "open_required_actions": (synthesis.get("open_required_actions") or [])[:max_facts],
            "resolved_report_ids": (synthesis.get("resolved_report_ids") or [])[:max_requirements],
        }
    return {
        "stage_name": payload.get("stage_name", ""),
        "round_index": round_index,
        "immutable_requirements": (payload.get("immutable_requirements") or [])[:max_requirements],
        "synthesis": compact_synthesis,
        "review_patch_strategy": payload.get("review_patch_strategy", ""),
        "notes": (payload.get("notes") or [])[:6],
    }


def _compact_worker_plan(payload: dict[str, object]) -> dict[str, object]:
    return {
        "worker": payload.get("worker", ""),
        "goal": payload.get("goal", ""),
        "relevant_files": (payload.get("relevant_files") or [])[:12],
        "planned_steps": (payload.get("planned_steps") or [])[:8],
        "verification_steps": (payload.get("verification_steps") or [])[:8],
        "risks": (payload.get("risks") or [])[:8],
        "summary": payload.get("summary", ""),
    }


def _compact_list(items: list[str], *, limit: int = MAX_PROMPT_LIST_ITEMS) -> str:
    if not items:
        return "- none"
    visible = items[:limit]
    lines = [f"- {item}" for item in visible]
    remaining = len(items) - len(visible)
    if remaining > 0:
        lines.append(f"- ... and {remaining} more")
    return "\n".join(lines)


def _compact_artifact_contracts(stage: StageSpec) -> str:
    if not stage.artifact_contracts:
        return "- none"

    lines: list[str] = []
    for contract in stage.artifact_contracts[:MAX_PROMPT_LIST_ITEMS]:
        keys = ", ".join(contract.required_keys) if contract.required_keys else "none"
        lines.append(f"- {contract.path} ({contract.format}) required_keys=[{keys}]")

    remaining = len(stage.artifact_contracts) - min(
        len(stage.artifact_contracts), MAX_PROMPT_LIST_ITEMS
    )
    if remaining > 0:
        lines.append(f"- ... and {remaining} more artifact contracts")
    return "\n".join(lines)


def _build_stage_contract_packet(stage: StageSpec) -> dict[str, object]:
    return {
        "stage_name": stage.name,
        "stage_id": stage.stage_id,
        "objective": stage.objective,
        "risk_level": stage.risk_level,
        "scope_hint": stage.scope_hint,
        "write_scope": stage.write_scope,
        "depends_on_stages": stage.depends_on_stages,
        "required_inputs": stage.required_inputs,
        "produces_artifacts": stage.produces_artifacts,
        "expected_artifact_paths": stage.expected_artifact_paths,
        "invariants": stage.invariants,
        "acceptance_criteria": stage.acceptance_criteria,
        "non_goals": stage.non_goals,
        "trust_sources": stage.trust_sources,
        "trust_priority": stage.trust_priority,
        "examples": stage.examples,
        "subgoals": [subgoal.model_dump() for subgoal in stage.subgoals],
        "feature_checklist": [item.model_dump() for item in stage.feature_checklist],
        "harness_constraints": stage.harness_constraints,
        "artifact_contracts": [contract.model_dump() for contract in stage.artifact_contracts],
        "remote_gate_contracts": [contract.model_dump() for contract in stage.remote_gate_contracts],
    }


def compact_check_summary(summary: str, *, max_chars: int = MAX_CHECK_SUMMARY_CHARS) -> str:
    summary = summary.strip()
    if not summary:
        return ""

    lines = summary.splitlines()
    preferred_prefixes = (
        "Tests passed:",
        "Lint passed:",
        "Perf passed:",
        "Harness passed:",
        "[TEST] FAIL:",
        "[LINT] FAIL:",
        "[PERF] FAIL:",
        "[HARNESS] FAIL:",
        "[TEST] PASS:",
        "[LINT] PASS:",
        "[PERF] PASS:",
        "[HARNESS] PASS:",
        "stderr:",
        "stdout:",
    )
    filtered = [
        line for line in lines
        if line.strip().startswith(preferred_prefixes)
    ]
    if not filtered:
        filtered = lines[:30]

    text = "\n".join(filtered)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...<CHECK SUMMARY TRUNCATED>..."


def extract_failed_check_details(summary: str, *, max_chars: int = 8_000) -> str:
    summary = summary.strip()
    if not summary:
        return ""

    lines = summary.splitlines()
    selected: list[str] = []
    current_block: list[str] = []
    capturing_fail_block = False
    preferred_headers = (
        "=== Automated Check Results",
        "Tests passed:",
        "Lint passed:",
        "Perf passed:",
        "Harness passed:",
    )
    fail_prefixes = (
        "[TEST] FAIL:",
        "[LINT] FAIL:",
        "[PERF] FAIL:",
        "[HARNESS] FAIL:",
    )
    pass_prefixes = (
        "[TEST] PASS:",
        "[LINT] PASS:",
        "[PERF] PASS:",
        "[HARNESS] PASS:",
    )

    for line in lines:
        stripped = line.strip()
        if stripped.startswith(preferred_headers):
            selected.append(line)
            continue
        if stripped.startswith(fail_prefixes):
            if current_block:
                selected.extend(current_block)
                current_block = []
            current_block = [line]
            capturing_fail_block = True
            continue
        if stripped.startswith(pass_prefixes):
            if current_block:
                selected.extend(current_block)
                current_block = []
            capturing_fail_block = False
            continue
        if capturing_fail_block and (line.startswith("    ") or line.startswith("\t")):
            current_block.append(line)

    if current_block:
        selected.extend(current_block)

    text = "\n".join(selected) if selected else summary
    return trim_patch(text, max_chars=max_chars)


def judge_stage_gate_prompt(
    stage: StageSpec,
    default_max_round: int,
) -> str:
    source_requirements = _extract_stage_source_requirements(stage)
    stage_contract_packet = _build_stage_contract_packet(stage)
    hard_requirements: list[str] = []
    hard_requirements.extend(f"Local test command: {cmd}" for cmd in stage.test_commands)
    hard_requirements.extend(f"Local lint command: {cmd}" for cmd in stage.lint_commands)
    hard_requirements.extend(f"Local perf command: {cmd}" for cmd in stage.perf_checks)
    hard_requirements.extend(f"Remote gate command: {cmd}" for cmd in stage.gate_commands_remote)
    hard_requirements.extend(
        f"Remote gate contract: {contract.model_dump()}"
        for contract in stage.remote_gate_contracts
    )
    hard_requirements.extend(
        f"Expected evidence artifact: {path}" for path in stage.expected_artifact_paths
    )
    hard_requirements.extend(
        f"Harness constraint: {constraint}" for constraint in stage.harness_constraints
    )
    hard_requirement_text = "\n".join(f"- {item}" for item in hard_requirements) or "- None"
    return f"""
You are the JUDGE agent. Your role is to define clear, measurable acceptance
gates for each stage. Workers will be evaluated against these gates.

Current stage: {stage.name}
Objective: {stage.objective}
Scope hints: {', '.join(stage.scope_hint) if stage.scope_hint else 'N/A'}
{source_requirements}

Immutable StageSpec hard requirements:
{hard_requirement_text}
Structured STAGE_CONTRACT (authoritative):
```json
{json.dumps(stage_contract_packet, ensure_ascii=False, indent=2)}
```

{SEVERITY_POLICY}

Define acceptance gate criteria. For each category, provide EXECUTABLE
shell commands that can be run in the worker workspace:
- test_commands: commands to run tests (e.g. "pytest tests/", "npm test")
- lint_commands: commands to run linters (e.g. "ruff check .", "eslint src/")
- perf_checks: commands to check performance (e.g. benchmark scripts), or empty
- interface_contracts: textual descriptions of API contracts that must hold
- pass_criteria: human-readable summary of what "pass" means

Constraints:
- max_round_per_stage must be <= {default_max_round}
- All criteria must be measurable and executable (no vague statements)
- Commands must be runnable from the workspace root directory
- You MUST preserve all immutable StageSpec hard requirements above
- For `test_commands`, `lint_commands`, and `perf_checks`, do NOT invent, remove, or rewrite commands. Copy the StageSpec command sets verbatim.
- Do not replace stage-specific evidence requirements with generic smoke-only gates

Return ONLY valid JSON matching model: StageGate
""".strip()


def judge_plan_gate_prompt(
    stage: StageSpec,
    round_index: int,
    worker_plan_json: str,
    context_packet_json: str = "",
) -> str:
    source_requirements = _extract_stage_source_requirements(stage)
    payload: dict[str, object] = {
        "stage_contract": _build_stage_contract_packet(stage),
        "round_index": round_index,
        "worker_plan": json.loads(worker_plan_json),
    }
    if context_packet_json:
        try:
            payload["context_packet"] = json.loads(context_packet_json)
        except json.JSONDecodeError:
            payload["context_packet_raw"] = trim_patch(context_packet_json, max_chars=8_000)
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"""
You are the JUDGE agent. Review the worker plan before implementation starts.

Stage: {stage.name}
Objective: {stage.objective}
Round: {round_index}
{source_requirements}

Structured PLAN_GATE_PACKET:
```json
{payload_json}
```

Approval policy:
- Reject plans that are vague, out of scope, or missing verification.
- Reject plans that widen scope beyond declared write scope / non-goals.
- Reject plans that ignore hard invariants or acceptance criteria.
- Prefer narrow, file-tied, verifiable plans.
- If the plan is deficient, return targeted required actions in `worker_required_actions`.
- `pass_gate=true` only when the worker plan is concrete enough to proceed.

Return ONLY valid JSON matching model: PlanGateReview
""".strip()


def worker_implementation_prompt(
    worker: str,
    stage: StageSpec,
    stage_gate: StageGate,
    workspace: str,
    judge_feedback: list[str],
    approved_plan_json: str = "",
    auto_check_summary: str = "",
    context_packet_json: str = "",
    worker_entry_packet_json: str = "",
    runtime_nudges_text: str = "",
    context_budget_max_chars: int = 80_000,
) -> str:
    source_report = build_stage_source_requirements_report(stage)
    source_requirements = _extract_stage_source_requirements(stage)
    hard_requirements: list[str] = []
    hard_requirements.extend(f"local test: {cmd}" for cmd in stage.test_commands)
    hard_requirements.extend(f"local lint: {cmd}" for cmd in stage.lint_commands)
    hard_requirements.extend(f"local perf: {cmd}" for cmd in stage.perf_checks)
    hard_requirements.extend(f"remote gate: {cmd}" for cmd in stage.gate_commands_remote)
    hard_requirements.extend(
        f"expected artifact: {path}" for path in stage.expected_artifact_paths
    )
    hard_requirements.extend(
        f"harness constraint: {constraint}" for constraint in stage.harness_constraints
    )
    implementation_packet: dict[str, object] = {
        "worker": worker,
        "workspace": workspace,
        "stage_name": stage_gate.stage_name,
        "objective": stage_gate.objective,
        "allowed_write_scope": stage.write_scope,
        "source_ref": {
            "source_file": source_report.source_path,
            "source_mode": source_report.source_mode,
            "matched_sections": source_report.matched_sections,
            "source_truncated": source_report.truncated,
            "extraction_sha256": source_report.extraction_sha256 or "",
        },
        "hard_requirements": hard_requirements,
        "acceptance_gate": {
            "test_commands": stage_gate.test_commands,
            "lint_commands": stage_gate.lint_commands,
            "perf_checks": stage_gate.perf_checks,
            "artifact_paths": stage.expected_artifact_paths,
            "pass_criteria": stage.acceptance_criteria,
        },
        "judge_feedback": judge_feedback,
    }
    if context_packet_json:
        try:
            dynamic_max_facts = max(4, context_budget_max_chars // 10_000)
            dynamic_max_reqs = max(10, context_budget_max_chars // 4_000)
            implementation_packet["context_packet"] = _compact_context_packet(
                json.loads(context_packet_json),
                max_facts=dynamic_max_facts,
                max_requirements=dynamic_max_reqs,
            )
        except json.JSONDecodeError:
            implementation_packet["context_packet_raw"] = trim_patch(
                context_packet_json,
                max_chars=3_000,
            )
    if worker_entry_packet_json:
        try:
            implementation_packet["worker_entry_packet"] = json.loads(worker_entry_packet_json)
        except json.JSONDecodeError:
            implementation_packet["worker_entry_packet_raw"] = trim_patch(
                worker_entry_packet_json,
                max_chars=2_500,
            )
    if approved_plan_json:
        try:
            implementation_packet["approved_plan"] = _compact_worker_plan(
                json.loads(approved_plan_json)
            )
        except json.JSONDecodeError:
            implementation_packet["approved_plan_raw"] = trim_patch(
                approved_plan_json,
                max_chars=3_000,
            )
    failed_check_details = extract_failed_check_details(auto_check_summary)
    if failed_check_details:
        implementation_packet["previous_failing_check_details"] = failed_check_details
    implementation_packet_json = json.dumps(
        implementation_packet,
        ensure_ascii=False,
        indent=2,
    )
    nudge_section = f"\nRuntime nudges:\n{runtime_nudges_text}\n" if runtime_nudges_text else ""

    return f"""
You are {worker}. Implement this stage in your OWN isolated workspace.
{nudge_section}

Authoritative source requirements:
{source_requirements}

Structured IMPLEMENTATION_PACKET (authoritative input; do not invent extra goals):
```json
{implementation_packet_json}
```

Required behavior:
1) Treat `source_requirements` plus `hard_requirements` as the authoritative implementation scope.
2) Satisfy every item in `hard_requirements` and `acceptance_gate`.
3) Follow `approved_plan` as the execution envelope. Do not widen scope unless hard evidence forces it.
4) If `previous_failing_check_details` is present, address those failures first.
5) Use the context packet synthesis instead of replaying prior conversation.
6) Prefer code changes and fast local validation. Do NOT spend long blocking time on canonical remote gate reruns; the harness auto-check phase is authoritative for remote gates.
7) Do the work silently. Do NOT narrate your plan or progress outside the final JSON object.
8) Final summary must include changed files, tests/lint/perf executed, risks, and unresolved items.

Return ONLY valid JSON matching model: WorkerDelivery
""".strip()


def worker_repair_prompt(
    worker: str,
    stage: StageSpec,
    workspace: str,
    required_actions: list[str],
    approved_plan_json: str = "",
    auto_check_summary: str = "",
    context_packet_json: str = "",
    worker_entry_packet_json: str = "",
    previous_handoff_json: str = "",
    current_changed_files: list[str] | None = None,
    current_status_lines: list[str] | None = None,
    current_patch: str = "",
    runtime_nudges_text: str = "",
    current_blocker: str = "",
    context_budget_max_chars: int = 80_000,
) -> str:
    source_requirements = _extract_stage_source_requirements(stage)
    current_changed_files = current_changed_files or []
    current_status_lines = current_status_lines or []
    failed_check_details = extract_failed_check_details(auto_check_summary)
    repair_packet: dict[str, object] = {
        "worker": worker,
        "workspace": workspace,
        "stage_name": stage.name,
        "objective": stage.objective,
        "allowed_write_scope": stage.write_scope,
        "expected_artifact_paths": stage.expected_artifact_paths,
        "required_actions": required_actions,
        "current_blocker": current_blocker,
        "current_changed_files": current_changed_files[:40],
        "current_git_status": current_status_lines[:60],
    }
    if context_packet_json:
        try:
            dynamic_max_facts = max(4, context_budget_max_chars // 10_000)
            dynamic_max_reqs = max(10, context_budget_max_chars // 4_000)
            repair_packet["context_packet"] = _compact_context_packet(
                json.loads(context_packet_json),
                max_facts=dynamic_max_facts,
                max_requirements=dynamic_max_reqs,
            )
        except json.JSONDecodeError:
            repair_packet["context_packet_raw"] = trim_patch(
                context_packet_json,
                max_chars=3_000,
            )
    if worker_entry_packet_json:
        try:
            repair_packet["worker_entry_packet"] = json.loads(worker_entry_packet_json)
        except json.JSONDecodeError:
            repair_packet["worker_entry_packet_raw"] = trim_patch(
                worker_entry_packet_json,
                max_chars=2_500,
            )
    if approved_plan_json:
        try:
            repair_packet["approved_plan"] = _compact_worker_plan(
                json.loads(approved_plan_json)
            )
        except json.JSONDecodeError:
            repair_packet["approved_plan_raw"] = trim_patch(
                approved_plan_json,
                max_chars=3_000,
            )
    if failed_check_details:
        repair_packet["failing_check_details"] = failed_check_details
    if previous_handoff_json:
        try:
            repair_packet["previous_handoff"] = json.loads(previous_handoff_json)
        except json.JSONDecodeError:
            repair_packet["previous_handoff_raw"] = trim_patch(
                previous_handoff_json,
                max_chars=3_000,
            )
    if current_patch.strip():
        repair_packet["current_workspace_patch"] = trim_patch(
            current_patch,
            max_chars=6_000,
        )
    repair_packet_json = json.dumps(
        repair_packet,
        ensure_ascii=False,
        indent=2,
    )
    nudge_section = f"\nRuntime nudges:\n{runtime_nudges_text}\n" if runtime_nudges_text else ""

    return f"""
You are {worker}. Repair only the remaining blocking issues in your OWN isolated workspace.

Workspace: {workspace}
Stage: {stage.name}
Objective: {stage.objective}
{source_requirements}
{nudge_section}

Structured REPAIR_PACKET (authoritative input; do not invent extra goals):
```json
{repair_packet_json}
```

Rules:
1) Do NOT restart the stage from scratch. Only act on `required_actions`, `current_blocker`, `failing_check_details`, `context_packet.synthesis.open_required_actions`, and `approved_plan`.
2) Constrain edits to your own workspace and to files already changed, files named in failing checks, or files inside `allowed_write_scope`.
3) Preserve already-working behavior and existing passing checks.
4) Keep repairs inside the approved plan envelope unless failing evidence proves a missing root cause.
5) Prioritize the narrowest root cause for build/test/gate failures before documentation polish.
6) Prefer fixing the code and preparing evidence locally. Do NOT block for long on canonical remote gate reruns; the harness auto-check phase will rerun authoritative remote gates after your delivery.
7) If the packet shows no remaining worker-local action, return a valid JSON no-op summary with empty changed_files/tests/lint/perf lists immediately.
8) Do the work silently. Do NOT narrate your plan or progress outside the final JSON object.

Return ONLY valid JSON matching model: WorkerDelivery
""".strip()


def worker_planner_prompt(
    worker: str,
    stage: StageSpec,
    workspace: str,
    round_index: int,
    judge_feedback: list[str],
    context_packet_json: str = "",
    auto_check_summary: str = "",
    worker_entry_packet_json: str = "",
    runtime_nudges_text: str = "",
    context_budget_max_chars: int = 80_000,
) -> str:
    source_requirements = _extract_stage_source_requirements(stage)
    planner_packet: dict[str, object] = {
        "worker": worker,
        "workspace": workspace,
        "stage_name": stage.name,
        "round_index": round_index,
        "objective": stage.objective,
        "stage_contract": _build_stage_contract_packet(stage),
        "judge_feedback": judge_feedback,
        "allowed_write_scope": stage.write_scope or stage.scope_hint,
    }
    if context_packet_json:
        try:
            dynamic_max_facts = max(4, context_budget_max_chars // 10_000)
            dynamic_max_reqs = max(10, context_budget_max_chars // 4_000)
            planner_packet["context_packet"] = _compact_context_packet(
                json.loads(context_packet_json),
                max_facts=dynamic_max_facts,
                max_requirements=dynamic_max_reqs,
            )
        except json.JSONDecodeError:
            planner_packet["context_packet_raw"] = trim_patch(
                context_packet_json,
                max_chars=8_000,
            )
    if worker_entry_packet_json:
        try:
            planner_packet["worker_entry_packet"] = json.loads(worker_entry_packet_json)
        except json.JSONDecodeError:
            planner_packet["worker_entry_packet_raw"] = trim_patch(
                worker_entry_packet_json,
                max_chars=3_000,
            )
    failed_check_details = extract_failed_check_details(auto_check_summary)
    if failed_check_details:
        planner_packet["previous_failing_check_details"] = failed_check_details
    planner_packet_json = json.dumps(
        planner_packet,
        ensure_ascii=False,
        indent=2,
    )
    nudge_section = f"\nRuntime nudges:\n{runtime_nudges_text}\n" if runtime_nudges_text else ""

    return f"""
You are {worker}. Produce a structured read-first implementation plan before any coding.

Workspace: {workspace}
Stage: {stage.name}
Objective: {stage.objective}
{source_requirements}
{nudge_section}

Structured PLANNER_PACKET (authoritative input; do not invent extra goals):
```json
{planner_packet_json}
```

Rules:
1) This is Plan Mode, not implementation mode. Do NOT edit files, run long builds, or claim completion.
2) Treat `source_of_truth.extracted_text`, `stage_contract`, and `context_packet` as authoritative.
3) The plan must stay within `allowed_write_scope`.
4) `planned_steps` should be concrete, ordered, and tied to files.
5) `verification_steps` should prefer the narrowest fast checks that prove progress.
6) Surface risks and assumptions explicitly; do not hide uncertainty in prose.

Return ONLY valid JSON matching model: WorkerPlan
""".strip()


def worker_self_review_prompt(
    worker: str,
    stage_name: str,
    patch: str,
    auto_check_summary: str = "",
    context_packet_json: str = "",
    check_artifact_json: str = "",
) -> str:
    context_section = f"\nContext packet:\n{context_packet_json}\n" if context_packet_json else ""
    check_artifact_section = (
        f"\nStructured check artifact:\n{check_artifact_json}\n"
        if check_artifact_json else ""
    )
    check_section = ""
    if auto_check_summary:
        check_section = f"""
Automated check results for your code:
{auto_check_summary}

Any failing checks are objective evidence of defects. Prioritize fixing those.
"""
    return f"""
You are {worker}. Perform self-review on your newly changed code.

Stage: {stage_name}

{SEVERITY_POLICY}
{context_section}
{check_artifact_section}

Your patch:
{trim_patch(patch)}
{check_section}
Required behavior:
1) Aggressively find potential bugs, security issues, and correctness problems.
2) Fix issues you can fix now in your own workspace.
3) For each issue found, classify severity (S0/S1/S2/S3).
4) Separate verified facts from inferences in your internal reasoning.
5) Report what was fixed and what remains as risk.

Return ONLY valid JSON matching model: SelfReviewResult
""".strip()


# DEPRECATED in single-worker mode: peer review was removed in the
# single-worker + dual-judge architecture.  Kept for reference only.
def worker_peer_review_prompt(
    reviewer: str,
    target_worker: str,
    stage_name: str,
    target_patch: str,
    target_auto_check_summary: str = "",
    context_packet_json: str = "",
    target_check_artifact_json: str = "",
    seen_report_ids: list[str] | None = None,
    resolved_report_ids: list[str] | None = None,
) -> str:
    seen_report_ids = seen_report_ids or []
    resolved_report_ids = resolved_report_ids or []
    context_section = f"\nContext packet:\n{context_packet_json}\n" if context_packet_json else ""
    dedupe_section = f"""
Previously seen report IDs for this stage:
{seen_report_ids}

Previously resolved/rejected report IDs:
{resolved_report_ids}
""" if (seen_report_ids or resolved_report_ids) else ""
    check_section = ""
    if target_auto_check_summary:
        check_section = f"""
Automated check results for {target_worker}'s code:
{target_auto_check_summary}

Use these objective results as evidence in your reports. Failing checks
should be reported as bugs with the check output as evidence.
"""
    if target_check_artifact_json:
        check_section += f"""
Structured check artifact for {target_worker}:
{target_check_artifact_json}
"""
    return f"""
You are {reviewer}. Review peer code from {target_worker}.

Stage: {stage_name}

{SEVERITY_POLICY}

{BUG_REPORT_TEMPLATE}
{context_section}
{dedupe_section}

Target patch from {target_worker}:
{trim_patch(target_patch)}
{check_section}
Rules:
- Be strict and thorough. Report ALL potential defects with evidence.
- Every report MUST follow the structured BugReport schema above.
- Include file_path and line number for each finding.
- S0/S1 findings are mandatory to report. Do not skip them.
- S2 findings should be reported but can be marked for deferral.
- S3 findings: report only if clearly problematic.
- Do NOT report style preferences as S0/S1.
- Use certainty='fact' only when the evidence is directly observable from code/check output.
- Use certainty='inference' when the issue is reasoned but not fully proven.
- Use certainty='to_verify' when runtime proof is still missing.
- Do NOT re-report an existing report_id unless the severity or evidence materially changed.

Return ONLY valid JSON matching model: PeerReviewResult
""".strip()


# DEPRECATED in single-worker mode: owner triage was removed in the
# single-worker + dual-judge architecture.  Kept for reference only.
def owner_triage_prompt(
    owner: str,
    stage_name: str,
    reports_json: str,
    context_packet_json: str = "",
    latest_handoff_json: str = "",
) -> str:
    context_section = f"\nContext packet:\n{context_packet_json}\n" if context_packet_json else ""
    handoff_section = f"\nLatest handoff:\n{latest_handoff_json}\n" if latest_handoff_json else ""
    return f"""
You are {owner}. Triage peer bug reports filed against your code.

Stage: {stage_name}

{SEVERITY_POLICY}
{context_section}
{handoff_section}

Peer reports:
{reports_json}

Rules:
- For EACH report, choose action: "accept_fix" or "reject".
- S0/S1 reports: you MUST either accept_fix or provide strong evidence for reject.
  Rejecting S0/S1 without concrete evidence will escalate to judge as a dispute.
- S2 reports: accept_fix if feasible now, or reject with rationale to defer.
- S3 reports: accept_fix or reject at your discretion.
- Reports with certainty='inference' should either be upgraded with evidence or rejected with proof.
- Reports with certainty='to_verify' should usually become checklist/defer items, not hard blockers.
- If accept_fix: patch code in your workspace and summarize the patch.
- If reject: explain clearly WHY with evidence (not just "disagree").

Return ONLY valid JSON matching model: OwnerTriageResult
""".strip()


def verifier_review_prompt(
    stage: StageSpec,
    round_index: int,
    verifier_packet_json: str,
    context_packet_json: str = "",
) -> str:
    source_requirements = _extract_stage_source_requirements(stage)
    context_section = f"\nContext packet:\n{context_packet_json}\n" if context_packet_json else ""
    return f"""
You are VERIFIER. Audit the stage strictly against the declared contract.

Stage: {stage.name}
Round: {round_index}
{source_requirements}
{context_section}

Structured VERIFIER_PACKET:
```json
{verifier_packet_json}
```

Rules:
- Evaluate acceptance criteria, invariants, artifact contracts, and remote/check evidence criterion-by-criterion.
- Output gaps and evidence only. Do NOT propose code changes and do NOT make the final pass/fail governance decision.
- Use `satisfied` only when the criterion is backed by direct evidence from code/checks/artifacts.
- Use `needs_more_evidence` when the criterion may be met but proof is incomplete.
- Use `gap` when the criterion is not met or contradicted by evidence.
- Set `spec_gap_detected=true` only when the contract itself is too ambiguous or internally inconsistent to verify reliably.
- If `spec_gap_detected=true`, fill `ambiguous_contracts` and `requested_clarifications` with concrete contract defects; do not blame implementation for ambiguity.
- `pass_ready=true` only if there are no blocking gaps and no missing mandatory evidence.

Return ONLY valid JSON matching model: VerifierReport
""".strip()


def judge_independent_review_prompt(
    judge_role: str,
    stage_name: str,
    round_index: int,
    worker_patch: str,
    auto_check_summary: str = "",
    context_packet_json: str = "",
    check_artifact_json: str = "",
    verifier_report_json: str = "",
) -> str:
    context_section = f"\nContext packet:\n{context_packet_json}\n" if context_packet_json else ""
    check_artifact_section = (
        f"\nStructured check artifact:\n{check_artifact_json}\n"
        if check_artifact_json else ""
    )
    verifier_section = (
        f"\nVerifier report:\n{verifier_report_json}\n"
        if verifier_report_json else ""
    )
    check_section = ""
    if auto_check_summary:
        check_section = f"""
Automated check results (objective, non-negotiable):
{auto_check_summary}

IMPORTANT: If automated checks fail, the stage CANNOT pass regardless of
subjective review outcomes. Automated check failures are S0/S1 by definition.
"""
    return f"""
You are {judge_role}. Perform an INDEPENDENT code review and gate decision.

Stage: {stage_name}
Round: {round_index}

{SEVERITY_POLICY}

{BUG_REPORT_TEMPLATE}
{context_section}
{check_artifact_section}
{verifier_section}

Worker patch:
{trim_patch(worker_patch)}
{check_section}
Review rules:
1. Review the worker's code changes thoroughly and independently.
2. Report ALL potential defects with evidence using the BugReport schema.
3. Evaluate verifier blocking gaps, high-severity (S0/S1) items, and automated checks.
4. Automated check failures are non-negotiable blockers.
5. Set pass_gate=true ONLY if:
   - All automated checks pass
   - Verifier says pass_ready=true
   - No open fact-grade S0/S1 issues remain
6. If pass_gate=false, provide concrete required_actions for the next round.
7. Be strict, thorough, and evidence-based.

Return ONLY valid JSON matching model: JudgeGateReview
""".strip()

def judge_gate_review_prompt(
    stage_name: str,
    round_index: int,
    judge_packet_json: str,
    auto_check_summary: str = "",
    context_packet_json: str = "",
    check_artifacts_json: str = "",
    convergence_signal_json: str = "",
) -> str:
    context_section = f"\nContext packet:\n{context_packet_json}\n" if context_packet_json else ""
    check_artifacts_section = (
        f"\nStructured check artifacts:\n{check_artifacts_json}\n"
        if check_artifacts_json else ""
    )
    convergence_section = (
        f"\nConvergence signal:\n{convergence_signal_json}\n"
        if convergence_signal_json else ""
    )
    check_section = ""
    if auto_check_summary:
        check_section = f"""
Automated check results (objective, non-negotiable):
{auto_check_summary}

IMPORTANT: If automated checks fail, the stage CANNOT pass regardless of
subjective review outcomes. Automated check failures are S0/S1 by definition.
"""
    return f"""
You are JUDGE. Perform the final gate review for this round.

Stage: {stage_name}
Round: {round_index}

{SEVERITY_POLICY}
{context_section}
{check_artifacts_section}
{convergence_section}

Input (high-severity + disputed items only):
{judge_packet_json}
{check_section}
Gate review rules:
1. Review verifier blocking gaps, high-severity (S0/S1) items, and automated checks.
2. Treat verifier as the authoritative gap auditor. Do NOT redo full criterion-by-criterion verification from scratch.
3. Automated check failures are non-negotiable blockers.
4. Set pass_gate=true ONLY if:
   - All automated checks pass
   - Verifier says pass_ready=true
   - No open fact-grade S0/S1 issues remain
5. inference/to_verify findings should request more evidence or checklist follow-up,
   not block by themselves unless automated checks or direct code facts prove them.
6. If pass_gate=false, provide concrete required_actions for the next round.
7. Be concise but specific in rationale.

Return ONLY valid JSON matching model: JudgeGateReview
""".strip()
