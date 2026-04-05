from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter, ValidationError

logger = logging.getLogger(__name__)

from core.models import BudgetEnforcement, RunBudgetMode, StageArtifact, StageSpec

Provider = Literal["codex"]
OwnerWorker = Literal["worker_a", "worker_b"]
CURRENT_STAGE_ARTIFACT_SCHEMA_VERSION = 1


@dataclass
class RuntimeConfig:
    target_repo: Path
    stages_file: Path
    runtime_dir: Path
    seed_artifacts_dir: Path | None
    model: str
    sandbox_mode: str
    enable_a2a: bool
    a2a_endpoints: dict[str, str]
    max_round_per_stage: int
    output_file: Path
    provider: Provider = "codex"
    opencode_extra_args: list[str] = field(default_factory=list)
    remote_host: str = ""
    remote_workdir: str = ""
    remote_host_secondary: str = ""
    remote_workdir_secondary: str = ""
    split_worker_remote_endpoints: bool = False
    auto_approve_decisions: list[str] = field(default_factory=list)
    owner_worker: OwnerWorker = "worker_a"
    enable_convergence_signals: bool = True
    max_no_progress_rounds: int = 2
    max_repeated_failure_rounds: int = 2
    triage_require_reject_rationale: bool = True
    triage_block_fact_high_severity_reject: bool = True
    promotion_require_all_checks: bool = True
    promotion_require_no_open_fact_high_severity: bool = True
    promotion_require_no_disputes: bool = True
    drift_fail_on_suspicious_items: bool = True
    drift_fail_on_extra_commands: bool = True
    warn_budget_usd: float = 0.0
    hard_budget_usd: float = 0.0
    per_stage_budget_usd: float = 0.0
    hard_budget_enforcement: BudgetEnforcement = "stage_boundary"
    run_budget_mode: RunBudgetMode = "run_only"
    context_budget_max_chars: int = 80_000
    layered_policy_file: Path | None = None

    @property
    def remote_host_node1(self) -> str:
        return self.remote_host_secondary

    @property
    def remote_workdir_node1(self) -> str:
        return self.remote_workdir_secondary

    @property
    def split_worker_remote_hosts(self) -> bool:
        return self.split_worker_remote_endpoints

def parse_stage_specs(stages_file: Path) -> list[StageSpec]:
    payload = json.loads(stages_file.read_text(encoding="utf-8"))
    adapter = TypeAdapter(list[StageSpec])
    stages = adapter.validate_python(payload)
    for stage in stages:
        if stage.source_file:
            path = Path(stage.source_file)
            if not path.is_absolute():
                stage.source_file = str((stages_file.parent / path).resolve())
    return stages


def parse_a2a_endpoints(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("--a2a-endpoints must be a JSON object")
    endpoints: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str):
            logger.warning("Skipping non-string key in --a2a-endpoints: %r", key)
            continue
        if not isinstance(value, str):
            logger.warning("Skipping non-string value for key %r in --a2a-endpoints: %.200r", key, value)
            continue
        endpoints[key] = value.rstrip("/")
    return endpoints


def load_stage_artifacts(artifacts_dir: Path | None) -> dict[str, list[StageArtifact]]:
    if artifacts_dir is None or not artifacts_dir.exists():
        return {}

    stage_artifacts: dict[str, list[StageArtifact]] = {}
    adapter = TypeAdapter(StageArtifact)
    for artifact_file in sorted(artifacts_dir.glob("*.json")):
        try:
            payload = json.loads(artifact_file.read_text(encoding="utf-8"))
            payload = migrate_stage_artifact_payload(payload)
            artifact = adapter.validate_python(payload)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read/parse artifact file %s: %s",
                artifact_file,
                str(exc)[:500],
            )
            continue
        except ValueError as exc:
            logger.warning(
                "Skipping artifact file %s due to migration/compatibility error: %s",
                artifact_file,
                str(exc)[:300],
            )
            continue
        except ValidationError as exc:
            logger.debug(
                "Skipping non-StageArtifact file %s: %s",
                artifact_file.name,
                str(exc)[:200],
            )
            continue
        stage_artifacts.setdefault(artifact.stage_name, []).append(artifact)
    return stage_artifacts


def migrate_stage_artifact_payload(payload: object) -> dict[str, object]:
    """Normalize StageArtifact payloads across schema versions.

    Current schema is v1. Missing schema_version is treated as v1 for backward
    compatibility with early artifacts.
    """
    if not isinstance(payload, dict):
        raise ValueError("StageArtifact payload must be a JSON object")
    migrated = dict(payload)
    raw_version = migrated.get("schema_version", CURRENT_STAGE_ARTIFACT_SCHEMA_VERSION)
    try:
        schema_version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid stage artifact schema_version={raw_version!r}") from exc
    if schema_version < 1:
        raise ValueError(f"Invalid stage artifact schema_version={schema_version}")
    if schema_version > CURRENT_STAGE_ARTIFACT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported stage artifact schema_version={schema_version}"
        )
    if "schema_version" not in migrated:
        migrated["schema_version"] = CURRENT_STAGE_ARTIFACT_SCHEMA_VERSION
    return migrated
