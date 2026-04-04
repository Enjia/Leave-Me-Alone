"""File-backed event bus: JSONL append-write + incremental tail-read.

Design
------
* **Write path**: ``EventBus.emit(event)`` appends one JSON line to
  ``<runtime_dir>/events.jsonl``.  Writes are atomic at the line level
  (single ``file.write`` call with a trailing newline).
* **Read path**: ``EventBus.tail(since_offset)`` seeks to *since_offset*
  bytes and reads all complete lines from there, returning parsed events
  and the new byte offset.  This lets the monitor do incremental reads
  without re-scanning the whole file.
* **Fail-closed**: any I/O error is logged and swallowed so that a broken
  event bus never crashes the main orchestration loop.
* **Artifact fallback**: the monitor can fall back to the existing JSON
  artifact scan when ``events.jsonl`` is absent or empty.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from .models import RuntimeEvent

if TYPE_CHECKING:
    from .models import (
        AgentCallEvent,
        CheckResultEvent,
        CostEvent,
        PolicyEvent,
        RoundFinishedEvent,
        RoundStartedEvent,
        StageFinishedEvent,
        StageStartedEvent,
    )

logger = logging.getLogger(__name__)

_EVENT_FILENAME = "events.jsonl"
_event_adapter: TypeAdapter[RuntimeEvent] = TypeAdapter(RuntimeEvent)


class EventBus:
    """Append-only JSONL event bus backed by a single file per run."""

    def __init__(self, runtime_dir: Path) -> None:
        self._path = runtime_dir / _EVENT_FILENAME

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def emit(self, event: object) -> None:
        """Append *event* as a single JSON line.  Silently ignores I/O errors."""
        try:
            line = _event_adapter.dump_json(event).decode() + "\n"  # type: ignore[arg-type]
            with self._path.open("a", encoding="utf-8") as file_handle:
                file_handle.write(line)
        except Exception:
            logger.debug("event_bus: failed to emit event", exc_info=True)

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def tail(self, since_offset: int = 0) -> tuple[list[RuntimeEvent], int]:
        """Read events written after *since_offset* bytes.

        Returns ``(events, new_offset)`` where *new_offset* is the byte
        position after the last successfully parsed line.  Pass the returned
        offset back on the next call for incremental reads.
        """
        if not self._path.exists():
            return [], 0

        events: list[RuntimeEvent] = []
        new_offset = since_offset

        try:
            with self._path.open("rb") as file_handle:
                file_handle.seek(since_offset)
                for raw_line in file_handle:
                    stripped = raw_line.strip()
                    if not stripped:
                        new_offset += len(raw_line)
                        continue
                    try:
                        event = _event_adapter.validate_json(stripped)
                        events.append(event)
                        new_offset += len(raw_line)
                    except Exception:
                        logger.debug(
                            "event_bus: skipping malformed line at offset %d",
                            new_offset,
                            exc_info=True,
                        )
                        new_offset += len(raw_line)
        except Exception:
            logger.debug("event_bus: failed to tail events", exc_info=True)

        return events, new_offset

    def all_events(self) -> list[RuntimeEvent]:
        """Return all events from the beginning of the file."""
        events, _ = self.tail(since_offset=0)
        return events

    @property
    def path(self) -> Path:
        return self._path


def make_event_bus(runtime_dir: Path) -> EventBus:
    """Factory used by the facade and monitor."""
    return EventBus(runtime_dir)
