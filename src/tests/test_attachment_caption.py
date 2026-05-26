"""Testes da legenda de anexos."""

from __future__ import annotations

from whatsapp_auto_downloader import caption_matches_expected, normalize_for_compare


def test_caption_matches_expected_exact() -> None:
    assert caption_matches_expected("Olá grupo", normalize_for_compare("Olá grupo"))


def test_caption_matches_expected_partial() -> None:
    assert caption_matches_expected("Olá grupo\ncom quebra", normalize_for_compare("Olá grupo"))


def test_caption_matches_expected_empty() -> None:
    assert caption_matches_expected("", "")
