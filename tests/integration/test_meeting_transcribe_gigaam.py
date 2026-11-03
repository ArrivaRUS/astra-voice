"""E13.M4: real GigaAM v3 e2e RNN-T + Silero VAD over two synthetic meeting tracks.

Known speech (data/test «Проверка связи») on the mic track from recorded 0 s and
on the far track opened at recorded 30 s with -200 ppm drift; utterances must
carry the text and recorded-axis timecodes within ±1 s of the true speech onset.
"""

from __future__ import annotations

import os
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_engine_gigaam import AMX_XFAIL

from astra_voice.core import paths
from astra_voice.meetings.model import Transcript
from astra_voice.worker import vad
from astra_voice.worker.engine import LAYOUTS, make_engine
from astra_voice.worker.meeting_transcribe import _default_vad
from helpers.meeting_audio import MID, MODEL, Rig, make_disk, write_track

pytestmark = [pytest.mark.engine, pytest.mark.usefixtures("filesystem_euid")]
REPO_ROOT = Path(__file__).resolve().parents[2]
LAYOUT, VARIANT = MODEL["layout"], MODEL["variant"]
FAR_OPEN_MS = 30_000


@pytest.fixture(scope="module", autouse=True)
def runtime() -> None:
    pytest.importorskip("onnxruntime", reason="Для engine-тестов нужен onnxruntime")
    pytest.importorskip("onnx_asr", reason="Для engine-тестов нужен onnx-asr")


@pytest.fixture
def model_dir() -> Path:
    directory = Path(
        os.environ.get(
            "ASTRA_VOICE_TEST_MODEL_DIR", "~/.cache/astra-voice-spike/gigaam-v3/e2e_rnnt"
        )
    ).expanduser()
    if not directory.is_dir():
        pytest.skip(f"Каталог модели GigaAM отсутствует: {directory}")
    for name in LAYOUTS[str(LAYOUT)][str(VARIANT)].required:
        if not (directory / name).is_file():
            pytest.skip(f"Отсутствует обязательный файл модели GigaAM: {directory / name}")
    return directory


@pytest.fixture(autouse=True)
def silero(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if not (REPO_ROOT / "data" / "vad" / "silero_vad.onnx").is_file():
        pytest.skip("Модель Silero VAD отсутствует в data/vad")
    monkeypatch.setattr(paths, "data_dir_static", lambda: REPO_ROOT / "data")
    monkeypatch.setattr(vad, "_session", None)
    monkeypatch.setattr(vad, "_load_attempted", False)
    yield
    vad.unload()


def pcm(name: str) -> np.ndarray[Any, np.dtype[np.int16]]:
    path = REPO_ROOT / "data" / "test" / name
    if not path.is_file():
        pytest.skip(f"Тестовый WAV отсутствует: {path}")
    with wave.open(str(path), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").copy()


def onsets_ms(samples: np.ndarray[Any, np.dtype[np.int16]]) -> list[int]:
    """Independent reference: starts of >=0.3 s loud stretches after >=0.5 s quiet."""
    frame = 160  # 10 ms
    count = len(samples) // frame
    rms = np.sqrt((samples[: count * frame].astype(np.float64) / 32768).reshape(count, frame) ** 2)
    loud = rms.mean(axis=1) > 0.02
    result: list[int] = []
    quiet = 50
    index = 0
    while index < count:
        if loud[index] and quiet >= 50 and loud[index : index + 30].mean() > 0.5:
            result.append(index * 10)
            quiet = 0
        quiet = 0 if loud[index] else quiet + 1
        index += 1
    return result


@AMX_XFAIL
def test_two_tracks_real_gigaam_text_and_recorded_timecodes(
    tmp_path: Path, model_dir: Path
) -> None:
    mic, far = pcm("test-ru-6s.wav"), pcm("test-ru-20s.wav")
    disk = make_disk(tmp_path)
    write_track(disk, "mic", mic, segment_s=4)
    write_track(disk, "far", far, recorded0=FAR_OPEN_MS, mono0=9_000_000, ppm=-200, segment_s=7)
    rig = Rig(disk, engine=make_engine(str(LAYOUT)), vad=_default_vad)
    try:
        rig.send("model.load", **{**MODEL, "dir": str(model_dir)})
        loaded = rig.wait(lambda e: e["type"] in {"model.loaded", "error"}, 60)
        assert loaded["type"] == "model.loaded", loaded
        rig.start()
        accepted = rig.wait(lambda e: e["type"] == "meeting.job.accepted", 30)
        assert accepted["resume_from"] == {"mic_ms": 0, "far_ms": 0}
        done = rig.finish(timeout=240)
        assert done["type"] == "meeting.job.done", done
        items = rig.utterances()
        assert items and [u["seq"] for u in items] == list(range(len(items)))
        assert [u["start_ms"] for u in items] == sorted(u["start_ms"] for u in items)
        for track, audio, offset in (("mic", mic, 0), ("far", far, FAR_OPEN_MS)):
            mine = [u for u in items if u["track"] == track]
            assert mine, track
            text = " ".join(u["text"] for u in mine).casefold()
            assert "провер" in text and "связ" in text, (track, text)
            length = len(audio) * 1000 // 16000
            assert all(
                offset - 1000 <= u["start_ms"] <= u["end_ms"] <= offset + length + 1000
                for u in mine
            )
            reference = [offset + ms for ms in onsets_ms(audio)]
            for utterance in mine:
                # Every utterance begins within ±1 s of a true speech onset or a
                # 24 s split; these files have short phrases, so onsets only.
                assert min(abs(utterance["start_ms"] - r) for r in reference) <= 1000, (
                    utterance["start_ms"],
                    reference,
                )
        final = disk.read(MID, "transcript.json")
        assert isinstance(final, Transcript) and final.labels_final
        assert [u.text for u in final.utterances] == [u["text"] for u in items]
        assert not (disk.root / MID / "checkpoint.json").exists()
    finally:
        rig.close()
