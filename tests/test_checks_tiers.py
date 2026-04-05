from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager

import core.checks as checks_module
from core.models import (
    CheckCommandResult,
    BuildStrategyProfile,
    RemoteGateContract,
    StageSpec,
    TieredGateCommand,
)


def _passed(command: str) -> CheckCommandResult:
    return CheckCommandResult(command=command, exit_code=0, passed=True)


def _endpoint() -> checks_module.RemoteEndpoint:
    return checks_module.RemoteEndpoint(
        display_host="10.0.0.1",
        ssh_host="10.0.0.1",
        user="root",
        port=2222,
        password="",
    )


def test_ssh_transport_enforces_host_key_checks_by_default(monkeypatch) -> None:
    monkeypatch.delenv("MULTI_CODEX_REMOTE_SSH_INSECURE_SKIP_HOST_KEY_CHECK", raising=False)
    monkeypatch.delenv("MULTI_CODEX_REMOTE_SSH_KNOWN_HOSTS_FILE", raising=False)

    ssh_base = checks_module._build_ssh_base(_endpoint())
    assert "StrictHostKeyChecking=yes" in ssh_base
    assert "StrictHostKeyChecking=no" not in ssh_base
    assert "UserKnownHostsFile=/dev/null" not in ssh_base

    rsync_transport = checks_module._build_rsync_ssh_transport(_endpoint())
    assert "StrictHostKeyChecking=yes" in rsync_transport
    assert "StrictHostKeyChecking=no" not in rsync_transport
    assert "UserKnownHostsFile=/dev/null" not in rsync_transport


def test_ssh_transport_allows_explicit_insecure_override(monkeypatch) -> None:
    monkeypatch.setenv("MULTI_CODEX_REMOTE_SSH_INSECURE_SKIP_HOST_KEY_CHECK", "1")
    monkeypatch.setenv("MULTI_CODEX_REMOTE_SSH_KNOWN_HOSTS_FILE", "/tmp/known_hosts")

    ssh_base = checks_module._build_ssh_base(_endpoint())
    assert "StrictHostKeyChecking=no" in ssh_base
    assert "UserKnownHostsFile=/dev/null" in ssh_base
    assert "StrictHostKeyChecking=yes" not in ssh_base
    assert "UserKnownHostsFile=/tmp/known_hosts" not in ssh_base

    rsync_transport = checks_module._build_rsync_ssh_transport(_endpoint())
    assert "StrictHostKeyChecking=no" in rsync_transport
    assert "UserKnownHostsFile=/dev/null" in rsync_transport


def test_ssh_transport_uses_known_hosts_override_in_secure_mode(monkeypatch) -> None:
    monkeypatch.delenv("MULTI_CODEX_REMOTE_SSH_INSECURE_SKIP_HOST_KEY_CHECK", raising=False)
    monkeypatch.setenv("MULTI_CODEX_REMOTE_SSH_KNOWN_HOSTS_FILE", "/tmp/known_hosts")

    ssh_base = checks_module._build_ssh_base(_endpoint())
    assert "StrictHostKeyChecking=yes" in ssh_base
    assert "UserKnownHostsFile=/tmp/known_hosts" in ssh_base
    assert "UserKnownHostsFile=/dev/null" not in ssh_base

    rsync_transport = checks_module._build_rsync_ssh_transport(_endpoint())
    assert "StrictHostKeyChecking=yes" in rsync_transport
    assert "UserKnownHostsFile=/tmp/known_hosts" in rsync_transport


def test_run_checks_filters_remote_commands_and_contracts_by_tier(monkeypatch, tmp_path: Path) -> None:
    executed_batches: list[list[str]] = []

    monkeypatch.setattr(checks_module, "_validate_local_command_harness", lambda commands, remote_paths: [])
    monkeypatch.setattr(
        checks_module,
        "_run_local_stage_command_list",
        lambda commands, workspace, *, stage, category: [],
    )
    monkeypatch.setattr(
        checks_module,
        "_validate_remote_preconditions",
        lambda stage, remote_host, remote_workdir, remote_host_secondary="", remote_workdir_secondary="", *, remote_commands=None: [],
    )
    monkeypatch.setattr(checks_module, "_resolve_sync_targets", lambda stage, remote_host, remote_workdir, remote_host_secondary="", remote_workdir_secondary="": [])
    monkeypatch.setattr(checks_module, "_resolve_remote_targets", lambda stage, remote_host, remote_workdir, remote_host_secondary="", remote_workdir_secondary="": [(remote_host, remote_workdir)])
    monkeypatch.setattr(checks_module, "_check_remote_workdir_exists", lambda host, workdir: _passed(f"[remote:{host}] preflight test -d {workdir}"))

    def _run_remote_command_list(
        commands: list[str],
        host: str,
        workdir: str,
        timeout: int | None = None,
        **kwargs: object,
    ) -> list[CheckCommandResult]:
        del workdir, timeout, kwargs
        executed_batches.append(list(commands))
        return [_passed(f"[remote:{host}] {command}") for command in commands]

    monkeypatch.setattr(checks_module, "_run_remote_command_list", _run_remote_command_list)

    stage = StageSpec(
        name="stage-tiered",
        objective="test tiered gate commands",
        acceptance_criteria=["done"],
        invariants=["safe"],
        requires_remote=True,
        execution_env="remote_primary",
        gate_commands_remote_tiered=[
            TieredGateCommand(command="echo fast", tier="fast_round"),
            TieredGateCommand(command="echo heavy", tier="pre_promotion"),
        ],
        remote_gate_contracts=[
            RemoteGateContract(command="echo fast", tier="fast_round", required_exit_code=0),
            RemoteGateContract(command="echo heavy", tier="pre_promotion", required_exit_code=0),
        ],
    )

    fast_result = checks_module.run_checks_from_stage_spec(
        "worker_a",
        stage,
        tmp_path,
        remote_host="node0",
        remote_workdir="/tmp/worker_a",
        gate_tier="fast_round",
    )
    assert executed_batches == [["echo fast"]]
    assert fast_result.all_tests_passed is True
    assert fast_result.all_harness_passed is True

    executed_batches.clear()
    pre_promotion_result = checks_module.run_checks_from_stage_spec(
        "worker_a",
        stage,
        tmp_path,
        remote_host="node0",
        remote_workdir="/tmp/worker_a",
        gate_tier="pre_promotion",
    )
    assert executed_batches == [["echo heavy"]]
    assert pre_promotion_result.all_tests_passed is True
    assert pre_promotion_result.all_harness_passed is True


def test_run_checks_uses_min_budget_for_remote_timeout(monkeypatch, tmp_path: Path) -> None:
    captured_timeouts: list[int] = []

    monkeypatch.setattr(checks_module, "_resolve_remote_command_timeout_sec", lambda: 1_200)
    monkeypatch.setattr(checks_module, "_validate_local_command_harness", lambda commands, remote_paths: [])
    monkeypatch.setattr(
        checks_module,
        "_run_local_stage_command_list",
        lambda commands, workspace, *, stage, category: [],
    )
    monkeypatch.setattr(
        checks_module,
        "_validate_remote_preconditions",
        lambda stage, remote_host, remote_workdir, remote_host_secondary="", remote_workdir_secondary="", *, remote_commands=None: [],
    )
    monkeypatch.setattr(checks_module, "_resolve_sync_targets", lambda stage, remote_host, remote_workdir, remote_host_secondary="", remote_workdir_secondary="": [])
    monkeypatch.setattr(checks_module, "_resolve_remote_targets", lambda stage, remote_host, remote_workdir, remote_host_secondary="", remote_workdir_secondary="": [(remote_host, remote_workdir)])
    monkeypatch.setattr(checks_module, "_check_remote_workdir_exists", lambda host, workdir: _passed(f"[remote:{host}] preflight test -d {workdir}"))

    def _run_remote_command_list(
        commands: list[str],
        host: str,
        workdir: str,
        timeout: int | None = None,
        **kwargs: object,
    ) -> list[CheckCommandResult]:
        del workdir, kwargs
        captured_timeouts.append(int(timeout or 0))
        return [_passed(f"[remote:{host}] {command}") for command in commands]

    monkeypatch.setattr(checks_module, "_run_remote_command_list", _run_remote_command_list)

    stage = StageSpec(
        name="stage-budget-timeout",
        objective="test timeout budget",
        acceptance_criteria=["done"],
        invariants=["safe"],
        requires_remote=True,
        execution_env="remote_primary",
        gate_commands_remote_tiered=[
            TieredGateCommand(command="echo fast", tier="fast_round"),
        ],
        remote_gate_contracts=[
            RemoteGateContract(command="echo fast", tier="fast_round", required_exit_code=0),
        ],
    )

    result = checks_module.run_checks_from_stage_spec(
        "worker_a",
        stage,
        tmp_path,
        remote_host="node0",
        remote_workdir="/tmp/worker_a",
        gate_tier="fast_round",
        stage_budget_sec=900,
        round_budget_sec=300,
        phase_timeout_cap_sec=600,
    )
    assert result.all_tests_passed is True
    assert result.all_harness_passed is True
    assert captured_timeouts == [300]


def test_attempt_remote_timeout_recovery_reports_success(monkeypatch) -> None:
    calls: list[str] = []

    def _fake_run_remote_management_command(*, remote_host: str, remote_workdir: str, remote_command: str, timeout: int = 20):
        del remote_host, remote_workdir, timeout
        calls.append(remote_command)
        if "REMOTE_TIMEOUT_RECOVERY_STOPPED" in remote_command:
            return CheckCommandResult(
                command="verify",
                exit_code=0,
                stdout="REMOTE_TIMEOUT_RECOVERY_STOPPED",
                stderr="",
                passed=True,
            )
        return CheckCommandResult(
            command="cleanup",
            exit_code=0,
            stdout="REMOTE_TIMEOUT_RECOVERY_OK",
            stderr="",
            passed=True,
        )

    monkeypatch.setattr(
        checks_module,
        "_run_remote_management_command",
        _fake_run_remote_management_command,
    )
    result = checks_module._attempt_remote_timeout_recovery(
        remote_host="node0",
        remote_workdir="/tmp/worker_a",
        token="mcr_gate_token",
    )

    assert result["recovered"] is True
    assert result["cleanup_ok"] is True
    assert result["verify_ok"] is True
    assert len(calls) == 2


def test_run_remote_command_timeout_appends_recovery_summary(monkeypatch) -> None:
    class _FakeProc:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.returncode = 0
            self._killed = False

        def poll(self):
            return None if not self._killed else self.returncode

        def kill(self) -> None:
            self._killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    monkeypatch.setattr(checks_module.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(checks_module.time, "sleep", lambda _: None)
    monkeypatch.setattr(checks_module, "_build_remote_command_token", lambda **kwargs: "mcr_gate_token")
    monkeypatch.setattr(
        checks_module,
        "_attempt_remote_timeout_recovery",
        lambda **kwargs: {"recovered": True, "summary": "remote timeout recovery succeeded"},
    )

    result = checks_module._run_remote_command(
        "echo test",
        "node0",
        "/tmp/worker_a",
        timeout=0,
        worker="worker_a",
        stage_name="stage3",
        gate_tier="fast_round",
    )

    assert result.passed is False
    assert result.exit_code == -1
    assert "TIMEOUT_RECOVERY" in result.stderr


def test_sync_to_remote_default_disables_build_cache_preserve(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MULTI_CODEX_REMOTE_SYNC_PRESERVE_BUILD_CACHE", raising=False)
    monkeypatch.delenv("MULTI_CODEX_REMOTE_SYNC_DELETE", raising=False)

    commands: list[list[str]] = []

    monkeypatch.setattr(
        checks_module,
        "_resolve_remote_endpoint",
        lambda host: checks_module.RemoteEndpoint(
            display_host=host,
            ssh_host="127.0.0.1",
            user="root",
            port=22,
            password="",
        ),
    )
    monkeypatch.setattr(checks_module, "_build_rsync_ssh_transport", lambda endpoint: "ssh -p 22")

    def _fake_run(command: list[str], **kwargs: object) -> object:
        del kwargs
        commands.append(list(command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(checks_module.subprocess, "run", _fake_run)

    result = checks_module._sync_to_remote(tmp_path, "node0", "/tmp/remote")
    assert result.passed is True
    assert len(commands) >= 2
    rsync_command = commands[1]
    assert "--delete" in rsync_command
    assert "build/" not in rsync_command


def test_sync_to_remote_can_enable_build_cache_preserve(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MULTI_CODEX_REMOTE_SYNC_PRESERVE_BUILD_CACHE", "true")
    monkeypatch.delenv("MULTI_CODEX_REMOTE_SYNC_DELETE", raising=False)

    commands: list[list[str]] = []

    monkeypatch.setattr(
        checks_module,
        "_resolve_remote_endpoint",
        lambda host: checks_module.RemoteEndpoint(
            display_host=host,
            ssh_host="127.0.0.1",
            user="root",
            port=22,
            password="",
        ),
    )
    monkeypatch.setattr(checks_module, "_build_rsync_ssh_transport", lambda endpoint: "ssh -p 22")

    def _fake_run(command: list[str], **kwargs: object) -> object:
        del kwargs
        commands.append(list(command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(checks_module.subprocess, "run", _fake_run)

    result = checks_module._sync_to_remote(tmp_path, "node0", "/tmp/remote")
    assert result.passed is True
    assert len(commands) >= 2
    rsync_command = commands[1]
    assert "--delete" in rsync_command
    assert "build/" in rsync_command


def test_sync_to_remote_acquires_target_scoped_lock(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    lock_calls: list[tuple[str, str, int]] = []

    monkeypatch.setattr(
        checks_module,
        "_resolve_remote_endpoint",
        lambda host: checks_module.RemoteEndpoint(
            display_host=host,
            ssh_host="127.0.0.1",
            user="root",
            port=22,
            password="",
        ),
    )
    monkeypatch.setattr(checks_module, "_build_rsync_ssh_transport", lambda endpoint: "ssh -p 22")
    monkeypatch.setattr(checks_module, "_read_int_env", lambda key, default: 15 if key == "MULTI_CODEX_REMOTE_SYNC_LOCK_TIMEOUT_SEC" else default)

    @contextmanager
    def _fake_lock(remote_host: str, sync_remote_path: str, *, timeout_sec: int):
        lock_calls.append((remote_host, sync_remote_path, timeout_sec))
        yield

    monkeypatch.setattr(checks_module, "_acquire_remote_sync_lock", _fake_lock)

    def _fake_run(command: list[str], **kwargs: object) -> object:
        del kwargs
        commands.append(list(command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(checks_module.subprocess, "run", _fake_run)

    result = checks_module._sync_to_remote(tmp_path, "node0", "/tmp/remote")
    assert result.passed is True
    assert lock_calls == [("node0", "/tmp/remote", 15)]
    assert len(commands) >= 2


def test_sync_to_remote_fails_closed_when_lock_times_out(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        checks_module,
        "_resolve_remote_endpoint",
        lambda host: checks_module.RemoteEndpoint(
            display_host=host,
            ssh_host="127.0.0.1",
            user="root",
            port=22,
            password="",
        ),
    )
    monkeypatch.setattr(checks_module, "_read_int_env", lambda key, default: 7 if key == "MULTI_CODEX_REMOTE_SYNC_LOCK_TIMEOUT_SEC" else default)

    def _raise_lock_timeout(remote_host: str, sync_remote_path: str, *, timeout_sec: int):
        del remote_host, sync_remote_path, timeout_sec
        raise TimeoutError("lock timeout")

    monkeypatch.setattr(checks_module, "_acquire_remote_sync_lock", _raise_lock_timeout)

    result = checks_module._sync_to_remote(tmp_path, "node0", "/tmp/remote")
    assert result.passed is False
    assert result.exit_code == -1
    assert "remote sync lock acquisition exceeded 7s" in result.stderr


def test_run_checks_can_preserve_cache_by_build_strategy(monkeypatch, tmp_path: Path) -> None:
    sync_calls: list[dict[str, object]] = []

    monkeypatch.delenv("MULTI_CODEX_REMOTE_SYNC_PRESERVE_BUILD_CACHE", raising=False)
    monkeypatch.setattr(checks_module, "_validate_local_command_harness", lambda commands, remote_paths: [])
    monkeypatch.setattr(
        checks_module,
        "_run_local_stage_command_list",
        lambda commands, workspace, *, stage, category: [],
    )
    monkeypatch.setattr(
        checks_module,
        "_validate_remote_preconditions",
        lambda stage, remote_host, remote_workdir, remote_host_secondary="", remote_workdir_secondary="", *, remote_commands=None: [],
    )
    monkeypatch.setattr(
        checks_module,
        "_resolve_sync_targets",
        lambda stage, remote_host, remote_workdir, remote_host_secondary="", remote_workdir_secondary="": [
            (remote_host, remote_workdir)
        ],
    )
    monkeypatch.setattr(
        checks_module,
        "_resolve_remote_targets",
        lambda stage, remote_host, remote_workdir, remote_host_secondary="", remote_workdir_secondary="": [
            (remote_host, remote_workdir)
        ],
    )
    monkeypatch.setattr(checks_module, "_cleanup_remote_path_sensitive_metadata", lambda host, workdir: _passed("cleanup"))
    monkeypatch.setattr(checks_module, "_check_remote_workdir_exists", lambda host, workdir: _passed("preflight"))
    monkeypatch.setattr(
        checks_module,
        "_run_remote_command_list",
        lambda commands, host, workdir, timeout=None, **kwargs: [_passed(f"[remote:{host}] {cmd}") for cmd in commands],
    )

    def _fake_sync_to_remote(
        local_path: Path,
        remote_host: str,
        remote_path: str,
        *,
        preserve_build_cache: bool | None = None,
        cache_paths: list[str] | None = None,
    ) -> CheckCommandResult:
        del local_path
        sync_calls.append(
            {
                "remote_host": remote_host,
                "remote_path": remote_path,
                "preserve_build_cache": preserve_build_cache,
                "cache_paths": list(cache_paths or []),
            }
        )
        return _passed("sync")

    monkeypatch.setattr(checks_module, "_sync_to_remote", _fake_sync_to_remote)

    stage = StageSpec(
        name="stage-build-strategy-cache",
        objective="test cache preserve policy",
        acceptance_criteria=["done"],
        invariants=["safe"],
        requires_remote=True,
        execution_env="remote_primary",
        sync_strategy="sync_to_remote_primary",
        build_strategy=BuildStrategyProfile(
            preserve_remote_cache_default=False,
            preserve_remote_cache_tiers=["fast_round"],
            remote_cache_paths=["build/", ".gradle/"],
        ),
        gate_commands_remote_tiered=[
            TieredGateCommand(command="ninja -C build all", tier="fast_round"),
            TieredGateCommand(command="pytest -q", tier="pre_promotion"),
        ],
    )

    checks_module.run_checks_from_stage_spec(
        "worker_a",
        stage,
        tmp_path,
        remote_host="node0",
        remote_workdir="/tmp/worker_a",
        gate_tier="fast_round",
    )
    checks_module.run_checks_from_stage_spec(
        "worker_a",
        stage,
        tmp_path,
        remote_host="node0",
        remote_workdir="/tmp/worker_a",
        gate_tier="pre_promotion",
    )

    assert len(sync_calls) == 2
    assert sync_calls[0]["preserve_build_cache"] is True
    assert sync_calls[0]["cache_paths"] == ["build/", ".gradle/"]
    assert sync_calls[1]["preserve_build_cache"] is False
    assert sync_calls[1]["cache_paths"] == ["build/", ".gradle/"]
