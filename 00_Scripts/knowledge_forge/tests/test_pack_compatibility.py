from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from knowledge_forge import services
from knowledge_forge.db import connect, init_db


def make_pack_system(tmp_path: Path):
    database_path = tmp_path / "knowledge.db"
    source_path = tmp_path / "lesson.md"
    source_path.write_text("Original knowledge", encoding="utf-8")
    init_db(database_path, tmp_path / "managed")
    with connect(database_path) as conn:
        file_id = conn.execute(
            """
            INSERT INTO files(source_path, library_type, title, filename, status)
            VALUES (?, 'standard', 'Lesson', 'lesson.md', 'completed')
            """,
            (str(source_path),),
        ).lastrowid
        source_id = conn.execute(
            "INSERT INTO knowledge_sources(source_file_id, canonical_name) VALUES (?, 'lesson.md')",
            (file_id,),
        ).lastrowid
        version_id = conn.execute(
            """
            INSERT INTO source_versions(
                source_id, upload_file_id, standard_file_id, standard_path,
                status, version_number, original_filename
            ) VALUES (?, ?, ?, ?, 'available', 1, 'lesson.md')
            """,
            (source_id, file_id, file_id, str(source_path)),
        ).lastrowid
        conn.execute("UPDATE knowledge_sources SET current_version_id=? WHERE id=?", (version_id, source_id))
        conn.execute("INSERT INTO chunks(file_id, chunk_index, text) VALUES (?, 0, 'Original knowledge')", (file_id,))
        tag_id = conn.execute("INSERT INTO tags(name) VALUES ('08_Art')").lastrowid
        conn.execute(
            """
            INSERT INTO tag_assignments(
                target_type, target_id, tag_id, scope, confidence, status, source
            ) VALUES ('file', ?, ?, 'file_strong', 1.0, 'user_approved', 'human')
            """,
            (file_id, tag_id),
        )
        pack_id = conn.execute(
            """
            INSERT INTO packs(
                name, recipe_json, include_sop, include_insight, include_source
            ) VALUES ('Art pack', ?, 1, 1, 1)
            """,
            (json.dumps({"include_tags": ["08_Art"], "min_confidence": 0.7, "tag_statuses": ["user_approved"]}),),
        ).lastrowid
    return database_path, int(source_id), int(version_id), int(file_id), int(pack_id), source_path


def test_pack_preflight_lists_missing_required_artifacts_before_export(tmp_path, monkeypatch):
    database_path, _, _, file_id, pack_id, _ = make_pack_system(tmp_path)
    monkeypatch.setattr(services, "connect", lambda: connect(database_path))
    monkeypatch.setattr(services, "seed_defaults", lambda: None)
    monkeypatch.setattr(services, "PACK_DIR", tmp_path / "packs")

    preflight = services.pack_export_preflight(pack_id)

    assert preflight.ready is False
    assert [(item.file_id, item.artifact_type) for item in preflight.missing] == [
        (file_id, "sop"), (file_id, "insight")
    ]
    with pytest.raises(services.PackExportBlockedError):
        services.export_pack(pack_id)
    assert not (tmp_path / "packs").exists()


def test_export_manifest_is_versioned_snapshot_and_existing_zip_never_changes(tmp_path, monkeypatch):
    database_path, source_id, version_id, file_id, pack_id, source_path = make_pack_system(tmp_path)
    sop_path = tmp_path / "sop.md"
    insight_path = tmp_path / "insight.md"
    sop_path.write_text("SOP content", encoding="utf-8")
    insight_path.write_text("Insight content", encoding="utf-8")
    with connect(database_path) as conn:
        conn.execute("INSERT INTO artifacts(file_id, artifact_type, path, title) VALUES (?, 'sop', ?, 'SOP')", (file_id, str(sop_path)))
        conn.execute("INSERT INTO artifacts(file_id, artifact_type, path, title) VALUES (?, 'insight', ?, 'Insight')", (file_id, str(insight_path)))
    monkeypatch.setattr(services, "connect", lambda: connect(database_path))
    monkeypatch.setattr(services, "seed_defaults", lambda: None)
    monkeypatch.setattr(services, "PACK_DIR", tmp_path / "packs")

    exported = services.export_pack(pack_id)
    original_bytes = exported.read_bytes()
    with ZipFile(exported) as archive:
        manifest = json.loads(archive.read("manifest.json"))

    assert manifest["snapshot"][0]["source_id"] == source_id
    assert manifest["snapshot"][0]["version_id"] == version_id
    assert set(manifest["snapshot"][0]["artifacts"]) == {"source", "sop", "insight"}

    source_path.write_text("Changed later", encoding="utf-8")
    with connect(database_path) as conn:
        conn.execute(
            "UPDATE knowledge_sources SET deleted_at=CURRENT_TIMESTAMP, purge_after='2999-01-01' WHERE id=?",
            (source_id,),
        )

    assert exported.read_bytes() == original_bytes
    assert services.files_for_pack(pack_id) == []


def test_accept_import_upload_does_not_create_a_legacy_job(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    init_db(database_path, tmp_path / "managed")
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(services, "connect", lambda: connect(database_path))
    monkeypatch.setattr(services, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(services, "seed_defaults", lambda: None)

    file_id = services.accept_import_upload("lesson.txt", b"useful lesson")

    with connect(database_path) as conn:
        uploaded = conn.execute("SELECT library_type FROM files WHERE id=?", (file_id,)).fetchone()
        old_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert uploaded["library_type"] == "upload"
    assert old_jobs == 0
