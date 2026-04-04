"""Minimal self-contained Flow runtime.

Drop-in replacement for ``crewai.flow.flow.Flow`` and ``crewai.flow.flow.start``
that removes the external dependency while preserving the exact public API
surface used by this project.

Design goals
------------
- Zero external dependencies beyond ``pydantic`` (already required).
- Support ``Flow[S]`` generic with automatic ``self.state`` initialisation.
- ``@start()`` marks the async entry-point method.
- ``kickoff()`` runs the entry-point synchronously via ``asyncio.run()``,
  or in a new daemon thread when a loop is already running.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from typing import Any, Generic, TypeVar, get_args

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_FLOW_START_ATTR = "__flow_start__"

StateT = TypeVar("StateT", bound=BaseModel)


def start() -> Any:
    """Decorator that marks a method as the flow entry-point.

    Usage::

        class MyFlow(FlowLite[MyState]):
            @start()
            async def run(self) -> Result:
                ...

    The decorated method is invoked by :meth:`FlowLite.kickoff`.
    Both sync and async methods are supported; async is preferred.
    """

    def decorator(func: Any) -> Any:
        setattr(func, _FLOW_START_ATTR, True)
        return func

    return decorator


def _resolve_state_class(cls: type) -> type[BaseModel] | None:
    """Walk the MRO to find the concrete ``StateT`` bound to ``FlowLite[S]``."""
    for klass in cls.__mro__:
        for base in getattr(klass, "__orig_bases__", ()):
            origin = getattr(base, "__origin__", None)
            if origin is FlowLite:
                args = get_args(base)
                if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
                    return args[0]
    return None


class FlowLite(Generic[StateT]):
    """Lightweight orchestration base class.

    Provides:

    - ``self.state: StateT`` — auto-instantiated from the generic parameter.
    - ``kickoff() -> Any`` — synchronous driver that locates the ``@start``
      method and runs it, returning whatever the method returns.
    """

    state: StateT  # type: ignore[assignment]

    def __init__(self) -> None:
        state_cls = _resolve_state_class(type(self))
        if state_cls is not None:
            self.state = state_cls()  # type: ignore[assignment]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

    def kickoff(self) -> Any:
        """Run the ``@start`` entry-point and return its result.

        If the entry-point is a coroutine function the method creates a new
        event loop via ``asyncio.run()``.  If a loop is already running
        (e.g. inside Jupyter or an async framework) the coroutine is
        executed in a **new daemon thread** with its own event loop so that
        the caller is never blocked by a "loop already running" error.

        .. note::

           The new-thread strategy means the ``@start`` method runs
           outside the caller's ``contextvars`` and event-loop scope.
           This is acceptable for the current project but should be
           revisited if future entry-points depend on thread-local or
           loop-bound resources.
        """
        entry_method = self._find_start_method()
        if entry_method is None:
            raise RuntimeError(
                f"{type(self).__name__} has no method decorated with @start(). "
                "Decorate exactly one method with @start() to define the entry-point."
            )

        bound_method = entry_method.__get__(self, type(self))

        if inspect.iscoroutinefunction(entry_method):
            return self._run_async(bound_method)
        return bound_method()

    def _find_start_method(self) -> Any | None:
        """Locate the single ``@start``-decorated method in the MRO."""
        for klass in type(self).__mro__:
            for attr_name in vars(klass):
                attr = getattr(klass, attr_name, None)
                if callable(attr) and getattr(attr, _FLOW_START_ATTR, False):
                    return attr
        return None

    @staticmethod
    def _run_async(coro_func: Any) -> Any:
        """Execute an async callable, handling both fresh and running loops.

        When no event loop is running the coroutine is executed directly via
        ``asyncio.run()``.  When a loop *is* already running (e.g. Jupyter,
        an async web framework, or nested ``kickoff()`` calls) we spin up a
        **new daemon thread** with its own event loop so that the caller is
        never blocked by ``run_until_complete`` on a running loop.
        """
        try:
            asyncio.get_running_loop()
            has_running_loop = True
        except RuntimeError:
            has_running_loop = False

        if not has_running_loop:
            return asyncio.run(coro_func())

        # A loop is already running — execute in a fresh thread to avoid
        # "This event loop is already running" RuntimeError.
        logger.debug(
            "asyncio loop already running; delegating to a new thread for FlowLite.kickoff()"
        )
        result_container: list[Any] = []
        error_container: list[BaseException] = []

        def _thread_target() -> None:
            try:
                result_container.append(asyncio.run(coro_func()))
            except BaseException as exc:
                error_container.append(exc)

        worker_thread = threading.Thread(target=_thread_target, daemon=True)
        worker_thread.start()
        worker_thread.join()

        if error_container:
            raise error_container[0]
        return result_container[0]
