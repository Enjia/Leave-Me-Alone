from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import shlex
import sys

from agents.agents import create_agent_bundle
from config.layered_policy import load_layered_policy
from engine.flow import BlockingDecisionRequired, build_flow
from .monitor import serve_monitor, write_monitor_html
from core.models import RunSummary, StageResult
from app.runtime_config import (
    RuntimeConfig,
    load_stage_artifacts,
    parse_a2a_endpoints,
    parse_stage_specs,
)
from core.workspace_manager import WorkspaceManager


logger = logging.getLogger(__name__)


def build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Multi-Codex stage-based code review orchestrator"
    )
    parser.add_argument("--target-repo", required=True, help="Absolute path of target repository")
    parser.add_argument("--stages-file", required=True, help="JSON file containing stage specs")
    parser.add_argument(
        "--runtime-dir",
        default=".runtime",
        help="Runtime directory for workspaces and summary output",
    )
    parser.add_argument(
        "--seed-artifacts-dir",
        default="",
        help="Optional artifacts directory from a previous runtime to seed required_inputs",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume from previous runtime state under --runtime-dir. "
            "Auto-loads <runtime-dir>/artifacts as seed artifacts when available, "
            "and skips stages that were already passed in the last review-summary.json."
        ),
    )
    parser.add_argument(
        "--output-file",
        default="review-summary.json",
        help="Output JSON file name under runtime dir",
    )
    parser.add_argument("--model", default="gpt-5.3-codex", help="Model name (format depends on provider)")
    parser.add_argument(
        "--provider",
        default="codex",
        choices=["codex"],
        help="Agent provider. v1 stage execution is codex-only.",
    )
    parser.add_argument(
        "--sandbox-mode",
        default="workspace-write",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Codex CLI sandbox mode (only used with --provider codex)",
    )
    parser.add_argument(
        "--max-round-per-stage",
        type=int,
        default=2,
        help="Maximum rounds per stage",
    )
    parser.add_argument(
        "--enable-a2a",
        action="store_true",
        help="Enable A2A configuration for judge/workers",
    )
    parser.add_argument(
        "--a2a-endpoints",
        default="",
        help=(
            "JSON object for role->base_url map, "
            "e.g. '{\"judge\":\"http://127.0.0.1:9100\",\"worker_a\":\"http://127.0.0.1:9101\",\"worker_b\":\"http://127.0.0.1:9102\"}'"
        ),
    )
    parser.add_argument(
        "--opencode-extra-args",
        type=str,
        default="",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--remote-host",
        type=str,
        default="",
        help="SSH host for primary remote command execution (e.g. '10.0.0.1' or 'user@host')",
    )
    parser.add_argument(
        "--remote-workdir",
        type=str,
        default="",
        help="Working directory on primary remote host (e.g. '/workspace/project')",
    )
    parser.add_argument(
        "--remote-host-secondary",
        type=str,
        dest="remote_host_secondary",
        default="",
        help="SSH host for secondary remote command execution",
    )
    parser.add_argument(
        "--remote-workdir-secondary",
        type=str,
        dest="remote_workdir_secondary",
        default="",
        help="Working directory on secondary remote host (defaults to --remote-workdir)",
    )
    parser.add_argument(
        "--split-worker-remote-endpoints",
        action="store_true",
        dest="split_worker_remote_endpoints",
        help=(
            "Route worker_a remote checks to --remote-host and worker_b checks to "
            "--remote-host-secondary for remote_primary stages. "
            "Use this to avoid compile contention when both workers run heavy gates."
        ),
    )
    parser.add_argument(
        "--remote-host-node1",
        type=str,
        dest="remote_host_secondary",
        default="",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--remote-workdir-node1",
        type=str,
        dest="remote_workdir_secondary",
        default="",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--split-worker-remote-hosts",
        action="store_true",
        dest="split_worker_remote_endpoints",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--auto-approve-decisions",
        type=str,
        default="",
        help=(
            "Comma-separated list of blocking decision names to auto-approve. "
            "Use 'all' to approve all decisions without prompting. "
            "Example: --auto-approve-decisions 'choose_transport_backend,accept_p0_capability_conclusion'"
        ),
    )
    parser.add_argument(
        "--owner-worker",
        type=str,
        default="worker_a",
        choices=["worker_a", "worker_b"],
        help=(
            "Which worker workspace is promoted back to target repo when a stage passes. "
            "Default: worker_a"
        ),
    )
    parser.add_argument(
        "--triage-allow-empty-reject-rationale",
        action="store_true",
        help="Relax triage policy and allow reject decisions with empty rationale.",
    )
    parser.add_argument(
        "--triage-allow-fact-high-severity-reject",
        action="store_true",
        help="Relax triage policy and allow rejecting fact-grade S0/S1 without blocking audit.",
    )
    parser.add_argument(
        "--promotion-allow-failing-checks",
        action="store_true",
        help="Relax promotion policy and do not require all checks to pass before promote.",
    )
    parser.add_argument(
        "--promotion-allow-open-fact-high-severity",
        action="store_true",
        help="Relax promotion policy and allow open fact-grade S0/S1 reports before promote.",
    )
    parser.add_argument(
        "--promotion-allow-disputes",
        action="store_true",
        help="Relax promotion policy and allow disputed items before promote.",
    )
    parser.add_argument(
        "--drift-allow-suspicious-items",
        action="store_true",
        help="Relax drift policy and do not hard-block suspicious stage-gate items.",
    )
    parser.add_argument(
        "--drift-allow-extra-commands",
        action="store_true",
        help="Relax drift policy and allow judge-added commands outside StageSpec.",
    )
    parser.add_argument(
        "--warn-budget-usd",
        type=float,
        default=0.0,
        help="Emit a warning when estimated run cost reaches this USD threshold (0=disabled).",
    )
    parser.add_argument(
        "--hard-budget-usd",
        type=float,
        default=0.0,
        help="Block new stages when estimated run cost reaches this USD threshold (0=disabled).",
    )
    parser.add_argument(
        "--per-stage-budget-usd",
        type=float,
        default=0.0,
        help="Block the next stage if the previous stage exceeded this USD threshold (0=disabled).",
    )
    parser.add_argument(
        "--hard-budget-enforcement",
        type=str,
        default="stage_boundary",
        choices=["stage_boundary", "immediate_abort"],
        help=(
            "How hard_budget_usd is enforced. "
            "'stage_boundary' (default): only block new stages. "
            "'immediate_abort': also abort agent calls mid-stage."
        ),
    )
    parser.add_argument(
        "--run-budget-mode",
        type=str,
        default="run_only",
        choices=["run_only", "include_resume_history"],
        help=(
            "How the cost ledger is initialized. "
            "'run_only' (default): start fresh each run. "
            "'include_resume_history': restore historical records from previous runs."
        ),
    )
    parser.add_argument(
        "--context-budget-max-chars",
        type=int,
        default=80_000,
        help=(
            "Maximum total characters for layered context compression. "
            "Controls how much context is passed to agents per prompt (default: 80000)."
        ),
    )
    parser.add_argument(
        "--layered-policy-file",
        type=str,
        default="",
        help=(
            "Optional JSON file for global/project/profile/stage policy layering. "
            "Can override max_round_per_stage, context_budget_max_chars, and budget thresholds by stage."
        ),
    )
    return parser


def build_monitor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an HTML monitor page from a runtime directory"
    )
    parser.add_argument(
        "--runtime-dir",
        required=True,
        help="Runtime directory containing artifacts and review-summary.json",
    )
    parser.add_argument(
        "--output-html",
        default="",
        help="Optional output HTML path. Defaults to <runtime-dir>/monitor.html",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start a local HTTP monitor server with auto-refresh.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for --serve (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Bind port for --serve (default: 8765).",
    )
    parser.add_argument(
        "--refresh-sec",
        type=float,
        default=2.0,
        help="Browser auto-refresh interval in seconds for --serve.",
    )
    return parser


def main() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    argv = sys.argv[1:]
    if argv and argv[0] == "monitor":
        monitor_args = build_monitor_parser().parse_args(argv[1:])
        runtime_dir = Path(monitor_args.runtime_dir)
        if monitor_args.serve:
            logger.info(
                "monitor serving at http://%s:%s (runtime=%s, refresh=%.2fs)",
                monitor_args.host,
                monitor_args.port,
                runtime_dir,
                monitor_args.refresh_sec,
            )
            serve_monitor(
                runtime_dir=runtime_dir,
                host=monitor_args.host,
                port=monitor_args.port,
                refresh_sec=monitor_args.refresh_sec,
            )
            return
        output_path = write_monitor_html(
            runtime_dir=runtime_dir,
            output_html=Path(monitor_args.output_html) if monitor_args.output_html else None,
        )
        logger.info("monitor written to %s", output_path)
        return

    run_argv = argv[1:] if argv and argv[0] == "run" else argv
    parser = build_run_parser()
    args = parser.parse_args(run_argv)

    target_repo = Path(args.target_repo).expanduser().resolve()
    stages_file = Path(args.stages_file).expanduser().resolve()
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)

    cfg = RuntimeConfig(
        target_repo=target_repo,
        stages_file=stages_file,
        runtime_dir=runtime_dir,
        seed_artifacts_dir=(
            Path(args.seed_artifacts_dir).expanduser().resolve()
            if args.seed_artifacts_dir
            else None
        ),
        model=args.model,
        sandbox_mode=args.sandbox_mode,
        enable_a2a=bool(args.enable_a2a),
        a2a_endpoints=parse_a2a_endpoints(args.a2a_endpoints),
        max_round_per_stage=max(1, int(args.max_round_per_stage)),
        output_file=runtime_dir / args.output_file,
        provider=args.provider,
        opencode_extra_args=shlex.split(args.opencode_extra_args) if args.opencode_extra_args else [],
        remote_host=args.remote_host,
        remote_workdir=args.remote_workdir,
        remote_host_secondary=args.remote_host_secondary,
        remote_workdir_secondary=args.remote_workdir_secondary or args.remote_workdir,
        split_worker_remote_endpoints=bool(args.split_worker_remote_endpoints),
        auto_approve_decisions=(
            [item.strip() for item in args.auto_approve_decisions.split(",") if item.strip()]
            if args.auto_approve_decisions
            else []
        ),
        owner_worker=args.owner_worker,
        triage_require_reject_rationale=not args.triage_allow_empty_reject_rationale,
        triage_block_fact_high_severity_reject=not args.triage_allow_fact_high_severity_reject,
        promotion_require_all_checks=not args.promotion_allow_failing_checks,
        promotion_require_no_open_fact_high_severity=not args.promotion_allow_open_fact_high_severity,
        promotion_require_no_disputes=not args.promotion_allow_disputes,
        drift_fail_on_suspicious_items=not args.drift_allow_suspicious_items,
        drift_fail_on_extra_commands=not args.drift_allow_extra_commands,
        warn_budget_usd=args.warn_budget_usd,
        hard_budget_usd=args.hard_budget_usd,
        per_stage_budget_usd=args.per_stage_budget_usd,
        hard_budget_enforcement=args.hard_budget_enforcement,
        run_budget_mode=args.run_budget_mode,
        context_budget_max_chars=args.context_budget_max_chars,
        layered_policy_file=(
            Path(args.layered_policy_file).expanduser().resolve()
            if args.layered_policy_file
            else None
        ),
    )
    if cfg.layered_policy_file is not None:
        try:
            # Fail early with actionable CLI feedback when layered policy
            # schema/version/content is invalid.
            load_layered_policy(cfg.layered_policy_file, log_migration=False)
        except ValueError as exc:
            parser.error(f"--layered-policy-file invalid: {exc}")
    resume_stage_results: list[StageResult] = []
    resume_passed_stage_names: list[str] = []

    if args.provider != "codex":
        parser.error("leave-me-alone is codex-only for stage execution.")
    if args.opencode_extra_args:
        parser.error("--opencode-extra-args is not supported in codex-only v1.")

    stages = parse_stage_specs(cfg.stages_file)
    if args.resume:
        if cfg.seed_artifacts_dir is None:
            auto_seed = (cfg.runtime_dir / "artifacts").resolve()
            if auto_seed.exists():
                cfg.seed_artifacts_dir = auto_seed
                logger.info("resume mode: using seed artifacts from %s", cfg.seed_artifacts_dir)
            else:
                logger.warning(
                    "resume mode requested but default artifacts dir does not exist: %s",
                    auto_seed,
                )
        summary_path = cfg.runtime_dir / "review-summary.json"
        if summary_path.exists():
            try:
                previous_summary = RunSummary.model_validate(
                    json.loads(summary_path.read_text(encoding="utf-8"))
                )
                previous_target_repo = Path(previous_summary.target_repo).expanduser().resolve()
                if previous_target_repo != cfg.target_repo:
                    parser.error(
                        "resume runtime target repo mismatch: "
                        f"summary has {previous_target_repo}, "
                        f"but --target-repo is {cfg.target_repo}."
                    )
                stage_name_set = {stage.name for stage in stages}
                for stage_result in previous_summary.stage_results:
                    if not stage_result.passed:
                        break
                    if stage_result.stage_name in stage_name_set:
                        resume_stage_results.append(stage_result)
                        resume_passed_stage_names.append(stage_result.stage_name)
                if resume_passed_stage_names:
                    logger.info(
                        "resume mode: skipping previously passed stages: %s",
                        ", ".join(resume_passed_stage_names),
                    )
            except Exception:
                logger.exception("resume mode: failed to parse previous summary at %s", summary_path)
        else:
            logger.warning("resume mode: previous summary not found at %s", summary_path)

    manager = WorkspaceManager(target_repo=cfg.target_repo, runtime_dir=cfg.runtime_dir)
    agents = create_agent_bundle(cfg, manager)

    flow = build_flow(
        cfg=cfg,
        agents=agents,
        workspace_manager=manager,
        stages=stages,
    )
    flow.state.stage_artifacts = load_stage_artifacts(cfg.seed_artifacts_dir)
    flow.state.resumed_stage_results = list(resume_stage_results)
    flow.state.resume_passed_stage_names = list(resume_passed_stage_names)

    try:
        output = flow.kickoff()
    except BlockingDecisionRequired as exc:
        logger.error(
            "Blocking decision required at stage '%s': %s",
            exc.stage_name,
            ", ".join(exc.decisions),
        )
        logger.error(
            "Approve decisions via --auto-approve-decisions or the persisted decision request file, then rerun."
        )
        sys.exit(2)
    except KeyboardInterrupt:
        logger.info("Review interrupted by user.")
        sys.exit(130)
    except Exception:
        logger.exception("Review flow failed with an unexpected error")
        sys.exit(1)

    if isinstance(output, RunSummary):
        summary = output
    else:
        summary = RunSummary.model_validate(output)

    cfg.output_file.write_text(
        json.dumps(summary.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info("review completed. overall_passed=%s", summary.overall_passed)
    logger.info("summary written to %s", cfg.output_file)

    if not summary.overall_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
