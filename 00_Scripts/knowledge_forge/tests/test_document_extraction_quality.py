from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_forge.core_processing import CoreTextProcessor
from knowledge_forge.db import connect, init_db
from knowledge_forge.extraction import (
    DamagedDocumentError,
    EncryptedDocumentError,
    ExtractedDocument,
    TemporaryExtractionError,
    UnsupportedDocumentError,
)
from knowledge_forge.ingestion import PersistentTextImportQueue


SUPPORTED_DOCUMENTS = (
    ".md", ".txt", ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".xls", ".xlsx", ".html", ".csv", ".json",
)


class FakeExtractor:
    def __init__(self, outcome=None):
        self.outcome = outcome or ExtractedDocument(
            text="A sufficiently detailed lesson. " * 50,
            source_format="fake",
            metadata={"adapter": "fake"},
        )
        self.calls = []

    def extract(self, path: Path) -> ExtractedDocument:
        self.calls.append(path)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return ExtractedDocument(
            text=self.outcome.text,
            source_format=path.suffix.lower().lstrip("."),
            metadata=self.outcome.metadata,
        )


def make_system(tmp_path, extractor):
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

    processor = CoreTextProcessor(database_path, tmp_path / "standard", extractor=extractor)
    queue = PersistentTextImportQueue(database_path, accept, processor.process)
    return database_path, queue


def process(queue, task):
    assert queue.acquire_worker() is True
    queue.process_next()
    result = queue.get_task(task.id)
    queue.release_worker()
    return result


@pytest.mark.parametrize("extension", SUPPORTED_DOCUMENTS)
def test_every_declared_document_format_uses_the_same_extractor(extension, tmp_path):
    extractor = FakeExtractor()
    database_path, queue = make_system(tmp_path, extractor)
    task = queue.submit(f"lesson{extension}", b"document bytes")

    result = process(queue, task)

    assert result.status == "completed"
    assert [path.suffix.lower() for path in extractor.calls] == [extension]
    with connect(database_path) as conn:
        version = conn.execute("SELECT * FROM source_versions WHERE id=?", (task.version_id,)).fetchone()
    assert version["extraction_metadata_json"] is not None
    assert version["quality_warnings_json"] == "[]"


@pytest.mark.parametrize(
    "error,failure_type,message",
    [
        (TemporaryExtractionError(), "transient", "文档暂时无法读取，系统将自动重试。"),
        (UnsupportedDocumentError(), "deterministic", "当前文件格式不受支持，请转换为受支持的文档格式。"),
        (EncryptedDocumentError(), "deterministic", "文档已加密或受密码保护，请解除保护后继续。"),
        (DamagedDocumentError(), "deterministic", "文档可能已经损坏，请修复或重新导出后继续。"),
    ],
)
def test_extraction_failures_are_classified_without_exposing_adapter_details(
    error, failure_type, message, tmp_path
):
    database_path, queue = make_system(tmp_path, FakeExtractor(error))
    task = queue.submit("broken.pdf", b"broken")

    result = process(queue, task)

    assert result.failure_type == failure_type
    assert result.user_message == message
    with connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM files WHERE library_type='standard'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
        assert conn.execute("SELECT current_version_id FROM knowledge_sources").fetchone()[0] is None


@pytest.mark.parametrize(
    "text,expected_message",
    [
        ("", "文档没有提取到可用内容，请检查文件或重新导出。"),
        ("�" * 80 + "valid", "提取内容包含过多异常字符，请重新导出文档。"),
        ("404 Not Found\nAccess denied", "提取结果是错误页面，请检查原文档后继续。"),
    ],
)
def test_unusable_extraction_is_blocked_before_formal_document_or_chunks(
    text, expected_message, tmp_path
):
    database_path, queue = make_system(
        tmp_path,
        FakeExtractor(ExtractedDocument(text=text, source_format="pdf", metadata={})),
    )
    task = queue.submit("quality.pdf", b"source")

    result = process(queue, task)

    assert result.status == "needs_attention"
    assert result.user_message == expected_message
    with connect(database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM files WHERE library_type='standard'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_short_sparse_content_records_warnings_but_remains_available(tmp_path):
    extractor = FakeExtractor(
        ExtractedDocument(text="Q1 | 42", source_format="xlsx", metadata={"sheets": 1})
    )
    database_path, queue = make_system(tmp_path, extractor)
    task = queue.submit("small.xlsx", b"sheet")

    result = process(queue, task)
    history = queue.version_history_for_file(queue.knowledge_entry_for_task(task.id))

    assert result.status == "completed"
    assert history[0]["quality_warnings"] == [
        "内容较短，请确认提取结果是否完整。",
        "表格或幻灯片文字较少，请确认结构信息是否完整。",
    ]
