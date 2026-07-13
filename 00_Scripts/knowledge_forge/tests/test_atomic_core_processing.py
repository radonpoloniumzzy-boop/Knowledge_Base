from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_forge.core_processing import CORE_STAGES, CoreTextProcessor
from knowledge_forge.db import connect, init_db
from knowledge_forge.ingestion import PersistentTextImportQueue


def make_system(tmp_path, hook=None):
    database_path = tmp_path / "knowledge.db"
    standard_dir = tmp_path / "standard"
    upload_dir = tmp_path / "uploads"
    init_db(database_path, tmp_path / "managed")

    def accept(filename: str, data: bytes) -> int:
        upload_dir.mkdir(exist_ok=True)
        source = upload_dir / filename
        source.write_bytes(data)
        with connect(database_path) as conn:
            return int(
                conn.execute(
                    """
                    INSERT INTO files(source_path, library_type, title, filename, extension, status)
                    VALUES (?, 'upload', ?, ?, ?, 'processing')
                    """,
                    (str(source), source.stem, filename, source.suffix),
                ).lastrowid
            )

    processor = CoreTextProcessor(database_path, standard_dir, stage_hook=hook)
    queue = PersistentTextImportQueue(database_path, accept, processor.process)
    return database_path, standard_dir, processor, queue


def completed_stages(database_path: Path, task_id: int) -> list[str]:
    with connect(database_path) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT stage_name FROM import_stage_results WHERE task_id=? ORDER BY id",
                (task_id,),
            )
        ]


def test_core_stages_commit_in_order_and_promote_available_unreviewed_version(tmp_path):
    database_path, standard_dir, _, queue = make_system(tmp_path)
    task = queue.submit("lesson.txt", b"First idea.\n\nSecond idea.")

    assert queue.acquire_worker() is True
    assert queue.process_next() is True

    assert completed_stages(database_path, task.id) == list(CORE_STAGES)
    with connect(database_path) as conn:
        version = conn.execute(
            "SELECT * FROM source_versions WHERE id=(SELECT version_id FROM import_tasks WHERE id=?)",
            (task.id,),
        ).fetchone()
        source = conn.execute(
            "SELECT * FROM knowledge_sources WHERE id=?", (version["source_id"],)
        ).fetchone()
        chunks = conn.execute(
            "SELECT text FROM chunks WHERE file_id=? ORDER BY chunk_index",
            (version["standard_file_id"],),
        ).fetchall()
    assert version["status"] == "available"
    assert version["review_status"] == "unreviewed"
    assert source["current_version_id"] == version["id"]
    assert [row["text"] for row in chunks]
    assert Path(version["standard_path"]).is_file()
    assert Path(version["standard_path"]).parent == standard_dir / "00_Pending_Review"
    queue.release_worker()


def test_interrupted_chunk_stage_exposes_no_document_or_partial_chunks(tmp_path):
    def interrupt(stage, moment):
        if stage == "chunk_indexing" and moment == "before_commit":
            raise RuntimeError("simulated crash")

    database_path, _, _, queue = make_system(tmp_path, interrupt)
    task = queue.submit("crash.md", b"# Heading\n\nUseful content")

    assert queue.acquire_worker() is True
    queue.process_next()

    with connect(database_path) as conn:
        version = conn.execute(
            "SELECT * FROM source_versions WHERE id=(SELECT version_id FROM import_tasks WHERE id=?)",
            (task.id,),
        ).fetchone()
        visible_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        staged_chunks = conn.execute("SELECT COUNT(*) FROM staged_chunks").fetchone()[0]
    assert version["standard_file_id"] is None
    assert visible_chunks == 0
    assert staged_chunks == 0
    assert completed_stages(database_path, task.id) == list(CORE_STAGES[:3])
    queue.release_worker()


def test_resume_skips_committed_stages_and_repeated_run_is_idempotent(tmp_path):
    calls = []

    def observe(stage, moment):
        if moment == "before_commit":
            calls.append(stage)
            if stage == "quality_validation" and calls.count(stage) == 1:
                raise RuntimeError("stop once")

    database_path, _, processor, queue = make_system(tmp_path, observe)
    task = queue.submit("resume.txt", b"Resume without repeating extraction.")
    queued_task = queue.get_task(task.id)

    with pytest.raises(RuntimeError, match="stop once"):
        processor.process(queued_task)
    assert completed_stages(database_path, task.id) == list(CORE_STAGES[:2])

    processor.process(queue.get_task(task.id))
    processor.process(queue.get_task(task.id))

    assert calls.count("extract_text") == 1
    assert calls.count("standard_document") == 1
    with connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM files WHERE library_type='standard'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM import_stage_results WHERE task_id=?", (task.id,)).fetchone()[0] == len(CORE_STAGES)


def test_failed_promotion_preserves_existing_current_version(tmp_path):
    def fail_promotion(stage, moment):
        if stage == "promote_version" and moment == "before_commit":
            raise RuntimeError("promotion unavailable")

    database_path, _, processor, queue = make_system(tmp_path, fail_promotion)
    task = queue.submit("replacement.txt", b"New candidate content")
    with connect(database_path) as conn:
        source_id = conn.execute(
            "INSERT INTO knowledge_sources(source_file_id) VALUES (?)", (999,)
        ).lastrowid
        old_version = conn.execute(
            "INSERT INTO source_versions(source_id, upload_file_id, status, review_status) VALUES (?, 998, 'available', 'reviewed')",
            (source_id,),
        ).lastrowid
        conn.execute(
            "UPDATE knowledge_sources SET current_version_id=? WHERE id=?",
            (old_version, source_id),
        )
        conn.execute(
            "UPDATE import_tasks SET source_id=? WHERE id=?", (source_id, task.id)
        )

    with pytest.raises(RuntimeError, match="promotion unavailable"):
        processor.process(queue.get_task(task.id))

    with connect(database_path) as conn:
        current = conn.execute(
            "SELECT current_version_id FROM knowledge_sources WHERE id=?", (source_id,)
        ).fetchone()[0]
    assert current == old_version
