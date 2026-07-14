from types import SimpleNamespace

from fastapi.testclient import TestClient

from knowledge_forge import app as app_module


class FakeBatchQueue:
    def __init__(self): self.created = []
    def create(self, operation, file_ids, parameters=None, selection=None):
        self.created.append((operation, list(file_ids), parameters or {}, selection or {}))
        return SimpleNamespace(id=1)
    def list_jobs(self): return []


def test_library_batch_can_snapshot_all_filtered_results(monkeypatch):
    batch = FakeBatchQueue()
    monkeypatch.setattr(app_module, "batch_queue", batch)
    monkeypatch.setattr(app_module.services, "library_file_ids", lambda *args: [11, 12, 13])
    client = TestClient(app_module.app)

    response = client.post("/library/batch", data={
        "operation": "regenerate", "all_filtered": "true", "category": "08_Art",
        "kinds": ["sop", "insight"],
    }, follow_redirects=False)

    assert response.status_code == 303
    assert batch.created[0][0] == "regenerate"
    assert batch.created[0][1] == [11, 12, 13]
    assert batch.created[0][2]["kinds"] == ["sop", "insight"]
    assert batch.created[0][3]["all_filtered"] is True


def test_recycle_bin_batch_restore_only_uses_current_recycled_files(monkeypatch):
    batch = FakeBatchQueue()
    monkeypatch.setattr(app_module, "batch_queue", batch)
    monkeypatch.setattr(
        app_module.recycle_bin,
        "list_recycled",
        lambda: [SimpleNamespace(file_id=21), SimpleNamespace(file_id=22)],
    )
    client = TestClient(app_module.app)

    response = client.post(
        "/recycle-bin/batch-restore",
        data={"selected_ids": ["21", "999"]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert batch.created[0][0] == "restore"
    assert batch.created[0][1] == [21]


def test_model_connection_route_returns_readable_feedback(monkeypatch):
    monkeypatch.setattr(
        app_module.enhancement_queue,
        "adapter",
        SimpleNamespace(test_connection=lambda: "已连接 Test / model"),
    )
    response = TestClient(app_module.app).post("/settings/model/test", follow_redirects=False)

    assert response.status_code == 303
    assert "model_status=ok" in response.headers["location"]


def test_single_file_can_request_full_reprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(
        app_module,
        "ingestion_queue",
        SimpleNamespace(reprocess=lambda file_id, replacement_data=None, replacement_filename=None: calls.append((file_id, replacement_data, replacement_filename))),
    )
    response = TestClient(app_module.app).post("/files/7/reprocess", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/ingest"
    assert calls == [(7, None, None)]
