"""Lightweight self-contained orchestration runtime.

Replaces the external ``crewai.flow`` dependency with a minimal,
zero-dependency implementation that provides the same public API surface
used by this project:

- ``FlowLite[S]`` – generic base class with automatic Pydantic state
- ``start()``     – decorator that marks the entry-point method
- ``kickoff()``   – synchronous driver that runs the ``@start`` method
"""

from .flow_lite import FlowLite, start

__all__ = ["FlowLite", "start"]
