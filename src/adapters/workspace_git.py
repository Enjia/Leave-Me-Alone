from __future__ import annotations


class WorkspaceManagerAdapter:
    def __init__(self, workspace_manager: object) -> None:
        self._workspace_manager = workspace_manager

    def __getattr__(self, name: str) -> object:
        return getattr(self._workspace_manager, name)
