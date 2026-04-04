from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core.models import BudgetPolicy, CostSnapshot
from state.cost_ledger import CostLedger, _estimate_cost_usd
from agents.codex_exec_agent import CodexExecUsage


class TestEstimateCostUsd:
    def test_known_model_o3(self) -> None:
        cost = _estimate_cost_usd("o3", input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost == pytest.approx(50.0)

    def test_known_model_o4_mini(self) -> None:
        cost = _estimate_cost_usd("o4-mini", input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost == pytest.approx(5.5)

    def test_known_model_case_insensitive(self) -> None:
        cost = _estimate_cost_usd("O4-Mini", input_tokens=1_000_000, output_tokens=0)
        assert cost == pytest.approx(1.1)

    def test_unknown_model_uses_fallback(self) -> None:
        cost = _estimate_cost_usd("unknown-model-xyz", input_tokens=1_000_000, output_tokens=1_000_000)
        # Fallback: $3/M input + $12/M output
        assert cost == pytest.approx(15.0)

    def test_zero_tokens(self) -> None:
        cost = _estimate_cost_usd("o3", input_tokens=0, output_tokens=0)
        assert cost == 0.0


class TestCostLedger:
    def test_record_and_snapshot(self) -> None:
        ledger = CostLedger()
        ledger.record(
            input_tokens=1000,
            output_tokens=500,
            model="o4-mini",
            agent_role="worker_a",
            stage_name="stage-1",
            latency_sec=2.5,
        )
        snapshot = ledger.snapshot()
        assert snapshot.invocation_count == 1
        assert snapshot.total_input_tokens == 1000
        assert snapshot.total_output_tokens == 500
        assert snapshot.total_tokens == 1500
        assert snapshot.estimated_cost_usd > 0

    def test_multiple_records_aggregate(self) -> None:
        ledger = CostLedger()
        ledger.record(input_tokens=100, output_tokens=50, model="o3", stage_name="s1", agent_role="worker_a_impl")
        ledger.record(input_tokens=200, output_tokens=100, model="o3", stage_name="s1", agent_role="worker_b_impl")
        ledger.record(input_tokens=300, output_tokens=150, model="o4-mini", stage_name="s2", agent_role="judge_gate")

        snapshot = ledger.snapshot()
        assert snapshot.invocation_count == 3
        assert snapshot.total_input_tokens == 600
        assert snapshot.total_output_tokens == 300

        assert "s1" in snapshot.by_stage
        assert snapshot.by_stage["s1"].invocation_count == 2
        assert snapshot.by_stage["s1"].input_tokens == 300

        assert "s2" in snapshot.by_stage
        assert snapshot.by_stage["s2"].invocation_count == 1

        assert "o3" in snapshot.by_model
        assert snapshot.by_model["o3"].invocation_count == 2
        assert "o4-mini" in snapshot.by_model
        assert snapshot.by_model["o4-mini"].invocation_count == 1
        assert snapshot.by_worker["worker_a"].invocation_count == 1
        assert snapshot.by_worker["worker_b"].invocation_count == 1
        assert snapshot.by_worker["judge"].invocation_count == 1

    def test_stage_cost_usd(self) -> None:
        ledger = CostLedger()
        ledger.record(input_tokens=1_000_000, output_tokens=0, model="o3", stage_name="expensive")
        cost = ledger.stage_cost_usd("expensive")
        assert cost == pytest.approx(10.0)

        assert ledger.stage_cost_usd("nonexistent") == 0.0

    def test_budget_warn_triggered(self) -> None:
        policy = BudgetPolicy(warn_budget_usd=0.001, hard_budget_usd=0.0)
        ledger = CostLedger(budget_policy=policy)
        assert not ledger.budget_hard_triggered

        ledger.record(input_tokens=1_000_000, output_tokens=1_000_000, model="o3")
        snapshot = ledger.snapshot()
        assert snapshot.budget_warn_triggered
        assert not snapshot.budget_hard_triggered

    def test_budget_hard_triggered(self) -> None:
        policy = BudgetPolicy(warn_budget_usd=0.001, hard_budget_usd=0.01)
        ledger = CostLedger(budget_policy=policy)

        ledger.record(input_tokens=1_000_000, output_tokens=1_000_000, model="o3")
        assert ledger.budget_hard_triggered
        snapshot = ledger.snapshot()
        assert snapshot.budget_hard_triggered

    def test_check_stage_budget(self) -> None:
        policy = BudgetPolicy(per_stage_budget_usd=0.001)
        ledger = CostLedger(budget_policy=policy)

        ledger.record(input_tokens=1_000_000, output_tokens=1_000_000, model="o3", stage_name="big")
        reason = ledger.check_stage_budget("big")
        assert reason is not None
        assert "big" in reason

        assert ledger.check_stage_budget("small") is None

    def test_check_stage_budget_disabled(self) -> None:
        ledger = CostLedger()
        ledger.record(input_tokens=1_000_000, output_tokens=1_000_000, model="o3", stage_name="big")
        assert ledger.check_stage_budget("big") is None

    def test_persist_and_load(self, tmp_path: Path) -> None:
        ledger = CostLedger()
        ledger.record(input_tokens=100, output_tokens=50, model="o3", stage_name="s1", agent_role="worker_a")

        artifact_path = tmp_path / "cost_ledger.json"
        ledger.persist(artifact_path)

        assert artifact_path.exists()
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert "snapshot" in payload
        assert "records" in payload
        assert len(payload["records"]) == 1
        assert payload["snapshot"]["invocation_count"] == 1
        assert payload["snapshot"]["total_input_tokens"] == 100

    def test_empty_ledger_snapshot(self) -> None:
        ledger = CostLedger()
        snapshot = ledger.snapshot()
        assert snapshot.invocation_count == 0
        assert snapshot.total_tokens == 0
        assert snapshot.estimated_cost_usd == 0.0
        assert snapshot.by_stage == {}
        assert snapshot.by_worker == {}
        assert snapshot.by_model == {}


class TestCodexExecUsage:
    def test_defaults(self) -> None:
        usage = CodexExecUsage()
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.total_tokens == 0
        assert usage.model == ""
        assert usage.latency_sec == 0.0

    def test_populated(self) -> None:
        usage = CodexExecUsage(
            input_tokens=1000,
            output_tokens=500,
            total_tokens=1500,
            model="o3",
            latency_sec=3.14,
        )
        assert usage.total_tokens == 1500
        assert usage.model == "o3"


class TestKickoffRunCodexExecContract:
    """Verify that kickoff() correctly unpacks the (text, usage) tuple from _run_codex_exec."""

    def test_kickoff_returns_result_with_usage(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.codex_exec_agent import (
            CodexExecAgent,
            CodexExecAgentConfig,
            CodexExecResult,
            CodexExecUsage,
        )

        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/codex")

        config = CodexExecAgentConfig(
            role="test_worker",
            model="o4-mini",
            workspace=tmp_path,
        )
        agent = CodexExecAgent(config)

        fake_usage = CodexExecUsage(
            input_tokens=400, output_tokens=200, total_tokens=600,
            model="o4-mini", latency_sec=1.5,
        )

        def fake_run_codex_exec(prompt: str, **kwargs: object) -> tuple[str, CodexExecUsage]:
            return ("hello from codex", fake_usage)

        monkeypatch.setattr(agent, "_run_codex_exec", fake_run_codex_exec)

        result = agent.kickoff("test prompt")

        assert isinstance(result, CodexExecResult)
        assert result.raw == "hello from codex"
        assert result.usage is not None
        assert result.usage.input_tokens == 400
        assert result.usage.output_tokens == 200
        assert result.usage.total_tokens == 600
        assert result.usage.model == "o4-mini"
        assert result.usage.latency_sec == 1.5

    def test_kickoff_no_usage_still_populates_result(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.codex_exec_agent import (
            CodexExecAgent,
            CodexExecAgentConfig,
            CodexExecResult,
            CodexExecUsage,
        )

        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/codex")

        config = CodexExecAgentConfig(
            role="test_worker",
            model="gpt-4.1",
            workspace=tmp_path,
        )
        agent = CodexExecAgent(config)

        zero_usage = CodexExecUsage(model="gpt-4.1", latency_sec=0.5)

        def fake_run_codex_exec(prompt: str, **kwargs: object) -> tuple[str, CodexExecUsage]:
            return ("just text no usage", zero_usage)

        monkeypatch.setattr(agent, "_run_codex_exec", fake_run_codex_exec)

        result = agent.kickoff("test prompt")

        assert isinstance(result, CodexExecResult)
        assert result.raw == "just text no usage"
        assert result.usage is not None
        assert result.usage.input_tokens == 0
        assert result.usage.output_tokens == 0
        assert result.usage.model == "gpt-4.1"

    def test_run_codex_exec_return_type_annotation(self) -> None:
        """Verify _run_codex_exec is annotated to return tuple, not str."""
        import inspect
        from agents.codex_exec_agent import CodexExecAgent

        sig = inspect.signature(CodexExecAgent._run_codex_exec)
        return_annotation = str(sig.return_annotation)
        assert "tuple" in return_annotation.lower(), (
            f"_run_codex_exec should return tuple[str, CodexExecUsage], got: {return_annotation}"
        )


class TestExtractUsageFromJsonOutput:
    def test_extracts_usage_from_response_completed(self) -> None:
        from agents.codex_exec_agent import CodexExecAgent

        json_output = "\n".join([
            json.dumps({
                "type": "response.completed",
                "response": {
                    "usage": {"input_tokens": 500, "output_tokens": 200}
                },
            }),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "hello world"},
            }),
            json.dumps({
                "type": "response.completed",
                "response": {
                    "usage": {"input_tokens": 300, "output_tokens": 100}
                },
            }),
        ])

        text, usage = CodexExecAgent._extract_assistant_text_and_usage(
            json_output, model="o3", elapsed_sec=5.0,
        )
        assert text == "hello world"
        assert usage.input_tokens == 800
        assert usage.output_tokens == 300
        assert usage.total_tokens == 1100
        assert usage.model == "o3"
        assert usage.latency_sec == 5.0

    def test_no_usage_events(self) -> None:
        from agents.codex_exec_agent import CodexExecAgent

        json_output = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "just text"},
        })

        text, usage = CodexExecAgent._extract_assistant_text_and_usage(
            json_output, model="gpt-4.1", elapsed_sec=1.0,
        )
        assert text == "just text"
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.model == "gpt-4.1"

    def test_malformed_lines_skipped(self) -> None:
        from agents.codex_exec_agent import CodexExecAgent

        json_output = "not json\n" + json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "ok"},
        })

        text, usage = CodexExecAgent._extract_assistant_text_and_usage(json_output)
        assert text == "ok"
        assert usage.total_tokens == 0


class TestResetStage:
    def test_reset_stage_clears_records(self) -> None:
        ledger = CostLedger()
        ledger.record(input_tokens=100, output_tokens=50, model="o3", stage_name="s1")
        ledger.record(input_tokens=200, output_tokens=100, model="o3", stage_name="s2")
        ledger.record(input_tokens=300, output_tokens=150, model="o3", stage_name="s1")

        assert ledger.snapshot().invocation_count == 3

        ledger.reset_stage("s1")

        snapshot = ledger.snapshot()
        assert snapshot.invocation_count == 1
        assert "s1" not in snapshot.by_stage
        assert "s2" in snapshot.by_stage

    def test_reset_stage_nonexistent_is_noop(self) -> None:
        ledger = CostLedger()
        ledger.record(input_tokens=100, output_tokens=50, model="o3", stage_name="s1")
        ledger.reset_stage("nonexistent")
        assert ledger.snapshot().invocation_count == 1

    def test_reset_stage_resets_per_stage_budget(self) -> None:
        policy = BudgetPolicy(per_stage_budget_usd=0.001)
        ledger = CostLedger(budget_policy=policy)
        ledger.record(input_tokens=1_000_000, output_tokens=1_000_000, model="o3", stage_name="big")

        assert ledger.check_stage_budget("big") is not None

        ledger.reset_stage("big")
        assert ledger.check_stage_budget("big") is None


class TestRestoreFromJson:
    def test_restore_loads_historical_records(self, tmp_path: Path) -> None:
        original = CostLedger()
        original.record(input_tokens=100, output_tokens=50, model="o3", stage_name="s1")
        original.record(input_tokens=200, output_tokens=100, model="o4-mini", stage_name="s2")
        artifact = tmp_path / "cost_ledger.json"
        original.persist(artifact)

        restored = CostLedger()
        restored.restore_from_json(artifact)

        snapshot = restored.snapshot()
        assert snapshot.invocation_count == 2
        assert snapshot.total_input_tokens == 300
        assert "s1" in snapshot.by_stage
        assert "s2" in snapshot.by_stage

    def test_restore_nonexistent_file_is_noop(self, tmp_path: Path) -> None:
        ledger = CostLedger()
        ledger.restore_from_json(tmp_path / "nonexistent.json")
        assert ledger.snapshot().invocation_count == 0

    def test_restore_malformed_json_is_noop(self, tmp_path: Path) -> None:
        artifact = tmp_path / "bad.json"
        artifact.write_text("not json", encoding="utf-8")
        ledger = CostLedger()
        ledger.restore_from_json(artifact)
        assert ledger.snapshot().invocation_count == 0

    def test_restore_triggers_budget_check(self, tmp_path: Path) -> None:
        original = CostLedger()
        original.record(input_tokens=1_000_000, output_tokens=1_000_000, model="o3")
        artifact = tmp_path / "cost_ledger.json"
        original.persist(artifact)

        policy = BudgetPolicy(hard_budget_usd=0.01)
        restored = CostLedger(budget_policy=policy)
        restored.restore_from_json(artifact)

        assert restored.budget_hard_triggered


class TestBudgetExhaustedError:
    def test_immediate_abort_raises_when_budget_exceeded(self) -> None:
        from state.cost_ledger import BudgetExhaustedError

        policy = BudgetPolicy(
            hard_budget_usd=0.01,
            hard_budget_enforcement="immediate_abort",
        )
        ledger = CostLedger(budget_policy=policy)
        ledger.record(input_tokens=1_000_000, output_tokens=1_000_000, model="o3")

        assert ledger.budget_hard_triggered
        with pytest.raises(BudgetExhaustedError, match="immediate_abort"):
            ledger.check_budget_for_agent_call()

    def test_stage_boundary_does_not_raise(self) -> None:
        policy = BudgetPolicy(
            hard_budget_usd=0.01,
            hard_budget_enforcement="stage_boundary",
        )
        ledger = CostLedger(budget_policy=policy)
        ledger.record(input_tokens=1_000_000, output_tokens=1_000_000, model="o3")

        assert ledger.budget_hard_triggered
        # Should NOT raise — stage_boundary enforcement only blocks at stage boundaries.
        ledger.check_budget_for_agent_call()

    def test_immediate_abort_no_raise_when_under_budget(self) -> None:
        policy = BudgetPolicy(
            hard_budget_usd=999.0,
            hard_budget_enforcement="immediate_abort",
        )
        ledger = CostLedger(budget_policy=policy)
        ledger.record(input_tokens=100, output_tokens=50, model="o3")

        assert not ledger.budget_hard_triggered
        ledger.check_budget_for_agent_call()


class TestBudgetPolicyNewFields:
    def test_default_values(self) -> None:
        policy = BudgetPolicy()
        assert policy.hard_budget_enforcement == "stage_boundary"
        assert policy.run_budget_mode == "run_only"

    def test_custom_values(self) -> None:
        policy = BudgetPolicy(
            hard_budget_enforcement="immediate_abort",
            run_budget_mode="include_resume_history",
        )
        assert policy.hard_budget_enforcement == "immediate_abort"
        assert policy.run_budget_mode == "include_resume_history"
