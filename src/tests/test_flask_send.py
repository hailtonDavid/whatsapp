"""Testes da API Flask de envio de mensagens."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]


def test_flask_list_send_targets(client, targets_file) -> None:
    response = client.get(f"/api/send/targets?targets={targets_file.as_posix()}")

    assert response.status_code == 200
    payload = response.get_json()
    assert len(payload["targets"]) == 2
    ids = {item["id"] for item in payload["targets"]}
    assert ids == {"grupo_teste", "numero_teste"}
    by_id = {item["id"]: item for item in payload["targets"]}
    assert by_id["grupo_teste"]["send_enabled"] is True
    assert by_id["numero_teste"]["phone"] == "5562999000000"


def test_flask_send_once_dry_run(client, targets_file) -> None:
    response = client.post(
        "/api/send/once",
        json={
            "targets": targets_file.as_posix(),
            "target_ids": ["grupo_teste"],
            "confirm": False,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["dry_run"] is True
    assert payload["confirmed"] is False
    assert payload["total"] == 1
    assert payload["successes"] == 1
    assert payload["results"][0]["message"] == "Mensagem via Flask"


def test_flask_send_once_requires_target_ids_with_message(client, targets_file) -> None:
    response = client.post(
        "/api/send/once",
        json={
            "targets": targets_file.as_posix(),
            "message": "Mensagem solta",
            "confirm": False,
        },
    )

    assert response.status_code == 422
    payload = response.get_json()
    assert payload["ok"] is False
    assert "target_ids" in payload["error"]


def test_flask_send_once_with_message_and_target(client, targets_file) -> None:
    response = client.post(
        "/api/send/once",
        json={
            "targets": targets_file.as_posix(),
            "target_ids": ["grupo_teste"],
            "message": "Override Flask",
            "confirm": False,
        },
    )

    assert response.status_code == 200
    assert response.get_json()["results"][0]["message"] == "Override Flask"


def test_flask_send_last_empty(client) -> None:
    response = client.get("/api/send/last")

    assert response.status_code == 200
    payload = response.get_json()
    assert "results" in payload
    assert "total" in payload
