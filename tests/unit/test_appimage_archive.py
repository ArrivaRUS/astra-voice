"""Транспорт AppImage: GNU tar, точные байты/0755, повторяемость и отказ на подмене."""

from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packaging/appimage/archive.py"
NAME = "Astra_Voice-0.1.1~dev11-x86_64.AppImage"


def run(image: Path, *args: str, epoch: str = "1791547200") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, str(image)],
        env={**os.environ, "SOURCE_DATE_EPOCH": epoch},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def make_image(tmp_path: Path) -> Path:
    image = tmp_path / NAME
    # Исполняемая заглушка сообщает о нежелательном запуске payload.
    image.write_bytes(b"#!/bin/sh\ntouch SHOULD_NOT_RUN\nexit 99\n" + bytes(range(256)) * 4096)
    image.chmod(0o600)
    (tmp_path / "user-profile-secret").write_bytes(b"must not be archived")
    return image


@pytest.mark.parametrize("umask", [0o022, 0o077])
def test_gnu_tar_extracts_exact_bytes_with_executable_mode(tmp_path: Path, umask: int) -> None:
    if shutil.which("tar") is None:
        pytest.skip("нужен GNU tar")
    image = make_image(tmp_path)
    result = run(image)
    assert result.returncode == 0, result.stderr
    archive = image.with_name(NAME + ".tar.gz")
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        assert [member.name for member in members] == [NAME]
        assert members[0].mode == 0o755
        assert members[0].mtime == 1791547200
        assert members[0].uid == members[0].gid == 0
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    result = subprocess.run(
        [
            "bash",
            "-c",
            'umask "$1"; tar -xzf "$2" --same-permissions -C "$3"',
            "extract",
            f"{umask:o}",
            str(archive),
            str(extracted),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    restored = extracted / NAME
    assert restored.read_bytes() == image.read_bytes()
    assert stat.S_IMODE(restored.stat().st_mode) == 0o755
    assert sorted(path.name for path in extracted.iterdir()) == [NAME]
    assert stat.S_IMODE(image.stat().st_mode) == 0o600
    assert not (tmp_path / "SHOULD_NOT_RUN").exists()
    assert run(image, "--check").returncode == 0


def test_archive_is_repeatable_across_source_metadata(tmp_path: Path) -> None:
    image = make_image(tmp_path)
    assert run(image).returncode == 0
    archive = image.with_name(NAME + ".tar.gz")
    original = archive.read_bytes()
    image.chmod(0o755)
    os.utime(image, (123, 456))
    assert run(image).returncode == 0
    assert archive.read_bytes() == original
    assert sorted(path.name for path in tmp_path.glob(".appimage-archive-*")) == []


@pytest.mark.parametrize("kind", ["bytes", "mode", "extra", "link", "path"])
def test_check_rejects_unexpected_archive(tmp_path: Path, kind: str) -> None:
    image = make_image(tmp_path)
    archive = image.with_name(NAME + ".tar.gz")
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("../" + NAME if kind == "path" else NAME)
        payload = image.read_bytes()
        if kind == "bytes":
            payload = bytes([payload[0] ^ 1]) + payload[1:]
        member.size = len(payload)
        member.mode = 0o644 if kind == "mode" else 0o755
        if kind == "link":
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            member.size = 0
        bundle.addfile(member, io.BytesIO(payload))
        if kind == "extra":
            bundle.addfile(tarfile.TarInfo("user-profile-secret"), io.BytesIO())
    result = run(image, "--check")
    assert result.returncode == 1
    assert "ОШИБКА" in result.stderr
    assert not (tmp_path / "SHOULD_NOT_RUN").exists()


def test_symlink_input_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"not for distribution")
    image = tmp_path / NAME
    image.symlink_to(source)
    result = run(image)
    assert result.returncode == 1
    assert "не символической ссылкой" in result.stderr
    assert not image.with_name(NAME + ".tar.gz").exists()


def test_invalid_epoch_keeps_previous_archive(tmp_path: Path) -> None:
    image = make_image(tmp_path)
    assert run(image).returncode == 0
    archive = image.with_name(NAME + ".tar.gz")
    previous = archive.read_bytes()
    assert run(image, epoch="-1").returncode == 1
    assert archive.read_bytes() == previous
