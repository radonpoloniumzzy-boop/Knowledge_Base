from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from knowledge_forge import services
from knowledge_forge.db import connect, init_db
from knowledge_forge.recycle_bin import KnowledgeRecycleBin
import pytest


def make_available_source(tmp_path: Path):
    database_path = tmp_path / "knowledge.db"
    managed_dir = tmp_path / "managed"
    standard_path = managed_dir / "Standard" / "lesson.md"
    standard_path.parent.mkdir(parents=True)
    standard_path.write_text("Useful lesson content", encoding="utf-8")
    init_db(database_path, managed_dir)
    with connect(database_path) as conn:
        file_id = conn.execute(
            """
            INSERT INTO files(
                source_path, library_type, title, filename, main_category, status
            ) VALUES (?, 'standard', 'Lesson', 'lesson.md', '08_Art', 'completed')
            """,
            (str(standard_path),),
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
            (source_id, file_id, file_id, str(standard_path)),
        ).lastrowid
        conn.execute(
            "UPDATE knowledge_sources SET current_version_id=? WHERE id=?",
            (version_id, source_id),
        )
        tag_id = conn.execute("INSERT INTO tags(name) VALUES ('08_Art/色彩')").lastrowid
        conn.execute(
            """
            INSERT INTO tag_assignments(
                target_type, target_id, tag_id, scope, confidence, status, source
            ) VALUES ('file', ?, ?, 'file_strong', 1.0, 'user_approved', 'human')
            """,
            (file_id, tag_id),
        )
        pack_id = conn.execute(
            "INSERT INTO packs(name, recipe_json) VALUES ('Art', ?)",
            (json.dumps({"include_tags": ["08_Art"], "min_confidence": 0.7}),),
        ).lastrowid
    return database_path, managed_dir, int(source_id), int(file_id), int(pack_id), standard_path


def test_recycle_isolates_source_from_library_stats_and_pack_then_restore_recovers_it(tmp_path, monkeypatch):
    database_path, managed_dir, source_id, file_id, pack_id, _ = make_available_source(tmp_path)
    recycle_bin = KnowledgeRecycleBin(database_path, (managed_dir,))
    monkeypatch.setattr(services, "connect", lambda: connect(database_path))
    monkeypatch.setattr(services, "seed_defaults", lambda: None)

    assert [row["id"] for row in services.list_files()] == [file_id]
    assert [row["id"] for row in services.files_for_pack(pack_id)] == [file_id]

    result = recycle_bin.recycle(source_id)

    assert result.status == "recycled"
    assert services.list_files() == []
    assert services.files_for_pack(pack_id) == []
    assert services.dashboard_stats()["stats"]["standard"] == 0
    assert recycle_bin.list_recycled()[0].source_id == source_id

    recycle_bin.restore(source_id)

    assert [row["id"] for row in services.list_files()] == [file_id]
    assert [row["id"] for row in services.files_for_pack(pack_id)] == [file_id]
    with connect(database_path) as conn:
        source = conn.execute("SELECT * FROM knowledge_sources WHERE id=?", (source_id,)).fetchone()
    assert source["current_version_id"] is not None
    assert source["deleted_at"] is None


def test_active_import_requests_pause_and_finalizes_after_task_settles(tmp_path):
    database_path, managed_dir, source_id, file_id, _, _ = make_available_source(tmp_path)
    recycle_bin = KnowledgeRecycleBin(database_path, (managed_dir,))
    with connect(database_path) as conn:
        task_id = conn.execute(
            """
            INSERT INTO import_tasks(
                file_id, filename, status, current_stage, source_id,
                version_id, pause_requested
            ) VALUES (?, 'lesson.md', 'processing', 'chunk_indexing', ?,
                (SELECT current_version_id FROM knowledge_sources WHERE id=?), 0)
            """,
            (file_id, source_id, source_id),
        ).lastrowid

    result = recycle_bin.recycle(source_id)

    assert result.status == "stopping"
    with connect(database_path) as conn:
        task = conn.execute("SELECT * FROM import_tasks WHERE id=?", (task_id,)).fetchone()
        source = conn.execute("SELECT * FROM knowledge_sources WHERE id=?", (source_id,)).fetchone()
    assert task["pause_requested"] == 1
    assert source["deleted_at"] is None
    assert source["recycle_requested_at"] is not None

    with connect(database_path) as conn:
        conn.execute("UPDATE import_tasks SET status='paused', pause_requested=0 WHERE id=?", (task_id,))
    assert recycle_bin.finalize_pending() == 1
    assert recycle_bin.list_recycled()[0].source_id == source_id


def test_recycle_pauses_enhancement_jobs_and_restore_requeues_them(tmp_path):
    database_path, managed_dir, source_id, _, _, _ = make_available_source(tmp_path)
    recycle_bin = KnowledgeRecycleBin(database_path, (managed_dir,))
    with connect(database_path) as conn:
        version_id = conn.execute(
            "SELECT current_version_id FROM knowledge_sources WHERE id=?", (source_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO knowledge_enhancement_jobs(version_id, kind, status) VALUES (?, 'insight', 'processing')",
            (version_id,),
        )

    recycle_bin.recycle(source_id)
    with connect(database_path) as conn:
        assert conn.execute("SELECT status FROM knowledge_enhancement_jobs").fetchone()[0] == "paused"

    recycle_bin.restore(source_id)
    with connect(database_path) as conn:
        assert conn.execute("SELECT status FROM knowledge_enhancement_jobs").fetchone()[0] == "waiting"


def test_expired_source_cannot_be_restored(tmp_path):
    database_path, managed_dir, source_id, _, _, _ = make_available_source(tmp_path)
    deleted = datetime.now(timezone.utc) - timedelta(days=31)
    recycle_bin = KnowledgeRecycleBin(database_path, (managed_dir,), clock=lambda: deleted)
    recycle_bin.recycle(source_id)

    current = KnowledgeRecycleBin(database_path, (managed_dir,), clock=lambda: datetime.now(timezone.utc))
    with pytest.raises(ValueError, match="保留期限"):
        current.restore(source_id)


def test_purge_expired_removes_only_managed_files_and_preserves_exports(tmp_path):
    database_path, managed_dir, source_id, _, pack_id, standard_path = make_available_source(tmp_path)
    outside_path = tmp_path / "personal-source.txt"
    outside_path.write_text("personal", encoding="utf-8")
    export_path = tmp_path / "export.zip"
    export_path.write_bytes(b"snapshot")
    with connect(database_path) as conn:
        conn.execute("UPDATE files SET source_path=? WHERE id=(SELECT source_file_id FROM knowledge_sources WHERE id=?)", (str(outside_path), source_id))
        conn.execute("INSERT INTO exports(pack_id, export_format, path) VALUES (?, 'zip', ?)", (pack_id, str(export_path)))

    old_now = datetime.now(timezone.utc) - timedelta(days=31)
    recycle_bin = KnowledgeRecycleBin(database_path, (managed_dir,), clock=lambda: old_now)
    recycle_bin.recycle(source_id)
    current = datetime.now(timezone.utc)
    purge_bin = KnowledgeRecycleBin(database_path, (managed_dir,), clock=lambda: current)

    assert purge_bin.purge_expired() == 1
    assert not standard_path.exists()
    assert outside_path.exists()
    assert export_path.exists()
    with connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 1
