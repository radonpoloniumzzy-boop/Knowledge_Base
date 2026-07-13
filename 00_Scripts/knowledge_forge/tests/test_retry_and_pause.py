from __future__ import annotations

from knowledge_forge.db import init_db
from knowledge_forge.ingestion import (
    DeterministicImportError,
    PersistentTextImportQueue,
    TransientImportError,
)


class Clock:
    def __init__(self, value: float = 1_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


def make_queue(tmp_path, processor, clock=None):
    database_path = tmp_path / "knowledge.db"
    init_db(database_path, tmp_path / "managed")
    next_file_id = 0

    def accept(_filename, _data):
        nonlocal next_file_id
        next_file_id += 1
        return next_file_id

    return PersistentTextImportQueue(
        database_path,
        accept,
        processor,
        clock=clock or Clock(),
    )


def test_transient_failure_retries_on_persisted_schedule_then_needs_attention(tmp_path):
    clock = Clock()
    attempts = []

    def fail(task):
        attempts.append(task.id)
        raise TransientImportError("extract_text", "文件暂时被占用，请稍后重试。")

    queue = make_queue(tmp_path, fail, clock)
    task = queue.submit("locked.txt", b"content")
    assert queue.acquire_worker() is True

    queue.process_next()
    first = queue.get_task(task.id)
    assert first.status == "waiting"
    assert first.retry_count == 1
    assert first.next_attempt_at == 1_005.0
    assert queue.claim_next() is None

    queue.release_worker()
    queue = PersistentTextImportQueue(queue.database_path, lambda *_: 99, fail, clock=clock)
    persisted = queue.get_task(task.id)
    assert persisted.retry_count == 1
    assert persisted.next_attempt_at == 1_005.0

    for expected_retry, delay in ((2, 30), (3, 120)):
        clock.value = queue.get_task(task.id).next_attempt_at
        assert queue.acquire_worker() is True
        queue.process_next()
        current = queue.get_task(task.id)
        assert current.status == "waiting"
        assert current.retry_count == expected_retry
        assert current.next_attempt_at == clock.value + delay

    clock.value = queue.get_task(task.id).next_attempt_at
    assert queue.acquire_worker() is True
    queue.process_next()
    exhausted = queue.get_task(task.id)
    assert exhausted.status == "needs_attention"
    assert exhausted.failure_type == "transient"
    assert exhausted.failed_stage == "extract_text"
    assert exhausted.user_message == "文件暂时被占用，请稍后重试。"
    assert exhausted.retry_count == 3
    assert len(attempts) == 4
    queue.release_worker()


def test_deterministic_failure_stops_without_retry_or_raw_exception(tmp_path):
    def fail(_task):
        raise DeterministicImportError("quality_validation", "文档内容为空，请更换文件。")

    queue = make_queue(tmp_path, fail)
    task = queue.submit("empty.txt", b"")
    assert queue.acquire_worker() is True
    queue.process_next()

    failed = queue.get_task(task.id)
    assert failed.status == "needs_attention"
    assert failed.retry_count == 0
    assert failed.next_attempt_at is None
    assert failed.failure_type == "deterministic"
    assert failed.failed_stage == "quality_validation"
    assert failed.user_message == "文档内容为空，请更换文件。"
    assert "DeterministicImportError" not in failed.user_message
    queue.release_worker()


def test_waiting_task_pauses_immediately_and_resume_resets_failure_state(tmp_path):
    processed = []
    queue = make_queue(tmp_path, lambda task: processed.append(task.id))
    task = queue.submit("pause.txt", b"content")
    assert queue.acquire_worker() is True

    paused = queue.pause(task.id)
    assert paused.status == "paused"
    assert queue.claim_next() is None
    assert processed == []

    resumed = queue.resume(task.id)
    assert resumed.status == "waiting"
    assert resumed.retry_count == 0
    assert resumed.pause_requested is False
    assert queue.process_next() is True
    assert queue.get_task(task.id).status == "completed"
    queue.release_worker()
