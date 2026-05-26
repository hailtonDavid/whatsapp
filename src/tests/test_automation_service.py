"""Testes dos jobs async em automation_service (sem browser real)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from automation_service import (
    execute_send_job,
    execute_sync_contacts_job,
    resolve_allowed_attachment_path,
    save_uploaded_attachment,
)
from whatsapp_auto_downloader import load_app_config


@pytest.mark.asyncio
async def test_execute_send_job_dry_run(env_file: Path, targets_file: Path) -> None:
    outcome = await execute_send_job(
        env_file,
        targets_file,
        target_ids=["numero_teste"],
        confirm=False,
    )

    assert outcome["ok"] is True
    assert outcome["dry_run"] is True
    assert outcome["confirmed"] is False
    assert outcome["total"] >= 1


@pytest.mark.asyncio
async def test_execute_send_job_rejects_message_without_targets(env_file: Path, targets_file: Path) -> None:
    outcome = await execute_send_job(
        env_file,
        targets_file,
        message="Olá",
        confirm=False,
    )

    assert outcome["ok"] is False
    assert "target_ids" in outcome["error"]


@pytest.mark.asyncio
async def test_execute_send_job_dry_run_with_attachment(env_file: Path, targets_file: Path) -> None:
    config = load_app_config(env_file)
    saved = save_uploaded_attachment(config, "teste.pdf", b"%PDF-1.4 test")
    outcome = await execute_send_job(
        env_file,
        targets_file,
        target_ids=["grupo_teste"],
        attachment=saved["path"],
        confirm=False,
    )

    assert outcome["ok"] is True
    assert outcome["dry_run"] is True
    assert outcome["results"][0]["attachment"] is not None
    assert outcome["results"][0]["attachment_name"] == "teste.pdf"


def test_save_uploaded_attachment_and_resolve(env_file: Path) -> None:
    config = load_app_config(env_file)
    saved = save_uploaded_attachment(config, "foto.png", b"fake-image")
    path = resolve_allowed_attachment_path(config, saved["path"])
    assert path.is_file()
    assert path.name.startswith("foto")


@pytest.mark.asyncio
async def test_execute_sync_contacts_job_mocked(env_file: Path, tmp_path: Path) -> None:
    output_path = tmp_path / "contacts.json"
    targets_path = tmp_path / "targets.json"
    targets_path.write_text('{"targets": []}', encoding="utf-8")

    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_playwright = AsyncMock()

    with (
        patch(
            "automation_service.connect_whatsapp_for_operation",
            AsyncMock(
                return_value=type(
                    "Conn",
                    (),
                    {
                        "page": mock_page,
                        "bootstrap": type("B", (), {"config": load_app_config(env_file)})(),
                    },
                )()
            ),
        ),
        patch(
            "automation_service.extract_whatsapp_contacts",
            AsyncMock(
                return_value={
                    "ok": True,
                    "contacts": [{"name": "Ana", "phone": "5511999999999", "whatsapp_id": "5511999999999@c.us"}],
                }
            ),
        ),
        patch(
            "automation_service.merge_contacts_into_targets",
            return_value={"ok": True, "added": 1, "updated": 0, "total_phones": 1},
        ),
        patch("automation_service.release_whatsapp_operation", AsyncMock()),
    ):
        outcome = await execute_sync_contacts_job(
            env_file,
            output_path=output_path,
            targets_path=targets_path,
        )

    assert outcome["ok"] is True
    assert outcome["total_contacts"] == 1
    assert outcome["added"] == 1
    assert output_path.exists()
