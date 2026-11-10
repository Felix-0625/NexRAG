"""BGE-M3 lexical weights from the pinned official encoder and sparse head."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
_HEAD_SHA256 = "45c93804d2142b8f6d7ec6914ae23a1eee9c6a1d27d83d908a20d2afb3595ad9"
_FILES = (
    "onnx/model.onnx",
    "onnx/model.onnx_data",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sparse_linear.pt",
)


class BgeM3SparseEncoder:
    """Apply ReLU sparse projection and max-pool positive weights per token ID.

    The two fixed tensors are read as data from the pinned official archive.
    Model loading never executes its pickle. The ONNX backbone is the official
    export of the same revision and exposes the last token hidden states.
    """

    def __init__(self, model_path: str = "", threads: int = 2, providers=None):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        if model_path:
            root = Path(model_path).resolve(strict=True)
        else:
            from huggingface_hub import hf_hub_download

            downloaded = {
                name: Path(hf_hub_download("BAAI/bge-m3", name, revision=REVISION))
                for name in _FILES
            }
            root = downloaded["tokenizer.json"].parent
        head = (root / "sparse_linear.pt").read_bytes()
        if hashlib.sha256(head).hexdigest() != _HEAD_SHA256:
            raise ValueError("BGE-M3 sparse head differs from the pinned official revision")
        with zipfile.ZipFile(root / "sparse_linear.pt") as archive:
            if archive.read("sparse_linear/byteorder") != b"little":
                raise ValueError("Unsupported BGE-M3 sparse tensor byte order")
            weight = archive.read("sparse_linear/data/0")
            bias = archive.read("sparse_linear/data/1")
        if len(weight) != 2048 or len(bias) != 2:
            raise ValueError("Unexpected BGE-M3 sparse head tensor dimensions")
        self.weight = np.frombuffer(weight, dtype="<f2").astype(np.float32)
        self.bias = np.frombuffer(bias, dtype="<f2").astype(np.float32)[0]
        self.tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=8192)
        self.tokenizer.no_padding()
        config = json.loads((root / "special_tokens_map.json").read_text("utf-8"))
        self.excluded_ids = set()
        for key in ("cls_token", "eos_token", "pad_token", "unk_token"):
            special = config.get(key)
            if isinstance(special, dict):
                special = special["content"]
            if special:
                self.excluded_ids.add(self.tokenizer.token_to_id(special))
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        selected = providers or ["CPUExecutionProvider"]
        if any(
            (p[0] if isinstance(p, tuple) else p) not in ort.get_available_providers()
            for p in selected
        ):
            raise ValueError("Requested BGE-M3 execution provider is unavailable")
        self.session = ort.InferenceSession(
            str(root / "onnx/model.onnx"),
            sess_options=options,
            providers=selected,
        )
        outputs = {output.name for output in self.session.get_outputs()}
        if "token_embeddings" not in outputs:
            raise ValueError("BGE-M3 ONNX model must expose original token hidden states")
        self.lock = Lock()
        self.model_path = root
        self.execution_providers = self.session.get_providers()

    def embed(self, texts):
        """Return one embedding per input text in input order."""
        import numpy as np

        # Bound activation memory independently of the caller's input batch.
        for text in texts:
            with self.lock:
                encoding = self.tokenizer.encode(text)
                ids = np.asarray(encoding.ids, dtype=np.int64)
                mask = np.asarray(encoding.attention_mask, dtype=np.int64)
                hidden = self.session.run(
                    ["token_embeddings"],
                    {"input_ids": ids[None, :], "attention_mask": mask[None, :]},
                )[0][0]
                weights = np.maximum(hidden @ self.weight + self.bias, 0)
            lexical = {}
            for token_id, value, visible in zip(ids, weights, mask, strict=True):
                token_id = int(token_id)
                if visible and token_id not in self.excluded_ids and value > 0:
                    lexical[token_id] = max(lexical.get(token_id, 0.0), float(value))
            ordered = sorted(lexical)
            yield SimpleNamespace(
                indices=np.asarray(ordered, dtype=np.int32),
                values=np.asarray([lexical[index] for index in ordered], dtype=np.float32),
            )

    def query_embed(self, texts):
        """Yield sparse query vectors using the shared BGE-M3 model."""
        return self.embed(texts)
