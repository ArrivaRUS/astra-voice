"""Actual resource staging recipes preserve the meeting app catalog.

No git, vendor download, models, audio, install or full AppImage toolchain.
The AppImage test executes its resource block, not a parallel copy recipe.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from astra_voice.meetings.apps import AppClass, AppVerification, load_apps_for_tests

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
PACKAGING_TIMEOUT = 27


def check_catalog(path: Path) -> None:
    assert path.read_bytes() == (ROOT / "data/meeting-apps.json").read_bytes()
    catalog = load_apps_for_tests(path)
    assert {AppClass.DION, AppClass.BROWSER} <= {app.app_class for app in catalog}
    assert all(app.verification is AppVerification.EXECUTABLE and app.exe for app in catalog)


def _run_in_group(argv: list[str], *, deadline: float, ready_marker: Path | None = None) -> None:
    # The whole check (including copying and Python assertions) shares one deadline.
    child = subprocess.Popen(
        argv,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        wait_deadline = deadline
        if ready_marker is not None:
            while not ready_marker.exists():
                remaining = deadline - time.monotonic()
                assert remaining > 0, "packaging descendant did not signal readiness"
                assert child.poll() is None, "packaging leader exited before readiness"
                time.sleep(min(0.01, remaining))
            wait_deadline = min(deadline, time.monotonic() + 0.2)
        output, _ = child.communicate(timeout=max(0.0, wait_deadline - time.monotonic()))
    except BaseException:
        # make/bash/dpkg-deb may have descendants that still hold the output pipe.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=1)
        finally:
            if child.stdout is not None:
                child.stdout.close()
        raise
    assert child.returncode == 0, output.decode(errors="replace")


def _check_deb(tmp_path: Path) -> None:
    stage = tmp_path / "deb"
    subprocess.run(
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
        check=True,
    )
    check_catalog(stage / "usr/share/astra-voice/data/meeting-apps.json")
    # A real local .deb archive. Fast compression keeps the staging check within
    # its deadline even when other tests are building or compressing archives.
    control = stage / "DEBIAN"
    control.mkdir()
    control.chmod(0o755)
    (control / "control").write_text(
        "Package: astra-voice\nVersion: 0.0.0\nArchitecture: all\n"
        "Maintainer: Test <test@example.invalid>\nDescription: Resource staging test\n"
    )
    archive = tmp_path / "test.deb"
    subprocess.run(
        ["dpkg-deb", "-Zgzip", "-z1", "--build", "--root-owner-group", str(stage), str(archive)],
        check=True,
    )
    extracted = tmp_path / "extracted"
    subprocess.run(["dpkg-deb", "--extract", str(archive), str(extracted)], check=True)
    check_catalog(extracted / "usr/share/astra-voice/data/meeting-apps.json")
    for resource in ("MeetingOffer.qml", "components/MeetingOfferControl.qml"):
        assert (extracted / "usr/share/astra-voice/qml" / resource).read_bytes() == (
            ROOT / "qml" / resource
        ).read_bytes()


def _check_appimage(tmp_path: Path) -> None:
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
    subprocess.run(
        ["bash", "-euc", block],
        env={**os.environ, "ROOT": str(ROOT), "BUILD": str(build), "SHARE": str(share)},
        check=True,
    )
    check_catalog(share / "data/meeting-apps.json")


def test_catalog_in_deb_staging_and_archive(tmp_path: Path) -> None:
    _run_in_group(
        [sys.executable, __file__, "deb", str(tmp_path)],
        deadline=time.monotonic() + PACKAGING_TIMEOUT,
    )


def test_catalog_in_actual_appimage_resource_staging_block(tmp_path: Path) -> None:
    _run_in_group(
        [sys.executable, __file__, "appimage", str(tmp_path)],
        deadline=time.monotonic() + PACKAGING_TIMEOUT,
    )


def test_packaging_deadline_kills_a_descendant_during_python_work(tmp_path: Path) -> None:
    # The check process hangs in Python after launching a child, as it could
    # during copying or validation. Both must stop before any timed-out step returns.
    deadline = time.monotonic() + PACKAGING_TIMEOUT
    leader = """\
import subprocess
import sys
import time
subprocess.Popen([sys.executable, "-c", sys.argv[1], sys.argv[2]])
time.sleep(60)
"""
    descendant = """\
import os
import sys
import time
from pathlib import Path
# Reproduce readiness taking longer than the old two-second timeout.
time.sleep(2.2)
marker = Path(sys.argv[1])
pending = marker.with_suffix(".tmp")
pending.write_text(f"{os.getpgrp()} {os.getpid()}")
pending.replace(marker)
time.sleep(60)
"""
    marker = tmp_path / "group"
    try:
        with pytest.raises(subprocess.TimeoutExpired) as expired:
            _run_in_group(
                [sys.executable, "-c", leader, descendant, str(marker)],
                deadline=deadline,
                ready_marker=marker,
            )
        assert expired.value.timeout <= 0.21
        _, pid = marker.read_text().split()
        stat = Path("/proc") / pid / "stat"
        cleanup_deadline = min(deadline, time.monotonic() + 2)
        while time.monotonic() < cleanup_deadline:
            try:
                state = stat.read_bytes().rpartition(b") ")[2].split()[0]
            except FileNotFoundError:
                break
            if state == b"Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail("packaging descendant survived the group deadline")
    finally:
        if marker.exists():
            group, _ = marker.read_text().split()
            try:
                os.killpg(int(group), signal.SIGKILL)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    kind, target = sys.argv[1:]
    {"deb": _check_deb, "appimage": _check_appimage}[kind](Path(target))
