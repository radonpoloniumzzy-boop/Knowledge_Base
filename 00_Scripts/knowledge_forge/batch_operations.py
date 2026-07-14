from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import services
from .db import connect


BATCH_OPERATIONS = {
    "add_tag", "remove_tag", "set_category", "set_review",
    "regenerate", "reprocess", "recycle", "restore",
}


@dataclass(frozen=True)
class BatchJob:
    id: int
    operation: str
    status: str
    total_count: int
    completed_count: int
    failed_count: int
    message: str | None
    pause_requested: bool


class BatchOperationQueue:
    """Persistent coordinator that dispatches one knowledge operation at a time."""

    def __init__(self, database_path: Path, ingestion_queue, enhancement_queue, recycle_bin) -> None:
        self.database_path = Path(database_path)
        self.ingestion_queue = ingestion_queue
        self.enhancement_queue = enhancement_queue
        self.recycle_bin = recycle_bin
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def create(
        self,
        operation: str,
        file_ids: Iterable[int],
        parameters: dict | None = None,
        selection: dict | None = None,
    ) -> BatchJob:
        if operation not in BATCH_OPERATIONS:
            raise ValueError(operation)
        ids = list(dict.fromkeys(int(item) for item in file_ids))
        if not ids:
            raise ValueError("请至少选择一份资料。")
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_id = int(conn.execute(
                "INSERT INTO batch_jobs(operation, parameters_json, selection_json, total_count) VALUES (?, ?, ?, ?)",
                (operation, json.dumps(parameters or {}, ensure_ascii=False), json.dumps(selection or {}, ensure_ascii=False), len(ids)),
            ).lastrowid)
            for file_id in ids:
                source = conn.execute(
                    """
                    SELECT ks.id FROM knowledge_sources ks
                    JOIN source_versions sv ON sv.source_id=ks.id
                    WHERE sv.standard_file_id=? OR sv.upload_file_id=?
                    ORDER BY CASE WHEN ks.current_version_id=sv.id THEN 0 ELSE 1 END LIMIT 1
                    """,
                    (file_id, file_id),
                ).fetchone()
                conn.execute(
                    "INSERT INTO batch_job_items(batch_job_id, file_id, source_id) VALUES (?, ?, ?)",
                    (job_id, file_id, int(source["id"]) if source else None),
                )
        return self.get(job_id)

    def get(self, job_id: int) -> BatchJob:
        with connect(self.database_path) as conn:
            row = conn.execute("SELECT * FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        values = dict(row)
        return BatchJob(**{key: values[key] for key in BatchJob.__dataclass_fields__})

    def list_jobs(self, limit: int = 20) -> list[dict]:
        try:
            with connect(self.database_path) as conn:
                rows = conn.execute("SELECT * FROM batch_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]

    def process_next(self) -> bool:
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM batch_jobs WHERE status IN ('waiting','processing') AND pause_requested=0 ORDER BY id LIMIT 1"
            ).fetchone()
            if job is None:
                return False
            item = conn.execute(
                "SELECT * FROM batch_job_items WHERE batch_job_id=? AND status='waiting' ORDER BY id LIMIT 1",
                (job["id"],),
            ).fetchone()
            if item is None:
                self._finish_job(conn, int(job["id"]))
                return True
            conn.execute("UPDATE batch_jobs SET status='processing', current_item_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (item["id"], job["id"]))
            conn.execute("UPDATE batch_job_items SET status='processing', updated_at=CURRENT_TIMESTAMP WHERE id=?", (item["id"],))
        try:
            linked_task = self._execute(dict(job), dict(item))
        except Exception:
            with connect(self.database_path) as conn:
                conn.execute("UPDATE batch_job_items SET status='failed', message='该资料处理失败，可在资料详情中单独重试。', updated_at=CURRENT_TIMESTAMP WHERE id=?", (item["id"],))
                conn.execute("UPDATE batch_jobs SET failed_count=failed_count+1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (job["id"],))
        else:
            with connect(self.database_path) as conn:
                conn.execute("UPDATE batch_job_items SET status='completed', linked_task_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (linked_task, item["id"]))
                conn.execute("UPDATE batch_jobs SET completed_count=completed_count+1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (job["id"],))
        with connect(self.database_path) as conn:
            paused = conn.execute("SELECT pause_requested FROM batch_jobs WHERE id=?", (job["id"],)).fetchone()[0]
            remaining = conn.execute("SELECT 1 FROM batch_job_items WHERE batch_job_id=? AND status='waiting'", (job["id"],)).fetchone()
            if paused:
                conn.execute("UPDATE batch_jobs SET status='paused', current_item_id=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (job["id"],))
            elif not remaining:
                self._finish_job(conn, int(job["id"]))
        return True

    def _execute(self, job: dict, item: dict) -> int | None:
        operation = job["operation"]
        params = json.loads(job["parameters_json"] or "{}")
        file_id = int(item["file_id"])
        if operation in {"add_tag", "remove_tag"}:
            services.update_file_tag(file_id, params["tag"], "add" if operation == "add_tag" else "reject")
        elif operation == "set_category":
            with connect(self.database_path) as conn:
                row = conn.execute("SELECT title FROM files WHERE id=?", (file_id,)).fetchone()
            services.update_file_metadata(file_id, row["title"], params.get("main_category", ""), params.get("sub_category", ""))
        elif operation == "set_review":
            with connect(self.database_path) as conn:
                conn.execute("UPDATE files SET review_status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (params.get("review_status", "unreviewed"), file_id))
        elif operation == "regenerate":
            version_id = self._current_version(item["source_id"])
            for kind in params.get("kinds", ["structure", "sop", "insight"]):
                self.enhancement_queue.regenerate(version_id, kind)
        elif operation == "reprocess":
            return int(self.ingestion_queue.reprocess(file_id).id)
        elif operation == "recycle":
            self.recycle_bin.recycle(int(item["source_id"]))
        elif operation == "restore":
            self.recycle_bin.restore(int(item["source_id"]))
        return None

    def _current_version(self, source_id: int | None) -> int:
        with connect(self.database_path) as conn:
            row = conn.execute("SELECT current_version_id FROM knowledge_sources WHERE id=?", (source_id,)).fetchone()
        if row is None or row["current_version_id"] is None:
            raise ValueError("资料没有当前版本")
        return int(row["current_version_id"])

    @staticmethod
    def _finish_job(conn, job_id: int) -> None:
        row = conn.execute("SELECT failed_count, pause_requested FROM batch_jobs WHERE id=?", (job_id,)).fetchone()
        status = "paused" if row["pause_requested"] else ("needs_attention" if row["failed_count"] else "completed")
        conn.execute("UPDATE batch_jobs SET status=?, current_item_id=NULL, completed_at=CASE WHEN ? IN ('completed','needs_attention') THEN CURRENT_TIMESTAMP ELSE NULL END, updated_at=CURRENT_TIMESTAMP WHERE id=?", (status, status, job_id))

    def pause(self, job_id: int) -> None:
        with connect(self.database_path) as conn:
            cursor = conn.execute("UPDATE batch_jobs SET pause_requested=1, status=CASE WHEN status='waiting' THEN 'paused' ELSE status END WHERE id=?", (job_id,))
        if cursor.rowcount == 0:
            raise KeyError(job_id)

    def resume(self, job_id: int) -> None:
        with connect(self.database_path) as conn:
            conn.execute("UPDATE batch_job_items SET status='waiting', message=NULL WHERE batch_job_id=? AND status='failed'", (job_id,))
            cursor = conn.execute("UPDATE batch_jobs SET status='waiting', pause_requested=0, failed_count=0, completed_at=NULL WHERE id=?", (job_id,))
        if cursor.rowcount == 0:
            raise KeyError(job_id)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with connect(self.database_path) as conn:
            conn.execute("UPDATE batch_job_items SET status='waiting' WHERE status='processing'")
            conn.execute("UPDATE batch_jobs SET status='waiting', current_item_id=NULL WHERE status='processing'")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="knowledge-batch-coordinator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.process_next():
                self._stop.wait(0.5)
