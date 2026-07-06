"""Persistência de conversas do WhatsApp em MongoDB."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from whatsapp_auto_downloader import normalize_phone_digits, now_iso, safe_id


@dataclass(frozen=True)
class MongoSettings:
    uri: str
    database: str
    conversations_collection: str = "conversations"
    messages_collection: str = "conversation_messages"


def load_mongo_settings() -> MongoSettings:
    return MongoSettings(
        uri=os.getenv("MONGODB_URI", "").strip(),
        database=os.getenv("MONGODB_DB", "whatsapp").strip() or "whatsapp",
        conversations_collection=os.getenv("MONGODB_CONVERSATIONS_COLLECTION", "conversations").strip()
        or "conversations",
        messages_collection=os.getenv("MONGODB_MESSAGES_COLLECTION", "conversation_messages").strip()
        or "conversation_messages",
    )


def conversation_key_for(*, phone: str | None = None, target_id: str | None = None) -> str:
    digits = normalize_phone_digits(phone or "")
    if digits:
        return digits
    if target_id:
        return safe_id(target_id)
    raise ValueError("Informe phone ou target_id.")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ConversationStore:
    """Grava e recupera conversas por telefone ou target_id."""

    def __init__(self, settings: MongoSettings) -> None:
        self.settings = settings
        self._memory: dict[str, Any] | None = None
        self._client: Any = None
        self._db: Any = None

        if settings.uri == "memory://":
            self._memory = {"conversations": {}, "messages": {}}
            return
        if not settings.uri:
            return

        from pymongo import MongoClient
        from pymongo.errors import PyMongoError

        self._PyMongoError = PyMongoError
        self._client = MongoClient(settings.uri, serverSelectionTimeoutMS=5000)
        self._db = self._client[settings.database]
        self._ensure_indexes()

    @property
    def enabled(self) -> bool:
        return self._memory is not None or self._db is not None

    def _ensure_indexes(self) -> None:
        if self._db is None:
            return
        messages = self._db[self.settings.messages_collection]
        messages.create_index([("conversation_key", 1), ("hash", 1)], unique=True)
        messages.create_index([("conversation_key", 1), ("captured_at", 1)])
        conversations = self._db[self.settings.conversations_collection]
        conversations.create_index("conversation_key", unique=True)
        conversations.create_index("phone")
        conversations.create_index("target_id")

    def _conversations_col(self) -> Any:
        if self._memory is not None:
            return self._memory["conversations"]
        if self._db is None:
            raise RuntimeError("MongoDB não configurado. Defina MONGODB_URI no .env.")
        return self._db[self.settings.conversations_collection]

    def _messages_col(self) -> Any:
        if self._memory is not None:
            return self._memory["messages"]
        if self._db is None:
            raise RuntimeError("MongoDB não configurado. Defina MONGODB_URI no .env.")
        return self._db[self.settings.messages_collection]

    def ping(self) -> dict[str, Any]:
        if self._memory is not None:
            return {"ok": True, "backend": "memory"}
        if self._db is None:
            return {"ok": False, "error": "MONGODB_URI não configurado."}
        try:
            self._client.admin.command("ping")
            return {"ok": True, "backend": "mongodb", "database": self.settings.database}
        except self._PyMongoError as exc:
            return {"ok": False, "error": str(exc)}

    def save_conversation_messages(
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
            return {
                "conversation_key": conversation_key,
                "inserted": 0,
                "updated": 0,
                "total_in_batch": 0,
            }

        synced_at = now_iso()
        inserted = 0
        updated = 0

        if self._memory is not None:
            bucket = self._messages_col().setdefault(conversation_key, {})
            for msg in messages:
                msg_hash = str(msg.get("hash") or "")
                if not msg_hash:
                    continue
                doc = {
                    "conversation_key": conversation_key,
                    "target_id": target_id,
                    "target_type": target_type,
                    "target_phone": phone,
                    "target_name": target_name,
                    "hash": msg_hash,
                    "direction": msg.get("direction"),
                    "sender": msg.get("sender"),
                    "timestamp_text": msg.get("timestamp_text"),
                    "text": msg.get("text"),
                    "captured_at": msg.get("captured_at") or synced_at,
                    "position": msg.get("position"),
                    "synced_at": synced_at,
                }
                if msg_hash in bucket:
                    bucket[msg_hash] = {**bucket[msg_hash], **doc}
                    updated += 1
                else:
                    bucket[msg_hash] = doc
                    inserted += 1
            total = len(bucket)
            self._conversations_col()[conversation_key] = {
                "conversation_key": conversation_key,
                "target_id": target_id,
                "target_type": target_type,
                "phone": phone,
                "target_name": target_name,
                "updated_at": synced_at,
                "last_sync_at": synced_at,
                "message_count": total,
            }
            return {
                "conversation_key": conversation_key,
                "inserted": inserted,
                "updated": updated,
                "total_in_batch": len(messages),
                "message_count": total,
            }

        from pymongo import UpdateOne

        ops: list[UpdateOne] = []
        for msg in messages:
            msg_hash = str(msg.get("hash") or "")
            if not msg_hash:
                continue
            doc = {
                "conversation_key": conversation_key,
                "target_id": target_id,
                "target_type": target_type,
                "target_phone": phone,
                "target_name": target_name,
                "hash": msg_hash,
                "direction": msg.get("direction"),
                "sender": msg.get("sender"),
                "timestamp_text": msg.get("timestamp_text"),
                "text": msg.get("text"),
                "captured_at": msg.get("captured_at") or synced_at,
                "position": msg.get("position"),
                "synced_at": synced_at,
            }
            ops.append(
                UpdateOne(
                    {"conversation_key": conversation_key, "hash": msg_hash},
                    {"$set": doc, "$setOnInsert": {"created_at": synced_at}},
                    upsert=True,
                )
            )

        if ops:
            result = self._messages_col().bulk_write(ops, ordered=False)
            inserted = result.upserted_count
            updated = result.modified_count

        message_count = self._messages_col().count_documents({"conversation_key": conversation_key})
        meta = {
            "conversation_key": conversation_key,
            "target_id": target_id,
            "target_type": target_type,
            "phone": phone,
            "target_name": target_name,
            "updated_at": synced_at,
            "last_sync_at": synced_at,
            "message_count": message_count,
        }
        self._conversations_col().update_one(
            {"conversation_key": conversation_key},
            {"$set": meta, "$setOnInsert": {"created_at": synced_at}},
            upsert=True,
        )
        return {
            "conversation_key": conversation_key,
            "inserted": inserted,
            "updated": updated,
            "total_in_batch": len(messages),
            "message_count": message_count,
        }

    def get_conversation(
        self,
        *,
        phone: str | None = None,
        target_id: str | None = None,
        conversation_key: str | None = None,
        limit: int = 500,
        skip: int = 0,
    ) -> dict[str, Any]:
        key = conversation_key or conversation_key_for(phone=phone, target_id=target_id)

        if self._memory is not None:
            meta = self._conversations_col().get(key)
            if not meta:
                return {"ok": False, "error": "Conversa não encontrada.", "conversation_key": key}
            bucket = self._messages_col().get(key, {})
            messages = sorted(
                bucket.values(),
                key=lambda item: (
                    str(item.get("captured_at") or ""),
                    (item.get("position") or {}).get("y") or 0,
                ),
            )
            page = messages[skip : skip + limit]
            return {
                "ok": True,
                "conversation": meta,
                "messages": page,
                "total_messages": len(messages),
                "limit": limit,
                "skip": skip,
            }

        meta = self._conversations_col().find_one({"conversation_key": key}, {"_id": 0})
        if not meta:
            return {"ok": False, "error": "Conversa não encontrada.", "conversation_key": key}

        cursor = (
            self._messages_col()
            .find({"conversation_key": key}, {"_id": 0})
            .sort([("captured_at", 1), ("position.y", 1)])
            .skip(max(0, skip))
            .limit(max(1, min(limit, 5000)))
        )
        messages = list(cursor)
        total = self._messages_col().count_documents({"conversation_key": key})
        return {
            "ok": True,
            "conversation": meta,
            "messages": messages,
            "total_messages": total,
            "limit": limit,
            "skip": skip,
        }

    def list_conversations(self, *, limit: int = 100) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        if self._memory is not None:
            items = list(self._conversations_col().values())
            items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
            return {"ok": True, "total": len(items), "conversations": items[:limit]}

        cursor = (
            self._conversations_col()
            .find({}, {"_id": 0})
            .sort("updated_at", -1)
            .limit(limit)
        )
        items = list(cursor)
        total = self._conversations_col().count_documents({})
        return {"ok": True, "total": total, "conversations": items}

    def get_saved_hashes(self, conversation_key: str) -> set[str]:
        if self._memory is not None:
            bucket = self._messages_col().get(conversation_key, {})
            return {str(item.get("hash")) for item in bucket.values() if item.get("hash")}

        cursor = self._messages_col().find(
            {"conversation_key": conversation_key},
            {"hash": 1, "_id": 0},
        )
        return {str(doc.get("hash")) for doc in cursor if doc.get("hash")}


_store_cache: ConversationStore | None = None


def get_conversation_store(*, force_new: bool = False) -> ConversationStore:
    global _store_cache
    if force_new or _store_cache is None:
        _store_cache = ConversationStore(load_mongo_settings())
    return _store_cache


def reset_conversation_store_cache_for_tests() -> None:
    global _store_cache
    _store_cache = None
