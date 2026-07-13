from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from typing import Callable, Iterable

from .db import connect


@dataclass(frozen=True)
class RecycleResult:
    source_id: int
    status: str
    deleted_at: str | None
    purge_after: str | None


@dataclass(frozen=True)
class RecycledSource:
    source_id: int
    title: str
    filename: str
    deleted_at: str
    purge_after: str
    current_version_id: int | None


class KnowledgeRecycleBin:
    """Soft-delete boundary for a knowledge source and all managed relationships."""

    def __init__(
        self,
        database_path: Path,
        managed_roots: Iterable[Path],
        *,
        retention_days: int = 30,
        clock: Callable[[], datetime] | None = None,
        cleanup_interval: float = 3600.0,
    ) -> None:
        self.database_path = Path(database_path)
        self.managed_roots = tuple(Path(root).resolve() for root in managed_roots)
        self.retention_days = retention_days
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cleanup_interval = cleanup_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def recycle(self, source_id: int) -> RecycleResult:
        requested_at = self._timestamp(self._clock())
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute(
                "SELECT id, deleted_at, purge_after FROM knowledge_sources WHERE id=?",
                (source_id,),
            ).fetchone()
            if source is None:
                raise KeyError(source_id)
            if source["deleted_at"] is not None:
                return RecycleResult(source_id, "recycled", source["deleted_at"], source["purge_after"])

            active = conn.execute(
                "SELECT COUNT(*) FROM import_tasks WHERE source_id=? AND status='processing'",
                (source_id,),
            ).fetchone()[0]
            conn.execute(
                """
                UPDATE import_tasks
                SET pause_requested=CASE WHEN status='processing' THEN 1 ELSE pause_requested END,
                    status=CASE WHEN status='waiting' THEN 'paused' ELSE status END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE source_id=? AND status IN ('waiting', 'processing')
                """,
                (source_id,),
            )
            conn.execute(
                "UPDATE knowledge_sources SET recycle_requested_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (requested_at, source_id),
            )
            conn.execute(
                """
                UPDATE knowledge_enhancement_jobs
                SET status='paused', updated_at=CURRENT_TIMESTAMP
                WHERE version_id IN (SELECT id FROM source_versions WHERE source_id=?)
                  AND status IN ('waiting', 'processing', 'needs_attention')
                """,
                (source_id,),
            )
            if active:
                return RecycleResult(source_id, "stopping", None, None)
            deleted_at, purge_after = self._mark_recycled(conn, source_id, self._clock())
        return RecycleResult(source_id, "recycled", deleted_at, purge_after)

    def restore(self, source_id: int) -> RecycleResult:
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute(
                "SELECT id, purge_after FROM knowledge_sources WHERE id=? AND (deleted_at IS NOT NULL OR recycle_requested_at IS NOT NULL)",
                (source_id,),
            ).fetchone()
            if source is None:
                raise KeyError(source_id)
            if source["purge_after"] and source["purge_after"] <= self._timestamp(self._clock()):
                raise ValueError("知识资料已超过 30 天保留期限，不能恢复。")
            conn.execute(
                """
                UPDATE knowledge_sources
                SET recycle_requested_at=NULL, deleted_at=NULL, purge_after=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (source_id,),
            )
            conn.execute(
                """
                UPDATE knowledge_enhancement_jobs
                SET status='waiting', message=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE version_id IN (SELECT id FROM source_versions WHERE source_id=?)
                  AND status='paused'
                """,
                (source_id,),
            )
        return RecycleResult(source_id, "active", None, None)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.purge_expired()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._cleanup_loop, name="knowledge-recycle-cleanup", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _cleanup_loop(self) -> None:
        while not self._stop_event.wait(self._cleanup_interval):
            try:
                self.purge_expired()
            except Exception:
                pass

    def finalize_pending(self) -> int:
        now = self._clock()
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT ks.id
                FROM knowledge_sources ks
                WHERE ks.recycle_requested_at IS NOT NULL
                  AND ks.deleted_at IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM import_tasks it
                      WHERE it.source_id=ks.id AND it.status='processing'
                  )
                """
            ).fetchall()
            for row in rows:
                self._mark_recycled(conn, int(row["id"]), now)
        return len(rows)

    def list_recycled(self) -> list[RecycledSource]:
        with connect(self.database_path) as conn:
            rows = conn.execute(
                """
                SELECT ks.id AS source_id, ks.deleted_at, ks.purge_after,
                       ks.current_version_id, f.title, f.filename
                FROM knowledge_sources ks
                JOIN files f ON f.id=ks.source_file_id
                WHERE ks.deleted_at IS NOT NULL
                ORDER BY ks.deleted_at DESC, ks.id DESC
                """
            ).fetchall()
        return [RecycledSource(**dict(row)) for row in rows]

    def source_id_for_file(self, file_id: int) -> int | None:
        with connect(self.database_path) as conn:
            row = conn.execute(
                """
                SELECT ks.id
                FROM knowledge_sources ks
                LEFT JOIN source_versions sv ON sv.source_id=ks.id
                WHERE ks.source_file_id=? OR sv.upload_file_id=? OR sv.standard_file_id=?
                ORDER BY CASE WHEN sv.standard_file_id=? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (file_id, file_id, file_id, file_id),
            ).fetchone()
        return int(row["id"]) if row is not None else None

    def purge_expired(self) -> int:
        cutoff = self._timestamp(self._clock())
        with connect(self.database_path) as conn:
            source_ids = [
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM knowledge_sources WHERE deleted_at IS NOT NULL AND purge_after<=?",
                    (cutoff,),
                ).fetchall()
            ]
        for source_id in source_ids:
            self._purge_source(source_id)
        return len(source_ids)

    def _purge_source(self, source_id: int) -> None:
        paths: set[Path] = set()
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = conn.execute(
                "SELECT source_file_id FROM knowledge_sources WHERE id=? AND deleted_at IS NOT NULL",
                (source_id,),
            ).fetchone()
            if source is None:
                return
            versions = conn.execute(
                "SELECT id, upload_file_id, standard_file_id, standard_path FROM source_versions WHERE source_id=?",
                (source_id,),
            ).fetchall()
            version_ids = [int(row["id"]) for row in versions]
            file_ids = {int(source["source_file_id"])}
            for row in versions:
                file_ids.add(int(row["upload_file_id"]))
                if row["standard_file_id"] is not None:
                    file_ids.add(int(row["standard_file_id"]))
                if row["standard_path"]:
                    paths.add(Path(row["standard_path"]))
            if file_ids:
                placeholders = ",".join("?" for _ in file_ids)
                for row in conn.execute(
                    f"SELECT source_path FROM files WHERE id IN ({placeholders})", tuple(file_ids)
                ).fetchall():
                    paths.add(Path(row["source_path"]))
                for row in conn.execute(
                    f"SELECT path FROM artifacts WHERE file_id IN ({placeholders})", tuple(file_ids)
                ).fetchall():
                    paths.add(Path(row["path"]))
                conn.execute(
                    f"DELETE FROM tag_assignments WHERE target_type='file' AND target_id IN ({placeholders})",
                    tuple(file_ids),
                )
                conn.execute(f"DELETE FROM chunks WHERE file_id IN ({placeholders})", tuple(file_ids))
                conn.execute(f"DELETE FROM artifacts WHERE file_id IN ({placeholders})", tuple(file_ids))
            task_ids = [
                int(row["id"])
                for row in conn.execute("SELECT id FROM import_tasks WHERE source_id=?", (source_id,)).fetchall()
            ]
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                conn.execute(f"DELETE FROM import_stage_results WHERE task_id IN ({placeholders})", tuple(task_ids))
                conn.execute(f"DELETE FROM import_tasks WHERE id IN ({placeholders})", tuple(task_ids))
            if version_ids:
                placeholders = ",".join("?" for _ in version_ids)
                conn.execute(f"DELETE FROM knowledge_enhancement_jobs WHERE version_id IN ({placeholders})", tuple(version_ids))
            for path in paths:
                if self._is_managed_file(path):
                    path.unlink()
            conn.execute("DELETE FROM knowledge_sources WHERE id=?", (source_id,))
            if file_ids:
                placeholders = ",".join("?" for _ in file_ids)
                conn.execute(f"DELETE FROM files WHERE id IN ({placeholders})", tuple(file_ids))

    def _mark_recycled(self, conn, source_id: int, now: datetime) -> tuple[str, str]:
        deleted_at = self._timestamp(now)
        purge_after = self._timestamp(now + timedelta(days=self.retention_days))
        conn.execute(
            """
            UPDATE knowledge_sources
            SET recycle_requested_at=NULL, deleted_at=?, purge_after=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (deleted_at, purge_after, source_id),
        )
        return deleted_at, purge_after

    def _is_managed_file(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return resolved.is_file() and any(resolved.is_relative_to(root) for root in self.managed_roots)

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
