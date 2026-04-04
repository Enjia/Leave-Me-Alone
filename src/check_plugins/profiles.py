"""Stage-aware check-plugin profiles.

A *check profile* maps a ``GateTier`` to the list of plugin names that
should run at that tier.  Stages can embed a ``CheckProfile`` in their
``StageSpec`` to override the default mapping; otherwise the built-in
defaults below are used.

Default tier → plugin mapping
-----------------------------
- **fast_round** : ``lint``, ``test``
- **pre_promotion** : ``lint``, ``test``, ``perf``, ``harness``
- **full_regression**: ``lint``, ``test``, ``perf``, ``harness``, ``remote_preflight``
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.models import GateTier

# ------------------------------------------------------------------
# Default profiles
# ------------------------------------------------------------------

DEFAULT_TIER_PLUGINS: dict[str, list[str]] = {
    "fast_round": ["lint", "test"],
    "pre_promotion": ["lint", "test", "perf", "harness"],
    "full_regression": ["lint", "test", "perf", "harness", "remote_preflight"],
}

ALL_BUILTIN_PLUGIN_NAMES: list[str] = [
    "lint",
    "test",
    "perf",
    "harness",
    "remote_preflight",
]


def resolve_plugins_for_tier(
    gate_tier: GateTier,
    *,
    tier_plugins: dict[str, list[str]] | None = None,
) -> list[str]:
    """Return the ordered list of plugin names for *gate_tier*.

    Parameters
    ----------
    gate_tier:
        The gate tier to resolve (``"fast_round"``, ``"pre_promotion"``,
        or ``"full_regression"``).
    tier_plugins:
        Optional custom mapping.  Falls back to ``DEFAULT_TIER_PLUGINS``
        when ``None`` or when *gate_tier* is not present in the mapping.
    """
    mapping = tier_plugins or DEFAULT_TIER_PLUGINS
    return list(mapping.get(gate_tier, DEFAULT_TIER_PLUGINS.get(gate_tier, [])))


def extract_commands_from_stage(
    stage: object,
    plugin_names: list[str],
    *,
    gate_tier: GateTier | None = None,
) -> dict[str, list[str]]:
    """Build a ``commands_by_plugin`` dict from a stage object's fields.

    This bridges the existing ``StageSpec`` command fields (``lint_commands``,
    ``test_commands``, ``perf_checks``, ``gate_commands_remote_tiered``) to
    the plugin-based API.

    Parameters
    ----------
    gate_tier:
        When provided, harness commands are filtered from
        ``gate_commands_remote_tiered`` to include only commands matching
        the requested tier.  When ``None``, falls back to the legacy
        ``gate_commands_remote`` (full set) for backward compatibility.
    """
    commands: dict[str, list[str]] = {}

    if "lint" in plugin_names:
        lint_cmds = getattr(stage, "lint_commands", []) or []
        if lint_cmds:
            commands["lint"] = list(lint_cmds)

    if "test" in plugin_names:
        test_cmds = getattr(stage, "test_commands", []) or []
        if test_cmds:
            commands["test"] = list(test_cmds)

    if "perf" in plugin_names:
        perf_cmds = getattr(stage, "perf_checks", []) or []
        if perf_cmds:
            commands["perf"] = list(perf_cmds)

    if "harness" in plugin_names:
        gate_cmds = _extract_harness_commands(stage, gate_tier)
        if gate_cmds:
            commands["harness"] = gate_cmds

    # remote_preflight does not use explicit commands — it reads from
    # context.extra["stage"] directly.

    return commands


def _extract_harness_commands(
    stage: object,
    gate_tier: GateTier | None,
) -> list[str]:
    """Extract harness commands, filtered by tier when possible."""
    tiered_items = getattr(stage, "gate_commands_remote_tiered", []) or []

    if gate_tier is not None and tiered_items:
        filtered = [
            item.command.strip()
            for item in tiered_items
            if getattr(item, "tier", "") == gate_tier and item.command.strip()
        ]
        return filtered

    # Fallback: use the legacy aggregated field.
    legacy_cmds = getattr(stage, "gate_commands_remote", []) or []
    return [cmd for cmd in legacy_cmds if cmd.strip()]
