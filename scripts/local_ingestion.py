"""Reuse upstream ingestion with an in-process document converter.

Only native startup installs this converter; upstream files remain untouched. PDF, DOCX,
and UTF-8 text formats need no sidecar process.
"""

from pathlib import Path

from haystack import Document, component

from src.rag.document_reader import read_document


@component
class NativeConverter:
    """Convert native local documents through the existing ingestion component contract."""

    @component.output_types(documents=list[Document])
    def run(self, sources: list[str | Path], meta: dict | list[dict] | None = None):
        """Convert files to Documents with shared or per-file metadata; require OCR for empty
        scans.
        """
        documents = []
        for index, source in enumerate(sources):
            path = Path(source)
            metadata = dict(meta[index] if isinstance(meta, list) else meta or {})
            text = read_document(path)
            if not text.strip():
                raise ValueError(
                    "No extractable text. Scanned documents require OCR before upload."
                )
            documents.append(Document(content=text, meta=metadata))
        return {"documents": documents}


# Upstream initializes pipelines during lifespan, after this factory override.
from app.pipelines import indexing  # noqa: E402

_original_converter = indexing.build_converter


def _build_converter(settings, engine_override=None):
    if (engine_override or settings.extraction_engine).lower() == "native":
        return NativeConverter()
    return _original_converter(settings, engine_override)


indexing.build_converter = _build_converter

from app.routes import extract  # noqa: E402

extract.build_converter = _build_converter

from app.main import app  # noqa: E402, F401
