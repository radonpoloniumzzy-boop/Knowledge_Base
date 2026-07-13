from __future__ import annotations

from knowledge_forge import services
from knowledge_forge import app as app_module
from fastapi.testclient import TestClient
from knowledge_forge.db import connect, init_db


def test_markdown_reader_builds_toc_and_removes_dangerous_html(tmp_path, monkeypatch):
    db = tmp_path / "knowledge.db"
    standard = tmp_path / "lesson.md"
    standard.write_text("# Lesson\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(2))\n\n## Steps\n\n- One", encoding="utf-8")
    init_db(db, tmp_path / "managed")
    with connect(db) as conn:
        file_id = conn.execute(
            "INSERT INTO files(source_path,library_type,title,filename,status) VALUES (?, 'standard','Lesson','lesson.md','completed')",
            (str(standard),),
        ).lastrowid
    monkeypatch.setattr(services, "connect", lambda: connect(db))

    reader = services.document_reader(file_id)

    assert "Lesson" in reader["toc"] and "Steps" in reader["toc"]
    assert "<script" not in reader["html"]
    assert "javascript:" not in reader["html"]
    assert "<h1" in reader["html"]


def test_reader_switches_to_artifact_and_handles_missing_content(tmp_path, monkeypatch):
    db = tmp_path / "knowledge.db"
    standard = tmp_path / "lesson.md"
    sop = tmp_path / "sop.md"
    standard.write_text("# Standard", encoding="utf-8")
    sop.write_text("# SOP\n\n1. Act", encoding="utf-8")
    init_db(db, tmp_path / "managed")
    with connect(db) as conn:
        file_id = conn.execute("INSERT INTO files(source_path,library_type,title,filename,status) VALUES (?, 'standard','Lesson','lesson.md','completed')", (str(standard),)).lastrowid
        conn.execute("INSERT INTO artifacts(file_id,artifact_type,path,title) VALUES (?, 'sop', ?, 'SOP')", (file_id, str(sop)))
    monkeypatch.setattr(services, "connect", lambda: connect(db))

    sop_reader = services.document_reader(file_id, "sop")
    missing = services.document_reader(file_id, "insight")

    assert "Act" in sop_reader["html"]
    assert "sop" in sop_reader["available_views"]
    assert "尚未生成" in missing["html"]


def test_detail_defaults_to_read_mode_and_exposes_manage_mode(tmp_path, monkeypatch):
    db = tmp_path / "knowledge.db"
    standard = tmp_path / "lesson.md"
    standard.write_text("# Read me", encoding="utf-8")
    init_db(db, tmp_path / "managed")
    with connect(db) as conn:
        file_id = conn.execute("INSERT INTO files(source_path,library_type,title,filename,status) VALUES (?, 'standard','Lesson','lesson.md','completed')", (str(standard),)).lastrowid
    monkeypatch.setattr(services, "connect", lambda: connect(db))
    monkeypatch.setattr(app_module.ingestion_queue, "version_history_for_file", lambda _id: [])
    monkeypatch.setattr(app_module.recycle_bin, "source_id_for_file", lambda _id: None)

    read = TestClient(app_module.app).get(f"/files/{file_id}")
    manage = TestClient(app_module.app).get(f"/files/{file_id}?mode=manage")

    assert read.status_code == 200 and "Read me" in read.text
    assert "学者阅读台" not in read.text or "Scholar desk" in read.text
    assert manage.status_code == 200 and "资料治理与版本状态" in manage.text
