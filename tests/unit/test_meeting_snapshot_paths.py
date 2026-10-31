"""Meeting snapshots use private output without changing approved design references."""

from __future__ import annotations

import importlib
import os
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import snapshots  # noqa: E402

pytestmark = pytest.mark.unit
SPECIFIC = "ASTRA_VOICE_SNAPSHOT_DIR_MEETINGS"
GENERAL = "ASTRA_VOICE_SNAPSHOT_DIR"


@pytest.fixture
def meeting_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "xvfb"))
    module = importlib.import_module("test_meetings_section")
    repo, home = tmp_path / "repo", tmp_path / "home"
    repo.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    monkeypatch.setattr(module, "REPO", repo)
    monkeypatch.setattr(snapshots, "REPO", repo)
    monkeypatch.setattr(snapshots, "real_home", lambda: home)
    for variable in (SPECIFIC, GENERAL, "ASTRA_VOICE_UPDATE_REFS"):
        monkeypatch.delenv(variable, raising=False)
    return module


def cache_dir(module: ModuleType) -> Path:
    return (
        Path(module.REPO.parent)
        / "home/.cache/astra-voice/snapshots"
        / snapshots.tree_key()
        / "meetings"
    )


@pytest.mark.parametrize("foreign_owner", [False, True])
def test_default_writes_private_cache_even_with_foreign_checkout_owner(
    meeting_snapshots: ModuleType, monkeypatch: pytest.MonkeyPatch, foreign_owner: bool
) -> None:
    repo = meeting_snapshots.REPO
    if foreign_owner:
        original = os.lstat

        def lstat(path: str | Path, *, dir_fd: int | None = None) -> os.stat_result:
            info = original(path, dir_fd=dir_fd)
            if Path(path) == repo:
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info

        monkeypatch.setattr(os, "lstat", lstat)

    directory = meeting_snapshots.snapshots()
    assert directory == cache_dir(meeting_snapshots)
    written = snapshots.write_snapshot(directory, "meeting.png", b"snapshot")
    assert written.read_bytes() == b"snapshot"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert not list(repo.iterdir())


@pytest.mark.parametrize("variable", [GENERAL, SPECIFIC])
@pytest.mark.parametrize("relative", [False, True])
def test_explicit_output_respects_standard_path_contract(
    meeting_snapshots: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    relative: bool,
) -> None:
    repo = meeting_snapshots.REPO
    value = "artifacts/meetings" if relative else str(repo.parent / "output")
    monkeypatch.setenv(variable, value)
    monkeypatch.chdir(repo.parent)
    assert meeting_snapshots.snapshots() == repo / value


def test_meeting_output_takes_precedence_over_general_output(
    meeting_snapshots: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = meeting_snapshots.REPO
    monkeypatch.setenv(GENERAL, str(repo.parent / "general"))
    monkeypatch.setenv(SPECIFIC, str(repo.parent / "specific"))
    assert meeting_snapshots.snapshots() == repo.parent / "specific"
    assert not (repo.parent / "general").exists()


@pytest.mark.parametrize("variable", [GENERAL, SPECIFIC])
def test_design_output_redirects_to_cache_even_with_update_flag(
    meeting_snapshots: ModuleType, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    repo = meeting_snapshots.REPO
    reference = repo / "design/refs/impl/meetings/approved.png"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"approved")
    before = snapshots.refs_inventory(repo / "design/refs")
    monkeypatch.setenv(variable, str(reference.parent))
    monkeypatch.setenv("ASTRA_VOICE_UPDATE_REFS", "1")
    with pytest.warns(UserWarning, match="эталоны.*не тронуты"):
        directory = meeting_snapshots.snapshots()
    snapshots.write_snapshot(directory, "approved.png", b"new snapshot")
    assert directory == cache_dir(meeting_snapshots)
    assert snapshots.refs_inventory(repo / "design/refs") == before
    assert os.environ["ASTRA_VOICE_UPDATE_REFS"] == "1"
