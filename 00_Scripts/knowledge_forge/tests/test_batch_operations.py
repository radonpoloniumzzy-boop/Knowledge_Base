from pathlib import Path

from knowledge_forge import services
from knowledge_forge.batch_operations import BatchOperationQueue
from knowledge_forge.db import connect, init_db
import pytest


class FakeQueue:
    def reprocess(self, file_id):
        return type("Task", (), {"id": file_id + 100})()


class FakeEnhancements:
    def __init__(self): self.calls = []
    def regenerate(self, version_id, kind): self.calls.append((version_id, kind))


class FakeRecycle:
    def __init__(self): self.recycled = []; self.restored = []
    def recycle(self, source_id): self.recycled.append(source_id)
    def restore(self, source_id): self.restored.append(source_id)


def make_batch_system(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    init_db(database_path, tmp_path / "managed")
    monkeypatch.setattr(services, "connect", lambda: connect(database_path))
    with connect(database_path) as conn:
        file_id = conn.execute("INSERT INTO files(source_path,library_type,title,filename,status) VALUES ('x.md','standard','X','x.md','completed')").lastrowid
        source_id = conn.execute("INSERT INTO knowledge_sources(source_file_id,canonical_name) VALUES (?,'x.md')", (file_id,)).lastrowid
        version_id = conn.execute("INSERT INTO source_versions(source_id,upload_file_id,standard_file_id,status,version_number) VALUES (?,?,?,'available',1)", (source_id,file_id,file_id)).lastrowid
        conn.execute("UPDATE knowledge_sources SET current_version_id=? WHERE id=?", (version_id, source_id))
    recycle = FakeRecycle(); enhancements = FakeEnhancements()
    queue = BatchOperationQueue(database_path, FakeQueue(), enhancements, recycle)
    return database_path, int(file_id), int(source_id), queue, enhancements, recycle


def test_batch_job_persists_snapshot_and_processes_tag_operation(tmp_path, monkeypatch):
    database_path, file_id, _, queue, _, _ = make_batch_system(tmp_path, monkeypatch)
    job = queue.create("add_tag", [file_id], {"tag": "08_Art/Color"}, {"scope": "page"})
    assert queue.process_next() is True
    assert queue.get(job.id).status == "completed"
    with connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tag_assignments WHERE target_id=?", (file_id,)).fetchone()[0] == 1


def test_batch_recycle_and_resume_failed_items_are_independent(tmp_path, monkeypatch):
    _, file_id, source_id, queue, _, recycle = make_batch_system(tmp_path, monkeypatch)
    job = queue.create("recycle", [file_id])
    queue.process_next()
    assert recycle.recycled == [source_id]
    assert queue.get(job.id).completed_count == 1


def test_waiting_batch_can_pause_and_resume_without_losing_snapshot(tmp_path, monkeypatch):
    _, file_id, _, queue, _, _ = make_batch_system(tmp_path, monkeypatch)
    job = queue.create("set_review", [file_id], {"review_status": "reviewed"})
    queue.pause(job.id)
    assert queue.get(job.id).status == "paused"
    assert queue.process_next() is False
    queue.resume(job.id)
    assert queue.process_next() is True
    assert queue.get(job.id).status == "completed"
    with pytest.raises(KeyError):
        queue.pause(9999)
