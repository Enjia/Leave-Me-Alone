from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from adapters import check_runner as check_runner_module
from adapters.check_runner import DefaultCheckRunner


def test_default_check_runner_can_split_worker_remote_hosts(monkeypatch) -> None:
    observed_checks: dict[str, str] = {}
    observed_preflight: dict[str, str] = {}

    async def _fake_run_checks_from_stage_spec_async(
        worker: str,
        stage: object,
        workspace: Path,
        *,
        remote_host: str = "",
        remote_workdir: str = "",
        remote_host_node1: str = "",
        remote_workdir_node1: str = "",
        **kwargs: object,
    ) -> object:
        del worker, stage, workspace, kwargs
        observed_checks.update(
            {
                "remote_host": remote_host,
                "remote_workdir": remote_workdir,
                "remote_host_node1": remote_host_node1,
                "remote_workdir_node1": remote_workdir_node1,
            }
        )
        return SimpleNamespace()

    async def _fake_run_remote_preflight_from_stage_spec_async(
        worker: str,
        stage: object,
        workspace: Path,
        *,
        remote_host: str = "",
        remote_workdir: str = "",
        remote_host_node1: str = "",
        remote_workdir_node1: str = "",
    ) -> object:
        del worker, stage, workspace
        observed_preflight.update(
            {
                "remote_host": remote_host,
                "remote_workdir": remote_workdir,
                "remote_host_node1": remote_host_node1,
                "remote_workdir_node1": remote_workdir_node1,
            }
        )
        return SimpleNamespace()

    monkeypatch.setattr(
        check_runner_module,
        "run_checks_from_stage_spec_async",
        _fake_run_checks_from_stage_spec_async,
    )
    monkeypatch.setattr(
        check_runner_module,
        "run_remote_preflight_from_stage_spec_async",
        _fake_run_remote_preflight_from_stage_spec_async,
    )

    runner = DefaultCheckRunner(
        remote_host="node0",
        remote_workdir="/workspace/project-node0",
        remote_host_node1="node1",
        remote_workdir_node1="/workspace/project-node1",
        split_worker_remote_hosts=True,
    )
    stage = SimpleNamespace(execution_env="node0_container")

    asyncio.run(runner.run_stage_checks("worker_b", stage, Path("/tmp/worker_b")))
    asyncio.run(runner.run_remote_preflight("worker_b", stage, Path("/tmp/worker_b")))

    assert observed_checks["remote_host"] == "node1"
    assert observed_checks["remote_workdir"] == "/workspace/project-node1"
    assert observed_checks["remote_host_node1"] == ""
    assert observed_checks["remote_workdir_node1"] == ""
    assert observed_preflight == observed_checks


def test_default_check_runner_keeps_default_routing_without_split(monkeypatch) -> None:
    observed: dict[str, str] = {}

    async def _fake_run_checks_from_stage_spec_async(
        worker: str,
        stage: object,
        workspace: Path,
        *,
        remote_host: str = "",
        remote_workdir: str = "",
        remote_host_node1: str = "",
        remote_workdir_node1: str = "",
        **kwargs: object,
    ) -> object:
        del worker, stage, workspace, kwargs
        observed.update(
            {
                "remote_host": remote_host,
                "remote_workdir": remote_workdir,
                "remote_host_node1": remote_host_node1,
                "remote_workdir_node1": remote_workdir_node1,
            }
        )
        return SimpleNamespace()

    monkeypatch.setattr(
        check_runner_module,
        "run_checks_from_stage_spec_async",
        _fake_run_checks_from_stage_spec_async,
    )

    runner = DefaultCheckRunner(
        remote_host="node0",
        remote_workdir="/workspace/project-node0",
        remote_host_node1="node1",
        remote_workdir_node1="/workspace/project-node1",
        split_worker_remote_hosts=False,
    )
    stage = SimpleNamespace(execution_env="node0_container")

    asyncio.run(runner.run_stage_checks("worker_b", stage, Path("/tmp/worker_b")))

    assert observed["remote_host"] == "node0"
    assert observed["remote_workdir"] == "/workspace/project-node0"
    assert observed["remote_host_node1"] == "node1"
    assert observed["remote_workdir_node1"] == "/workspace/project-node1"
