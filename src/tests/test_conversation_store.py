"""Testes do armazenamento de conversas em MongoDB."""

from __future__ import annotations

from conversation_store import ConversationStore, MongoSettings, conversation_key_for


def _sample_messages() -> list[dict]:
    return [
        {
            "hash": "abc123",
            "direction": "incoming",
            "sender": "Ana",
            "timestamp_text": "10:00",
            "text": "Olá",
            "captured_at": "2026-05-26T10:00:00",
            "position": {"y": 100},
        },
        {
            "hash": "def456",
            "direction": "outgoing",
            "sender": "Eu",
            "timestamp_text": "10:01",
            "text": "Oi",
            "captured_at": "2026-05-26T10:01:00",
            "position": {"y": 200},
        },
    ]


def test_conversation_key_prefers_phone() -> None:
    assert conversation_key_for(phone="5511999999999") == "5511999999999"


def test_save_and_get_conversation_memory_backend() -> None:
    store = ConversationStore(MongoSettings(uri="memory://", database="test"))
    messages = _sample_messages()
    stats = store.save_conversation_messages(
        conversation_key="5511999999999",
        target_id="numero_5511999999999",
        target_type="phone",
        phone="5511999999999",
        target_name="Ana",
        messages=messages,
    )
    assert stats["inserted"] == 2
    assert stats["message_count"] == 2

    outcome = store.get_conversation(phone="5511999999999")
    assert outcome["ok"] is True
    assert outcome["total_messages"] == 2
    assert outcome["messages"][0]["text"] == "Olá"

    # Upsert same hash updates instead of duplicating
    messages[0]["text"] = "Olá atualizado"
    stats2 = store.save_conversation_messages(
        conversation_key="5511999999999",
        target_id="numero_5511999999999",
        target_type="phone",
        phone="5511999999999",
        target_name="Ana",
        messages=[messages[0]],
    )
    assert stats2["updated"] == 1
    outcome2 = store.get_conversation(phone="5511999999999")
    texts = [item["text"] for item in outcome2["messages"]]
    assert "Olá atualizado" in texts
    assert outcome2["total_messages"] == 2


def test_list_conversations_memory_backend() -> None:
    store = ConversationStore(MongoSettings(uri="memory://", database="test"))
    store.save_conversation_messages(
        conversation_key="5511888888888",
        target_id="numero_5511888888888",
        target_type="phone",
        phone="5511888888888",
        target_name="Bob",
        messages=_sample_messages()[:1],
    )
    listed = store.list_conversations()
    assert listed["ok"] is True
    assert listed["total"] == 1
    assert listed["conversations"][0]["phone"] == "5511888888888"


def test_get_saved_hashes_memory_backend() -> None:
    store = ConversationStore(MongoSettings(uri="memory://", database="test"))
    messages = _sample_messages()
    store.save_conversation_messages(
        conversation_key="5511777777777",
        target_id="numero_5511777777777",
        target_type="phone",
        phone="5511777777777",
        target_name="Carla",
        messages=messages,
    )
    hashes = store.get_saved_hashes("5511777777777")
    assert hashes == {"abc123", "def456"}
