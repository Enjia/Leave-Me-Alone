"""Check execution plugin system.

Public API
----------
- ``CheckPlugin``         – Protocol for check plugins
- ``CheckPluginRegistry`` – Registry for discovering and invoking plugins
- ``CheckContext``        – Immutable context passed to every plugin
- ``CheckResult``         – Standardized result returned by every plugin
- ``resolve_plugins_for_tier`` – Resolve plugin names for a gate tier
- ``extract_commands_from_stage`` – Extract commands from a stage object
- ``check_results_to_auto_check_result`` – Adapt plugin results to AutoCheckResult
"""

from .adapt import check_results_to_auto_check_result
from .plugin import CheckContext, CheckPlugin, CheckResult
from .profiles import extract_commands_from_stage, resolve_plugins_for_tier
from .registry import CheckPluginRegistry

__all__ = [
    "CheckContext",
    "CheckPlugin",
    "CheckPluginRegistry",
    "CheckResult",
    "check_results_to_auto_check_result",
    "extract_commands_from_stage",
    "resolve_plugins_for_tier",
]
