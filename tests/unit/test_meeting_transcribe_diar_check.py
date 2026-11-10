"""CLI input layout and summary use the same complete job as production."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest
from tools.meeting_transcribe_diar_check import prepare_meeting

from astra_voice.meetings.model import Meeting
from astra_voice.meetings.segments import SegmentSidecar

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_prepare_wav_tracks_has_valid_sidecars_and_generation(tmp_path: Path) -> None:
    path = tmp_path / "far.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x00" * 32000)
    store, mid = prepare_meeting(tmp_path / "run", {"far": path}, duration_s=1)
    meeting = store.read(mid, "meeting.json")
    assert isinstance(meeting, Meeting) and meeting.recorded_ms == 1000
    sidecar = store.read(mid, "audio/far-0000.json")
    assert isinstance(sidecar, SegmentSidecar)
    assert sidecar.sample_end == sidecar.frames == 16000 and sidecar.closed
    assert sidecar.recorded_ms_at_open == 0


def test_prepare_rejects_wrong_wav_format(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((2, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00" * 128)
    with pytest.raises(ValueError, match="wav-must"):
        prepare_meeting(tmp_path / "run", {"mic": path})


@pytest.mark.parametrize("explicit", [False, True])
def test_complete_cli_job_summary_with_scripted_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    from typing import Any

    from tools import meeting_transcribe_diar_check as check

    from astra_voice.worker.meeting_transcribe import MeetingTranscribeState

    class Peer:
        peak_rss_kb = 1234
        overall_deadline = float("inf")

        def load(self, models: Any) -> None:
            assert models["segmentation_sha256"] == "0" * 64
            assert models["embedder_sha256"] == "0" * 64

        def block(self, audio: Any, index: int, offset: int) -> list[list[Any]]:
            return [[offset, offset + len(audio) // 16, "S1"]]

        def finish(self) -> dict[str, Any]:
            return {"label_map": {"S1": "S1"}, "speakers_total": 1}

        def close(self, *, completed: bool = False) -> bool:
            return completed

    def factory(**options: Any) -> MeetingTranscribeState:
        return MeetingTranscribeState(
            **options, diar_factory=lambda *args, **kwargs: Peer(), mem_available_mb=lambda: 100_000
        )

    monkeypatch.setattr(check, "MeetingTranscribeState", factory)
    path = tmp_path / "room.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x40" * 16000)
    store, mid = prepare_meeting(tmp_path / "run", {"mic": path})
    hashes = {"segmentation_sha256": "0" * 64, "embedder_sha256": "0" * 64}
    models = {"segmentation_dir": "/seg", "embedder_dir": "/emb"}
    if explicit:
        models.update(hashes)
    else:
        from astra_voice.models import diarization

        monkeypatch.setattr(diarization, "catalog_diarization_hashes", lambda: hashes)
    result = check.run_job(store, mid, models, fake_asr=True)
    assert result["diar_status"] == "done" and result["labels_final"]
    assert result["speakers"] == result["utterances"] == result["blocks"] == 1
    assert result["diar_peak_rss_kb"] == 1234
    assert result["transcriber_peak_rss_kb"] > 0
    assert {"asr", "diar", "reconcile", "finalize"} <= result["phase_s"].keys()
