"""验证临时的 Pillow 安全覆盖版本，且不使用员工数据。

在项目的 Python 环境中运行，并需已安装 Pillow >=13。若要做隔离的覆盖测试，
把 Pillow 13 的安装目录放到 PYTHONPATH 最前面；结果会记录实际测试的版本。
BM42 权重必须已存在于 --cache-dir；本脚本离线运行。
"""

from __future__ import annotations

import argparse
import io
import json
from importlib.metadata import version
from pathlib import Path

import PIL
from fastembed import SparseTextEmbedding
from PIL import Image, ImageOps


def image_roundtrips() -> list[str]:
    """用生成的合成图像验证 Pillow 的多种格式编解码、EXIF 转正、缩放和 PDF 编码，返回已验证的操作名称。"""
    original = Image.new("RGB", (31, 19), (72, 140, 210))
    formats = ["PNG", "JPEG", "TIFF", "BMP", "WEBP"]
    for file_format in formats:
        data = io.BytesIO()
        original.save(data, format=file_format)
        data.seek(0)
        with Image.open(data) as restored:
            restored.load()
            assert restored.size == original.size
            assert restored.convert("RGB").getpixel((15, 9))[2] > 190
            processed = ImageOps.exif_transpose(restored).convert("RGB")
            processed = ImageOps.fit(processed, (24, 24), method=Image.Resampling.BICUBIC)
            assert processed.size == (24, 24)
    # PDF 视觉抽取器使用 Pillow 的本地图像编码器；这里用生成的页面图像来验证，
    # 而不是打开来源不可信的员工 PDF。
    pdf = io.BytesIO()
    original.save(pdf, format="PDF")
    assert pdf.getvalue().startswith(b"%PDF-")
    return [*formats, "PDF encoding", "EXIF transpose and resize"]


def main() -> None:
    """要求 Pillow 主版本不低于 13，执行图像往返与离线 BM42 稀疏嵌入检查，打印结果并可写入 --output。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    assert int(PIL.__version__.split(".")[0]) >= 13, "This check requires the patched Pillow"
    formats = image_roundtrips()
    model = SparseTextEmbedding(
        model_name="Qdrant/bm42-all-minilm-l6-v2-attentions",
        cache_dir=str(args.cache_dir.resolve()),
        local_files_only=True,
        threads=2,
    )
    documents = list(
        model.embed(["Synthetic business policy: authorised discount is six percent."])
    )
    queries = list(model.query_embed(["What is the authorised discount?"]))
    assert len(documents) == len(queries) == 1
    assert len(documents[0].indices) and len(queries[0].indices)
    assert set(documents[0].indices) & set(queries[0].indices)
    result = {
        "synthetic_only": True,
        "pillow_version": PIL.__version__,
        "fastembed_version": version("fastembed"),
        "image_operations": formats,
        "bm42_model": "Qdrant/bm42-all-minilm-l6-v2-attentions",
        "bm42_document_nonzero": len(documents[0].indices),
        "bm42_query_nonzero": len(queries[0].indices),
        "passed": True,
        "limits": [
            "No adversarial-image fuzzing",
            "No model-weight or library API compatibility guarantee beyond these exercised paths",
        ],
    }
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
