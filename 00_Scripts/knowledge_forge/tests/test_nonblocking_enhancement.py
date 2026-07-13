from __future__ import annotations

import json
from pathlib import Path

from knowledge_forge.core_processing import CoreTextProcessor
from knowledge_forge.db import connect, init_db
from knowledge_forge.enhancement import (
    ArtifactOutcome,
    AutomaticTag,
    ClassificationOutcome,
    KnowledgeEnhancementQueue,
)
from knowledge_forge.ingestion import PersistentTextImportQueue
from knowledge_forge import services


class FakeEnhancer:
    def __init__(self, failures=(), sop_not_applicable=False):
        self.failures = set(failures)
        self.sop_not_applicable = sop_not_applicable
        self.calls = []

    def classify(self, _title, _text):
        self.calls.append("classification")
        if "classification" in self.failures:
            raise RuntimeError("classifier secret details")
        return ClassificationOutcome(
            category="08_Art",
            tags=(
                AutomaticTag("08_Art/色彩", 0.91, "主要章节讨论色彩"),
                AutomaticTag("08_Art/构图", 0.84, "主要章节讨论构图"),
            ),
        )

    def generate(self, kind, title, _text, _prompt):
        self.calls.append(kind)
        if kind in self.failures:
            raise RuntimeError(f"{kind} secret details")
        if kind == "sop" and self.sop_not_applicable:
            return ArtifactOutcome(content=None, not_applicable_reason="资料为理论说明，不包含操作流程。")
        return ArtifactOutcome(content=f"# {kind}\n\n{title}")


def make_system(tmp_path, enhancer):
    database_path = tmp_path / "knowledge.db"
    upload_dir = tmp_path / "uploads"
    init_db(database_path, tmp_path / "managed")
    sequence = 0

    def accept(filename, data):
        nonlocal sequence
        sequence += 1
        upload_dir.mkdir(exist_ok=True)
        path = upload_dir / f"{sequence}-{filename}"
        path.write_bytes(data)
        with connect(database_path) as conn:
            return int(conn.execute(
                """
                INSERT INTO files(source_path, library_type, title, filename, extension, status)
                VALUES (?, 'upload', ?, ?, ?, 'processing')
                """,
                (str(path), Path(filename).stem, filename, Path(filename).suffix),
            ).lastrowid)

    enhancement_queue = KnowledgeEnhancementQueue(
        database_path, tmp_path / "artifacts", enhancer
    )
    core = CoreTextProcessor(database_path, tmp_path / "standard")
    import_queue = PersistentTextImportQueue(
        database_path,
        accept,
        core.process,
        completion_callback=enhancement_queue.enqueue_for_task,
    )
    return database_path, import_queue, enhancement_queue


def complete_import(import_queue, filename="lesson.txt"):
    task = import_queue.submit(filename, ("Useful lesson content. " * 30).encode())
    assert import_queue.acquire_worker() is True
    import_queue.process_next()
    import_queue.release_worker()
    return task


def test_available_version_enqueues_independent_enhancements_and_failures_do_not_block(tmp_path, monkeypatch):
    database_path, imports, enhancements = make_system(
        tmp_path, FakeEnhancer(failures={"classification", "insight"})
    )
    task = complete_import(imports)

    assert imports.get_task(task.id).status == "completed"
    assert [job.kind for job in enhancements.list_jobs(task.version_id)] == [
        "classification", "structure", "sop", "insight"
    ]
    enhancements.process_all_ready()

    statuses = {job.kind: job.status for job in enhancements.list_jobs(task.version_id)}
    assert statuses == {
        "classification": "needs_attention",
        "structure": "completed",
        "sop": "completed",
        "insight": "needs_attention",
    }
    assert "secret details" not in enhancements.get_job(task.version_id, "classification").message
    with connect(database_path) as conn:
        version = conn.execute("SELECT status FROM source_versions WHERE id=?", (task.version_id,)).fetchone()[0]
        category = conn.execute(
            "SELECT main_category FROM files WHERE id=(SELECT standard_file_id FROM source_versions WHERE id=?)",
            (task.version_id,),
        ).fetchone()[0]
    assert version == "available"
    assert category == "00_Unsorted"
    file_id = imports.knowledge_entry_for_task(task.id)
    monkeypatch.setattr(services, "connect", lambda: connect(database_path))
    assert file_id in [row["id"] for row in services.list_files(limit=100)]
    with connect(database_path) as conn:
        tag_id = conn.execute("INSERT INTO tags(name) VALUES ('08_Art')").lastrowid
        pack_id = conn.execute(
            "INSERT INTO packs(name, recipe_json) VALUES ('Art only', ?)",
            (json.dumps({"include_tags": ["08_Art"], "min_confidence": 0.7}),),
        ).lastrowid
    assert services.files_for_pack(pack_id) == []


def test_automatic_tags_keep_confidence_evidence_and_never_overwrite_human_tags(tmp_path):
    database_path, imports, enhancements = make_system(tmp_path, FakeEnhancer())
    task = complete_import(imports)
    file_id = imports.knowledge_entry_for_task(task.id)
    with connect(database_path) as conn:
        human_tag = conn.execute("INSERT INTO tags(name) VALUES ('08_Art/色彩')").lastrowid
        conn.execute(
            """
            INSERT INTO tag_assignments(
                target_type, target_id, tag_id, scope, confidence, status, source, evidence
            ) VALUES ('file', ?, ?, 'file_strong', 1.0, 'user_approved', 'human', '人工确认')
            """,
            (file_id, human_tag),
        )

    enhancements.process_kind(task.version_id, "classification")

    with connect(database_path) as conn:
        assignment = conn.execute(
            """
            SELECT ta.* FROM tag_assignments ta JOIN tags t ON t.id=ta.tag_id
            WHERE ta.target_id=? AND t.name='08_Art/色彩'
            """,
            (file_id,),
        ).fetchone()
    assert assignment["source"] == "human"
    assert assignment["confidence"] == 1.0
    assert assignment["evidence"] == "人工确认"
    with connect(database_path) as conn:
        automatic = conn.execute(
            """
            SELECT ta.* FROM tag_assignments ta JOIN tags t ON t.id=ta.tag_id
            WHERE ta.target_id=? AND t.name='08_Art/构图'
            """,
            (file_id,),
        ).fetchone()
    assert automatic["confidence"] == 0.84
    assert automatic["source"] == "classifier"
    assert automatic["evidence"] == "主要章节讨论构图"


def test_sop_can_be_not_applicable_and_targeted_regeneration_does_not_touch_other_jobs(tmp_path):
    _, imports, enhancements = make_system(
        tmp_path, FakeEnhancer(sop_not_applicable=True)
    )
    task = complete_import(imports)
    enhancements.process_all_ready()
    before = {job.kind: (job.status, job.updated_at) for job in enhancements.list_jobs(task.version_id)}

    sop = enhancements.get_job(task.version_id, "sop")
    assert sop.status == "not_applicable"
    assert sop.message == "资料为理论说明，不包含操作流程。"

    enhancements.regenerate(task.version_id, "insight")
    after = {job.kind: (job.status, job.updated_at) for job in enhancements.list_jobs(task.version_id)}
    assert after["insight"][0] == "waiting"
    assert after["structure"] == before["structure"]
    assert after["sop"] == before["sop"]


def test_restart_reconciles_available_version_missing_enhancement_jobs(tmp_path):
    database_path, imports, enhancements = make_system(tmp_path, FakeEnhancer())
    task = complete_import(imports)
    with connect(database_path) as conn:
        conn.execute("DELETE FROM knowledge_enhancement_jobs WHERE version_id=?", (task.version_id,))

    enhancements.reconcile_available_versions()

    assert [job.kind for job in enhancements.list_jobs(task.version_id)] == [
        "classification", "structure", "sop", "insight"
    ]
