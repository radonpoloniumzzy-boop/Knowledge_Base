from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from knowledge_forge.migrations import (
    DEFAULT_MIGRATIONS,
    Migration,
    MigrationRunner,
    MigrationValidationError,
)


def test_new_database_records_current_schema_version(tmp_path):
    database_path = tmp_path / "knowledge.db"
    managed_data_dir = tmp_path / "managed"

    report = MigrationRunner(database_path, managed_data_dir).migrate()

    with sqlite3.connect(database_path) as conn:
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert report.from_version == 0
    assert report.to_version == 10
    assert version == 10
    assert {"files", "jobs", "import_tasks", "import_stage_results", "source_versions", "worker_leases", "packs", "schema_migrations"} <= tables


def test_count_mismatch_keeps_live_database_and_backup(tmp_path):
    database_path = tmp_path / "knowledge.db"
    managed_data_dir = tmp_path / "managed"
    MigrationRunner(database_path, managed_data_dir).migrate()
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            INSERT INTO files(source_path, library_type, title, filename)
            VALUES ('lesson.md', 'standard', 'Lesson', 'lesson.md')
            """
        )

    def destructive_migration(conn):
        conn.execute("DELETE FROM files")

    runner = MigrationRunner(
        database_path,
        managed_data_dir,
        migrations=(Migration(11, "destructive", destructive_migration),),
    )

    with pytest.raises(MigrationValidationError):
        runner.migrate()

    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 10
    assert len(list((managed_data_dir / "Backups").glob("*.db"))) == 1


def test_existing_database_is_backed_up_preserved_and_migrated_once(tmp_path):
    database_path = tmp_path / "legacy.db"
    managed_data_dir = tmp_path / "managed"
    with closing(sqlite3.connect(database_path)) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL UNIQUE,
                    library_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    extension TEXT,
                    size_bytes INTEGER DEFAULT 0,
                    mtime REAL DEFAULT 0,
                    main_category TEXT,
                    sub_category TEXT,
                    status TEXT NOT NULL DEFAULT 'completed',
                    confidence REAL DEFAULT 0.9,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                INSERT INTO files(source_path, library_type, title, filename)
                VALUES ('existing.md', 'standard', 'Existing', 'existing.md')
                """
            )

    first = MigrationRunner(database_path, managed_data_dir).migrate()
    second = MigrationRunner(database_path, managed_data_dir).migrate()

    assert first.backup_path is not None and first.backup_path.exists()
    assert first.before_counts["files"] == 1
    assert first.after_counts["files"] == 1
    assert first.applied_versions == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)
    assert second.applied_versions == ()
    assert second.backup_path is None
    with sqlite3.connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 10


def test_schema_10_replaces_legacy_worker_setting_without_changing_record_count(tmp_path):
    database_path = tmp_path / "knowledge.db"
    managed_data_dir = tmp_path / "managed"
    MigrationRunner(database_path, managed_data_dir, DEFAULT_MIGRATIONS[:9]).migrate()
    with closing(sqlite3.connect(database_path)) as conn:
        conn.execute("INSERT INTO settings(key, value) VALUES ('max_workers', '3')")
        conn.commit()

    report = MigrationRunner(database_path, managed_data_dir).migrate()

    with closing(sqlite3.connect(database_path)) as conn:
        settings = dict(conn.execute("SELECT key, value FROM settings"))
    assert report.applied_versions == (10,)
    assert report.before_counts["settings"] == report.after_counts["settings"] == 1
    assert settings == {"import_concurrency": "1"}
