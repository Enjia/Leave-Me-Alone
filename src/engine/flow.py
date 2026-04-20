from __future__ import annotations

import json
import logging
from pathlib import Path
import time
from typing import Any, TypeVar

from runtime.flow_lite import FlowLite as Flow, start
from pydantic import BaseModel

from agents.agent_bundle import AgentBundle
from adapters.structured_agents import (
    build_read_only_planner_agent,
    detect_transient_cli_failure,
    invoke_agent_structured,
    invoke_agent_structured_sync,
    invoke_cli_structured_agent,
)
from config.layered_policy import (
    LayeredPolicyConfig,
    load_layered_policy,
    resolve_stage_policy,
)
from core.models import BudgetPolicy, ContextBudgetConfig
from state.cost_ledger import CostLedger
from .flow_facade_mixins import FlowHarnessFacadeMixin
from .flow_dependencies import build_default_flow_dependencies
from core.models import (
    CheckSummaryArtifact,
    JudgeGateReview,
    OwnerTriageResult,
    PeerReviewResult,
    PlanDriftArtifact,
    PlanGateReview,
    ReportMemoryEntry,
    ReviewFlowState,
    RunSummary,
    SelfReviewResult, ContextSynthesis,
    StageContextPacket,
    StageExecutionPlan,
    StageGate,
    StageResult,
    StageSpec,
    WorkerDelivery,
    WorkerPlan,
)
from .orchestration import build_stage_dag_plan
from orchestrator.run_loop import run_review_inner
from orchestrator.agent_round_calls import (
    invoke_plan_gate_review,
    invoke_worker_delivery_for_round,
    invoke_worker_plan_for_round,
)
from orchestrator.stage_loop import run_stage_round_loop
from orchestrator.stage_runner import finalize_failed_stage, run_single_stage
from persistence.service import PersistenceService
from policy.context_memory import (
    build_context_synthesis,
    build_stage_context_packet,
    merge_stage_report_memory,
    report_ids_for_stage,
    resolved_report_ids_for_stage,
    stable_report_id,
)
from policy.worker_actions import (
    build_plan_drift_artifact,
    build_runtime_nudges,
    build_worker_required_actions,
    check_summary_has_failures,
    classify_required_action_scope,
    latest_plan_drift_actions,
    latest_runtime_nudges_text,
)
from core.prompts import SourceRequirementsReport
from ports.agent import AgentPort
from ports.artifact_store import ArtifactStorePort
from ports.check_runner import CheckRunnerPort
from ports.workspace import WorkspacePort
from app.runtime_config import RuntimeConfig


logger = logging.getLogger(__name__)


TModel = TypeVar("TModel", bound=BaseModel)


class MultiCodexReviewFlow(FlowHarnessFacadeMixin, Flow[ReviewFlowState]):
    def __init__(
        self,
        cfg: RuntimeConfig,
        agents: AgentBundle,
        workspace_manager: WorkspacePort,
        workspace_port: WorkspacePort | None = None,
        check_runner: CheckRunnerPort | None = None,
        artifact_store: ArtifactStorePort | None = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.agents = agents
        self.workspace_manager = workspace_manager
        self.persistence_service = PersistenceService()
        default_dependencies = None
        if workspace_port is None or check_runner is None or artifact_store is None:
            default_dependencies = build_default_flow_dependencies(
                cfg=self.cfg,
                workspace_manager=workspace_manager,
                slugify=self._artifact_slug,
            )
        self.workspace_port: WorkspacePort = workspace_port or default_dependencies.workspace_port
        self.check_runner: CheckRunnerPort = check_runner or default_dependencies.check_runner
        self.artifact_store: ArtifactStorePort = artifact_store or default_dependencies.artifact_store

        if self.cfg.provider != "codex":
            raise RuntimeError(
                "leave-me-alone is codex-only and will not execute with opencode."
            )

        self.cost_ledger = CostLedger(
            budget_policy=BudgetPolicy(
                warn_budget_usd=self.cfg.warn_budget_usd,
                hard_budget_usd=self.cfg.hard_budget_usd,
                per_stage_budget_usd=self.cfg.per_stage_budget_usd,
                hard_budget_enforcement=self.cfg.hard_budget_enforcement,
                run_budget_mode=self.cfg.run_budget_mode,
            ),
        )

        if self.cfg.run_budget_mode == "include_resume_history":
            history_path = self.cfg.runtime_dir / "artifacts" / "cost_ledger.json"
            self.cost_ledger.restore_from_json(history_path)

        self._context_budget_config = ContextBudgetConfig(
            max_total_chars=self.cfg.context_budget_max_chars,
        )
        self._layered_policy: LayeredPolicyConfig | None = load_layered_policy(
            self.cfg.layered_policy_file
        )

        # Start A2A adapters if the bundle has any (opencode provider)
        self.agents.start_a2a()

    def _apply_stage_policy_overrides(self, stage: StageSpec) -> None:
        """Apply layered config overrides for a specific stage.

        Merge order: global -> project -> stage-profile -> stage overrides.
        """
        overrides = resolve_stage_policy(self._layered_policy, stage_name=stage.name)
        if overrides.max_round_per_stage is not None:
            self.state.max_round_per_stage = max(1, int(overrides.max_round_per_stage))
        if overrides.context_budget_max_chars is not None:
            value = max(5_000, int(overrides.context_budget_max_chars))
            self.cfg.context_budget_max_chars = value
            self._context_budget_config.max_total_chars = value

        budget = self.cost_ledger.budget_policy
        if overrides.warn_budget_usd is not None:
            budget.warn_budget_usd = float(overrides.warn_budget_usd)
        if overrides.hard_budget_usd is not None:
            budget.hard_budget_usd = float(overrides.hard_budget_usd)
        if overrides.per_stage_budget_usd is not None:
            budget.per_stage_budget_usd = float(overrides.per_stage_budget_usd)

    @start()
    async def run_review(self) -> RunSummary:
        try:
            return await self._run_review_inner()
        finally:
            self.agents.shutdown_a2a()

    async def _run_review_inner(self) -> RunSummary:
        return await run_review_inner(self)

    async def _run_single_stage(self, stage: StageSpec) -> StageResult:
        return await run_single_stage(self, stage)

    async def _run_single_stage_impl_after_preflight(
        self,
        *,
        stage: StageSpec,
        stage_plan: StageExecutionPlan,
        stage_deadline_monotonic: float,
        idle_timeout_sec: int,
        stage_gate: StageGate,
        max_round: int,
    ) -> StageResult:
        return await run_stage_round_loop(
            self,
            stage=stage,
            stage_plan=stage_plan,
            stage_deadline_monotonic=stage_deadline_monotonic,
            idle_timeout_sec=idle_timeout_sec,
            stage_gate=stage_gate,
            max_round=max_round,
        )

    def _build_stage_context_packet(
        self,
        *,
        stage: StageSpec,
        stage_gate: StageGate,
        stage_plan: StageExecutionPlan,
        round_index: int,
        judge_feedback: list[str],
        prev_check_summary_a: str,
        prev_check_summary_b: str,
        review_memory: list[ReportMemoryEntry],
    ) -> StageContextPacket:
        return build_stage_context_packet(
            self,
            stage=stage,
            stage_gate=stage_gate,
            stage_plan=stage_plan,
            round_index=round_index,
            judge_feedback=judge_feedback,
            prev_check_summary_a=prev_check_summary_a,
            prev_check_summary_b=prev_check_summary_b,
            review_memory=review_memory,
        )

    async def _invoke_worker_delivery_for_round(
        self,
        *,
        worker_name: str,
        agent: AgentPort,
        workspace: Any,
        stage: StageSpec,
        stage_gate: StageGate,
        round_index: int,
        judge_feedback: list[str],
        approved_plan: WorkerPlan,
        auto_check_summary: str,
        context_packet_json: str,
        worker_entry_packet_json: str,
        stage_deadline_monotonic: float,
    ) -> WorkerDelivery:
        return await invoke_worker_delivery_for_round(
            self,
            worker_name=worker_name,
            agent=agent,
            workspace=workspace,
            stage=stage,
            stage_gate=stage_gate,
            round_index=round_index,
            judge_feedback=judge_feedback,
            approved_plan=approved_plan,
            auto_check_summary=auto_check_summary,
            context_packet_json=context_packet_json,
            worker_entry_packet_json=worker_entry_packet_json,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )

    async def _invoke_worker_plan_for_round(
        self,
        *,
        worker_name: str,
        agent: AgentPort,
        workspace: Any,
        stage: StageSpec,
        round_index: int,
        judge_feedback: list[str],
        auto_check_summary: str,
        context_packet_json: str,
        worker_entry_packet_json: str,
        stage_deadline_monotonic: float,
    ) -> WorkerPlan:
        return await invoke_worker_plan_for_round(
            self,
            worker_name=worker_name,
            agent=agent,
            workspace=workspace,
            stage=stage,
            round_index=round_index,
            judge_feedback=judge_feedback,
            auto_check_summary=auto_check_summary,
            context_packet_json=context_packet_json,
            worker_entry_packet_json=worker_entry_packet_json,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )

    async def _invoke_plan_gate_review(
        self,
        *,
        stage: StageSpec,
        round_index: int,
        context_packet_json: str,
        worker_plan: WorkerPlan,
        stage_deadline_monotonic: float,
    ) -> PlanGateReview:
        return await invoke_plan_gate_review(
            self,
            stage=stage,
            round_index=round_index,
            context_packet_json=context_packet_json,
            worker_plan=worker_plan,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )

    @staticmethod
    def _build_plan_drift_artifact(
        *,
        stage_name: str,
        round_index: int,
        worker: str,
        plan: WorkerPlan,
        delivery: WorkerDelivery,
    ) -> PlanDriftArtifact:
        return build_plan_drift_artifact(
            stage_name=stage_name,
            round_index=round_index,
            worker=worker,
            plan=plan,
            delivery=delivery,
        )

    @staticmethod
    def _check_summary_has_failures(summary: str) -> bool:
        return check_summary_has_failures(summary)

    def _build_worker_required_actions(
        self,
        *,
        stage_name: str,
        worker_name: str,
        judge_feedback: list[str],
        auto_check_summary: str,
    ) -> list[str]:
        return build_worker_required_actions(
            self,
            stage_name=stage_name,
            worker_name=worker_name,
            judge_feedback=judge_feedback,
            auto_check_summary=auto_check_summary,
        )

    def _latest_plan_drift_actions(
        self,
        *,
        stage_name: str,
        worker_name: str,
    ) -> list[str]:
        return latest_plan_drift_actions(self, stage_name=stage_name, worker_name=worker_name)

    def _build_runtime_nudges(
        self,
        *,
        stage: StageSpec,
        round_index: int,
        convergence_signal: ConvergenceSignal | None = None,
        drift_a: PlanDriftArtifact | None = None,
        drift_b: PlanDriftArtifact | None = None,
        check_artifact_a: CheckSummaryArtifact | None = None,
        check_artifact_b: CheckSummaryArtifact | None = None,
    ) -> list[RuntimeNudgeArtifact]:
        return build_runtime_nudges(
            stage=stage,
            round_index=round_index,
            convergence_signal=convergence_signal,
            drift_a=drift_a,
            drift_b=drift_b,
            check_artifact_a=check_artifact_a,
            check_artifact_b=check_artifact_b,
        )

    def _latest_runtime_nudges_text(
        self,
        *,
        stage_name: str,
        target: str,
        limit: int = 6,
    ) -> str:
        return latest_runtime_nudges_text(self, stage_name=stage_name, target=target, limit=limit)

    @staticmethod
    def _classify_required_action_scope(action: str) -> tuple[str, str]:
        return classify_required_action_scope(action)

    @staticmethod
    def _truncate_text(value: str, limit: int = 600) -> str:
        cleaned = value.strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[:limit] + "...<TRUNCATED>..."

    def _build_context_synthesis(
        self,
        *,
        judge_feedback: list[str],
        prev_check_summary_a: str,
        prev_check_summary_b: str,
        review_memory: list[ReportMemoryEntry],
        round_index: int = 1,
    ) -> ContextSynthesis:
        synthesis, _compression_events = build_context_synthesis(
            self,
            judge_feedback=judge_feedback,
            prev_check_summary_a=prev_check_summary_a,
            prev_check_summary_b=prev_check_summary_b,
            review_memory=review_memory,
            round_index=round_index,
            context_budget=getattr(self, "_context_budget_config", None),
        )
        return synthesis

    @staticmethod
    def _report_ids_for_stage(
        stage_name: str,
        review_memory: list[ReportMemoryEntry],
    ) -> list[str]:
        del stage_name
        return report_ids_for_stage(review_memory)

    @staticmethod
    def _resolved_report_ids_for_stage(
        stage_name: str,
        review_memory: list[ReportMemoryEntry],
    ) -> list[str]:
        del stage_name
        return resolved_report_ids_for_stage(review_memory)

    @staticmethod
    def _stable_report_id(
        *,
        stage_name: str,
        target_worker: str,
        file_path: str,
        line: int | None,
        title: str,
    ) -> str:
        return stable_report_id(
            stage_name=stage_name,
            target_worker=target_worker,
            file_path=file_path,
            line=line,
            title=title,
        )

    def _merge_stage_report_memory(
        self,
        *,
        stage_name: str,
        round_index: int,
        review_memory: list[ReportMemoryEntry],
    ) -> list[ReportMemoryEntry]:
        del stage_name
        return merge_stage_report_memory(
            round_index=round_index,
            review_memory=review_memory,
        )

    async def _invoke_agent_structured(
        self,
        agent: AgentPort,
        prompt: str,
        model_cls: type[TModel],
        *,
        stage_name: str = "",
        stage_deadline_monotonic: float | None = None,
    ) -> TModel:
        return await invoke_agent_structured(
            self,
            agent,
            prompt,
            model_cls,
            stage_name=stage_name,
            stage_deadline_monotonic=stage_deadline_monotonic,
        )

    def _invoke_agent_structured_sync(
        self,
        agent: AgentPort,
        prompt: str,
        model_cls: type[TModel],
        timeout_override_sec: int | None = None,
    ) -> TModel:
        return invoke_agent_structured_sync(
            self,
            agent,
            prompt,
            model_cls,
            timeout_override_sec=timeout_override_sec,
        )

    @staticmethod
    def _build_read_only_planner_agent(agent: AgentPort) -> AgentPort:
        return build_read_only_planner_agent(agent)

    def _invoke_opencode_agent(
        self,
        agent: AgentPort,
        prompt: str,
        model_cls: type[TModel],
        timeout_override_sec: int | None = None,
    ) -> TModel:
        return invoke_cli_structured_agent(
            self,
            agent,
            prompt,
            model_cls,
            timeout_override_sec=timeout_override_sec,
        )

    @staticmethod
    def _detect_transient_cli_failure(raw_text: str) -> str | None:
        return detect_transient_cli_failure(raw_text)

    @staticmethod
    def _raise_blocking_decision_required(stage_name: str, decisions: list[str]) -> None:
        raise BlockingDecisionRequired(
            stage_name=stage_name,
            decisions=decisions,
        )


class BlockingDecisionRequired(Exception):
    """Raised when a stage has blocking decisions that need human approval."""

    def __init__(self, stage_name: str, decisions: list[str]) -> None:
        self.stage_name = stage_name
        self.decisions = decisions
        super().__init__(
            f"Stage '{stage_name}' requires human approval for: {decisions}"
        )


def build_flow(
    cfg: RuntimeConfig,
    agents: AgentBundle,
    workspace_manager: WorkspacePort,
    stages: list[StageSpec],
    workspace_port: WorkspacePort | None = None,
    check_runner: CheckRunnerPort | None = None,
    artifact_store: ArtifactStorePort | None = None,
) -> MultiCodexReviewFlow:
    flow = MultiCodexReviewFlow(
        cfg=cfg,
        agents=agents,
        workspace_manager=workspace_manager,
        workspace_port=workspace_port,
        check_runner=check_runner,
        artifact_store=artifact_store,
    )
    flow.state.target_repo = str(cfg.target_repo)
    flow.state.stages = stages
    flow.state.max_round_per_stage = cfg.max_round_per_stage
    flow.state.approved_decisions = list(cfg.auto_approve_decisions)
    return flow
