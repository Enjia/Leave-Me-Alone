"""CheckPluginRegistry — register, discover, and invoke check plugins."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from errors.recovery import compute_backoff_delay
from errors.taxonomy import classify_transient_error
from .plugin import CheckContext, CheckPlugin, CheckResult

logger = logging.getLogger(__name__)


class CheckPluginRegistry:
    """Central registry for check plugins.

    Plugins are registered by name and invoked in the order specified by
    the caller.  The registry provides middleware hooks for timeout
    enforcement, deduplication, and result aggregation.

    Usage::

        registry = CheckPluginRegistry()
        registry.register(LintCheckPlugin())
        registry.register(TestCheckPlugin())

        results = await registry.run_plugins(
            plugin_names=["lint", "test"],
            context=context,
            commands_by_plugin={"lint": ["ruff check ."], "test": ["pytest"]},
        )
    """

    def __init__(self) -> None:
        self._plugins: dict[str, CheckPlugin] = {}

    def register(self, plugin: CheckPlugin) -> None:
        """Register a plugin.  Overwrites any existing plugin with the same name."""
        self._plugins[plugin.name] = plugin

    def has_plugin(self, name: str) -> bool:
        return name in self._plugins

    def get_plugin(self, name: str) -> CheckPlugin | None:
        return self._plugins.get(name)

    @property
    def registered_names(self) -> list[str]:
        return list(self._plugins.keys())

    async def run_plugins(
        self,
        plugin_names: list[str],
        context: CheckContext,
        commands_by_plugin: dict[str, list[str]] | None = None,
        *,
        timeout_sec: int | None = None,
        dedup: bool = True,
        max_attempts: int = 1,
        backoff_base_sec: float = 1.0,
        backoff_max_sec: float = 8.0,
    ) -> list[CheckResult]:
        """Run the specified plugins in order and return their results.

        Parameters
        ----------
        plugin_names:
            Ordered list of plugin names to execute.
        context:
            Immutable execution context.
        commands_by_plugin:
            Optional mapping of plugin name → command list.  If a plugin
            is not in this dict, an empty list is passed.
        timeout_sec:
            Optional per-plugin timeout.  If a plugin exceeds this, it is
            cancelled and a failed result is returned.
        dedup:
            If ``True``, skip duplicate plugin names (only the first
            occurrence is executed).
        """
        commands_by_plugin = commands_by_plugin or {}
        results: list[CheckResult] = []
        seen: set[str] = set()

        for plugin_name in plugin_names:
            # --- Dedup ---
            if dedup and plugin_name in seen:
                logger.debug("Skipping duplicate plugin: %s", plugin_name)
                continue
            seen.add(plugin_name)

            # --- Lookup ---
            plugin = self._plugins.get(plugin_name)
            if plugin is None:
                logger.warning("Plugin not found: %s (skipping)", plugin_name)
                results.append(CheckResult(
                    plugin_name=plugin_name,
                    passed=False,
                    skipped=True,
                    skip_reason=f"Plugin '{plugin_name}' not registered.",
                ))
                continue

            # --- Execute with optional timeout ---
            commands = commands_by_plugin.get(plugin_name, [])
            start_time = time.monotonic()
            attempt = 1
            attempt_limit = max(1, int(max_attempts))
            while True:
                try:
                    if timeout_sec is not None and timeout_sec > 0:
                        result = await asyncio.wait_for(
                            plugin.run(context, commands),
                            timeout=timeout_sec,
                        )
                    else:
                        result = await plugin.run(context, commands)
                    result.duration_sec = time.monotonic() - start_time
                    if (
                        not result.passed
                        and attempt < attempt_limit
                        and self._is_retryable_result(result)
                    ):
                        delay = compute_backoff_delay(
                            attempt,
                            base_delay_sec=backoff_base_sec,
                            max_delay_sec=backoff_max_sec,
                        )
                        logger.info(
                            "Retrying plugin '%s' attempt %d/%d after %.1fs (error_category=%s)",
                            plugin_name, attempt + 1, attempt_limit, delay, result.error_category,
                        )
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue
                    results.append(result)
                    break
                except asyncio.TimeoutError:
                    elapsed = time.monotonic() - start_time
                    logger.warning(
                        "Plugin '%s' timed out after %.1fs (limit=%ds)",
                        plugin_name, elapsed, timeout_sec,
                    )
                    timeout_result = CheckResult(
                        plugin_name=plugin_name,
                        passed=False,
                        error_category="timeout",
                        evidence=[f"Plugin timed out after {elapsed:.1f}s"],
                        duration_sec=elapsed,
                    )
                    if attempt < attempt_limit:
                        delay = compute_backoff_delay(
                            attempt,
                            base_delay_sec=backoff_base_sec,
                            max_delay_sec=backoff_max_sec,
                        )
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue
                    results.append(timeout_result)
                    break
                except Exception as exc:
                    elapsed = time.monotonic() - start_time
                    logger.exception("Plugin '%s' raised an unexpected error", plugin_name)
                    message = str(exc)[:300]
                    unknown_result = CheckResult(
                        plugin_name=plugin_name,
                        passed=False,
                        error_category="unknown",
                        evidence=[f"Plugin raised an unexpected exception: {message}"],
                        duration_sec=elapsed,
                    )
                    if attempt < attempt_limit and classify_transient_error(message) is not None:
                        delay = compute_backoff_delay(
                            attempt,
                            base_delay_sec=backoff_base_sec,
                            max_delay_sec=backoff_max_sec,
                        )
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue
                    results.append(unknown_result)
                    break

        return results

    async def run_all(
        self,
        context: CheckContext,
        commands_by_plugin: dict[str, list[str]] | None = None,
        *,
        timeout_sec: int | None = None,
        max_attempts: int = 1,
    ) -> list[CheckResult]:
        """Convenience: run all registered plugins in registration order."""
        return await self.run_plugins(
            plugin_names=self.registered_names,
            context=context,
            commands_by_plugin=commands_by_plugin,
            timeout_sec=timeout_sec,
            max_attempts=max_attempts,
        )

    @staticmethod
    def _is_retryable_result(result: CheckResult) -> bool:
        if result.error_category in {"timeout", "transient"}:
            return True
        text = "\n".join(result.evidence or [])
        return classify_transient_error(text) is not None
