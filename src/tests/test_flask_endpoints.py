"""Testes de integração dos endpoints Flask via test client."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from wa_selectors import WHATSAPP_LOGIN_SELECTOR
from whatsapp_auto_downloader import WA_URL

pytestmark = [pytest.mark.integration]

PUBLIC_GET_ROUTES = (
    ("/api", {"status": "ok", "service": "whatsapp-web-automation"}),
    ("/health", {"status": "ok"}),
    ("/api/automation/status", {"automation_status": "ready", "env_loaded": True, "session_active": False}),
    ("/api/send/last", {"total": 0, "results": []}),
    ("/api/groups/last", {"total_groups": 0, "groups": []}),
)


@pytest.mark.parametrize("route,expected_fields", PUBLIC_GET_ROUTES)
def test_flask_public_get_endpoints(client, route: str, expected_fields: dict) -> None:
    response = client.get(route)

    assert response.status_code == 200
    payload = response.get_json()
    for key, value in expected_fields.items():
        assert payload[key] == value


def test_flask_dashboard_returns_html(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in (response.content_type or "")
    assert b"WhatsApp Web Automation" in response.data
    assert b"btn-launch" in response.data


def test_flask_index_lists_documented_endpoints(client) -> None:
    response = client.get("/api")

    payload = response.get_json()
    endpoints = payload["endpoints"]
    assert endpoints["send_once"] == "POST /api/send/once"
    assert endpoints["groups_generate"] == "POST /api/groups/generate"
    assert endpoints["groups_last"] == "/api/groups/last"
    assert endpoints["contacts_sync"] == "POST /api/contacts/sync"
    assert endpoints["contacts_last"] == "/api/contacts/last"


def test_flask_automation_start_returns_login_selector(client) -> None:
    response = client.post("/api/automation/start")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["automation_status"] == "ready"
    assert payload["session_active"] is False
    assert payload["login_selector"] == WHATSAPP_LOGIN_SELECTOR
    assert payload["whatsapp_url"] == WA_URL


def test_flask_automation_start_blocked_without_profile_dir(tmp_path: Path) -> None:
    from app import create_app

    missing_env = tmp_path / "missing.env"
    missing_env.write_text("WA_HEADLESS=true\n", encoding="utf-8")
    blocked_client = create_app(env_file=missing_env).test_client()

    response = blocked_client.post("/api/automation/start")

    assert response.status_code == 400
    assert response.get_json()["automation_status"] == "failed"


def test_flask_send_targets_endpoint(client, targets_file: Path) -> None:
    response = client.get(f"/api/send/targets?targets={targets_file.as_posix()}")

    assert response.status_code == 200
    payload = response.get_json()
    ids = {item["id"] for item in payload["targets"]}
    assert "grupo_teste" in ids
    assert payload["targets_file"].endswith("targets.json")


def test_flask_send_once_dry_run(client, targets_file: Path) -> None:
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
    assert payload["successes"] == 1


def test_flask_send_once_rejects_message_without_targets(client, targets_file: Path) -> None:
    response = client.post(
        "/api/send/once",
        json={
            "targets": targets_file.as_posix(),
            "message": "Mensagem solta",
            "confirm": False,
        },
    )

    assert response.status_code == 422
    assert response.get_json()["ok"] is False


def test_flask_send_once_returns_404_for_missing_targets_file(client, tmp_path: Path) -> None:
    missing = tmp_path / "inexistente.json"
    response = client.post(
        "/api/send/once",
        json={"targets": missing.as_posix(), "target_ids": ["x"], "confirm": False},
    )

    assert response.status_code == 404


def test_flask_groups_generate_endpoint(client, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_job = AsyncMock(
        return_value={
            "ok": True,
            "total_groups": 1,
            "groups": [{"name": "Grupo Teste"}],
            "inventory_path": "exports/groups/groups.json",
            "targets_path": "exports/groups/groups_targets_template.json",
        }
    )
    monkeypatch.setattr("app.execute_list_groups_job", mock_job)

    response = client.post("/api/groups/generate", json={})

    assert response.status_code == 200
    assert response.get_json()["total_groups"] == 1
    mock_job.assert_awaited_once()


def test_flask_groups_targets_template(client, tmp_path: Path) -> None:
    template = tmp_path / "groups_targets.json"
    template.write_text(
        json.dumps({"targets": [{"id": "grupo_a", "type": "group", "name": "Grupo A"}]}),
        encoding="utf-8",
    )

    response = client.get(f"/api/groups/targets-template?targets_output={template.as_posix()}")

    assert response.status_code == 200
    assert len(response.get_json()["targets"]) == 1


def test_flask_send_and_groups_require_env(tmp_path: Path) -> None:
    from app import create_app

    missing_env = tmp_path / "missing.env"
    missing_env.write_text("WA_HEADLESS=true\n", encoding="utf-8")
    blocked_client = create_app(env_file=missing_env).test_client()

    send_response = blocked_client.post("/api/send/once", json={"confirm": False})
    groups_response = blocked_client.post("/api/groups/generate", json={})
    contacts_response = blocked_client.post("/api/contacts/sync", json={})

    assert send_response.status_code == 400
    assert groups_response.status_code == 400
    assert contacts_response.status_code == 400
    assert send_response.get_json()["ok"] is False
    assert groups_response.get_json()["ok"] is False
    assert contacts_response.get_json()["ok"] is False
