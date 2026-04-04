from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import shutil
import subprocess
from typing import Any

from pydantic import BaseModel


logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 200_000
MAX_A2A_PEER_PAYLOAD_CHARS = 8_000
DEFAULT_TIMEOUT = 600


@dataclass
class OpenCodeAgentConfig:
    role: str
    model: str
    workspace: Path
    goal: str = ""
    backstory: str = ""
    timeout: int = DEFAULT_TIMEOUT
    extra_args: list[str] = field(default_factory=list)


@dataclass
class OpenCodeResult:
    """Mimics the essential shape of an Agent kickoff result."""

    raw: str
    pydantic: BaseModel | None = None


class OpenCodeAgent:
    """Adapter that wraps the ``opencode run`` CLI to behave like an Agent.

    The agent executes ``opencode run`` in a subprocess, targeting the
    configured workspace directory, and returns structured output that
    the flow can parse just like a standard Agent response.

    When an A2A adapter is attached (via :meth:`attach_a2a`), the agent
    can also serve as an A2A server and query A2A peers.
    """

    def __init__(self, config: OpenCodeAgentConfig) -> None:
        self.config = config
        self.role = config.role
        self._a2a_adapter: Any | None = None
        self._validate_opencode_available()

    def attach_a2a(self, adapter: Any) -> None:
        """Attach an A2A adapter for server/client capabilities.

        The adapter should be an ``OpenCodeA2AAdapter`` instance.
        Call ``adapter.start()`` separately to begin serving.
        """
        self._a2a_adapter = adapter

    @property
    def a2a_adapter(self) -> Any | None:
        """Return the attached A2A adapter, if any."""
        return self._a2a_adapter

    @staticmethod
    def _validate_opencode_available() -> None:
        if shutil.which("opencode") is None:
            raise RuntimeError(
                "opencode CLI not found in PATH. "
                "Install opencode first: https://opencode.ai"
            )

    def kickoff(
        self,
        prompt: str,
        response_format: type[BaseModel] | None = None,
        *,
        _skip_a2a_peers: bool = False,
    ) -> OpenCodeResult:
        """Execute a prompt via ``opencode run`` and return the result.

        Parameters
        ----------
        prompt:
            The full prompt text to send to opencode.
        response_format:
            If provided, the prompt is augmented with instructions to
            return JSON matching this Pydantic model schema.
        _skip_a2a_peers:
            Internal flag set to ``True`` when this call originates from
            an inbound A2A ``tasks/send`` request.  Prevents recursive
            peer fan-out (A→B→A→…).
        """
        # Query A2A peers first if an adapter with peers is attached,
        # but skip when serving an inbound A2A request to prevent recursion.
        peer_context = ""
        if not _skip_a2a_peers and self._a2a_adapter is not None:
            peer_responses = self._a2a_adapter.query_peers(prompt)
            if peer_responses:
                peer_sections = []
                for peer_role, peer_text in peer_responses.items():
                    normalized = self._normalize_peer_payload(peer_role, peer_text)
                    if normalized:
                        peer_sections.append(normalized)
                if peer_sections:
                    peer_context = (
                        "\n\n=== A2A Peer Context ===\n"
                        + "\n\n".join(peer_sections)
                        + "\n=== End Peer Context ===\n\n"
                    )

        augmented_prompt = self._build_prompt(
            peer_context + prompt if peer_context else prompt,
            response_format,
        )
        raw_output = self._run_opencode(augmented_prompt)

        parsed_pydantic: BaseModel | None = None
        if response_format is not None:
            parsed_pydantic = self._try_parse_structured(raw_output, response_format)

        return OpenCodeResult(raw=raw_output, pydantic=parsed_pydantic)

    def _build_prompt(
        self,
        prompt: str,
        response_format: type[BaseModel] | None,
    ) -> str:
        parts = [prompt]
        if response_format is not None:
            schema_json = json.dumps(
                response_format.model_json_schema(),
                indent=2,
                ensure_ascii=False,
            )
            parts.append(
                f"\n\nYou MUST respond with ONLY a valid JSON object matching "
                f"this schema:\n```json\n{schema_json}\n```\n"
                f"Do NOT include any text before or after the JSON."
            )
        return "\n".join(parts)

    def _run_opencode(self, prompt: str) -> str:
        command = [
            "opencode",
            "run",
            prompt,
            "-m", self.config.model,
            "--format", "json",
            *self.config.extra_args,
        ]

        logger.info(
            "OpenCodeAgent [%s] executing in %s with model %s",
            self.role,
            self.config.workspace,
            self.config.model,
        )

        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.config.timeout,
                cwd=str(self.config.workspace),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"OpenCodeAgent [{self.role}] timed out after "
                f"{self.config.timeout}s"
            )

        if proc.returncode != 0:
            logger.warning(
                "OpenCodeAgent [%s] exited with code %d.\nstderr: %s",
                self.role,
                proc.returncode,
                proc.stderr[:2000] if proc.stderr else "(empty)",
            )

        raw_output = proc.stdout[:MAX_OUTPUT_CHARS] if proc.stdout else ""

        assistant_text = self._extract_assistant_text(raw_output)
        return assistant_text

    @staticmethod
    def _extract_assistant_text(raw_json_output: str) -> str:
        """Extract the final assistant message text from opencode JSON output.

        ``opencode run --format json`` emits newline-delimited JSON events.
        We look for the last assistant message event and extract its text.
        If parsing fails, fall back to returning the raw output.
        """
        last_text = ""
        for line in raw_json_output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")

            if event_type == "text":
                content = event.get("content", "")
                if isinstance(content, str) and content:
                    last_text = content
                    continue

                text = event.get("text", "")
                if isinstance(text, str) and text:
                    last_text = text
                    continue

                part = event.get("part", {})
                if isinstance(part, dict):
                    part_text = part.get("text", "")
                    if isinstance(part_text, str) and part_text:
                        last_text = part_text
                        continue

            elif event_type == "message":
                role = event.get("role", "")
                content = event.get("content", "")
                if role == "assistant" and isinstance(content, str) and content:
                    last_text = content
                    continue

                part = event.get("part", {})
                if isinstance(part, dict):
                    part_text = part.get("text", "")
                    if isinstance(part_text, str) and part_text:
                        last_text = part_text
                        continue

            if isinstance(event.get("text"), str) and event.get("text"):
                last_text = event["text"]

            elif isinstance(event.get("content"), str) and event.get("content"):
                last_text = event["content"]

        return last_text if last_text else raw_json_output

    @staticmethod
    def _try_parse_structured(
        raw_text: str,
        model_cls: type[BaseModel],
    ) -> BaseModel | None:
        """Attempt to parse the raw text as a Pydantic model."""
        text = raw_text.strip()

        if text.startswith("```"):
            lines = text.splitlines()
            start_index = 1
            end_index = len(lines)
            for index, current_line in enumerate(lines[1:], start=1):
                if current_line.strip() == "```":
                    end_index = index
                    break
            text = "\n".join(lines[start_index:end_index]).strip()

        try:
            payload = json.loads(text)
            return model_cls.model_validate(payload)
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning(
                "Failed to parse opencode output as %s: %s\nRaw (first 500 chars): %s",
                model_cls.__name__,
                exc,
                text[:500],
            )
            return None

    @staticmethod
    def _normalize_peer_payload(peer_role: str, peer_text: str) -> str | None:
        raw = peer_text.strip()
        if not raw:
            return None
        if len(raw) > MAX_A2A_PEER_PAYLOAD_CHARS:
            raw = raw[:MAX_A2A_PEER_PAYLOAD_CHARS] + "\n...<PEER_PAYLOAD_TRUNCATED>..."

        payload: object | None = None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start >= 0 and end > start:
                try:
                    payload = json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    payload = None

        if not isinstance(payload, dict):
            logger.warning(
                "A2A peer [%s] returned non-JSON payload; dropping peer context.",
                peer_role,
            )
            return None

        expected_keys = {
            "stage_name",
            "objective",
            "summary",
            "reports",
            "decisions",
            "required_actions",
        }
        if not any(key in payload for key in expected_keys):
            logger.warning(
                "A2A peer [%s] payload missing expected semantic keys; dropping peer context.",
                peer_role,
            )
            return None

        return json.dumps(
            {"peer_role": peer_role, "payload": payload},
            ensure_ascii=False,
        )


def is_opencode_agent(agent: Any) -> bool:
    """Check if an agent is an OpenCodeAgent instance."""
    return isinstance(agent, OpenCodeAgent)
