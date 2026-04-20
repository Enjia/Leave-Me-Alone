from __future__ import annotations

import logging

from agents.agent_bundle import AgentBundle
from .codex_agent_factory import create_codex_agent
from .opencode_a2a_adapter import OpenCodeA2AAdapter, build_opencode_a2a_config
from ports.workspace import WorkspacePort
from app.runtime_config import RuntimeConfig


logger = logging.getLogger(__name__)


def create_agent_bundle_from_runtime(cfg: RuntimeConfig, manager: WorkspacePort) -> AgentBundle:
    if cfg.provider != "codex":
        raise ValueError(
            "leave-me-alone is codex-only. "
            "Use --provider codex and do not route stage execution through opencode."
        )

    judge_workspace = cfg.runtime_dir / "judge_workspace"
    judge_workspace.mkdir(parents=True, exist_ok=True)
    verifier_workspace = cfg.runtime_dir / "verifier_workspace"
    verifier_workspace.mkdir(parents=True, exist_ok=True)
    add_dir_alias_root = cfg.runtime_dir / "codex_add_dir_aliases"
    add_dir_alias_root.mkdir(parents=True, exist_ok=True)

    worker_workspace = manager.prepare_worker_workspace("worker")
    judge_b_workspace = cfg.runtime_dir / "judge_b_workspace"
    judge_b_workspace.mkdir(parents=True, exist_ok=True)

    judge = create_codex_agent(
        role="judge",
        goal="Set stage gates and perform independent code review as primary judge",
        backstory="Strict staff engineer responsible for release gate quality and independent code review.",
        model=cfg.model,
        workspace=judge_workspace,
        sandbox_mode=cfg.sandbox_mode,
        add_dirs=[cfg.target_repo, worker_workspace],
        add_dir_alias_root=add_dir_alias_root,
    )
    judge_b = create_codex_agent(
        role="judge_b",
        goal="Perform independent code review as secondary judge for cross-validation",
        backstory="Independent senior engineer providing a second opinion on code quality and correctness.",
        model=cfg.model,
        workspace=judge_b_workspace,
        sandbox_mode=cfg.sandbox_mode,
        add_dirs=[cfg.target_repo, worker_workspace],
        add_dir_alias_root=add_dir_alias_root,
    )
    verifier = create_codex_agent(
        role="verifier",
        goal="Audit stage criteria and evidence without making the final gate decision",
        backstory="Independent verification engineer focused on criterion-by-criterion contract auditing.",
        model=cfg.model,
        workspace=verifier_workspace,
        sandbox_mode=cfg.sandbox_mode,
        add_dirs=[cfg.target_repo, worker_workspace],
        add_dir_alias_root=add_dir_alias_root,
    )
    worker = create_codex_agent(
        role="worker",
        goal="Implement stage goals, fix accepted reports, and self-repair based on review feedback",
        backstory="Independent coding worker focused on robust implementation and self-driven quality.",
        model=cfg.model,
        workspace=worker_workspace,
        sandbox_mode=cfg.sandbox_mode,
        add_dirs=[],
        add_dir_alias_root=add_dir_alias_root,
    )

    a2a_adapters: list[OpenCodeA2AAdapter] | None = None
    if cfg.enable_a2a:
        a2a_adapters = []
        for agent_instance in (judge, judge_b, verifier, worker):
            a2a_config = build_opencode_a2a_config(agent_instance.role, cfg)
            if a2a_config is not None:
                adapter = OpenCodeA2AAdapter(agent_instance, a2a_config)
                agent_instance.attach_a2a(adapter)
                a2a_adapters.append(adapter)
        if not a2a_adapters:
            logger.warning(
                "--enable-a2a is set but no A2A adapters were created. "
                "Check --a2a-endpoints configuration."
            )
            a2a_adapters = None

    return AgentBundle(
        judge=judge,
        judge_b=judge_b,
        verifier=verifier,
        worker=worker,
        worker_workspace=worker_workspace,
        judge_b_workspace=judge_b_workspace,
        a2a_adapters=a2a_adapters,
    )
