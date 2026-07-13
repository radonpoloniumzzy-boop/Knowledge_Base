from __future__ import annotations

import time

from knowledge_forge.db import init_db
from knowledge_forge.ingestion import PersistentTextImportQueue


def make_queue(tmp_path, accepted, processed, **queue_options):
    database_path = tmp_path / "knowledge.db"
    init_db(database_path, tmp_path / "managed")

    def accept(filename: str, data: bytes) -> int:
        accepted.append((filename, data))
        return len(accepted)

    def process(file_id: int) -> None:
        processed.append(file_id)

    return PersistentTextImportQueue(database_path, accept, process, **queue_options)


def test_bulk_submit_creates_one_durable_waiting_task_per_file(tmp_path):
    accepted = []
    queue = make_queue(tmp_path, accepted, [])

    submitted = queue.submit_many(
        [("lesson-a.txt", b"alpha"), ("lesson-b.md", b"beta")]
    )

    reopened = PersistentTextImportQueue(queue.database_path, lambda *_: 99, lambda _: None)
    tasks = reopened.list_tasks()
    assert [task.filename for task in submitted] == ["lesson-a.txt", "lesson-b.md"]
    assert [task.status for task in tasks] == ["waiting", "waiting"]
    assert reopened.summary() == {
        "waiting": 2,
        "processing": 0,
        "completed": 0,
        "needs_attention": 0,
    }
    assert accepted == [("lesson-a.txt", b"alpha"), ("lesson-b.md", b"beta")]


def test_only_one_queue_instance_can_own_and_process_work(tmp_path):
    processed = []
    first = make_queue(tmp_path, [], processed)
    first.submit_many([("one.txt", b"1"), ("two.txt", b"2")])
    second = PersistentTextImportQueue(first.database_path, lambda *_: 3, processed.append)

    assert first.acquire_worker() is True
    assert second.acquire_worker() is False
    assert first.process_next() is True
    assert first.summary() == {
        "waiting": 1,
        "processing": 0,
        "completed": 1,
        "needs_attention": 0,
    }
    assert processed == [1]
    first.release_worker()


def test_restart_recovers_interrupted_task_and_continues_from_queue(tmp_path):
    processed = []
    original = make_queue(tmp_path, [], processed)
    task = original.submit("restart.txt", b"resume me")
    assert original.acquire_worker() is True
    assert original.claim_next().id == task.id
    original.release_worker()

    restarted = PersistentTextImportQueue(
        original.database_path,
        lambda *_: 7,
        processed.append,
    )
    assert restarted.acquire_worker() is True
    assert restarted.recover_interrupted() == 1
    assert restarted.process_next() is True

    current = restarted.get_task(task.id)
    assert current.status == "completed"
    assert current.started_at is not None
    assert current.completed_at is not None
    assert processed == [1]
    restarted.release_worker()


def test_processor_failure_is_persisted_as_needs_attention(tmp_path):
    queue = make_queue(tmp_path, [], [])
    task = queue.submit("broken.txt", b"bad")
    failing = PersistentTextImportQueue(
        queue.database_path,
        lambda *_: 2,
        lambda _: (_ for _ in ()).throw(ValueError("cannot read")),
    )

    assert failing.acquire_worker() is True
    assert failing.process_next() is True
    failed = failing.get_task(task.id)
    assert failed.status == "needs_attention"
    assert failed.current_stage == "failed"
    assert failed.error == "cannot read"
    failing.release_worker()


def test_running_worker_renews_lease_during_long_processing_window(tmp_path):
    first = make_queue(tmp_path, [], [], lease_seconds=0.15)
    competing = PersistentTextImportQueue(
        first.database_path,
        lambda *_: 2,
        lambda _: None,
        lease_seconds=0.15,
    )

    assert first.start() is True
    time.sleep(0.35)
    assert competing.acquire_worker() is False
    first.stop()
