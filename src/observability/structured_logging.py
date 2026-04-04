"""Structured logging with automatic trace context injection.

Provides helpers that emit log records enriched with trace context fields
(``trace_id``, ``span_id``, ``stage_name``, ``round_index``, ``worker``).
These fields are automatically extracted from the current ``TraceContext``
in ``contextvars``, so callers do not need to pass them explicitly.

Usage::

    from observability import get_structured_logger

    logger = get_structured_logger(__name__)
    logger.info("check passed", check_name="lint", duration_sec=1.2)
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .trace_context import current_trace


def structured_log(
    logger: logging.Logger,
    level: int,
    message: str,
    **extra: Any,
) -> None:
    """Emit a structured log entry with trace context.

    The log record's ``extra`` dict is populated with trace context fields
    and any additional keyword arguments.  A JSON-formatted ``structured``
    field is added for machine-parseable consumption.
    """
    trace = current_trace()
    structured_fields: dict[str, Any] = {
        "ts": time.time(),
        "msg": message,
    }

    if trace is not None:
        structured_fields["trace_id"] = trace.trace_id
        structured_fields["span_id"] = trace.span_id
        if trace.parent_span_id:
            structured_fields["parent_span_id"] = trace.parent_span_id
        if trace.stage_name:
            structured_fields["stage"] = trace.stage_name
        if trace.round_index:
            structured_fields["round"] = trace.round_index
        if trace.worker:
            structured_fields["worker"] = trace.worker

    structured_fields.update(extra)

    logger.log(
        level,
        "%s | %s",
        message,
        json.dumps(structured_fields, default=str, ensure_ascii=False),
        extra={"structured": structured_fields},
    )


class StructuredLogger:
    """Wrapper around ``logging.Logger`` that auto-injects trace context.

    Usage::

        logger = StructuredLogger(logging.getLogger(__name__))
        logger.info("check passed", check_name="lint")
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    @property
    def name(self) -> str:
        return self._logger.name

    def debug(self, message: str, **extra: Any) -> None:
        structured_log(self._logger, logging.DEBUG, message, **extra)

    def info(self, message: str, **extra: Any) -> None:
        structured_log(self._logger, logging.INFO, message, **extra)

    def warning(self, message: str, **extra: Any) -> None:
        structured_log(self._logger, logging.WARNING, message, **extra)

    def error(self, message: str, **extra: Any) -> None:
        structured_log(self._logger, logging.ERROR, message, **extra)

    def exception(self, message: str, **extra: Any) -> None:
        structured_log(self._logger, logging.ERROR, message, **extra)
        # Also log the traceback via the standard logger
        self._logger.debug("Traceback for: %s", message, exc_info=True)


def get_structured_logger(name: str) -> StructuredLogger:
    """Create a ``StructuredLogger`` wrapping ``logging.getLogger(name)``."""
    return StructuredLogger(logging.getLogger(name))
