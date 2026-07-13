from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from markitdown import MarkItDown


SUPPORTED_DOCUMENT_EXTENSIONS = {
    ".md", ".txt", ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".xls", ".xlsx", ".html", ".csv", ".json",
}
PLAIN_TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json"}


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    source_format: str
    metadata: dict[str, Any]


class DocumentExtractionError(Exception):
    pass


class TemporaryExtractionError(DocumentExtractionError):
    pass


class UnsupportedDocumentError(DocumentExtractionError):
    pass


class EncryptedDocumentError(DocumentExtractionError):
    pass


class DamagedDocumentError(DocumentExtractionError):
    pass


class DocumentExtractor(Protocol):
    def extract(self, path: Path) -> ExtractedDocument: ...


class MarkItDownDocumentExtractor:
    def __init__(self) -> None:
        self._converter = MarkItDown()

    def extract(self, path: Path) -> ExtractedDocument:
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
            raise UnsupportedDocumentError()
        try:
            if suffix in PLAIN_TEXT_EXTENSIONS:
                text = path.read_text(encoding="utf-8-sig")
                adapter = "plain-text"
            else:
                result = self._converter.convert(str(path))
                text = result.text_content or ""
                adapter = "markitdown"
        except PermissionError as exc:
            raise TemporaryExtractionError() from exc
        except OSError as exc:
            raise TemporaryExtractionError() from exc
        except Exception as exc:
            message = str(exc).casefold()
            if any(word in message for word in ("unsupported", "not supported", "unrecognized format")):
                raise UnsupportedDocumentError() from exc
            if any(word in message for word in ("password", "encrypted", "decrypt")):
                raise EncryptedDocumentError() from exc
            if any(word in message for word in ("corrupt", "damaged", "invalid file")):
                raise DamagedDocumentError() from exc
            raise DamagedDocumentError() from exc
        return ExtractedDocument(
            text=text,
            source_format=suffix.lstrip("."),
            metadata={"adapter": adapter, "source_suffix": suffix},
        )
