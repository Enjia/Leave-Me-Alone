from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

from .agent_env_config import load_agent_runtime_env
from .codex_exec_agent import CodexExecAgent, CodexExecAgentConfig


logger = logging.getLogger(__name__)


def _is_ascii_path(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def sanitize_codex_add_dirs(paths: list[Path], alias_root: Path) -> list[str]:
    sanitized: list[str] = []
    alias_root = alias_root.expanduser().resolve(strict=False)
    alias_root.mkdir(parents=True, exist_ok=True)

    for path in paths:
        normalized = path.expanduser().resolve(strict=False)
        if _is_ascii_path(normalized):
            sanitized.append(str(normalized))
            continue

        digest = hashlib.sha256(str(normalized).encode("utf-8")).hexdigest()[:16]
        alias_path = alias_root / f"add_dir_{digest}"
        try:
            if alias_path.exists() or alias_path.is_symlink():
                if alias_path.is_symlink() and alias_path.resolve(strict=False) == normalized:
                    sanitized.append(str(alias_path))
                    continue
                if alias_path.is_dir() and not alias_path.is_symlink():
                    shutil.rmtree(alias_path)
                else:
                    alias_path.unlink()
            alias_path.symlink_to(normalized, target_is_directory=True)
            logger.info(
                "Use ASCII alias for non-ASCII codex add-dir path: %s -> %s",
                normalized,
                alias_path,
            )
            sanitized.append(str(alias_path))
        except Exception:
            logger.exception(
                "Failed to create ASCII alias for non-ASCII codex add-dir path: %s",
                normalized,
            )
            logger.warning(
                "Skip non-ASCII codex add-dir path due to alias failure: %s",
                normalized,
            )
    return sanitized


def create_codex_agent(
    *,
    role: str,
    goal: str,
    backstory: str,
    model: str,
    workspace: Path,
    sandbox_mode: str,
    add_dirs: list[Path],
    add_dir_alias_root: Path,
) -> CodexExecAgent:
    runtime_env = load_agent_runtime_env()
    return CodexExecAgent(
        config=CodexExecAgentConfig(
            role=role,
            model=model,
            workspace=workspace,
            sandbox_mode=sandbox_mode,
            add_dirs=sanitize_codex_add_dirs(add_dirs, add_dir_alias_root),
            goal=goal,
            backstory=backstory,
            timeout=runtime_env.timeout_sec,
            idle_timeout=runtime_env.idle_timeout_sec,
        )
    )
