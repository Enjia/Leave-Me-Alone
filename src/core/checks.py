from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
from contextlib import contextmanager
from typing import Any

from core.models import (
    AutoCheckResult,
    CheckCommandResult,
    GateTier,
    RemoteGateContract,
    StageGate,
    StageSpec,
)


logger = logging.getLogger(__name__)
try:
    import fcntl
except ImportError:  # pragma: no cover - non-posix fallback
    fcntl = None  # type: ignore[assignment]

MAX_OUTPUT_CHARS = 8000
DEV_ENV_REMOTE_SCRIPT = (
    Path.home() / ".codex/skills/dev-env-verify/scripts/dev_env_remote.sh"
)
DEV_ENV_CONTROL_DIR = Path("/tmp/devenv-ssh-control")
DEV_ENV_KNOWN_HOSTS = DEV_ENV_CONTROL_DIR / "known_hosts"

ALLOWED_EXECUTABLES: frozenset[str] = frozenset({
    # Test runners
    "pytest", "unittest",
    # Python interpreters (controlled execution only)
    "python3", "python",
    # JS/TS package managers & runners (no raw node/npx)
    "npm", "yarn", "pnpm",
    # Rust
    "cargo",
    # Go
    "go",
    # Build systems
    "make", "cmake", "mvn", "gradle", "gradlew",
    # Python linters/formatters
    "ruff", "flake8", "pylint", "mypy", "pyright", "black", "isort",
    # JS/TS linters/formatters
    "eslint", "tsc", "biome", "prettier",
    # Rust linter
    "clippy",
    # Go linter
    "golangci-lint",
    # Benchmarking
    "hyperfine", "bench",
    # Remote sync tools
    "rsync", "scp",
})

DENIED_ARGUMENTS: frozenset[str] = frozenset({
    "-c", "--command", "-e", "--eval", "exec",
})

NODE_ALIAS_DEFAULTS: dict[str, tuple[str, str, int, str]] = {
    "node0": ("10.236.220.127", "root", 2022, "Qwe123!@#"),
    "node1": ("10.236.221.9", "root", 2022, "Qwe123!@#"),
}

DEFAULT_CONTAINER_WORKDIR_PREFIX = "/enjia"
DEFAULT_ALIAS_HOST_SYNC_PREFIX = "/root/enjia"
REMOTE_PATH_SENSITIVE_METADATA_PATTERNS: tuple[str, ...] = (
    "build/obj/collectives/device/Makefile.rules",
    "build/obj/collectives/device/*.dep",
    "build/obj/collectives/device/*.d",
    "build/obj/collectives/device/gensrc/symmetric/rules.mk",
    "build/obj/collectives/device/genobj/symmetric/*.dep",
    "build/obj/collectives/device/genobj/symmetric/*.d",
)


@dataclass(frozen=True)
class RemoteEndpoint:
    display_host: str
    ssh_host: str
    user: str
    port: int
    password: str


def _read_int_env(key: str, default: int) -> int:
    raw = os.getenv(key, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _read_bool_env(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_local_command_timeout_sec() -> int:
    return _read_int_env("MULTI_CODEX_LOCAL_COMMAND_TIMEOUT_SEC", 300)


def _resolve_remote_command_timeout_sec() -> int:
    stage_default = _read_int_env("MULTI_CODEX_STAGE_TIMEOUT_SEC", 10_800)
    return _read_int_env("MULTI_CODEX_REMOTE_COMMAND_TIMEOUT_SEC", stage_default)


def _resolve_effective_remote_command_timeout_sec(
    *,
    stage_budget_sec: int | None = None,
    round_budget_sec: int | None = None,
    phase_timeout_cap_sec: int | None = None,
) -> int:
    candidates = [_resolve_remote_command_timeout_sec()]
    for candidate in (stage_budget_sec, round_budget_sec, phase_timeout_cap_sec):
        if isinstance(candidate, int) and candidate > 0:
            candidates.append(candidate)
    return max(1, min(candidates))


def _resolve_remote_heartbeat_interval_sec() -> int:
    return _read_int_env("MULTI_CODEX_REMOTE_HEARTBEAT_INTERVAL_SEC", 5)


def _iso_utc(epoch_sec: float) -> str:
    return datetime.fromtimestamp(epoch_sec, tz=timezone.utc).isoformat()


def _emit_remote_heartbeat(
    heartbeat_sink: Callable[[dict[str, Any]], None] | None,
    *,
    event: str,
    worker: str,
    stage_name: str,
    gate_tier: GateTier,
    remote_host: str,
    remote_workdir: str,
    command: str,
    timeout_sec: int,
    started_epoch_sec: float,
    elapsed_sec: float,
    command_index: int,
    command_total: int,
    status: str,
    heartbeat_id: str,
    exit_code: int | None = None,
    last_output_epoch_sec: float | None = None,
    recovery: dict[str, Any] | None = None,
) -> None:
    if heartbeat_sink is None:
        return
    now_epoch_sec = time.time()
    payload: dict[str, Any] = {
        "event": event,
        "heartbeat_id": heartbeat_id,
        "worker": worker,
        "stage_name": stage_name,
        "gate_tier": gate_tier,
        "remote_host": remote_host,
        "remote_workdir": remote_workdir,
        "command": command,
        "command_index": command_index,
        "command_total": command_total,
        "timeout_sec": timeout_sec,
        "elapsed_sec": max(0, int(elapsed_sec)),
        "started_at": _iso_utc(started_epoch_sec),
        "last_progress_at": _iso_utc(now_epoch_sec),
        "last_output_at": _iso_utc(last_output_epoch_sec) if last_output_epoch_sec else "",
        "status": status,
    }
    if isinstance(exit_code, int):
        payload["exit_code"] = exit_code
    if isinstance(recovery, dict):
        payload["recovery"] = recovery
    heartbeat_sink(payload)


def _resolve_makeflags() -> str:
    inherited = os.getenv("MAKEFLAGS", "").strip()
    if inherited:
        return inherited
    return os.getenv("MULTI_CODEX_MAKEFLAGS", "-j4").strip()


def _should_inject_makeflags(tokens: list[str]) -> bool:
    if not tokens or Path(tokens[0]).name != "make":
        return False
    for token in tokens[1:]:
        if token == "-j" or token.startswith("-j") or token == "--jobs" or token.startswith("--jobs="):
            return False
    return bool(_resolve_makeflags())


def _build_command_env(tokens: list[str]) -> dict[str, str] | None:
    if not _should_inject_makeflags(tokens):
        return None
    env = os.environ.copy()
    env["MAKEFLAGS"] = _resolve_makeflags()
    return env


def _maybe_wrap_remote_make_command(command: str) -> str:
    try:
        tokens = _validate_command(command)
    except ValueError:
        return command
    if not _should_inject_makeflags(tokens):
        return command
    return f"env MAKEFLAGS={shlex.quote(_resolve_makeflags())} {command}"


def _resolve_remote_endpoint(remote_host: str) -> RemoteEndpoint:
    host = remote_host.strip()
    if not host:
        return RemoteEndpoint(
            display_host="",
            ssh_host="",
            user="root",
            port=22,
            password="",
        )

    alias = host.lower()
    if alias in NODE_ALIAS_DEFAULTS:
        default_ip, default_user, default_port, default_password = NODE_ALIAS_DEFAULTS[alias]
        user = os.getenv(f"MULTI_CODEX_{alias.upper()}_SSH_USER", default_user).strip() or default_user
        port = _read_int_env(f"MULTI_CODEX_{alias.upper()}_SSH_PORT", default_port)
        password = os.getenv(
            f"MULTI_CODEX_{alias.upper()}_SSH_PASSWORD",
            default_password,
        ).strip()
        return RemoteEndpoint(
            display_host=host,
            ssh_host=default_ip,
            user=user,
            port=port,
            password=password,
        )

    parsed_user = ""
    parsed_host = host
    if "@" in host:
        parsed_user, parsed_host = host.split("@", 1)
    user = parsed_user or os.getenv("MULTI_CODEX_REMOTE_SSH_USER", "root").strip() or "root"
    port = _read_int_env("MULTI_CODEX_REMOTE_SSH_PORT", 22)
    password = os.getenv("MULTI_CODEX_REMOTE_SSH_PASSWORD", "").strip()
    return RemoteEndpoint(
        display_host=host,
        ssh_host=parsed_host,
        user=user,
        port=port,
        password=password,
    )


def _build_ssh_base(endpoint: RemoteEndpoint) -> list[str]:
    command: list[str] = []
    if endpoint.password:
        command.extend(["sshpass", "-p", endpoint.password])
    command.extend(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=10",
        ]
    )
    alias = endpoint.display_host.strip().lower()
    if alias in NODE_ALIAS_DEFAULTS:
        DEV_ENV_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        command.extend(
            [
                "-o",
                "ControlMaster=auto",
                "-o",
                f"ControlPath={DEV_ENV_CONTROL_DIR / f'ctl-{endpoint.user}-{endpoint.ssh_host}-{endpoint.port}'}",
                "-o",
                "ControlPersist=30m",
                "-o",
                f"UserKnownHostsFile={DEV_ENV_KNOWN_HOSTS}",
            ]
        )
    command.extend(
        [
            "-p",
            str(endpoint.port),
            f"{endpoint.user}@{endpoint.ssh_host}",
        ]
    )
    return command


def _build_rsync_ssh_transport(endpoint: RemoteEndpoint) -> str:
    parts = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
    ]
    alias = endpoint.display_host.strip().lower()
    if alias in NODE_ALIAS_DEFAULTS:
        DEV_ENV_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        parts.extend(
            [
                "-o",
                "ControlMaster=auto",
                "-o",
                f"ControlPath={DEV_ENV_CONTROL_DIR / f'ctl-{endpoint.user}-{endpoint.ssh_host}-{endpoint.port}'}",
                "-o",
                "ControlPersist=30m",
                "-o",
                f"UserKnownHostsFile={DEV_ENV_KNOWN_HOSTS}",
            ]
        )
    parts.extend(["-p", str(endpoint.port)])
    return " ".join(parts)


def _validate_command(command: str) -> list[str]:
    """Parse and validate a shell command string.

    Security model:
    - Only allow specific tool executables (no shells or interpreters).
    - Reject dangerous arguments like ``-c`` / ``--eval`` that allow
      arbitrary code execution even on otherwise-safe executables.

    Raises ``ValueError`` if the command is rejected.
    Returns the tokenised argument list suitable for ``subprocess.run``
    with ``shell=False``.
    """
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Malformed command string: {command!r}") from exc

    if not tokens:
        raise ValueError("Empty command string")

    executable = Path(tokens[0]).name

    if executable not in ALLOWED_EXECUTABLES:
        raise ValueError(
            f"Command executable {executable!r} is not in the allow-list. "
            f"Allowed: {sorted(ALLOWED_EXECUTABLES)}"
        )

    denied_found = DENIED_ARGUMENTS.intersection(tokens[1:])
    if denied_found:
        raise ValueError(
            f"Command contains denied argument(s) {sorted(denied_found)}. "
            f"These allow arbitrary code execution and are blocked."
        )

    # Special validation for interpreters: only allow repo-internal scripts
    if executable in ("python3", "python"):
        if len(tokens) < 2:
            raise ValueError(
                f"{executable} requires a script path argument"
            )
        script_path = tokens[1]
        allowed_prefixes = ("tests/", "scripts/", "./tests/", "./scripts/")
        if not script_path.startswith(allowed_prefixes):
            raise ValueError(
                f"{executable} can only execute scripts under tests/ or scripts/. "
                f"Got: {script_path!r}"
            )

    return tokens


def _run_command(command: str, workspace: Path) -> CheckCommandResult:
    try:
        tokens = _validate_command(command)
    except ValueError as exc:
        logger.warning("Rejected command %r: %s", command, exc)
        return CheckCommandResult(
            command=command,
            exit_code=-2,
            stdout="",
            stderr=f"REJECTED: {exc}",
            passed=False,
        )

    timeout_sec = _resolve_local_command_timeout_sec()
    env = _build_command_env(tokens)
    try:
        proc = subprocess.run(
            tokens,
            shell=False,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return CheckCommandResult(
            command=command,
            exit_code=-1,
            stdout="",
            stderr=f"TIMEOUT: command exceeded {timeout_sec}s limit",
            passed=False,
        )
    except FileNotFoundError:
        return CheckCommandResult(
            command=command,
            exit_code=-3,
            stdout="",
            stderr=f"Executable not found: {tokens[0]!r}",
            passed=False,
        )

    stdout = proc.stdout[:MAX_OUTPUT_CHARS] if proc.stdout else ""
    stderr = proc.stderr[:MAX_OUTPUT_CHARS] if proc.stderr else ""

    return CheckCommandResult(
        command=command,
        exit_code=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        passed=proc.returncode == 0,
    )

def _run_command_list(commands: list[str], workspace: Path) -> list[CheckCommandResult]:
    return [_run_command(cmd, workspace) for cmd in commands]


def _failed_check(command: str, message: str, exit_code: int = -4) -> CheckCommandResult:
    return CheckCommandResult(
        command=command,
        exit_code=exit_code,
        stdout="",
        stderr=message,
        passed=False,
    )


def _skipped_check(command: str, message: str) -> CheckCommandResult:
    return CheckCommandResult(
        command=command,
        exit_code=0,
        stdout=message,
        stderr="",
        passed=True,
    )


def _run_local_stage_command_list(
    commands: list[str],
    workspace: Path,
    *,
    stage: StageSpec,
    category: str,
) -> list[CheckCommandResult]:
    if stage.requires_remote and os.getenv("MULTI_CODEX_REMOTE_CANONICAL") == "1":
        return [
            _skipped_check(
                cmd,
                (
                    f"SKIPPED local {category}: remote canonical mode is enabled "
                    f"for stage '{stage.name}', remote gates are authoritative."
                ),
            )
            for cmd in commands
        ]
    return _run_command_list(commands, workspace)


def run_automated_checks(
    worker: str,
    stage_name: str,
    stage_gate: StageGate,
    workspace: Path,
) -> AutoCheckResult:
    test_results = _run_command_list(stage_gate.test_commands, workspace)
    lint_results = _run_command_list(stage_gate.lint_commands, workspace)
    perf_results = _run_command_list(stage_gate.perf_checks, workspace)

    return AutoCheckResult(
        worker=worker,
        stage_name=stage_name,
        test_results=test_results,
        lint_results=lint_results,
        perf_results=perf_results,
        harness_results=[],
        all_tests_passed=all(result.passed for result in test_results) if test_results else True,
        all_lint_passed=all(result.passed for result in lint_results) if lint_results else True,
        all_perf_passed=all(result.passed for result in perf_results) if perf_results else True,
        all_harness_passed=True,
    )


async def run_automated_checks_async(
    worker: str,
    stage_name: str,
    stage_gate: StageGate,
    workspace: Path,
) -> AutoCheckResult:
    return await asyncio.to_thread(
        run_automated_checks, worker, stage_name, stage_gate, workspace
    )


def format_check_summary(result: AutoCheckResult) -> str:
    lines = [
        f"=== Automated Check Results for {result.worker} (stage: {result.stage_name}) ===",
        f"Tests passed: {result.all_tests_passed}",
        f"Lint passed: {result.all_lint_passed}",
        f"Perf passed: {result.all_perf_passed}",
        f"Harness passed: {result.all_harness_passed}",
    ]

    for category, results in [
        ("TEST", result.test_results),
        ("LINT", result.lint_results),
        ("PERF", result.perf_results),
        ("HARNESS", result.harness_results),
    ]:
        for check_result in results:
            status = "PASS" if check_result.passed else "FAIL"
            lines.append(f"  [{category}] {status}: `{check_result.command}` (exit={check_result.exit_code})")
            if not check_result.passed:
                if check_result.stderr:
                    lines.append(f"    stderr: {check_result.stderr[:2000]}")
                if check_result.stdout:
                    lines.append(f"    stdout: {check_result.stdout[:2000]}")

    return "\n".join(lines)


def _build_remote_command_token(
    *,
    remote_host: str,
    remote_workdir: str,
    command: str,
) -> str:
    seed = f"{remote_host}|{remote_workdir}|{command}|{time.time_ns()}|{os.getpid()}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"mcr_gate_{digest}"


def _wrap_remote_command_with_token(command: str, token: str) -> str:
    script = (
        f"export MULTI_CODEX_GATE_TOKEN={shlex.quote(token)}; "
        f"{command} # token:{token}"
    )
    return f"bash -lc {shlex.quote(script)}"


def _run_remote_management_command(
    *,
    remote_host: str,
    remote_workdir: str,
    remote_command: str,
    timeout: int = 20,
) -> CheckCommandResult:
    alias = remote_host.strip().lower()
    if alias in NODE_ALIAS_DEFAULTS and DEV_ENV_REMOTE_SCRIPT.exists():
        local_command = [
            str(DEV_ENV_REMOTE_SCRIPT),
            "--node",
            alias,
            "--workdir",
            remote_workdir,
            "--cmd",
            remote_command,
        ]
        display_host = remote_host
        missing_message = "dev_env_remote.sh not found in PATH"
    else:
        endpoint = _resolve_remote_endpoint(remote_host)
        local_command = _build_ssh_base(endpoint) + [
            f"cd {shlex.quote(remote_workdir)} && {remote_command}",
        ]
        display_host = endpoint.display_host
        missing_message = "ssh not found in PATH"
    try:
        proc = subprocess.run(
            local_command,
            shell=False,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout)),
        )
    except subprocess.TimeoutExpired:
        return CheckCommandResult(
            command=f"[remote:{display_host}] timeout-recovery",
            exit_code=-1,
            stdout="",
            stderr=f"TIMEOUT: timeout-recovery command exceeded {timeout}s limit",
            passed=False,
        )
    except FileNotFoundError:
        return CheckCommandResult(
            command=f"[remote:{display_host}] timeout-recovery",
            exit_code=-3,
            stdout="",
            stderr=missing_message,
            passed=False,
        )
    return CheckCommandResult(
        command=f"[remote:{display_host}] timeout-recovery",
        exit_code=proc.returncode,
        stdout=(proc.stdout or "")[:MAX_OUTPUT_CHARS],
        stderr=(proc.stderr or "")[:MAX_OUTPUT_CHARS],
        passed=proc.returncode == 0,
    )


def _attempt_remote_timeout_recovery(
    *,
    remote_host: str,
    remote_workdir: str,
    token: str,
) -> dict[str, Any]:
    token_q = shlex.quote(token)
    cleanup_command = (
        f"pids=$(pgrep -f -- {token_q} || true); "
        "if [ -n \"$pids\" ]; then kill -TERM $pids || true; sleep 1; fi; "
        f"pids=$(pgrep -f -- {token_q} || true); "
        "if [ -n \"$pids\" ]; then kill -KILL $pids || true; sleep 1; fi; "
        f"pids=$(pgrep -f -- {token_q} || true); "
        "if [ -n \"$pids\" ]; then echo REMOTE_TIMEOUT_RECOVERY_FAILED:$pids; exit 2; fi; "
        "echo REMOTE_TIMEOUT_RECOVERY_OK"
    )
    cleanup = _run_remote_management_command(
        remote_host=remote_host,
        remote_workdir=remote_workdir,
        remote_command=cleanup_command,
        timeout=20,
    )
    verify_command = (
        f"if pgrep -f -- {token_q} >/dev/null 2>&1; then "
        "echo REMOTE_TIMEOUT_RECOVERY_RUNNING; exit 2; "
        "else echo REMOTE_TIMEOUT_RECOVERY_STOPPED; fi"
    )
    verify = _run_remote_management_command(
        remote_host=remote_host,
        remote_workdir=remote_workdir,
        remote_command=verify_command,
        timeout=10,
    )
    cleanup_ok = cleanup.passed and "REMOTE_TIMEOUT_RECOVERY_OK" in cleanup.stdout
    verify_ok = verify.passed and "REMOTE_TIMEOUT_RECOVERY_STOPPED" in verify.stdout
    recovered = cleanup_ok and verify_ok
    summary = (
        "remote timeout recovery succeeded: remote processes terminated and verified stopped."
        if recovered
        else "remote timeout recovery failed: some remote processes may still be running."
    )
    return {
        "token": token,
        "recovered": recovered,
        "cleanup_ok": cleanup_ok,
        "verify_ok": verify_ok,
        "cleanup_exit_code": cleanup.exit_code,
        "verify_exit_code": verify.exit_code,
        "cleanup_stdout": cleanup.stdout[:500],
        "cleanup_stderr": cleanup.stderr[:500],
        "verify_stdout": verify.stdout[:500],
        "verify_stderr": verify.stderr[:500],
        "summary": summary,
    }


def _run_remote_command(
    command: str,
    remote_host: str,
    remote_workdir: str,
    timeout: int | None = None,
    *,
    worker: str = "worker",
    stage_name: str = "",
    gate_tier: GateTier = "fast_round",
    command_index: int = 1,
    command_total: int = 1,
    heartbeat_sink: Callable[[dict[str, Any]], None] | None = None,
) -> CheckCommandResult:
    """Execute a command on a remote host via SSH."""
    timeout_sec = timeout if timeout is not None else _resolve_remote_command_timeout_sec()
    executed_command = _maybe_wrap_remote_make_command(command)
    command_token = _build_remote_command_token(
        remote_host=remote_host,
        remote_workdir=remote_workdir,
        command=command,
    )
    tracked_command = _wrap_remote_command_with_token(executed_command, command_token)
    heartbeat_id = (
        f"{stage_name}:{worker}:{gate_tier}:{remote_host}:{command_index}/{command_total}:{command}"
    )
    start_epoch_sec = time.time()
    start_monotonic = time.monotonic()
    heartbeat_interval_sec = max(1, _resolve_remote_heartbeat_interval_sec())
    last_heartbeat_epoch_sec = start_epoch_sec
    last_output_epoch_sec: float | None = None

    def _heartbeat(
        event: str,
        *,
        status: str,
        exit_code: int | None = None,
        recovery: dict[str, Any] | None = None,
    ) -> None:
        nonlocal last_heartbeat_epoch_sec
        elapsed = time.monotonic() - start_monotonic
        _emit_remote_heartbeat(
            heartbeat_sink,
            event=event,
            worker=worker,
            stage_name=stage_name,
            gate_tier=gate_tier,
            remote_host=remote_host,
            remote_workdir=remote_workdir,
            command=command,
            timeout_sec=timeout_sec,
            started_epoch_sec=start_epoch_sec,
            elapsed_sec=elapsed,
            command_index=command_index,
            command_total=command_total,
            status=status,
            heartbeat_id=heartbeat_id,
            exit_code=exit_code,
            last_output_epoch_sec=last_output_epoch_sec,
            recovery=recovery,
        )
        last_heartbeat_epoch_sec = time.time()

    def _run_process(process_command: list[str], *, missing_message: str) -> CheckCommandResult:
        nonlocal last_output_epoch_sec
        try:
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                proc = subprocess.Popen(
                    process_command,
                    shell=False,
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
                _heartbeat("start", status="running")

                timed_out = False
                while proc.poll() is None:
                    now_epoch_sec = time.time()
                    if now_epoch_sec - last_heartbeat_epoch_sec >= heartbeat_interval_sec:
                        _heartbeat("progress", status="running")
                    elapsed_sec = time.monotonic() - start_monotonic
                    if elapsed_sec >= timeout_sec:
                        timed_out = True
                        proc.kill()
                        break
                    sleep_sec = min(heartbeat_interval_sec, max(0.2, timeout_sec - elapsed_sec))
                    time.sleep(sleep_sec)

                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read(MAX_OUTPUT_CHARS).decode("utf-8", errors="replace")
                stderr = stderr_file.read(MAX_OUTPUT_CHARS).decode("utf-8", errors="replace")
                if stdout or stderr:
                    last_output_epoch_sec = time.time()

                if timed_out:
                    recovery = _attempt_remote_timeout_recovery(
                        remote_host=remote_host,
                        remote_workdir=remote_workdir,
                        token=command_token,
                    )
                    _heartbeat(
                        "timeout_recovery",
                        status="recovered" if recovery.get("recovered") else "cleanup_failed",
                        exit_code=-1,
                        recovery=recovery,
                    )
                    _heartbeat("finish", status="timeout", exit_code=-1)
                    recovery_line = (
                        "TIMEOUT_RECOVERY: remote process cleanup succeeded."
                        if recovery.get("recovered")
                        else "TIMEOUT_RECOVERY_FAILED: remote process cleanup could not be verified."
                    )
                    return CheckCommandResult(
                        command=f"[remote:{remote_host}] {command}",
                        exit_code=-1,
                        stdout=stdout,
                        stderr=(
                            f"TIMEOUT: remote command exceeded {timeout_sec}s limit\n"
                            f"{recovery_line}\n"
                            f"{str(recovery.get('summary', '')).strip()}"
                        ).strip(),
                        passed=False,
                    )

                _heartbeat(
                    "finish",
                    status="passed" if proc.returncode == 0 else "failed",
                    exit_code=proc.returncode,
                )
                return CheckCommandResult(
                    command=f"[remote:{remote_host}] {command}",
                    exit_code=proc.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    passed=proc.returncode == 0,
                )
        except FileNotFoundError:
            _heartbeat("finish", status="error", exit_code=-3)
            return CheckCommandResult(
                command=f"[remote:{remote_host}] {command}",
                exit_code=-3,
                stdout="",
                stderr=missing_message,
                passed=False,
            )

    alias = remote_host.strip().lower()
    if alias in NODE_ALIAS_DEFAULTS and DEV_ENV_REMOTE_SCRIPT.exists():
        script_command = [
            str(DEV_ENV_REMOTE_SCRIPT),
            "--node",
            alias,
            "--workdir",
            remote_workdir,
            "--cmd",
            tracked_command,
        ]
        return _run_process(
            script_command,
            missing_message="dev_env_remote.sh not found in PATH",
        )

    endpoint = _resolve_remote_endpoint(remote_host)
    ssh_command = _build_ssh_base(endpoint) + [
        f"cd {shlex.quote(remote_workdir)} && {tracked_command}",
    ]
    return _run_process(
        ssh_command,
        missing_message="ssh not found in PATH",
    )


def _run_remote_command_list(
    commands: list[str],
    remote_host: str,
    remote_workdir: str,
    timeout: int | None = None,
    *,
    worker: str = "worker",
    stage_name: str = "",
    gate_tier: GateTier = "fast_round",
    heartbeat_sink: Callable[[dict[str, Any]], None] | None = None,
) -> list[CheckCommandResult]:
    return [
        _run_remote_command(
            cmd,
            remote_host,
            remote_workdir,
            timeout=timeout,
            worker=worker,
            stage_name=stage_name,
            gate_tier=gate_tier,
            command_index=index,
            command_total=max(1, len(commands)),
            heartbeat_sink=heartbeat_sink,
        )
        for index, cmd in enumerate(commands, start=1)
    ]


def _resolve_sync_remote_path(remote_host: str, remote_path: str) -> str:
    """Map container workdir path to host-mounted path for node aliases."""
    normalized_path = remote_path.strip()
    alias = remote_host.strip().lower()
    if alias not in NODE_ALIAS_DEFAULTS:
        return normalized_path
    if not normalized_path.startswith("/"):
        return normalized_path

    container_prefix = os.getenv(
        f"MULTI_CODEX_{alias.upper()}_CONTAINER_WORKDIR_PREFIX",
        DEFAULT_CONTAINER_WORKDIR_PREFIX,
    ).strip() or DEFAULT_CONTAINER_WORKDIR_PREFIX
    host_prefix = os.getenv(
        f"MULTI_CODEX_{alias.upper()}_HOST_SYNC_PREFIX",
        DEFAULT_ALIAS_HOST_SYNC_PREFIX,
    ).strip() or DEFAULT_ALIAS_HOST_SYNC_PREFIX

    container_prefix = container_prefix.rstrip("/") or "/"
    host_prefix = host_prefix.rstrip("/") or "/"

    if normalized_path == container_prefix:
        return host_prefix
    if normalized_path.startswith(f"{container_prefix}/"):
        return host_prefix + normalized_path[len(container_prefix) :]
    return normalized_path


def _remote_sync_lock_file_path(remote_host: str, sync_remote_path: str) -> Path:
    lock_dir = Path(
        os.getenv(
            "MULTI_CODEX_REMOTE_SYNC_LOCK_DIR",
            "/tmp/multi-codex-remote-sync-locks",
        )
    )
    scope = f"{remote_host.strip().lower()}|{sync_remote_path.strip()}"
    lock_key = hashlib.sha1(scope.encode("utf-8")).hexdigest()[:20]
    return lock_dir / f"remote-sync-{lock_key}.lock"


@contextmanager
def _acquire_remote_sync_lock(
    remote_host: str,
    sync_remote_path: str,
    *,
    timeout_sec: int,
):
    """Acquire a best-effort cross-process lock for a remote sync target."""
    if fcntl is None:  # pragma: no cover - non-posix fallback
        yield
        return

    lock_file = _remote_sync_lock_file_path(remote_host, sync_remote_path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+", encoding="utf-8") as fh:
        deadline = time.monotonic() + max(1, timeout_sec)
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "remote sync lock acquisition timed out "
                        f"for {remote_host}:{sync_remote_path}"
                    )
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _sync_to_remote(
    local_path: Path,
    remote_host: str,
    remote_path: str,
    *,
    preserve_build_cache: bool | None = None,
    cache_paths: list[str] | None = None,
) -> CheckCommandResult:
    """Sync local workspace to remote host via rsync."""
    endpoint = _resolve_remote_endpoint(remote_host)
    sync_remote_path = _resolve_sync_remote_path(remote_host, remote_path)
    result_command = (
        f"rsync to {endpoint.display_host}:{sync_remote_path} (stage workdir: {remote_path})"
    )
    lock_timeout_sec = _read_int_env("MULTI_CODEX_REMOTE_SYNC_LOCK_TIMEOUT_SEC", 120)

    try:
        lock_cm = _acquire_remote_sync_lock(
            endpoint.display_host,
            sync_remote_path,
            timeout_sec=lock_timeout_sec,
        )
        lock_cm.__enter__()
    except TimeoutError:
        return CheckCommandResult(
            command=result_command,
            exit_code=-1,
            stdout="",
            stderr=(
                "TIMEOUT: remote sync lock acquisition exceeded "
                f"{lock_timeout_sec}s for {endpoint.display_host}:{sync_remote_path}"
            ),
            passed=False,
        )
    except Exception as exc:
        return CheckCommandResult(
            command=result_command,
            exit_code=-4,
            stdout="",
            stderr=f"Remote sync lock error: {str(exc)[:400]}",
            passed=False,
        )

    try:
        mkdir_command = _build_ssh_base(endpoint) + [f"mkdir -p {shlex.quote(sync_remote_path)}"]
        try:
            mkdir_proc = subprocess.run(
                mkdir_command,
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return CheckCommandResult(
                command=f"prepare remote dir {endpoint.display_host}:{sync_remote_path}",
                exit_code=-1,
                stdout="",
                stderr="TIMEOUT: remote mkdir exceeded 30s limit",
                passed=False,
            )
        except FileNotFoundError:
            return CheckCommandResult(
                command=f"prepare remote dir {endpoint.display_host}:{sync_remote_path}",
                exit_code=-3,
                stdout="",
                stderr="ssh/sshpass not found in PATH",
                passed=False,
            )

        if mkdir_proc.returncode != 0:
            return CheckCommandResult(
                command=f"prepare remote dir {endpoint.display_host}:{sync_remote_path}",
                exit_code=mkdir_proc.returncode,
                stdout=mkdir_proc.stdout[:MAX_OUTPUT_CHARS] if mkdir_proc.stdout else "",
                stderr=mkdir_proc.stderr[:MAX_OUTPUT_CHARS] if mkdir_proc.stderr else "",
                passed=False,
            )

        ssh_transport = _build_rsync_ssh_transport(endpoint)
        last_result: CheckCommandResult | None = None
        if preserve_build_cache is None:
            preserve_build_cache = _read_bool_env(
                "MULTI_CODEX_REMOTE_SYNC_PRESERVE_BUILD_CACHE",
                False,
            )
        enable_delete = _read_bool_env("MULTI_CODEX_REMOTE_SYNC_DELETE", True)
        cache_exclude_paths = [
            path.strip()
            for path in (cache_paths or [])
            if path.strip()
        ]
        if preserve_build_cache and not cache_exclude_paths:
            cache_exclude_paths = ["build/"]

        for attempt in range(1, 4):
            rsync_command: list[str] = [
                "rsync",
                "-rlz",
                "--no-perms",
                "--no-owner",
                "--no-group",
                "--omit-dir-times",
                "--no-times",
                "-e", ssh_transport,
            ]
            if enable_delete:
                rsync_command.append("--delete")
            if preserve_build_cache:
                for cache_path in cache_exclude_paths:
                    rsync_command.extend(["--exclude", cache_path])
            for pattern in REMOTE_PATH_SENSITIVE_METADATA_PATTERNS:
                rsync_command.extend(["--exclude", pattern])
            rsync_command.extend(
                [
                    f"{local_path}/",
                    f"{endpoint.user}@{endpoint.ssh_host}:{sync_remote_path}/",
                ]
            )
            env = None
            if endpoint.password:
                env = os.environ.copy()
                env["SSHPASS"] = endpoint.password
                rsync_command = ["sshpass", "-e"] + rsync_command
            try:
                proc = subprocess.run(
                    rsync_command,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                last_result = CheckCommandResult(
                    command=result_command,
                    exit_code=-1,
                    stdout="",
                    stderr=f"TIMEOUT: rsync attempt {attempt}/3 exceeded 120s limit",
                    passed=False,
                )
            except FileNotFoundError:
                return CheckCommandResult(
                    command=result_command,
                    exit_code=-3,
                    stdout="",
                    stderr="rsync/sshpass not found in PATH",
                    passed=False,
                )
            else:
                last_result = CheckCommandResult(
                    command=result_command,
                    exit_code=proc.returncode,
                    stdout=proc.stdout[:MAX_OUTPUT_CHARS] if proc.stdout else "",
                    stderr=proc.stderr[:MAX_OUTPUT_CHARS] if proc.stderr else "",
                    passed=proc.returncode == 0,
                )
                if last_result.passed:
                    return last_result

            if last_result is not None:
                logger.warning(
                    "Remote sync attempt %s/3 failed for %s:%s: exit=%s stderr=%s",
                    attempt,
                    endpoint.display_host,
                    sync_remote_path,
                    last_result.exit_code,
                    (last_result.stderr or "").strip()[:1000],
                )
            if attempt < 3:
                import time
                time.sleep(attempt)

        assert last_result is not None
        return last_result
    finally:
        lock_cm.__exit__(None, None, None)


def _cleanup_remote_path_sensitive_metadata(
    remote_host: str,
    remote_workdir: str,
) -> CheckCommandResult:
    endpoint = _resolve_remote_endpoint(remote_host)
    quoted_workdir = shlex.quote(remote_workdir)
    cleanup_fragments = [
        f"rm -f {shlex.quote(str(Path(remote_workdir) / pattern))}"
        for pattern in REMOTE_PATH_SENSITIVE_METADATA_PATTERNS
    ]
    cleanup_command = _build_ssh_base(endpoint) + [
        f"cd {quoted_workdir} && {'; '.join(cleanup_fragments)}"
    ]
    result_command = (
        f"cleanup remote path-sensitive metadata {endpoint.display_host}:{remote_workdir}"
    )
    try:
        proc = subprocess.run(
            cleanup_command,
            shell=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return CheckCommandResult(
            command=result_command,
            exit_code=-1,
            stdout="",
            stderr="TIMEOUT: remote metadata cleanup exceeded 30s limit",
            passed=False,
        )
    except FileNotFoundError:
        return CheckCommandResult(
            command=result_command,
            exit_code=-3,
            stdout="",
            stderr="ssh/sshpass not found in PATH",
            passed=False,
        )
    return CheckCommandResult(
        command=result_command,
        exit_code=proc.returncode,
        stdout=proc.stdout[:MAX_OUTPUT_CHARS] if proc.stdout else "",
        stderr=proc.stderr[:MAX_OUTPUT_CHARS] if proc.stderr else "",
        passed=proc.returncode == 0,
    )


def _resolve_remote_targets(
    stage: StageSpec,
    remote_host: str,
    remote_workdir: str,
    remote_host_node1: str = "",
    remote_workdir_node1: str = "",
) -> list[tuple[str, str]]:
    """Return list of (host, workdir) pairs based on execution_env."""
    targets: list[tuple[str, str]] = []
    env = stage.execution_env

    if env == "node0_container" and remote_host:
        targets.append((remote_host, remote_workdir or str(Path.cwd())))
    elif env == "node1_container" and remote_host_node1:
        targets.append((remote_host_node1, remote_workdir_node1 or remote_workdir or str(Path.cwd())))
    elif env == "node0_and_node1":
        if remote_host:
            targets.append((remote_host, remote_workdir or str(Path.cwd())))
        if remote_host_node1:
            targets.append((remote_host_node1, remote_workdir_node1 or remote_workdir or str(Path.cwd())))

    return targets


def _worker_scoped_remote_workdir(base_workdir: str, worker: str) -> str:
    """Create a deterministic worker-isolated remote workdir."""
    normalized = base_workdir.strip()
    if not normalized:
        return ""
    return f"{normalized.rstrip('/')}/{worker}"


def _validate_remote_preconditions(
    stage: StageSpec,
    remote_host: str,
    remote_workdir: str,
    remote_host_node1: str = "",
    remote_workdir_node1: str = "",
    *,
    remote_commands: list[str] | None = None,
) -> list[CheckCommandResult]:
    if not (remote_commands or []):
        return []

    errors: list[CheckCommandResult] = []

    if stage.execution_env == "node0_container":
        if not remote_host:
            errors.append(_failed_check(
                "remote-precondition:node0",
                "Stage requires node0 remote execution but --remote-host was not provided.",
            ))
        if not remote_workdir:
            errors.append(_failed_check(
                "remote-precondition:node0-workdir",
                "Stage requires node0 remote execution but --remote-workdir was not provided.",
            ))
    elif stage.execution_env == "node1_container":
        if not remote_host_node1:
            errors.append(_failed_check(
                "remote-precondition:node1",
                "Stage requires node1 remote execution but --remote-host-node1 was not provided.",
            ))
        if not (remote_workdir_node1 or remote_workdir):
            errors.append(_failed_check(
                "remote-precondition:node1-workdir",
                "Stage requires node1 remote execution but no node1 workdir was provided.",
            ))
    elif stage.execution_env == "node0_and_node1":
        if not remote_host:
            errors.append(_failed_check(
                "remote-precondition:node0",
                "Stage requires node0 remote execution but --remote-host was not provided.",
            ))
        if not remote_host_node1:
            errors.append(_failed_check(
                "remote-precondition:node1",
                "Stage requires node1 remote execution but --remote-host-node1 was not provided.",
            ))
        if not remote_workdir:
            errors.append(_failed_check(
                "remote-precondition:node0-workdir",
                "Stage requires node0 remote execution but --remote-workdir was not provided.",
            ))
        if not (remote_workdir_node1 or remote_workdir):
            errors.append(_failed_check(
                "remote-precondition:node1-workdir",
                "Stage requires node1 remote execution but no node1 workdir was provided.",
            ))

    return errors


def _resolve_sync_targets(
    stage: StageSpec,
    remote_host: str,
    remote_workdir: str,
    remote_host_node1: str = "",
    remote_workdir_node1: str = "",
) -> list[tuple[str, str]]:
    """Return list of (host, workdir) pairs to sync based on sync_strategy."""
    strategy = stage.sync_strategy
    targets: list[tuple[str, str]] = []

    if strategy in ("sync_to_node0",) and remote_host:
        targets.append((remote_host, remote_workdir or str(Path.cwd())))
    elif strategy == "sync_to_node0_and_node1":
        if remote_host:
            targets.append((remote_host, remote_workdir or str(Path.cwd())))
        if remote_host_node1:
            targets.append((remote_host_node1, remote_workdir_node1 or remote_workdir or str(Path.cwd())))

    return targets


def _check_remote_workdir_exists(
    remote_host: str,
    remote_workdir: str,
    timeout: int = 30,
) -> CheckCommandResult:
    """Fail fast if the remote execution workdir is missing."""
    command = f"test -d {shlex.quote(remote_workdir)}"
    alias = remote_host.strip().lower()
    if alias in NODE_ALIAS_DEFAULTS and DEV_ENV_REMOTE_SCRIPT.exists():
        script_command = [
            str(DEV_ENV_REMOTE_SCRIPT),
            "--node",
            alias,
            "--workdir",
            "/",
            "--cmd",
            command,
        ]
        try:
            proc = subprocess.run(
                script_command,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return CheckCommandResult(
                command=f"[remote:{remote_host}] preflight {command}",
                exit_code=-1,
                stdout="",
                stderr=f"TIMEOUT: remote preflight exceeded {timeout}s limit",
                passed=False,
            )
        except FileNotFoundError:
            return CheckCommandResult(
                command=f"[remote:{remote_host}] preflight {command}",
                exit_code=-3,
                stdout="",
                stderr="dev_env_remote.sh not found in PATH",
                passed=False,
            )
        stderr = proc.stderr[:MAX_OUTPUT_CHARS] if proc.stderr else ""
        if proc.returncode != 0:
            message = (
                f"Remote workdir missing or inaccessible: {remote_workdir}. "
                f"preflight command failed: {command}"
            )
            if stderr:
                message = f"{message}\n{stderr}"
            return _failed_check(
                f"[remote:{remote_host}] preflight {command}",
                message,
                exit_code=proc.returncode,
            )
        return CheckCommandResult(
            command=f"[remote:{remote_host}] preflight {command}",
            exit_code=proc.returncode,
            stdout=proc.stdout[:MAX_OUTPUT_CHARS] if proc.stdout else "",
            stderr=stderr,
            passed=True,
        )

    endpoint = _resolve_remote_endpoint(remote_host)
    ssh_command = _build_ssh_base(endpoint) + [command]
    try:
        proc = subprocess.run(
            ssh_command,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CheckCommandResult(
            command=f"[remote:{endpoint.display_host}] preflight {command}",
            exit_code=-1,
            stdout="",
            stderr=f"TIMEOUT: remote preflight exceeded {timeout}s limit",
            passed=False,
        )
    except FileNotFoundError:
        return CheckCommandResult(
            command=f"[remote:{endpoint.display_host}] preflight {command}",
            exit_code=-3,
            stdout="",
            stderr="ssh not found in PATH",
            passed=False,
        )

    stderr = proc.stderr[:MAX_OUTPUT_CHARS] if proc.stderr else ""
    if proc.returncode != 0:
        message = (
            f"Remote workdir missing or inaccessible: {remote_workdir}. "
            f"preflight command failed: {command}"
        )
        if stderr:
            message = f"{message}\n{stderr}"
        return _failed_check(
            f"[remote:{endpoint.display_host}] preflight {command}",
            message,
            exit_code=proc.returncode,
        )

    return CheckCommandResult(
        command=f"[remote:{endpoint.display_host}] preflight {command}",
        exit_code=proc.returncode,
        stdout=proc.stdout[:MAX_OUTPUT_CHARS] if proc.stdout else "",
        stderr=stderr,
        passed=True,
    )


def _validate_local_command_harness(
    commands: list[str],
    remote_paths: list[str],
) -> list[CheckCommandResult]:
    results: list[CheckCommandResult] = []
    for command in commands:
        if any(remote_path and remote_path in command for remote_path in remote_paths):
            results.append(_failed_check(
                command,
                "Local stage commands must be workspace-relative and must not reference remote workdirs.",
            ))
        elif "ssh " in command or command.startswith("ssh "):
            results.append(_failed_check(
                command,
                "Local stage commands must not shell out to SSH. Use gate_commands_remote instead.",
            ))
        elif "rsync " in command or command.startswith("rsync "):
            results.append(_failed_check(
                command,
                "Local stage commands must not shell out to rsync. Use sync_strategy instead.",
            ))
    return results


def _remote_command_suffix(command: str) -> str:
    if command.startswith("[remote:") and "] " in command:
        return command.split("] ", 1)[1]
    return command


def _resolve_gate_commands_for_tier(stage: StageSpec, gate_tier: GateTier) -> list[str]:
    if stage.gate_commands_remote_tiered:
        return [
            item.command
            for item in stage.gate_commands_remote_tiered
            if item.tier == gate_tier and item.command.strip()
        ]
    if gate_tier == "fast_round":
        return [command for command in stage.gate_commands_remote if command.strip()]
    return []


def _resolve_remote_cache_paths(stage: StageSpec) -> list[str]:
    profile = getattr(stage, "build_strategy", None)
    if profile is not None and profile.remote_cache_paths:
        return [path for path in profile.remote_cache_paths if path.strip()]
    return ["build/"]


def _should_preserve_remote_cache(stage: StageSpec, gate_tier: GateTier) -> bool:
    env_override = os.getenv("MULTI_CODEX_REMOTE_SYNC_PRESERVE_BUILD_CACHE")
    if env_override is not None:
        return _read_bool_env("MULTI_CODEX_REMOTE_SYNC_PRESERVE_BUILD_CACHE", False)

    profile = getattr(stage, "build_strategy", None)
    if profile is not None:
        if gate_tier in profile.preserve_remote_cache_tiers:
            return True
        return bool(profile.preserve_remote_cache_default)
    return False


def _resolve_remote_contracts_for_tier(
    stage: StageSpec,
    gate_tier: GateTier,
) -> list[RemoteGateContract]:
    return [contract for contract in stage.remote_gate_contracts if contract.tier == gate_tier]


def _validate_single_remote_contract(
    contract: RemoteGateContract,
    result: CheckCommandResult,
) -> list[CheckCommandResult]:
    failures: list[CheckCommandResult] = []
    if result.exit_code != contract.required_exit_code:
        failures.append(
            _failed_check(
                result.command,
                (
                    "Remote contract exit-code mismatch: "
                    f"expected={contract.required_exit_code}, actual={result.exit_code}"
                ),
            )
        )

    combined_output = f"{result.stdout}\n{result.stderr}"
    for required in contract.required_substrings:
        if required not in combined_output:
            failures.append(
                _failed_check(
                    result.command,
                    f"Remote contract missing required substring: {required}",
                )
            )

    for pattern in contract.required_regexes:
        try:
            matched = re.search(pattern, combined_output) is not None
        except re.error as exc:
            failures.append(
                _failed_check(
                    result.command,
                    f"Remote contract regex is invalid: {pattern!r} ({exc})",
                )
            )
            continue
        if not matched:
            failures.append(
                _failed_check(
                    result.command,
                    f"Remote contract missing regex match: {pattern!r}",
                )
            )

    if contract.required_json_keys:
        json_text = result.stdout.strip() or result.stderr.strip()
        if not json_text:
            failures.append(
                _failed_check(
                    result.command,
                    "Remote contract expected JSON output but command emitted no output.",
                )
            )
        else:
            payload: object | None = None
            try:
                payload = json.loads(json_text)
            except json.JSONDecodeError as exc:
                start = json_text.find("{")
                end = json_text.rfind("}")
                if start >= 0 and end > start:
                    try:
                        payload = json.loads(json_text[start : end + 1])
                    except json.JSONDecodeError:
                        payload = None
                if payload is None:
                    failures.append(
                        _failed_check(
                            result.command,
                            f"Remote contract expected JSON output but parse failed: {exc}",
                        )
                    )
            if isinstance(payload, dict):
                for key in contract.required_json_keys:
                    if key not in payload:
                        failures.append(
                            _failed_check(
                                result.command,
                                f"Remote contract missing JSON key: {key}",
                            )
                        )
            elif payload is not None:
                failures.append(
                    _failed_check(
                        result.command,
                        (
                            "Remote contract expected JSON object output for key checks, "
                            f"got {type(payload).__name__}."
                        ),
                    )
                )
    return failures


def _validate_remote_contracts(
    stage: StageSpec,
    remote_results: list[CheckCommandResult],
    *,
    gate_tier: GateTier,
) -> list[CheckCommandResult]:
    failures: list[CheckCommandResult] = []
    contracts = _resolve_remote_contracts_for_tier(stage, gate_tier)
    if not contracts:
        return failures

    grouped: dict[str, list[CheckCommandResult]] = {}
    for result in remote_results:
        grouped.setdefault(_remote_command_suffix(result.command), []).append(result)

    for contract in contracts:
        command = contract.command.strip()
        if not command:
            failures.append(
                _failed_check(
                    "remote-contract",
                    "remote_gate_contracts[*].command must be non-empty.",
                )
            )
            continue

        matched_results = grouped.get(command, [])
        if not matched_results:
            failures.append(
                _failed_check(
                    f"remote-contract:{command}",
                    (
                        "Remote contract refers to a command that was not executed: "
                        f"{command}"
                    ),
                )
            )
            continue

        for result in matched_results:
            failures.extend(_validate_single_remote_contract(contract, result))

    return failures


def _validate_remote_results(
    stage: StageSpec,
    remote_results: list[CheckCommandResult],
    *,
    gate_tier: GateTier,
    selected_remote_commands: list[str],
) -> list[CheckCommandResult]:
    results: list[CheckCommandResult] = []
    if selected_remote_commands and not remote_results:
        results.append(_failed_check(
            "remote-gate",
            (
                "Stage declares remote gate commands for "
                f"tier={gate_tier}, but no remote commands were executed."
            ),
        ))
    results.extend(_validate_remote_contracts(stage, remote_results, gate_tier=gate_tier))
    return results


def run_remote_preflight_from_stage_spec(
    worker: str,
    stage: StageSpec,
    workspace: Path,
    remote_host: str = "",
    remote_workdir: str = "",
    remote_host_node1: str = "",
    remote_workdir_node1: str = "",
) -> list[CheckCommandResult]:
    """Validate remote stage prerequisites before worker delivery starts.

    This is intentionally stricter than the post-implementation harness path:
    it front-loads missing SSH/workdir/sync failures so the stage does not spend
    agent budget producing code for a run that can never pass its remote gate.
    """
    effective_remote_workdir = _worker_scoped_remote_workdir(remote_workdir, worker)
    node1_base = remote_workdir_node1 or remote_workdir
    effective_remote_workdir_node1 = _worker_scoped_remote_workdir(node1_base, worker)

    results = _validate_remote_preconditions(
        stage,
        remote_host,
        effective_remote_workdir,
        remote_host_node1,
        effective_remote_workdir_node1,
        remote_commands=stage.gate_commands_remote,
    )
    if results:
        return results

    sync_targets = _resolve_sync_targets(
        stage,
        remote_host,
        effective_remote_workdir,
        remote_host_node1,
        effective_remote_workdir_node1,
    )
    synced_targets: set[tuple[str, str]] = set()
    for host, workdir in sync_targets:
        sync_result = _sync_to_remote(
            workspace,
            host,
            workdir,
            preserve_build_cache=False,
        )
        results.append(sync_result)
        if not sync_result.passed:
            results.append(
                _failed_check(
                    f"remote-preflight-sync:{host}",
                    f"Remote preflight aborted for {host}:{workdir} because workspace sync failed.",
                )
            )
            continue
        cleanup_result = _cleanup_remote_path_sensitive_metadata(host, workdir)
        results.append(cleanup_result)
        if cleanup_result.passed:
            synced_targets.add((host, workdir))
        else:
            results.append(
                _failed_check(
                    f"remote-preflight-cleanup:{host}",
                    f"Remote preflight aborted for {host}:{workdir} because metadata cleanup failed.",
                )
            )

    exec_targets = _resolve_remote_targets(
        stage,
        remote_host,
        effective_remote_workdir,
        remote_host_node1,
        effective_remote_workdir_node1,
    )
    for host, workdir in exec_targets:
        if sync_targets and (host, workdir) not in synced_targets:
            results.append(
                _failed_check(
                    f"remote-preflight-exec:{host}",
                    f"Remote preflight skipped execution probe on {host}:{workdir} because sync did not complete successfully.",
                )
            )
            continue
        workdir_result = _check_remote_workdir_exists(host, workdir)
        results.append(workdir_result)
        if not workdir_result.passed:
            continue
        probe_result = _run_remote_command(
            "pwd >/dev/null && test -w .",
            host,
            workdir,
            timeout=30,
        )
        probe_result.command = f"[remote:{host}] preflight writable-workdir"
        results.append(probe_result)

    return results


def run_checks_from_stage_spec(
    worker: str,
    stage: StageSpec,
    workspace: Path,
    remote_host: str = "",
    remote_workdir: str = "",
    remote_host_node1: str = "",
    remote_workdir_node1: str = "",
    gate_tier: GateTier = "fast_round",
    stage_budget_sec: int | None = None,
    round_budget_sec: int | None = None,
    phase_timeout_cap_sec: int | None = None,
    heartbeat_sink: Callable[[dict[str, Any]], None] | None = None,
) -> AutoCheckResult:
    """Run automated checks using commands defined in StageSpec.

    Respects execution_env and sync_strategy:
    - Syncs workspace to remote targets before running remote commands.
    - Executes selected tier gate commands on all nodes dictated by execution_env.
    - Local commands (test_commands, lint_commands, perf_checks) run locally.
    """
    effective_remote_workdir = _worker_scoped_remote_workdir(remote_workdir, worker)
    node1_base = remote_workdir_node1 or remote_workdir
    effective_remote_workdir_node1 = _worker_scoped_remote_workdir(node1_base, worker)

    remote_path_candidates = [effective_remote_workdir, effective_remote_workdir_node1]
    harness_results = _validate_local_command_harness(
        stage.test_commands + stage.lint_commands + stage.perf_checks,
        remote_path_candidates,
    )

    test_results = _run_local_stage_command_list(
        stage.test_commands, workspace, stage=stage, category="test"
    )
    lint_results = _run_local_stage_command_list(
        stage.lint_commands, workspace, stage=stage, category="lint"
    )
    perf_results = _run_local_stage_command_list(
        stage.perf_checks, workspace, stage=stage, category="perf"
    )

    remote_results: list[CheckCommandResult] = []
    selected_remote_commands = _resolve_gate_commands_for_tier(stage, gate_tier)

    remote_precondition_results = _validate_remote_preconditions(
        stage,
        remote_host,
        effective_remote_workdir,
        remote_host_node1,
        effective_remote_workdir_node1,
        remote_commands=selected_remote_commands,
    )
    harness_results.extend(remote_precondition_results)

    if selected_remote_commands and not remote_precondition_results:
        preserve_remote_cache = _should_preserve_remote_cache(stage, gate_tier)
        remote_cache_paths = _resolve_remote_cache_paths(stage)
        # Step 1: Sync workspace to remote targets based on sync_strategy
        sync_targets = _resolve_sync_targets(
            stage, remote_host, effective_remote_workdir,
            remote_host_node1, effective_remote_workdir_node1,
        )
        synced_targets: set[tuple[str, str]] = set()
        for host, workdir in sync_targets:
            sync_result = _sync_to_remote(
                workspace,
                host,
                workdir,
                preserve_build_cache=preserve_remote_cache,
                cache_paths=remote_cache_paths,
            )
            harness_results.append(sync_result)
            if not sync_result.passed:
                logger.error("Sync to %s:%s failed, skipping remote commands for this target", host, workdir)
                continue
            cleanup_result = _cleanup_remote_path_sensitive_metadata(host, workdir)
            harness_results.append(cleanup_result)
            if cleanup_result.passed:
                synced_targets.add((host, workdir))
            else:
                logger.error(
                    "Remote metadata cleanup failed for %s:%s, skipping remote commands for this target",
                    host,
                    workdir,
                )

        # Step 2: Execute remote commands on all execution targets
        exec_targets = _resolve_remote_targets(
            stage, remote_host, effective_remote_workdir,
            remote_host_node1, effective_remote_workdir_node1,
        )
        remote_command_timeout_sec = _resolve_effective_remote_command_timeout_sec(
            stage_budget_sec=stage_budget_sec,
            round_budget_sec=round_budget_sec,
            phase_timeout_cap_sec=phase_timeout_cap_sec,
        )
        for host, workdir in exec_targets:
            if sync_targets and (host, workdir) not in synced_targets:
                harness_results.append(_failed_check(
                    f"remote-exec:{host}",
                    f"Skipped remote commands on {host}:{workdir} because sync failed.",
                ))
                continue
            preflight_result = _check_remote_workdir_exists(host, workdir)
            harness_results.append(preflight_result)
            if not preflight_result.passed:
                harness_results.append(_failed_check(
                    f"remote-exec:{host}",
                    f"Skipped remote commands on {host}:{workdir} because remote workdir preflight failed.",
                ))
                continue
            node_results = _run_remote_command_list(
                selected_remote_commands,
                host,
                workdir,
                timeout=remote_command_timeout_sec,
                worker=worker,
                stage_name=stage.name,
                gate_tier=gate_tier,
                heartbeat_sink=heartbeat_sink,
            )
            remote_results.extend(node_results)

    harness_results.extend(
        _validate_remote_results(
            stage,
            remote_results,
            gate_tier=gate_tier,
            selected_remote_commands=selected_remote_commands,
        )
    )

    all_remote_passed = all(r.passed for r in remote_results) if remote_results else True
    all_harness_passed = all(r.passed for r in harness_results) if harness_results else True

    return AutoCheckResult(
        worker=worker,
        stage_name=stage.name,
        test_results=test_results + remote_results,
        lint_results=lint_results,
        perf_results=perf_results,
        harness_results=harness_results,
        all_tests_passed=(
            (all(r.passed for r in test_results) if test_results else True)
            and all_remote_passed
        ),
        all_lint_passed=all(r.passed for r in lint_results) if lint_results else True,
        all_perf_passed=all(r.passed for r in perf_results) if perf_results else True,
        all_harness_passed=all_harness_passed,
    )


async def run_checks_from_stage_spec_async(
    worker: str,
    stage: StageSpec,
    workspace: Path,
    remote_host: str = "",
    remote_workdir: str = "",
    remote_host_node1: str = "",
    remote_workdir_node1: str = "",
    gate_tier: GateTier = "fast_round",
    stage_budget_sec: int | None = None,
    round_budget_sec: int | None = None,
    phase_timeout_cap_sec: int | None = None,
    heartbeat_sink: Callable[[dict[str, Any]], None] | None = None,
) -> AutoCheckResult:
    return await asyncio.to_thread(
        run_checks_from_stage_spec,
        worker, stage, workspace,
        remote_host, remote_workdir,
        remote_host_node1, remote_workdir_node1,
        gate_tier,
        stage_budget_sec,
        round_budget_sec,
        phase_timeout_cap_sec,
        heartbeat_sink,
    )


async def run_remote_preflight_from_stage_spec_async(
    worker: str,
    stage: StageSpec,
    workspace: Path,
    remote_host: str = "",
    remote_workdir: str = "",
    remote_host_node1: str = "",
    remote_workdir_node1: str = "",
) -> list[CheckCommandResult]:
    return await asyncio.to_thread(
        run_remote_preflight_from_stage_spec,
        worker,
        stage,
        workspace,
        remote_host,
        remote_workdir,
        remote_host_node1,
        remote_workdir_node1,
    )
