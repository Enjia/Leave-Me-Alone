from __future__ import annotations

import json
from pathlib import Path

from app.runtime_config import load_stage_artifacts, migrate_stage_artifact_payload


def _write_artifact(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_migrate_stage_artifact_payload_defaults_missing_schema_version_to_v1() -> None:
    migrated = migrate_stage_artifact_payload(
        {
            "stage_name": "s1",
            "artifact_name": "a1",
            "data": {},
        }
    )
    assert migrated["schema_version"] == 1


def test_migrate_stage_artifact_payload_rejects_future_schema_version() -> None:
    try:
        migrate_stage_artifact_payload({"schema_version": 999})
    except ValueError as exc:
        assert "Unsupported stage artifact schema_version=999" in str(exc)
    else:
        raise AssertionError("expected ValueError for future schema version")


def test_load_stage_artifacts_skips_future_schema_file(tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    _write_artifact(
        artifacts_dir / "future.json",
        {
            "schema_version": 999,
            "stage_name": "s1",
            "artifact_name": "future",
            "data": {},
        },
    )
    _write_artifact(
        artifacts_dir / "ok.json",
        {
            "stage_name": "s1",
            "artifact_name": "ok",
            "data": {"k": "v"},
        },
    )

    loaded = load_stage_artifacts(artifacts_dir)
    assert "s1" in loaded
    assert len(loaded["s1"]) == 1
    assert loaded["s1"][0].artifact_name == "ok"
    assert loaded["s1"][0].schema_version == 1
