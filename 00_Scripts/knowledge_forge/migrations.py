from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable
from uuid import uuid4


INITIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
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
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT,
    prompt_name TEXT,
    prompt_version TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(file_id, artifact_type, path),
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    parent_id INTEGER,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(parent_id) REFERENCES tags(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS tag_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    scope TEXT NOT NULL DEFAULT 'file_strong',
    confidence REAL DEFAULT 0.9,
    status TEXT NOT NULL DEFAULT 'auto_accepted',
    source TEXT NOT NULL DEFAULT 'system',
    evidence TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(target_type, target_id, tag_id, scope),
    FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_estimate INTEGER DEFAULT 0,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(file_id, chunk_index),
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    step TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    content TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS packs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    recipe_json TEXT NOT NULL,
    include_sop INTEGER NOT NULL DEFAULT 1,
    include_insight INTEGER NOT NULL DEFAULT 1,
    include_source INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id INTEGER NOT NULL,
    export_format TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(pack_id) REFERENCES packs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_files_library_type ON files(library_type);
CREATE INDEX IF NOT EXISTS idx_files_category ON files(main_category, sub_category);
CREATE INDEX IF NOT EXISTS idx_tag_assignments_target ON tag_assignments(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationReport:
    from_version: int
    to_version: int
    applied_versions: tuple[int, ...]
    backup_path: Path | None
    before_counts: dict[str, int]
    after_counts: dict[str, int]


def _apply_initial_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(INITIAL_SCHEMA)


def _add_persistent_import_queue(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS import_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            current_stage TEXT NOT NULL DEFAULT 'queued',
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS worker_leases (
            queue_name TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            expires_at REAL NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_import_tasks_status_id
            ON import_tasks(status, id);
        """
    )


def _add_atomic_core_processing(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        ALTER TABLE import_tasks ADD COLUMN source_id INTEGER;
        ALTER TABLE import_tasks ADD COLUMN version_id INTEGER;

        CREATE TABLE knowledge_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file_id INTEGER NOT NULL UNIQUE,
            current_version_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE source_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            upload_file_id INTEGER NOT NULL,
            standard_file_id INTEGER,
            standard_path TEXT,
            status TEXT NOT NULL DEFAULT 'processing',
            review_status TEXT NOT NULL DEFAULT 'unreviewed',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            available_at TEXT,
            FOREIGN KEY(source_id) REFERENCES knowledge_sources(id) ON DELETE CASCADE,
            FOREIGN KEY(standard_file_id) REFERENCES files(id) ON DELETE SET NULL
        );

        CREATE TABLE import_stage_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            stage_name TEXT NOT NULL,
            payload_text TEXT,
            completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(task_id, stage_name),
            FOREIGN KEY(task_id) REFERENCES import_tasks(id) ON DELETE CASCADE
        );

        CREATE TABLE staged_chunks (
            version_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            token_estimate INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT,
            PRIMARY KEY(version_id, chunk_index),
            FOREIGN KEY(version_id) REFERENCES source_versions(id) ON DELETE CASCADE
        );

        CREATE INDEX idx_source_versions_source ON source_versions(source_id, id);
        CREATE INDEX idx_import_stage_results_task ON import_stage_results(task_id, id);
        """
    )


def _add_retry_pause_and_errors(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        ALTER TABLE import_tasks ADD COLUMN failure_type TEXT;
        ALTER TABLE import_tasks ADD COLUMN failed_stage TEXT;
        ALTER TABLE import_tasks ADD COLUMN user_message TEXT;
        ALTER TABLE import_tasks ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE import_tasks ADD COLUMN next_attempt_at REAL;
        ALTER TABLE import_tasks ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0;

        CREATE INDEX idx_import_tasks_ready
            ON import_tasks(status, pause_requested, next_attempt_at, id);
        """
    )


def _add_content_identity_and_versions(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        ALTER TABLE knowledge_sources ADD COLUMN canonical_name TEXT;
        ALTER TABLE source_versions ADD COLUMN content_fingerprint TEXT;
        ALTER TABLE source_versions ADD COLUMN version_number INTEGER;
        ALTER TABLE source_versions ADD COLUMN original_filename TEXT;

        CREATE UNIQUE INDEX idx_knowledge_sources_canonical_name
            ON knowledge_sources(canonical_name)
            WHERE canonical_name IS NOT NULL;
        CREATE UNIQUE INDEX idx_source_versions_fingerprint
            ON source_versions(content_fingerprint)
            WHERE content_fingerprint IS NOT NULL;
        CREATE UNIQUE INDEX idx_source_versions_number
            ON source_versions(source_id, version_number)
            WHERE version_number IS NOT NULL;
        """
    )


def _add_extraction_quality_metadata(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        ALTER TABLE source_versions ADD COLUMN extraction_metadata_json TEXT;
        ALTER TABLE source_versions ADD COLUMN quality_warnings_json TEXT NOT NULL DEFAULT '[]';
        """
    )


def _add_nonblocking_enhancement_jobs(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE knowledge_enhancement_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            message TEXT,
            prompt_name TEXT,
            prompt_version TEXT,
            artifact_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            UNIQUE(version_id, kind),
            FOREIGN KEY(version_id) REFERENCES source_versions(id) ON DELETE CASCADE,
            FOREIGN KEY(artifact_id) REFERENCES artifacts(id) ON DELETE SET NULL
        );

        CREATE INDEX idx_enhancement_jobs_status
            ON knowledge_enhancement_jobs(status, id);
        """
    )


DEFAULT_MIGRATIONS = (
    Migration(1, "initial-schema", _apply_initial_schema),
    Migration(2, "persistent-text-import-queue", _add_persistent_import_queue),
    Migration(3, "atomic-text-core-processing", _add_atomic_core_processing),
    Migration(4, "retry-pause-and-actionable-errors", _add_retry_pause_and_errors),
    Migration(5, "content-identity-and-source-versions", _add_content_identity_and_versions),
    Migration(6, "document-extraction-and-quality-gates", _add_extraction_quality_metadata),
    Migration(7, "nonblocking-knowledge-enhancement", _add_nonblocking_enhancement_jobs),
)


class MigrationValidationError(RuntimeError):
    pass


class MigrationRunner:
    def __init__(
        self,
        database_path: Path,
        managed_data_dir: Path,
        migrations: Iterable[Migration] = DEFAULT_MIGRATIONS,
    ) -> None:
        self.database_path = Path(database_path)
        self.managed_data_dir = Path(managed_data_dir)
        self.migrations = tuple(sorted(migrations, key=lambda item: item.version))

    def migrate(self) -> MigrationReport:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.managed_data_dir.mkdir(parents=True, exist_ok=True)
        existed = self.database_path.exists()
        from_version = self._read_version(self.database_path) if existed else 0
        pending = tuple(item for item in self.migrations if item.version > from_version)
        if not pending:
            return MigrationReport(from_version, from_version, (), None, {}, {})

        work_dir = self.managed_data_dir / "Migration_Work"
        work_dir.mkdir(parents=True, exist_ok=True)
        rehearsal_path = work_dir / f"rehearsal-{uuid4().hex}.db"
        backup_path = self._backup() if existed else None
        if existed:
            self._copy_database(self.database_path, rehearsal_path)

        before_counts = self._table_counts(rehearsal_path) if existed else {}
        applied: list[int] = []
        try:
            with closing(sqlite3.connect(rehearsal_path)) as conn:
                with conn:
                    conn.execute("PRAGMA foreign_keys = ON")
                    self._ensure_migration_table(conn)
                    for migration in pending:
                        migration.apply(conn)
                        conn.execute(
                            "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                            (migration.version, migration.name),
                        )
                        applied.append(migration.version)
            after_counts = self._table_counts(rehearsal_path)
            self._validate_counts(before_counts, after_counts)
            os.replace(rehearsal_path, self.database_path)
        except Exception:
            rehearsal_path.unlink(missing_ok=True)
            raise

        return MigrationReport(
            from_version=from_version,
            to_version=applied[-1],
            applied_versions=tuple(applied),
            backup_path=backup_path,
            before_counts=before_counts,
            after_counts=after_counts,
        )

    @staticmethod
    def _validate_counts(before_counts: dict[str, int], after_counts: dict[str, int]) -> None:
        changed = {
            table: (count, after_counts.get(table))
            for table, count in before_counts.items()
            if table != "schema_migrations" and after_counts.get(table) != count
        }
        if changed:
            details = ", ".join(
                f"{table}: {before} -> {after}"
                for table, (before, after) in sorted(changed.items())
            )
            raise MigrationValidationError(f"Migration changed protected record counts: {details}")

    @staticmethod
    def _ensure_migration_table(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    @classmethod
    def _read_version(cls, database_path: Path) -> int:
        with closing(sqlite3.connect(database_path)) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if not table:
                return 0
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            return int(row[0] or 0)

    @staticmethod
    def _table_counts(database_path: Path) -> dict[str, int]:
        if not database_path.exists():
            return {}
        with closing(sqlite3.connect(database_path)) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            return {
                table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in tables
            }

    def _backup(self) -> Path:
        backup_dir = self.managed_data_dir / "Backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = backup_dir / f"knowledge-forge-{stamp}.db"
        self._copy_database(self.database_path, backup_path)
        return backup_path

    @staticmethod
    def _copy_database(source_path: Path, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source_path)) as source:
            with closing(sqlite3.connect(target_path)) as target:
                source.backup(target)
