"""Сборка длинного WAV: формат, повторение кадров и отсутствие перезаписи."""

from __future__ import annotations

import importlib.util
import os
import sys
import wave
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/make_long_wav.py"

pytestmark = pytest.mark.unit


def load_make_long_wav() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_long_wav", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_long_wav"] = module
    spec.loader.exec_module(module)
    return module


def write_source(path: Path, channels: int = 1) -> bytes:
    frames = b"".join(sample.to_bytes(2, "little", signed=True) for sample in range(-1600, 1600))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(frames * channels)
    return frames


@pytest.mark.parametrize("frame_count", [16000, 16001])
def test_repeat_and_skip_existing(tmp_path: Path, frame_count: int) -> None:
    module = load_make_long_wav()
    source = tmp_path / "source.wav"
    target = tmp_path / "nested" / "long.wav"
    frames = write_source(source)
    seconds = frame_count / 16000

    assert module.build_long_wav(source, target, seconds) == frame_count
    with wave.open(str(target), "rb") as wav:
        assert wav.getnframes() == frame_count
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getcomptype() == "NONE"
        assert wav.readframes(frame_count + 1) == (frames * 6)[: frame_count * 2]

    # Старое время изменения позволяет обнаружить перезапись без sleep.
    os.utime(target, ns=(1_000_000_000, 1_000_000_000))
    before = target.stat().st_mtime_ns
    content = target.read_bytes()
    assert module.build_long_wav(source, target, seconds) == frame_count
    assert target.stat().st_mtime_ns == before
    assert target.read_bytes() == content


def test_reject_stereo(tmp_path: Path) -> None:
    source = tmp_path / "stereo.wav"
    target = tmp_path / "long.wav"
    write_source(source, channels=2)

    with pytest.raises(ValueError, match="PCM16: моно, 16 кГц, 16 бит, comptype NONE"):
        load_make_long_wav().build_long_wav(source, target, 1)
    assert not target.exists()


def test_cli_and_skip_message(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "source.wav"
    target = tmp_path / "long.wav"
    write_source(source)
    module = load_make_long_wav()
    args = ["--source", str(source), "--target", str(target), "--seconds", "1"]

    assert module.main(args) == 0
    capsys.readouterr()
    assert module.main(args) == 0
    assert "уже существует с нужной длительностью" in capsys.readouterr().out


def test_cli_requires_target() -> None:
    with pytest.raises(SystemExit, match="2"):
        load_make_long_wav().main([])
