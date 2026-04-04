"""Observability primitives: structured logging and trace context.

Public API
----------
- ``TraceContext``        – Immutable trace context (trace_id, span_id, parent_span_id)
- ``current_trace``       – Get the current trace context from contextvars
- ``new_trace``           – Create a new root trace context
- ``child_span``          – Create a child span from the current context
- ``with_trace``          – Context manager that sets the trace context
- ``structured_log``      – Emit a structured log entry with trace context
- ``get_structured_logger`` – Get a logger that auto-injects trace context
"""

from .trace_context import (
    TraceContext,
    child_span,
    current_trace,
    new_trace,
    with_trace,
)
from .structured_logging import get_structured_logger, structured_log

__all__ = [
    "TraceContext",
    "child_span",
    "current_trace",
    "get_structured_logger",
    "new_trace",
    "structured_log",
    "with_trace",
]
