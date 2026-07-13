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
        self.version_history = []

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

    def version_history_for_file(self, _file_id):
        return self.version_history


class FakeEnhancementQueue:
    def __init__(self):
        self.jobs = []
        self.regenerated = []

    def list_jobs(self, _version_id):
        return self.jobs

    def regenerate(self, version_id, kind):
        self.regenerated.append((version_id, kind))


class FakeRecycleBin:
    def __init__(self):
        self.recycled = []
        self.restored = []

    def source_id_for_file(self, file_id):
        return 81 if file_id == 7 else None

    def recycle(self, source_id):
        self.recycled.append(source_id)

    def restore(self, source_id):
        self.restored.append(source_id)

    def finalize_pending(self):
        return 0

    def purge_expired(self):
        return 0

    def list_recycled(self):
        return []


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


def test_file_detail_displays_nonblocking_quality_warning(monkeypatch):
    queue = FakeQueue()
    queue.version_history = [
        {
            "version_number": 1,
            "original_filename": "small.xlsx",
            "is_current": True,
            "status": "available",
            "task_status": "completed",
            "standard_file_id": 7,
            "quality_warnings": ["内容较短，请确认提取结果是否完整。"],
            "extraction_metadata": {"source_format": "xlsx", "character_count": 7},
        }
    ]
    monkeypatch.setattr(app_module, "ingestion_queue", queue)
    monkeypatch.setattr(app_module, "enhancement_queue", FakeEnhancementQueue())
    monkeypatch.setattr(
        app_module.services,
        "file_detail",
        lambda _file_id: {
            "file": {
                "id": 7,
                "title": "Small sheet",
                "source_path": "small.xlsx",
                "library_type": "standard",
                "filename": "small.xlsx",
                "main_category": None,
                "sub_category": None,
                "size_bytes": 100,
                "status": "completed",
            },
            "tags": [],
            "artifacts": [],
            "chunks": [],
        },
    )
    client = TestClient(app_module.app)

    response = client.get("/files/7")

    assert response.status_code == 200
    assert "内容较短，请确认提取结果是否完整。" in response.text
    assert "XLSX" in response.text
    assert "7 字符" in response.text


def test_targeted_enhancement_regeneration_only_resets_requested_kind(monkeypatch):
    queue = FakeQueue()
    queue.version_history = [{"id": 31, "is_current": True}]
    enhancements = FakeEnhancementQueue()
    monkeypatch.setattr(app_module, "ingestion_queue", queue)
    monkeypatch.setattr(app_module, "enhancement_queue", enhancements)
    client = TestClient(app_module.app)

    response = client.post(
        "/files/7/enhancements/insight/regenerate", follow_redirects=False
    )

    assert response.status_code == 303
    assert enhancements.regenerated == [(31, "insight")]


def test_http_recycle_and_restore_use_recycle_bin_interface(monkeypatch):
    recycle_bin = FakeRecycleBin()
    monkeypatch.setattr(app_module, "recycle_bin", recycle_bin)
    client = TestClient(app_module.app)

    recycle = client.post("/files/7/recycle", follow_redirects=False)
    restore = client.post("/recycle-bin/81/restore", follow_redirects=False)
    page = client.get("/recycle-bin")

    assert recycle.status_code == 303
    assert recycle.headers["location"] == "/recycle-bin"
    assert restore.status_code == 303
    assert recycle_bin.recycled == [81]
    assert recycle_bin.restored == [81]
    assert page.status_code == 200
    assert "回收站为空" in page.text
