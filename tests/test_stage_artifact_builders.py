from __future__ import annotations

from dataclasses import dataclass

from core.models import (
    JudgeGateReview,
    StageProgressLedger,
    StageSpec,
    VerifierReport,
)
from persistence.stage_artifact_builders import (
    build_remote_preflight_payload,
    build_repo_progress_markdown,
    build_repo_progress_note,
    build_source_preflight_payload,
    build_spec_gap_failure_classification,
    build_spec_gap_report,
    build_stage_completion_artifacts,
    build_stage_spec_snapshot,
)
from core.prompts import SourceRequirementsReport


@dataclass
class _PreflightResult:
    passed: bool
    command: str

    def model_dump(self) -> dict[str, object]:
        return {"passed": self.passed, "command": self.command}


@dataclass
class _StageResult:
    passed: bool
    rounds_used: int
    gate: JudgeGateReview | None


def _stage() -> StageSpec:
    return StageSpec(
        name="Stage A",
        stage_id="stage.a",
        objective="ship it",
        acceptance_criteria=["done"],
        invariants=["safe"],
        produces_artifacts=["report"],
        trust_sources=["stage_spec"],
        non_goals=["skip docs"],
        examples=["example"],
    )


def test_repo_progress_builders_render_expected_markdown() -> None:
    stage = _stage()
    ledger = StageProgressLedger(
        stage_name=stage.name,
        stage_id=stage.stage_id,
        round_index=2,
        status="running",
        active_subgoal_id="sg1",
        active_subgoal_title="Subgoal 1",
        passed_gates=["plan_gate"],
        latest_artifacts=["artifacts/x.json"],
        current_blocker="none",
        current_blocker_category="",
        notes=["n1"],
    )
    note = build_repo_progress_note(
        stage=stage,
        ledger=ledger,
        verified_facts=["fact"],
        repeated_failure_points=["failure"],
        stable_workarounds=["workaround"],
    )
    markdown = build_repo_progress_markdown(note)

    assert note.stage_name == "Stage A"
    assert "## Verified Facts" in markdown
    assert "- fact" in markdown


def test_spec_gap_builders_preserve_verifier_evidence() -> None:
    report = build_spec_gap_report(
        stage=_stage(),
        round_index=3,
        verifier_report=VerifierReport(
            stage_name="Stage A",
            round_index=3,
            pass_ready=False,
            criteria_results=[],
            blocking_gaps=[],
            evidence_gaps=[],
            ambiguous_contracts=["ambiguous"],
            requested_clarifications=["clarify"],
            spec_gap_detected=True,
        ),
    )
    classification = build_spec_gap_failure_classification(report)

    assert report.ambiguous_contracts == ["ambiguous"]
    assert classification.category == "spec_gap"
    assert classification.evidence == ["ambiguous", "clarify"]


def test_stage_snapshot_and_source_payload_builders() -> None:
    stage = _stage()
    snapshot = build_stage_spec_snapshot(stage)
    source_payload = build_source_preflight_payload(
        stage,
        SourceRequirementsReport(
            source_path="docs/spec.md",
            source_mode="path",
            source_query="",
            source_anchor="",
            extracted_text="requirements",
            extraction_sha256="abc123",
            matched_sections=[],
            errors=[],
            truncated=False,
        ),
    )

    assert snapshot.stage_id == "stage.a"
    assert snapshot.trust_sources == ["stage_spec"]
    assert source_payload["status"] == "ok"


def test_remote_preflight_and_stage_completion_builders() -> None:
    payload = build_remote_preflight_payload(
        _stage(),
        "worker_a",
        [_PreflightResult(passed=True, command="echo ok")],
    )
    artifacts = build_stage_completion_artifacts(
        _stage(),
        _StageResult(
            passed=True,
            rounds_used=1,
            gate=JudgeGateReview(
                stage_name="Stage A",
                round_index=1,
                pass_gate=True,
                rationale="ok",
            ),
        ),
    )

    assert payload["status"] == "passed"
    assert artifacts[0].artifact_name == "report"
    assert artifacts[0].data["stage_passed"] is True
