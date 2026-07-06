"""Testes de busca semântica (pgvector / memory backend)."""

from __future__ import annotations

import pytest

from conversation_store import ConversationStore, MongoSettings, get_conversation_store
from semantic_store import (
    SemanticSearchStore,
    SemanticSettings,
    get_semantic_store,
    reindex_conversation_from_store,
)


def _memory_store() -> SemanticSearchStore:
    return SemanticSearchStore(SemanticSettings(uri="memory://", embedding_dim=384))


def test_semantic_index_and_search_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMANTIC_EMBEDDING_PROVIDER", "hash")
    store = _memory_store()
    assert store.enabled is True

    messages = [
        {
            "hash": "m1",
            "direction": "incoming",
            "sender": "Cliente",
            "timestamp_text": "10:00",
            "text": "Preciso do boleto atualizado do pedido 123",
            "captured_at": "2026-07-06T10:00:00",
        },
        {
            "hash": "m2",
            "direction": "outgoing",
            "sender": "Eu",
            "timestamp_text": "10:01",
            "text": "Segue a chave pix para pagamento",
            "captured_at": "2026-07-06T10:01:00",
        },
    ]
    store.index_messages(
        conversation_key="5511999999999",
        target_id="numero_teste",
        target_type="phone",
        phone="5511999999999",
        target_name="Cliente",
        messages=messages,
    )

    outcome = store.search(
        query="Preciso do boleto atualizado do pedido 123",
        conversation_key="5511999999999",
        min_score=0.1,
    )
    assert outcome["ok"] is True
    assert outcome["total"] >= 1
    assert "boleto" in outcome["results"][0]["text"].lower()


def test_reindex_from_mongo_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMANTIC_EMBEDDING_PROVIDER", "hash")
    mongo = ConversationStore(MongoSettings(uri="memory://", database="test"))
    mongo.save_conversation_messages(
        conversation_key="5562999000000",
        target_id="numero_teste",
        target_type="phone",
        phone="5562999000000",
        target_name="Teste",
        messages=[
            {
                "hash": "r1",
                "direction": "incoming",
                "sender": "A",
                "timestamp_text": "11:00",
                "text": "Quando chega meu produto?",
                "captured_at": "2026-07-06T11:00:00",
            }
        ],
    )

    semantic = _memory_store()
    outcome = reindex_conversation_from_store(
        phone="5562999000000",
        conversation_store=mongo,
        semantic_store=semantic,
    )
    assert outcome.get("ok") is True
    assert outcome.get("indexed", 0) >= 1

    search = semantic.search(query="Quando chega meu produto?", phone="5562999000000", min_score=0.1)
    assert search["ok"] is True
    assert search["total"] >= 1


def test_semantic_status_api(client) -> None:
    response = client.get("/api/semantic/status")
    assert response.status_code == 200
    body = response.get_json()
    assert body["configured"] is True
    assert body["ok"] is True


def test_semantic_search_api(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEMANTIC_EMBEDDING_PROVIDER", "hash")
    semantic = get_semantic_store(force_new=True)
    semantic.index_messages(
        conversation_key="5511888888888",
        target_id="n1",
        target_type="phone",
        phone="5511888888888",
        target_name="Maria",
        messages=[
            {
                "hash": "api1",
                "text": "Você tem desconto para pagamento à vista?",
                "direction": "incoming",
                "sender": "Maria",
                "timestamp_text": "09:00",
                "captured_at": "2026-07-06T09:00:00",
            }
        ],
    )

    response = client.post(
        "/api/semantic/search",
        json={"query": "Você tem desconto para pagamento à vista?", "phone": "5511888888888", "min_score": 0.1},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["total"] >= 1
