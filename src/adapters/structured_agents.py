from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, TypeVar

from agents.codex_exec_agent import CodexExecAgent, is_codex_exec_agent
from agents.opencode_agent import OpenCodeAgent, is_opencode_agent
from ports.agent import AgentPort


logger = logging.getLogger(__name__)
TModel = TypeVar("TModel")


async def invoke_agent_structured(
    flow: object,
    agent: AgentPort,
    prompt: str,
    model_cls: type[TModel],
    *,
    stage_name: str = "",
    stage_deadline_monotonic: float | None = None,
) -> TModel:
    role = getattr(agent, "role", "unknown")
    stage_remaining_sec: int | None = None
    if stage_deadline_monotonic is not None:
        stage_remaining_sec = flow._remaining_stage_budget_sec(
            stage_name=stage_name or "<unknown>",
            stage_deadline_monotonic=stage_deadline_monotonic,
        )
    idle_timeout_hint_sec = getattr(flow, "_current_stage_idle_timeout_sec", None)
    if not isinstance(idle_timeout_hint_sec, int) or idle_timeout_hint_sec <= 0:
        idle_timeout_hint_sec = None

    timeout_candidates: list[int] = []
    agent_timeout_sec = int(flow._resolve_agent_timeout_sec())
    if agent_timeout_sec > 0:
        timeout_candidates.append(agent_timeout_sec)
    if stage_remaining_sec is not None and stage_remaining_sec > 0:
        timeout_candidates.append(stage_remaining_sec)
    if idle_timeout_hint_sec is not None:
        timeout_candidates.append(idle_timeout_hint_sec)
    timeout_override_sec = min(timeout_candidates) if timeout_candidates else None

    logger.info(
        "Invoking agent role=%s target=%s stage_remaining=%s idle_timeout_hint=%s timeout_override=%s",
        role,
        model_cls.__name__,
        f"{stage_remaining_sec}s" if stage_remaining_sec is not None else "n/a",
        f"{idle_timeout_hint_sec}s" if idle_timeout_hint_sec is not None else "n/a",
        f"{timeout_override_sec}s" if timeout_override_sec is not None else "n/a",
    )

    call = asyncio.to_thread(
        invoke_agent_structured_sync,
        flow,
        agent,
        prompt,
        model_cls,
        timeout_override_sec,
        stage_name,
    )

    if is_codex_exec_agent(agent):
        result = await call
        logger.info("Agent role=%s completed target=%s", role, model_cls.__name__)
        return result

    timeout_sec = timeout_override_sec or flow._resolve_agent_timeout_sec()
    if timeout_sec <= 0:
        result = await call
        logger.info("Agent role=%s completed target=%s", role, model_cls.__name__)
        return result
    try:
        result = await asyncio.wait_for(call, timeout=timeout_sec)
        logger.info("Agent role=%s completed target=%s", role, model_cls.__name__)
        return result
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Agent '{role}' timed out after {timeout_sec}s "
            f"while producing {model_cls.__name__} "
            f"(stage={stage_name or 'unknown'})"
        ) from exc


def invoke_agent_structured_sync(
    flow: object,
    agent: AgentPort,
    prompt: str,
    model_cls: type[TModel],
    timeout_override_sec: int | None = None,
    stage_name: str = "",
) -> TModel:
    if is_opencode_agent(agent) or is_codex_exec_agent(agent):
        return invoke_cli_structured_agent(
            flow,
            agent,
            prompt,
            model_cls,
            timeout_override_sec=timeout_override_sec,
            stage_name=stage_name,
        )

    output = agent.kickoff(
        prompt,
        response_format=model_cls,
        timeout_override_sec=timeout_override_sec,
    )
    if asyncio.iscoroutine(output):
        logger.warning(
            "Agent %s returned a coroutine from kickoff(); resolving via asyncio.run().",
            agent.role,
        )
        output = asyncio.run(output)

    if output.pydantic is not None:
        if isinstance(output.pydantic, model_cls):
            return output.pydantic
        return model_cls.model_validate(output.pydantic.model_dump())

    raw = output.raw.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        flow._bump_metric("agent_non_json_count")
        logger.debug(
            "Full non-JSON response from agent %s for %s:\n%s",
            agent.role,
            model_cls.__name__,
            raw,
        )
        raise RuntimeError(
            f"Agent {agent.role} returned non-JSON for {model_cls.__name__} "
            f"(enable DEBUG logging for full output):\n{raw[:800]}"
        ) from exc

    return model_cls.model_validate(payload)


def build_read_only_planner_agent(agent: AgentPort) -> AgentPort:
    if not is_codex_exec_agent(agent):
        return agent
    cfg = agent.config
    from agents.codex_exec_agent import CodexExecAgentConfig

    return CodexExecAgent(
        config=CodexExecAgentConfig(
            role=f"{cfg.role}_planner",
            model=cfg.model,
            workspace=cfg.workspace,
            sandbox_mode="read-only",
            add_dirs=list(cfg.add_dirs),
            goal=f"Read-only planner for {cfg.role}",
            backstory="Read-only planner that produces structured execution plans without editing files.",
            timeout=cfg.timeout,
            idle_timeout=cfg.idle_timeout,
            extra_args=list(cfg.extra_args),
        )
    )


def _record_result_usage(
    flow: object,
    result_obj: Any,
    *,
    agent_role: str,
    stage_name: str,
) -> None:
    """Record usage from a CLI agent result to the flow's cost ledger."""
    usage = getattr(result_obj, "usage", None)
    if usage is None:
        return
    record_fn = getattr(flow, "_record_usage", None)
    if record_fn is None:
        return

    input_tokens = getattr(usage, "input_tokens", 0)
    output_tokens = getattr(usage, "output_tokens", 0)
    model = getattr(usage, "model", "")
    latency_sec = getattr(usage, "latency_sec", 0.0)

    record_fn(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=getattr(usage, "total_tokens", 0),
        model=model,
        agent_role=agent_role,
        stage_name=stage_name,
        latency_sec=latency_sec,
    )

    _emit_agent_call_event(
        flow,
        agent_role=agent_role,
        stage_name=stage_name,
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        model=str(model),
        latency_ms=float(latency_sec) * 1000.0,
    )


def _emit_agent_call_event(
    flow: object,
    *,
    agent_role: str,
    stage_name: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    model: str = "",
    latency_ms: float = 0.0,
) -> None:
    """Emit an AgentCallEvent after recording usage (fail-silent)."""
    emit = getattr(flow, "_emit_event", None)
    if emit is None:
        return
    try:
        from events.models import AgentCallEvent

        # Derive round_index from the last persisted RuntimeStatusSnapshot,
        # which is cached on flow by persist_runtime_status().
        round_index = 0
        last_snapshot = getattr(flow, "_last_runtime_snapshot", None)
        if last_snapshot is not None:
            round_index = int(getattr(last_snapshot, "current_round", 0) or 0)

        emit(AgentCallEvent(
            stage_name=stage_name,
            round_index=round_index,
            agent_role=agent_role,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
        ))
    except Exception:
        pass


def invoke_cli_structured_agent(
    flow: object,
    agent: OpenCodeAgent | CodexExecAgent,
    prompt: str,
    model_cls: type[TModel],
    timeout_override_sec: int | None = None,
    stage_name: str = "",
) -> TModel:
    def _kickoff(current_prompt: str) -> Any:
        if is_codex_exec_agent(agent):
            return agent.kickoff(
                current_prompt,
                response_format=model_cls,
                timeout_override_sec=timeout_override_sec,
            )
        return agent.kickoff(current_prompt, response_format=model_cls)

    def _parse_result(result_obj: Any) -> TModel:
        if result_obj.pydantic is not None:
            if isinstance(result_obj.pydantic, model_cls):
                return result_obj.pydantic
            return model_cls.model_validate(result_obj.pydantic.model_dump())

        raw = result_obj.raw.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            start_index = 1
            end_index = len(lines)
            for index, current_line in enumerate(lines[1:], start=1):
                if current_line.strip() == "```":
                    end_index = index
                    break
            raw = "\n".join(lines[start_index:end_index]).strip()

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            flow._bump_metric("cli_non_json_count")
            raise RuntimeError(
                f"CLI agent [{agent.role}] returned non-JSON for {model_cls.__name__}:\n{raw[:800]}"
            ) from exc
        return model_cls.model_validate(payload)

    agent_role = getattr(agent, "role", "unknown")

    # Pre-check: if immediate_abort enforcement is active, fail early.
    check_fn = getattr(flow, "_check_budget_for_agent_call", None)
    if check_fn is not None:
        check_fn()

    if not is_codex_exec_agent(agent):
        result_obj = _kickoff(prompt)
        _record_result_usage(flow, result_obj, agent_role=agent_role, stage_name=stage_name)
        return _parse_result(result_obj)

    max_attempts = flow._read_positive_env_int("MULTI_CODEX_STRUCTURED_MAX_ATTEMPTS", 4)
    current_prompt = prompt
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        result: Any | None = None
        raw_text = ""
        try:
            result = _kickoff(current_prompt)
        except Exception as exc:
            last_exc = exc
            raw_text = str(exc)
            transient_reason = detect_transient_cli_failure(raw_text)
            if transient_reason:
                flow._bump_metric("cli_transient_retry_count")
                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"CLI agent [{agent.role}] exhausted retries for transient kickoff error "
                        f"while producing {model_cls.__name__}: {transient_reason}"
                    ) from exc
                sleep_sec = min(30, 2 ** (attempt - 1))
                logger.warning(
                    "CLI agent [%s] transient kickoff failure for %s (attempt %d/%d): %s. Retrying in %ss.",
                    agent.role, model_cls.__name__, attempt, max_attempts, transient_reason, sleep_sec
                )
                time.sleep(sleep_sec)
                continue
            raise
        try:
            assert result is not None
            _record_result_usage(flow, result, agent_role=agent_role, stage_name=stage_name)
            return _parse_result(result)
        except Exception as exc:
            last_exc = exc
            if hasattr(result, "raw") and isinstance(result.raw, str):
                raw_text = result.raw
            transient_reason = detect_transient_cli_failure(raw_text)
            if transient_reason:
                flow._bump_metric("cli_transient_retry_count")
                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"CLI agent [{agent.role}] exhausted retries for transient error "
                        f"while producing {model_cls.__name__}: {transient_reason}"
                    ) from exc
                sleep_sec = min(30, 2 ** (attempt - 1))
                logger.warning(
                    "CLI agent [%s] transient failure for %s (attempt %d/%d): %s. Retrying in %ss.",
                    agent.role, model_cls.__name__, attempt, max_attempts, transient_reason, sleep_sec
                )
                time.sleep(sleep_sec)
                continue
            flow._bump_metric("cli_structured_retry_count")
            if attempt >= max_attempts:
                raise
            logger.warning(
                "CLI agent [%s] produced non-structured output for %s (attempt %d/%d); retrying with strict JSON instruction.",
                agent.role, model_cls.__name__, attempt, max_attempts
            )
            current_prompt = (
                "STRUCTURED OUTPUT FAILURE RECOVERY (MANDATORY): "
                "The previous response was rejected because it was not a pure JSON object. "
                "Do not narrate intent, plan, or progress. Do not apologize. "
                "Do not wrap JSON in markdown or code fences. "
                "Your next response must start with '{' and end with '}'.\n\n"
                f"{prompt}\n\n"
                f"FORMAT RETRY ({attempt}/{max_attempts}, MANDATORY): Return ONLY one valid "
                "JSON object matching the schema. No progress narration, no prose, no "
                "markdown, no code fences, and no leading or trailing text."
            )
    assert last_exc is not None
    raise last_exc


def detect_transient_cli_failure(raw_text: str) -> str | None:
    if not raw_text:
        return None
    structured_candidates: list[str] = []
    fallback_candidates: list[str] = []
    normalized = raw_text.strip()
    if normalized:
        fallback_candidates.append(normalized)
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type", ""))
        message = payload.get("message", "")
        if isinstance(message, str) and message:
            structured_candidates.append(message)
        if event_type == "turn.failed":
            err = payload.get("error", {})
            if isinstance(err, dict):
                err_message = err.get("message", "")
                if isinstance(err_message, str) and err_message:
                    structured_candidates.append(err_message)
    keywords = (
        "429", "too many requests", "rate limit", "exceeded retry limit",
        "temporarily unavailable", "service unavailable", "gateway timeout",
        "connection reset", "timed out", "timeout", "network error",
    )
    for text in structured_candidates + fallback_candidates:
        lowered = text.lower()
        if any(keyword in lowered for keyword in keywords):
            return text[:300]
    return None
