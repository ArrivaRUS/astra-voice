"""Actual resource staging recipes preserve the meeting app catalog.

No git, vendor download, models, audio, install or full AppImage toolchain.
The AppImage test executes its resource block, not a parallel copy recipe.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from astra_voice.meetings.apps import AppClass, load_apps_for_tests

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def check_catalog(path: Path) -> None:
    assert path.read_bytes() == (ROOT / "data/meeting-apps.json").read_bytes()
    classes = {app.app_class for app in load_apps_for_tests(path)}
    assert {AppClass.DION, AppClass.BROWSER} <= classes


def test_catalog_in_deb_staging_and_archive(tmp_path: Path) -> None:
    stage = tmp_path / "deb"
    result = subprocess.run(
        [
            "make",
            "-f",
            "packaging/debian/rules",
            "override_dh_auto_install",
            f"PKG={stage}",
            "VENDOR=",
        ],
        cwd=ROOT,
        env={**os.environ, "ASTRA_VOICE_VERSION_FILE": "", "ASTRA_VOICE_VENDOR": ""},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
    check_catalog(stage / "usr/share/astra-voice/data/meeting-apps.json")
    # A real local .deb archive (no installation, signing or vendor wheels).
    control = stage / "DEBIAN"
    control.mkdir()
    control.chmod(0o755)
    (control / "control").write_text(
        "Package: astra-voice\nVersion: 0.0.0\nArchitecture: all\n"
        "Maintainer: Test <test@example.invalid>\nDescription: Resource staging test\n"
    )
    archive = tmp_path / "test.deb"
    subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(stage), str(archive)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    extracted = tmp_path / "extracted"
    subprocess.run(
        ["dpkg-deb", "--extract", str(archive), str(extracted)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    check_catalog(extracted / "usr/share/astra-voice/data/meeting-apps.json")


def test_catalog_in_actual_appimage_resource_staging_block(tmp_path: Path) -> None:
    script = (ROOT / "packaging/appimage/build.sh").read_text()
    # Source selection in the full builder covers data; do not invoke git here.
    assert "ls-files -z -- src/astra_voice qml data" in script
    start = script.index('mv "$BUILD/src/data" "$SHARE/data"')
    end = script.index("# Значки темы", start)
    block = script[start:end]
    assert "git " not in block
    build = tmp_path / "build"
    share = tmp_path / "AppDir/usr/share/astra-voice"
    shutil.copytree(ROOT / "data", build / "src/data")
    share.mkdir(parents=True)
    result = subprocess.run(
        ["bash", "-euc", block],
        env={**os.environ, "ROOT": str(ROOT), "BUILD": str(build), "SHARE": str(share)},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
    check_catalog(share / "data/meeting-apps.json")
