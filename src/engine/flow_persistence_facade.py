from __future__ import annotations

from typing import Any

from events.event_bus import EventBus, make_event_bus

class FlowPersistenceFacadeMixin:
    def _persistence_service(self) -> object:
        # Explicit service boundary for tests and readability:
        # all persistence goes through self.persistence_service.<method>.
        return self.persistence_service

    # ------------------------------------------------------------------
    # Event bus — lazy-initialized on first use
    # ------------------------------------------------------------------

    @property
    def _event_bus(self) -> EventBus:
        if not hasattr(self, "_event_bus_instance"):
            runtime_dir = getattr(self, "runtime_dir", None)
            if runtime_dir is None:
                cfg = getattr(self, "cfg", None)
                runtime_dir = getattr(cfg, "runtime_dir", None)
            from pathlib import Path
            self._event_bus_instance = make_event_bus(
                Path(runtime_dir) if runtime_dir else Path(".")
            )
        return self._event_bus_instance

    def _emit_event(self, event: object) -> None:
        """Emit a typed runtime event to the event stream (fail-silent).

        Automatically injects ``trace_id`` and ``span_id`` from the
        current ``TraceContext`` if the event has those fields and they
        are empty.
        """
        try:
            from .observability.trace_context import current_trace
            trace = current_trace()
            if trace is not None:
                if hasattr(event, "trace_id") and not getattr(event, "trace_id", ""):
                    object.__setattr__(event, "trace_id", trace.trace_id)
                if hasattr(event, "span_id") and not getattr(event, "span_id", ""):
                    object.__setattr__(event, "span_id", trace.span_id)
        except Exception:
            pass
        self._event_bus.emit(event)

    def __getattr__(self, name: str) -> Any:
        """Delegate persistence helpers to ``self.persistence_service``.

        This keeps flow/orchestrator code stable while enforcing a service
        boundary: facade methods named ``_persist_*`` (and
        ``_persist_and_build_*``) are dynamically bridged to matching
        ``PersistenceService`` methods (without the leading underscore).
        """
        if not (name.startswith("_persist_") or name.startswith("_persist_and_build_")):
            raise AttributeError(name)

        try:
            service = self._persistence_service()
        except AttributeError:
            raise AttributeError(name)

        method_name = name[1:]
        method = getattr(service, method_name, None)
        if method is None or not callable(method):
            raise AttributeError(name)

        def _delegate(*args: object, **kwargs: object) -> Any:
            return method(self, *args, **kwargs)

        return _delegate
