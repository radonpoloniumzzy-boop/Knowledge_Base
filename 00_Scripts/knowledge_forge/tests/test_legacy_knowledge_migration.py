from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from knowledge_forge.db import connect, init_db
from knowledge_forge.legacy_migration import LegacyKnowledgeMigrator, LegacyMigrationValidationError
import pytest


def make_legacy_database(tmp_path: Path) -> tuple[Path, Path, dict[str, int]]:
    database_path = tmp_path / "knowledge.db"
    managed_dir = tmp_path / "managed"
    (tmp_path / "color.md").write_text("Color theory", encoding="utf-8")
    (tmp_path / "color-insight.md").write_text("Color insight", encoding="utf-8")
    init_db(database_path, managed_dir)
    with closing(connect(database_path)) as conn, conn:
        standard_id = conn.execute(
            """
            INSERT INTO files(
                source_path, library_type, title, filename, main_category, status
            ) VALUES (?, 'standard', 'Color lesson', 'color.md', '08_Art', 'completed')
            """,
            (str(tmp_path / "color.md"),),
        ).lastrowid
        derived_id = conn.execute(
            """
            INSERT INTO files(source_path, library_type, title, filename, status)
            VALUES (?, 'insight', 'Color insight', 'color-insight.md', 'completed')
            """,
            (str(tmp_path / "color-insight.md"),),
        ).lastrowid
        unavailable_id = conn.execute(
            """
            INSERT INTO files(source_path, library_type, title, filename, status)
            VALUES (?, 'sop', 'Missing SOP', 'missing-sop.md', 'completed')
            """,
            (str(tmp_path / "missing-sop.md"),),
        ).lastrowid
        chunk_id = conn.execute(
            "INSERT INTO chunks(file_id, chunk_index, text) VALUES (?, 0, 'Color theory')",
            (standard_id,),
        ).lastrowid
        tag_id = conn.execute("INSERT INTO tags(name) VALUES ('08_Art/色彩')").lastrowid
        assignment_id = conn.execute(
            """
            INSERT INTO tag_assignments(
                target_type, target_id, tag_id, scope, confidence, status, source
            ) VALUES ('file', ?, ?, 'file_strong', 0.9, 'user_approved', 'human')
            """,
            (standard_id, tag_id),
        ).lastrowid
        artifact_id = conn.execute(
            """
            INSERT INTO artifacts(file_id, artifact_type, path, title, prompt_version)
            VALUES (?, 'insight', ?, 'Color insight', 'v2')
            """,
            (standard_id, str(tmp_path / "insight.md")),
        ).lastrowid
        derived_artifact_id = conn.execute(
            """
            INSERT INTO artifacts(file_id, artifact_type, path, title, prompt_version)
            VALUES (?, 'insight', ?, 'Color insight', 'v1')
            """,
            (derived_id, str(tmp_path / "color-insight.md")),
        ).lastrowid
        job_id = conn.execute(
            "INSERT INTO jobs(file_id, job_type, status, step) VALUES (?, 'ingest_upload', 'pending', 'queued')",
            (standard_id,),
        ).lastrowid
        pack_id = conn.execute(
            "INSERT INTO packs(name, recipe_json) VALUES ('Art', ?)",
            (json.dumps({"include_tags": ["08_Art"]}),),
        ).lastrowid
    return database_path, managed_dir, {
        "standard": int(standard_id), "derived": int(derived_id), "unavailable": int(unavailable_id), "chunk": int(chunk_id),
        "assignment": int(assignment_id), "artifact": int(artifact_id),
        "derived_artifact": int(derived_artifact_id),
        "job": int(job_id), "pack": int(pack_id),
    }


def test_migration_maps_legacy_standard_without_reprocessing_and_preserves_relationships(tmp_path):
    database_path, managed_dir, ids = make_legacy_database(tmp_path)
    migrator = LegacyKnowledgeMigrator(database_path, managed_dir)

    report = migrator.migrate()

    assert report.created_sources == 2
    assert report.created_versions == 2
    assert report.backup_path.exists()
    assert report.report_path.exists()
    assert report.protected_counts_unchanged is True
    with closing(connect(database_path)) as conn:
        source = conn.execute("SELECT * FROM knowledge_sources WHERE source_file_id=?", (ids["standard"],)).fetchone()
        version = conn.execute("SELECT * FROM source_versions WHERE standard_file_id=?", (ids["standard"],)).fetchone()
        enhancement = {
            row["kind"]: row
            for row in conn.execute("SELECT * FROM knowledge_enhancement_jobs WHERE version_id=?", (version["id"],))
        }
        assert source["source_file_id"] == ids["standard"]
        assert source["canonical_name"] == "color.md"
        assert source["current_version_id"] == version["id"]
        assert version["standard_file_id"] == ids["standard"]
        assert version["upload_file_id"] == ids["standard"]
        assert version["status"] == "available"
        assert version["content_fingerprint"] is None
        assert enhancement["classification"]["status"] == "completed"
        assert enhancement["insight"]["artifact_id"] == ids["artifact"]
        assert enhancement["insight"]["status"] == "completed"
        assert enhancement["sop"]["status"] == "needs_attention"
        assert conn.execute("SELECT COUNT(*) FROM chunks WHERE id=?", (ids["chunk"],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tag_assignments WHERE id=?", (ids["assignment"],)).fetchone()[0] == 1
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (ids["job"],)).fetchone()[0] == "archived"
        assert conn.execute("SELECT COUNT(*) FROM packs WHERE id=?", (ids["pack"],)).fetchone()[0] == 1
        ambiguity = conn.execute(
            "SELECT record_type, record_id, reason FROM legacy_migration_ambiguities"
        ).fetchone()
    assert tuple(ambiguity) == (
        "unavailable_legacy_file", ids["unavailable"], "旧资料文件不存在或内容为空，保留数据库记录但不提升为可用版本。"
    )


def test_migration_is_idempotent_and_does_not_queue_legacy_enhancement(tmp_path):
    database_path, managed_dir, _ = make_legacy_database(tmp_path)
    migrator = LegacyKnowledgeMigrator(database_path, managed_dir)
    assert migrator.needs_migration() is True

    first = migrator.migrate()
    second = migrator.migrate()

    assert first.created_sources == 2
    assert second.created_sources == 0
    assert second.created_versions == 0
    assert migrator.needs_migration() is False
    with closing(connect(database_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM source_versions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM knowledge_enhancement_jobs WHERE status='waiting'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM legacy_migration_runs").fetchone()[0] == 2


def test_validation_failure_keeps_live_database_unchanged_and_backup_available(tmp_path):
    database_path, managed_dir, ids = make_legacy_database(tmp_path)
    with closing(connect(database_path)) as conn, conn:
        conn.execute(
            """
            CREATE TRIGGER sabotage_legacy_migration
            AFTER INSERT ON legacy_migration_runs
            BEGIN
                DELETE FROM packs;
            END
            """
        )
    migrator = LegacyKnowledgeMigrator(database_path, managed_dir)

    with pytest.raises(LegacyMigrationValidationError):
        migrator.migrate()

    with closing(connect(database_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM packs WHERE id=?", (ids["pack"],)).fetchone()[0] == 1
    assert list((managed_dir / "Backups").glob("knowledge-forge-legacy-*.db"))
