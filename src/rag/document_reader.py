"""In-process extraction for native local ingestion."""

from pathlib import Path
from zipfile import ZipFile

# Uploads are untrusted: defusedxml blocks entity expansion and external entities.
from defusedxml import ElementTree


def read_document(path: Path) -> str:
    """Extract PDF, DOCX, and UTF-8 text by extension with hardened XML parsing.

    Reject unsupported extensions or DOCX XML over 50 MB; defusedxml blocks entity attacks.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        return "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if suffix == ".docx":
        with ZipFile(path) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 50 * 1024 * 1024:
                raise ValueError("DOCX document XML exceeds the 50 MB native parser limit")
            root = ElementTree.fromstring(archive.read(info))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        return "\n".join("".join(p.itertext()) for p in root.findall(".//w:p", ns))
    if suffix not in {".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".xml", ".log"}:
        raise ValueError(
            f"Native ingestion does not support {suffix}; use PDF, DOCX or UTF-8 text."
        )
    return path.read_text(encoding="utf-8-sig")
