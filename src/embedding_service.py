"""Geração de embeddings para busca semântica de mensagens."""

from __future__ import annotations

import hashlib
import math
import os
import struct
from functools import lru_cache
from typing import Any


def load_embedding_settings() -> dict[str, Any]:
    return {
        "provider": (os.getenv("SEMANTIC_EMBEDDING_PROVIDER") or "fastembed").strip().lower(),
        "model": (os.getenv("SEMANTIC_EMBEDDING_MODEL") or "").strip(),
        "dimensions": int(os.getenv("SEMANTIC_EMBEDDING_DIM") or "384"),
        "openai_api_key": (os.getenv("OPENAI_API_KEY") or "").strip(),
        "openai_base_url": (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip(),
        "ollama_url": (os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").strip(),
        "ollama_model": (os.getenv("OLLAMA_EMBEDDING_MODEL") or "nomic-embed-text").strip(),
    }


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm <= 0:
        return vector
    return [x / norm for x in vector]


def _hash_embed(text: str, *, dimensions: int) -> list[float]:
    """Embedding determinístico para testes (não semântico de verdade)."""
    seed = (text or "").strip().lower()
    raw: list[float] = []
    counter = 0
    while len(raw) < dimensions:
        digest = hashlib.sha256(f"{seed}:{counter}".encode("utf-8")).digest()
        for offset in range(0, len(digest) - 3, 4):
            chunk = digest[offset : offset + 4]
            value = struct.unpack("!i", chunk)[0]
            raw.append(value / 2_147_483_648.0)
            if len(raw) >= dimensions:
                break
        counter += 1
    return _normalize(raw[:dimensions])


@lru_cache(maxsize=1)
def _fastembed_model(model_name: str):
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=model_name)


def _fastembed_vectors(texts: list[str], *, model_name: str) -> list[list[float]]:
    resolved = model_name or "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    model = _fastembed_model(resolved)
    return [list(vec) for vec in model.embed(texts)]


def _openai_vectors(texts: list[str], *, settings: dict[str, Any]) -> list[list[float]]:
    import httpx

    api_key = settings["openai_api_key"]
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada para embeddings OpenAI.")

    model = settings["model"] or "text-embedding-3-small"
    url = f"{settings['openai_base_url'].rstrip('/')}/embeddings"
    response = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"input": texts, "model": model},
        timeout=60.0,
    )
    response.raise_for_status()
    payload = response.json()
    data = sorted(payload.get("data") or [], key=lambda item: item.get("index", 0))
    return [list(item.get("embedding") or []) for item in data]


def _ollama_vectors(texts: list[str], *, settings: dict[str, Any]) -> list[list[float]]:
    import httpx

    model = settings["ollama_model"]
    base = settings["ollama_url"].rstrip("/")
    vectors: list[list[float]] = []
    for text in texts:
        response = httpx.post(
            f"{base}/api/embeddings",
            json={"model": model, "prompt": text},
            timeout=120.0,
        )
        response.raise_for_status()
        embedding = response.json().get("embedding") or []
        vectors.append(list(embedding))
    return vectors


def embed_texts(texts: list[str]) -> list[list[float]]:
    cleaned = [(text or "").strip() for text in texts]
    if not cleaned:
        return []

    settings = load_embedding_settings()
    provider = settings["provider"]

    if provider == "hash":
        dim = settings["dimensions"]
        return [_hash_embed(text, dimensions=dim) for text in cleaned]

    if provider == "fastembed":
        try:
            return _fastembed_vectors(cleaned, model_name=settings["model"])
        except ImportError as exc:
            raise RuntimeError(
                "Instale fastembed: pip install fastembed "
                "(ou use SEMANTIC_EMBEDDING_PROVIDER=ollama|openai|hash)."
            ) from exc

    if provider == "openai":
        return _openai_vectors(cleaned, settings=settings)

    if provider == "ollama":
        return _ollama_vectors(cleaned, settings=settings)

    raise RuntimeError(f"SEMANTIC_EMBEDDING_PROVIDER desconhecido: {provider}")


def embed_text(text: str) -> list[float]:
    vectors = embed_texts([text])
    return vectors[0] if vectors else []
