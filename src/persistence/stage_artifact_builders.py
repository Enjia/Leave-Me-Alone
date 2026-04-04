from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from core.models import (
    FailureClassification,
    RepoProgressNote,
    SpecGapReport,
    StageArtifact,
    StageSpec,
    StageSpecSnapshotArtifact,
    StageProgressLedger,
    VerifierReport,
)
from core.prompts import SourceRequirementsReport


def build_repo_progress_note(
    *,
    stage: StageSpec,
    ledger: StageProgressLedger,
    verified_facts: list[str],
    repeated_failure_points: list[str],
    stable_workarounds: list[str],
) -> RepoProgressNote:
    return RepoProgressNote(
        stage_name=stage.name,
        stage_id=stage.stage_id,
        status=ledger.status,
        round_index=ledger.round_index,
        active_subgoal=ledger.active_subgoal_title,
        current_blocker=ledger.current_blocker,
        verified_facts=verified_facts,
        repeated_failure_points=repeated_failure_points,
        stable_workarounds=stable_workarounds,
    )


def build_repo_progress_markdown(note: RepoProgressNote) -> str:
    lines = [
        f"# {note.stage_name}",
        "",
        f"- status: `{note.status}`",
        f"- round: `{note.round_index}`",
        f"- active_subgoal: `{note.active_subgoal or 'N/A'}`",
        f"- current_blocker: `{note.current_blocker or 'none'}`",
        "",
        "## Verified Facts",
    ]
    lines.extend(f"- {item}" for item in (note.verified_facts or ["none"]))
    lines.extend(["", "## Repeated Failure Points"])
    lines.extend(f"- {item}" for item in (note.repeated_failure_points or ["none"]))
    lines.extend(["", "## Stable Workarounds"])
    lines.extend(f"- {item}" for item in (note.stable_workarounds or ["none"]))
    lines.append("")
    return "\n".join(lines)


def build_spec_gap_report(
    *,
    stage: StageSpec,
    round_index: int,
    verifier_report: VerifierReport,
) -> SpecGapReport:
    return SpecGapReport(
        stage_name=stage.name,
        round_index=round_index,
        spec_gap_detected=True,
        ambiguous_contracts=list(verifier_report.ambiguous_contracts),
        requested_clarifications=list(verifier_report.requested_clarifications),
        rationale="Verifier determined the stage contract is too ambiguous to verify reliably.",
    )


def build_spec_gap_failure_classification(report: SpecGapReport) -> FailureClassification:
    return FailureClassification(
        code="spec_gap_detected",
        category="spec_gap",
        disposition="blocked",
        summary="Verifier detected a contract ambiguity; the harness rolled back to spec.",
        owner="system",
        retryable=False,
        evidence=list(report.ambiguous_contracts) + list(report.requested_clarifications),
    )


def build_stage_spec_snapshot(stage: StageSpec) -> StageSpecSnapshotArtifact:
    return StageSpecSnapshotArtifact(
        stage_name=stage.name,
        stage_id=stage.stage_id,
        objective=stage.objective,
        risk_level=stage.risk_level,
        depends_on_stages=list(stage.depends_on_stages),
        invariants=list(stage.invariants),
        acceptance_criteria=list(stage.acceptance_criteria),
        non_goals=list(stage.non_goals),
        trust_sources=list(stage.trust_sources),
        trust_priority=list(stage.trust_priority),
        examples=list(stage.examples),
    )


def build_source_preflight_payload(stage: StageSpec, report: SourceRequirementsReport) -> dict[str, Any]:
    return {
        "stage_name": stage.name,
        "status": "ok" if not report.errors and not report.truncated else "error",
        "report": asdict(report),
    }


def build_remote_preflight_payload(stage: StageSpec, worker: str, results: list[Any]) -> dict[str, Any]:
    return {
        "stage_name": stage.name,
        "worker": worker,
        "status": "passed" if all(item.passed for item in results) else "failed",
        "results": [item.model_dump() for item in results],
    }


def build_stage_completion_artifacts(stage: StageSpec, result: Any) -> list[StageArtifact]:
    timestamp = datetime.now(timezone.utc).isoformat()
    objective_hash = hashlib.sha256(stage.objective.encode("utf-8")).hexdigest()
    stage_artifacts: list[StageArtifact] = []
    for artifact_name in stage.produces_artifacts:
        artifact_data = {
            "stage_passed": result.passed,
            "rounds_used": result.rounds_used,
            "gate_rationale": result.gate.rationale if result.gate else "",
        }
        data_hash = hashlib.sha256(
            json.dumps(artifact_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        stage_artifacts.append(
            StageArtifact(
                stage_name=stage.name,
                artifact_name=artifact_name,
                objective_hash=objective_hash,
                data_hash=data_hash,
                data=artifact_data,
                produced_at=timestamp,
                notes=f"Auto-generated from stage '{stage.name}' completion",
            )
        )
    return stage_artifacts
