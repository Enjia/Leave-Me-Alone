from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import shutil
import subprocess


logger = logging.getLogger(__name__)

GENERATED_WORKSPACE_PREFIXES = (
    ".runtime",
    ".seed-",
)


@dataclass
class WorkspaceArtifacts:
    changed_files: list[str]
    patch: str
    review_patch: str
    status_lines: list[str]


@dataclass
class WorkspaceSyncReport:
    copied_files: list[str]
    deleted_files: list[str]


@dataclass(frozen=True)
class WorkspaceSnapshot:
    file_hashes: dict[str, str]
    status_lines: list[str]


class WorkspaceManager:
    def __init__(self, target_repo: Path, runtime_dir: Path) -> None:
        self.target_repo = target_repo
        self.runtime_dir = runtime_dir
        self.workspaces_dir = runtime_dir / "workspaces"
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

    def prepare_worker_workspace(self, worker: str) -> Path:
        workspace = self.workspaces_dir / worker
        if workspace.exists():
            self._refresh_worker_workspace(workspace)
            return workspace

        if self._is_git_repo(self.target_repo):
            self._create_git_worktree(workspace)
            return workspace

        shutil.copytree(
            self.target_repo,
            workspace,
            ignore=self._copytree_ignore_generated_paths,
            symlinks=True,
            ignore_dangling_symlinks=True,
        )
        return workspace

    def _refresh_worker_workspace(self, workspace: Path) -> None:
        if self._is_git_repo(self.target_repo):
            self._remove_existing_workspace(workspace)
            self._create_git_worktree(workspace)
            return
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.copytree(
            self.target_repo,
            workspace,
            ignore=self._copytree_ignore_generated_paths,
            symlinks=True,
            ignore_dangling_symlinks=True,
        )

    def _remove_existing_workspace(self, workspace: Path) -> None:
        if not workspace.exists():
            return
        if self._is_git_repo(self.target_repo):
            self._run(
                [
                    "git",
                    "-C",
                    str(self.target_repo),
                    "worktree",
                    "remove",
                    "--force",
                    str(workspace),
                ],
                check=False,
            )
        shutil.rmtree(workspace, ignore_errors=True)

    def _copytree_ignore_generated_paths(self, src: str, names: list[str]) -> set[str]:
        src_path = Path(src).resolve()
        ignored: set[str] = set()
        for name in names:
            candidate = (src_path / name).resolve()
            if self._should_ignore_copy_path(candidate):
                ignored.add(name)
        return ignored

    def _should_ignore_copy_path(self, candidate: Path) -> bool:
        if candidate == self.runtime_dir.resolve():
            return True
        return any(candidate.name.startswith(prefix) for prefix in GENERATED_WORKSPACE_PREFIXES)

    def capture_snapshot(self, workspace: Path) -> WorkspaceSnapshot:
        if not self._is_git_repo(workspace):
            return WorkspaceSnapshot(file_hashes={}, status_lines=[])

        status_lines = self._get_status_lines(workspace)
        file_hashes: dict[str, str] = {}
        for line in status_lines:
            rel_path = self._extract_status_path(line)
            if not rel_path:
                continue
            file_hashes[rel_path] = self._hash_workspace_entry(workspace, rel_path)
        return WorkspaceSnapshot(file_hashes=file_hashes, status_lines=status_lines)

    def capture_artifacts(
        self,
        workspace: Path,
        baseline_snapshot: WorkspaceSnapshot | None = None,
    ) -> WorkspaceArtifacts:
        if not self._is_git_repo(workspace):
            return WorkspaceArtifacts(
                changed_files=[],
                patch="",
                review_patch="",
                status_lines=[],
            )

        status_lines = self._get_status_lines(workspace)
        patch = self._run(
            ["git", "-C", str(workspace), "diff", "--patch", "--unified=3"],
            check=False,
        )

        if not status_lines and not patch:
            logger.warning(
                "capture_artifacts returned empty status and patch for %s — "
                "git commands may have failed silently",
                workspace,
            )

        changed_files: list[str] = []
        for line in status_lines:
            rel_path = self._extract_status_path(line)
            if rel_path:
                changed_files.append(rel_path)

        review_patch = patch
        if baseline_snapshot is not None:
            current_snapshot = self.capture_snapshot(workspace)
            delta_paths = self._compute_delta_paths(
                baseline_snapshot.file_hashes,
                current_snapshot.file_hashes,
            )
            if delta_paths:
                review_patch = self._build_patch_for_paths(workspace, delta_paths)
            else:
                review_patch = ""

        return WorkspaceArtifacts(
            changed_files=sorted(set(changed_files)),
            patch=patch,
            review_patch=review_patch,
            status_lines=status_lines,
        )

    def _create_git_worktree(self, workspace: Path) -> None:
        workspace.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "git",
                "-C",
                str(self.target_repo),
                "worktree",
                "add",
                "--detach",
                str(workspace),
                "HEAD",
            ]
        )

    def promote_owner_workspace(
        self,
        owner_workspace: Path,
        peer_workspace: Path | None = None,
    ) -> WorkspaceSyncReport:
        """Promote owner workspace changes into target repo and peer workspace."""
        report = self.sync_workspace_changes(owner_workspace, self.target_repo)
        logger.info(
            "Promoted owner workspace changes to target repo: copied=%d deleted=%d",
            len(report.copied_files),
            len(report.deleted_files),
        )

        if peer_workspace is not None and peer_workspace != owner_workspace:
            peer_report = self.sync_workspace_changes(owner_workspace, peer_workspace)
            logger.info(
                "Propagated owner changes to peer workspace %s: copied=%d deleted=%d",
                peer_workspace,
                len(peer_report.copied_files),
                len(peer_report.deleted_files),
            )
        return report

    def sync_workspace_changes(
        self,
        source_workspace: Path,
        destination_root: Path,
    ) -> WorkspaceSyncReport:
        """Copy only changed files from source workspace to destination root."""
        copy_paths, delete_paths = self._collect_workspace_change_set(source_workspace)
        copied_files: list[str] = []
        deleted_files: list[str] = []

        for rel_path in sorted(copy_paths):
            src = source_workspace / rel_path
            dst = destination_root / rel_path

            if not src.exists():
                logger.warning(
                    "Skipping copy for missing source path: %s",
                    src,
                )
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied_files.append(rel_path)

        for rel_path in sorted(delete_paths):
            dst = destination_root / rel_path
            if not dst.exists():
                continue
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
            deleted_files.append(rel_path)

        return WorkspaceSyncReport(copied_files=copied_files, deleted_files=deleted_files)

    def _collect_workspace_change_set(self, workspace: Path) -> tuple[set[str], set[str]]:
        copy_paths: set[str] = set()
        delete_paths: set[str] = set()

        if not self._is_git_repo(workspace):
            return copy_paths, delete_paths

        name_status = self._run(
            [
                "git",
                "-C",
                str(workspace),
                "diff",
                "HEAD",
                "--name-status",
                "--find-renames",
            ],
            check=False,
        )
        for raw_line in name_status.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            fields = line.split("\t")
            status = fields[0]

            if status.startswith("R") and len(fields) >= 3:
                old_rel = self._normalize_relative_path(fields[1])
                new_rel = self._normalize_relative_path(fields[2])
                if old_rel:
                    delete_paths.add(old_rel)
                if new_rel:
                    copy_paths.add(new_rel)
                continue

            rel = self._normalize_relative_path(fields[-1] if len(fields) > 1 else "")
            if not rel:
                continue

            if status.startswith("D"):
                delete_paths.add(rel)
            else:
                copy_paths.add(rel)

        untracked = self._run(
            [
                "git",
                "-C",
                str(workspace),
                "ls-files",
                "--others",
                "--exclude-standard",
            ],
            check=False,
        )
        for raw_line in untracked.splitlines():
            rel = self._normalize_relative_path(raw_line.strip())
            if rel:
                copy_paths.add(rel)

        # If a file is copied as part of this change-set, do not delete it.
        delete_paths -= copy_paths
        return copy_paths, delete_paths

    def _get_status_lines(self, workspace: Path) -> list[str]:
        status = self._run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            check=False,
        )
        return [line for line in status.splitlines() if line.strip()]

    def _hash_workspace_entry(self, workspace: Path, rel_path: str) -> str:
        path = workspace / rel_path
        if not path.exists():
            return "__deleted__"
        if path.is_dir():
            return "__dir__"
        try:
            payload = path.read_bytes()
        except OSError:
            return "__unreadable__"
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _compute_delta_paths(
        baseline_hashes: dict[str, str],
        current_hashes: dict[str, str],
    ) -> list[str]:
        delta = [
            path
            for path in sorted(set(baseline_hashes) | set(current_hashes))
            if baseline_hashes.get(path) != current_hashes.get(path)
        ]
        return delta

    def _build_patch_for_paths(self, workspace: Path, rel_paths: list[str]) -> str:
        tracked_paths: list[str] = []
        patches: list[str] = []

        for rel_path in rel_paths:
            abs_path = workspace / rel_path
            tracked = self._run(
                ["git", "-C", str(workspace), "ls-files", "--error-unmatch", rel_path],
                check=False,
            )
            if tracked.strip():
                tracked_paths.append(rel_path)
                continue
            if abs_path.exists() and abs_path.is_file():
                patches.append(self._build_untracked_patch(abs_path, rel_path))

        if tracked_paths:
            tracked_patch = self._run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "diff",
                    "HEAD",
                    "--patch",
                    "--unified=3",
                    "--",
                    *tracked_paths,
                ],
                check=False,
            )
            if tracked_patch:
                patches.insert(0, tracked_patch)

        return "\n".join(chunk for chunk in patches if chunk.strip()).strip()

    @staticmethod
    def _build_untracked_patch(abs_path: Path, rel_path: str) -> str:
        try:
            content = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"diff --git a/{rel_path} b/{rel_path}\nBinary files /dev/null and b/{rel_path} differ\n"
        lines = content.splitlines()
        header = [
            f"diff --git a/{rel_path} b/{rel_path}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ b/{rel_path}",
            f"@@ -0,0 +1,{len(lines)} @@",
        ]
        body = [f"+{line}" for line in lines]
        if content.endswith("\n"):
            return "\n".join(header + body) + "\n"
        return "\n".join(header + body + ["\\ No newline at end of file"]) + "\n"

    @staticmethod
    def _extract_status_path(line: str) -> str | None:
        if not line:
            return None
        payload = line[3:].strip() if len(line) > 3 else ""
        if " -> " in payload:
            payload = payload.split(" -> ", maxsplit=1)[1].strip()
        return WorkspaceManager._normalize_relative_path(payload)

    @staticmethod
    def _normalize_relative_path(path: str) -> str | None:
        if not path:
            return None
        p = Path(path)
        if p.is_absolute() or ".." in p.parts:
            return None
        return p.as_posix()

    @staticmethod
    def _is_git_repo(path: Path) -> bool:
        probe = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            return False
        try:
            git_root = Path(probe.stdout.strip()).resolve()
        except OSError:
            return False
        return git_root == path.resolve()

    @staticmethod
    def _run(cmd: list[str], check: bool = True) -> str:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}"
            )
        if not check and proc.returncode != 0:
            logger.warning(
                "Command returned non-zero exit code %d (check=False): %s\nstderr: %s",
                proc.returncode,
                " ".join(cmd),
                proc.stderr.strip()[:500],
            )
        return proc.stdout
