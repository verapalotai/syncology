"""Unified interface over sentence-transformers embedding models.

Wraps several open embedders behind one ``encode`` so the food-reconciliation
benchmark can compare them fairly. Instruction-tuned models (Qwen3-Embedding) get
their task instruction on the query side only; the corpus (USDA descriptions) is
always embedded plain — the standard asymmetric bi-encoder setup for retrieval.
Runs on Apple MPS when available.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Retrieval task instruction for instruction-tuned query encoders.
_TASK = "Given a food product name, retrieve the matching generic food from a nutrition database"


@dataclass(frozen=True)
class EmbedderSpec:
    name: str
    model_id: str
    instruct: bool  # prepend the task instruction on the query side (Qwen3-style)
    params: str  # rough size, for the report


SPECS: dict[str, EmbedderSpec] = {
    "bge-m3": EmbedderSpec("bge-m3", "BAAI/bge-m3", False, "0.6B"),
    "qwen3-0.6b": EmbedderSpec("qwen3-0.6b", "Qwen/Qwen3-Embedding-0.6B", True, "0.6B"),
    "qwen3-4b": EmbedderSpec("qwen3-4b", "Qwen/Qwen3-Embedding-4B", True, "4B"),
    "qwen3-8b": EmbedderSpec("qwen3-8b", "Qwen/Qwen3-Embedding-8B", True, "8B"),
    "harrier-0.6b": EmbedderSpec("harrier-0.6b", "microsoft/harrier-oss-v1-0.6b", True, "0.6B"),
}


def _device() -> str:
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    return "cuda" if torch.cuda.is_available() else "cpu"


class Embedder:
    def __init__(self, spec: EmbedderSpec, batch_size: int = 64):
        from sentence_transformers import SentenceTransformer

        self.spec = spec
        self.batch_size = batch_size
        self.model = SentenceTransformer(
            spec.model_id, device=_device(), trust_remote_code=True,
            model_kwargs={"torch_dtype": "float16"},
        )

    def _encode(self, texts: list[str], instruct: bool) -> np.ndarray:
        prompt = f"Instruct: {_TASK}\nQuery: " if (instruct and self.spec.instruct) else None
        emb = self.model.encode(
            texts, batch_size=self.batch_size, prompt=prompt,
            normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False,
        )
        return emb.astype(np.float32)

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts, instruct=True)

    def encode_docs(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts, instruct=False)
