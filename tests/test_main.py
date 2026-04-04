from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from engine.flow import BlockingDecisionRequired
from core.models import RunSummary, StageSpec


main_module = importlib.import_module("app.main")


class _DummyFlow:
    def __init__(self, *, result: object = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.state = type("State", (), {"stage_artifacts": {}})()

    def kickoff(self) -> object:
        if self.exc is not None:
            raise self.exc
        return self.result


def _stage() -> StageSpec:
    return StageSpec(
        name="stage-a",
        objective="obj",
        acceptance_criteria=["done"],
        invariants=["safe"],
    )


def test_main_monitor_subcommand(monkeypatch, tmp_path: Path) -> None:
    called: dict[str, object] = {}

    def _write_monitor_html(*, runtime_dir: Path, output_html: Path | None) -> Path:
        called["runtime_dir"] = runtime_dir
        called["output_html"] = output_html
        return runtime_dir / "monitor.html"

    monkeypatch.setattr(main_module, "write_monitor_html", _write_monitor_html)
    monkeypatch.setattr("sys.argv", ["leave-me-alone", "monitor", "--runtime-dir", str(tmp_path)])

    main_module.main()

    assert called["runtime_dir"] == tmp_path
    assert called["output_html"] is None


def test_main_monitor_subcommand_serve(monkeypatch, tmp_path: Path) -> None:
    called: dict[str, object] = {}

    def _serve_monitor(*, runtime_dir: Path, host: str, port: int, refresh_sec: float) -> None:
        called["runtime_dir"] = runtime_dir
        called["host"] = host
        called["port"] = port
        called["refresh_sec"] = refresh_sec

    monkeypatch.setattr(main_module, "serve_monitor", _serve_monitor)
    monkeypatch.setattr(
        main_module,
        "write_monitor_html",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("write_monitor_html should not be called")),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "leave-me-alone",
            "monitor",
            "--runtime-dir",
            str(tmp_path),
            "--serve",
            "--host",
            "0.0.0.0",
            "--port",
            "9001",
            "--refresh-sec",
            "1.5",
        ],
    )

    main_module.main()

    assert called["runtime_dir"] == tmp_path
    assert called["host"] == "0.0.0.0"
    assert called["port"] == 9001
    assert called["refresh_sec"] == 1.5


def test_main_maps_cli_args_to_runtime_config(monkeypatch, tmp_path: Path) -> None:
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    stages_file = tmp_path / "stages.json"
    stages_file.write_text("[]", encoding="utf-8")
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()

    captured: dict[str, object] = {}

    def _parse_stage_specs(path: Path) -> list[StageSpec]:
        assert path == stages_file.resolve()
        return [_stage()]

    class _Manager:
        def __init__(self, *, target_repo: Path, runtime_dir: Path) -> None:
            captured["manager_target_repo"] = target_repo
            captured["manager_runtime_dir"] = runtime_dir

    def _create_agent_bundle(cfg, manager):
        captured["cfg"] = cfg
        captured["manager"] = manager
        return "agents"

    def _build_flow(*, cfg, agents, workspace_manager, stages):
        captured["flow_cfg"] = cfg
        captured["flow_agents"] = agents
        captured["flow_manager"] = workspace_manager
        captured["flow_stages"] = stages
        return _DummyFlow(
            result=RunSummary(
                target_repo=str(cfg.target_repo),
                overall_passed=True,
                stage_results=[],
            )
        )

    monkeypatch.setattr(main_module, "parse_stage_specs", _parse_stage_specs)
    monkeypatch.setattr(main_module, "WorkspaceManager", _Manager)
    monkeypatch.setattr(main_module, "create_agent_bundle", _create_agent_bundle)
    monkeypatch.setattr(main_module, "build_flow", _build_flow)
    monkeypatch.setattr(main_module, "load_stage_artifacts", lambda seed: {"seed": str(seed)})
    monkeypatch.setattr(
        "sys.argv",
        [
            "leave-me-alone",
            "--target-repo",
            str(target_repo),
            "--stages-file",
            str(stages_file),
            "--runtime-dir",
            str(tmp_path / "runtime"),
            "--seed-artifacts-dir",
            str(seed_dir),
            "--output-file",
            "out.json",
            "--model",
            "gpt-5.4",
            "--sandbox-mode",
            "danger-full-access",
            "--max-round-per-stage",
            "0",
            "--remote-host",
            "node0",
            "--remote-workdir",
            "/enjia/repo",
            "--split-worker-remote-hosts",
            "--auto-approve-decisions",
            "foo, bar",
            "--owner-worker",
            "worker_b",
            "--triage-allow-empty-reject-rationale",
            "--promotion-allow-failing-checks",
            "--drift-allow-extra-commands",
        ],
    )

    main_module.main()

    cfg = captured["cfg"]
    assert cfg.target_repo == target_repo.resolve()
    assert cfg.stages_file == stages_file.resolve()
    assert cfg.seed_artifacts_dir == seed_dir.resolve()
    assert cfg.output_file == (tmp_path / "runtime" / "out.json").resolve()
    assert cfg.model == "gpt-5.4"
    assert cfg.sandbox_mode == "danger-full-access"
    assert cfg.max_round_per_stage == 1
    assert cfg.remote_host == "node0"
    assert cfg.remote_workdir == "/enjia/repo"
    assert cfg.remote_workdir_node1 == "/enjia/repo"
    assert cfg.split_worker_remote_hosts is True
    assert cfg.auto_approve_decisions == ["foo", "bar"]
    assert cfg.owner_worker == "worker_b"
    assert cfg.triage_require_reject_rationale is False
    assert cfg.promotion_require_all_checks is False
    assert cfg.drift_fail_on_extra_commands is False
    assert captured["flow_agents"] == "agents"
    assert captured["flow_stages"] == [_stage()]


def test_main_exits_2_on_blocking_decision(monkeypatch, tmp_path: Path) -> None:
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    stages_file = tmp_path / "stages.json"
    stages_file.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(main_module, "parse_stage_specs", lambda path: [_stage()])
    monkeypatch.setattr(main_module, "WorkspaceManager", lambda **kwargs: object())
    monkeypatch.setattr(main_module, "create_agent_bundle", lambda cfg, manager: object())
    monkeypatch.setattr(main_module, "build_flow", lambda **kwargs: _DummyFlow(exc=BlockingDecisionRequired("stage-a", ["need_human"])))
    monkeypatch.setattr(main_module, "load_stage_artifacts", lambda seed: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "leave-me-alone",
            "--target-repo",
            str(target_repo),
            "--stages-file",
            str(stages_file),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 2


def test_main_exits_130_on_keyboard_interrupt(monkeypatch, tmp_path: Path) -> None:
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    stages_file = tmp_path / "stages.json"
    stages_file.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(main_module, "parse_stage_specs", lambda path: [_stage()])
    monkeypatch.setattr(main_module, "WorkspaceManager", lambda **kwargs: object())
    monkeypatch.setattr(main_module, "create_agent_bundle", lambda cfg, manager: object())
    monkeypatch.setattr(main_module, "build_flow", lambda **kwargs: _DummyFlow(exc=KeyboardInterrupt()))
    monkeypatch.setattr(main_module, "load_stage_artifacts", lambda seed: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "leave-me-alone",
            "--target-repo",
            str(target_repo),
            "--stages-file",
            str(stages_file),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 130


def test_main_rejects_non_codex_provider_at_parser_layer(monkeypatch, tmp_path: Path) -> None:
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    stages_file = tmp_path / "stages.json"
    stages_file.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        [
            "leave-me-alone",
            "--target-repo",
            str(target_repo),
            "--stages-file",
            str(stages_file),
            "--provider",
            "opencode",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 2


def test_main_exits_1_when_summary_fails(monkeypatch, tmp_path: Path) -> None:
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    stages_file = tmp_path / "stages.json"
    stages_file.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(main_module, "parse_stage_specs", lambda path: [_stage()])
    monkeypatch.setattr(main_module, "WorkspaceManager", lambda **kwargs: object())
    monkeypatch.setattr(main_module, "create_agent_bundle", lambda cfg, manager: object())
    monkeypatch.setattr(
        main_module,
        "build_flow",
        lambda **kwargs: _DummyFlow(
            result=RunSummary(
                target_repo=str(target_repo.resolve()),
                overall_passed=False,
                stage_results=[],
            )
        ),
    )
    monkeypatch.setattr(main_module, "load_stage_artifacts", lambda seed: {})
    monkeypatch.setattr(
        "sys.argv",
        [
            "leave-me-alone",
            "--target-repo",
            str(target_repo),
            "--stages-file",
            str(stages_file),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 1


def test_main_fails_fast_on_invalid_layered_policy_file(monkeypatch, tmp_path: Path) -> None:
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    stages_file = tmp_path / "stages.json"
    stages_file.write_text("[]", encoding="utf-8")
    layered_policy_file = tmp_path / "layered_policy.json"
    layered_policy_file.write_text('{"schema_version": 999}', encoding="utf-8")

    monkeypatch.setattr(
        main_module,
        "build_flow",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("build_flow should not be called")),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "leave-me-alone",
            "--target-repo",
            str(target_repo),
            "--stages-file",
            str(stages_file),
            "--layered-policy-file",
            str(layered_policy_file),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 2


def test_main_resume_uses_runtime_artifacts_and_reuses_passed_stage_prefix(
    monkeypatch, tmp_path: Path
) -> None:
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "artifacts").mkdir()
    stages_file = tmp_path / "stages.json"
    stages_file.write_text("[]", encoding="utf-8")

    previous_summary = {
        "target_repo": str(target_repo.resolve()),
        "overall_passed": False,
        "stage_results": [
            {
                "stage_name": "stage-a",
                "passed": True,
                "rounds_used": 1,
                "gate": {
                    "stage_name": "stage-a",
                    "round_index": 1,
                    "pass_gate": True,
                    "high_severity_open": [],
                    "disputed_items": [],
                    "required_actions": [],
                    "rationale": "ok",
                },
            },
            {
                "stage_name": "stage-b",
                "passed": False,
                "rounds_used": 1,
                "gate": {
                    "stage_name": "stage-b",
                    "round_index": 1,
                    "pass_gate": False,
                    "high_severity_open": [],
                    "disputed_items": [],
                    "required_actions": ["fix"],
                    "rationale": "failed",
                },
            },
        ],
    }
    (runtime_dir / "review-summary.json").write_text(
        json.dumps(previous_summary),
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        main_module,
        "parse_stage_specs",
        lambda path: [
            _stage(),
            StageSpec(
                name="stage-b",
                objective="obj-b",
                acceptance_criteria=["done"],
                invariants=["safe"],
            ),
        ],
    )
    monkeypatch.setattr(main_module, "WorkspaceManager", lambda **kwargs: object())
    monkeypatch.setattr(main_module, "create_agent_bundle", lambda cfg, manager: object())

    def _build_flow(**kwargs):
        captured["cfg"] = kwargs["cfg"]
        flow = _DummyFlow(
            result=RunSummary(
                target_repo=str(target_repo.resolve()),
                overall_passed=True,
                stage_results=[],
            )
        )
        captured["flow"] = flow
        return flow

    monkeypatch.setattr(main_module, "build_flow", _build_flow)
    monkeypatch.setattr(
        main_module,
        "load_stage_artifacts",
        lambda seed: {"seed": str(seed)},
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "leave-me-alone",
            "--target-repo",
            str(target_repo),
            "--stages-file",
            str(stages_file),
            "--runtime-dir",
            str(runtime_dir),
            "--resume",
        ],
    )

    main_module.main()

    cfg = captured["cfg"]
    flow = captured["flow"]
    assert cfg.seed_artifacts_dir == (runtime_dir / "artifacts").resolve()
    assert flow.state.stage_artifacts == {"seed": str((runtime_dir / "artifacts").resolve())}
    assert flow.state.resume_passed_stage_names == ["stage-a"]
    assert [item.stage_name for item in flow.state.resumed_stage_results] == ["stage-a"]


def test_main_resume_fails_closed_on_target_repo_mismatch(
    monkeypatch, tmp_path: Path
) -> None:
    target_repo = tmp_path / "repo"
    target_repo.mkdir()
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "artifacts").mkdir()
    stages_file = tmp_path / "stages.json"
    stages_file.write_text("[]", encoding="utf-8")

    previous_summary = {
        "target_repo": str(other_repo.resolve()),
        "overall_passed": False,
        "stage_results": [],
    }
    (runtime_dir / "review-summary.json").write_text(
        json.dumps(previous_summary),
        encoding="utf-8",
    )

    monkeypatch.setattr(main_module, "parse_stage_specs", lambda path: [_stage()])
    monkeypatch.setattr(main_module, "WorkspaceManager", lambda **kwargs: object())
    monkeypatch.setattr(main_module, "create_agent_bundle", lambda cfg, manager: object())
    monkeypatch.setattr(
        main_module,
        "build_flow",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("build_flow should not be called")),
    )
    monkeypatch.setattr(
        main_module,
        "load_stage_artifacts",
        lambda seed: (_ for _ in ()).throw(AssertionError("load_stage_artifacts should not be called")),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "leave-me-alone",
            "--target-repo",
            str(target_repo),
            "--stages-file",
            str(stages_file),
            "--runtime-dir",
            str(runtime_dir),
            "--resume",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        main_module.main()

    assert exc.value.code == 2


class TestContextBudgetCLIArg:
    """Integration tests for --context-budget-max-chars CLI parameter mapping."""

    def test_default_context_budget_max_chars(self, tmp_path: Path) -> None:
        parser = main_module.build_run_parser()
        target_repo = tmp_path / "repo"
        target_repo.mkdir()
        stages_file = tmp_path / "stages.json"
        stages_file.write_text("[]", encoding="utf-8")
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        args = parser.parse_args([
            "--target-repo", str(target_repo),
            "--stages-file", str(stages_file),
            "--runtime-dir", str(runtime_dir),
        ])
        assert args.context_budget_max_chars == 80_000

    def test_custom_context_budget_max_chars(self, tmp_path: Path) -> None:
        parser = main_module.build_run_parser()
        target_repo = tmp_path / "repo"
        target_repo.mkdir()
        stages_file = tmp_path / "stages.json"
        stages_file.write_text("[]", encoding="utf-8")
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        args = parser.parse_args([
            "--target-repo", str(target_repo),
            "--stages-file", str(stages_file),
            "--runtime-dir", str(runtime_dir),
            "--context-budget-max-chars", "50000",
        ])
        assert args.context_budget_max_chars == 50_000

    def test_hard_budget_enforcement_choices(self, tmp_path: Path) -> None:
        parser = main_module.build_run_parser()
        target_repo = tmp_path / "repo"
        target_repo.mkdir()
        stages_file = tmp_path / "stages.json"
        stages_file.write_text("[]", encoding="utf-8")
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        args = parser.parse_args([
            "--target-repo", str(target_repo),
            "--stages-file", str(stages_file),
            "--runtime-dir", str(runtime_dir),
            "--hard-budget-enforcement", "immediate_abort",
        ])
        assert args.hard_budget_enforcement == "immediate_abort"

    def test_run_budget_mode_choices(self, tmp_path: Path) -> None:
        parser = main_module.build_run_parser()
        target_repo = tmp_path / "repo"
        target_repo.mkdir()
        stages_file = tmp_path / "stages.json"
        stages_file.write_text("[]", encoding="utf-8")
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        args = parser.parse_args([
            "--target-repo", str(target_repo),
            "--stages-file", str(stages_file),
            "--runtime-dir", str(runtime_dir),
            "--run-budget-mode", "include_resume_history",
        ])
        assert args.run_budget_mode == "include_resume_history"

    def test_layered_policy_file_arg(self, tmp_path: Path) -> None:
        parser = main_module.build_run_parser()
        target_repo = tmp_path / "repo"
        target_repo.mkdir()
        stages_file = tmp_path / "stages.json"
        stages_file.write_text("[]", encoding="utf-8")
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        layered_policy = tmp_path / "layered_policy.json"
        layered_policy.write_text("{}", encoding="utf-8")
        args = parser.parse_args([
            "--target-repo", str(target_repo),
            "--stages-file", str(stages_file),
            "--runtime-dir", str(runtime_dir),
            "--layered-policy-file", str(layered_policy),
        ])
        assert args.layered_policy_file == str(layered_policy)


class TestRuntimeConfigEndToEndMapping:
    """Verify CLI args map correctly to RuntimeConfig fields (end-to-end)."""

    @staticmethod
    def _make_cfg(tmp_path: Path, **overrides: object) -> "RuntimeConfig":
        from app.runtime_config import RuntimeConfig

        target_repo = tmp_path / "repo"
        target_repo.mkdir(exist_ok=True)
        stages_file = tmp_path / "stages.json"
        stages_file.write_text("[]", encoding="utf-8")
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir(exist_ok=True)

        defaults: dict[str, object] = dict(
            target_repo=target_repo,
            stages_file=stages_file,
            runtime_dir=runtime_dir,
            seed_artifacts_dir=None,
            model="o3",
            sandbox_mode="none",
            enable_a2a=False,
            a2a_endpoints={},
            max_round_per_stage=3,
            output_file=runtime_dir / "output.json",
        )
        defaults.update(overrides)
        return RuntimeConfig(**defaults)

    def test_budget_fields_map_to_runtime_config(self, tmp_path: Path) -> None:
        cfg = self._make_cfg(
            tmp_path,
            warn_budget_usd=1.5,
            hard_budget_usd=5.0,
            per_stage_budget_usd=0.5,
            hard_budget_enforcement="immediate_abort",
            run_budget_mode="include_resume_history",
            context_budget_max_chars=50_000,
        )
        assert cfg.warn_budget_usd == 1.5
        assert cfg.hard_budget_usd == 5.0
        assert cfg.per_stage_budget_usd == 0.5
        assert cfg.hard_budget_enforcement == "immediate_abort"
        assert cfg.run_budget_mode == "include_resume_history"
        assert cfg.context_budget_max_chars == 50_000

    def test_default_budget_fields(self, tmp_path: Path) -> None:
        cfg = self._make_cfg(tmp_path)
        assert cfg.warn_budget_usd == 0.0
        assert cfg.hard_budget_usd == 0.0
        assert cfg.per_stage_budget_usd == 0.0
        assert cfg.hard_budget_enforcement == "stage_boundary"
        assert cfg.run_budget_mode == "run_only"
        assert cfg.context_budget_max_chars == 80_000
        assert cfg.layered_policy_file is None

    def test_layered_policy_file_runtime_config_mapping(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "layered_policy.json"
        policy_file.write_text("{}", encoding="utf-8")
        cfg = self._make_cfg(
            tmp_path,
            layered_policy_file=policy_file,
        )
        assert cfg.layered_policy_file == policy_file
