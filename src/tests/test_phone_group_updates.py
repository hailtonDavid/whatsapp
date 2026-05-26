"""Testes de atualização de telefones e mesclagem de grupos."""

from __future__ import annotations

import json
from pathlib import Path

from automation_service import apply_phone_updates
from whatsapp_auto_downloader import (
    _group_name_matches_wanted,
    default_group_search_prefixes,
    list_group_names_from_targets_file,
    merge_group_entries,
    merge_groups_into_targets,
)


def test_apply_phone_updates_changes_number_and_message(tmp_path: Path) -> None:
    targets_path = tmp_path / "targets.json"
    targets_path.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "id": "numero_5511999999999",
                        "type": "phone",
                        "phone": "5511999999999",
                        "enabled": False,
                        "send": {"enabled": False, "message": ""},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    outcome = apply_phone_updates(
        targets_path,
        [{"id": "numero_5511999999999", "phone": "5511888888888", "message": "Oi", "enabled": True}],
    )

    assert outcome["ok"] is True
    assert outcome["updated_count"] == 1
    data = json.loads(targets_path.read_text(encoding="utf-8"))
    phone = data["targets"][0]
    assert phone["phone"] == "5511888888888"
    assert phone["enabled"] is True
    assert phone["send"]["message"] == "Oi"


def test_merge_group_entries_adds_missing_name() -> None:
    merged, added = merge_group_entries(
        [{"name": "Grupo A", "whatsapp_id": "1@g.us"}],
        [{"name": "Tal pai, tal filhas", "source": "dom:scroll-supplement"}],
    )

    assert added == 1
    names = {item["name"] for item in merged}
    assert "Tal pai, tal filhas" in names


def test_merge_groups_into_targets_preserves_phones(tmp_path: Path) -> None:
    targets_path = tmp_path / "targets.json"
    targets_path.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "id": "numero_5511999999999",
                        "type": "phone",
                        "phone": "5511999999999",
                        "enabled": True,
                        "send": {"enabled": True, "message": "Olá"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    outcome = merge_groups_into_targets(
        targets_path,
        [{"name": "Tal pai, tal filhas", "whatsapp_id": None, "source": "dom:pane-side"}],
    )

    assert outcome["added"] == 1
    data = json.loads(targets_path.read_text(encoding="utf-8"))
    phones = [t for t in data["targets"] if t["type"] == "phone"]
    groups = [t for t in data["targets"] if t["type"] == "group"]
    assert len(phones) == 1
    assert len(groups) == 1
    assert groups[0]["name"] == "Tal pai, tal filhas"


def test_default_group_search_prefixes_includes_letters_and_digits() -> None:
    tokens = default_group_search_prefixes()
    assert "a" in tokens
    assert "z" in tokens
    assert "0" in tokens
    assert "9" in tokens
    assert len(tokens) == 36


def test_list_group_names_from_targets_file(tmp_path: Path) -> None:
    targets_path = tmp_path / "targets.json"
    targets_path.write_text(
        json.dumps(
            {
                "targets": [
                    {"id": "g1", "type": "group", "name": "Tal pai, tal filhas"},
                    {"id": "g2", "type": "group", "name": "NOME EXATO DO GRUPO AQUI"},
                    {"id": "p1", "type": "phone", "phone": "5511999999999"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    names = list_group_names_from_targets_file(targets_path)
    assert names == ["Tal pai, tal filhas"]


def test_group_name_matches_wanted() -> None:
    assert _group_name_matches_wanted("Tal pai, tal filhas", "tal pai, tal filhas")
    assert _group_name_matches_wanted("Tal pai, tal filhas", "Tal pai")
    assert not _group_name_matches_wanted("Outro grupo", "Tal pai, tal filhas")
