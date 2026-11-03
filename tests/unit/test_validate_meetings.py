"""The meetings stand is explicit, bounded and reports 0/1/2 unchanged."""

from __future__ import annotations

import json
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> tuple[Callable[[list[str]], int], list[dict[str, Any]]]:
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    calls: list[dict[str, Any]] = []
    module = ModuleType("tools.meeting_stand")

    def run_stand(**kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls.append(kwargs)
        return 0, {"exit_code": 0, "verdict": "pass", "criteria": {}}

    module.__dict__["run_stand"] = run_stand
    monkeypatch.setitem(sys.modules, "tools.meeting_stand", module)
    namespace = runpy.run_path(str(ROOT / "tools/validate"))
    return cast(Callable[[list[str]], int], namespace["main"]), calls


@pytest.mark.parametrize("server", ["pipewire", "pulseaudio"])
def test_meetings_stand_arguments_reach_isolated_runner(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    server: str,
) -> None:
    main, calls = cli
    assert (
        main(["meetings", "--stand", "--minutes", "2", "--server", server, "--json", "--keep"]) == 0
    )
    assert calls == [{"minutes": 2.0, "server": server, "keep": True}]
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1 and not captured.err
    assert json.loads(captured.out) == {"exit_code": 0, "verdict": "pass", "criteria": {}}


def test_meetings_default_is_sixty_minutes_without_retained_processes(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    main, calls = cli
    assert main(["meetings", "--stand", "--json"]) == 0
    assert calls == [{"minutes": 60.0, "server": "pipewire", "keep": False}]
    capsys.readouterr()


def test_meetings_profile_reaches_only_explicit_stand_runner(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    main, calls = cli
    profile = tmp_path / "capture.pstats"
    assert main(["meetings", "--stand", "--profile", str(profile), "--json"]) == 0
    assert calls == [{"minutes": 60.0, "server": "pipewire", "keep": False, "profile": profile}]
    capsys.readouterr()
    with pytest.raises(SystemExit) as error:
        main(["meetings", "--profile", str(profile)])
    assert error.value.code == 2
    assert len(calls) == 1


@pytest.mark.parametrize("code", [0, 1, 2])
def test_meetings_preserves_runner_verdict_and_return_code(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int,
) -> None:
    main, _calls = cli
    facts = {"exit_code": code, "verdict": ["pass", "fail", "unavailable"][code]}
    monkeypatch.setattr(
        sys.modules["tools.meeting_stand"], "run_stand", lambda **kwargs: (code, facts)
    )
    assert main(["meetings", "--stand", "--json"]) == code
    assert json.loads(capsys.readouterr().out) == facts


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--minutes", "0"],
        ["--minutes", "-1"],
        ["--minutes", "nan"],
        ["--minutes", "inf"],
        ["--server", "remote"],
        ["--minutes", "text"],
    ],
)
def test_meetings_rejects_implicit_or_invalid_stand_without_running(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]], arguments: list[str]
) -> None:
    main, calls = cli
    with pytest.raises(SystemExit) as error:
        main(["meetings", *([] if not arguments else ["--stand"]), *arguments])
    assert error.value.code == 2 and not calls


def test_meetings_text_mode_reports_verdict(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]], capsys: pytest.CaptureFixture[str]
) -> None:
    main, _calls = cli
    assert main(["meetings", "--stand"]) == 0
    assert "pass" in capsys.readouterr().out


def test_profile_help_identifies_json_not_pstats(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    main, _ = cli
    with pytest.raises(SystemExit) as result:
        main(["meetings", "--help"])
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    assert "JSON capture-worker" in help_text
    assert "pstats" not in help_text


# ---- meetings --transcribe (E13.M4) -----------------------------------------

from tools import meeting_transcribe_check as check  # noqa: E402


@pytest.fixture
def transcribe_cli(
    cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Callable[[list[str]], int], list[dict[str, Any]]]:
    main, _stand_calls = cli
    calls: list[dict[str, Any]] = []
    module = ModuleType("tools.meeting_transcribe_check")
    module.__dict__.update(
        text_report=check.text_report,
        progress_printer=check.progress_printer,
    )

    def run_transcribe(corpus: Path, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls.append({"corpus": corpus, **kwargs})
        return 0, {"verdict": "pass", "exit_code": 0}

    module.__dict__["run_transcribe"] = run_transcribe
    monkeypatch.setitem(sys.modules, "tools.meeting_transcribe_check", module)
    return main, calls


def test_transcribe_defaults_reach_the_runner(
    transcribe_cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    main, calls = transcribe_cli
    assert main(["meetings", "--transcribe", str(tmp_path), "--json"]) == 0
    [call] = calls
    progress = call.pop("progress")
    assert call == {
        "corpus": tmp_path,
        "minutes": None,
        "model_dir": None,
        "threads": 2,
        "echo_gate": False,
        "out": None,
    }
    progress("asr", 1, 2)  # JSON mode: silent progress
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"verdict": "pass", "exit_code": 0}
    assert len(captured.out.splitlines()) == 1 and not captured.err


def test_transcribe_explicit_options_and_exit_code(
    transcribe_cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    main, calls = transcribe_cli

    def failing(corpus: Path, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        calls.append(kwargs)
        return 1, {"verdict": "fail"}

    monkeypatch.setattr(sys.modules["tools.meeting_transcribe_check"], "run_transcribe", failing)
    arguments = [
        "meetings",
        "--transcribe",
        str(tmp_path),
        "--minutes",
        "10",
        "--model-dir",
        str(tmp_path / "model"),
        "--threads",
        "1",
        "--echo-gate",
        "--out",
        str(tmp_path / "out"),
    ]
    assert main(arguments) == 1
    [call] = calls
    assert (call["minutes"], call["threads"], call["echo_gate"]) == (10.0, 1, True)
    assert (call["model_dir"], call["out"]) == (tmp_path / "model", tmp_path / "out")
    call["progress"]("asr", 50, 100)  # text mode: progress goes to stderr only
    captured = capsys.readouterr()
    assert captured.out.startswith("meetings --transcribe: fail")
    assert "прогресс asr: 50 %" in captured.err


@pytest.mark.parametrize(
    "arguments",
    [
        ["--stand", "--transcribe", "corpus"],
        ["--transcribe", "corpus", "--server", "pipewire"],
        ["--transcribe", "corpus", "--keep"],
        ["--transcribe", "corpus", "--profile", "p.json"],
        ["--stand", "--out", "dir"],
        ["--stand", "--model-dir", "dir"],
        ["--stand", "--echo-gate"],
        ["--stand", "--threads", "2"],
        ["--transcribe", "corpus", "--minutes", "0"],
        ["--transcribe", "corpus", "--threads", "0"],
        ["--transcribe"],
    ],
)
def test_transcribe_rejects_mixed_or_invalid_modes_without_running(
    transcribe_cli: tuple[Callable[[list[str]], int], list[dict[str, Any]]],
    arguments: list[str],
) -> None:
    main, calls = transcribe_cli
    with pytest.raises(SystemExit) as error:
        main(["meetings", *arguments])
    assert error.value.code == 2 and not calls


def test_normalization_lowercase_yo_and_punctuation() -> None:
    assert check.normalize_words("Ёжик, «ЁЛКА» — ну-ка 2026!") == [
        "ежик",
        "елка",
        "ну",
        "ка",
        "2026",
    ]


@pytest.mark.parametrize(
    ("reference", "hypothesis", "errors"),
    [
        ("а б в", "а б в", 0),
        ("а б в", "а х в", 1),  # substitution
        ("а б в", "а в", 1),  # deletion
        ("а б в", "а б х в", 1),  # insertion
        ("а б в г", "б в г д", 2),  # shifted: deletion + insertion
        ("", "а б", 2),
        ("а б", "", 2),
        ("к о т ё н о к", "с и д е н и е", 6),
        ("раз два три четыре пять", "два три пять шесть", 3),
    ],
)
def test_word_errors_known_pairs(reference: str, hypothesis: str, errors: int) -> None:
    assert check.word_errors(reference.split(), hypothesis.split()) == errors


def test_word_errors_match_textbook_dynamic_programming() -> None:
    import random

    def slow(a: list[str], b: list[str]) -> int:
        row = list(range(len(b) + 1))
        for i, x in enumerate(a, 1):
            previous, row[0] = row[0], i
            for j, y in enumerate(b, 1):
                previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (x != y))
        return row[-1]

    rng = random.Random(7)
    for _ in range(200):
        a = [rng.choice("абвг") for _ in range(rng.randrange(0, 12))]
        b = [rng.choice("абвг") for _ in range(rng.randrange(0, 12))]
        assert check.word_errors(a, b) == slow(a, b), (a, b)


def test_wer_percent_and_empty_reference() -> None:
    assert check.wer("а б в г".split(), "а б х г".split()) == 25.0
    assert check.wer([], ["а"]) is None


def test_percentile_is_nearest_rank_and_deltas_use_nearest_start() -> None:
    values = list(range(1, 101))
    assert (check.percentile(values, 50), check.percentile(values, 95)) == (50, 95)
    assert check.percentile([7], 95) == 7 and check.percentile([], 50) is None
    assert check.nearest_deltas([0, 1000, 5000], [200, 4100, 9000]) == [200, 800, 900]
    assert check.nearest_deltas([1000], []) == []


def corpus_document() -> dict[str, Any]:
    def words(speaker: str, start: float, text: str) -> list[dict[str, Any]]:
        return [
            {"speaker": speaker, "start": start + 0.3 * i, "end": start + 0.3 * i + 0.3, "word": w}
            for i, w in enumerate(text.split())
        ]

    return {
        "timing": "approx",
        "utterances": [
            {"speaker": "far1", "start": 0.0, "end": 3.0, "sentences": [{"start": 0.0}]},
            # 0.3 s after far1 on the same track: merges with it in our VAD.
            {"speaker": "far2", "start": 3.3, "end": 5.0, "sentences": [{"start": 3.3}]},
            {"speaker": "owner1", "start": 4.0, "end": 6.0, "sentences": [{"start": 4.0}]},
            {"speaker": "far3", "start": 9.0, "end": 11.0, "sentences": [{"start": 9.0}]},
            # Starts 0.5 s before a 12 s cut: not enough sound to be expected.
            {"speaker": "far1", "start": 11.5, "end": 14.0, "sentences": [{"start": 11.5}]},
        ],
        "words": [
            *words("far1", 0.0, "Был мальчик, звали его"),
            *words("far2", 3.3, "Пошли раз"),
            *words("owner1", 4.0, "Ёлка стоит"),
            *words("far3", 9.0, "Филипп взял"),
            *words("far1", 11.5, "шапку и пошёл"),
        ],
    }


def test_reference_cut_tracks_and_separable_starts() -> None:
    reference = check.load_reference(corpus_document(), 12_000)
    assert reference.words["mic"] == ["елка", "стоит"]
    # Words whose middle is before the cut; «пошёл» (12.1–12.4 s) is cut off.
    assert reference.words["far"][-4:] == ["филипп", "взял", "шапку", "и"]
    assert reference.starts == {"mic": [4000], "far": [0, 3300, 9000]}
    assert reference.separable == {"mic": [4000], "far": [0, 9000]}


def utterance(seq: int, track: str, start: int, text: str) -> dict[str, Any]:
    return {"seq": seq, "track": track, "start_ms": start, "end_ms": start + 1000, "text": text}


DONE = {"wall_ms": 30_000, "peak_rss_kb": 512_000, "echo_dropped": 0}


def test_evaluate_passes_with_close_timecodes_and_reports_numbers_only() -> None:
    reference = check.load_reference(corpus_document(), 12_000)
    ours = [
        utterance(0, "far", 150, "Был мальчик звали его."),
        utterance(1, "far", 3_500, "Пошли раз"),
        utterance(2, "mic", 4_180, "Ёлка стоит!"),
        utterance(3, "far", 8_800, "Филипп взял шапку и"),
        utterance(4, "far", 11_000, "[не распознано]"),
    ]
    report = check.evaluate(reference, ours, audio_ms=12_000, wall_ms=600, done=DONE)
    assert report["wer"]["mic"] == {"percent": 0.0, "reference_words": 2, "hypothesis_words": 2}
    assert report["wer"]["far"]["percent"] == 0.0  # the placeholder is not text
    # Gate: every reply of ours -> nearest reference onset of its track.
    assert report["timecodes"] == {
        "n": 4,
        "p50": 180,
        "p95": 200,
        "max": 200,
        "missing_tracks": [],
    }
    assert (report["utterances"], report["unrecognized"]) == (5, 1)
    assert report["rtf"] == 0.05 and report["hour_wall_ms"] == 180_000
    assert report["verdicts"]["timecodes"] == "pass" and report["verdicts"]["job"] == "pass"
    assert report["verdicts"]["speed"].startswith("pass")
    assert (report["verdict"], report["exit_code"]) == ("pass", 0)
    dumped = json.dumps(report, ensure_ascii=False) + "\n".join(check.text_report(report))
    for word in ("мальчик", "Филипп", "Ёлка", "распознано]"):
        assert word not in dumped


def test_merged_turn_is_information_only_late_reply_or_silent_track_fail() -> None:
    reference = check.load_reference(corpus_document(), 12_000)
    merged = [
        utterance(0, "far", 100, "был мальчик звали его пошли раз"),
        utterance(1, "mic", 4_100, "елка стоит"),
        utterance(2, "far", 9_100, "филипп взял шапку и"),
    ]
    report = check.evaluate(reference, merged, audio_ms=12_000, wall_ms=600, done=DONE)
    # far2 (3.3 s) joined far1 by VAD: «reference -> ours» shows it, without a verdict.
    info = report["reference_to_ours"]
    assert (info["max"], info["after_pause_1s"]["max"]) == (3_200, 100)
    assert "0,5 с" in info["note"] and "reference_to_ours" not in report["verdicts"]
    assert report["timecodes"]["max"] == 100
    assert report["verdicts"]["timecodes"] == "pass" and report["exit_code"] == 0
    late = [*merged, utterance(3, "far", 6_500, "лишнее")]  # 2.5 s from any onset
    report = check.evaluate(reference, late, audio_ms=12_000, wall_ms=600, done=DONE)
    assert report["timecodes"]["max"] == 2_500
    assert report["verdicts"]["timecodes"] == "fail" and report["exit_code"] == 1
    no_mic = [u for u in merged if u["track"] == "far"]
    report = check.evaluate(reference, no_mic, audio_ms=12_000, wall_ms=600, done=DONE)
    assert report["timecodes"]["missing_tracks"] == ["mic"]
    assert report["wer"]["mic"]["percent"] == 100.0
    assert report["verdict"] == "fail"
    lines = "\n".join(check.text_report(report))
    assert "дорожки без реплик: mic" in lines and "без вердикта" in lines
    close = [utterance(0, "far", 0, "x"), utterance(1, "far", 3_300, "x")]
    close += [utterance(2, "mic", 4_000, "x"), utterance(3, "far", 9_000, "x")]
    report = check.evaluate(reference, close, audio_ms=12_000, wall_ms=6_000, done=None)
    assert report["verdicts"] == {
        "job": "fail",
        "timecodes": "pass",
        "speed": "fail (ориентир: эталон — ноутбук)",
        "wer": "ориентир: WER диктовки на этом TTS-корпусе неизвестен",
    }
    assert (report["verdict"], report["exit_code"]) == ("fail", 1)


def test_missing_modules_are_named_and_stop_before_any_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = {"onnxruntime", "jsonschema"}
    assert check.missing_modules(lambda name: None if name in absent else object()) == [
        "onnxruntime",
        "jsonschema",
    ]
    monkeypatch.setattr(check, "missing_modules", lambda: ["onnxruntime", "onnx_asr"])
    out = tmp_path / "out"
    code, report = check.run_transcribe(tmp_path, out=out)
    assert (code, report["verdict"]) == (2, "unavailable")
    assert report["failure"] == "modules-missing: onnxruntime, onnx_asr"
    assert "onnxruntime, onnx_asr" in "\n".join(check.text_report(report))
    assert not out.exists()


@pytest.fixture
def modules_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check, "missing_modules", lambda: [])


@pytest.mark.usefixtures("modules_present")
def test_run_transcribe_without_model_is_unavailable_and_leaves_no_files(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    out = tmp_path / "out"
    code, report = check.run_transcribe(corpus, model_dir=tmp_path / "absent", out=out)
    assert (code, report["verdict"], report["failure"]) == (2, "unavailable", "model-missing")
    assert not out.exists()


@pytest.mark.usefixtures("modules_present")
def test_run_transcribe_refuses_non_empty_out_and_bad_corpus(tmp_path: Path) -> None:
    from astra_voice.worker.engine import LAYOUTS

    model = tmp_path / "model"
    model.mkdir()
    for name in LAYOUTS[str(check.MODEL["layout"])][str(check.MODEL["variant"])].required:
        (model / name).write_bytes(b"")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    code, report = check.run_transcribe(corpus, model_dir=model, out=tmp_path / "o1")
    assert (code, report["failure"]) == (2, "corpus-transcript")
    (corpus / "transcript.json").write_text(json.dumps(corpus_document()), encoding="utf-8")
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep").write_text("x")
    code, report = check.run_transcribe(corpus, model_dir=model, out=busy)
    assert (code, report["failure"]) == (2, "out-not-empty")
    assert sorted(p.name for p in busy.iterdir()) == ["keep"]
    code, report = check.run_transcribe(corpus, model_dir=model, out=tmp_path / "o2")
    assert (code, report["failure"]) == (2, "corpus-unreadable")  # no mic.wav/far.wav


# Real private store files: the owner check needs the real euid (root in CI).
@pytest.mark.usefixtures("filesystem_euid")
def test_build_meeting_writes_m2_segments_from_the_corpus(tmp_path: Path) -> None:
    import wave

    import numpy as np

    from astra_voice.meetings.model import Meeting
    from astra_voice.meetings.segments import SegmentSidecar
    from astra_voice.meetings.store import MeetingStore

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name, value in (("mic.wav", 1), ("far.wav", 2)):
        with wave.open(str(corpus / name), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(np.full(16000 * 7, value, dtype="<i2").tobytes())
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    original = check.SEGMENT_S
    try:
        check.SEGMENT_S = 3  # tiny segments for the test: 3 + 3 + 0.5 s
        assert check.build_meeting(corpus, data, 6.5 / 60) == 6_500
    finally:
        check.SEGMENT_S = original
    store = MeetingStore(data / "astra-voice" / "meetings")
    meeting = store.read(check.MEETING_ID, "meeting.json")
    assert isinstance(meeting, Meeting) and meeting.recorded_ms == 6_500
    audio = store.root / check.MEETING_ID / "audio"
    assert sorted(p.name for p in audio.glob("*.wav")) == [
        f"{track}-{index:04d}.wav" for track in ("far", "mic") for index in range(3)
    ]
    last = store.read(check.MEETING_ID, "audio/far-0002.json")
    assert isinstance(last, SegmentSidecar)
    assert (last.sample_start, last.sample_end, last.recorded_ms_at_open) == (
        96_000,
        104_000,
        6_000,
    )
    assert last.closed and last.close_reason == "stop" and last.timeline[-1][0] == 104_000
    with wave.open(str(audio / "far-0002.wav"), "rb") as wav:
        assert wav.getnframes() == 8_000
        assert set(np.frombuffer(wav.readframes(8_000), dtype="<i2")) == {2}
