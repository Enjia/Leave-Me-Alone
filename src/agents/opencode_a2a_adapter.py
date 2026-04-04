"""A2A (Agent-to-Agent) adapter for OpenCodeAgent.

Provides a lightweight HTTP server that exposes the Google A2A protocol
(agent-card + JSON-RPC task endpoint) and an HTTP client that can send
tasks to peer A2A agents.  This bridges the gap between the subprocess-
based OpenCodeAgent and the network-based A2A protocol so that
``--enable-a2a`` works with ``--provider opencode``.

Server lifecycle:
    adapter = OpenCodeA2AAdapter(agent, server_url, peer_endpoints)
    adapter.start()       # spawns background thread with HTTP server
    ...                   # agent.kickoff() works as before; peers can call in
    adapter.shutdown()    # stops the HTTP server

Client usage (automatic):
    When ``peer_endpoints`` is non-empty, every ``kickoff()`` call on the
    wrapped agent will first broadcast the prompt to all peers and collect
    their responses, which are appended to the prompt context before the
    local opencode subprocess runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlsplit
import uuid


if TYPE_CHECKING:
    from .opencode_agent import OpenCodeAgent

logger = logging.getLogger(__name__)

AGENT_CARD_PATH = "/.well-known/agent-card.json"
JSONRPC_PATH = "/"

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

@dataclass
class A2APeerConfig:
    """Configuration for a single A2A peer agent."""
    role: str
    endpoint: str
    timeout: int = 30
    max_turns: int = 5


@dataclass
class A2AAdapterConfig:
    """Full A2A adapter configuration for an OpenCodeAgent."""
    server_url: str = ""
    peers: list[A2APeerConfig] = field(default_factory=list)


def _build_agent_card(role: str, server_url: str) -> dict[str, Any]:
    """Build a minimal A2A agent-card JSON object."""
    return {
        "name": f"opencode-{role}",
        "description": f"OpenCode-based agent playing the '{role}' role in multi-codex review.",
        "url": server_url,
        "version": "0.1.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
        },
        "skills": [
            {
                "id": "code-review",
                "name": "Code Review & Implementation",
                "description": "Implements code changes and performs code review.",
            }
        ],
    }


def _make_jsonrpc_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _make_jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# A2A HTTP Server (runs in a background thread)
# ---------------------------------------------------------------------------

class _A2ARequestHandler(BaseHTTPRequestHandler):
    """Handles A2A protocol requests: agent-card GET and JSON-RPC POST."""

    server: _A2AHTTPServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("A2A server [%s]: %s", self.server.agent_role, format % args)

    def _strip_prefix(self, path: str) -> str | None:
        """Strip the server's path prefix from the request path.

        Returns the path with the prefix removed, or ``None`` if a prefix
        is configured but the request path does not start with it.
        """
        prefix = self.server.path_prefix
        if not prefix:
            return path
        if path == prefix:
            return "/"
        if path.startswith(prefix + "/"):
            return path[len(prefix):] or "/"
        return None

    def do_GET(self) -> None:
        request_path = urlsplit(self.path).path
        effective_path = self._strip_prefix(request_path)
        if effective_path is not None and effective_path == AGENT_CARD_PATH:
            body = json.dumps(self.server.agent_card).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        request_path = urlsplit(self.path).path
        effective_path = self._strip_prefix(request_path)
        if effective_path is None or effective_path != JSONRPC_PATH:
            self.send_error(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        try:
            request = json.loads(raw_body)
        except json.JSONDecodeError:
            self._send_json(400, _make_jsonrpc_error(None, -32700, "Parse error"))
            return

        method = request.get("method", "")
        request_id = request.get("id")
        params = request.get("params", {})

        if method == "tasks/send":
            self._handle_task_send(request_id, params)
        elif method == "tasks/get":
            self._handle_task_get(request_id, params)
        else:
            self._send_json(200, _make_jsonrpc_error(request_id, -32601, f"Method not found: {method}"))

    def _handle_task_send(self, request_id: Any, params: dict[str, Any]) -> None:
        """Handle tasks/send: extract prompt from message, delegate to OpenCodeAgent."""
        task_id = params.get("id", str(uuid.uuid4()))
        message = params.get("message", {})
        parts = message.get("parts", [])

        prompt_text = ""
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                prompt_text += part.get("text", "")

        if not prompt_text:
            self._send_json(200, _make_jsonrpc_error(request_id, -32602, "No text part in message"))
            return

        agent = self.server.agent_ref
        if agent is None:
            self._send_json(200, _make_jsonrpc_error(request_id, -32603, "Agent not available"))
            return

        try:
            result = agent.kickoff(prompt_text, _skip_a2a_peers=True)
            response_text = result.raw
        except Exception as exc:
            logger.exception("A2A task execution failed for task %s", task_id)
            task_result = {
                "id": task_id,
                "status": {
                    "state": "failed",
                    "message": {"role": "agent", "parts": [{"type": "text", "text": str(exc)}]},
                },
            }
            self._send_json(200, _make_jsonrpc_response(request_id, task_result))
            return

        task_result = {
            "id": task_id,
            "status": {"state": "completed"},
            "artifacts": [
                {
                    "parts": [{"type": "text", "text": response_text}],
                }
            ],
        }
        self._send_json(200, _make_jsonrpc_response(request_id, task_result))

    def _handle_task_get(self, request_id: Any, params: dict[str, Any]) -> None:
        """Handle tasks/get: return task not found (we don't persist tasks)."""
        task_id = params.get("id", "unknown")
        self._send_json(
            200,
            _make_jsonrpc_error(request_id, -32604, f"Task {task_id} not found (stateless agent)"),
        )

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _A2AHTTPServer(HTTPServer):
    """HTTPServer subclass that holds references to agent state."""

    def __init__(
        self,
        server_address: tuple[str, int],
        agent_role: str,
        agent_card: dict[str, Any],
        agent_ref: OpenCodeAgent | None,
        path_prefix: str = "",
    ) -> None:
        super().__init__(server_address, _A2ARequestHandler)
        self.agent_role = agent_role
        self.agent_card = agent_card
        self.agent_ref: OpenCodeAgent | None = agent_ref
        # Normalized path prefix (e.g. "/a2a") without trailing slash
        self.path_prefix = path_prefix.rstrip("/") if path_prefix else ""


# ---------------------------------------------------------------------------
# A2A Client
# ---------------------------------------------------------------------------

def _send_task_to_peer(
    peer: A2APeerConfig,
    prompt: str,
    timeout: int | None = None,
) -> str | None:
    """Send a tasks/send request to a peer A2A agent and return the response text.

    Uses urllib to avoid adding httpx/requests as a hard dependency.
    Returns None on failure (logged but not raised).
    """
    import urllib.error
    import urllib.request

    effective_timeout = timeout or peer.timeout

    # Resolve JSON-RPC endpoint from the peer endpoint URL, preserving path prefix
    parsed = urlparse(peer.endpoint)
    if parsed.path.endswith("agent-card.json"):
        # Strip /.well-known/agent-card.json to get the base path
        base_path = parsed.path.rsplit("/.well-known/agent-card.json", 1)[0]
        base_url = f"{parsed.scheme}://{parsed.netloc}{base_path}"
    else:
        base_url = peer.endpoint.rstrip("/")

    jsonrpc_url = base_url + "/"

    task_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tasks/send",
        "params": {
            "id": task_id,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": prompt}],
            },
        },
    }

    request_body = json.dumps(payload).encode()
    req = urllib.request.Request(
        jsonrpc_url,
        data=request_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
            response_data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Failed to send A2A task to peer %s at %s: %s", peer.role, jsonrpc_url, exc)
        return None

    if "error" in response_data:
        logger.warning(
            "A2A peer %s returned error: %s",
            peer.role,
            response_data["error"].get("message", "unknown"),
        )
        return None

    result = response_data.get("result", {})
    status = result.get("status", {})
    state = status.get("state")
    if state and state != "completed":
        logger.warning(
            "A2A peer %s returned non-completed state: %s",
            peer.role,
            state,
        )
        return None
    artifacts = result.get("artifacts", [])
    response_parts: list[str] = []
    for artifact in artifacts:
        for part in artifact.get("parts", []):
            if isinstance(part, dict) and part.get("type") == "text":
                response_parts.append(part.get("text", ""))

    return "\n".join(response_parts) if response_parts else None


def query_a2a_peers(
    peers: list[A2APeerConfig],
    prompt: str,
) -> dict[str, str]:
    """Query all A2A peers with a prompt and return {role: response_text}.

    Peers that fail or return empty responses are omitted from the result.
    """
    results: dict[str, str] = {}
    for peer in peers:
        response = _send_task_to_peer(peer, prompt)
        if response:
            results[peer.role] = response
    return results


# ---------------------------------------------------------------------------
# Main Adapter
# ---------------------------------------------------------------------------

class OpenCodeA2AAdapter:
    """Wraps an OpenCodeAgent with A2A server and client capabilities.

    Usage::

        adapter = OpenCodeA2AAdapter(agent, config)
        adapter.start()       # starts HTTP server in background thread
        # ... use agent normally; peers can call in via A2A protocol
        adapter.shutdown()    # stops the server
    """

    def __init__(
        self,
        agent: OpenCodeAgent,
        config: A2AAdapterConfig,
    ) -> None:
        self.agent = agent
        self.config = config
        self._server: _A2AHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._started = False

    @property
    def peers(self) -> list[A2APeerConfig]:
        return self.config.peers

    @property
    def server_url(self) -> str:
        return self.config.server_url

    def start(self) -> None:
        """Start the A2A HTTP server in a background daemon thread."""
        if self._started:
            return

        if not self.config.server_url:
            logger.info(
                "A2A adapter for [%s]: no server_url configured, "
                "skipping server start (client-only mode).",
                self.agent.role,
            )
            self._started = True
            return

        parsed = urlparse(self.config.server_url)
        host = parsed.hostname or "0.0.0.0"
        port = parsed.port or 8080
        path_prefix = parsed.path.rstrip("/") if parsed.path else ""

        agent_card = _build_agent_card(self.agent.role, self.config.server_url)

        self._server = _A2AHTTPServer(
            server_address=(host, port),
            agent_role=self.agent.role,
            agent_card=agent_card,
            agent_ref=self.agent,
            path_prefix=path_prefix,
        )

        self._server_thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"a2a-server-{self.agent.role}",
            daemon=True,
        )
        self._server_thread.start()
        self._started = True

        logger.info(
            "A2A server started for [%s] at %s:%d",
            self.agent.role,
            host,
            port,
        )

    def shutdown(self) -> None:
        """Stop the A2A HTTP server and release the listening socket."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._server_thread is not None:
            self._server_thread.join(timeout=5)
            self._server_thread = None
        self._started = False
        logger.info("A2A adapter shut down for [%s]", self.agent.role)

    def query_peers(self, prompt: str) -> dict[str, str]:
        """Send a prompt to all configured A2A peers and collect responses."""
        if not self.config.peers:
            return {}
        return query_a2a_peers(self.config.peers, prompt)


def build_opencode_a2a_config(
    role: str,
    cfg: Any,
) -> A2AAdapterConfig | None:
    """Build an A2AAdapterConfig from RuntimeConfig for an OpenCodeAgent.

    Mirrors the logic of ``_build_a2a_configs`` in agents.py but produces
    an ``A2AAdapterConfig`` instead of external A2A config objects.

    Returns None if A2A is not enabled or no endpoints are configured.
    """
    if not getattr(cfg, "enable_a2a", False):
        return None

    endpoints: dict[str, str] = getattr(cfg, "a2a_endpoints", {})
    if not endpoints:
        logger.warning(
            "A2A enabled for opencode but --a2a-endpoints is empty. "
            "No A2A connections will be configured for [%s].",
            role,
        )
        return None

    server_url = endpoints.get(role, "")

    peers: list[A2APeerConfig] = []
    for peer_role, peer_url in endpoints.items():
        if peer_role == role:
            continue
        # Normalize to agent-card endpoint for discovery
        cleaned = peer_url.rstrip("/")
        if not cleaned.endswith("agent-card.json"):
            cleaned = f"{cleaned}/.well-known/agent-card.json"
        peers.append(
            A2APeerConfig(
                role=peer_role,
                endpoint=cleaned,
                timeout=30,
                max_turns=5,
            )
        )

    if not server_url and not peers:
        logger.warning(
            "A2A enabled but no server URL or peers configured for [%s].",
            role,
        )
        return None

    return A2AAdapterConfig(server_url=server_url, peers=peers)
