from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


BudgetEnforcement = Literal["stage_boundary", "immediate_abort"]
RunBudgetMode = Literal["run_only", "include_resume_history"]
Severity = Literal["S0", "S1", "S2", "S3"]
OwnerDecisionAction = Literal["accept_fix", "reject"]
EvidenceCertainty = Literal["fact", "inference", "to_verify"]
StageRiskLevel = Literal["low", "medium", "high", "critical"]
FailureCategory = Literal[
    "transient",
    "spec_gap",
    "structured_output",
    "automated_checks",
    "remote_gate",
    "artifact_contract",
    "planner",
    "workspace_state",
    "promotion",
    "timeout",
    "input_contract",
    "human_decision",
    "policy",
    "unknown",
]
FailureDisposition = Literal[
    "retry_same_round",
    "retry_next_round",
    "repair_required",
    "blocked",
    "terminal",
    "degrade",
]
CheckPhase = Literal["post_impl", "post_self_review", "post_triage", "pre_promotion", "full_regression"]
HandoffTrigger = Literal[
    "round_start",
    "round_end",
    "judge_retry",
    "stage_pass",
    "stage_fail",
    "spec_gap",
    "timeout_recovery",
]
ConvergenceRecommendation = Literal["continue", "retry", "stop_and_replan"]
GateTier = Literal["fast_round", "pre_promotion", "full_regression"]
PlanNodeKind = Literal[
    "stage_gate",
    "planner",
    "plan_gate",
    "implementation",
    "auto_checks",
    "self_review",
    "peer_review",
    "owner_triage",
    "verifier",
    "judge_gate",
    "promotion",
]


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UsageRecord(StrictBaseModel):
    """Token usage from a single agent invocation."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    agent_role: str = ""
    stage_name: str = ""
    latency_sec: float = 0.0
    timestamp_iso: str = ""


class BudgetPolicy(StrictBaseModel):
    """Cost budget thresholds for a run."""

    warn_budget_usd: float = 0.0
    hard_budget_usd: float = 0.0
    per_stage_budget_usd: float = 0.0
    hard_budget_enforcement: BudgetEnforcement = "stage_boundary"
    run_budget_mode: RunBudgetMode = "run_only"


class CostSnapshot(StrictBaseModel):
    """Aggregated cost snapshot for monitor display."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    by_stage: dict[str, "StageCostEntry"] = Field(default_factory=dict)
    by_worker: dict[str, "WorkerCostEntry"] = Field(default_factory=dict)
    by_model: dict[str, "ModelCostEntry"] = Field(default_factory=dict)
    invocation_count: int = 0
    budget_warn_triggered: bool = False
    budget_hard_triggered: bool = False


class StageCostEntry(StrictBaseModel):
    """Per-stage cost breakdown."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    invocation_count: int = 0


class ModelCostEntry(StrictBaseModel):
    """Per-model cost breakdown."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    invocation_count: int = 0


class WorkerCostEntry(StrictBaseModel):
    """Per-worker/role cost breakdown."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    invocation_count: int = 0


class ArtifactContract(StrictBaseModel):
    path: str
    format: Literal["json", "text"] = "json"
    required_keys: list[str] = Field(default_factory=list)
    required_json_nonempty: list[str] = Field(default_factory=list)
    required_json_values: dict[str, Any] = Field(default_factory=dict)
    required_substrings: list[str] = Field(default_factory=list)


class RemoteGateContract(StrictBaseModel):
    command: str
    tier: GateTier = "fast_round"
    required_exit_code: int = 0
    required_substrings: list[str] = Field(default_factory=list)
    required_regexes: list[str] = Field(default_factory=list)
    required_json_keys: list[str] = Field(default_factory=list)


class TieredGateCommand(StrictBaseModel):
    command: str
    tier: GateTier = "fast_round"


class BuildStrategyProfile(StrictBaseModel):
    strategy_id: str = ""
    # auto: infer from commands/patterns, always: force serialize, never: force parallel.
    serialize_remote_checks: Literal["auto", "always", "never"] = "auto"
    serialize_command_patterns: list[str] = Field(default_factory=list)
    preserve_remote_cache_default: bool = False
    preserve_remote_cache_tiers: list[GateTier] = Field(default_factory=list)
    remote_cache_paths: list[str] = Field(default_factory=list)


class FailureClassification(StrictBaseModel):
    code: str
    category: FailureCategory = "unknown"
    disposition: FailureDisposition = "repair_required"
    summary: str
    owner: Literal["system", "judge", "worker_a", "worker_b", "shared"] = "system"
    retryable: bool = False
    evidence: list[str] = Field(default_factory=list)


class CheckFailure(StrictBaseModel):
    check_type: Literal["test", "lint", "perf", "harness"]
    command: str
    exit_code: int
    summary: str
    classification: FailureClassification


class CheckSummaryArtifact(StrictBaseModel):
    stage_name: str
    round_index: int
    worker: str
    phase: CheckPhase
    all_passed: bool = False
    passed_commands: list[str] = Field(default_factory=list)
    failed_checks: list[CheckFailure] = Field(default_factory=list)
    normalized_error_class: str = ""
    likely_subsystem: str = ""
    representative_log_lines: list[str] = Field(default_factory=list)
    environment_metadata: dict[str, str] = Field(default_factory=dict)
    raw_summary: str = ""
    signal_hash: str = ""


class TaskHandoffPacket(StrictBaseModel):
    stage_name: str
    round_index: int
    worker: str
    trigger: HandoffTrigger = "round_end"
    objective: str
    completed_facts: list[str] = Field(default_factory=list)
    current_status: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    evidence_artifacts: list[str] = Field(default_factory=list)
    open_blockers: list[str] = Field(default_factory=list)
    immutable_requirements: list[str] = Field(default_factory=list)
    related_report_ids: list[str] = Field(default_factory=list)


class StageProgressLedger(StrictBaseModel):
    stage_name: str
    stage_id: str = ""
    round_index: int = 0
    status: Literal["pending", "running", "blocked", "failed", "passed"] = "pending"
    active_subgoal_id: str = ""
    active_subgoal_title: str = ""
    passed_gates: list[str] = Field(default_factory=list)
    current_blocker: str = ""
    current_blocker_category: str = ""
    frozen_non_goals: list[str] = Field(default_factory=list)
    latest_artifacts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class WorkerEntryPacket(StrictBaseModel):
    stage_name: str
    round_index: int
    worker: str
    objective: str
    active_subgoal_id: str = ""
    active_subgoal_title: str = ""
    active_subgoal_description: str = ""
    passed_gates: list[str] = Field(default_factory=list)
    current_blocker: str = ""
    current_blocker_category: str = ""
    allowed_write_scope: list[str] = Field(default_factory=list)
    allowed_read_refs: list[str] = Field(default_factory=list)
    immutable_requirements: list[str] = Field(default_factory=list)
    frozen_non_goals: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class BaselineStatusArtifact(StrictBaseModel):
    stage_name: str
    round_index: int = 0
    worker: Literal["worker_a", "worker_b", "shared"] = "shared"
    passed: bool = False
    checks: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)


class ActiveConstraintsArtifact(StrictBaseModel):
    stage_name: str
    round_index: int = 0
    active_subgoal_id: str = ""
    active_subgoal_title: str = ""
    immutable_requirements: list[str] = Field(default_factory=list)
    frozen_non_goals: list[str] = Field(default_factory=list)
    allowed_write_scope: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class InitializerReportArtifact(StrictBaseModel):
    stage_name: str
    round_index: int = 0
    objective: str
    source_file: str = ""
    seed_artifacts: list[str] = Field(default_factory=list)
    initialization_steps: list[str] = Field(default_factory=list)
    environment_ready: bool = False
    notes: list[str] = Field(default_factory=list)


class CleanStateArtifact(StrictBaseModel):
    stage_name: str
    round_index: int
    worker: Literal["worker_a", "worker_b", "shared"] = "shared"
    passed: bool = False
    unresolved_changes: list[str] = Field(default_factory=list)
    undocumented_blockers: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class StageSubgoal(StrictBaseModel):
    subgoal_id: str
    title: str
    description: str
    allowed_files: list[str] = Field(default_factory=list)
    verification_targets: list[str] = Field(default_factory=list)
    status: Literal["pending", "active", "blocked", "verified"] = "pending"


class FeatureChecklistItem(StrictBaseModel):
    item_id: str
    description: str
    category: str = "functional"
    verification_hint: str = ""
    status: Literal["pending", "in_progress", "verified", "blocked"] = "pending"
    notes: list[str] = Field(default_factory=list)


class FeatureChecklistArtifact(StrictBaseModel):
    stage_name: str
    stage_id: str = ""
    round_index: int = 0
    items: list[FeatureChecklistItem] = Field(default_factory=list)


class RepoProgressNote(StrictBaseModel):
    stage_name: str
    stage_id: str = ""
    status: str
    round_index: int = 0
    active_subgoal: str = ""
    current_blocker: str = ""
    verified_facts: list[str] = Field(default_factory=list)
    repeated_failure_points: list[str] = Field(default_factory=list)
    stable_workarounds: list[str] = Field(default_factory=list)


class ConvergenceSignal(StrictBaseModel):
    stage_name: str
    round_index: int
    no_progress_detected: bool = False
    repeated_failure_signature: bool = False
    recommended_action: ConvergenceRecommendation = "continue"
    reasons: list[str] = Field(default_factory=list)


class RuntimeNudgeArtifact(StrictBaseModel):
    stage_name: str
    round_index: int
    target: Literal["worker_a", "worker_b", "judge", "shared"] = "shared"
    category: Literal[
        "no_progress",
        "plan_drift",
        "artifact_missing",
        "structured_retry_risk",
        "phase_control",
        "todo_enforcement",
        "error_recovery",
        "behavioral",
        "json_retry",
    ]
    message: str
    severity: Literal["info", "warning", "blocking"] = "warning"
    recommended_action: str = ""
    evidence: list[str] = Field(default_factory=list)
    dedupe_key: str = ""


class StageSpecSnapshotArtifact(StrictBaseModel):
    stage_name: str
    stage_id: str
    objective: str
    risk_level: StageRiskLevel = "medium"
    depends_on_stages: list[str] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    trust_sources: list[str] = Field(default_factory=list)
    trust_priority: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class FailureEventArtifact(StrictBaseModel):
    stage_name: str
    round_index: int = 0
    source: str
    classification: FailureClassification
    details: dict[str, Any] = Field(default_factory=dict)


class StageGateDriftArtifact(StrictBaseModel):
    stage_name: str
    round_index: int = 0
    extra_test_commands: list[str] = Field(default_factory=list)
    extra_lint_commands: list[str] = Field(default_factory=list)
    extra_perf_checks: list[str] = Field(default_factory=list)
    extra_interface_contracts: list[str] = Field(default_factory=list)
    extra_pass_criteria: list[str] = Field(default_factory=list)
    suspicious_items: list[str] = Field(default_factory=list)
    policy_blockers: list[str] = Field(default_factory=list)
    policy_warnings: list[str] = Field(default_factory=list)


class TriageAuditArtifact(StrictBaseModel):
    stage_name: str
    round_index: int
    invalid_rejections: list[str] = Field(default_factory=list)
    fact_high_severity_rejections: list[str] = Field(default_factory=list)
    unresolved_owner_items: list[str] = Field(default_factory=list)
    policy_blockers: list[str] = Field(default_factory=list)
    passed: bool = True


class PromotionReadinessArtifact(StrictBaseModel):
    stage_name: str
    round_index: int
    owner_worker: Literal["worker_a", "worker_b"]
    all_checks_passed: bool = False
    final_gate_passed: bool = False
    unresolved_blockers: list[str] = Field(default_factory=list)
    blocking_report_ids: list[str] = Field(default_factory=list)
    disputed_items: list[str] = Field(default_factory=list)
    policy_rules: list[str] = Field(default_factory=list)
    ready: bool = False


class RuntimeStatusSnapshot(StrictBaseModel):
    target_repo: str
    current_stage: str = ""
    current_round: int = 0
    phase: str = ""
    overall_state: Literal["running", "blocked", "failed", "passed"] = "running"
    worker_states: dict[str, str] = Field(default_factory=dict)
    judge_state: str = ""
    latest_artifacts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    sli_metrics: dict[str, float] = Field(default_factory=dict)
    sli_alerts: list[str] = Field(default_factory=list)
    cost_snapshot: CostSnapshot | None = None


class GovernancePolicySnapshot(StrictBaseModel):
    triage_require_reject_rationale: bool = True
    triage_block_fact_high_severity_reject: bool = True
    promotion_require_all_checks: bool = True
    promotion_require_no_open_fact_high_severity: bool = True
    promotion_require_no_disputes: bool = True
    drift_fail_on_suspicious_items: bool = True
    drift_fail_on_extra_commands: bool = True


class HarnessRoleSpec(StrictBaseModel):
    role: str
    responsibility: str


class HarnessRetryPolicy(StrictBaseModel):
    max_round_per_stage: int = 2
    max_no_progress_rounds: int = 2
    max_repeated_failure_rounds: int = 2


class HarnessStopPolicy(StrictBaseModel):
    enable_convergence_signals: bool = True
    stop_on_spec_gap: bool = True
    promotion_require_all_checks: bool = True
    promotion_require_no_open_fact_high_severity: bool = True
    promotion_require_no_disputes: bool = True
    drift_fail_on_suspicious_items: bool = True
    drift_fail_on_extra_commands: bool = True


class HarnessSpecSnapshot(StrictBaseModel):
    provider: str
    owner_worker: Literal["worker_a", "worker_b"]
    topology: str
    roles: list[HarnessRoleSpec] = Field(default_factory=list)
    validation_gates: list[str] = Field(default_factory=list)
    retry_policy: HarnessRetryPolicy = Field(default_factory=HarnessRetryPolicy)
    stop_policy: HarnessStopPolicy = Field(default_factory=HarnessStopPolicy)
    state_semantics: list[str] = Field(default_factory=list)
    adapter_policy: list[str] = Field(default_factory=list)
    failure_taxonomy: list[str] = Field(default_factory=list)
    stage_profiles: list["HarnessStageProfile"] = Field(default_factory=list)


class HarnessStageProfile(StrictBaseModel):
    profile_id: str
    stage_types: list[str] = Field(default_factory=list)
    execution_envs: list[str] = Field(default_factory=list)
    required_artifacts: list[str] = Field(default_factory=list)
    preferred_validation_gates: list[str] = Field(default_factory=list)
    retry_bias: Literal["conservative", "balanced", "aggressive"] = "balanced"
    stop_conditions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class StageDashboardArtifact(StrictBaseModel):
    stage_name: str
    stage_id: str = ""
    objective: str
    status: Literal["pending", "running", "blocked", "failed", "passed"] = "pending"
    current_round: int = 0
    max_rounds: int = 0
    worker_states: dict[str, str] = Field(default_factory=dict)
    judge_state: str = ""
    latest_plan_overview: dict[str, str] = Field(default_factory=dict)
    latest_check_overview: dict[str, str] = Field(default_factory=dict)
    latest_nudges: list[str] = Field(default_factory=list)
    latest_convergence_action: str = ""
    open_fact_high_severity_reports: list[str] = Field(default_factory=list)
    unresolved_actions: list[str] = Field(default_factory=list)
    latest_failure_codes: list[str] = Field(default_factory=list)
    latest_artifacts: list[str] = Field(default_factory=list)
    sli_metrics: dict[str, float] = Field(default_factory=dict)
    sli_alerts: list[str] = Field(default_factory=list)


class StageSpec(StrictBaseModel):
    name: str
    stage_id: str = ""
    objective: str
    depends_on_stages: list[str] = Field(default_factory=list)
    scope_hint: list[str] = Field(default_factory=list)
    source_file: str = ""
    source_mode: Literal["query_sections", "anchor_section", "full_file"] = "query_sections"
    source_query: str = ""
    source_anchor: str = ""
    write_scope: list[str] = Field(default_factory=list)
    risk_level: StageRiskLevel = "medium"
    invariants: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    trust_sources: list[str] = Field(default_factory=list)
    trust_priority: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    subgoals: list[StageSubgoal] = Field(default_factory=list)
    feature_checklist: list[FeatureChecklistItem] = Field(default_factory=list)

    # Stage classification
    stage_type: Literal["design_probe", "implementation", "integration", "full_regression"] = "implementation"
    build_strategy: BuildStrategyProfile | None = None

    # Gate commands — split local vs remote
    test_commands: list[str] = Field(default_factory=list)
    lint_commands: list[str] = Field(default_factory=list)
    perf_checks: list[str] = Field(default_factory=list)
    gate_commands_remote_tiered: list[TieredGateCommand] = Field(default_factory=list)
    gate_commands_remote: list[str] = Field(default_factory=list)
    remote_gate_contracts: list[RemoteGateContract] = Field(default_factory=list)

    # Execution environment
    execution_env: Literal["local_only", "node0_container", "node1_container", "node0_and_node1"] = "local_only"
    requires_remote: bool = False
    sync_strategy: Literal["local_only", "sync_to_node0", "sync_to_node0_and_node1"] = "local_only"

    # Check plugin profile — maps gate_tier → plugin names.
    # When omitted the built-in defaults in check_plugins.profiles are used.
    check_profile: dict[str, list[str]] = Field(default_factory=dict)

    # Artifact dependencies
    required_inputs: list[str] = Field(default_factory=list)
    produces_artifacts: list[str] = Field(default_factory=list)
    expected_artifact_paths: list[str] = Field(default_factory=list)

    # Human decision points
    blocking_decisions: list[str] = Field(default_factory=list)

    # Rollback and verification
    rollback_requirements: list[str] = Field(default_factory=list)
    manual_checklist: list[str] = Field(default_factory=list)
    harness_constraints: list[str] = Field(default_factory=list)
    artifact_contracts: list[ArtifactContract] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_remote_gate_tiers(self) -> StageSpec:
        def _unique_commands(items: list[str]) -> list[str]:
            deduped: list[str] = []
            for item in items:
                normalized = item.strip()
                if not normalized or normalized in deduped:
                    continue
                deduped.append(normalized)
            return deduped

        # Backward compatibility: legacy gate_commands_remote implies fast_round tier.
        if self.gate_commands_remote and not self.gate_commands_remote_tiered:
            self.gate_commands_remote_tiered = [
                TieredGateCommand(command=command, tier="fast_round")
                for command in _unique_commands(self.gate_commands_remote)
            ]

        # Keep legacy field populated for existing prompt/policy paths that still read it.
        if self.gate_commands_remote_tiered:
            all_tiered_commands = _unique_commands(
                [item.command for item in self.gate_commands_remote_tiered]
            )
            self.gate_commands_remote = all_tiered_commands
        else:
            self.gate_commands_remote = _unique_commands(self.gate_commands_remote)

        return self


class StageArtifact(StrictBaseModel):
    """Structured output produced by a stage, persisted as JSON for downstream stages."""
    schema_version: int = 1
    stage_name: str
    artifact_name: str
    objective_hash: str = ""
    data_hash: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    produced_at: str = ""  # ISO timestamp
    notes: str = ""

class StageGate(StrictBaseModel):
    stage_name: str
    objective: str
    test_commands: list[str] = Field(default_factory=list)
    lint_commands: list[str] = Field(default_factory=list)
    perf_checks: list[str] = Field(default_factory=list)
    interface_contracts: list[str] = Field(default_factory=list)
    pass_criteria: list[str] = Field(default_factory=list)
    max_round_per_stage: int = 2


class WorkerDelivery(StrictBaseModel):
    worker: str
    summary: str
    changed_files: list[str] = Field(default_factory=list)
    tests_executed: list[str] = Field(default_factory=list)
    lint_executed: list[str] = Field(default_factory=list)
    perf_executed: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)


class WorkerPlanStep(StrictBaseModel):
    title: str
    files: list[str] = Field(default_factory=list)
    action: str


class WorkerPlan(StrictBaseModel):
    worker: str
    stage_name: str
    round_index: int
    goal: str
    relevant_files: list[str] = Field(default_factory=list)
    planned_steps: list[WorkerPlanStep] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    summary: str = ""


class PlanGateReview(StrictBaseModel):
    stage_name: str
    round_index: int
    pass_gate: bool
    worker_a_required_actions: list[str] = Field(default_factory=list)
    worker_b_required_actions: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    rationale: str


class PlanDriftArtifact(StrictBaseModel):
    stage_name: str
    round_index: int
    worker: str
    drift_detected: bool = False
    severity: Literal["none", "warning", "blocking"] = "none"
    out_of_plan_files: list[str] = Field(default_factory=list)
    missing_verification: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SelfReviewIssue(StrictBaseModel):
    severity: Severity
    title: str
    evidence: str
    fixed: bool
    note: str


class SelfReviewResult(StrictBaseModel):
    worker: str
    discovered_issues: list[SelfReviewIssue] = Field(default_factory=list)
    fixed_items: list[str] = Field(default_factory=list)
    remaining_risks: list[str] = Field(default_factory=list)


class CheckCommandResult(StrictBaseModel):
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    passed: bool = False

class AutoCheckResult(StrictBaseModel):
    worker: str
    stage_name: str
    test_results: list[CheckCommandResult] = Field(default_factory=list)
    lint_results: list[CheckCommandResult] = Field(default_factory=list)
    perf_results: list[CheckCommandResult] = Field(default_factory=list)
    harness_results: list[CheckCommandResult] = Field(default_factory=list)
    all_tests_passed: bool = False
    all_lint_passed: bool = False
    all_perf_passed: bool = False
    all_harness_passed: bool = False


class EvidenceSemantics(StrictBaseModel):
    certainty: EvidenceCertainty = "fact"
    facts: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    to_verify: list[str] = Field(default_factory=list)

class BugReport(StrictBaseModel):
    report_id: str
    severity: Severity
    title: str
    file_path: str
    line: int | None = Field(default=None, description="Line number in file, if applicable")
    evidence: str
    reasoning: str
    reproduction_or_inference: str
    fix_suggestion: str
    evidence_semantics: EvidenceSemantics = Field(default_factory=EvidenceSemantics)


class PeerReviewResult(StrictBaseModel):
    reviewer: str
    target_worker: str
    reports: list[BugReport] = Field(default_factory=list)
    overall_notes: list[str] = Field(default_factory=list)


class OwnerDecision(StrictBaseModel):
    report_id: str
    action: OwnerDecisionAction
    rationale: str
    patch_summary: str = ""


class OwnerTriageResult(StrictBaseModel):
    owner: str
    decisions: list[OwnerDecision] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)


class VerifierCriterionResult(StrictBaseModel):
    criterion: str
    status: Literal["satisfied", "gap", "needs_more_evidence"] = "gap"
    evidence: list[str] = Field(default_factory=list)
    gap_summary: str = ""


class VerifierReport(StrictBaseModel):
    stage_name: str
    round_index: int
    pass_ready: bool = False
    spec_gap_detected: bool = False
    ambiguous_contracts: list[str] = Field(default_factory=list)
    requested_clarifications: list[str] = Field(default_factory=list)
    blocking_gaps: list[str] = Field(default_factory=list)
    criteria_results: list[VerifierCriterionResult] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    verifier_notes: list[str] = Field(default_factory=list)


class SpecGapReport(StrictBaseModel):
    stage_name: str
    round_index: int
    spec_gap_detected: bool = False
    ambiguous_contracts: list[str] = Field(default_factory=list)
    requested_clarifications: list[str] = Field(default_factory=list)
    rationale: str = ""


class JudgeGateReview(StrictBaseModel):
    stage_name: str
    round_index: int
    pass_gate: bool
    high_severity_open: list[str] = Field(default_factory=list)
    disputed_items: list[str] = Field(default_factory=list)
    required_actions: list[str] = Field(default_factory=list)
    rationale: str


class ReportMemoryEntry(StrictBaseModel):
    report_id: str
    reviewer: str
    target_worker: str
    severity: Severity
    certainty: EvidenceCertainty = "fact"
    title: str
    file_path: str
    line: int | None = None
    status: Literal["open", "resolved", "rejected", "deferred"] = "open"
    first_round: int = 1
    last_round: int = 1


class ContextSynthesis(StrictBaseModel):
    confirmed_facts: list[str] = Field(default_factory=list)
    active_inferences: list[str] = Field(default_factory=list)
    verification_backlog: list[str] = Field(default_factory=list)
    open_required_actions: list[str] = Field(default_factory=list)
    dedupe_report_ids: list[str] = Field(default_factory=list)
    resolved_report_ids: list[str] = Field(default_factory=list)

class ContextBudgetConfig(StrictBaseModel):
    """Controls how much context each zone is allowed to consume."""
    max_total_chars: int = 80_000
    fixed_zone_ratio: float = 0.25
    current_round_ratio: float = 0.45
    history_zone_ratio: float = 0.30

class CompressionEvent(StrictBaseModel):
    """Records a single context compression action for observability."""
    stage_name: str
    round_index: int
    zone: str
    original_chars: int
    compressed_chars: int
    dropped_items: int = 0
    timestamp_iso: str = ""


class StageContextPacket(StrictBaseModel):
    stage_name: str
    round_index: int
    objective: str
    source_of_truth: str = ""
    plan_summary: list[str] = Field(default_factory=list)
    immutable_requirements: list[str] = Field(default_factory=list)
    synthesis: ContextSynthesis = Field(default_factory=ContextSynthesis)
    review_patch_strategy: Literal["full_patch", "delta_since_last_round"] = "full_patch"
    notes: list[str] = Field(default_factory=list)


class PlanNode(StrictBaseModel):
    node_id: str
    kind: PlanNodeKind
    owner: Literal["judge", "worker_a", "worker_b", "system"]
    depends_on: list[str] = Field(default_factory=list)
    wait_for: list[str] = Field(default_factory=list)
    write_scope: list[str] = Field(default_factory=list)
    artifact_contracts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class StageExecutionPlan(StrictBaseModel):
    stage_name: str
    objective: str
    planner: str = "deterministic_coordinator"
    nodes: list[PlanNode] = Field(default_factory=list)
    parallel_groups: list[list[str]] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)


class StageDagNode(StrictBaseModel):
    stage_name: str
    required_inputs: list[str] = Field(default_factory=list)
    produces_artifacts: list[str] = Field(default_factory=list)
    depends_on_stages: list[str] = Field(default_factory=list)
    write_scope: list[str] = Field(default_factory=list)
    blocking_decisions: list[str] = Field(default_factory=list)


class StageDagBatch(StrictBaseModel):
    batch_index: int
    stage_names: list[str] = Field(default_factory=list)
    parallel_safe: bool = False
    notes: list[str] = Field(default_factory=list)


class StageDagPlan(StrictBaseModel):
    nodes: list[StageDagNode] = Field(default_factory=list)
    batches: list[StageDagBatch] = Field(default_factory=list)
    serial_execution_order: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)


class StageRoundLog(StrictBaseModel):
    round_index: int
    worker_a_delivery: WorkerDelivery
    worker_b_delivery: WorkerDelivery
    worker_a_auto_checks: AutoCheckResult | None = None
    worker_b_auto_checks: AutoCheckResult | None = None
    worker_a_self_review: SelfReviewResult
    worker_b_self_review: SelfReviewResult
    peer_review_a_on_b: PeerReviewResult
    peer_review_b_on_a: PeerReviewResult
    triage_a: OwnerTriageResult
    triage_b: OwnerTriageResult
    verifier_report: VerifierReport
    judge_gate: JudgeGateReview


class CompactStageRoundLog(StrictBaseModel):
    """Lightweight summary of a round, used after stage completion to free memory.

    The full ``StageRoundLog`` is persisted to disk as an artifact; this compact
    version retains only the fields needed for cross-stage reasoning.
    """
    round_index: int
    worker_a_summary: str = ""
    worker_b_summary: str = ""
    gate_decision: str = ""
    gate_reasoning: str = ""
    open_report_ids: list[str] = Field(default_factory=list)
    closed_report_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_resolved_to_closed(cls, data: Any) -> Any:
        """Backward-compat: accept legacy ``resolved_report_ids`` from older JSON."""
        if isinstance(data, dict) and "resolved_report_ids" in data:
            data.setdefault("closed_report_ids", data.pop("resolved_report_ids"))
        return data

class StageResult(StrictBaseModel):
    stage_name: str
    passed: bool
    rounds_used: int
    gate: JudgeGateReview
    round_logs: list[StageRoundLog] = Field(default_factory=list)
    compact_round_logs: list[CompactStageRoundLog] = Field(default_factory=list)


class RunSummary(StrictBaseModel):
    target_repo: str
    overall_passed: bool
    stage_results: list[StageResult] = Field(default_factory=list)


class ReviewFlowState(StrictBaseModel):
    target_repo: str = ""
    stages: list[StageSpec] = Field(default_factory=list)
    max_round_per_stage: int = 2
    summary: RunSummary | None = None
    resumed_stage_results: list[StageResult] = Field(default_factory=list)
    resume_passed_stage_names: list[str] = Field(default_factory=list)
    stage_artifacts: dict[str, list[StageArtifact]] = Field(default_factory=dict)
    approved_decisions: list[str] = Field(default_factory=list)
    harness_metrics: dict[str, int] = Field(default_factory=dict)
    stage_execution_plans: dict[str, StageExecutionPlan] = Field(default_factory=dict)
    stage_dag_plan: StageDagPlan | None = None
    stage_context_packets: dict[str, list[StageContextPacket]] = Field(default_factory=dict)
    worker_plans: dict[str, list[WorkerPlan]] = Field(default_factory=dict)
    plan_gate_reviews: dict[str, list[PlanGateReview]] = Field(default_factory=dict)
    plan_drift_artifacts: dict[str, list[PlanDriftArtifact]] = Field(default_factory=dict)
    verifier_reports: dict[str, list[VerifierReport]] = Field(default_factory=dict)
    spec_gap_reports: dict[str, list[SpecGapReport]] = Field(default_factory=dict)
    runtime_nudges: dict[str, list[RuntimeNudgeArtifact]] = Field(default_factory=dict)
    check_summary_artifacts: dict[str, list[CheckSummaryArtifact]] = Field(default_factory=dict)
    task_handoffs: dict[str, list[TaskHandoffPacket]] = Field(default_factory=dict)
    convergence_signals: dict[str, list[ConvergenceSignal]] = Field(default_factory=dict)
    report_memory: dict[str, list[ReportMemoryEntry]] = Field(default_factory=dict)
    stage_progress_ledgers: dict[str, list[StageProgressLedger]] = Field(default_factory=dict)
    worker_entry_packets: dict[str, list[WorkerEntryPacket]] = Field(default_factory=dict)
    baseline_status_artifacts: dict[str, list[BaselineStatusArtifact]] = Field(default_factory=dict)
    clean_state_artifacts: dict[str, list[CleanStateArtifact]] = Field(default_factory=dict)
    feature_checklists: dict[str, list[FeatureChecklistArtifact]] = Field(default_factory=dict)
