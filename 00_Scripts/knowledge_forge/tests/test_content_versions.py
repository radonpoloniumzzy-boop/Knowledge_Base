from __future__ import annotations

import hashlib
import json
from pathlib import Path

from knowledge_forge.core_processing import CoreTextProcessor
from knowledge_forge.db import connect, init_db
from knowledge_forge.ingestion import DeterministicImportError, PersistentTextImportQueue, RecycledDuplicateError
from knowledge_forge.recycle_bin import KnowledgeRecycleBin
import pytest
from knowledge_forge import services


def make_versioned_system(tmp_path, processor_factory=None):
    database_path = tmp_path / "knowledge.db"
    upload_dir = tmp_path / "uploads"
    standard_dir = tmp_path / "standard"
    init_db(database_path, tmp_path / "managed")
    accepted = []

    def accept(filename, data):
        upload_dir.mkdir(exist_ok=True)
        path = upload_dir / f"{len(accepted) + 1}-{filename}"
        path.write_bytes(data)
        with connect(database_path) as conn:
            file_id = conn.execute(
                """
                INSERT INTO files(source_path, library_type, title, filename, extension, status)
                VALUES (?, 'upload', ?, ?, ?, 'processing')
                """,
                (str(path), Path(filename).stem, filename, Path(filename).suffix),
            ).lastrowid
        accepted.append(file_id)
        return int(file_id)

    processor = (
        processor_factory(database_path, standard_dir)
        if processor_factory
        else CoreTextProcessor(database_path, standard_dir)
    )
    queue = PersistentTextImportQueue(database_path, accept, processor.process)
    return database_path, accepted, queue


def process_one(queue):
    assert queue.acquire_worker() is True
    assert queue.process_next() is True
    queue.release_worker()


def test_exact_content_reuses_existing_version_and_task_without_second_upload(tmp_path):
    database_path, accepted, queue = make_versioned_system(tmp_path)
    content = b"Same lesson content"
    first = queue.submit("lesson.txt", content)
    process_one(queue)

    duplicate = queue.submit("renamed-copy.txt", content)

    assert duplicate.id == first.id
    assert accepted == [first.file_id]
    with connect(database_path) as conn:
        version = conn.execute("SELECT * FROM source_versions").fetchone()
        assert version["content_fingerprint"] == hashlib.sha256(content).hexdigest()
        assert conn.execute("SELECT COUNT(*) FROM source_versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM import_tasks").fetchone()[0] == 1
    assert queue.knowledge_entry_for_task(duplicate.id) is not None


def test_same_name_changed_content_creates_new_version_and_promotes_only_after_success(tmp_path, monkeypatch):
    database_path, accepted, queue = make_versioned_system(tmp_path)
    first = queue.submit("course.md", b"Version one")
    process_one(queue)
    first_entry = queue.knowledge_entry_for_task(first.id)

    second = queue.submit("COURSE.md", b"Version two")
    with connect(database_path) as conn:
        before = conn.execute(
            "SELECT current_version_id FROM knowledge_sources WHERE id=?", (second.source_id,)
        ).fetchone()[0]
        versions = conn.execute(
            "SELECT version_number FROM source_versions WHERE source_id=? ORDER BY version_number",
            (second.source_id,),
        ).fetchall()
    assert first.id != second.id
    assert first.source_id == second.source_id
    assert [row[0] for row in versions] == [1, 2]
    assert before == first.version_id
    assert queue.knowledge_entry_for_task(first.id) == first_entry
    monkeypatch.setattr(services, "connect", lambda: connect(database_path))
    assert first_entry in [row["id"] for row in services.list_files(limit=100)]
    with connect(database_path) as conn:
        tag_id = conn.execute("INSERT INTO tags(name) VALUES ('Versioned')").lastrowid
        conn.execute(
            """
            INSERT INTO tag_assignments(
                target_type, target_id, tag_id, scope, confidence, status, source
            ) VALUES ('file', ?, ?, 'file_strong', 1.0, 'user_approved', 'human')
            """,
            (first_entry, tag_id),
        )
        pack_id = conn.execute(
            """
            INSERT INTO packs(name, recipe_json)
            VALUES ('Version test', ?)
            """,
            (json.dumps({"include_tags": ["Versioned"], "min_confidence": 0.7}),),
        ).lastrowid
    assert [row["id"] for row in services.files_for_pack(pack_id)] == [first_entry]

    process_one(queue)
    with connect(database_path) as conn:
        after = conn.execute(
            "SELECT current_version_id FROM knowledge_sources WHERE id=?", (second.source_id,)
        ).fetchone()[0]
    assert after == second.version_id
    current_entry = queue.knowledge_entry_for_task(second.id)
    visible_ids = [row["id"] for row in services.list_files(limit=100)]
    assert current_entry in visible_ids
    assert first_entry not in visible_ids
    assert first_entry not in [row["id"] for row in services.files_for_pack(pack_id)]
    assert len(accepted) == 2


def test_failed_new_version_keeps_old_current_and_history_identifies_states(tmp_path):
    database_path, _, queue = make_versioned_system(tmp_path)
    first = queue.submit("policy.txt", b"Approved version")
    process_one(queue)
    second = queue.submit("policy.txt", b"")
    process_one(queue)

    with connect(database_path) as conn:
        current = conn.execute(
            "SELECT current_version_id FROM knowledge_sources WHERE id=?", (first.source_id,)
        ).fetchone()[0]
    history = queue.version_history_for_file(queue.knowledge_entry_for_task(first.id))
    assert current == first.version_id
    assert [(item["version_number"], item["is_current"], item["status"]) for item in history] == [
        (2, False, "processing"),
        (1, True, "available"),
    ]
    assert queue.get_task(second.id).status == "needs_attention"


def test_exact_duplicate_in_recycle_bin_requires_explicit_restore(tmp_path):
    database_path, _, queue = make_versioned_system(tmp_path)
    content = b"Archived lesson"
    first = queue.submit("lesson.txt", content)
    process_one(queue)
    recycle_bin = KnowledgeRecycleBin(database_path, (tmp_path,))
    recycle_bin.recycle(first.source_id)

    with pytest.raises(RecycledDuplicateError) as error:
        queue.submit("copy.txt", content)

    assert error.value.source_id == first.source_id
