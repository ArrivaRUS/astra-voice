"""`tools/benchmark --catalog`: зонд на настоящем WorkerState, родитель без движка."""

from __future__ import annotations

import json
import os
import runpy
import stat
import subprocess
import sys
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

import astra_voice.worker.state as worker_state
from astra_voice.core.measurements import read_measurements, saved_rtfx
from astra_voice.core.version import __version__
from astra_voice.models.store import ModelRecord, ModelStore

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WAV = ROOT / "data/test/test-ru-6s.wav"
SECRET = "секретная фраза"
NUMBERS = {
    "load_ms": 42.0,
    "cold_ms": 900.0,
    "audio_ms": 6000.0,
    "runs": 5,
    "rtfx": 20.0,
    "p50_ms": 300.0,
    "p95_ms": 500.0,
    "vm_hwm_kb": 512000,
    "ram_mb": 524,
    "pss_mb": 410,
}


@pytest.fixture
def bench(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr(sys, "path", sys.path.copy())
    namespace = runpy.run_path(str(ROOT / "tools/benchmark"))
    return cast(dict[str, Any], namespace["main"].__globals__)


@pytest.fixture
def fake_probe(bench: dict[str, Any]) -> list[str]:
    """Родитель без подпроцесса: числа по имени модели, отказ — по подстроке."""
    calls: list[str] = []
    error = bench["BenchmarkError"]

    def probe(record: ModelRecord, **options: Any) -> dict[str, Any]:
        calls.append(record.id)
        assert options["threads"] == 2 and options["runs"] == 5
        if "load-fails" in record.id:
            raise error("воркер сообщил ошибку engine-failed")
        if "empty" in record.id:
            raise error("пустой текст")
        return dict(NUMBERS)

    bench["probe_process"] = probe
    return calls


def install(store: ModelStore, model_id: str, *, broken: bool = False) -> None:
    staging = store.staging_dir(model_id, "r1")
    (staging / "weights.onnx").write_bytes(b"stub")
    (staging / "state.json").write_text(
        json.dumps({"layout": "onnx-asr-gigaam-v3", "variant": "v", "size_bytes": 4}),
        encoding="utf-8",
    )
    store.commit(model_id, "r1")
    if broken:
        store.mark_broken(model_id, "r1", "повреждена в тесте")


def run(bench: dict[str, Any], argv: list[str], capsys: pytest.CaptureFixture[str]) -> Any:
    main = cast(Callable[[list[str]], int], bench["main"])
    code = main(["--catalog", "--json", *argv])
    output = capsys.readouterr().out
    assert "секрет" not in output
    facts = json.loads(output)
    assert facts["exit_code"] == code
    return facts


@dataclass(frozen=True)
class Loaded:
    load_ms: float = 42.0
    engine_version: str = "test"
    sessions: int = 1


@dataclass(frozen=True)
class Heard:
    text: str
    infer_ms: float
    cancelled: bool = False


class BenchEngine:
    """Холодный прогон 900 мс, затем тёплые 100…500 мс."""

    def __init__(self, text: str = SECRET) -> None:
        self.text = text
        self.times: Iterator[float] = iter([900.0, 100.0, 200.0, 300.0, 400.0, 500.0])

    def load(self, model_dir: Path, layout: str, variant: str, threads: int) -> Loaded:
        assert threads == 2
        return Loaded()

    def transcribe(self, audio: Any, cancel: Any) -> Heard:
        return Heard(self.text, next(self.times))

    def unload(self) -> None:
        pass


def probe_options(**extra: Any) -> dict[str, Any]:
    return {
        "model_id": "a-model",
        "revision": "r1",
        "model_dir": Path("/nonexistent"),
        "layout": "onnx-asr-gigaam-v3",
        "variant": "v",
        "wav": WAV,
        "runs": 5,
        "threads": 2,
        "min_ram_mb": 768,
        **extra,
    }


def test_probe_uses_worker_rules(bench: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    # Без VAD длительность звука равна длине WAV: числа предсказуемы.
    monkeypatch.setattr(worker_state, "_available_vad", lambda: None)
    with wave.open(str(WAV), "rb") as stream:
        audio_ms = stream.getnframes() / stream.getframerate() * 1000

    result = bench["run_probe"](**probe_options(engine_factory=lambda layout: BenchEngine()))

    assert result["runs"] == 5
    assert result["cold_ms"] == 900.0  # холодный прогон не входит в медиану
    # RTFx по тёплым: audio/100…audio/500 → медиана audio/300.
    assert result["rtfx"] == round(audio_ms / 300, 2)
    assert result["p50_ms"] == 300.0
    assert result["p95_ms"] == 500.0  # ближайший ранг из 5 — максимум
    assert result["ram_mb"] == round(result["vm_hwm_kb"] * 1024 / 1_000_000) > 0
    assert SECRET not in json.dumps(result, ensure_ascii=False)


def test_probe_rejects_empty_text(bench: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_state, "_available_vad", lambda: None)
    with pytest.raises(bench["BenchmarkError"], match="пустой текст"):
        bench["run_probe"](**probe_options(engine_factory=lambda layout: BenchEngine(" ")))


def test_measures_models_and_writes_app_format(
    bench: dict[str, Any],
    fake_probe: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = ModelStore(tmp_path / "store")
    for model_id in ("a-model", "b-model", "c-model"):
        install(store, model_id)
    out = tmp_path / "measurements.json"

    facts = run(
        bench, ["--store", str(store.root), "--minimum-models", "3", "--out", str(out)], capsys
    )

    assert facts["exit_code"] == 0
    assert facts["measured"] == 3
    assert facts["readable_by_app"] is True
    assert fake_probe == ["a-model", "b-model", "c-model"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    data = read_measurements(out)
    assert set(data) == {"a-model@r1", "b-model@r1", "c-model@r1"}
    assert data["a-model@r1"] | {"measured_at": ""} == {
        "ram_mb": 524,
        "rtfx": 20.0,
        "measured_at": "",
        "threads": 2,
        "build": __version__,
    }
    assert saved_rtfx(out, "a-model", "r1", 2) == 20.0


def test_too_few_models_exit_2_with_explanation(
    bench: dict[str, Any],
    fake_probe: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = ModelStore(tmp_path / "store")
    install(store, "a-model")
    install(store, "b-model")

    facts = run(bench, ["--store", str(store.root), "--minimum-models", "3"], capsys)

    assert facts["exit_code"] == 2
    assert facts["measured"] == 2
    assert "Недостаточно моделей: замерено 2, нужно не меньше 3" in facts["explanation"]
    # Без --out — временный файл, не файл замеров программы.
    written = Path(facts["out"])
    try:
        assert written.is_file()
        assert written != Path(os.environ["XDG_DATA_HOME"]) / "astra-voice/measurements.json"
        assert set(read_measurements(written)) == {"a-model@r1", "b-model@r1"}
    finally:
        written.unlink()


def test_failed_and_broken_models(
    bench: dict[str, Any],
    fake_probe: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = ModelStore(tmp_path / "store")
    install(store, "a-model")
    install(store, "b-load-fails")
    install(store, "c-empty")
    install(store, "d-broken", broken=True)
    out = tmp_path / "m.json"

    facts = run(bench, ["--store", str(store.root), "--out", str(out)], capsys)

    statuses = {row["model"]: row["status"] for row in facts["rows"]}
    assert statuses == {
        "a-model@r1": "замерена",
        "b-load-fails@r1": "ошибка: воркер сообщил ошибку engine-failed",
        "c-empty@r1": "ошибка: пустой текст",
        "d-broken@r1": "повреждена",
    }
    assert "d-broken" not in fake_probe
    assert facts["exit_code"] == 1
    assert set(read_measurements(out)) == {"a-model@r1"}


def test_refuses_app_measurements_file(
    bench: dict[str, Any],
    fake_probe: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = ModelStore(tmp_path / "store")
    install(store, "a-model")
    app_file = Path(os.environ["XDG_DATA_HOME"]) / "astra-voice" / "measurements.json"

    facts = run(bench, ["--store", str(store.root), "--out", str(app_file)], capsys)

    assert facts["exit_code"] == 2
    assert facts["rows"] == []
    assert fake_probe == []
    assert not app_file.exists()


def test_plain_report(
    bench: dict[str, Any],
    fake_probe: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = ModelStore(tmp_path / "store")
    install(store, "a-model")
    main = cast(Callable[[list[str]], int], bench["main"])

    code = main(
        [
            "--catalog",
            "--store",
            str(store.root),
            "--out",
            str(tmp_path / "m.json"),
            "--minimum-models",
            "3",
        ]
    )

    output = capsys.readouterr().out
    assert code == 2
    assert "a-model@r1  замерена  20×   500      524      410" in output
    assert "Итог: прогон невозможен или мало моделей; код 2." in output


def test_probe_subprocess_reports_only_error_code(bench: dict[str, Any], tmp_path: Path) -> None:
    # Настоящий дочерний процесс и движок: заглушка весов не загружается.
    store = ModelStore(tmp_path / "store")
    install(store, "a-model")
    record = store.records()[0]

    with pytest.raises(bench["BenchmarkError"]) as failure:
        bench["probe_process"](record, min_ram_mb=768, wav=WAV, runs=5, threads=2, timeout=120)

    # Без onnx-asr в этом Python — понятная причина, с ним — код ошибки загрузки.
    assert str(failure.value) in {bench["ENGINE_MISSING"], "воркер сообщил ошибку engine-failed"}


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ('{"ok": false, "error": "/home/user/секрет"}', "сбой движка"),
        ('{"ok": false, "error": "воркер сообщил ошибку ../x y"}', "сбой движка"),
        ('{"ok": true, "rtfx": "20"}', "неверный ответ процесса"),
        ("не JSON", "неверный ответ процесса"),
    ],
)
def test_probe_output_is_untrusted(
    bench: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    reason: str,
) -> None:
    def fake_run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout + "\n", "")

    monkeypatch.setattr(bench["subprocess"], "run", fake_run)
    record = ModelRecord("a-model", "r1", tmp_path, "onnx-asr-gigaam-v3", "v", 4)

    with pytest.raises(bench["BenchmarkError"]) as failure:
        bench["probe_process"](record, min_ram_mb=768, wav=WAV, runs=5, threads=2, timeout=1)

    assert str(failure.value) == reason


@pytest.mark.parametrize("runs", ["4", "0"])
def test_runs_below_five_rejected(bench: dict[str, Any], runs: str) -> None:
    main = cast(Callable[[list[str]], int], bench["main"])
    with pytest.raises(SystemExit) as exit_info:
        main(["--catalog", "--runs", runs])
    assert exit_info.value.code == 2
