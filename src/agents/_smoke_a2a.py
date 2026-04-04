#!/usr/bin/env python3
"""Minimal smoke test for OpenCodeAgent A2A adapter.

Validates:
  1. Server starts and serves agent-card + JSON-RPC on root path
  2. Server starts and serves agent-card + JSON-RPC on /a2a path prefix
  3. Startup rollback: partial failure shuts down already-started adapters
  4. Shutdown releases port (no address-already-in-use on restart)
  5. POST to wrong path returns 404

Run:
    python3 -m agents._smoke_a2a
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.request


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http_get(url: str, timeout: int = 3) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode() if exc.fp else ""


def _http_post(url: str, body: dict, timeout: int = 3) -> tuple[int, str]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode() if exc.fp else ""


class _FakeOpenCodeAgent:
    """Minimal stub that satisfies OpenCodeA2AAdapter's interface."""

    def __init__(self, role: str) -> None:
        self.role = role
        self._a2a_adapter = None

    def attach_a2a(self, adapter):
        self._a2a_adapter = adapter

    @property
    def a2a_adapter(self):
        return self._a2a_adapter

    def kickoff(self, prompt: str, *, _skip_a2a_peers: bool = False):
        class _Result:
            raw = f"echo from {self.role}: {prompt[:50]}"
        return _Result()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_root_path_server():
    """Test 1: Server on root path serves agent-card and JSON-RPC."""
    from .opencode_a2a_adapter import A2AAdapterConfig, OpenCodeA2AAdapter

    port = _find_free_port()
    agent = _FakeOpenCodeAgent("test_root")
    config = A2AAdapterConfig(server_url=f"http://127.0.0.1:{port}", peers=[])
    adapter = OpenCodeA2AAdapter(agent, config)

    adapter.start()
    time.sleep(0.3)

    try:
        # GET agent-card
        status, body = _http_get(f"http://127.0.0.1:{port}/.well-known/agent-card.json")
        assert status == 200, f"Expected 200, got {status}"
        card = json.loads(body)
        assert card["name"] == "opencode-test_root", f"Unexpected name: {card['name']}"

        # POST JSON-RPC tasks/send
        rpc_body = {
            "jsonrpc": "2.0", "id": 1, "method": "tasks/send",
            "params": {"id": "t1", "message": {"role": "user", "parts": [{"type": "text", "text": "hello"}]}},
        }
        status, body = _http_post(f"http://127.0.0.1:{port}/", rpc_body)
        assert status == 200, f"Expected 200, got {status}"
        result = json.loads(body)
        assert result["result"]["status"]["state"] == "completed"

        # GET wrong path → 404
        status, _ = _http_get(f"http://127.0.0.1:{port}/wrong")
        assert status == 404, f"Expected 404, got {status}"

        print("  PASS: test_root_path_server")
    finally:
        adapter.shutdown()


def test_prefix_path_server():
    """Test 2: Server with /a2a prefix routes correctly."""
    from .opencode_a2a_adapter import A2AAdapterConfig, OpenCodeA2AAdapter

    port = _find_free_port()
    agent = _FakeOpenCodeAgent("test_prefix")
    config = A2AAdapterConfig(server_url=f"http://127.0.0.1:{port}/a2a", peers=[])
    adapter = OpenCodeA2AAdapter(agent, config)

    adapter.start()
    time.sleep(0.3)

    try:
        # GET agent-card at /a2a/.well-known/agent-card.json
        status, body = _http_get(f"http://127.0.0.1:{port}/a2a/.well-known/agent-card.json")
        assert status == 200, f"Expected 200, got {status}"
        card = json.loads(body)
        assert "test_prefix" in card["name"]

        # POST JSON-RPC at /a2a/
        rpc_body = {
            "jsonrpc": "2.0", "id": 1, "method": "tasks/send",
            "params": {"id": "t2", "message": {"role": "user", "parts": [{"type": "text", "text": "prefix test"}]}},
        }
        status, body = _http_post(f"http://127.0.0.1:{port}/a2a/", rpc_body)
        assert status == 200, f"Expected 200, got {status}"
        result = json.loads(body)
        assert result["result"]["status"]["state"] == "completed"

        # GET agent-card at root (no prefix) → 404
        status, _ = _http_get(f"http://127.0.0.1:{port}/.well-known/agent-card.json")
        assert status == 404, f"Expected 404 for root agent-card, got {status}"

        # POST at root → 404
        status, _ = _http_post(f"http://127.0.0.1:{port}/", rpc_body)
        assert status == 404, f"Expected 404 for root POST, got {status}"

        print("  PASS: test_prefix_path_server")
    finally:
        adapter.shutdown()


def test_shutdown_releases_port():
    """Test 3: After shutdown, port can be reused immediately."""
    from .opencode_a2a_adapter import A2AAdapterConfig, OpenCodeA2AAdapter

    port = _find_free_port()
    agent = _FakeOpenCodeAgent("test_reuse")
    config = A2AAdapterConfig(server_url=f"http://127.0.0.1:{port}", peers=[])

    # First start + shutdown
    adapter1 = OpenCodeA2AAdapter(agent, config)
    adapter1.start()
    time.sleep(0.2)
    adapter1.shutdown()
    time.sleep(0.2)

    # Second start on same port should succeed
    adapter2 = OpenCodeA2AAdapter(agent, config)
    try:
        adapter2.start()
        time.sleep(0.2)
        status, _ = _http_get(f"http://127.0.0.1:{port}/.well-known/agent-card.json")
        assert status == 200, f"Expected 200 on reuse, got {status}"
        print("  PASS: test_shutdown_releases_port")
    finally:
        adapter2.shutdown()


def test_startup_rollback():
    """Test 4: If one adapter fails to start, already-started ones are rolled back."""
    from .opencode_a2a_adapter import A2AAdapterConfig, OpenCodeA2AAdapter

    port = _find_free_port()

    agent1 = _FakeOpenCodeAgent("rollback_a")
    config1 = A2AAdapterConfig(server_url=f"http://127.0.0.1:{port}", peers=[])
    adapter1 = OpenCodeA2AAdapter(agent1, config1)

    # Second adapter on SAME port → will fail
    agent2 = _FakeOpenCodeAgent("rollback_b")
    config2 = A2AAdapterConfig(server_url=f"http://127.0.0.1:{port}", peers=[])
    adapter2 = OpenCodeA2AAdapter(agent2, config2)

    # Simulate AgentBundle.start_a2a() with rollback
    started = []
    rolled_back = False
    try:
        for adapter in [adapter1, adapter2]:
            adapter.start()
            started.append(adapter)
    except Exception:
        rolled_back = True
        for adapter in reversed(started):
            adapter.shutdown()

    assert rolled_back, "Expected startup failure due to port conflict"

    # Verify port is released after rollback
    time.sleep(0.2)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            print("  PASS: test_startup_rollback")
        except OSError:
            print("  FAIL: test_startup_rollback — port not released after rollback")
            sys.exit(1)


def test_failed_task_returns_failed_status():
    """Test 5: tasks/send that raises returns status.state='failed'."""
    from .opencode_a2a_adapter import A2AAdapterConfig, OpenCodeA2AAdapter

    port = _find_free_port()

    class _FailingAgent:
        role = "fail_agent"
        _a2a_adapter = None
        def attach_a2a(self, a): self._a2a_adapter = a
        def kickoff(self, prompt, *, _skip_a2a_peers=False):
            raise RuntimeError("intentional failure")

    agent = _FailingAgent()
    config = A2AAdapterConfig(server_url=f"http://127.0.0.1:{port}", peers=[])
    adapter = OpenCodeA2AAdapter(agent, config)
    adapter.start()
    time.sleep(0.3)

    try:
        rpc_body = {
            "jsonrpc": "2.0", "id": 1, "method": "tasks/send",
            "params": {"id": "t_fail", "message": {"role": "user", "parts": [{"type": "text", "text": "fail me"}]}},
        }
        status, body = _http_post(f"http://127.0.0.1:{port}/", rpc_body)
        assert status == 200, f"Expected 200, got {status}"
        result = json.loads(body)
        assert result["result"]["status"]["state"] == "failed", (
            f"Expected 'failed', got {result['result']['status']['state']}"
        )
        print("  PASS: test_failed_task_returns_failed_status")
    finally:
        adapter.shutdown()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== OpenCode A2A Adapter Smoke Tests ===\n")

    tests = [
        test_root_path_server,
        test_prefix_path_server,
        test_shutdown_releases_port,
        test_startup_rollback,
        test_failed_task_returns_failed_status,
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            print(f"  FAIL: {test_fn.__name__} — {exc}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    print("All smoke tests passed! ✅")


if __name__ == "__main__":
    main()
