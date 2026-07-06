"""Testes da API Flask de conversas."""

from __future__ import annotations

import pytest

from conversation_store import get_conversation_store

pytestmark = [pytest.mark.integration]


def test_conversations_status_memory(client) -> None:
    response = client.get("/api/conversations/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["configured"] is True
    assert payload["ok"] is True


def test_conversations_read_requires_targets(client, targets_file) -> None:
    response = client.post(
        "/api/conversations/read",
        json={"targets": targets_file.as_posix(), "target_ids": [], "phones": []},
    )
    assert response.status_code == 422
    error = response.get_json()["error"]
    assert "target_ids" in error or "phones" in error


def test_conversations_get_and_list(client) -> None:
    store = get_conversation_store(force_new=True)
    store.save_conversation_messages(
        conversation_key="5562999000000",
        target_id="numero_teste",
        target_type="phone",
        phone="5562999000000",
        target_name="Teste",
        messages=[
            {
                "hash": "h1",
                "direction": "incoming",
                "sender": "Teste",
                "timestamp_text": "09:00",
                "text": "Mensagem salva",
                "captured_at": "2026-05-26T09:00:00",
            }
        ],
    )

    listed = client.get("/api/conversations/list")
    assert listed.status_code == 200
    assert listed.get_json()["total"] >= 1

    fetched = client.get("/api/conversations?phone=5562999000000")
    assert fetched.status_code == 200
    body = fetched.get_json()
    assert body["ok"] is True
    assert body["messages"][0]["text"] == "Mensagem salva"


def test_conversations_preview_accepts_explicit_phone(client, targets_file) -> None:
    response = client.post(
        "/api/conversations/preview",
        json={
            "targets": targets_file.as_posix(),
            "phone": "5562999000000",
            "target_id": "numero_teste",
        },
    )
    # Sem WhatsApp conectado nos testes, pode falhar por auth/busy — mas não por seleção múltipla.
    body = response.get_json()
    assert "apenas um" not in str(body.get("error", "")).lower()


def test_conversations_save_selected(client) -> None:
    store = get_conversation_store(force_new=True)
    response = client.post(
        "/api/conversations/save",
        json={
            "phone": "5562999000000",
            "target_id": "numero_teste",
            "target_name": "Teste",
            "messages": [
                {
                    "hash": "save1",
                    "direction": "incoming",
                    "sender": "A",
                    "timestamp_text": "11:00",
                    "text": "Salva via API",
                    "captured_at": "2026-05-26T11:00:00",
                }
            ],
        },
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["saved_count"] == 1

    fetched = store.get_conversation(phone="5562999000000")
    assert fetched["total_messages"] == 1
    assert fetched["messages"][0]["text"] == "Salva via API"
