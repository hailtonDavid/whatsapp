"""Fixtures compartilhadas para testes da aplicação Flask."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import create_app


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    channel = os.getenv("WA_BROWSER_CHANNEL", "msedge")
    return {
        **browser_type_launch_args,
        "channel": channel,
    }


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "WA_PROFILE_DIR=profile_whatsapp_test",
                "WA_HEADLESS=true",
                "WA_READY_TIMEOUT=30",
                "WA_EXPORT_DIR=exports",
                "WA_STATE_DIR=state",
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def app(env_file: Path):
    return create_app(env_file=env_file)


@pytest.fixture
def client(app):
    return app.test_client()
