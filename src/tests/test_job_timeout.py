"""Testes de timeout de jobs."""

from __future__ import annotations

from whatsapp_auto_downloader import AppConfig
from pathlib import Path

from automation_service import automation_job_timeout


def test_automation_job_timeout_heavy_is_at_least_15_minutes() -> None:
    config = AppConfig(
        profile_dir=Path("profile"),
        export_dir=Path("exports"),
        state_dir=Path("state"),
        headless=True,
        ready_timeout=30,
    )
    assert automation_job_timeout(config, heavy=True) >= 900


def test_automation_job_timeout_light_scales_with_ready() -> None:
    config = AppConfig(
        profile_dir=Path("profile"),
        export_dir=Path("exports"),
        state_dir=Path("state"),
        headless=True,
        ready_timeout=180,
    )
    assert automation_job_timeout(config, heavy=False) >= 360
