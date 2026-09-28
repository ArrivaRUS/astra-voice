"""XDG autostart behavior in isolated configuration directories."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from astra_voice.platform import autostart

pytestmark = pytest.mark.unit

ENTRY = (
    b"[Desktop Entry]\nType=Application\nName=Astra Voice\n"
    b"Exec=/usr/bin/astra-voice --hidden\nIcon=astravoice\nNoDisplay=true\n"
    b"X-KDE-autostart-after=panel\n"
    b"X-AstraVoice-Managed=true\n"
)
FOREIGN = (
    b"[Desktop Entry]\nType=Application\nName=Astra Voice\n"
    b'Exec=sh -c "sleep 8; systemctl --user start pipewire-pulse.socket; '
    b'exec astra-voice --hidden"\nIcon=astravoice\nX-KDE-autostart-after=panel\n'
)


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    user = tmp_path / "user"
    system = tmp_path / "system"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(system))
    return user / "autostart" / "astra-voice.desktop", system / "autostart" / "astra-voice.desktop"


def test_create_and_remove_own_entry(paths: tuple[Path, Path]) -> None:
    """T-148: a private entry has exact content and public desktop permissions."""
    user, _ = paths
    assert not autostart.state().enabled
    old_umask = os.umask(0o022)
    try:
        autostart.set_enabled(True)
    finally:
        os.umask(old_umask)
    assert user.read_bytes() == ENTRY
    assert stat.S_IMODE(user.stat().st_mode) == 0o644
    assert stat.S_IMODE(user.parent.stat().st_mode) == 0o755
    assert autostart.state() == autostart.AutostartState(True, "ours", False, False)
    autostart.set_enabled(False)
    assert not user.exists()


def test_foreign_entry_preserved(paths: tuple[Path, Path]) -> None:
    """T-28: the customer's manual command and all other bytes survive toggling."""
    user, _ = paths
    user.parent.mkdir(parents=True)
    user.write_bytes(FOREIGN)
    autostart.set_enabled(False)
    assert user.read_bytes() == FOREIGN + b"Hidden=true\n"
    assert autostart.state().user == "foreign"
    autostart.set_enabled(True)
    assert user.read_bytes() == FOREIGN


@pytest.mark.parametrize("hidden", [b"false", b"true"])
def test_foreign_existing_hidden(paths: tuple[Path, Path], hidden: bytes) -> None:
    """T-149: change only Hidden within Desktop Entry, preserving other groups."""
    user, _ = paths
    user.parent.mkdir(parents=True)
    original = FOREIGN + b"Hidden=" + hidden + b"\n[Other]\nHidden=false\n"
    user.write_bytes(original)
    autostart.set_enabled(False)
    assert user.read_bytes() == FOREIGN + b"Hidden=true\n[Other]\nHidden=false\n"
    autostart.set_enabled(True)
    assert user.read_bytes() == FOREIGN + b"[Other]\nHidden=false\n"


def test_foreign_without_final_newline(paths: tuple[Path, Path]) -> None:
    """T-150: adding and removing Hidden restores the final newline convention."""
    user, _ = paths
    user.parent.mkdir(parents=True)
    original = FOREIGN.rstrip(b"\n")
    user.write_bytes(original)
    autostart.set_enabled(False)
    assert user.read_bytes() == original + b"\nHidden=true"
    autostart.set_enabled(True)
    assert user.read_bytes() == original


def test_system_override(paths: tuple[Path, Path]) -> None:
    """T-134: an active system entry is shadowed and restored."""
    user, system = paths
    system.parent.mkdir(parents=True)
    system.write_bytes(FOREIGN)
    assert autostart.state() == autostart.AutostartState(True, "none", True, False)
    autostart.set_enabled(True)
    assert not user.exists()
    autostart.set_enabled(False)
    assert user.read_bytes() == ENTRY + b"Hidden=true\n"
    assert not autostart.state().enabled
    autostart.set_enabled(True)
    assert not user.exists()
    assert autostart.state().enabled


def test_hidden_system_entry(paths: tuple[Path, Path]) -> None:
    """T-135: a hidden system entry can be enabled with a user override."""
    user, system = paths
    system.parent.mkdir(parents=True)
    system.write_bytes(FOREIGN + b"Hidden= TRUE \n")
    assert autostart.state() == autostart.AutostartState(False, "none", True, True)
    autostart.set_enabled(True)
    assert user.read_bytes() == ENTRY


def test_own_hidden_entry_without_system(paths: tuple[Path, Path]) -> None:
    """T-144: enabling a standalone managed override clears its Hidden line."""
    user, _ = paths
    user.parent.mkdir(parents=True)
    user.write_bytes(ENTRY + b"Hidden=true\n")
    autostart.set_enabled(True)
    assert user.read_bytes() == ENTRY


def test_first_existing_system_directory(
    paths: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-145: system directories are checked in XDG_CONFIG_DIRS order."""
    _, first = paths
    second = tmp_path / "second" / "autostart" / "astra-voice.desktop"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(FOREIGN + b"Hidden=true\n")
    second.write_bytes(FOREIGN)
    monkeypatch.setenv("XDG_CONFIG_DIRS", f"{first.parent.parent}:{second.parent.parent}")
    assert autostart.state() == autostart.AutostartState(False, "none", True, True)
    first.unlink()
    assert autostart.state() == autostart.AutostartState(True, "none", True, False)


def test_symlink_rejected(paths: tuple[Path, Path], tmp_path: Path) -> None:
    """T-136: writes never follow or replace a user symlink."""
    user, _ = paths
    target = tmp_path / "target.desktop"
    target.write_bytes(FOREIGN)
    user.parent.mkdir(parents=True)
    user.symlink_to(target)
    assert autostart.state().enabled
    with pytest.raises(autostart.AutostartError):
        autostart.set_enabled(False)
    assert user.is_symlink() and target.read_bytes() == FOREIGN


def test_replace_error_preserves_previous_file(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-137: a failed atomic replace retains old content and removes its temp file."""
    user, _ = paths
    user.parent.mkdir(parents=True)
    user.write_bytes(FOREIGN)

    def fail_replace(_source: str, _target: Path) -> None:
        raise OSError("test replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(autostart.AutostartError):
        autostart.set_enabled(False)
    assert user.read_bytes() == FOREIGN
    assert list(user.parent.iterdir()) == [user]


def test_state_is_read_only(paths: tuple[Path, Path]) -> None:
    """T-138: reading state does not create a directory or entry."""
    user, _ = paths
    assert not autostart.state().enabled
    assert not user.parent.exists()


@pytest.mark.parametrize("original", [b"", b"Name=Astra Voice\n"])
def test_invalid_group_cannot_silently_disable(paths: tuple[Path, Path], original: bytes) -> None:
    """T-151: an invalid entry cannot be silently disabled or changed."""
    user, _ = paths
    user.parent.mkdir(parents=True)
    user.write_bytes(original)
    with pytest.raises(autostart.AutostartError):
        autostart.set_enabled(False)
    assert autostart.state().enabled
    assert user.read_bytes() == original


def test_bom_hidden_preserves_other_bytes(paths: tuple[Path, Path]) -> None:
    """T-152: adding Hidden to a BOM entry preserves all other bytes."""
    user, _ = paths
    user.parent.mkdir(parents=True)
    original = b"\xef\xbb\xbf[Desktop Entry]\nExec=custom --hidden\n[Other]\nKey=value\n"
    user.write_bytes(original)
    autostart.set_enabled(False)
    assert user.read_bytes() == original.replace(b"[Other]", b"Hidden=true\n[Other]")
    assert not autostart.state().enabled


def test_crlf_hidden_before_next_group(paths: tuple[Path, Path]) -> None:
    """T-153: adding Hidden before the next group preserves CRLF endings."""
    user, _ = paths
    user.parent.mkdir(parents=True)
    original = b"[Desktop Entry]\r\nExec=custom\r\n[Other]\r\nKey=value\r\n"
    user.write_bytes(original)
    autostart.set_enabled(False)
    assert user.read_bytes() == original.replace(b"[Other]", b"Hidden=true\r\n[Other]")


def test_foreign_mode_preserved(paths: tuple[Path, Path]) -> None:
    """T-154: toggling a foreign entry preserves its file permissions."""
    user, _ = paths
    user.parent.mkdir(parents=True)
    user.write_bytes(FOREIGN)
    user.chmod(0o600)
    autostart.set_enabled(False)
    assert stat.S_IMODE(user.stat().st_mode) == 0o600


@pytest.mark.parametrize("value", [b"TRUE", b" 1 ", b"Yes", b"ON"])
def test_true_variants(paths: tuple[Path, Path], value: bytes) -> None:
    """T-155: common true values in Hidden disable autostart."""
    user, _ = paths
    user.parent.mkdir(parents=True)
    user.write_bytes(FOREIGN + b"Hidden=" + value + b"\n")
    assert not autostart.state().enabled


@pytest.mark.parametrize("location", ["user", "system"])
@pytest.mark.parametrize("kind", ["directory", "oversize"])
def test_invalid_entry_rejected(paths: tuple[Path, Path], location: str, kind: str) -> None:
    """T-156: directories and oversized entries are rejected."""
    user, system = paths
    path = user if location == "user" else system
    path.parent.mkdir(parents=True)
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(FOREIGN + b"#" * 65536)
    with pytest.raises(autostart.AutostartError):
        autostart.state()


def test_relative_xdg_paths_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """T-157: relative XDG paths fall back to absolute defaults."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    monkeypatch.setenv("XDG_CONFIG_DIRS", "relative:also-relative")
    user, systems = autostart._paths()
    assert user == Path.home() / ".config/autostart/astra-voice.desktop"
    assert systems == [Path("/etc/xdg/autostart/astra-voice.desktop")]
