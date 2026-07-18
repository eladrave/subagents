from pathlib import Path

import pytest

from support import build_sync_fixture


@pytest.fixture
def asset_source() -> Path:
    return Path("/usr/local/share/codex-drive-rag")


@pytest.fixture
def sync_fixture(tmp_path):
    fixture = build_sync_fixture(tmp_path)
    try:
        yield fixture
    finally:
        from chromadb.api.shared_system_client import SharedSystemClient

        SharedSystemClient.clear_system_cache()
