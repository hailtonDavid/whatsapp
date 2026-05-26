"""Testes da API Flask de números cadastrados (type=phone)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]


def test_flask_phones_send_targets(client, targets_file: Path) -> None:
    targets_file.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "id": "numero_a",
                        "type": "phone",
                        "phone": "5562999111111",
                        "enabled": True,
                        "send": {"enabled": True, "message": "Oi A"},
                    },
                    {
                        "id": "numero_b",
                        "type": "phone",
                        "phone": "5562999222222",
                        "enabled": False,
                        "send": {"enabled": False, "message": ""},
                    },
                    {
                        "id": "grupo_x",
                        "type": "group",
                        "name": "Grupo X",
                        "enabled": True,
                        "send": {"enabled": True, "message": "Grupo"},
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get(f"/api/phones/send-targets?targets={targets_file.as_posix()}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 2
    assert payload["enabled_count"] == 1
    assert all(item["type"] == "phone" for item in payload["targets"])
    assert payload["targets"][0]["phone"] == "5562999111111"


def test_flask_phones_selection_persists_enabled(client, targets_file: Path) -> None:
    targets_file.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "id": "numero_a",
                        "type": "phone",
                        "phone": "5562999111111",
                        "enabled": False,
                        "send": {"enabled": False, "message": ""},
                    },
                    {
                        "id": "numero_b",
                        "type": "phone",
                        "phone": "5562999222222",
                        "enabled": False,
                        "send": {"enabled": False, "message": ""},
                    },
                    {
                        "id": "grupo_x",
                        "type": "group",
                        "name": "Grupo X",
                        "enabled": False,
                        "send": {"enabled": False, "message": ""},
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/api/phones/selection",
        json={
            "targets": targets_file.as_posix(),
            "selected_ids": ["numero_b"],
            "message": "Mensagem individual",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["enabled_count"] == 1

    saved = json.loads(targets_file.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in saved["targets"]}
    assert by_id["numero_b"]["enabled"] is True
    assert by_id["numero_b"]["send"]["message"] == "Mensagem individual"
    assert by_id["numero_a"]["enabled"] is False
    assert by_id["grupo_x"]["enabled"] is False
