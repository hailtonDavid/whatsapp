"""Testes Flask para sincronização de contatos/números do WhatsApp."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from whatsapp_auto_downloader import merge_contacts_into_targets


def test_flask_contacts_last_empty(client) -> None:
    response = client.get("/api/contacts/last")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["total_contacts"] == 0
    assert payload["contacts"] == []


def test_flask_contacts_last_with_data(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = tmp_path / "contacts.json"
    inventory.write_text(
        json.dumps(
            {
                "ok": True,
                "total_contacts": 1,
                "contacts": [{"name": "João", "phone": "5511999999999"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from app import create_app

    app = create_app(default_contacts_output=str(inventory))
    test_client = app.test_client()

    response = test_client.get("/api/contacts/last")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["total_contacts"] == 1
    assert payload["contacts"][0]["phone"] == "5511999999999"


def test_flask_contacts_sync(client, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_job = AsyncMock(
        return_value={
            "ok": True,
            "total_contacts": 2,
            "contacts": [
                {"name": "Ana", "phone": "5511888888888"},
                {"name": "Bruno", "phone": "5511777777777"},
            ],
            "inventory_path": "exports/contacts/contacts.json",
            "targets_path": "config/targets.json",
            "added": 2,
            "updated": 0,
            "total_phones": 2,
        }
    )
    monkeypatch.setattr("app.execute_sync_contacts_job", mock_job)

    response = client.post(
        "/api/contacts/sync",
        json={"targets": "config/targets.json"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["total_contacts"] == 2
    assert payload["added"] == 2
    mock_job.assert_awaited_once()


def test_merge_contacts_into_targets_adds_and_updates(tmp_path: Path) -> None:
    targets_path = tmp_path / "targets.json"
    targets_path.write_text(
        json.dumps(
            {
                "interval_seconds": 60,
                "targets": [
                    {
                        "id": "grupo_a",
                        "type": "group",
                        "name": "Grupo A",
                        "enabled": True,
                    },
                    {
                        "id": "numero_5511999999999",
                        "type": "phone",
                        "phone": "5511999999999",
                        "name": "5511999999999",
                        "enabled": False,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    outcome = merge_contacts_into_targets(
        targets_path,
        [
            {"name": "João", "phone": "5511999999999", "whatsapp_id": "5511999999999@c.us"},
            {"name": "Maria", "phone": "5511888888888", "whatsapp_id": "5511888888888@c.us"},
        ],
    )

    assert outcome["ok"] is True
    assert outcome["added"] == 1
    assert outcome["updated"] == 1
    assert outcome["total_phones"] == 2

    data = json.loads(targets_path.read_text(encoding="utf-8"))
    phones = [item for item in data["targets"] if item["type"] == "phone"]
    groups = [item for item in data["targets"] if item["type"] == "group"]

    assert len(groups) == 1
    assert len(phones) == 2
    joao = next(item for item in phones if item["phone"] == "5511999999999")
    assert joao["name"] == "João"
    assert joao["metadata"]["whatsapp_id"] == "5511999999999@c.us"
