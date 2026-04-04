"""Adapter: convert plugin CheckResult list → legacy AutoCheckResult.

The orchestrator's downstream code (round_runner, runtime_artifacts,
format_check_summary) expects an ``AutoCheckResult`` with per-category
result lists and boolean roll-ups.  This module bridges the gap so that
``run_plugins_for_stage`` can return an ``AutoCheckResult`` directly.
"""
from __future__ import annotations

from core.models import AutoCheckResult, CheckCommandResult
from .plugin import CheckResult

# Plugin name → AutoCheckResult field mapping
_PLUGIN_TO_FIELD: dict[str, str] = {
    "lint": "lint_results",
    "test": "test_results",
    "perf": "perf_results",
    "harness": "harness_results",
    "remote_preflight": "harness_results",
}

_PLUGIN_TO_PASSED_FIELD: dict[str, str] = {
    "lint": "all_lint_passed",
    "test": "all_tests_passed",
    "perf": "all_perf_passed",
    "harness": "all_harness_passed",
    "remote_preflight": "all_harness_passed",
}


def _check_result_to_command_results(result: CheckResult) -> list[CheckCommandResult]:
    """Convert a single CheckResult's command dicts to CheckCommandResult list."""
    command_results: list[CheckCommandResult] = []
    for cmd_info in result.commands:
        command_results.append(CheckCommandResult(
            command=str(cmd_info.get("command", "")),
            exit_code=int(cmd_info.get("exit_code", -1)),
            stdout=str(cmd_info.get("stdout", "")),
            stderr=str(cmd_info.get("stderr", "")),
            passed=bool(cmd_info.get("passed", False)),
        ))

    # If the plugin returned no command details but did report pass/fail,
    # synthesize a single entry so downstream code has something to iterate.
    if not command_results and not result.skipped:
        command_results.append(CheckCommandResult(
            command=f"[{result.plugin_name}]",
            exit_code=0 if result.passed else 1,
            passed=result.passed,
        ))

    return command_results


def check_results_to_auto_check_result(
    results: list[CheckResult],
    *,
    worker: str,
    stage_name: str,
) -> AutoCheckResult:
    """Convert a list of plugin CheckResults to an AutoCheckResult.

    Each ``CheckResult`` is mapped to the appropriate category field
    (``test_results``, ``lint_results``, etc.) based on its
    ``plugin_name``.  Unknown plugin names are appended to
    ``harness_results`` as a safe default.

    The ``all_*_passed`` booleans are computed from the per-category
    results: a category passes if all its commands passed, or if no
    commands were run for that category.
    """
    test_results: list[CheckCommandResult] = []
    lint_results: list[CheckCommandResult] = []
    perf_results: list[CheckCommandResult] = []
    harness_results: list[CheckCommandResult] = []

    field_map: dict[str, list[CheckCommandResult]] = {
        "test_results": test_results,
        "lint_results": lint_results,
        "perf_results": perf_results,
        "harness_results": harness_results,
    }

    # Track per-category pass/fail (None = no results yet)
    category_passed: dict[str, bool | None] = {
        "all_tests_passed": None,
        "all_lint_passed": None,
        "all_perf_passed": None,
        "all_harness_passed": None,
    }

    for result in results:
        if result.skipped and result.passed:
            # Genuinely skipped (e.g. "no commands configured") — safe to ignore.
            continue

        if result.skipped and not result.passed:
            # Plugin was requested but not registered (fail-closed).
            # Treat as a harness failure so it surfaces in AutoCheckResult.
            target_field = "harness_results"
            passed_field = "all_harness_passed"
            harness_results.append(CheckCommandResult(
                command=f"[{result.plugin_name}]",
                exit_code=1,
                passed=False,
                stderr=result.skip_reason or f"Plugin '{result.plugin_name}' not registered.",
            ))
            current = category_passed[passed_field]
            category_passed[passed_field] = False if current is None else False
            continue

        target_field = _PLUGIN_TO_FIELD.get(result.plugin_name, "harness_results")
        passed_field = _PLUGIN_TO_PASSED_FIELD.get(result.plugin_name, "all_harness_passed")

        command_results = _check_result_to_command_results(result)
        field_map[target_field].extend(command_results)

        # Update category pass status
        current = category_passed[passed_field]
        if current is None:
            category_passed[passed_field] = result.passed
        else:
            category_passed[passed_field] = current and result.passed

    return AutoCheckResult(
        worker=worker,
        stage_name=stage_name,
        test_results=test_results,
        lint_results=lint_results,
        perf_results=perf_results,
        harness_results=harness_results,
        all_tests_passed=category_passed["all_tests_passed"] if category_passed["all_tests_passed"] is not None else True,
        all_lint_passed=category_passed["all_lint_passed"] if category_passed["all_lint_passed"] is not None else True,
        all_perf_passed=category_passed["all_perf_passed"] if category_passed["all_perf_passed"] is not None else True,
        all_harness_passed=category_passed["all_harness_passed"] if category_passed["all_harness_passed"] is not None else True,
    )
