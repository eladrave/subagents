"""Local multilingual E5 embeddings backed by FastEmbed."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from fastembed import TextEmbedding
from fastembed.common.model_description import ModelSource, PoolingType


E5_MODEL_ID = "intfloat/multilingual-e5-small"
E5_DIMENSION = 384


class Embedder(Protocol):
    model_id: str
    dimension: int

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def count(self, text: str) -> int: ...


class FastEmbedE5:
    model_id = E5_MODEL_ID
    dimension = E5_DIMENSION

    def __init__(self, state_root: Path) -> None:
        models_root = Path(state_root) / "models"
        models_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        registered = {
            str(item.get("model", "")).casefold()
            for item in TextEmbedding.list_supported_models()
        }
        if self.model_id.casefold() not in registered:
            TextEmbedding.add_custom_model(
                model=self.model_id,
                pooling=PoolingType.MEAN,
                normalization=True,
                sources=ModelSource(hf=self.model_id),
                dim=self.dimension,
                model_file="onnx/model.onnx",
            )
        self._embedding = TextEmbedding(
            model_name=self.model_id,
            cache_dir=str(models_root),
        )

    @staticmethod
    def _vectors(values) -> list[list[float]]:
        vectors: list[list[float]] = []
        for value in values:
            vector = value.tolist() if hasattr(value, "tolist") else list(value)
            vectors.append([float(item) for item in vector])
        return vectors

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._vectors(
            self._embedding.embed([f"passage: {text}" for text in texts])
        )

    def embed_query(self, text: str) -> list[float]:
        vectors = self._vectors(self._embedding.embed([f"query: {text}"]))
        return vectors[0]

    def count(self, text: str) -> int:
        encoded = self._embedding.model.tokenizer.encode(text)
        return len(encoded.ids)
