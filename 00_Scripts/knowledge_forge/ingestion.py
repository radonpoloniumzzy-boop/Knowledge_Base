from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from uuid import uuid4

from .db import connect


WAITING = "waiting"
PROCESSING = "processing"
COMPLETED = "completed"
NEEDS_ATTENTION = "needs_attention"
QUEUE_NAME = "text-import"


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
    ) -> None:
        self.database_path = Path(database_path)
        self._accept_upload = accept_upload
        self._process_file = process_file
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds
        self._owner_id = uuid4().hex
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None

    def submit(self, filename: str, data: bytes) -> ImportTask:
        file_id = self._accept_upload(filename, data)
        with connect(self.database_path) as conn:
            task_id = conn.execute(
                """
                INSERT INTO import_tasks(file_id, filename, status, current_stage)
                VALUES (?, ?, 'waiting', 'queued')
                """,
                (file_id, filename),
            ).lastrowid
        return self.get_task(int(task_id))

    def submit_many(self, uploads: Iterable[tuple[str, bytes]]) -> list[ImportTask]:
        return [self.submit(filename, data) for filename, data in uploads]

    def get_task(self, task_id: int) -> ImportTask:
        with connect(self.database_path) as conn:
            row = conn.execute("SELECT * FROM import_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return ImportTask(**dict(row))

    def list_tasks(self, limit: int = 50) -> list[ImportTask]:
        with connect(self.database_path) as conn:
            rows = conn.execute(
                "SELECT * FROM import_tasks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [ImportTask(**dict(row)) for row in rows]

    def summary(self) -> dict[str, int]:
        counts = {status: 0 for status in (WAITING, PROCESSING, COMPLETED, NEEDS_ATTENTION)}
        with connect(self.database_path) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM import_tasks GROUP BY status"
            ).fetchall()
        for row in rows:
            if row["status"] in counts:
                counts[row["status"]] = int(row["count"])
        return counts

    def acquire_worker(self) -> bool:
        now = time.time()
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
                SET status='waiting', current_stage='queued', updated_at=CURRENT_TIMESTAMP
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
                "SELECT id FROM import_tasks WHERE status='waiting' ORDER BY id LIMIT 1"
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
        except Exception as exc:
            with connect(self.database_path) as conn:
                conn.execute(
                    """
                    UPDATE import_tasks
                    SET status='needs_attention', current_stage='failed', error=?,
                        completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (str(exc), task.id),
                )
        else:
            with connect(self.database_path) as conn:
                conn.execute(
                    """
                    UPDATE import_tasks
                    SET status='completed', current_stage='completed', error=NULL,
                        completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (task.id,),
                )
        return True

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
            and float(row["expires_at"]) > time.time()
        )
