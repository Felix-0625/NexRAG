from zipfile import ZipFile

import pytest

from src.rag.document_reader import read_document


def test_utf8_text_and_bom_are_preserved(tmp_path):
    path = tmp_path / "knowledge.md"
    path.write_text("# 知识库\n业务说明", encoding="utf-8-sig")
    assert read_document(path) == "# 知识库\n业务说明"


def test_docx_keeps_paragraph_and_table_cell_text(tmp_path):
    path = tmp_path / "document.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>说明</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>数值</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:body></w:document>",
        )
    assert read_document(path) == "说明\n数值"


def test_docx_with_entity_expansion_is_rejected(tmp_path):
    """Entity expansion must not turn an upload into a memory bomb."""
    path = tmp_path / "bomb.docx"
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", bomb)
    with pytest.raises(Exception, match="(?i)entit"):
        read_document(path)


def test_binary_format_is_rejected_instead_of_indexing_gibberish(tmp_path):
    path = tmp_path / "data.xlsx"
    path.write_bytes(b"PK\x03\x04")
    with pytest.raises(ValueError, match="does not support .xlsx"):
        read_document(path)
