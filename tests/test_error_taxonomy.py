"""Tests for errors/taxonomy.py and errors/recovery.py."""
from __future__ import annotations

import pytest

from errors.taxonomy import (
    classify_budget_exhausted,
    classify_check_failure,
    classify_human_decision_required,
    classify_transient_error,
)
from errors.recovery import (
    ACTION_BLOCKED,
    ACTION_HUMAN_DECISION,
    ACTION_REPAIR_REQUIRED,
    ACTION_RETRY_IMMEDIATE,
    ACTION_RETRY_NEXT_ROUND,
    ACTION_RETRY_WITH_BACKOFF,
    ACTION_ROLLBACK_TO_SPEC,
    ACTION_STOP_AND_REPLAN,
    compute_backoff_delay,
    recommended_recovery,
)
from core.models import FailureClassification


# ---------------------------------------------------------------------------
# Tests: classify_transient_error
# ---------------------------------------------------------------------------

class TestClassifyTransientError:
    def test_returns_none_for_empty_text(self) -> None:
        assert classify_transient_error("") is None

    def test_returns_none_for_non_transient_text(self) -> None:
        assert classify_transient_error("build failed: missing semicolon") is None

    def test_detects_rate_limit(self) -> None:
        result = classify_transient_error("Error: 429 Too Many Requests")
        assert result is not None
        assert result.category == "transient"
        assert result.code == "rate_limited"
        assert result.retryable is True

    def test_detects_timeout(self) -> None:
        result = classify_transient_error("request timed out after 30s")
        assert result is not None
        assert result.code == "request_timeout"

    def test_detects_connection_error(self) -> None:
        result = classify_transient_error("ECONNRESET: connection reset by peer")
        assert result is not None
        assert result.code == "connection_error"

    def test_detects_service_unavailable(self) -> None:
        result = classify_transient_error("HTTP 503 Service Unavailable")
        assert result is not None
        assert result.code == "service_unavailable"

    def test_detects_gateway_timeout(self) -> None:
        result = classify_transient_error("504 Gateway Timeout")
        assert result is not None
        assert result.code == "service_unavailable"

    def test_detects_transient_in_json_line(self) -> None:
        json_line = '{"type": "turn.failed", "error": {"message": "rate limit exceeded"}}'
        result = classify_transient_error(json_line)
        assert result is not None
        assert result.code == "rate_limited"

    def test_case_insensitive(self) -> None:
        result = classify_transient_error("RATE LIMIT EXCEEDED")
        assert result is not None
        assert result.code == "rate_limited"


# ---------------------------------------------------------------------------
# Tests: classify_check_failure
# ---------------------------------------------------------------------------

class TestClassifyCheckFailure:
    def test_rejected_command(self) -> None:
        result = classify_check_failure(
            worker="worker_a",
            check_type="test",
            command="rm -rf /",
            exit_code=-2,
            stdout="",
            stderr="rejected",
        )
        assert result.category == "input_contract"
        assert result.code == "rejected_command"
        assert result.retryable is False

    def test_timeout_detection(self) -> None:
        result = classify_check_failure(
            worker="worker_a",
            check_type="test",
            command="pytest",
            exit_code=1,
            stdout="",
            stderr="command timed out",
        )
        assert result.category == "timeout"
        assert result.retryable is True

    def test_harness_remote_gate(self) -> None:
        result = classify_check_failure(
            worker="worker_b",
            check_type="harness",
            command="dev_env_remote.sh --cmd 'make test'",
            exit_code=1,
            stdout="FAIL",
            stderr="",
        )
        assert result.category == "remote_gate"

    def test_harness_artifact_contract(self) -> None:
        result = classify_check_failure(
            worker="worker_a",
            check_type="harness",
            command="validate_artifacts.sh",
            exit_code=1,
            stdout="missing required key",
            stderr="",
        )
        assert result.category == "artifact_contract"

    def test_standard_test_failure(self) -> None:
        result = classify_check_failure(
            worker="worker_a",
            check_type="test",
            command="pytest tests/",
            exit_code=1,
            stdout="3 failed",
            stderr="",
        )
        assert result.category == "automated_checks"
        assert result.code == "test_failed"

    def test_transient_in_check_output(self) -> None:
        result = classify_check_failure(
            worker="worker_a",
            check_type="test",
            command="pytest",
            exit_code=1,
            stdout="",
            stderr="429 Too Many Requests",
        )
        assert result.category == "transient"
        assert result.retryable is True


# ---------------------------------------------------------------------------
# Tests: classify_budget_exhausted / classify_human_decision_required
# ---------------------------------------------------------------------------

class TestPolicyClassifications:
    def test_budget_exhausted(self) -> None:
        result = classify_budget_exhausted(
            stage_name="lint",
            details="Hard budget $5.00 exceeded",
        )
        assert result.category == "policy"
        assert result.code == "budget_exhausted"
        assert result.retryable is False

    def test_human_decision_required(self) -> None:
        result = classify_human_decision_required(
            stage_name="integration",
            decisions=["approve remote deploy", "confirm rollback"],
        )
        assert result.category == "human_decision"
        assert result.code == "human_decision_required"
        assert len(result.evidence) == 2


# ---------------------------------------------------------------------------
# Tests: recommended_recovery
# ---------------------------------------------------------------------------

class TestRecommendedRecovery:
    def test_transient_gets_retry_with_backoff(self) -> None:
        classification = FailureClassification(
            code="rate_limited",
            category="transient",
            disposition="retry_same_round",
            summary="429",
            retryable=True,
        )
        recovery = recommended_recovery(classification)
        assert recovery["action"] == ACTION_RETRY_WITH_BACKOFF
        assert recovery["retryable"] is True
        assert "base_delay_sec" in recovery

    def test_timeout_gets_retry_next_round(self) -> None:
        classification = FailureClassification(
            code="check_timeout",
            category="timeout",
            disposition="retry_next_round",
            summary="timed out",
            retryable=True,
        )
        recovery = recommended_recovery(classification)
        assert recovery["action"] == ACTION_RETRY_NEXT_ROUND

    def test_policy_gets_blocked(self) -> None:
        classification = FailureClassification(
            code="budget_exhausted",
            category="policy",
            disposition="blocked",
            summary="budget exceeded",
        )
        recovery = recommended_recovery(classification)
        assert recovery["action"] == ACTION_BLOCKED
        assert recovery["retryable"] is False

    def test_human_decision_gets_human_action(self) -> None:
        classification = FailureClassification(
            code="human_decision_required",
            category="human_decision",
            disposition="blocked",
            summary="needs approval",
        )
        recovery = recommended_recovery(classification)
        assert recovery["action"] == ACTION_HUMAN_DECISION

    def test_spec_gap_gets_rollback(self) -> None:
        classification = FailureClassification(
            code="spec_gap",
            category="spec_gap",
            disposition="terminal",
            summary="spec gap detected",
        )
        recovery = recommended_recovery(classification)
        assert recovery["action"] == ACTION_ROLLBACK_TO_SPEC

    def test_structured_output_gets_retry_immediate(self) -> None:
        classification = FailureClassification(
            code="structured_output_parse_error",
            category="structured_output",
            disposition="retry_same_round",
            summary="non-JSON output",
            retryable=True,
        )
        recovery = recommended_recovery(classification)
        assert recovery["action"] == ACTION_RETRY_IMMEDIATE

    def test_workspace_state_gets_stop(self) -> None:
        classification = FailureClassification(
            code="workspace_corrupted",
            category="workspace_state",
            disposition="terminal",
            summary="workspace corrupted",
        )
        recovery = recommended_recovery(classification)
        assert recovery["action"] == ACTION_STOP_AND_REPLAN

    def test_automated_checks_gets_repair(self) -> None:
        classification = FailureClassification(
            code="test_failed",
            category="automated_checks",
            disposition="repair_required",
            summary="3 tests failed",
        )
        recovery = recommended_recovery(classification)
        assert recovery["action"] == ACTION_REPAIR_REQUIRED

    def test_unknown_category_fallback(self) -> None:
        classification = FailureClassification(
            code="something_weird",
            category="unknown",
            disposition="repair_required",
            summary="unknown error",
        )
        recovery = recommended_recovery(classification)
        assert recovery["action"] == ACTION_REPAIR_REQUIRED


# ---------------------------------------------------------------------------
# Tests: compute_backoff_delay
# ---------------------------------------------------------------------------

class TestComputeBackoffDelay:
    def test_first_attempt(self) -> None:
        assert compute_backoff_delay(1, base_delay_sec=2.0) == pytest.approx(2.0)

    def test_second_attempt(self) -> None:
        assert compute_backoff_delay(2, base_delay_sec=2.0) == pytest.approx(4.0)

    def test_capped_at_max(self) -> None:
        assert compute_backoff_delay(10, base_delay_sec=2.0, max_delay_sec=30.0) == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Tests: monitor._aggregate_failure_categories
# ---------------------------------------------------------------------------

class TestAggregateFailureCategories:
    def test_empty_events(self) -> None:
        from app.monitor import _aggregate_failure_categories

        result = _aggregate_failure_categories({})
        assert result == {}

    def test_counts_by_category(self) -> None:
        from app.monitor import _aggregate_failure_categories

        failure_events = {
            "lint": [
                {"classification": {"category": "transient", "code": "rate_limited"}},
                {"classification": {"category": "transient", "code": "timeout"}},
                {"classification": {"category": "automated_checks", "code": "test_failed"}},
            ],
            "build": [
                {"classification": {"category": "automated_checks", "code": "build_failed"}},
            ],
        }
        result = _aggregate_failure_categories(failure_events)
        assert result["transient"] == 2
        assert result["automated_checks"] == 2

    def test_skips_events_without_classification(self) -> None:
        from app.monitor import _aggregate_failure_categories

        failure_events = {
            "lint": [
                {"classification": {"category": "transient"}},
                {"no_classification": True},
                {"classification": "not_a_dict"},
            ],
        }
        result = _aggregate_failure_categories(failure_events)
        assert result == {"transient": 1}
