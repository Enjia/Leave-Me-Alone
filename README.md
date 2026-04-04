# Leave Me Alone

A universal multi-agent code engineering toolkit with a self-contained orchestration runtime.

Default pipeline uses the following 7-step workflow:

1. Judge publishes stage objective and acceptance gate (tests/lint/perf/interface).
2. Worker A/B implement in isolated workspaces.
3. Each worker performs self-review and self-fix.
4. Workers cross-review peer code with strict bug report schema.
5. Code owner accepts/rejects each report and optionally patches.
6. Judge reviews high-severity + disputed items and does gate review.
7. Stage passes and moves on, or retries once (`max_round_per_stage=2` by default).

## Highlights

- Uses a lightweight self-contained `FlowLite` orchestrator (zero external runtime dependency).
- Uses three independent Codex-backed agents (judge/worker_a/worker_b) as the default profile.
- Isolated worker workspaces with git-aware diff collection.
- Structured outputs via Pydantic models for every review artifact.
- Optional A2A protocol integration.

## Install

```bash
cd leave-me-alone
uv venv
source .venv/bin/activate
uv pip install -e .
# Optional dev dependencies:
uv pip install -e '.[dev]'
```

## Requirements

- Codex CLI available in `PATH` (`codex --help`).
- A target git repository for best diff fidelity.

## Quick Start

```bash
leave-me-alone \
  --target-repo /abs/path/to/your/repo \
  --stages-file ./stages.sample.json \
  --runtime-dir ./.runtime \
  --model gpt-5.3-codex
```

Result JSON is written to `./.runtime/review-summary.json` by default.

Resume from an interrupted runtime (reuse prior artifacts + skip already passed stages):

```bash
leave-me-alone \
  --target-repo /abs/path/to/your/repo \
  --stages-file ./stages.sample.json \
  --runtime-dir ./.runtime \
  --resume
```

## Monitor

Generate a self-contained HTML status page from a runtime directory:

```bash
leave-me-alone monitor \
  --runtime-dir ./.runtime
```

This writes `./.runtime/monitor.html` by default.

Or start a real-time local monitor website directly (SSE live updates, no full-page refresh):

```bash
leave-me-alone monitor \
  --runtime-dir ./.runtime \
  --serve \
  --host 127.0.0.1 \
  --port 8765 \
  --refresh-sec 2
```

Then open `http://127.0.0.1:8765` in your browser.

The monitor now includes dedicated sections for:

- `triage` audit status
- `promotion` readiness status
- `stage-gate drift` status
- the active governance policy snapshot

## Harness Engineering Constraints

This orchestrator treats `StageSpec` as the immutable contract and the judge's
`StageGate` as a refinement layer, not a replacement. In practice:

- Local commands must be workspace-relative and must not reference remote workdirs.
- Remote stages fail fast if required nodes/workdirs are not configured.
- Sync failures block remote gate execution for the affected target.
- Stage pass is revoked if declared evidence artifacts are missing or violate their contracts.
- Judge fallback is fail-closed: if structured gate review fails, the stage does not pass.
- Stage source extraction is preflight-validated and persisted to `.runtime/artifacts/*_source_preflight.json`.
- Harness drift metrics are persisted to `.runtime/artifacts/harness_metrics.json`.
- Runtime monitor status is persisted to `.runtime/artifacts/runtime_status.json`.
- Per-stage dashboard snapshots are persisted to `.runtime/artifacts/*_dashboard.json`.
- Governance policy snapshot is persisted to `.runtime/artifacts/governance_policy.json`.
- Triage / promotion / drift governance artifacts are persisted under `.runtime/artifacts/*_triage_audit.json`, `*_promotion_readiness.json`, and `*_stage_gate_drift.json`.
- Persisted stage artifacts include `schema_version`, `objective_hash`, and `data_hash` for provenance checks.
- Generic smoke tests are treated as baseline viability checks only; stage-specific evidence remains mandatory.
- Remote workdirs are worker-isolated (`<remote-workdir>/worker_a` and `<remote-workdir>/worker_b`) to avoid rsync overwrite races.
- When a stage passes, one owner workspace (`--owner-worker`) is promoted back to target repo and propagated to the peer workspace.

## Optional A2A Mode

A2A is opt-in and uses the built-in A2A adapter configuration.

```bash
leave-me-alone \
  --target-repo /abs/path/to/your/repo \
  --stages-file ./stages.sample.json \
  --enable-a2a \
  --a2a-endpoints '{"judge":"http://127.0.0.1:9100","worker_a":"http://127.0.0.1:9101","worker_b":"http://127.0.0.1:9102"}'
```

When `--enable-a2a` is set but endpoints are unavailable, delegation falls back to local execution (`fail_fast=False`).

## Stages File Format

```json
[
  {
    "name": "stage-1-auth-core",
    "objective": "Implement auth middleware and token validation baseline.",
    "scope_hint": ["src/auth", "src/middleware"],
    "test_commands": ["pytest tests/auth"],
    "gate_commands_remote": [],
    "remote_gate_contracts": [],
    "execution_env": "local_only",
    "sync_strategy": "local_only",
    "expected_artifact_paths": ["docs/stage-1-report.json"],
    "artifact_contracts": [
      {
        "path": "docs/stage-1-report.json",
        "format": "json",
        "required_keys": ["summary", "validation"],
        "required_substrings": []
      }
    ],
    "harness_constraints": [
      "Commands must be deterministic and rerunnable.",
      "Evidence artifacts are mandatory for stage pass."
    ]
  }
]
```

`remote_gate_contracts` can be used to enforce deterministic remote evidence checks:

```json
{
  "command": "python3 scripts/remote_gate.py",
  "required_exit_code": 0,
  "required_substrings": ["PASSED"],
  "required_regexes": ["duration_ms=\\d+"],
  "required_json_keys": ["status", "metrics"]
}
```

## Notes

- This project orchestrates workers but does not auto-merge worker branches.
- Judge gate strictly focuses on high severity and disputed findings.
- `max_round_per_stage` defaults to 2 to avoid infinite loops.

## Long-Run Recommendations (Remote)

- Use `--auto-approve-decisions` for stages with `blocking_decisions`; otherwise the flow exits with code `2` and writes a decision request JSON under `.runtime/artifacts`.
- For any stage with `execution_env=node0_and_node1`, always pass both `--remote-host-node1` and `--remote-workdir-node1`.
- Keep a deterministic owner policy (`--owner-worker worker_a|worker_b`) so stage artifacts land in `target_repo/docs/*` consistently.
- Timeout policy defaults:
  - `MULTI_CODEX_STAGE_TIMEOUT_SEC=10800` (3h hard cap per stage)
  - `MULTI_CODEX_AGENT_TIMEOUT_SEC=10800` (single invocation hard cap)
  - `MULTI_CODEX_AGENT_IDLE_TIMEOUT_SEC=600` (abnormal timeout; triggers only when there is no stdout/stderr output **and** no workspace file mtime change)

### Runtime Timeout / Heartbeat / Alert Tuning

You can copy `.env.example` to `.env` and override by environment export before running CLI.

Budget-driven timeout caps:

- `MULTI_CODEX_PHASE_TIMEOUT_FAST_ROUND_SEC=900`
- `MULTI_CODEX_PHASE_TIMEOUT_PRE_PROMOTION_SEC=1800`
- `MULTI_CODEX_PHASE_TIMEOUT_FULL_REGRESSION_SEC=3600`
- `MULTI_CODEX_PHASE_TIMEOUT_DEFAULT_SEC=1800`

Remote heartbeat and recovery windows:

- `MULTI_CODEX_REMOTE_HEARTBEAT_INTERVAL_SEC=5`
- `MULTI_CODEX_REMOTE_HEARTBEAT_STALE_TTL_SEC=30`
- `MULTI_CODEX_TIMEOUT_RECOVERY_DEDUPE_IDS_LIMIT=500`
- `MULTI_CODEX_TIMEOUT_RECOVERY_RECENT_EVENTS_LIMIT=50`

Monitor alert thresholds for timeout recovery:

- `MULTI_CODEX_TIMEOUT_RECOVERY_ALERT_MIN_ATTEMPTS=3`
- `MULTI_CODEX_TIMEOUT_RECOVERY_ALERT_FAILURE_RATE_PCT=50`
- `MULTI_CODEX_TIMEOUT_RECOVERY_ALERT_CONSECUTIVE_FAILURES=2`
- `MULTI_CODEX_TIMEOUT_RECOVERY_ALERT_STALE_RECYCLED=10`

Fail-closed escalation for repeated timeout recovery failures:

- `MULTI_CODEX_TIMEOUT_RECOVERY_BLOCK_CONSECUTIVE_FAILURES=2`

## Governance Policy Flags

Defaults are strict. You can relax specific governance rules when needed:

- `--triage-allow-empty-reject-rationale`
- `--triage-allow-fact-high-severity-reject`
- `--promotion-allow-failing-checks`
- `--promotion-allow-open-fact-high-severity`
- `--promotion-allow-disputes`
- `--drift-allow-suspicious-items`
- `--drift-allow-extra-commands`
