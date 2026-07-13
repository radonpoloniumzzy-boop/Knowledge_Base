from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from knowledge_forge import app as app_module


class FakeQueue:
    def __init__(self):
        self.paused = []
        self.resumed = []
        self.tasks = []
        self.submitted = []
        self.entry_id = None

    def pause(self, task_id):
        self.paused.append(task_id)

    def resume(self, task_id):
        self.resumed.append(task_id)

    def list_tasks(self):
        return self.tasks

    def summary(self):
        return {
            "waiting": 0,
            "processing": 0,
            "paused": 0,
            "completed": 0,
            "needs_attention": len(self.tasks),
        }

    def submit_many(self, uploads):
        self.submitted.extend(uploads)
        return [SimpleNamespace(id=23)]

    def knowledge_entry_for_task(self, _task_id):
        return self.entry_id


def test_http_pause_and_resume_use_ingestion_queue_interface(monkeypatch):
    queue = FakeQueue()
    monkeypatch.setattr(app_module, "ingestion_queue", queue)
    client = TestClient(app_module.app)

    pause = client.post("/ingest/tasks/17/pause", follow_redirects=False)
    resume = client.post("/ingest/tasks/17/resume", follow_redirects=False)

    assert pause.status_code == 303
    assert resume.status_code == 303
    assert queue.paused == [17]
    assert queue.resumed == [17]


def test_job_fragment_shows_actionable_message_without_raw_exception(monkeypatch):
    queue = FakeQueue()
    queue.tasks = [
        SimpleNamespace(
            id=4,
            filename="lesson.txt",
            status="needs_attention",
            current_stage="quality_validation",
            user_message="文档内容为空，请更换文件。",
            failed_stage="quality_validation",
            retry_count=0,
            next_attempt_at=None,
            pause_requested=False,
            error="ValueError: raw internal details",
        )
    ]
    monkeypatch.setattr(app_module, "ingestion_queue", queue)
    client = TestClient(app_module.app)

    response = client.get("/ingest/jobs")

    assert response.status_code == 200
    assert "文档内容为空，请更换文件。" in response.text
    assert "ValueError" not in response.text
    assert "继续" in response.text


def test_single_duplicate_upload_redirects_to_existing_knowledge_entry(monkeypatch):
    queue = FakeQueue()
    queue.entry_id = 42
    monkeypatch.setattr(app_module, "ingestion_queue", queue)
    client = TestClient(app_module.app)

    response = client.post(
        "/ingest/upload",
        files={"files": ("copy.txt", b"same content", "text/plain")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/files/42"
    assert queue.submitted == [("copy.txt", b"same content")]
