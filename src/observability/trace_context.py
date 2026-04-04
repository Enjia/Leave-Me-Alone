"""Trace context propagation via contextvars.

Provides a lightweight trace context that flows through async call chains
without explicit parameter passing.  Each context carries a ``trace_id``
(shared across the entire run), a ``span_id`` (unique per logical unit of
work), and an optional ``parent_span_id`` for parent-child relationships.

Usage::

    from observability import new_trace, child_span, with_trace

    # At run start:
    root = new_trace()
    with with_trace(root):
        # All code in this block sees root as the current trace.
        child = child_span()
        with with_trace(child):
            # Nested span with parent_span_id == root.span_id
            ...
"""
from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator

_TRACE_VAR: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "trace_context", default=None,
)


@dataclass(frozen=True)
class TraceContext:
    """Immutable trace context carried through async call chains."""

    trace_id: str
    span_id: str
    parent_span_id: str = ""
    stage_name: str = ""
    round_index: int = 0
    worker: str = ""


def _short_id() -> str:
    """Generate a short (12-char) hex id."""
    return uuid.uuid4().hex[:12]


def new_trace(
    *,
    trace_id: str | None = None,
    stage_name: str = "",
    round_index: int = 0,
    worker: str = "",
) -> TraceContext:
    """Create a new root trace context."""
    return TraceContext(
        trace_id=trace_id or _short_id(),
        span_id=_short_id(),
        stage_name=stage_name,
        round_index=round_index,
        worker=worker,
    )


def child_span(
    *,
    stage_name: str | None = None,
    round_index: int | None = None,
    worker: str | None = None,
) -> TraceContext:
    """Create a child span from the current trace context.

    Inherits ``trace_id`` and uses the current ``span_id`` as
    ``parent_span_id``.  If no current context exists, creates a new root.
    """
    parent = current_trace()
    if parent is None:
        return new_trace(
            stage_name=stage_name or "",
            round_index=round_index or 0,
            worker=worker or "",
        )
    return TraceContext(
        trace_id=parent.trace_id,
        span_id=_short_id(),
        parent_span_id=parent.span_id,
        stage_name=stage_name if stage_name is not None else parent.stage_name,
        round_index=round_index if round_index is not None else parent.round_index,
        worker=worker if worker is not None else parent.worker,
    )


def current_trace() -> TraceContext | None:
    """Return the current trace context, or ``None`` if not set."""
    return _TRACE_VAR.get()


def set_trace(ctx: TraceContext | None) -> contextvars.Token[TraceContext | None]:
    """Set the current trace context and return a reset token."""
    return _TRACE_VAR.set(ctx)


@contextmanager
def with_trace(ctx: TraceContext) -> Generator[TraceContext, None, None]:
    """Context manager that sets the trace context for the enclosed block."""
    token = set_trace(ctx)
    try:
        yield ctx
    finally:
        _TRACE_VAR.reset(token)
