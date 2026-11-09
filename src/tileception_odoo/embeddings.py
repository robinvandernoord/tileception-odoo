"""Offline-only inference; setup is the sole model download path."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.request import urlopen

import numpy as np

MODEL = "ibm-granite/granite-embedding-97m-multilingual-r2"
REVISION = "835ad14087e140460703cf0fae09f97d469d65c2"
IDENTITY = f"{MODEL}@{REVISION}:cls:384:chunks-v1"
FILES = {
    "onnx/model.onnx": "68e592b160673d30250824c1116bc6ab33f70efb22b97c9e1d7ce1e69c1c9d70",
    "tokenizer.json": "4f2842d568e2724370aec203652a42ac783c7937f8347a1a2cc7506d71f1582f",
}


def download(directory: Path) -> None:
    """Download pinned public weights, never sending any Odoo content."""
    for name, expected in FILES.items():
        target = directory / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.is_file():
            with target.open("rb") as existing:
                if hashlib.file_digest(existing, "sha256").hexdigest() == expected:
                    continue
        temporary = target.with_suffix(".download")
        try:
            digest = hashlib.sha256()
            with urlopen(f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}", timeout=60) as response:
                with temporary.open("wb") as output:
                    while block := response.read(1024 * 1024):
                        digest.update(block)
                        output.write(block)
            if digest.hexdigest() != expected:
                raise ValueError(f"Model checksum mismatch: {name}")
            temporary.chmod(0o600)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    (directory / "identity.json").write_text(json.dumps({"model": MODEL, "revision": REVISION}))


class Embedder:
    def __init__(self, directory: Path, threads: int = 2):
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        import onnxruntime as ort
        from tokenizers import Tokenizer

        if not all((directory / name).is_file() for name in FILES):
            raise ValueError("Embedding model is missing; run tileception-odoo setup-model")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.session = ort.InferenceSession(
            str(directory / "onnx/model.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.chunk_tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.chunk_tokenizer.no_truncation()
        self.chunk_tokenizer.no_padding()
        self.tokenizer.enable_truncation(max_length=512)
        # Preserve the model's end-of-text padding ID and right-padding convention.
        padding = self.tokenizer.padding
        self.tokenizer.enable_padding(
            pad_id=padding["pad_id"],
            pad_token=padding["pad_token"],
            direction=padding["direction"],
        )

    def chunks(self, title: str, description: str) -> list[str]:
        # Token boundaries bound inference cost even for long words, code and CJK.
        title_tokens = self.chunk_tokenizer.encode(title, add_special_tokens=False).ids[:96]
        header = self.chunk_tokenizer.decode(title_tokens)
        ids = self.chunk_tokenizer.encode(description, add_special_tokens=False).ids
        if not ids:
            return [header]
        return [
            f"{header}\n{self.chunk_tokenizer.decode(ids[start : start + 320])}"
            for start in range(0, len(ids), 280)
        ]

    def encode(self, texts: list[str]) -> np.ndarray:
        encoded = self.tokenizer.encode_batch(texts)
        available = {
            "input_ids": np.array([item.ids for item in encoded], dtype=np.int64),
            "attention_mask": np.array([item.attention_mask for item in encoded], dtype=np.int64),
            "token_type_ids": np.array([item.type_ids for item in encoded], dtype=np.int64),
        }
        inputs = {item.name: available[item.name] for item in self.session.get_inputs()}
        output = self.session.run(None, inputs)[0]
        vectors = output[:, 0, :] if output.ndim == 3 else output
        if vectors.shape != (len(texts), 384) or not np.isfinite(vectors).all():
            raise ValueError("Embedding model returned invalid 384-dimensional vectors")
        vectors = vectors.astype("<f4")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if (norms == 0).any():
            raise ValueError("Embedding model returned a zero vector")
        return vectors / norms
