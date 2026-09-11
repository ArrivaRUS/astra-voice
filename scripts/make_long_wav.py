#!/usr/bin/env python3
"""Сборка длинного тестового WAV из короткого без sox и внешних зависимостей."""

from __future__ import annotations

import argparse
import wave
from pathlib import Path

DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / "data/test/test-ru-6s.wav"
FORMAT = (1, 2, 16000, "NONE")


def build_long_wav(source: Path, target: Path, seconds: float) -> int:
    """Повторяет исходник до ближайшего к seconds кадра; возвращает число кадров.

    Готовый файл нужного формата и длительности не перезаписывается.
    В памяти хранится только исходник, результат пишется по одному повтору.
    """
    if not 0 < seconds < float("inf"):
        raise ValueError("Длительность должна быть положительным конечным числом секунд.")
    frame_count = round(seconds * 16000)
    if frame_count < 1:
        raise ValueError("Длительность должна составлять хотя бы один кадр.")

    with wave.open(str(source), "rb") as wav:
        if (
            wav.getnchannels(),
            wav.getsampwidth(),
            wav.getframerate(),
            wav.getcomptype(),
        ) != FORMAT:
            raise ValueError("Ожидается WAV PCM16: моно, 16 кГц, 16 бит, comptype NONE.")
        source_frames = wav.getnframes()
        frames = wav.readframes(source_frames)
    if not source_frames:
        raise ValueError("Исходный WAV пуст: нет кадров для повторения.")
    if len(frames) != source_frames * 2:
        raise ValueError("Исходный WAV неполный: число кадров не совпадает с заголовком.")

    if target.exists():
        with wave.open(str(target), "rb") as wav:
            if (
                wav.getnchannels(),
                wav.getsampwidth(),
                wav.getframerate(),
                wav.getcomptype(),
            ) == FORMAT and wav.getnframes() == frame_count:
                print(f"{target}: уже существует с нужной длительностью, пропускаем.")
                return frame_count

    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        remaining = frame_count
        while remaining:
            count = min(source_frames, remaining)
            wav.writeframes(frames[: count * 2])
            remaining -= count
    print(f"{target}: записано {frame_count} кадров ({frame_count / 16000:g} с).")
    return frame_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="исходный WAV")
    parser.add_argument(
        "--target", type=Path, required=True, help="путь результата вне репозитория"
    )
    parser.add_argument("--seconds", type=float, default=130, help="длительность в секундах")
    args = parser.parse_args(argv)
    try:
        build_long_wav(args.source, args.target, args.seconds)
    except (OSError, EOFError, wave.Error, ValueError, OverflowError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
