"""Busca semântica de mensagens com PostgreSQL + pgvector."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

from conversation_store import conversation_key_for
from embedding_service import embed_text, embed_texts, load_embedding_settings
from whatsapp_auto_downloader import normalize_phone_digits, now_iso, safe_id


@dataclass(frozen=True)
class SemanticSettings:
    uri: str
    embedding_dim: int


def load_semantic_settings() -> SemanticSettings:
    uri = (
        os.getenv("SEMANTIC_DB_URI")
        or os.getenv("DATABASE_URL")
        or os.getenv("POSTGRES_URI")
        or ""
    ).strip()
    return SemanticSettings(
        uri=uri,
        embedding_dim=int(os.getenv("SEMANTIC_EMBEDDING_DIM") or "384"),
    )


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


class SemanticSearchStore:
    """Indexa mensagens com embeddings e executa busca por similaridade."""

    def __init__(self, settings: SemanticSettings) -> None:
        self.settings = settings
        self._memory: dict[str, dict[str, Any]] | None = None
        self._conn: Any = None

        if settings.uri == "memory://":
            self._memory = {}
            return
        if not settings.uri:
            return

        import psycopg

        self._conn = psycopg.connect(settings.uri, autocommit=True)
        self._ensure_schema()

    @property
    def enabled(self) -> bool:
        return self._memory is not None or self._conn is not None

    def _ensure_schema(self) -> None:
        if self._conn is None:
            return
        dim = self.settings.embedding_dim
        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS semantic_messages (
                    id BIGSERIAL PRIMARY KEY,
                    conversation_key TEXT NOT NULL,
                    target_id TEXT,
                    target_type TEXT,
                    phone TEXT,
                    target_name TEXT,
                    message_hash TEXT NOT NULL,
                    direction TEXT,
                    sender TEXT,
                    timestamp_text TEXT,
                    text TEXT NOT NULL,
                    captured_at TIMESTAMPTZ,
                    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    embedding vector({dim}),
                    UNIQUE (conversation_key, message_hash)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_semantic_messages_conversation
                ON semantic_messages (conversation_key)
                """
            )
            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_semantic_messages_embedding
                ON semantic_messages USING hnsw (embedding vector_cosine_ops)
                """
            )

    def ping(self) -> dict[str, Any]:
        settings = load_embedding_settings()
        if self._memory is not None:
            return {
                "ok": True,
                "backend": "memory",
                "embedding_provider": settings["provider"],
                "embedding_dim": self.settings.embedding_dim,
            }
        if self._conn is None:
            return {"ok": False, "error": "SEMANTIC_DB_URI não configurada."}
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.execute("SELECT COUNT(*) FROM semantic_messages")
                total = cur.fetchone()[0]
            return {
                "ok": True,
                "backend": "postgres",
                "indexed_messages": int(total),
                "embedding_provider": settings["provider"],
                "embedding_dim": self.settings.embedding_dim,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def index_messages(
        self,
        *,
        conversation_key: str,
        target_id: str,
        target_type: str,
        phone: str | None,
        target_name: str | None,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not messages:
            return {"indexed": 0, "skipped": 0}

        texts: list[str] = []
        prepared: list[dict[str, Any]] = []
        for msg in messages:
            text = str(msg.get("text") or "").strip()
            msg_hash = str(msg.get("hash") or "")
            if not text or not msg_hash:
                continue
            prepared.append(msg)
            texts.append(text)

        if not prepared:
            return {"indexed": 0, "skipped": len(messages), "error": "Nenhuma mensagem com texto."}

        try:
            vectors = embed_texts(texts)
        except Exception as exc:
            return {"indexed": 0, "skipped": len(messages), "error": str(exc)}

        synced_at = now_iso()
        indexed = 0

        if self._memory is not None:
            bucket = self._memory.setdefault(conversation_key, {})
            for msg, vector in zip(prepared, vectors, strict=True):
                msg_hash = str(msg.get("hash"))
                bucket[msg_hash] = {
                    "conversation_key": conversation_key,
                    "target_id": target_id,
                    "target_type": target_type,
                    "phone": phone,
                    "target_name": target_name,
                    "message_hash": msg_hash,
                    "direction": msg.get("direction"),
                    "sender": msg.get("sender"),
                    "timestamp_text": msg.get("timestamp_text"),
                    "text": msg.get("text"),
                    "captured_at": msg.get("captured_at"),
                    "synced_at": synced_at,
                    "embedding": vector,
                }
                indexed += 1
            return {"indexed": indexed, "skipped": len(messages) - indexed, "conversation_key": conversation_key}

        from pgvector.psycopg import register_vector

        register_vector(self._conn)
        with self._conn.cursor() as cur:
            for msg, vector in zip(prepared, vectors, strict=True):
                cur.execute(
                    """
                    INSERT INTO semantic_messages (
                        conversation_key, target_id, target_type, phone, target_name,
                        message_hash, direction, sender, timestamp_text, text,
                        captured_at, synced_at, embedding
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s
                    )
                    ON CONFLICT (conversation_key, message_hash) DO UPDATE SET
                        target_id = EXCLUDED.target_id,
                        target_type = EXCLUDED.target_type,
                        phone = EXCLUDED.phone,
                        target_name = EXCLUDED.target_name,
                        direction = EXCLUDED.direction,
                        sender = EXCLUDED.sender,
                        timestamp_text = EXCLUDED.timestamp_text,
                        text = EXCLUDED.text,
                        captured_at = EXCLUDED.captured_at,
                        synced_at = EXCLUDED.synced_at,
                        embedding = EXCLUDED.embedding
                    """,
                    (
                        conversation_key,
                        target_id,
                        target_type,
                        phone,
                        target_name,
                        str(msg.get("hash")),
                        msg.get("direction"),
                        msg.get("sender"),
                        msg.get("timestamp_text"),
                        msg.get("text"),
                        msg.get("captured_at"),
                        synced_at,
                        vector,
                    ),
                )
                indexed += 1
        return {"indexed": indexed, "skipped": len(messages) - indexed, "conversation_key": conversation_key}

    def search(
        self,
        *,
        query: str,
        phone: str | None = None,
        target_id: str | None = None,
        conversation_key: str | None = None,
        limit: int = 20,
        min_score: float = 0.35,
    ) -> dict[str, Any]:
        cleaned_query = (query or "").strip()
        if not cleaned_query:
            return {"ok": False, "error": "Informe o texto da busca."}

        key = conversation_key
        if not key and (phone or target_id):
            key = conversation_key_for(phone=phone, target_id=target_id)

        try:
            query_vector = embed_text(cleaned_query)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        limit = max(1, min(limit, 100))

        if self._memory is not None:
            candidates: list[dict[str, Any]] = []
            buckets = (
                [self._memory.get(key, {})]
                if key
                else list(self._memory.values())
            )
            for bucket in buckets:
                for item in bucket.values():
                    score = _cosine_similarity(query_vector, item.get("embedding") or [])
                    if score >= min_score:
                        candidates.append({**item, "score": round(score, 4)})
            candidates.sort(key=lambda row: row.get("score") or 0, reverse=True)
            page = candidates[:limit]
            return {
                "ok": True,
                "query": cleaned_query,
                "conversation_key": key,
                "total": len(page),
                "results": [
                    {
                        "score": row["score"],
                        "conversation_key": row.get("conversation_key"),
                        "phone": row.get("phone"),
                        "target_name": row.get("target_name"),
                        "direction": row.get("direction"),
                        "sender": row.get("sender"),
                        "timestamp_text": row.get("timestamp_text"),
                        "text": row.get("text"),
                        "message_hash": row.get("message_hash"),
                        "captured_at": row.get("captured_at"),
                    }
                    for row in page
                ],
            }

        if self._conn is None:
            return {"ok": False, "error": "SEMANTIC_DB_URI não configurada."}

        from pgvector.psycopg import register_vector

        register_vector(self._conn)
        with self._conn.cursor() as cur:
            if key:
                cur.execute(
                    """
                    SELECT
                        conversation_key, phone, target_name, direction, sender,
                        timestamp_text, text, message_hash, captured_at,
                        1 - (embedding <=> %s) AS score
                    FROM semantic_messages
                    WHERE conversation_key = %s
                      AND 1 - (embedding <=> %s) >= %s
                    ORDER BY embedding <=> %s
                    LIMIT %s
                    """,
                    (query_vector, key, query_vector, min_score, query_vector, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT
                        conversation_key, phone, target_name, direction, sender,
                        timestamp_text, text, message_hash, captured_at,
                        1 - (embedding <=> %s) AS score
                    FROM semantic_messages
                    WHERE 1 - (embedding <=> %s) >= %s
                    ORDER BY embedding <=> %s
                    LIMIT %s
                    """,
                    (query_vector, query_vector, min_score, query_vector, limit),
                )
            rows = cur.fetchall()

        results = [
            {
                "score": round(float(row[9]), 4),
                "conversation_key": row[0],
                "phone": row[1],
                "target_name": row[2],
                "direction": row[3],
                "sender": row[4],
                "timestamp_text": row[5],
                "text": row[6],
                "message_hash": row[7],
                "captured_at": row[8].isoformat() if row[8] else None,
            }
            for row in rows
        ]
        return {
            "ok": True,
            "query": cleaned_query,
            "conversation_key": key,
            "total": len(results),
            "results": results,
        }

    def reindex_from_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        conversation_key: str,
        target_id: str,
        target_type: str,
        phone: str | None,
        target_name: str | None,
    ) -> dict[str, Any]:
        return self.index_messages(
            conversation_key=conversation_key,
            target_id=target_id,
            target_type=target_type,
            phone=phone,
            target_name=target_name,
            messages=messages,
        )


_store_cache: SemanticSearchStore | None = None


def get_semantic_store(*, force_new: bool = False) -> SemanticSearchStore:
    global _store_cache
    if force_new or _store_cache is None:
        _store_cache = SemanticSearchStore(load_semantic_settings())
    return _store_cache


def reset_semantic_store_cache_for_tests() -> None:
    global _store_cache
    _store_cache = None


def index_messages_for_search(
    *,
    phone: str | None = None,
    target_id: str | None = None,
    target_name: str | None = None,
    target_type: str = "phone",
    messages: list[dict[str, Any]],
    store: SemanticSearchStore | None = None,
) -> dict[str, Any]:
    semantic = store or get_semantic_store()
    if not semantic.enabled:
        return {"ok": False, "skipped": True, "error": "Busca semântica não configurada."}

    key = conversation_key_for(phone=phone, target_id=target_id)
    resolved_target_id = safe_id(target_id or f"numero_{normalize_phone_digits(phone or '')}")
    outcome = semantic.index_messages(
        conversation_key=key,
        target_id=resolved_target_id,
        target_type=target_type,
        phone=normalize_phone_digits(phone or "") or None,
        target_name=target_name,
        messages=messages,
    )
    return {"ok": "error" not in outcome, **outcome}


def reindex_conversation_from_store(
    *,
    phone: str | None = None,
    target_id: str | None = None,
    conversation_key: str | None = None,
    conversation_store: Any | None = None,
    semantic_store: SemanticSearchStore | None = None,
) -> dict[str, Any]:
    from conversation_store import get_conversation_store

    mongo = conversation_store or get_conversation_store()
    semantic = semantic_store or get_semantic_store()
    if not mongo.enabled:
        return {"ok": False, "error": "MongoDB não configurado."}
    if not semantic.enabled:
        return {"ok": False, "error": "SEMANTIC_DB_URI não configurada."}

    fetched = mongo.get_conversation(
        phone=phone,
        target_id=target_id,
        conversation_key=conversation_key,
        limit=5000,
    )
    if not fetched.get("ok"):
        return fetched

    meta = fetched.get("conversation") or {}
    return {"ok": True, **semantic.reindex_from_messages(
        fetched.get("messages") or [],
        conversation_key=str(meta.get("conversation_key") or conversation_key or ""),
        target_id=str(meta.get("target_id") or target_id or ""),
        target_type=str(meta.get("target_type") or "phone"),
        phone=meta.get("phone"),
        target_name=meta.get("target_name"),
    )}
