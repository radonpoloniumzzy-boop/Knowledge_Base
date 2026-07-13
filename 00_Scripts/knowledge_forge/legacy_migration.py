from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from contextlib import closing
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .enhancement import ENHANCEMENT_KINDS, PROMPT_NAMES
from .migrations import MigrationRunner


PROTECTED_TABLES = (
    "files", "artifacts", "chunks", "tags", "tag_assignments", "jobs",
    "prompts", "packs", "exports", "feedback_events", "settings",
)
RECONCILED_TABLES = PROTECTED_TABLES + (
    "knowledge_sources", "source_versions", "knowledge_enhancement_jobs",
    "legacy_migration_runs", "legacy_migration_ambiguities",
)


class LegacyMigrationValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class LegacyMigrationReport:
    run_id: int
    created_sources: int
    created_versions: int
    ambiguity_count: int
    backup_path: Path
    report_path: Path
    before_counts: dict[str, int]
    after_counts: dict[str, int]
    protected_counts_unchanged: bool


class LegacyKnowledgeMigrator:
    """Backfill legacy standard files into source/version records on a rehearsed copy."""

    def __init__(self, database_path: Path, managed_data_dir: Path) -> None:
        self.database_path = Path(database_path)
        self.managed_data_dir = Path(managed_data_dir)

    def needs_migration(self) -> bool:
        if not self.database_path.exists():
            return False
        with closing(sqlite3.connect(self.database_path)) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_versions'"
            ).fetchone()
            if table is None:
                return False
            conn.row_factory = sqlite3.Row
            return bool(self._unmapped_eligible_files(conn))

    def migrate(self) -> LegacyMigrationReport:
        MigrationRunner(self.database_path, self.managed_data_dir).migrate()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = self.managed_data_dir / "Backups" / f"knowledge-forge-legacy-{stamp}.db"
        rehearsal_path = self.managed_data_dir / "Migration_Work" / f"legacy-{uuid4().hex}.db"
        report_path = self.managed_data_dir / "Migration_Reports" / f"legacy-migration-{stamp}.json"
        temporary_report_path = report_path.with_suffix(".tmp")
        self._copy_database(self.database_path, backup_path)
        self._copy_database(self.database_path, rehearsal_path)
        live_signature = self._database_signature()
        before_counts = self._counts(rehearsal_path, RECONCILED_TABLES)
        activated = False
        try:
            with closing(sqlite3.connect(rehearsal_path)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                with conn:
                    run_id, created_sources, created_versions, ambiguities = self._transform(
                        conn, backup_path, report_path, before_counts
                    )
                    after_counts = self._counts_from_connection(conn, RECONCILED_TABLES)
                    self._validate(conn, before_counts, after_counts)
                    conn.execute(
                        """
                        UPDATE legacy_migration_runs
                        SET status='completed', after_counts_json=?, created_sources=?,
                            created_versions=?, ambiguity_count=?, completed_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """,
                        (
                            json.dumps(after_counts, ensure_ascii=False), created_sources,
                            created_versions, len(ambiguities), run_id,
                        ),
                    )
            report_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_report_path.write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "created_sources": created_sources,
                        "created_versions": created_versions,
                        "ambiguity_count": len(ambiguities),
                        "protected_counts_unchanged": self._protected_counts_unchanged(before_counts, after_counts),
                        "before_counts": before_counts,
                        "after_counts": after_counts,
                        "backup_path": str(backup_path),
                        "ambiguities": ambiguities,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary_report_path, report_path)
            if self._database_signature() != live_signature:
                raise LegacyMigrationValidationError("正式数据库在副本演练期间发生变化，已中止替换。")
            os.replace(rehearsal_path, self.database_path)
            activated = True
        except Exception:
            rehearsal_path.unlink(missing_ok=True)
            temporary_report_path.unlink(missing_ok=True)
            if not activated:
                report_path.unlink(missing_ok=True)
            raise
        return LegacyMigrationReport(
            run_id=run_id,
            created_sources=created_sources,
            created_versions=created_versions,
            ambiguity_count=len(ambiguities),
            backup_path=backup_path,
            report_path=report_path,
            before_counts=before_counts,
            after_counts=after_counts,
            protected_counts_unchanged=self._protected_counts_unchanged(before_counts, after_counts),
        )

    def _transform(self, conn, backup_path, report_path, before_counts):
        run_id = int(conn.execute(
            """
            INSERT INTO legacy_migration_runs(
                status, backup_path, report_path, before_counts_json
            ) VALUES ('processing', ?, ?, ?)
            """,
            (str(backup_path), str(report_path), json.dumps(before_counts, ensure_ascii=False)),
        ).lastrowid)
        legacy_files = self._unmapped_eligible_files(conn)
        all_standard_names = [
            self._canonical_name(row["filename"])
            for row in self._eligible_files(conn)
        ]
        name_counts = Counter(all_standard_names)
        created_sources = 0
        created_versions = 0
        for file in legacy_files:
            canonical_name = self._canonical_name(file["filename"])
            available_name = conn.execute(
                "SELECT 1 FROM knowledge_sources WHERE canonical_name=?", (canonical_name,)
            ).fetchone() is None
            if name_counts[canonical_name] != 1 or not available_name:
                canonical_name = f"legacy:{file['id']}"
            source_id = int(conn.execute(
                """
                INSERT INTO knowledge_sources(source_file_id, canonical_name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (file["id"], canonical_name, file["created_at"], file["updated_at"]),
            ).lastrowid)
            version_id = int(conn.execute(
                """
                INSERT INTO source_versions(
                    source_id, upload_file_id, standard_file_id, standard_path,
                    status, review_status, version_number, original_filename,
                    created_at, available_at
                ) VALUES (?, ?, ?, ?, 'available', 'unreviewed', 1, ?, ?, ?)
                """,
                (
                    source_id, file["id"], file["id"], file["source_path"],
                    file["filename"], file["created_at"], file["updated_at"],
                ),
            ).lastrowid)
            conn.execute(
                "UPDATE knowledge_sources SET current_version_id=? WHERE id=?",
                (version_id, source_id),
            )
            self._create_enhancement_history(conn, version_id, file)
            created_sources += 1
            created_versions += 1

        conn.execute(
            """
            UPDATE jobs
            SET status='archived', step='legacy_history', error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE job_type IN ('ingest_upload', 'scan_existing_library')
              AND status IN ('pending', 'waiting', 'processing', 'running')
            """
        )
        ambiguities = self._find_ambiguities(conn)
        conn.executemany(
            """
            INSERT INTO legacy_migration_ambiguities(run_id, record_type, record_id, reason)
            VALUES (?, ?, ?, ?)
            """,
            [(run_id, item["record_type"], item["record_id"], item["reason"]) for item in ambiguities],
        )
        return run_id, created_sources, created_versions, ambiguities

    @staticmethod
    def _create_enhancement_history(conn, version_id: int, file) -> None:
        has_tags = conn.execute(
            "SELECT 1 FROM tag_assignments WHERE target_type='file' AND target_id=? LIMIT 1",
            (file["id"],),
        ).fetchone() is not None
        classified = bool(file["main_category"] and file["main_category"] != "00_Unsorted") or has_tags
        classification_status = "completed" if classified else "needs_attention"
        classification_message = None if classified else "旧资料没有可靠分类，可按需重新生成。"
        conn.execute(
            """
            INSERT INTO knowledge_enhancement_jobs(
                version_id, kind, status, message, prompt_name, completed_at
            ) VALUES (?, 'classification', ?, ?, ?, CASE WHEN ?='completed' THEN CURRENT_TIMESTAMP END)
            """,
            (
                version_id, classification_status, classification_message,
                PROMPT_NAMES["classification"], classification_status,
            ),
        )
        for kind in (item for item in ENHANCEMENT_KINDS if item != "classification"):
            artifact = conn.execute(
                """
                SELECT id, prompt_version
                FROM artifacts
                WHERE file_id=? AND artifact_type=?
                ORDER BY id DESC LIMIT 1
                """,
                (file["id"], kind),
            ).fetchone()
            if artifact is None:
                conn.execute(
                    """
                    INSERT INTO knowledge_enhancement_jobs(
                        version_id, kind, status, message, prompt_name
                    ) VALUES (?, ?, 'needs_attention', ?, ?)
                    """,
                    (version_id, kind, f"旧资料未包含 {kind}，可按需重新生成。", PROMPT_NAMES[kind]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO knowledge_enhancement_jobs(
                        version_id, kind, status, prompt_name, prompt_version,
                        artifact_id, completed_at
                    ) VALUES (?, ?, 'completed', ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        version_id, kind, PROMPT_NAMES[kind],
                        artifact["prompt_version"], artifact["id"],
                    ),
                )

    @staticmethod
    def _find_ambiguities(conn) -> list[dict[str, object]]:
        ambiguities: list[dict[str, object]] = []
        unavailable = conn.execute(
            """
            SELECT f.id, f.source_path
            FROM files f
            WHERE f.library_type IN ('standard', 'sop', 'insight')
              AND f.status='completed'
            ORDER BY f.id
            """
        ).fetchall()
        ambiguities.extend(
            {
                "record_type": "unavailable_legacy_file",
                "record_id": int(row["id"]),
                "reason": "旧资料文件不存在或内容为空，保留数据库记录但不提升为可用版本。",
            }
            for row in unavailable
            if not LegacyKnowledgeMigrator._path_is_usable(row["source_path"])
        )
        missing_tag_targets = conn.execute(
            """
            SELECT ta.id
            FROM tag_assignments ta
            WHERE ta.target_type='file'
              AND NOT EXISTS (SELECT 1 FROM files f WHERE f.id=ta.target_id)
            ORDER BY ta.id
            """
        ).fetchall()
        ambiguities.extend(
            {
                "record_type": "missing_tag_target",
                "record_id": int(row["id"]),
                "reason": "标签关系指向不存在的旧文件，保留记录但不猜测归属。",
            }
            for row in missing_tag_targets
        )
        unsupported_tag_targets = conn.execute(
            "SELECT id FROM tag_assignments WHERE target_type!='file' ORDER BY id"
        ).fetchall()
        ambiguities.extend(
            {
                "record_type": "unsupported_tag_target",
                "record_id": int(row["id"]),
                "reason": "旧标签使用未知目标类型，保留记录但不猜测归属。",
            }
            for row in unsupported_tag_targets
        )
        missing_feedback_targets = conn.execute(
            """
            SELECT fe.id
            FROM feedback_events fe
            WHERE fe.target_type='file'
              AND NOT EXISTS (SELECT 1 FROM files f WHERE f.id=fe.target_id)
            ORDER BY fe.id
            """
        ).fetchall()
        ambiguities.extend(
            {
                "record_type": "missing_feedback_target",
                "record_id": int(row["id"]),
                "reason": "反馈指向不存在的旧文件，保留记录但不猜测归属。",
            }
            for row in missing_feedback_targets
        )
        unsupported_feedback_targets = conn.execute(
            "SELECT id FROM feedback_events WHERE target_type!='file' ORDER BY id"
        ).fetchall()
        ambiguities.extend(
            {
                "record_type": "unsupported_feedback_target",
                "record_id": int(row["id"]),
                "reason": "旧反馈使用未知目标类型，保留记录但不猜测归属。",
            }
            for row in unsupported_feedback_targets
        )
        missing_artifact_prompts = conn.execute(
            """
            SELECT a.id
            FROM artifacts a
            WHERE a.prompt_name IS NOT NULL AND a.prompt_version IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM prompts p
                  WHERE p.name=a.prompt_name AND p.version=a.prompt_version
              )
            ORDER BY a.id
            """
        ).fetchall()
        ambiguities.extend(
            {
                "record_type": "missing_artifact_prompt",
                "record_id": int(row["id"]),
                "reason": "产物引用的旧提示词版本不存在，保留产物但不补造提示词。",
            }
            for row in missing_artifact_prompts
        )
        for pack in conn.execute("SELECT id, recipe_json FROM packs ORDER BY id").fetchall():
            try:
                tags = json.loads(pack["recipe_json"]).get("include_tags", [])
            except (TypeError, ValueError, AttributeError):
                ambiguities.append({
                    "record_type": "invalid_pack_recipe", "record_id": int(pack["id"]),
                    "reason": "能力包旧配方无法解析，保留原值但不猜测修复。",
                })
                continue
            for tag in tags:
                exists = conn.execute(
                    "SELECT 1 FROM tags WHERE name=? OR name LIKE ? LIMIT 1", (tag, f"{tag}/%")
                ).fetchone()
                if exists is None:
                    ambiguities.append({
                        "record_type": "missing_pack_tag", "record_id": int(pack["id"]),
                        "reason": f"能力包引用不存在的标签：{tag}。保留配方但不猜测替换。",
                    })
        eligible = LegacyKnowledgeMigrator._eligible_files(conn)
        canonical_counts = Counter(
            LegacyKnowledgeMigrator._canonical_name(row["filename"]) for row in eligible
        )
        ambiguities.extend(
            {
                "record_type": "duplicate_canonical_name",
                "record_id": int(row["id"]),
                "reason": "存在同名旧资料，分别建立知识资料，不猜测合并。",
            }
            for row in eligible
            if canonical_counts[LegacyKnowledgeMigrator._canonical_name(row["filename"])] > 1
        )
        return ambiguities

    @staticmethod
    def _validate(conn, before_counts, after_counts) -> None:
        changed = {
            table: (before_counts[table], after_counts[table])
            for table in PROTECTED_TABLES
            if before_counts[table] != after_counts[table]
        }
        if changed:
            raise LegacyMigrationValidationError(f"迁移改变了受保护记录数量: {changed}")
        missing = 0
        for file in LegacyKnowledgeMigrator._eligible_files(conn):
            mapped = conn.execute(
                "SELECT COUNT(*) FROM source_versions WHERE standard_file_id=?", (file["id"],)
            ).fetchone()[0]
            missing += int(mapped != 1)
        invalid_current = conn.execute(
            """
            SELECT COUNT(*)
            FROM knowledge_sources ks
            WHERE ks.current_version_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM source_versions sv
                  WHERE sv.id=ks.current_version_id AND sv.source_id=ks.id
              )
            """
        ).fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        if missing or invalid_current or foreign_keys:
            raise LegacyMigrationValidationError(
                f"迁移完整性验证失败: missing={missing}, invalid_current={invalid_current}, foreign_keys={len(foreign_keys)}"
            )

    @staticmethod
    def _canonical_name(filename: str) -> str:
        return unicodedata.normalize("NFKC", Path(filename).name).casefold()

    @staticmethod
    def _path_is_usable(raw_path: str) -> bool:
        path = Path(raw_path)
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @classmethod
    def _eligible_files(cls, conn) -> list[sqlite3.Row]:
        rows = conn.execute(
            """
            SELECT f.* FROM files f
            WHERE f.library_type IN ('standard', 'sop', 'insight')
              AND f.status='completed'
            ORDER BY f.id
            """
        ).fetchall()
        return [row for row in rows if cls._path_is_usable(row["source_path"])]

    @classmethod
    def _unmapped_eligible_files(cls, conn) -> list[sqlite3.Row]:
        return [
            row for row in cls._eligible_files(conn)
            if conn.execute(
                "SELECT 1 FROM source_versions WHERE standard_file_id=?", (row["id"],)
            ).fetchone() is None
        ]

    def _database_signature(self) -> tuple[int, int]:
        stat = self.database_path.stat()
        return stat.st_size, stat.st_mtime_ns

    @staticmethod
    def _protected_counts_unchanged(before_counts, after_counts) -> bool:
        return all(before_counts[table] == after_counts[table] for table in PROTECTED_TABLES)

    @staticmethod
    def _counts(database_path: Path, tables) -> dict[str, int]:
        with closing(sqlite3.connect(database_path)) as conn:
            return LegacyKnowledgeMigrator._counts_from_connection(conn, tables)

    @staticmethod
    def _counts_from_connection(conn, tables) -> dict[str, int]:
        return {table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) for table in tables}

    @staticmethod
    def _copy_database(source_path: Path, target_path: Path) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source_path)) as source:
            with closing(sqlite3.connect(target_path)) as target:
                source.backup(target)
