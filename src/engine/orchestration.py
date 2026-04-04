from __future__ import annotations

from collections import defaultdict, deque

from core.models import (
    PlanNode,
    StageDagBatch,
    StageDagNode,
    StageDagPlan,
    StageExecutionPlan,
    StageSpec,
)


def infer_stage_write_scope(stage: StageSpec) -> list[str]:
    raw_paths = stage.write_scope or stage.scope_hint or ["."]
    normalized: list[str] = []
    for raw in raw_paths:
        cleaned = (raw or "").strip().strip("/")
        token = cleaned or "."
        if token not in normalized:
            normalized.append(token)
    return normalized


def build_stage_execution_plan(stage: StageSpec) -> StageExecutionPlan:
    scope_a = [f"workspace:worker_a:{path}" for path in infer_stage_write_scope(stage)]
    scope_b = [f"workspace:worker_b:{path}" for path in infer_stage_write_scope(stage)]
    artifact_contracts = [
        f"expected_artifact:{path}" for path in stage.expected_artifact_paths
    ]
    artifact_contracts.extend(
        f"artifact_contract:{contract.path}:{contract.format}"
        for contract in stage.artifact_contracts
    )

    nodes = [
        PlanNode(
            node_id="stage_gate",
            kind="stage_gate",
            owner="judge",
            wait_for=["structured_stage_gate"],
            notes=["Publish acceptance gate from immutable StageSpec constraints."],
        ),
        PlanNode(
            node_id="worker_a_planner",
            kind="planner",
            owner="worker_a",
            depends_on=["stage_gate"],
            wait_for=["worker_plan"],
            write_scope=scope_a,
            notes=["Produce a structured read-first execution plan before editing."],
        ),
        PlanNode(
            node_id="worker_b_planner",
            kind="planner",
            owner="worker_b",
            depends_on=["stage_gate"],
            wait_for=["worker_plan"],
            write_scope=scope_b,
            notes=["Produce a structured read-first execution plan before editing."],
        ),
        PlanNode(
            node_id="plan_gate",
            kind="plan_gate",
            owner="judge",
            depends_on=["worker_a_planner", "worker_b_planner"],
            wait_for=["structured_plan_gate"],
            artifact_contracts=artifact_contracts,
            notes=["Approve both worker plans before implementation starts."],
        ),
        PlanNode(
            node_id="worker_a_implementation",
            kind="implementation",
            owner="worker_a",
            depends_on=["plan_gate"],
            wait_for=["worker_delivery"],
            write_scope=scope_a,
            notes=["Implement the stage objective in worker_a isolated workspace."],
        ),
        PlanNode(
            node_id="worker_b_implementation",
            kind="implementation",
            owner="worker_b",
            depends_on=["plan_gate"],
            wait_for=["worker_delivery"],
            write_scope=scope_b,
            notes=["Implement the stage objective in worker_b isolated workspace."],
        ),
        PlanNode(
            node_id="worker_a_auto_checks_post_impl",
            kind="auto_checks",
            owner="system",
            depends_on=["worker_a_implementation"],
            wait_for=["checks_finished"],
            write_scope=scope_a,
            notes=["Run local/remote checks after worker_a implementation."],
        ),
        PlanNode(
            node_id="worker_b_auto_checks_post_impl",
            kind="auto_checks",
            owner="system",
            depends_on=["worker_b_implementation"],
            wait_for=["checks_finished"],
            write_scope=scope_b,
            notes=["Run local/remote checks after worker_b implementation."],
        ),
        PlanNode(
            node_id="worker_a_self_review",
            kind="self_review",
            owner="worker_a",
            depends_on=["worker_a_auto_checks_post_impl"],
            wait_for=["structured_self_review"],
            write_scope=scope_a,
            notes=["Use checks as hard evidence during self-review."],
        ),
        PlanNode(
            node_id="worker_b_self_review",
            kind="self_review",
            owner="worker_b",
            depends_on=["worker_b_auto_checks_post_impl"],
            wait_for=["structured_self_review"],
            write_scope=scope_b,
            notes=["Use checks as hard evidence during self-review."],
        ),
        PlanNode(
            node_id="worker_a_auto_checks_post_self",
            kind="auto_checks",
            owner="system",
            depends_on=["worker_a_self_review"],
            wait_for=["checks_finished"],
            write_scope=scope_a,
        ),
        PlanNode(
            node_id="worker_b_auto_checks_post_self",
            kind="auto_checks",
            owner="system",
            depends_on=["worker_b_self_review"],
            wait_for=["checks_finished"],
            write_scope=scope_b,
        ),
        PlanNode(
            node_id="worker_a_peer_review_on_b",
            kind="peer_review",
            owner="worker_a",
            depends_on=["worker_b_auto_checks_post_self"],
            wait_for=["structured_peer_review"],
            artifact_contracts=artifact_contracts,
            notes=["Review only delta patch + synthesized context, not whole history."],
        ),
        PlanNode(
            node_id="worker_b_peer_review_on_a",
            kind="peer_review",
            owner="worker_b",
            depends_on=["worker_a_auto_checks_post_self"],
            wait_for=["structured_peer_review"],
            artifact_contracts=artifact_contracts,
            notes=["Review only delta patch + synthesized context, not whole history."],
        ),
        PlanNode(
            node_id="worker_a_owner_triage",
            kind="owner_triage",
            owner="worker_a",
            depends_on=["worker_b_peer_review_on_a"],
            wait_for=["structured_owner_triage"],
            write_scope=scope_a,
        ),
        PlanNode(
            node_id="worker_b_owner_triage",
            kind="owner_triage",
            owner="worker_b",
            depends_on=["worker_a_peer_review_on_b"],
            wait_for=["structured_owner_triage"],
            write_scope=scope_b,
        ),
        PlanNode(
            node_id="worker_a_auto_checks_post_triage",
            kind="auto_checks",
            owner="system",
            depends_on=["worker_a_owner_triage"],
            wait_for=["checks_finished"],
            write_scope=scope_a,
            artifact_contracts=artifact_contracts,
        ),
        PlanNode(
            node_id="worker_b_auto_checks_post_triage",
            kind="auto_checks",
            owner="system",
            depends_on=["worker_b_owner_triage"],
            wait_for=["checks_finished"],
            write_scope=scope_b,
            artifact_contracts=artifact_contracts,
        ),
        PlanNode(
            node_id="verifier_review",
            kind="verifier",
            owner="system",
            depends_on=[
                "worker_a_auto_checks_post_triage",
                "worker_b_auto_checks_post_triage",
                "worker_a_owner_triage",
                "worker_b_owner_triage",
            ],
            wait_for=["structured_verifier_report"],
            artifact_contracts=artifact_contracts,
            notes=["Independent verifier audits the stage contract before judge ruling."],
        ),
        PlanNode(
            node_id="judge_gate",
            kind="judge_gate",
            owner="judge",
            depends_on=[
                "verifier_review",
            ],
            wait_for=["structured_judge_gate"],
            artifact_contracts=artifact_contracts,
            notes=["Only fact-grade S0/S1 findings and failed checks can block."],
        ),
        PlanNode(
            node_id="promotion",
            kind="promotion",
            owner="system",
            depends_on=["judge_gate"],
            wait_for=["owner_workspace_promotion"],
            notes=["Promote only selected owner workspace after gate pass."],
        ),
    ]
    parallel_groups = [
        ["stage_gate"],
        ["worker_a_planner", "worker_b_planner"],
        ["plan_gate"],
        ["worker_a_implementation", "worker_b_implementation"],
        ["worker_a_auto_checks_post_impl", "worker_b_auto_checks_post_impl"],
        ["worker_a_self_review", "worker_b_self_review"],
        ["worker_a_auto_checks_post_self", "worker_b_auto_checks_post_self"],
        ["worker_a_peer_review_on_b", "worker_b_peer_review_on_a"],
        ["worker_a_owner_triage", "worker_b_owner_triage"],
        ["worker_a_auto_checks_post_triage", "worker_b_auto_checks_post_triage"],
        ["verifier_review"],
        ["judge_gate"],
        ["promotion"],
    ]
    plan = StageExecutionPlan(
        stage_name=stage.name,
        objective=stage.objective,
        nodes=nodes,
        parallel_groups=parallel_groups,
        validation_notes=[
            "Execution plan is deterministic and validated before stage start.",
            "worker_a and worker_b write scopes are isolated by workspace namespace.",
        ],
    )
    plan.validation_errors = validate_stage_execution_plan(plan)
    return plan


def validate_stage_execution_plan(plan: StageExecutionPlan) -> list[str]:
    errors: list[str] = []
    node_ids = [node.node_id for node in plan.nodes]
    node_id_set = set(node_ids)

    if len(node_ids) != len(node_id_set):
        errors.append("stage execution plan contains duplicate node_id values")

    adjacency: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = {node.node_id: 0 for node in plan.nodes}

    for node in plan.nodes:
        for dep in node.depends_on:
            if dep not in node_id_set:
                errors.append(
                    f"stage execution plan node '{node.node_id}' depends on missing node '{dep}'"
                )
                continue
            adjacency[dep].append(node.node_id)
            indegree[node.node_id] += 1
        for wait_item in node.wait_for:
            if not wait_item.strip():
                errors.append(
                    f"stage execution plan node '{node.node_id}' has blank wait_for entry"
                )

    queue = deque([node_id for node_id, degree in indegree.items() if degree == 0])
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for child in adjacency.get(current, []):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(plan.nodes):
        errors.append("stage execution plan contains a dependency cycle")

    for group_index, group in enumerate(plan.parallel_groups, start=1):
        seen_group_scopes: set[str] = set()
        for node_id in group:
            if node_id not in node_id_set:
                errors.append(
                    f"parallel group {group_index} references missing node '{node_id}'"
                )
                continue
            node = next(node for node in plan.nodes if node.node_id == node_id)
            for dep in node.depends_on:
                if dep in group:
                    errors.append(
                        f"parallel group {group_index} has intra-group dependency '{node_id}' -> '{dep}'"
                    )
            for scope in node.write_scope:
                if scope in seen_group_scopes:
                    errors.append(
                        f"parallel group {group_index} has conflicting write scope '{scope}'"
                    )
                seen_group_scopes.add(scope)

    return errors


def build_stage_dag_plan(
    stages: list[StageSpec],
    initial_artifacts: set[str] | None = None,
) -> StageDagPlan:
    nodes: list[StageDagNode] = []
    artifact_producers: dict[str, str] = {}
    errors: list[str] = []
    seeded_artifacts = initial_artifacts or set()
    stage_aliases: dict[str, str] = {}

    for stage in stages:
        for artifact_name in stage.produces_artifacts:
            owner = artifact_producers.get(artifact_name)
            if owner is not None and owner != stage.name:
                errors.append(
                    f"artifact '{artifact_name}' is produced by both '{owner}' and '{stage.name}'"
                )
            artifact_producers[artifact_name] = stage.name

    stage_name_set = {stage.name for stage in stages}
    if len(stage_name_set) != len(stages):
        errors.append("stages contain duplicate stage.name values")
    for stage in stages:
        aliases = [stage.name]
        if stage.stage_id.strip():
            aliases.append(stage.stage_id.strip())
        for alias in aliases:
            owner = stage_aliases.get(alias)
            if owner is not None and owner != stage.name:
                errors.append(
                    f"stage alias '{alias}' is used by both '{owner}' and '{stage.name}'"
                )
                continue
            stage_aliases[alias] = stage.name

    for stage in stages:
        depends_on_stages: list[str] = []
        for required_input in stage.required_inputs:
            producer = artifact_producers.get(required_input)
            if producer is None:
                if required_input not in seeded_artifacts:
                    errors.append(
                        f"stage '{stage.name}' requires missing artifact '{required_input}'"
                    )
                continue
            if producer != stage.name and producer not in depends_on_stages:
                depends_on_stages.append(producer)
        for explicit_dep in stage.depends_on_stages:
            resolved_dep = stage_aliases.get(explicit_dep)
            if resolved_dep is None:
                errors.append(
                    f"stage '{stage.name}' declares unknown depends_on_stages entry '{explicit_dep}'"
                )
                continue
            if resolved_dep != stage.name and resolved_dep not in depends_on_stages:
                depends_on_stages.append(resolved_dep)
        nodes.append(
            StageDagNode(
                stage_name=stage.name,
                required_inputs=list(stage.required_inputs),
                produces_artifacts=list(stage.produces_artifacts),
                depends_on_stages=depends_on_stages,
                write_scope=infer_stage_write_scope(stage),
                blocking_decisions=list(stage.blocking_decisions),
            )
        )

    indegree = {node.stage_name: 0 for node in nodes}
    adjacency: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for dep in node.depends_on_stages:
            adjacency[dep].append(node.stage_name)
            indegree[node.stage_name] += 1

    queue = deque(sorted(name for name, degree in indegree.items() if degree == 0))
    topo_order: list[str] = []
    while queue:
        current = queue.popleft()
        topo_order.append(current)
        for child in sorted(adjacency.get(current, [])):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(topo_order) != len(nodes):
        errors.append("stage DAG contains a dependency cycle")

    produced: set[str] = set(seeded_artifacts)
    remaining = {node.stage_name: node for node in nodes}
    batches: list[StageDagBatch] = []
    batch_index = 1

    while remaining:
        ready = [
            node for node in remaining.values()
            if set(node.required_inputs).issubset(produced)
        ]
        if not ready:
            errors.append("stage DAG cannot make progress; required_inputs remain unsatisfied")
            break

        ready.sort(key=lambda item: item.stage_name)
        batch_stage_names: list[str] = []
        batch_scopes: set[str] = set()
        batch_notes: list[str] = []
        parallel_safe = True

        for node in ready:
            if any(scope in batch_scopes for scope in node.write_scope):
                parallel_safe = False
                batch_notes.append(
                    f"stage '{node.stage_name}' shares write_scope with another ready stage"
                )
                continue
            if node.blocking_decisions:
                parallel_safe = False
                batch_notes.append(
                    f"stage '{node.stage_name}' requires blocking decisions before parallel run"
                )
            batch_stage_names.append(node.stage_name)
            batch_scopes.update(node.write_scope)

        if not batch_stage_names:
            serial_stage = ready[0]
            batch_stage_names = [serial_stage.stage_name]
            parallel_safe = False
            batch_notes.append(
                "parallel batch collapsed to serial execution because all ready stages conflict"
            )

        batches.append(
            StageDagBatch(
                batch_index=batch_index,
                stage_names=batch_stage_names,
                parallel_safe=parallel_safe and len(batch_stage_names) > 1,
                notes=batch_notes,
            )
        )
        batch_index += 1

        for stage_name in batch_stage_names:
            node = remaining.pop(stage_name)
            produced.update(node.produces_artifacts)

    serial_execution_order = [stage_name for batch in batches for stage_name in batch.stage_names]
    validation_notes = [
        "Stage DAG derives dependencies from required_inputs -> produces_artifacts plus explicit depends_on_stages.",
        "Parallel batches are advisory unless the executor has isolated per-stage workspaces.",
    ]

    return StageDagPlan(
        nodes=nodes,
        batches=batches,
        serial_execution_order=serial_execution_order,
        validation_errors=errors,
        validation_notes=validation_notes,
    )
