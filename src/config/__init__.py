"""Centralized configuration management.

Public API
----------
- ``EnvVar``              – Declarative environment variable descriptor
- ``read_env``            – Read a registered env var by name
- ``read_env_int``        – Read an env var as a positive integer
- ``read_env_str``        – Read an env var as a stripped string
- ``read_env_bool``       – Read an env var as a boolean flag
- ``registered_env_vars`` – List all registered env var descriptors
"""

from .env_registry import (
    EnvVar,
    read_env,
    read_env_bool,
    read_env_int,
    read_env_str,
    registered_env_vars,
)
from .layered_policy import (
    CURRENT_SCHEMA_VERSION,
    LayeredPolicyConfig,
    PolicyValues,
    StagePolicySpec,
    load_layered_policy,
    migrate_layered_policy_payload,
    resolve_stage_policy,
)

__all__ = [
    "EnvVar",
    "read_env",
    "read_env_bool",
    "read_env_int",
    "read_env_str",
    "registered_env_vars",
    "CURRENT_SCHEMA_VERSION",
    "LayeredPolicyConfig",
    "PolicyValues",
    "StagePolicySpec",
    "load_layered_policy",
    "migrate_layered_policy_payload",
    "resolve_stage_policy",
]
