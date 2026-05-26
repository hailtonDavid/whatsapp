"""Testes dos jobs async em automation_service (sem browser real)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from automation_service import execute_send_job, execute_sync_contacts_job


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
async def test_execute_sync_contacts_job_mocked(env_file: Path, tmp_path: Path) -> None:
    output_path = tmp_path / "contacts.json"
    targets_path = tmp_path / "targets.json"
    targets_path.write_text('{"targets": []}', encoding="utf-8")

    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_playwright = AsyncMock()

    with (
        patch("automation_service.open_whatsapp", AsyncMock(return_value=(mock_playwright, mock_context, mock_page))),
        patch("automation_service.wait_for_whatsapp_ready", AsyncMock()),
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
