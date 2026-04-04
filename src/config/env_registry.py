"""Centralized environment variable registry.

Every environment variable used by the project is declared here as an
``EnvVar`` descriptor.  The ``read_env*`` family of functions provides
type-safe, validated access with consistent defaults and error handling.

Benefits over scattered ``os.getenv`` calls:
- Single source of truth for all env vars (name, type, default, doc).
- Eliminates duplicated ``_read_positive_int_env`` helpers across modules.
- Makes it trivial to list / document / validate all env vars at startup.
- Supports an override dict for testing without touching ``os.environ``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

EnvVarType = Literal["str", "int", "bool", "positive_int", "non_negative_int"]

# ------------------------------------------------------------------
# Descriptor
# ------------------------------------------------------------------

@dataclass(frozen=True)
class EnvVar:
    """Declarative descriptor for a single environment variable."""

    name: str
    var_type: EnvVarType = "str"
    default: str = ""
    description: str = ""
    sensitive: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


# ------------------------------------------------------------------
# Global registry
# ------------------------------------------------------------------

_REGISTRY: dict[str, EnvVar] = {}


def register(var: EnvVar) -> EnvVar:
    """Register an ``EnvVar`` and return it (for inline use)."""
    _REGISTRY[var.name] = var
    return var


def registered_env_vars() -> list[EnvVar]:
    """Return all registered env vars in registration order."""
    return list(_REGISTRY.values())


def get_descriptor(name: str) -> EnvVar | None:
    """Look up a registered descriptor by env var name."""
    return _REGISTRY.get(name)


# ------------------------------------------------------------------
# Type-safe readers
# ------------------------------------------------------------------

def read_env(name: str, *, overrides: dict[str, str] | None = None) -> str:
    """Read an env var using its registered type, returning a typed string.

    Falls back to ``os.getenv`` if *name* is not in the registry.
    """
    if overrides and name in overrides:
        return overrides[name]
    descriptor = _REGISTRY.get(name)
    default = descriptor.default if descriptor else ""
    return os.getenv(name, default).strip()


def read_env_str(
    name: str,
    default: str = "",
    *,
    overrides: dict[str, str] | None = None,
) -> str:
    """Read an env var as a stripped string."""
    if overrides and name in overrides:
        return overrides[name].strip()
    descriptor = _REGISTRY.get(name)
    fallback = descriptor.default if descriptor else default
    return os.getenv(name, fallback).strip()


def read_env_int(
    name: str,
    default: int = 0,
    *,
    minimum: int | None = None,
    overrides: dict[str, str] | None = None,
) -> int:
    """Read an env var as an integer with optional minimum enforcement.

    If the raw value is not a valid integer or is below *minimum*,
    *default* is returned.
    """
    if overrides and name in overrides:
        raw = overrides[name].strip()
    else:
        descriptor = _REGISTRY.get(name)
        fallback = descriptor.default if descriptor else str(default)
        raw = os.getenv(name, fallback).strip()
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def read_env_bool(
    name: str,
    default: bool = False,
    *,
    overrides: dict[str, str] | None = None,
) -> bool:
    """Read an env var as a boolean.

    Truthy values: ``"1"``, ``"true"``, ``"yes"`` (case-insensitive).
    """
    if overrides and name in overrides:
        raw = overrides[name].strip().lower()
    else:
        descriptor = _REGISTRY.get(name)
        fallback = descriptor.default if descriptor else str(int(default))
        raw = os.getenv(name, fallback).strip().lower()
    return raw in ("1", "true", "yes")


def read_positive_int(
    name: str,
    default: int = 1,
    *,
    overrides: dict[str, str] | None = None,
) -> int:
    """Convenience: read an env var as a positive (>0) integer."""
    return read_env_int(name, default, minimum=1, overrides=overrides)


def read_non_negative_int(
    name: str,
    default: int = 0,
    *,
    overrides: dict[str, str] | None = None,
) -> int:
    """Convenience: read an env var as a non-negative (>=0) integer."""
    return read_env_int(name, default, minimum=0, overrides=overrides)


# ------------------------------------------------------------------
# Standard env var declarations
# ------------------------------------------------------------------

# --- Agent timeouts ---
AGENT_TIMEOUT_SEC = register(EnvVar(
    name="MULTI_CODEX_AGENT_TIMEOUT_SEC",
    var_type="positive_int",
    default="10800",
    description="Per-agent invocation timeout in seconds.",
    tags=("timeout", "agent"),
))

AGENT_IDLE_TIMEOUT_SEC = register(EnvVar(
    name="MULTI_CODEX_AGENT_IDLE_TIMEOUT_SEC",
    var_type="non_negative_int",
    default="600",
    description="Agent idle timeout in seconds (0 = disabled).",
    tags=("timeout", "agent"),
))

STAGE_TIMEOUT_SEC = register(EnvVar(
    name="MULTI_CODEX_STAGE_TIMEOUT_SEC",
    var_type="positive_int",
    default="10800",
    description="Per-stage timeout in seconds.",
    tags=("timeout", "stage"),
))

# --- Remote SSH ---
REMOTE_SSH_USER = register(EnvVar(
    name="MULTI_CODEX_REMOTE_SSH_USER",
    var_type="str",
    default="root",
    description="Default SSH user for remote execution.",
    tags=("remote", "ssh"),
))

REMOTE_SSH_PASSWORD = register(EnvVar(
    name="MULTI_CODEX_REMOTE_SSH_PASSWORD",
    var_type="str",
    default="",
    description="Default SSH password for remote execution.",
    sensitive=True,
    tags=("remote", "ssh"),
))

REMOTE_CANONICAL = register(EnvVar(
    name="MULTI_CODEX_REMOTE_CANONICAL",
    var_type="bool",
    default="0",
    description="When '1', treat remote as canonical source for sync.",
    tags=("remote",),
))

CONTAINER_PREFIX = register(EnvVar(
    name="MULTI_CODEX_CONTAINER_PREFIX",
    var_type="str",
    default="sglang-jw",
    description="Docker container name prefix for remote execution.",
    tags=("remote", "container"),
))

# --- Build / Make ---
MAKEFLAGS = register(EnvVar(
    name="MULTI_CODEX_MAKEFLAGS",
    var_type="str",
    default="-j4",
    description="MAKEFLAGS passed to build commands.",
    tags=("build",),
))

# --- Worker orchestration ---
SERIALIZE_WORKER_REMOTE_CHECKS = register(EnvVar(
    name="MULTI_CODEX_SERIALIZE_WORKER_REMOTE_CHECKS",
    var_type="bool",
    default="0",
    description="When '1', serialize remote checks across workers.",
    tags=("orchestration", "remote"),
))

# --- Monitor ---
MONITOR_PORT = register(EnvVar(
    name="MULTI_CODEX_MONITOR_PORT",
    var_type="positive_int",
    default="0",
    description="HTTP port for the monitor dashboard (0 = disabled).",
    tags=("monitor",),
))
