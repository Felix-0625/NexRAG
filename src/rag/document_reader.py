"""原生本地摄入入口使用的进程内文本提取。"""

from pathlib import Path
from zipfile import ZipFile

# 上传文件不可信：defusedxml 拒绝实体膨胀（billion laughs）与外部实体（XXE）。
from defusedxml import ElementTree


def read_document(path: Path) -> str:
    """按扩展名在进程内提取文档文本，支持 PDF、DOCX 和 UTF-8 文本类格式。

    DOCX 的 XML 超过 50 MB 或扩展名不受支持时抛出 ValueError；XML 解析使用 defusedxml 防实体攻击。
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
