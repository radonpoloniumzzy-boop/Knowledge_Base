from __future__ import annotations

import sqlite3
import hashlib
import json
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from uuid import uuid4

from .db import connect


WAITING = "waiting"
PROCESSING = "processing"
COMPLETED = "completed"
NEEDS_ATTENTION = "needs_attention"
PAUSED = "paused"
QUEUE_NAME = "text-import"
RETRY_DELAYS = (5.0, 30.0, 120.0)


class ImportProcessingError(Exception):
    failure_type = "deterministic"

    def __init__(self, stage: str, user_message: str) -> None:
        super().__init__(user_message)
        self.stage = stage
        self.user_message = user_message


class TransientImportError(ImportProcessingError):
    failure_type = "transient"


class DeterministicImportError(ImportProcessingError):
    failure_type = "deterministic"


class TaskPaused(Exception):
    pass


class RecycledDuplicateError(Exception):
    def __init__(self, source_id: int) -> None:
        super().__init__("相同内容位于回收站，请先恢复知识资料。")
        self.source_id = source_id


@dataclass(frozen=True)
class ImportTask:
    id: int
    file_id: int
    filename: str
    status: str
    current_stage: str
    error: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    completed_at: str | None
    source_id: int | None
    version_id: int | None
    failure_type: str | None
    failed_stage: str | None
    user_message: str | None
    retry_count: int
    next_attempt_at: float | None
    pause_requested: bool


class PersistentTextImportQueue:
    """Durable, single-worker queue around the existing text ingestion adapters."""

    def __init__(
        self,
        database_path: Path,
        accept_upload: Callable[[str, bytes], int],
        process_file: Callable[[ImportTask], None],
        *,
        poll_interval: float = 0.5,
        lease_seconds: float = 10.0,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
        clock: Callable[[], float] = time.time,
        completion_callback: Callable[[ImportTask], None] | None = None,
        task_settled_callback: Callable[[], None] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self._accept_upload = accept_upload
        self._process_file = process_file
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds
        self._retry_delays = retry_delays
        self._clock = clock
        self._completion_callback = completion_callback
        self._task_settled_callback = task_settled_callback
        self._owner_id = uuid4().hex
        self._submit_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None

    def submit(self, filename: str, data: bytes) -> ImportTask:
        with self._submit_lock:
            return self._submit_locked(filename, data)

    def _submit_locked(self, filename: str, data: bytes) -> ImportTask:
        fingerprint = hashlib.sha256(data).hexdigest()
        existing = self._task_for_fingerprint(fingerprint)
        if existing is not None:
            return existing
        file_id = self._accept_upload(filename, data)
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            canonical_name = self._canonical_name(filename)
            source = conn.execute(
                "SELECT id FROM knowledge_sources WHERE canonical_name=?",
                (canonical_name,),
            ).fetchone()
            if source is None:
                source_id = conn.execute(
                    """
                    INSERT INTO knowledge_sources(source_file_id, canonical_name)
                    VALUES (?, ?)
                    """,
                    (file_id, canonical_name),
                ).lastrowid
            else:
                source_id = source["id"]
            version_number = conn.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM source_versions WHERE source_id=?",
                (source_id,),
            ).fetchone()[0]
            version_id = conn.execute(
                """
                INSERT INTO source_versions(
                    source_id, upload_file_id, content_fingerprint,
                    version_number, original_filename
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (source_id, file_id, fingerprint, version_number, filename),
            ).lastrowid
            task_id = conn.execute(
                """
                INSERT INTO import_tasks(
                    file_id, filename, status, current_stage, source_id, version_id
                ) VALUES (?, ?, 'waiting', 'queued', ?, ?)
                """,
                (file_id, filename, source_id, version_id),
            ).lastrowid
        return self.get_task(int(task_id))

    def submit_many(self, uploads: Iterable[tuple[str, bytes]]) -> list[ImportTask]:
        return [self.submit(filename, data) for filename, data in uploads]

    def get_task(self, task_id: int) -> ImportTask:
        with connect(self.database_path) as conn:
            row = conn.execute("SELECT * FROM import_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task_from_row(row)

    def list_tasks(self, limit: int = 50) -> list[ImportTask]:
        with connect(self.database_path) as conn:
            rows = conn.execute(
                "SELECT * FROM import_tasks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def summary(self) -> dict[str, int]:
        counts = {
            status: 0
            for status in (WAITING, PROCESSING, PAUSED, COMPLETED, NEEDS_ATTENTION)
        }
        with connect(self.database_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM import_tasks GROUP BY status"
            ).fetchall()
        for row in rows:
            if row["status"] in counts:
                counts[row["status"]] = int(row["count"])
        return counts

    def knowledge_entry_for_task(self, task_id: int) -> int | None:
        with connect(self.database_path) as conn:
            row = conn.execute(
                """
                SELECT sv.standard_file_id
                FROM import_tasks it
                JOIN source_versions sv ON sv.id=it.version_id
                JOIN knowledge_sources ks ON ks.id=sv.source_id
                WHERE it.id=? AND sv.status='available'
                  AND ks.deleted_at IS NULL
                  AND ks.recycle_requested_at IS NULL
                """,
                (task_id,),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def version_history_for_file(self, file_id: int) -> list[dict[str, object]]:
        with connect(self.database_path) as conn:
            source = conn.execute(
                """
                SELECT ks.id, ks.current_version_id
                FROM knowledge_sources ks
                JOIN source_versions sv ON sv.source_id=ks.id
                WHERE sv.standard_file_id=? OR sv.upload_file_id=?
                ORDER BY CASE WHEN sv.standard_file_id=? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (file_id, file_id, file_id),
            ).fetchone()
            if source is None:
                return []
            rows = conn.execute(
                """
                SELECT sv.*, it.status AS task_status, it.user_message
                FROM source_versions sv
                LEFT JOIN import_tasks it ON it.version_id=sv.id
                WHERE sv.source_id=?
                ORDER BY sv.version_number DESC, sv.id DESC
                """,
                (source["id"],),
            ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            item["is_current"] = row["id"] == source["current_version_id"]
            item["quality_warnings"] = json.loads(item.get("quality_warnings_json") or "[]")
            item["extraction_metadata"] = json.loads(item.get("extraction_metadata_json") or "{}")
            history.append(item)
        return history

    def _task_for_fingerprint(self, fingerprint: str) -> ImportTask | None:
        with connect(self.database_path) as conn:
            row = conn.execute(
                """
                SELECT it.id, ks.id AS source_id, ks.deleted_at, ks.recycle_requested_at
                FROM source_versions sv
                JOIN import_tasks it ON it.version_id=sv.id
                JOIN knowledge_sources ks ON ks.id=sv.source_id
                WHERE sv.content_fingerprint=?
                ORDER BY it.id LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        if row is None:
            return None
        if row["deleted_at"] is not None or row["recycle_requested_at"] is not None:
            raise RecycledDuplicateError(int(row["source_id"]))
        return self.get_task(int(row["id"]))

    @staticmethod
    def _canonical_name(filename: str) -> str:
        return unicodedata.normalize("NFKC", Path(filename).name).casefold()

    def acquire_worker(self) -> bool:
        now = self._clock()
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO worker_leases(queue_name, owner_id, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(queue_name) DO UPDATE SET
                    owner_id=excluded.owner_id,
                    expires_at=excluded.expires_at,
                    updated_at=CURRENT_TIMESTAMP
                WHERE worker_leases.expires_at <= ?
                   OR worker_leases.owner_id = excluded.owner_id
                """,
                (QUEUE_NAME, self._owner_id, now + self._lease_seconds, now),
            )
            row = conn.execute(
                "SELECT owner_id FROM worker_leases WHERE queue_name=?", (QUEUE_NAME,)
            ).fetchone()
        return row is not None and row["owner_id"] == self._owner_id

    def release_worker(self) -> None:
        with connect(self.database_path) as conn:
            conn.execute(
                "DELETE FROM worker_leases WHERE queue_name=? AND owner_id=?",
                (QUEUE_NAME, self._owner_id),
            )

    def recover_interrupted(self) -> int:
        if not self._owns_worker():
            return 0
        with connect(self.database_path) as conn:
            cursor = conn.execute(
                """
                UPDATE import_tasks
                SET status=CASE WHEN pause_requested=1 THEN 'paused' ELSE 'waiting' END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE status='processing'
                """
            )
        return cursor.rowcount

    def claim_next(self) -> ImportTask | None:
        if not self._owns_worker():
            return None
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id FROM import_tasks
                WHERE status='waiting'
                  AND pause_requested=0
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY id LIMIT 1
                """,
                (self._clock(),),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE import_tasks
                SET status='processing', current_stage='processing',
                    started_at=COALESCE(started_at, CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='waiting'
                """,
                (row["id"],),
            )
            task_id = int(row["id"])
        return self.get_task(task_id)

    def process_next(self) -> bool:
        task = self.claim_next()
        if task is None:
            return False
        try:
            self._process_file(task)
        except TaskPaused:
            with connect(self.database_path) as conn:
                conn.execute(
                    """
                    UPDATE import_tasks SET status='paused', pause_requested=0,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (task.id,),
                )
        except Exception as exc:
            self._record_failure(task, exc)
        else:
            with connect(self.database_path) as conn:
                conn.execute(
                    """
                    UPDATE import_tasks
                    SET status='completed', current_stage='completed', error=NULL,
                        failure_type=NULL, failed_stage=NULL, user_message=NULL,
                        next_attempt_at=NULL, pause_requested=0,
                        completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (task.id,),
                )
            if self._completion_callback is not None:
                try:
                    self._completion_callback(self.get_task(task.id))
                except Exception:
                    pass
        if self._task_settled_callback is not None:
            try:
                self._task_settled_callback()
            except Exception:
                pass
        return True

    def pause(self, task_id: int) -> ImportTask:
        with connect(self.database_path) as conn:
            row = conn.execute("SELECT status FROM import_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["status"] == PROCESSING:
                conn.execute(
                    "UPDATE import_tasks SET pause_requested=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (task_id,),
                )
            elif row["status"] == WAITING:
                conn.execute(
                    """
                    UPDATE import_tasks SET status='paused', pause_requested=0,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (task_id,),
                )
        return self.get_task(task_id)

    def resume(self, task_id: int) -> ImportTask:
        with connect(self.database_path) as conn:
            row = conn.execute("SELECT status FROM import_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["status"] in {PAUSED, NEEDS_ATTENTION}:
                conn.execute(
                    """
                    UPDATE import_tasks
                    SET status='waiting', pause_requested=0, retry_count=0,
                        next_attempt_at=NULL, failure_type=NULL, failed_stage=NULL,
                        user_message=NULL, error=NULL, completed_at=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (task_id,),
                )
        return self.get_task(task_id)

    def _record_failure(self, task: ImportTask, exc: Exception) -> None:
        task = self.get_task(task.id)
        failure = self._normalise_failure(task, exc)
        if failure.failure_type == "transient" and task.retry_count < len(self._retry_delays):
            retry_count = task.retry_count + 1
            next_attempt = self._clock() + self._retry_delays[task.retry_count]
            status = WAITING
        else:
            retry_count = task.retry_count
            next_attempt = None
            status = NEEDS_ATTENTION
        with connect(self.database_path) as conn:
            conn.execute(
                """
                UPDATE import_tasks SET status=?, failure_type=?, failed_stage=?,
                    user_message=?, error=NULL, retry_count=?, next_attempt_at=?,
                    completed_at=CASE WHEN ?='needs_attention' THEN CURRENT_TIMESTAMP ELSE NULL END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    status,
                    failure.failure_type,
                    failure.stage,
                    failure.user_message,
                    retry_count,
                    next_attempt,
                    status,
                    task.id,
                ),
            )

    @staticmethod
    def _normalise_failure(task: ImportTask, exc: Exception) -> ImportProcessingError:
        if isinstance(exc, ImportProcessingError):
            return exc
        if isinstance(exc, PermissionError):
            return TransientImportError(task.current_stage, "文件暂时被占用，请稍后重试。")
        if isinstance(exc, OSError):
            return TransientImportError(task.current_stage, "读取文件时遇到临时问题，系统将自动重试。")
        return DeterministicImportError(
            task.current_stage,
            "处理未能完成，请检查文件内容或设置后继续。",
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> ImportTask:
        data = dict(row)
        data["pause_requested"] = bool(data["pause_requested"])
        return ImportTask(**data)

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        if not self.acquire_worker():
            return False
        self.recover_interrupted()
        self._stop_event.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat,
            name="text-import-heartbeat",
            daemon=True,
        )
        self._thread = threading.Thread(target=self._run, name="text-import-worker", daemon=True)
        self._heartbeat_thread.start()
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self._poll_interval * 2))
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
        self.release_worker()

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                if not self.acquire_worker():
                    return
                if not self.process_next():
                    self._stop_event.wait(self._poll_interval)
        finally:
            self.release_worker()

    def _heartbeat(self) -> None:
        interval = max(0.1, self._lease_seconds / 3)
        while not self._stop_event.wait(interval):
            if not self.acquire_worker():
                self._stop_event.set()
                return

    def _owns_worker(self) -> bool:
        with connect(self.database_path) as conn:
            row = conn.execute(
                "SELECT owner_id, expires_at FROM worker_leases WHERE queue_name=?",
                (QUEUE_NAME,),
            ).fetchone()
        return bool(
            row
            and row["owner_id"] == self._owner_id
            and float(row["expires_at"]) > self._clock()
        )
