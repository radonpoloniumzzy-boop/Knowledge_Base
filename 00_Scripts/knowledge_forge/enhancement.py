from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from . import services
from .db import connect
from .ingestion import ImportTask


ENHANCEMENT_KINDS = ("classification", "structure", "sop", "insight")
PROMPT_NAMES = {
    "classification": "taxonomy_classifier",
    "structure": "structure_note",
    "sop": "sop_generator",
    "insight": "insight_generator",
}


@dataclass(frozen=True)
class AutomaticTag:
    name: str
    confidence: float
    evidence: str


@dataclass(frozen=True)
class ClassificationOutcome:
    category: str
    tags: tuple[AutomaticTag, ...]


@dataclass(frozen=True)
class ArtifactOutcome:
    content: str | None
    not_applicable_reason: str | None = None


@dataclass(frozen=True)
class EnhancementJob:
    id: int
    version_id: int
    kind: str
    status: str
    message: str | None
    prompt_name: str | None
    prompt_version: str | None
    artifact_id: int | None
    created_at: str
    updated_at: str
    completed_at: str | None


class EnhancementAdapter(Protocol):
    def classify(self, title: str, text: str) -> ClassificationOutcome: ...
    def generate(self, kind: str, title: str, text: str, prompt: str) -> ArtifactOutcome: ...


class OfflineEnhancementAdapter:
    KEYWORDS = (
        ("08_Art", ("绘画", "色彩", "构图", "透视", "art", "design")),
        ("03_Sales", ("销售", "成交", "客户", "营销", "sales", "marketing")),
        ("01_Finance", ("金融", "投资", "风控", "股票", "finance", "risk")),
        ("04_Coding", ("编程", "代码", "python", "javascript", "database")),
        ("05_Ops", ("管理", "绩效", "组织", "management", "operations")),
    )

    def classify(self, _title: str, text: str) -> ClassificationOutcome:
        lowered = text.casefold()
        for category, keywords in self.KEYWORDS:
            matched = next((word for word in keywords if word in lowered), None)
            if matched:
                return ClassificationOutcome(
                    category=category,
                    tags=(AutomaticTag(category, 0.78, f"正文多次涉及：{matched}"),),
                )
        return ClassificationOutcome(category="00_Unsorted", tags=())

    def generate(self, kind: str, title: str, text: str, _prompt: str) -> ArtifactOutcome:
        if kind == "structure":
            return ArtifactOutcome(services.build_structure_note(title, text))
        if kind == "insight":
            return ArtifactOutcome(services.build_insight_draft(title, text))
        if kind == "sop":
            procedural = re.search(r"步骤|流程|操作|方法|step|how to", text, re.IGNORECASE)
            if not procedural:
                return ArtifactOutcome(None, "资料为理论说明，不包含明确操作流程。")
            return ArtifactOutcome(services.build_sop_draft(title, text))
        raise ValueError(kind)


class KnowledgeEnhancementQueue:
    def __init__(self, database_path: Path, artifact_dir: Path, adapter: EnhancementAdapter) -> None:
        self.database_path = Path(database_path)
        self.artifact_dir = Path(artifact_dir)
        self.adapter = adapter
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def enqueue_for_task(self, task: ImportTask) -> None:
        if task.version_id is None:
            return
        with connect(self.database_path) as conn:
            available = conn.execute(
                """
                SELECT 1
                FROM source_versions sv
                JOIN knowledge_sources ks ON ks.id=sv.source_id
                WHERE sv.id=? AND sv.status='available'
                  AND ks.deleted_at IS NULL
                  AND ks.recycle_requested_at IS NULL
                """,
                (task.version_id,),
            ).fetchone()
            if not available:
                return
            conn.executemany(
                """
                INSERT OR IGNORE INTO knowledge_enhancement_jobs(version_id, kind, prompt_name)
                VALUES (?, ?, ?)
                """,
                [(task.version_id, kind, PROMPT_NAMES[kind]) for kind in ENHANCEMENT_KINDS],
            )

    def list_jobs(self, version_id: int) -> list[EnhancementJob]:
        with connect(self.database_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM knowledge_enhancement_jobs WHERE version_id=?
                ORDER BY CASE kind
                    WHEN 'classification' THEN 1 WHEN 'structure' THEN 2
                    WHEN 'sop' THEN 3 WHEN 'insight' THEN 4 END
                """,
                (version_id,),
            ).fetchall()
        return [EnhancementJob(**dict(row)) for row in rows]

    def get_job(self, version_id: int, kind: str) -> EnhancementJob:
        with connect(self.database_path) as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_enhancement_jobs WHERE version_id=? AND kind=?",
                (version_id, kind),
            ).fetchone()
        if row is None:
            raise KeyError((version_id, kind))
        return EnhancementJob(**dict(row))

    def process_all_ready(self) -> None:
        while True:
            with connect(self.database_path) as conn:
                row = conn.execute(
                    """
                    SELECT kej.version_id, kej.kind
                    FROM knowledge_enhancement_jobs kej
                    JOIN source_versions sv ON sv.id=kej.version_id
                    JOIN knowledge_sources ks ON ks.id=sv.source_id
                    WHERE kej.status='waiting'
                      AND ks.deleted_at IS NULL
                      AND ks.recycle_requested_at IS NULL
                    ORDER BY kej.id LIMIT 1
                    """
                ).fetchone()
            if row is None:
                return
            self.process_kind(row["version_id"], row["kind"])

    def process_kind(self, version_id: int, kind: str) -> None:
        if kind not in ENHANCEMENT_KINDS:
            raise ValueError(kind)
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM knowledge_enhancement_jobs WHERE version_id=? AND kind=?",
                (version_id, kind),
            ).fetchone()
            if job is None:
                raise KeyError((version_id, kind))
            if job["status"] != "waiting":
                return
            conn.execute(
                "UPDATE knowledge_enhancement_jobs SET status='processing', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (job["id"],),
            )
        try:
            self._execute(version_id, kind)
        except Exception:
            with connect(self.database_path) as conn:
                conn.execute(
                    """
                    UPDATE knowledge_enhancement_jobs
                    SET status='needs_attention', message='增强处理失败，可单独重试。',
                        updated_at=CURRENT_TIMESTAMP WHERE version_id=? AND kind=?
                    """,
                    (version_id, kind),
                )

    def _execute(self, version_id: int, kind: str) -> None:
        with connect(self.database_path) as conn:
            version = conn.execute(
                """
                SELECT sv.*, f.title, f.source_path
                FROM source_versions sv
                JOIN knowledge_sources ks ON ks.id=sv.source_id
                JOIN files f ON f.id=sv.standard_file_id
                WHERE sv.id=? AND sv.status='available'
                  AND ks.deleted_at IS NULL
                  AND ks.recycle_requested_at IS NULL
                """,
                (version_id,),
            ).fetchone()
            prompt = conn.execute(
                """
                SELECT name, version, content FROM prompts
                WHERE name=? AND active=1 ORDER BY id DESC LIMIT 1
                """,
                (PROMPT_NAMES[kind],),
            ).fetchone()
        if version is None:
            raise ValueError("version unavailable")
        text = Path(version["source_path"]).read_text(encoding="utf-8")
        prompt_version = prompt["version"] if prompt else "v1"
        prompt_content = prompt["content"] if prompt else ""
        if kind == "classification":
            outcome = self.adapter.classify(version["title"], text)
            self._commit_classification(version_id, version["standard_file_id"], outcome, prompt_version)
            return
        outcome = self.adapter.generate(kind, version["title"], text, prompt_content)
        if outcome.not_applicable_reason:
            with connect(self.database_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if not self._version_is_active(conn, version_id):
                    self._pause_job(conn, version_id, kind)
                    return
                conn.execute(
                    """
                    UPDATE knowledge_enhancement_jobs
                    SET status='not_applicable', message=?, prompt_version=?,
                        completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE version_id=? AND kind=?
                    """,
                    (outcome.not_applicable_reason, prompt_version, version_id, kind),
                )
            return
        self._commit_artifact(
            version_id, version["standard_file_id"], kind, version["title"],
            outcome.content or "", PROMPT_NAMES[kind], prompt_version,
        )

    def _commit_classification(
        self, version_id: int, file_id: int, outcome: ClassificationOutcome, prompt_version: str
    ) -> None:
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._version_is_active(conn, version_id):
                self._pause_job(conn, version_id, "classification")
                return
            conn.execute(
                """
                DELETE FROM tag_assignments
                WHERE target_type='file' AND target_id=? AND source='classifier'
                  AND status NOT LIKE 'user_%'
                """,
                (file_id,),
            )
            for tag in outcome.tags:
                services.assign_tag(
                    conn, "file", file_id, tag.name, "file_strong",
                    tag.confidence, "auto_accepted", "classifier", tag.evidence,
                )
            conn.execute(
                "UPDATE files SET main_category=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (outcome.category or "00_Unsorted", file_id),
            )
            conn.execute(
                """
                UPDATE knowledge_enhancement_jobs
                SET status='completed', message=NULL, prompt_version=?,
                    completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE version_id=? AND kind='classification'
                """,
                (prompt_version, version_id),
            )

    def _commit_artifact(
        self, version_id: int, file_id: int, kind: str, title: str,
        content: str, prompt_name: str, prompt_version: str,
    ) -> None:
        target_dir = self.artifact_dir / str(file_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{kind}_{prompt_version}_{uuid4().hex[:8]}.md"
        temporary = target.with_suffix(".tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            with connect(self.database_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                if not self._version_is_active(conn, version_id):
                    self._pause_job(conn, version_id, kind)
                    return
                os.replace(temporary, target)
                artifact_id = conn.execute(
                    """
                    INSERT INTO artifacts(file_id, artifact_type, path, title, prompt_name, prompt_version)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (file_id, kind, str(target), title, prompt_name, prompt_version),
                ).lastrowid
                conn.execute(
                    """
                    UPDATE knowledge_enhancement_jobs
                    SET status='completed', message=NULL, prompt_version=?, artifact_id=?,
                        completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE version_id=? AND kind=?
                    """,
                    (prompt_version, artifact_id, version_id, kind),
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _version_is_active(conn, version_id: int) -> bool:
        return conn.execute(
            """
            SELECT 1
            FROM source_versions sv
            JOIN knowledge_sources ks ON ks.id=sv.source_id
            WHERE sv.id=? AND ks.deleted_at IS NULL
              AND ks.recycle_requested_at IS NULL
            """,
            (version_id,),
        ).fetchone() is not None

    @staticmethod
    def _pause_job(conn, version_id: int, kind: str) -> None:
        conn.execute(
            """
            UPDATE knowledge_enhancement_jobs
            SET status='paused', updated_at=CURRENT_TIMESTAMP
            WHERE version_id=? AND kind=?
            """,
            (version_id, kind),
        )

    def regenerate(self, version_id: int, kind: str) -> None:
        if kind not in ENHANCEMENT_KINDS:
            raise ValueError(kind)
        with connect(self.database_path) as conn:
            cursor = conn.execute(
                """
                UPDATE knowledge_enhancement_jobs
                SET status='waiting', message=NULL, artifact_id=NULL,
                    completed_at=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE version_id=? AND kind=?
                """,
                (version_id, kind),
            )
        if cursor.rowcount == 0:
            raise KeyError((version_id, kind))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.reconcile_available_versions()
        with connect(self.database_path) as conn:
            conn.execute(
                """
                UPDATE knowledge_enhancement_jobs
                SET status='waiting'
                WHERE status='processing'
                  AND version_id IN (
                      SELECT sv.id FROM source_versions sv
                      JOIN knowledge_sources ks ON ks.id=sv.source_id
                      WHERE ks.deleted_at IS NULL AND ks.recycle_requested_at IS NULL
                  )
                """
            )
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="knowledge-enhancement", daemon=True)
        self._thread.start()

    def reconcile_available_versions(self) -> None:
        with connect(self.database_path) as conn:
            versions = conn.execute(
                """
                SELECT sv.id
                FROM source_versions sv
                JOIN knowledge_sources ks ON ks.id=sv.source_id
                WHERE sv.status='available'
                  AND ks.deleted_at IS NULL
                  AND ks.recycle_requested_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM knowledge_enhancement_jobs ej WHERE ej.version_id=sv.id
                  )
                """
            ).fetchall()
            for version in versions:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO knowledge_enhancement_jobs(version_id, kind, prompt_name)
                    VALUES (?, ?, ?)
                    """,
                    [(version["id"], kind, PROMPT_NAMES[kind]) for kind in ENHANCEMENT_KINDS],
                )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.process_all_ready()
            self._stop.wait(0.5)
