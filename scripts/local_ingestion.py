"""沿用现有摄取流水线，并换成进程内的文档转换器。

该入口不改动上游服务；只有原生启动才会使用此转换器。
PDF、DOCX 和 UTF-8 文本/Markdown/CSV 无需额外的 sidecar 进程。
"""

from pathlib import Path

from haystack import Document, component

from src.rag.document_reader import read_document


@component
class NativeConverter:
    @component.output_types(documents=list[Document])
    def run(self, sources: list[str | Path], meta: dict | list[dict] | None = None):
        """逐个读取文件并转为 Document（meta 可为单个字典或按序对应的列表）；提取不到文本时抛出 ValueError，扫描件需先 OCR。"""
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


# 上游流水线在 lifespan 中初始化，晚于这里对工厂函数的覆盖。
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
