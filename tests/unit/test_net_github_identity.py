"""Идентификаторы выпуска и адрес каталога зависят только от байтов схемы."""

import hashlib
import re
from pathlib import Path

import pytest

from astra_voice.core import paths, release_identity
from astra_voice.core.version import __version__
from astra_voice.net import github
from astra_voice.updates import checker, release

pytestmark = pytest.mark.unit
DATA_ROOT = Path(__file__).resolve().parents[2] / "data"


def test_schema_id_is_deterministic_and_byte_sensitive() -> None:
    raw = (DATA_ROOT / "catalog.schema.json").read_bytes()
    identifier = release_identity.schema_id(raw)
    assert identifier == release_identity.schema_id(raw)
    assert re.fullmatch(r"[0-9a-f]{16}", identifier)
    assert identifier == hashlib.sha256(raw).hexdigest()[:16]
    assert release_identity.schema_id(raw + b"\n") != identifier


def test_client_schema_id_and_default_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "data_dir_static", lambda: DATA_ROOT)
    identifier = hashlib.sha256((DATA_ROOT / "catalog.schema.json").read_bytes()).hexdigest()[:16]
    assert release_identity.client_schema_id() == identifier
    assert release_identity.catalog_default_url() == (
        "https://github.com/ArrivaRUS/astra-voice/releases/latest/download/"
        f"catalog-{identifier}.json"
    )


def test_client_schema_id_uses_packaged_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = b'{"packaged": true}\n'
    (tmp_path / "catalog.schema.json").write_bytes(raw)
    monkeypatch.setattr(paths, "data_dir_static", lambda: tmp_path)
    assert release_identity.client_schema_id() == hashlib.sha256(raw).hexdigest()[:16]


def test_release_addresses_and_user_agents_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.core.policy import Policy
    from astra_voice.core.settings import Settings
    from astra_voice.ui.model_downloads import ModelService

    assert release_identity.RELEASES_REPO == "ArrivaRUS/astra-voice"
    assert github.RELEASES_LATEST_URL == (
        "https://api.github.com/repos/ArrivaRUS/astra-voice/releases/latest"
    )
    assert github.RELEASE_URL_PREFIX == "https://github.com/ArrivaRUS/astra-voice/releases/"
    assert release.REPOSITORY_URL == "https://github.com/ArrivaRUS/astra-voice"
    expected = f"astra-voice/{__version__} (+https://github.com/ArrivaRUS/astra-voice)"
    assert checker.USER_AGENT == expected
    monkeypatch.setattr(paths, "data_dir_static", lambda: DATA_ROOT)
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    service = ModelService(Settings(), Policy())
    assert service._downloader._http._user_agent == expected
