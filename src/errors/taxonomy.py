"""Standard error taxonomy helpers.

This module provides functions that map raw error signals (exit codes, stderr
text, exception types) to a canonical ``FailureClassification``.  All
classification logic should eventually converge here so that the rest of the
system can rely on a single source of truth for error semantics.
"""
from __future__ import annotations

import json
import re
from typing import Sequence

from core.models import FailureClassification

# ---------------------------------------------------------------------------
# Transient-error detection (replaces the old string-only helper)
# ---------------------------------------------------------------------------

_TRANSIENT_KEYWORDS: tuple[str, ...] = (
    "429",
    "too many requests",
    "rate limit",
    "exceeded retry limit",
    "temporarily unavailable",
    "service unavailable",
    "gateway timeout",
    "connection reset",
    "timed out",
    "timeout",
    "network error",
    "econnrefused",
    "econnreset",
    "epipe",
    "broken pipe",
    "internal server error",
    "502",
    "503",
    "504",
)

_TRANSIENT_PATTERN = re.compile(
    "|".join(re.escape(keyword) for keyword in _TRANSIENT_KEYWORDS),
    re.IGNORECASE,
)


def classify_transient_error(raw_text: str) -> FailureClassification | None:
    """Detect a transient (retriable) error from raw CLI / API output.

    Returns a ``FailureClassification`` with ``category="transient"`` when a
    transient signal is found, or ``None`` otherwise.  This replaces the old
    ``detect_transient_cli_failure`` which returned a bare string.
    """
    if not raw_text:
        return None

    matched_text: str | None = None

    # 1. Try structured JSON lines first (e.g. OpenCode event stream).
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        for field in ("message", "error"):
            value = payload.get(field)
            if isinstance(value, dict):
                value = value.get("message", "")
            if isinstance(value, str) and _TRANSIENT_PATTERN.search(value):
                matched_text = value[:300]
                break
        if matched_text:
            break

    # 2. Fallback: scan the full raw text.
    if matched_text is None:
        match = _TRANSIENT_PATTERN.search(raw_text)
        if match:
            # Extract a short context window around the match.
            start = max(0, match.start() - 60)
            end = min(len(raw_text), match.end() + 120)
            matched_text = raw_text[start:end].strip()

    if matched_text is None:
        return None

    # Determine a more specific transient code.
    lowered = matched_text.lower()
    if "rate limit" in lowered or "429" in lowered or "too many requests" in lowered:
        code = "rate_limited"
    elif any(keyword in lowered for keyword in ("502", "503", "504", "service unavailable", "temporarily unavailable", "internal server error", "gateway timeout")):
        code = "service_unavailable"
    elif "timeout" in lowered or "timed out" in lowered:
        code = "request_timeout"
    elif any(keyword in lowered for keyword in ("connection reset", "econnreset", "econnrefused", "broken pipe", "epipe")):
        code = "connection_error"
    else:
        code = "transient_unknown"

    return FailureClassification(
        code=code,
        category="transient",
        disposition="retry_same_round",
        summary=matched_text[:240],
        owner="system",
        retryable=True,
        evidence=[matched_text[:500]],
    )


# ---------------------------------------------------------------------------
# Check-failure classification (enhanced version of the existing helper)
# ---------------------------------------------------------------------------

def classify_check_failure(
    *,
    worker: str,
    check_type: str,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> FailureClassification:
    """Classify a check command failure into the standard taxonomy.

    This is a pure-function replacement for the old ``classify_check_failure``
    in ``policy/runtime_artifacts.py`` which required a ``flow`` object.
    """
    evidence = _build_evidence(command, exit_code, stdout, stderr)
    combined_text = f"{stdout}\n{stderr}".lower()

    # --- Input contract violation (harness rejected the command) ---
    if exit_code == -2:
        return FailureClassification(
            code="rejected_command",
            category="input_contract",
            disposition="blocked",
            summary="Command rejected by harness allow-list or argument policy.",
            owner="system",
            retryable=False,
            evidence=evidence,
        )

    # --- Transient / timeout ---
    if "timeout" in combined_text or "timed out" in combined_text:
        return FailureClassification(
            code="check_timeout",
            category="timeout",
            disposition="retry_next_round",
            summary="Command timed out before producing a successful result.",
            owner=worker,
            retryable=True,
            evidence=evidence,
        )

    transient = classify_transient_error(f"{stdout}\n{stderr}")
    if transient is not None:
        transient.owner = worker
        transient.evidence = evidence
        return transient

    # --- Harness / artifact contract / remote gate ---
    if check_type == "harness":
        is_remote = "remote" in command or "dev_env_remote" in command
        return FailureClassification(
            code="harness_gate_failed",
            category="remote_gate" if is_remote else "artifact_contract",
            disposition="repair_required",
            summary="Harness-level gate or artifact contract failed.",
            owner=worker,
            retryable=False,
            evidence=evidence,
        )

    # --- Standard automated check failure ---
    return FailureClassification(
        code=f"{check_type}_failed",
        category="automated_checks",
        disposition="repair_required",
        summary=f"{check_type} command failed and requires code or build repair.",
        owner=worker,
        retryable=False,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Policy-level classification helpers
# ---------------------------------------------------------------------------

def classify_budget_exhausted(
    *,
    stage_name: str = "",
    details: str = "",
) -> FailureClassification:
    """Create a classification for budget-exhaustion events."""
    return FailureClassification(
        code="budget_exhausted",
        category="policy",
        disposition="blocked",
        summary=details or "Budget hard limit exceeded.",
        owner="system",
        retryable=False,
        evidence=[f"stage={stage_name}", details] if stage_name else [details],
    )


def classify_human_decision_required(
    *,
    stage_name: str = "",
    decisions: Sequence[str] = (),
) -> FailureClassification:
    """Create a classification for blocking human-decision events."""
    return FailureClassification(
        code="human_decision_required",
        category="human_decision",
        disposition="blocked",
        summary=f"Stage {stage_name} requires human approval.",
        owner="system",
        retryable=False,
        evidence=list(decisions),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_evidence(
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    limit: int = 240,
) -> list[str]:
    return [
        item
        for item in (
            f"command={command}",
            f"exit_code={exit_code}",
            stdout.strip()[:limit] if stdout.strip() else "",
            stderr.strip()[:limit] if stderr.strip() else "",
        )
        if item
    ]
