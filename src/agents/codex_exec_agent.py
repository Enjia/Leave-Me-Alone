from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any

from pydantic import BaseModel


logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 200_000
MAX_A2A_PEER_PAYLOAD_CHARS = 8_000
DEFAULT_TIMEOUT = 10_800
DEFAULT_IDLE_TIMEOUT = 600
FS_PROGRESS_SCAN_INTERVAL_SEC = 15.0
ABNORMAL_IDLE_CRITERIA = (
    "no stdout/stderr output AND no workspace file mtime change"
)


@dataclass
class CodexExecAgentConfig:
    role: str
    model: str
    workspace: Path
    sandbox_mode: str = "workspace-write"
    add_dirs: list[str] = field(default_factory=list)
    goal: str = ""
    backstory: str = ""
    timeout: int = DEFAULT_TIMEOUT
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT
    extra_args: list[str] = field(default_factory=list)


@dataclass
class CodexExecUsage:
    """Token usage extracted from a single codex exec invocation."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    latency_sec: float = 0.0


@dataclass
class CodexExecResult:
    """Mimics the essential shape of an Agent kickoff result."""

    raw: str
    pydantic: BaseModel | None = None
    usage: CodexExecUsage | None = None


class CodexExecAgent:
    """Adapter that wraps ``codex exec`` to behave like an Agent."""

    def __init__(self, config: CodexExecAgentConfig) -> None:
        self.config = config
        self.role = config.role
        self._a2a_adapter: Any | None = None
        self._validate_codex_available()

    def attach_a2a(self, adapter: Any) -> None:
        self._a2a_adapter = adapter

    @property
    def a2a_adapter(self) -> Any | None:
        return self._a2a_adapter

    @staticmethod
    def _validate_codex_available() -> None:
        if shutil.which("codex") is None:
            raise RuntimeError(
                "codex CLI not found in PATH. Install/enable codex CLI first."
            )

    def kickoff(
        self,
        prompt: str,
        response_format: type[BaseModel] | None = None,
        *,
        _skip_a2a_peers: bool = False,
        timeout_override_sec: int | None = None,
    ) -> CodexExecResult:
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
        raw_output, usage = self._run_codex_exec(
            augmented_prompt,
            response_format=response_format,
            timeout_override_sec=timeout_override_sec,
        )

        parsed_pydantic: BaseModel | None = None
        if response_format is not None:
            parsed_pydantic = self._try_parse_structured(raw_output, response_format)

        return CodexExecResult(raw=raw_output, pydantic=parsed_pydantic, usage=usage)

    def _build_prompt(
        self,
        prompt: str,
        response_format: type[BaseModel] | None,
    ) -> str:
        parts = [prompt]
        if response_format is not None:
            parts.insert(
                0,
                (
                    "STRUCTURED OUTPUT MODE (MANDATORY): "
                    "Think privately. Your final response must be exactly one JSON object "
                    "for machine parsing. The first non-whitespace character must be '{' "
                    "and the last non-whitespace character must be '}'. Do not output any "
                    "preamble, acknowledgement, progress update, explanation, markdown, "
                    "or code fence."
                ),
            )
            parts.append(
                "\n\nYou MUST respond with ONLY a valid JSON object matching the "
                "provided output schema. Do NOT include any text before or after "
                "the JSON. Any non-JSON output is a hard failure."
            )
        return "\n".join(parts)

    def _run_codex_exec(
        self,
        prompt: str,
        *,
        response_format: type[BaseModel] | None = None,
        timeout_override_sec: int | None = None,
    ) -> tuple[str, "CodexExecUsage"]:
        command = [
            "codex",
            "exec",
            "--json",
            "--model",
            self.config.model,
            "--skip-git-repo-check",
            "--cd",
            str(self.config.workspace),
        ]

        if self.config.sandbox_mode == "workspace-write":
            # Ensure non-interactive runs do not block on approval prompts.
            command.append("--full-auto")
        elif self.config.sandbox_mode == "danger-full-access":
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["--sandbox", self.config.sandbox_mode])

        for add_dir in self.config.add_dirs:
            command.extend(["--add-dir", add_dir])

        command.extend(self.config.extra_args)

        schema_tempfile: tempfile.NamedTemporaryFile[str] | None = None
        if response_format is not None:
            schema_tempfile = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".json",
                delete=False,
            )
            schema_json = json.dumps(
                self._build_codex_output_schema(response_format),
                indent=2,
                ensure_ascii=False,
            )
            schema_tempfile.write(schema_json)
            schema_tempfile.flush()
            schema_tempfile.close()
            command.extend(["--output-schema", schema_tempfile.name])

        command.append("-")

        logger.info(
            "CodexExecAgent [%s] executing in %s with model %s (prompt_chars=%d)",
            self.role,
            self.config.workspace,
            self.config.model,
            len(prompt),
        )

        hard_timeout = self.config.timeout
        if timeout_override_sec is not None and timeout_override_sec > 0:
            hard_timeout = min(hard_timeout, timeout_override_sec)
        idle_timeout = self.config.idle_timeout

        logger.info(
            "CodexExecAgent [%s] watchdog: hard_timeout=%ss idle_timeout=%ss "
            "(abnormal criteria: %s)",
            self.role,
            hard_timeout,
            idle_timeout,
            ABNORMAL_IDLE_CRITERIA,
        )

        proc = subprocess.Popen(
            command,
            cwd=str(self.config.workspace),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )

        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None

        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except BrokenPipeError:
            pass

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_size = 0
        stderr_size = 0

        start_ts = time.monotonic()
        last_progress_ts = start_ts
        last_fs_scan_ts = start_ts
        last_workspace_mtime_ns = self._workspace_latest_mtime_ns(self.config.workspace)

        try:
            while True:
                now = time.monotonic()

                if hard_timeout > 0 and now - start_ts > hard_timeout:
                    self._terminate_process_group(proc)
                    raise RuntimeError(
                        f"CodexExecAgent [{self.role}] hard-timeout after {hard_timeout}s "
                        "(per-agent/per-stage cap)."
                    )

                if (
                    idle_timeout > 0
                    and proc.poll() is None
                    and now - last_progress_ts > idle_timeout
                ):
                    self._terminate_process_group(proc)
                    raise RuntimeError(
                        f"CodexExecAgent [{self.role}] idle-timeout after {idle_timeout}s: "
                        f"{ABNORMAL_IDLE_CRITERIA}."
                    )

                progressed = False

                out_ready = self._stream_has_data(proc.stdout)
                err_ready = self._stream_has_data(proc.stderr)

                if out_ready:
                    chunk = os.read(proc.stdout.fileno(), 4096)
                    if chunk:
                        progressed = True
                        stdout_size = self._append_chunk_limited(
                            stdout_chunks, stdout_size, chunk
                        )

                if err_ready:
                    chunk = os.read(proc.stderr.fileno(), 4096)
                    if chunk:
                        progressed = True
                        stderr_size = self._append_chunk_limited(
                            stderr_chunks, stderr_size, chunk
                        )

                if now - last_fs_scan_ts >= FS_PROGRESS_SCAN_INTERVAL_SEC:
                    latest_mtime_ns = self._workspace_latest_mtime_ns(self.config.workspace)
                    if latest_mtime_ns > last_workspace_mtime_ns:
                        progressed = True
                        last_workspace_mtime_ns = latest_mtime_ns
                    last_fs_scan_ts = now

                if progressed:
                    last_progress_ts = now

                if proc.poll() is not None:
                    break

                time.sleep(0.2)
        finally:
            if schema_tempfile is not None:
                try:
                    os.unlink(schema_tempfile.name)
                except OSError:
                    pass
            try:
                if proc.stdout:
                    stdout_size = self._drain_stream_nonblocking(
                        proc.stdout,
                        stdout_chunks,
                        stdout_size,
                    )
            except Exception:
                pass
            try:
                if proc.stderr:
                    stderr_size = self._drain_stream_nonblocking(
                        proc.stderr,
                        stderr_chunks,
                        stderr_size,
                    )
            except Exception:
                pass

        try:
            return_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._terminate_process_group(proc)
            return_code = proc.wait(timeout=3)
        stdout_text = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        if return_code != 0:
            logger.warning(
                "CodexExecAgent [%s] exited with code %d.\nstderr: %s",
                self.role,
                return_code,
                stderr_text[:2000] if stderr_text else "(empty)",
            )

        raw_output = stdout_text[:MAX_OUTPUT_CHARS] if stdout_text else ""
        elapsed_sec = time.monotonic() - start_ts
        assistant_text, usage = self._extract_assistant_text_and_usage(
            raw_output, model=self.config.model, elapsed_sec=elapsed_sec,
        )

        if return_code != 0 and not assistant_text.strip():
            short_stderr = stderr_text[:2000] if stderr_text else "(empty)"
            raise RuntimeError(
                f"CodexExecAgent [{self.role}] failed with exit code {return_code}. "
                f"stderr: {short_stderr}"
            )

        return assistant_text, usage

    @staticmethod
    def _stream_has_data(stream: Any) -> bool:
        import select

        if stream is None:
            return False
        try:
            rlist, _, _ = select.select([stream], [], [], 0)
            return bool(rlist)
        except Exception:
            return False

    @staticmethod
    def _append_chunk_limited(
        chunks: list[bytes],
        current_size: int,
        chunk: bytes,
    ) -> int:
        if not chunk:
            return current_size

        if current_size == 0:
            keep = chunk[-MAX_OUTPUT_CHARS:]
            chunks.append(keep)
            return len(keep)

        chunks.append(chunk)
        current_size += len(chunk)
        if current_size <= MAX_OUTPUT_CHARS:
            return current_size

        merged = b"".join(chunks)[-MAX_OUTPUT_CHARS:]
        chunks.clear()
        chunks.append(merged)
        return len(merged)

    @staticmethod
    def _drain_stream_nonblocking(
        stream: Any,
        chunks: list[bytes],
        current_size: int,
    ) -> int:
        while CodexExecAgent._stream_has_data(stream):
            try:
                chunk = os.read(stream.fileno(), 4096)
            except OSError:
                break
            if not chunk:
                break
            current_size = CodexExecAgent._append_chunk_limited(
                chunks,
                current_size,
                chunk,
            )
            if current_size >= MAX_OUTPUT_CHARS:
                break
        return current_size

    @staticmethod
    def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                return
        try:
            proc.wait(timeout=3)
            return
        except Exception:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                return

    @staticmethod
    def _workspace_latest_mtime_ns(workspace: Path) -> int:
        latest = 0
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".cache"}]
            for name in files:
                path = Path(root) / name
                try:
                    mtime_ns = path.stat().st_mtime_ns
                except OSError:
                    continue
                if mtime_ns > latest:
                    latest = mtime_ns
        return latest

    @staticmethod
    def _extract_assistant_text_and_usage(
        raw_json_output: str,
        *,
        model: str = "",
        elapsed_sec: float = 0.0,
    ) -> tuple[str, CodexExecUsage]:
        """Extract the final assistant text and aggregated usage from ``codex exec --json`` output."""
        last_text = ""
        total_input = 0
        total_output = 0

        for line in raw_json_output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Accumulate usage from response.completed events.
            event_type = event.get("type", "")
            if event_type == "response.completed":
                usage_data = event.get("response", {}).get("usage", {})
                if isinstance(usage_data, dict):
                    total_input += int(usage_data.get("input_tokens", 0) or 0)
                    total_output += int(usage_data.get("output_tokens", 0) or 0)

            if event_type == "item.completed":
                item = event.get("item", {})
                if isinstance(item, dict):
                    if item.get("type") == "agent_message":
                        text = item.get("text", "")
                        if isinstance(text, str) and text:
                            last_text = text
                            continue
                    text = item.get("content", "")
                    if isinstance(text, str) and text:
                        last_text = text
                        continue

            text = event.get("text", "")
            if isinstance(text, str) and text:
                last_text = text
                continue

            content = event.get("content", "")
            if isinstance(content, str) and content:
                last_text = content
                continue

        usage = CodexExecUsage(
            input_tokens=total_input,
            output_tokens=total_output,
            total_tokens=total_input + total_output,
            model=model,
            latency_sec=round(elapsed_sec, 2),
        )
        return (last_text if last_text else raw_json_output, usage)

    @staticmethod
    def _try_parse_structured(
        raw_text: str,
        model_cls: type[BaseModel],
    ) -> BaseModel | None:
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
                "Failed to parse codex output as %s: %s\nRaw (first 500 chars): %s",
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

    @staticmethod
    def _build_codex_output_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
        schema = json.loads(
            json.dumps(
                model_cls.model_json_schema(),
                ensure_ascii=False,
            )
        )
        CodexExecAgent._normalize_schema_for_codex(schema)
        return schema

    @staticmethod
    def _normalize_schema_for_codex(node: Any) -> None:
        if isinstance(node, dict):
            defs = node.get("$defs")
            if isinstance(defs, dict):
                for value in defs.values():
                    CodexExecAgent._normalize_schema_for_codex(value)

            properties = node.get("properties")
            if isinstance(properties, dict):
                for value in properties.values():
                    CodexExecAgent._normalize_schema_for_codex(value)
                node["required"] = list(properties.keys())
                node["additionalProperties"] = False

            items = node.get("items")
            if items is not None:
                CodexExecAgent._normalize_schema_for_codex(items)

            for key in ("anyOf", "oneOf", "allOf"):
                variants = node.get(key)
                if isinstance(variants, list):
                    for value in variants:
                        CodexExecAgent._normalize_schema_for_codex(value)
        elif isinstance(node, list):
            for value in node:
                CodexExecAgent._normalize_schema_for_codex(value)


def is_codex_exec_agent(agent: Any) -> bool:
    return isinstance(agent, CodexExecAgent)
