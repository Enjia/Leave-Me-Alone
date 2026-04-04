from __future__ import annotations

from app.monitor import build_monitor_view, render_monitor_html


def test_build_monitor_view_summarizes_pipeline_and_agents() -> None:
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {
            "target_repo": "/tmp/repo",
            "overall_state": "running",
            "current_stage": "stage-a",
            "current_round": 1,
            "phase": "round_start",
            "worker_states": {"worker_a": "planning", "worker_b": "implementing"},
            "judge_state": "waiting_for_worker_plan",
        },
        "summary": {"overall_passed": None},
        "stage_dashboards": [
            {
                "stage_name": "stage-a",
                "objective": "实现 A",
                "status": "running",
                "current_round": 1,
                "max_rounds": 3,
                "passed_gates": ["stage_initialized", "remote_preflight", "stage_gate"],
                "worker_states": {"worker_a": "planning", "worker_b": "implementing"},
                "judge_state": "waiting_for_worker_plan",
            },
            {
                "stage_name": "stage-b",
                "objective": "实现 B",
                "status": "pending",
                "current_round": 0,
                "max_rounds": 2,
            },
        ],
    }

    view = build_monitor_view(payload)

    assert view["repo"] == "/tmp/repo"
    assert view["current"]["stage_name"] == "stage-a"
    assert view["current"]["phase_label"] == "本轮规划"
    assert view["pipeline"]["total"] == 2
    assert view["agents"]["worker_a"]["state"] == "planning"
    assert view["agents"]["worker_b"]["state"] == "implementing"
    assert view["current"]["passed_gates"] == ["stage_initialized", "remote_preflight", "stage_gate"]
    assert view["stages"][0]["passed_gates"] == ["stage_initialized", "remote_preflight", "stage_gate"]


def test_build_monitor_view_marks_round_two_as_repair_cycle() -> None:
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {
            "target_repo": "/tmp/repo",
            "overall_state": "running",
            "current_stage": "stage-a",
            "current_round": 2,
            "phase": "round_start",
            "worker_states": {"worker_a": "planning", "worker_b": "planning"},
            "judge_state": "waiting_for_worker_plan",
        },
        "summary": {},
        "stage_dashboards": [
            {
                "stage_name": "stage-a",
                "objective": "实现 A",
                "status": "running",
                "current_round": 2,
                "max_rounds": 2,
                "worker_states": {"worker_a": "planning", "worker_b": "planning"},
                "judge_state": "waiting_for_worker_plan",
                "latest_check_overview": {"worker_a": "post_triage:fail:2"},
                "latest_nudges": ["worker_a:todo_enforcement:warning:Return to approved plan scope."],
                "latest_convergence_action": "retry",
                "unresolved_actions": ["Fix all compile/runtime blockers so both required remote commands pass."],
            }
        ],
    }

    view = build_monitor_view(payload)

    assert view["current"]["phase"] == "repair_round"
    assert view["current"]["phase_label"] == "第 2 轮修复"
    active_nodes = [node["id"] for node in view["pipeline_nodes"] if node["state"] == "active"]
    assert active_nodes == ["implementation"]


def test_build_monitor_view_advances_stale_plan_gate_phase_when_workers_are_implementing() -> None:
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {
            "target_repo": "/tmp/repo",
            "overall_state": "running",
            "current_stage": "stage-a",
            "current_round": 1,
            "phase": "plan_gate_review",
            "worker_states": {"worker_a": "implementing", "worker_b": "implementing"},
            "judge_state": "plan_approved",
        },
        "summary": {},
        "stage_dashboards": [
            {
                "stage_name": "stage-a",
                "objective": "实现 A",
                "status": "running",
                "current_round": 1,
                "max_rounds": 2,
                "phase": "plan_gate_review",
                "worker_states": {"worker_a": "implementing", "worker_b": "implementing"},
                "judge_state": "plan_approved",
            }
        ],
    }

    view = build_monitor_view(payload)

    assert view["current"]["phase"] == "implementation"
    assert view["current"]["phase_label"] == "编码实现"
    active_nodes = [node["id"] for node in view["pipeline_nodes"] if node["state"] == "active"]
    assert active_nodes == ["implementation"]


def test_build_monitor_view_summarizes_focus_items_and_lists_all_stages() -> None:
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {
            "target_repo": "/tmp/repo",
            "overall_state": "running",
            "current_stage": "stage-b",
            "current_round": 2,
        },
        "summary": {},
        "stage_dashboards": [
            {
                "stage_name": "stage-b",
                "objective": "实现 B",
                "status": "running",
                "current_round": 2,
                "max_rounds": 3,
                "latest_nudges": [
                    "worker_a:todo_enforcement:warning:Return to approved plan scope and verification obligations before broadening implementation. -> Only touch in-plan files and execute the promised verification steps first."
                ],
                "unresolved_actions": [
                    "Fix all compile/runtime blockers so both required remote commands pass with exit 0."
                ],
            }
        ],
        "stage_dag_plan": {
            "serial_execution_order": ["stage-a", "stage-b", "stage-c"],
            "nodes": [
                {"stage_name": "stage-a", "objective": "实现 A"},
                {"stage_name": "stage-b", "objective": "实现 B"},
                {"stage_name": "stage-c", "objective": "实现 C"},
            ],
        },
    }

    view = build_monitor_view(payload)

    assert [item["name"] for item in view["stage_roadmap"]] == ["stage-a", "stage-b", "stage-c"]
    assert view["current"]["stage_index"] == 2
    assert view["focus_items"][0]["summary"] == "Worker A 偏离已批准方案，需要先回到既定修复范围"
    assert "todo_enforcement" in view["focus_items"][0]["detail"]
    assert view["focus_items"][1]["summary"] == "先修复编译/运行阻断，恢复核心远端命令通过"
    assert view["stages"][0]["highlight"] == "Worker A 偏离已批准方案，需要先回到既定修复范围"
    assert view["stages"][0]["is_current"] is True


def test_build_monitor_view_uses_stage_progress_ledger_as_passed_gate_fallback() -> None:
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {
            "target_repo": "/tmp/repo",
            "overall_state": "running",
            "current_stage": "stage-b",
            "current_round": 2,
        },
        "summary": {},
        "stage_dashboards": [
            {
                "stage_name": "stage-b",
                "objective": "实现 B",
                "status": "running",
                "current_round": 2,
                "max_rounds": 3,
            }
        ],
        "stage_progress_ledgers": {
            "stage-b": {
                "stage_name": "stage-b",
                "status": "running",
                "passed_gates": [
                    "stage_initialized",
                    "remote_preflight",
                    "stage_gate",
                    "plan_gate",
                    "pre_promotion",
                ],
            }
        },
    }

    view = build_monitor_view(payload)

    assert view["current"]["passed_gates"] == [
        "stage_initialized",
        "remote_preflight",
        "stage_gate",
        "plan_gate",
        "pre_promotion",
    ]
    assert view["stages"][0]["passed_gates"] == [
        "stage_initialized",
        "remote_preflight",
        "stage_gate",
        "plan_gate",
        "pre_promotion",
    ]


def test_build_monitor_view_exposes_active_remote_checks_for_current_stage() -> None:
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {
            "target_repo": "/tmp/repo",
            "overall_state": "running",
            "current_stage": "stage-b",
            "current_round": 2,
        },
        "summary": {},
        "stage_dashboards": [
            {
                "stage_name": "stage-b",
                "objective": "实现 B",
                "status": "running",
                "current_round": 2,
                "max_rounds": 3,
            }
        ],
        "remote_check_heartbeats": {
            "active": [
                {
                    "heartbeat_id": "hb-a",
                    "stage_name": "stage-a",
                    "round_index": 1,
                    "worker": "worker_a",
                    "gate_tier": "fast_round",
                    "remote_host": "node0",
                    "command": "echo a",
                    "command_index": 1,
                    "command_total": 1,
                    "status": "running",
                    "elapsed_sec": 12,
                    "timeout_sec": 120,
                },
                {
                    "heartbeat_id": "hb-b",
                    "stage_name": "stage-b",
                    "round_index": 2,
                    "worker": "worker_b",
                    "gate_tier": "pre_promotion",
                    "remote_host": "node1",
                    "command": "python3 tests/smoke_suite.py --scenario all",
                    "command_index": 2,
                    "command_total": 3,
                    "status": "running",
                    "elapsed_sec": 180,
                    "timeout_sec": 600,
                },
            ]
        },
    }

    view = build_monitor_view(payload)

    assert len(view["active_remote_checks"]) == 1
    check = view["active_remote_checks"][0]
    assert check["stage_name"] == "stage-b"
    assert check["worker"] == "worker_b"
    assert check["gate_tier"] == "pre_promotion"
    assert check["progress"] == 30


def test_build_monitor_view_filters_stale_active_remote_checks(monkeypatch) -> None:
    monkeypatch.setenv("MULTI_CODEX_REMOTE_HEARTBEAT_STALE_TTL_SEC", "30")
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {
            "target_repo": "/tmp/repo",
            "overall_state": "running",
            "current_stage": "stage-a",
            "current_round": 1,
        },
        "summary": {},
        "stage_dashboards": [
            {
                "stage_name": "stage-a",
                "objective": "实现 A",
                "status": "running",
                "current_round": 1,
                "max_rounds": 2,
            }
        ],
        "remote_check_heartbeats": {
            "active": [
                {
                    "heartbeat_id": "hb-stale",
                    "stage_name": "stage-a",
                    "round_index": 1,
                    "worker": "worker_a",
                    "gate_tier": "fast_round",
                    "remote_host": "node0",
                    "command": "echo stale",
                    "status": "running",
                    "elapsed_sec": 120,
                    "timeout_sec": 300,
                    "updated_at_epoch_sec": 1,
                }
            ]
        },
    }

    view = build_monitor_view(payload)
    assert view["active_remote_checks"] == []


def test_build_monitor_view_includes_timeout_recovery_in_activity_feed() -> None:
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {
            "target_repo": "/tmp/repo",
            "overall_state": "running",
            "current_stage": "stage-a",
            "current_round": 1,
        },
        "summary": {},
        "stage_dashboards": [
            {
                "stage_name": "stage-a",
                "objective": "实现 A",
                "status": "running",
                "current_round": 1,
                "max_rounds": 2,
            }
        ],
        "remote_check_heartbeats": {
            "recent": [
                {
                    "event": "timeout_recovery",
                    "stage_name": "stage-a",
                    "worker": "worker_a",
                    "gate_tier": "pre_promotion",
                    "command": "python3 tests/smoke_suite.py --scenario all",
                    "recovery": {
                        "recovered": False,
                        "summary": "remote timeout recovery failed: some remote processes may still be running.",
                    },
                }
            ]
        },
    }

    view = build_monitor_view(payload)
    assert any(
        item.get("type") == "timeout_recovery" and "进程清理未确认成功" in str(item.get("summary", ""))
        for item in view["activity_feed"]
    )
    assert view["timeout_recovery"]["attempted"] == 0


def test_build_monitor_view_exposes_timeout_recovery_summary_counts() -> None:
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {
            "target_repo": "/tmp/repo",
            "overall_state": "running",
            "current_stage": "stage-a",
            "current_round": 1,
        },
        "summary": {},
        "stage_dashboards": [
            {
                "stage_name": "stage-a",
                "objective": "实现 A",
                "status": "running",
                "current_round": 1,
                "max_rounds": 2,
            }
        ],
        "timeout_recovery_summary": {
            "updated_at_epoch_sec": 123,
            "totals": {
                "timeout_recovery_attempted": 4,
                "timeout_recovery_recovered": 3,
                "timeout_recovery_failed": 1,
                "stale_recycled": 2,
            },
            "recent_events": [
                {
                    "event": "timeout_recovery",
                    "stage_name": "stage-a",
                    "worker": "worker_a",
                    "gate_tier": "pre_promotion",
                    "status": "recovered",
                    "detail": "remote timeout recovery succeeded",
                    "updated_at_epoch_sec": 123,
                }
            ],
        },
    }

    view = build_monitor_view(payload)
    assert view["timeout_recovery"]["attempted"] == 4
    assert view["timeout_recovery"]["recovered"] == 3
    assert view["timeout_recovery"]["failed"] == 1
    assert view["timeout_recovery"]["stale_recycled"] == 2
    assert view["timeout_recovery"]["thresholds"]["min_attempts_for_rate"] == 3
    assert view["timeout_recovery"]["thresholds"]["failure_rate_pct"] == 50
    assert view["timeout_recovery"]["thresholds"]["consecutive_failures"] == 2
    assert view["timeout_recovery"]["thresholds"]["stale_recycled"] == 10
    assert view["timeout_recovery"]["recent_events"][0]["event"] == "timeout_recovery"


def test_build_monitor_view_marks_timeout_recovery_alerts(monkeypatch) -> None:
    monkeypatch.setenv("MULTI_CODEX_TIMEOUT_RECOVERY_ALERT_MIN_ATTEMPTS", "3")
    monkeypatch.setenv("MULTI_CODEX_TIMEOUT_RECOVERY_ALERT_FAILURE_RATE_PCT", "50")
    monkeypatch.setenv("MULTI_CODEX_TIMEOUT_RECOVERY_ALERT_CONSECUTIVE_FAILURES", "2")
    monkeypatch.setenv("MULTI_CODEX_TIMEOUT_RECOVERY_ALERT_STALE_RECYCLED", "10")
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {
            "target_repo": "/tmp/repo",
            "overall_state": "running",
            "current_stage": "stage-a",
            "current_round": 2,
        },
        "summary": {},
        "stage_dashboards": [
            {
                "stage_name": "stage-a",
                "objective": "实现 A",
                "status": "running",
                "current_round": 2,
                "max_rounds": 3,
            }
        ],
        "timeout_recovery_summary": {
            "updated_at_epoch_sec": 123,
            "totals": {
                "timeout_recovery_attempted": 4,
                "timeout_recovery_recovered": 1,
                "timeout_recovery_failed": 3,
                "stale_recycled": 12,
            },
            "recent_events": [
                {
                    "event": "timeout_recovery",
                    "status": "cleanup_failed",
                    "stage_name": "stage-a",
                    "worker": "worker_a",
                    "gate_tier": "pre_promotion",
                    "detail": "first failed recovery",
                    "updated_at_epoch_sec": 120,
                },
                {
                    "event": "timeout_recovery",
                    "status": "cleanup_failed",
                    "stage_name": "stage-a",
                    "worker": "worker_b",
                    "gate_tier": "pre_promotion",
                    "detail": "second failed recovery",
                    "updated_at_epoch_sec": 121,
                },
            ],
        },
    }

    view = build_monitor_view(payload)
    overview = view["timeout_recovery"]
    assert overview["is_alerting"] is True
    assert overview["failure_rate_pct"] == 75
    assert overview["thresholds"]["failure_rate_pct"] == 50
    assert overview["thresholds"]["consecutive_failures"] == 2
    assert overview["consecutive_failed_recoveries"] == 2
    alert_codes = {item["code"] for item in overview["alerts"]}
    assert "timeout_recovery_failure_rate" in alert_codes
    assert "timeout_recovery_consecutive_failures" in alert_codes
    assert "timeout_recovery_stale_recycled_high" in alert_codes


def test_build_monitor_view_surfaces_timeout_recovery_budget_exhausted_block() -> None:
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {
            "target_repo": "/tmp/repo",
            "overall_state": "blocked",
            "current_stage": "stage-a",
            "current_round": 2,
            "phase": "pre_promotion_timeout_blocked",
            "worker_states": {"worker_a": "blocked", "worker_b": "blocked"},
            "judge_state": "blocked_on_timeout_recovery",
        },
        "summary": {},
        "stage_dashboards": [
            {
                "stage_name": "stage-a",
                "objective": "实现 A",
                "status": "blocked",
                "current_round": 2,
                "max_rounds": 3,
                "latest_failure_codes": ["pre_promotion_timeout_recovery_exhausted"],
                "unresolved_actions": [
                    "worker_a: repeated pre_promotion remote timeouts exhausted automatic recovery budget (2 consecutive failures, threshold=2)"
                ],
            }
        ],
    }

    view = build_monitor_view(payload)

    assert view["current"]["phase"] == "pre_promotion_timeout_blocked"
    assert view["current"]["phase_label"] == "超时恢复阻断"
    assert any(
        item.get("id") == "checks" and item.get("state") == "active"
        for item in view["pipeline_nodes"]
    )
    assert any(
        "超时恢复预算耗尽" in str(item.get("summary", ""))
        for item in view["focus_items"]
    )
    assert any(
        item.get("type") == "failure" and "超时恢复预算耗尽" in str(item.get("summary", ""))
        for item in view["activity_feed"]
    )


def test_render_monitor_html_sse_mode_disables_meta_refresh() -> None:
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {},
        "summary": {},
        "stage_dashboards": [],
    }
    html = render_monitor_html(payload, sse_url="/events")
    assert "EventSource" in html
    assert "/events" in html
    assert "<meta http-equiv=\"refresh\"" not in html


def test_render_monitor_html_static_mode_keeps_meta_refresh() -> None:
    payload = {
        "runtime_dir": "/tmp/runtime",
        "runtime_status": {},
        "summary": {},
        "stage_dashboards": [],
    }
    html = render_monitor_html(payload, auto_refresh_sec=3.0)
    assert "<meta http-equiv=\"refresh\"" in html


def test_render_monitor_html_places_stage_overview_before_pipeline() -> None:
    html = render_monitor_html(
        {
            "runtime_dir": "/tmp/runtime",
            "runtime_status": {},
            "summary": {},
            "stage_dashboards": [],
        }
    )
    assert html.index("项目阶段总览") < html.index("执行流水线（当前阶段内部节点）")
    assert "阶段状态详情" in html
    assert "远端超时恢复统计" in html


def test_render_monitor_html_contains_remote_check_section() -> None:
    html = render_monitor_html(
        {
            "runtime_dir": "/tmp/runtime",
            "runtime_status": {
                "target_repo": "/tmp/repo",
                "overall_state": "running",
                "current_stage": "stage-a",
            },
            "summary": {},
            "stage_dashboards": [
                {
                    "stage_name": "stage-a",
                    "objective": "实现 A",
                    "status": "running",
                    "current_round": 1,
                    "max_rounds": 2,
                }
            ],
            "remote_check_heartbeats": {
                "active": [
                    {
                        "heartbeat_id": "hb-1",
                        "stage_name": "stage-a",
                        "round_index": 1,
                        "worker": "worker_a",
                        "gate_tier": "fast_round",
                        "remote_host": "node0",
                        "command": "echo test",
                        "command_index": 1,
                        "command_total": 1,
                        "status": "running",
                        "elapsed_sec": 5,
                        "timeout_sec": 10,
                    }
                ]
            },
        }
    )
    assert "远端检查实时心跳" in html
    assert "remote-checks-section" in html


def test_render_monitor_html_contains_timeout_recovery_alert_hooks() -> None:
    html = render_monitor_html(
        {
            "runtime_dir": "/tmp/runtime",
            "runtime_status": {},
            "summary": {},
            "stage_dashboards": [],
        }
    )
    assert "timeout-recovery-alerts" in html
    assert "timeout-recovery-thresholds" in html
    assert "timeout-recovery-alerting" in html
    assert "recovery-alert-item" in html


def test_build_monitor_view_includes_sli_metrics_and_alerts() -> None:
    view = build_monitor_view(
        {
            "runtime_dir": "/tmp/runtime",
            "runtime_status": {
                "target_repo": "/tmp/repo",
                "sli_metrics": {
                    "run_elapsed_sec": 120.5,
                    "avg_stage_rounds": 1.25,
                    "retry_rate_per_stage": 0.4,
                    "cost_burn_rate_usd_per_min": 0.08,
                    "compression_rate": 0.33,
                },
                "sli_alerts": ["retry_rate_high"],
            },
            "summary": {},
            "stage_dashboards": [],
        }
    )
    sli = view["sli"]
    assert sli["metrics"]["run_elapsed_sec"] == 120.5
    assert sli["metrics"]["avg_stage_rounds"] == 1.25
    assert sli["metrics"]["retry_rate_per_stage"] == 0.4
    assert sli["metrics"]["cost_burn_rate_usd_per_min"] == 0.08
    assert sli["metrics"]["compression_rate"] == 0.33
    assert sli["alerts"] == ["retry_rate_high"]
    assert sli["is_alerting"] is True


def test_build_monitor_view_sli_no_alerts_sets_is_alerting_false() -> None:
    view = build_monitor_view(
        {
            "runtime_dir": "/tmp/runtime",
            "runtime_status": {
                "sli_metrics": {
                    "run_elapsed_sec": 0.0,
                    "avg_stage_rounds": 0.0,
                },
                "sli_alerts": [],
            },
            "summary": {},
            "stage_dashboards": [],
        }
    )
    sli = view["sli"]
    assert sli["alerts"] == []
    assert sli["is_alerting"] is False


def test_build_monitor_view_sli_supports_multiple_alerts() -> None:
    view = build_monitor_view(
        {
            "runtime_dir": "/tmp/runtime",
            "runtime_status": {
                "sli_metrics": {"run_elapsed_sec": 10.0},
                "sli_alerts": ["retry_rate_high", "cost_burn_rate_high"],
            },
            "summary": {},
            "stage_dashboards": [],
        }
    )
    sli = view["sli"]
    assert sli["alerts"] == ["retry_rate_high", "cost_burn_rate_high"]
    assert sli["is_alerting"] is True


def test_build_monitor_view_sli_handles_missing_metrics_and_computes_compression_fallback(
    tmp_path: "Path",
) -> None:
    import json

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    jsonl_path = artifacts_dir / "context_compression.jsonl"
    events = [
        {"original_chars": 1000, "compressed_chars": 800},
        {"original_chars": 1000, "compressed_chars": 400},
    ]
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for evt in events:
            fh.write(json.dumps(evt) + "\n")

    view = build_monitor_view(
        {
            "runtime_dir": str(tmp_path),
            "runtime_status": {
                "sli_metrics": {
                    "run_elapsed_sec": None,
                    "avg_stage_rounds": "nan",
                    "retry_rate_per_stage": "inf",
                    "cost_burn_rate_usd_per_min": "-inf",
                    "compression_rate": None,
                },
                "sli_alerts": None,
            },
            "summary": {},
            "stage_dashboards": [],
        }
    )
    sli = view["sli"]
    assert sli["metrics"]["run_elapsed_sec"] == 0.0
    assert sli["metrics"]["avg_stage_rounds"] == 0.0
    assert sli["metrics"]["retry_rate_per_stage"] == 0.0
    assert sli["metrics"]["cost_burn_rate_usd_per_min"] == 0.0
    assert sli["metrics"]["compression_rate"] == 0.4
    assert sli["alerts"] == []
    assert sli["is_alerting"] is False


def test_render_monitor_html_contains_sli_summary_hook() -> None:
    html = render_monitor_html(
        {
            "runtime_dir": "/tmp/runtime",
            "runtime_status": {
                "sli_metrics": {"run_elapsed_sec": 2.0},
                "sli_alerts": ["cost_burn_rate_high"],
            },
            "summary": {},
            "stage_dashboards": [],
        }
    )
    assert "sli-summary" in html
    assert "📈 SLI" in html
    assert "safeNumber" in html


class TestCostViewCompressionFields:
    """Integration tests for compression fields in monitor cost view."""

    def test_cost_view_includes_compression_fields(self) -> None:
        view = build_monitor_view({
            "runtime_dir": "",
            "runtime_status": {},
            "summary": {},
            "stage_dashboards": [],
            "cost_ledger": {
                "snapshot": {
                    "total_tokens": 1000,
                    "total_input_tokens": 600,
                    "total_output_tokens": 400,
                    "estimated_cost_usd": 0.05,
                    "invocation_count": 3,
                },
            },
        })
        cost = view["cost"]
        assert "compression_events" in cost
        assert "compression_saved_chars" in cost
        assert "by_worker" in cost
        assert isinstance(cost["by_worker"], list)
        assert isinstance(cost["compression_events"], list)
        assert isinstance(cost["compression_saved_chars"], (int, float))

    def test_cost_view_compression_from_jsonl(self, tmp_path: "Path") -> None:
        import json
        from pathlib import Path

        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()
        jsonl_path = artifacts_dir / "context_compression.jsonl"
        events = [
            {"stage_name": "s1", "round_index": 1, "zone": "history_review_memory",
             "original_chars": 5000, "compressed_chars": 2000, "dropped_items": 3,
             "timestamp_iso": ""},
            {"stage_name": "s1", "round_index": 2, "zone": "history_synthesis",
             "original_chars": 3000, "compressed_chars": 1000, "dropped_items": 5,
             "timestamp_iso": ""},
        ]
        with jsonl_path.open("w", encoding="utf-8") as fh:
            for evt in events:
                fh.write(json.dumps(evt) + "\n")

        view = build_monitor_view({
            "runtime_dir": str(tmp_path),
            "runtime_status": {},
            "summary": {},
            "stage_dashboards": [],
            "cost_ledger": {
                "snapshot": {
                    "total_tokens": 500,
                    "estimated_cost_usd": 0.01,
                    "invocation_count": 1,
                },
            },
        })
        cost = view["cost"]
        assert len(cost["compression_events"]) == 2
        assert cost["compression_saved_chars"] == (5000 - 2000) + (3000 - 1000)

    def test_render_html_contains_compression_info_js(self) -> None:
        html = render_monitor_html({
            "runtime_dir": "",
            "runtime_status": {},
            "summary": {},
            "stage_dashboards": [],
        })
        assert "compression_saved_chars" in html
        assert "压缩节省" in html
