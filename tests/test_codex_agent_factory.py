from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from agents.codex_agent_factory import sanitize_codex_add_dirs


def _alias_path(alias_root: Path, target: Path) -> Path:
    digest = hashlib.sha256(str(target.resolve(strict=False)).encode("utf-8")).hexdigest()[:16]
    return alias_root.resolve(strict=False) / f"add_dir_{digest}"


def test_sanitize_codex_add_dirs_keeps_ascii_paths_direct() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        alias_root = root / "aliases"
        ascii_dir = root / "ascii_dir"
        ascii_dir.mkdir()

        result = sanitize_codex_add_dirs([ascii_dir], alias_root)

        assert result == [str(ascii_dir.resolve(strict=False))]
        assert not alias_root.exists() or list(alias_root.iterdir()) == []


def test_sanitize_codex_add_dirs_reuses_existing_matching_alias() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        alias_root = root / "aliases"
        alias_root.mkdir()
        non_ascii_dir = root / "目录"
        non_ascii_dir.mkdir()
        alias_path = _alias_path(alias_root, non_ascii_dir)
        alias_path.symlink_to(non_ascii_dir, target_is_directory=True)

        result = sanitize_codex_add_dirs([non_ascii_dir], alias_root)

        assert result == [str(alias_path)]
        assert alias_path.is_symlink()
        assert alias_path.resolve(strict=False) == non_ascii_dir.resolve(strict=False)


def test_sanitize_codex_add_dirs_replaces_conflicting_alias_directory() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        alias_root = root / "aliases"
        alias_root.mkdir()
        non_ascii_dir = root / "目录"
        non_ascii_dir.mkdir()
        alias_path = _alias_path(alias_root, non_ascii_dir)
        alias_path.mkdir()
        (alias_path / "stale.txt").write_text("stale", encoding="utf-8")

        result = sanitize_codex_add_dirs([non_ascii_dir], alias_root)

        assert result == [str(alias_path)]
        assert alias_path.is_symlink()
        assert alias_path.resolve(strict=False) == non_ascii_dir.resolve(strict=False)


def test_sanitize_codex_add_dirs_replaces_conflicting_alias_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        alias_root = root / "aliases"
        alias_root.mkdir()
        non_ascii_dir = root / "目录"
        non_ascii_dir.mkdir()
        alias_path = _alias_path(alias_root, non_ascii_dir)
        alias_path.write_text("conflict", encoding="utf-8")

        result = sanitize_codex_add_dirs([non_ascii_dir], alias_root)

        assert result == [str(alias_path)]
        assert alias_path.is_symlink()
        assert alias_path.resolve(strict=False) == non_ascii_dir.resolve(strict=False)
