"""Выбор каталога снимков проверяется без Qt и записи настоящих эталонов."""

from __future__ import annotations

import sys
import tempfile
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import snapshots  # noqa: E402

pytestmark = pytest.mark.unit
ENV_VAR = "ASTRA_VOICE_SNAPSHOT_DIR_TEST"
REFS_DEFAULT = "design/refs/impl/test"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(snapshots, "REPO", root)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "temporary"))
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.delenv("ASTRA_VOICE_UPDATE_REFS", raising=False)
    return root


@pytest.mark.parametrize("flag", [None, "", "0", "true"])
def test_default_uses_temporary_directory(
    repo: Path, monkeypatch: pytest.MonkeyPatch, flag: str | None
) -> None:
    if flag is not None:
        monkeypatch.setenv("ASTRA_VOICE_UPDATE_REFS", flag)
    with pytest.warns(UserWarning, match="временный каталог.*эталоны.*не тронуты"):
        actual = snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert actual == repo.parent / "temporary/astra-voice-snapshots/test"
    assert not actual.exists()
    assert not (repo / "design").exists()


@pytest.mark.parametrize("relative", [False, True])
def test_explicit_directory_outside_refs_is_used(
    repo: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    explicit = "artifacts/test" if relative else str(repo.parent / "outside")
    monkeypatch.setenv(ENV_VAR, explicit)
    monkeypatch.chdir(repo.parent)
    with warnings.catch_warnings(record=True) as caught:
        actual = snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert actual == (repo / explicit).resolve()
    assert caught == []
    assert not actual.exists()


@pytest.mark.parametrize("explicit", [None, "artifacts/test", "design/refs/impl/other"])
def test_update_flag_selects_default_refs_and_ignores_env(
    repo: Path, monkeypatch: pytest.MonkeyPatch, explicit: str | None
) -> None:
    monkeypatch.setenv("ASTRA_VOICE_UPDATE_REFS", "1")
    if explicit is not None:
        monkeypatch.setenv(ENV_VAR, explicit)
    with warnings.catch_warnings(record=True) as caught:
        actual = snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert actual == repo / REFS_DEFAULT
    assert caught == []
    assert not actual.exists()


@pytest.mark.parametrize("relative", [False, True])
def test_explicit_refs_directory_is_redirected(
    repo: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    explicit = "design/refs/impl/other" if relative else str(repo / "design/refs")
    monkeypatch.setenv(ENV_VAR, explicit)
    with pytest.warns(UserWarning, match="эталоны.*не тронуты"):
        actual = snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert actual == repo.parent / "temporary/astra-voice-snapshots/test"
    assert not (repo / "design").exists()


def test_symlink_into_refs_is_redirected(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "alias").symlink_to(repo / "design/refs", target_is_directory=True)
    monkeypatch.setenv(ENV_VAR, "alias/impl/test")
    with pytest.warns(UserWarning, match="эталоны.*не тронуты"):
        actual = snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
    assert actual == repo.parent / "temporary/astra-voice-snapshots/test"
    assert not (repo / "design").exists()


def test_update_flag_rejects_default_outside_refs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRA_VOICE_UPDATE_REFS", "1")
    with pytest.raises(ValueError, match="refs_default"):
        snapshots.snapshot_dir(ENV_VAR, "test", "artifacts/test")


def test_temporary_symlink_into_refs_is_rejected(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = repo.parent / "temporary"
    temporary.mkdir()
    (temporary / "astra-voice-snapshots").symlink_to(repo / "design/refs", target_is_directory=True)
    with pytest.raises(ValueError, match="Временный каталог"):
        snapshots.snapshot_dir(ENV_VAR, "test", REFS_DEFAULT)
