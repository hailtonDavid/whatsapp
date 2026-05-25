"""Testes da API Flask de inventário de grupos."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytestmark = [pytest.mark.integration]


def test_flask_groups_last_empty(client) -> None:
    response = client.get("/api/groups/last")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total_groups"] == 0
    assert payload["groups"] == []
    assert "inventory_path" in payload


def test_flask_groups_last_with_data(client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = tmp_path / "groups.json"
    inventory.write_text(
        json.dumps(
            {
                "ok": True,
                "total_groups": 1,
                "groups": [{"name": "Grupo A", "whatsapp_id": "123@g.us"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get(f"/api/groups/last?output={inventory.as_posix()}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total_groups"] == 1
    assert payload["groups"][0]["name"] == "Grupo A"


def test_flask_groups_targets_template_empty(client) -> None:
    response = client.get("/api/groups/targets-template?targets_output=exports/groups/inexistente.json")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["targets"] == []


def test_flask_groups_targets_template_with_data(client, tmp_path: Path) -> None:
    template = tmp_path / "groups_targets.json"
    template.write_text(
        json.dumps(
            {
                "targets": [
                    {"id": "grupo_a", "type": "group", "name": "Grupo A", "enabled": False}
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = client.get(f"/api/groups/targets-template?targets_output={template.as_posix()}")

    assert response.status_code == 200
    payload = response.get_json()
    assert len(payload["targets"]) == 1
    assert payload["targets"][0]["id"] == "grupo_a"


def test_flask_groups_generate(client, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_job = AsyncMock(
        return_value={
            "ok": True,
            "total_groups": 2,
            "groups": [{"name": "Grupo A"}, {"name": "Grupo B"}],
            "inventory_path": "exports/groups/groups.json",
            "targets_path": "exports/groups/groups_targets_template.json",
        }
    )
    monkeypatch.setattr("app.execute_list_groups_job", mock_job)

    response = client.post(
        "/api/groups/generate",
        json={
            "output": "exports/groups/groups.json",
            "targets_output": "exports/groups/groups_targets_template.json",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["total_groups"] == 2
    mock_job.assert_awaited_once()
