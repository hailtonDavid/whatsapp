"""Testes do job de leitura de conversas."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from automation_service import (
    build_phone_target,
    resolve_read_targets,
    resolve_single_read_target,
    save_selected_conversation_messages,
)
from conversation_store import ConversationStore, MongoSettings


def test_resolve_read_targets_from_phone_and_id(targets_file: Path) -> None:
    by_phone = resolve_read_targets(targets_file, phones=["5562999000000"])
    assert len(by_phone) == 1
    assert by_phone[0].phone == "5562999000000"

    by_id = resolve_read_targets(targets_file, target_ids=["numero_teste"])
    assert len(by_id) == 1
    assert by_id[0].id == "numero_teste"


def test_build_phone_target_normalizes_digits() -> None:
    target = build_phone_target("(62) 99900-0000")
    assert target.phone == "62999000000"


def test_resolve_single_read_target_requires_one(targets_file: Path) -> None:
    one = resolve_single_read_target(targets_file, target_ids=["numero_teste"])
    assert one.id == "numero_teste"

    with pytest.raises(ValueError, match="apenas um"):
        resolve_single_read_target(
            targets_file,
            phones=["5562999000000", "5511999999999"],
        )


def test_save_selected_conversation_messages() -> None:
    store = ConversationStore(MongoSettings(uri="memory://", database="test"))
    outcome = save_selected_conversation_messages(
        phone="5562999000000",
        target_id="numero_teste",
        target_name="Teste",
        messages=[
            {
                "hash": "sel1",
                "direction": "incoming",
                "sender": "A",
                "timestamp_text": "10:00",
                "text": "Selecionada",
                "captured_at": "2026-05-26T10:00:00",
            }
        ],
        store=store,
    )
    assert outcome["ok"] is True
    assert outcome["saved_count"] == 1
    saved = store.get_conversation(phone="5562999000000")
    assert saved["total_messages"] == 1


@pytest.mark.asyncio
async def test_execute_preview_conversation_job_mocked(env_file: Path, targets_file: Path) -> None:
    from automation_service import execute_preview_conversation_job

    store = ConversationStore(MongoSettings(uri="memory://", database="test"))
    store.save_conversation_messages(
        conversation_key="5562999000000",
        target_id="numero_teste",
        target_type="phone",
        phone="5562999000000",
        target_name="Teste",
        messages=[
            {
                "hash": "existing",
                "direction": "incoming",
                "sender": "A",
                "timestamp_text": "09:00",
                "text": "Já salva",
                "captured_at": "2026-05-26T09:00:00",
            }
        ],
    )
    mock_page = AsyncMock()

    with (
        patch(
            "automation_service.connect_whatsapp_for_operation",
            AsyncMock(
                return_value=type(
                    "Conn",
                    (),
                    {
                        "page": mock_page,
                        "bootstrap": type("B", (), {"config": type("C", (), {"export_dir": Path("exports")})()})(),
                    },
                )()
            ),
        ),
        patch("automation_service.open_target", AsyncMock(return_value=(True, None))),
        patch(
            "automation_service.collect_messages_for_target",
            AsyncMock(
                return_value=[
                    {
                        "hash": "existing",
                        "direction": "incoming",
                        "sender": "A",
                        "timestamp_text": "09:00",
                        "text": "Já salva",
                        "captured_at": "2026-05-26T09:00:00",
                    },
                    {
                        "hash": "new1",
                        "direction": "incoming",
                        "sender": "A",
                        "timestamp_text": "10:00",
                        "text": "Nova",
                        "captured_at": "2026-05-26T10:00:00",
                    },
                ]
            ),
        ),
        patch("automation_service.release_whatsapp_operation", AsyncMock()),
    ):
        outcome = await execute_preview_conversation_job(
            env_file,
            targets_path=targets_file,
            target_ids=["numero_teste"],
            store=store,
        )

    assert outcome["ok"] is True
    assert outcome["preview"] is True
    assert outcome["captured_messages"] == 2
    assert outcome["already_saved_count"] == 1
    assert outcome["messages"][0]["saved_in_db"] is True
    assert outcome["messages"][1]["saved_in_db"] is False
    saved = store.get_conversation(phone="5562999000000")
    assert saved["total_messages"] == 1


@pytest.mark.asyncio
async def test_execute_read_conversations_job_mocked(env_file: Path, targets_file: Path) -> None:
    from automation_service import execute_read_conversations_job

    store = ConversationStore(MongoSettings(uri="memory://", database="test"))
    mock_page = AsyncMock()

    with (
        patch(
            "automation_service.connect_whatsapp_for_operation",
            AsyncMock(
                return_value=type(
                    "Conn",
                    (),
                    {
                        "page": mock_page,
                        "bootstrap": type("B", (), {"config": type("C", (), {"export_dir": Path("exports")})()})(),
                    },
                )()
            ),
        ),
        patch("automation_service.open_target", AsyncMock(return_value=(True, None))),
        patch(
            "automation_service.collect_messages_for_target",
            AsyncMock(
                return_value=[
                    {
                        "hash": "x1",
                        "direction": "incoming",
                        "sender": "A",
                        "timestamp_text": "10:00",
                        "text": "Oi",
                        "captured_at": "2026-05-26T10:00:00",
                    }
                ]
            ),
        ),
        patch("automation_service.release_whatsapp_operation", AsyncMock()),
    ):
        outcome = await execute_read_conversations_job(
            env_file,
            targets_path=targets_file,
            target_ids=["numero_teste"],
            store=store,
        )

    assert outcome["ok"] is True
    assert outcome["successes"] == 1
    saved = store.get_conversation(phone="5562999000000")
    assert saved["ok"] is True
    assert saved["total_messages"] == 1
