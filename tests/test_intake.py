from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.intake import (
    _build_uploaded_context_section,
    confirm_draft_to_stages_file,
    create_session,
    generate_stage_draft,
    load_session,
    register_uploaded_file,
    update_session,
)


def test_intake_generate_and_confirm_uses_heuristic_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target_repo = tmp_path / "repo"
    (target_repo / "src" / "api").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("MULTI_CODEX_INTAKE_ENABLE_MODEL", "0")

    session = create_session(
        runtime_dir,
        goal="Build a robust API auth flow",
        target_repo=str(target_repo),
    )
    session_id = str(session["session_id"])

    rel_path = register_uploaded_file(
        runtime_dir,
        session_id=session_id,
        original_name="src/api/routes.py",
        payload=b"# route placeholders\n",
    )
    assert rel_path == "src/api/routes.py"

    draft = generate_stage_draft(runtime_dir, session_id=session_id)
    assert draft["generator"] == "heuristic"
    assert draft["model_error"] == "model_generation_disabled_by_env"
    assert len(draft["stages"]) >= 2

    persisted = load_session(runtime_dir, session_id)
    assert persisted["status"] == "draft_ready"

    stages_file = confirm_draft_to_stages_file(runtime_dir, session_id=session_id)
    assert stages_file.exists()

    stage_specs = json.loads(stages_file.read_text(encoding="utf-8"))
    assert isinstance(stage_specs, list) and stage_specs
    first = stage_specs[0]
    assert first["objective"]
    assert first["acceptance_criteria"]
    assert first["manual_checklist"]


def test_intake_feedback_is_persisted_in_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target_repo = tmp_path / "repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("MULTI_CODEX_INTAKE_ENABLE_MODEL", "0")

    session = create_session(
        runtime_dir,
        goal="Improve request validation and error semantics",
        target_repo=str(target_repo),
    )
    session_id = str(session["session_id"])

    draft = generate_stage_draft(
        runtime_dir,
        session_id=session_id,
        feedback="Split into narrower stages and emphasize compatibility.",
    )
    assert draft["feedback"] == "Split into narrower stages and emphasize compatibility."

    persisted = load_session(runtime_dir, session_id)
    assert persisted["feedback_history"]
    assert persisted["feedback_history"][-1] == "Split into narrower stages and emphasize compatibility."


def test_update_session_allows_editing_remote_validation_within_same_session(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target_repo = tmp_path / "repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    session = create_session(
        runtime_dir,
        goal="Initial goal",
        target_repo=str(target_repo),
        remote_validation={
            "enabled": True,
            "servers": [
                {
                    "label": "server_1",
                    "host": "10.0.0.1",
                    "user": "root",
                    "password": "old-pass",
                    "workdir": "/workspace/project",
                }
            ],
        },
    )
    session_id = str(session["session_id"])

    updated = update_session(
        runtime_dir,
        session_id=session_id,
        goal="Updated goal",
        remote_validation={
            "enabled": True,
            "servers": [
                {
                    "label": "server_1",
                    "host": "10.0.0.1",
                    "user": "root",
                    "password": "new-pass",
                    "workdir": "/workspace/project-v2",
                },
                {
                    "label": "server_2",
                    "host": "10.0.0.2",
                    "user": "root",
                    "password": "second-pass",
                    "workdir": "/workspace/project-v2",
                },
            ],
        },
    )

    assert updated["goal"] == "Updated goal"
    assert updated["remote_validation"]["enabled"] is True
    assert len(updated["remote_validation"]["servers"]) == 2
    assert updated["remote_validation"]["servers"][0]["password"] == "new-pass"
    assert updated["remote_validation"]["servers"][0]["workdir"] == "/workspace/project-v2"

    persisted = load_session(runtime_dir, session_id)
    assert persisted["goal"] == "Updated goal"
    assert persisted["remote_validation"]["servers"][1]["host"] == "10.0.0.2"


def test_update_session_supports_multiple_target_repos(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_c = tmp_path / "repo-c"
    repo_a.mkdir(parents=True, exist_ok=True)
    repo_b.mkdir(parents=True, exist_ok=True)
    repo_c.mkdir(parents=True, exist_ok=True)

    session = create_session(
        runtime_dir,
        goal="migrate code across repos",
        target_repo=str(repo_a),
        target_repos=[str(repo_a), str(repo_b)],
    )
    session_id = str(session["session_id"])

    assert session["target_repo"] == str(repo_a)
    assert session["target_repos"] == [str(repo_a), str(repo_b)]

    updated = update_session(
        runtime_dir,
        session_id=session_id,
        target_repos=[str(repo_b), str(repo_c)],
    )
    assert updated["target_repo"] == str(repo_b)
    assert updated["target_repos"] == [str(repo_b), str(repo_c)]

    persisted = load_session(runtime_dir, session_id)
    assert persisted["target_repo"] == str(repo_b)
    assert persisted["target_repos"] == [str(repo_b), str(repo_c)]


def test_create_session_rejects_incomplete_remote_validation_when_enabled(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target_repo = tmp_path / "repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="server_1 is incomplete"):
        create_session(
            runtime_dir,
            goal="Need remote verification",
            target_repo=str(target_repo),
            remote_validation={
                "enabled": True,
                "servers": [
                    {
                        "label": "server_1",
                        "host": "10.0.0.1",
                        "user": "root",
                        "password": "abc",
                        "workdir": "",
                    }
                ],
            },
        )


def test_update_session_rejects_incomplete_secondary_remote_validation_when_enabled(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target_repo = tmp_path / "repo"
    target_repo.mkdir(parents=True, exist_ok=True)
    session = create_session(
        runtime_dir,
        goal="Need remote verification",
        target_repo=str(target_repo),
    )

    with pytest.raises(ValueError, match="server_2 is incomplete"):
        update_session(
            runtime_dir,
            session_id=str(session["session_id"]),
            remote_validation={
                "enabled": True,
                "servers": [
                    {
                        "label": "server_1",
                        "host": "10.0.0.1",
                        "user": "root",
                        "password": "abc",
                        "workdir": "/workspace/project",
                    },
                    {
                        "label": "server_2",
                        "host": "",
                        "user": "root",
                        "password": "abc",
                        "workdir": "/workspace/project",
                    },
                ],
            },
        )


def test_uploaded_context_section_uses_full_content_for_small_attachments(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target_repo = tmp_path / "repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    session = create_session(
        runtime_dir,
        goal="analyze context",
        target_repo=str(target_repo),
    )
    session_id = str(session["session_id"])

    rel = register_uploaded_file(
        runtime_dir,
        session_id=session_id,
        original_name="docs/spec.txt",
        payload=b"line1\nline2\nline3\n",
    )
    mode, context = _build_uploaded_context_section(
        runtime_dir,
        session_id=session_id,
        uploaded_files=[rel],
    )
    assert mode == "full_content_mandatory"
    assert "### FILE: docs/spec.txt" in context
    assert "line1\nline2\nline3" in context


def test_uploaded_context_section_keeps_large_attachment_in_full(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target_repo = tmp_path / "repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    session = create_session(
        runtime_dir,
        goal="analyze context",
        target_repo=str(target_repo),
    )
    session_id = str(session["session_id"])

    long_text = ("HEAD_" * 80) + "MIDDLE_SECRET_MARKER" + ("_TAIL" * 80)
    rel = register_uploaded_file(
        runtime_dir,
        session_id=session_id,
        original_name="notes/long.txt",
        payload=long_text.encode("utf-8"),
    )
    mode, context = _build_uploaded_context_section(
        runtime_dir,
        session_id=session_id,
        uploaded_files=[rel],
    )
    assert mode == "full_content_mandatory"
    assert "MIDDLE_SECRET_MARKER" in context


def test_uploaded_context_section_embeds_binary_as_full_base64(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    target_repo = tmp_path / "repo"
    target_repo.mkdir(parents=True, exist_ok=True)

    session = create_session(
        runtime_dir,
        goal="analyze context",
        target_repo=str(target_repo),
    )
    session_id = str(session["session_id"])

    rel = register_uploaded_file(
        runtime_dir,
        session_id=session_id,
        original_name="bin/blob.bin",
        payload=b"\x00\x01\x02\xff",
    )

    mode, context = _build_uploaded_context_section(
        runtime_dir,
        session_id=session_id,
        uploaded_files=[rel],
    )
    assert mode == "full_content_mandatory"
    assert "type: binary_or_non_utf8" in context
    assert "AAEC/w==" in context
