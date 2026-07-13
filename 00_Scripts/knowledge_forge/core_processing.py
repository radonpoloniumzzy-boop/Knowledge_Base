from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable
from uuid import uuid4

from .db import connect
from .extraction import (
    DamagedDocumentError,
    DocumentExtractor,
    EncryptedDocumentError,
    MarkItDownDocumentExtractor,
    TemporaryExtractionError,
    UnsupportedDocumentError,
)
from .ingestion import (
    DeterministicImportError,
    ImportTask,
    TaskPaused,
    TransientImportError,
)
from .services import chunk_text, clean_text, estimate_tokens


CORE_STAGES = (
    "extract_text",
    "standard_document",
    "quality_validation",
    "chunk_indexing",
    "promote_version",
)


class CoreTextProcessor:
    """Runs resumable core text stages and exposes results only at promotion."""

    def __init__(
        self,
        database_path: Path,
        standard_dir: Path,
        *,
        stage_hook: Callable[[str, str], None] | None = None,
        extractor: DocumentExtractor | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.standard_dir = Path(standard_dir)
        self._stage_hook = stage_hook or (lambda _stage, _moment: None)
        self._extractor = extractor or MarkItDownDocumentExtractor()

    def process(self, task: ImportTask) -> None:
        version_id = self._ensure_version(task.id, task.file_id)
        completed = self._completed_stages(task.id)
        for stage in CORE_STAGES:
            if stage in completed:
                continue
            with connect(self.database_path) as conn:
                conn.execute(
                    "UPDATE import_tasks SET current_stage=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (stage, task.id),
                )
            getattr(self, f"_{stage}")(task.id, task.file_id, version_id)
            with connect(self.database_path) as conn:
                pause_requested = conn.execute(
                    "SELECT pause_requested FROM import_tasks WHERE id=?", (task.id,)
                ).fetchone()[0]
            if pause_requested:
                raise TaskPaused()

    def _ensure_version(self, task_id: int, file_id: int) -> int:
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute(
                "SELECT source_id, version_id FROM import_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if task["version_id"] is not None:
                return int(task["version_id"])
            source_id = task["source_id"]
            if source_id is None:
                conn.execute(
                    "INSERT OR IGNORE INTO knowledge_sources(source_file_id) VALUES (?)",
                    (file_id,),
                )
                source_id = conn.execute(
                    "SELECT id FROM knowledge_sources WHERE source_file_id=?", (file_id,)
                ).fetchone()[0]
            version_id = conn.execute(
                """
                INSERT INTO source_versions(source_id, upload_file_id)
                VALUES (?, ?)
                """,
                (source_id, file_id),
            ).lastrowid
            conn.execute(
                "UPDATE import_tasks SET source_id=?, version_id=? WHERE id=?",
                (source_id, version_id, task_id),
            )
            return int(version_id)

    def _completed_stages(self, task_id: int) -> set[str]:
        with connect(self.database_path) as conn:
            return {
                row[0]
                for row in conn.execute(
                    "SELECT stage_name FROM import_stage_results WHERE task_id=?",
                    (task_id,),
                )
            }

    def _payload(self, task_id: int, stage: str) -> str:
        with connect(self.database_path) as conn:
            row = conn.execute(
                "SELECT payload_text FROM import_stage_results WHERE task_id=? AND stage_name=?",
                (task_id, stage),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"Missing committed stage: {stage}")
        return row[0] or ""

    def _commit_stage(self, task_id: int, stage: str, payload: str = "") -> None:
        self._stage_hook(stage, "before_commit")
        with connect(self.database_path) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO import_stage_results(task_id, stage_name, payload_text)
                VALUES (?, ?, ?)
                """,
                (task_id, stage, payload),
            )

    def _extract_text(self, task_id: int, file_id: int, _version_id: int) -> None:
        with connect(self.database_path) as conn:
            row = conn.execute("SELECT source_path FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise DeterministicImportError("extract_text", "找不到上传文件，请重新选择文件。")
        source = Path(row["source_path"])
        try:
            extracted = self._extractor.extract(source)
        except TemporaryExtractionError as exc:
            raise TransientImportError(
                "extract_text", "文档暂时无法读取，系统将自动重试。"
            ) from exc
        except UnsupportedDocumentError as exc:
            raise DeterministicImportError(
                "extract_text", "当前文件格式不受支持，请转换为受支持的文档格式。"
            ) from exc
        except EncryptedDocumentError as exc:
            raise DeterministicImportError(
                "extract_text", "文档已加密或受密码保护，请解除保护后继续。"
            ) from exc
        except DamagedDocumentError as exc:
            raise DeterministicImportError(
                "extract_text", "文档可能已经损坏，请修复或重新导出后继续。"
            ) from exc
        text = clean_text(extracted.text)
        if not text:
            raise DeterministicImportError(
                "extract_text", "文档没有提取到可用内容，请检查文件或重新导出。"
            )
        invalid_count = text.count("�") + sum(
            1 for char in text if ord(char) < 32 and char not in "\n\r\t"
        )
        if invalid_count / max(len(text), 1) > 0.1:
            raise DeterministicImportError(
                "extract_text", "提取内容包含过多异常字符，请重新导出文档。"
            )
        compact = " ".join(text.casefold().split())
        if len(compact) < 1000 and (
            compact.startswith("404 not found")
            or ("access denied" in compact and len(compact) < 300)
            or compact.startswith("conversion failed")
        ):
            raise DeterministicImportError(
                "extract_text", "提取结果是错误页面，请检查原文档后继续。"
            )
        metadata = {
            **extracted.metadata,
            "source_format": extracted.source_format,
            "character_count": len(text),
        }
        self._stage_hook("extract_text", "before_commit")
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE source_versions SET extraction_metadata_json=? WHERE id=?",
                (json.dumps(metadata, ensure_ascii=False), _version_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO import_stage_results(task_id, stage_name, payload_text)
                VALUES (?, 'extract_text', ?)
                """,
                (task_id, text),
            )

    def _standard_document(self, task_id: int, file_id: int, version_id: int) -> None:
        text = self._payload(task_id, "extract_text")
        with connect(self.database_path) as conn:
            source = conn.execute("SELECT filename FROM files WHERE id=?", (file_id,)).fetchone()
        stem = Path(source["filename"]).stem if source else "knowledge"
        target_dir = self.standard_dir / "00_Pending_Review"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"v{version_id}-{stem}.md"
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            if temporary.read_text(encoding="utf-8") != text:
                raise DeterministicImportError(
                    "standard_document", "标准知识文档写入验证失败，请检查存储位置。"
                )
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        self._stage_hook("standard_document", "before_commit")
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE source_versions SET standard_path=? WHERE id=?",
                (str(target), version_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO import_stage_results(task_id, stage_name, payload_text)
                VALUES (?, 'standard_document', ?)
                """,
                (task_id, str(target)),
            )

    def _quality_validation(self, task_id: int, _file_id: int, _version_id: int) -> None:
        target = Path(self._payload(task_id, "standard_document"))
        if not target.is_file():
            raise DeterministicImportError(
                "quality_validation", "标准知识文档缺失，请继续任务以重新生成。"
            )
        text = target.read_text(encoding="utf-8")
        if not text.strip():
            raise DeterministicImportError(
                "quality_validation", "标准知识文档为空，请检查源文件。"
            )
        with connect(self.database_path) as conn:
            metadata_row = conn.execute(
                "SELECT extraction_metadata_json FROM source_versions WHERE id=?",
                (_version_id,),
            ).fetchone()
        metadata = json.loads(metadata_row[0] or "{}")
        warnings = []
        if len(text.strip()) < 200:
            warnings.append("内容较短，请确认提取结果是否完整。")
        if metadata.get("source_format") in {"ppt", "pptx", "xls", "xlsx"} and len(text) < 1000:
            warnings.append("表格或幻灯片文字较少，请确认结构信息是否完整。")
        alphanumeric = sum(char.isalnum() for char in text)
        if len(text) >= 200 and alphanumeric / max(len(text), 1) < 0.2:
            warnings.append("文字密度较低，请确认图表或版式信息是否完整。")
        self._stage_hook("quality_validation", "before_commit")
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE source_versions SET quality_warnings_json=? WHERE id=?",
                (json.dumps(warnings, ensure_ascii=False), _version_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO import_stage_results(task_id, stage_name, payload_text)
                VALUES (?, 'quality_validation', ?)
                """,
                (task_id, json.dumps({"warnings": warnings}, ensure_ascii=False)),
            )

    def _chunk_indexing(self, task_id: int, _file_id: int, version_id: int) -> None:
        text = self._payload(task_id, "extract_text")
        chunks = chunk_text(text)
        if not chunks or any(not chunk.strip() for chunk in chunks):
            raise DeterministicImportError(
                "chunk_indexing", "无法生成完整检索分块，请检查文档内容。"
            )
        self._stage_hook("chunk_indexing", "before_commit")
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM staged_chunks WHERE version_id=?", (version_id,))
            conn.executemany(
                """
                INSERT INTO staged_chunks(version_id, chunk_index, text, token_estimate, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (version_id, index, chunk, estimate_tokens(chunk), json.dumps({"task_id": task_id}))
                    for index, chunk in enumerate(chunks)
                ],
            )
            conn.execute(
                "INSERT INTO import_stage_results(task_id, stage_name, payload_text) VALUES (?, 'chunk_indexing', ?)",
                (task_id, str(len(chunks))),
            )

    def _promote_version(self, task_id: int, file_id: int, version_id: int) -> None:
        path = Path(self._payload(task_id, "standard_document"))
        self._stage_hook("promote_version", "before_commit")
        with connect(self.database_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute(
                "SELECT source_id FROM source_versions WHERE id=?", (version_id,)
            ).fetchone()
            conn.execute(
                """
                INSERT OR IGNORE INTO files(
                    source_path, library_type, title, filename, extension,
                    size_bytes, mtime, main_category, status, confidence
                ) VALUES (?, 'standard', ?, ?, '.md', ?, ?, '00_Pending_Review', 'completed', 0.5)
                """,
                (str(path), path.stem, path.name, path.stat().st_size, path.stat().st_mtime),
            )
            standard_file_id = conn.execute(
                "SELECT id FROM files WHERE source_path=?", (str(path),)
            ).fetchone()[0]
            conn.execute("DELETE FROM chunks WHERE file_id=?", (standard_file_id,))
            conn.execute(
                """
                INSERT INTO chunks(file_id, chunk_index, text, token_estimate, metadata_json)
                SELECT ?, chunk_index, text, token_estimate, metadata_json
                FROM staged_chunks WHERE version_id=? ORDER BY chunk_index
                """,
                (standard_file_id, version_id),
            )
            expected = conn.execute(
                "SELECT COUNT(*) FROM staged_chunks WHERE version_id=?", (version_id,)
            ).fetchone()[0]
            actual = conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE file_id=?", (standard_file_id,)
            ).fetchone()[0]
            if not expected or expected != actual:
                raise DeterministicImportError(
                    "promote_version", "检索分块完整性验证失败，旧版本保持可用。"
                )
            conn.execute(
                """
                UPDATE source_versions
                SET standard_file_id=?, status='available', review_status='unreviewed',
                    available_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (standard_file_id, version_id),
            )
            conn.execute(
                "UPDATE knowledge_sources SET current_version_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (version_id, version["source_id"]),
            )
            conn.execute("DELETE FROM staged_chunks WHERE version_id=?", (version_id,))
            conn.execute(
                "INSERT INTO import_stage_results(task_id, stage_name, payload_text) VALUES (?, 'promote_version', ?)",
                (task_id, str(standard_file_id)),
            )
