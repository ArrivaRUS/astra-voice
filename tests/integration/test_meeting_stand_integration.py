"""Explicitly opted-in private real-audio stand, never the desktop server."""

from __future__ import annotations

import os
import shutil

import pytest
from tools.meeting_stand import run_stand

pytestmark = [pytest.mark.machine, pytest.mark.stress, pytest.mark.usefixtures("filesystem_euid")]


def test_private_pipewire_capture_one_minute() -> None:
    if os.environ.get("ASTRA_VOICE_MEETING_STAND") != "1":
        pytest.skip("set ASTRA_VOICE_MEETING_STAND=1 for the private real-server stand")
    if any(
        shutil.which(name) is None
        for name in ("pipewire", "wireplumber", "pipewire-pulse", "pactl", "paplay")
    ):
        pytest.skip("private PipeWire tools unavailable")
    rc, report = run_stand(minutes=1)
    assert rc == 0, report
    assert report["cleanup"] is True
