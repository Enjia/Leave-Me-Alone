from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.layered_policy import (
    CURRENT_SCHEMA_VERSION,
    load_layered_policy,
    migrate_layered_policy_payload,
    resolve_stage_policy,
)
from engine.flow import MultiCodexReviewFlow
from core.models import BudgetPolicy, ContextBudgetConfig, StageSpec


def test_load_and_resolve_layered_policy_merge_order(tmp_path: Path) -> None:
    policy_path = tmp_path / "layered_policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "global": {"max_round_per_stage": 2, "context_budget_max_chars": 70000},
                "project": {"context_budget_max_chars": 65000},
                "profiles": {
                    "hotfix": {"max_round_per_stage": 4, "per_stage_budget_usd": 1.5},
                },
                "stages": {
                    "stage-A": {
                        "profile": "hotfix",
                        "overrides": {"max_round_per_stage": 5},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_layered_policy(policy_path)
    assert cfg is not None
    resolved = resolve_stage_policy(cfg, stage_name="stage-A")
    assert resolved.max_round_per_stage == 5
    assert resolved.context_budget_max_chars == 65000
    assert resolved.per_stage_budget_usd == 1.5


def test_load_layered_policy_migrates_v1_payload(tmp_path: Path) -> None:
    policy_path = tmp_path / "layered_policy_v1.json"
    policy_path.write_text(
        json.dumps(
            {
                "global_policy": {"max_round_per_stage": 2},
                "profiles": {
                    "hotfix": {"warn_budget_usd": 1.5},
                },
                "stages": {
                    "stage-A": {
                        "profile_id": "hotfix",
                        "max_round_per_stage": 5,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_layered_policy(policy_path)
    assert cfg is not None
    assert cfg.schema_version == CURRENT_SCHEMA_VERSION
    resolved = resolve_stage_policy(cfg, stage_name="stage-A")
    assert resolved.max_round_per_stage == 5
    assert resolved.warn_budget_usd == 1.5


def test_migrate_layered_policy_payload_rejects_future_schema() -> None:
    with pytest.raises(ValueError, match="Unsupported layered policy schema_version=999"):
        migrate_layered_policy_payload({"schema_version": 999})


def test_load_layered_policy_rejects_undefined_profile(tmp_path: Path) -> None:
    policy_path = tmp_path / "layered_policy_invalid_profile.json"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": CURRENT_SCHEMA_VERSION,
                "stages": {
                    "stage-A": {
                        "profile": "missing-profile",
                        "overrides": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="undefined profiles: missing-profile"):
        load_layered_policy(policy_path)


def test_load_layered_policy_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        load_layered_policy(tmp_path / "not_found.json")


def test_apply_stage_policy_overrides_updates_runtime_state() -> None:
    stage = StageSpec(
        name="stage-A",
        objective="obj",
        acceptance_criteria=["done"],
        invariants=["safe"],
    )
    policy_cfg = load_layered_policy(None)
    # build policy via model-validate path for brevity
    from config.layered_policy import LayeredPolicyConfig

    policy_cfg = LayeredPolicyConfig.model_validate(
        {
            "global": {"max_round_per_stage": 2, "context_budget_max_chars": 80000},
            "stages": {
                "stage-A": {
                    "overrides": {
                        "max_round_per_stage": 6,
                        "context_budget_max_chars": 50000,
                        "warn_budget_usd": 2.0,
                        "hard_budget_usd": 4.0,
                        "per_stage_budget_usd": 1.0,
                    }
                }
            },
        }
    )

    flow = SimpleNamespace(
        _layered_policy=policy_cfg,
        state=SimpleNamespace(max_round_per_stage=2),
        cfg=SimpleNamespace(context_budget_max_chars=80000),
        _context_budget_config=ContextBudgetConfig(max_total_chars=80000),
        cost_ledger=SimpleNamespace(budget_policy=BudgetPolicy()),
    )

    MultiCodexReviewFlow._apply_stage_policy_overrides(flow, stage)
    assert flow.state.max_round_per_stage == 6
    assert flow.cfg.context_budget_max_chars == 50000
    assert flow._context_budget_config.max_total_chars == 50000
    assert flow.cost_ledger.budget_policy.warn_budget_usd == 2.0
    assert flow.cost_ledger.budget_policy.hard_budget_usd == 4.0
    assert flow.cost_ledger.budget_policy.per_stage_budget_usd == 1.0
