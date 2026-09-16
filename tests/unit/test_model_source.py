"""Приоритет источников модели и совместимость запросов с воркером."""

from __future__ import annotations

import hashlib
import json
import logging
import wave
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import FrozenInstanceError, asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.core import model_source, paths
from astra_voice.core.model_request import ModelNotConfigured
from astra_voice.core.model_source import (
    ModelRecord,
    SmokeResult,
    SmokeRunner,
    resolve_model_request,
    smoke_wav_path,
)
from astra_voice.core.settings import Settings, from_dict
from astra_voice.models.store import ModelStore
from astra_voice.worker import ipc

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class FakeRecord(ModelRecord):
    id: str = "stored-model"
    revision: str = "stored-revision"
    dir: Path = Path("~/selected/model/r2")
    layout: str = "stored-layout"
    variant: str = "stored-variant"
    size_bytes: int = 0


@dataclass
class FakeStore(ModelStore):
    record: ModelRecord | None = None
    error: Exception | None = None
    calls: int = 0

    def current(self) -> ModelRecord | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.record


@pytest.fixture
def settings() -> Settings:
    return from_dict(
        {
            "model_id": "saved-model",
            "model_revision": "r1",
            "model_layout": "saved-layout",
            "model_variant": "saved-variant",
            "model_threads": 4,
            "model_min_ram_mb": 1024,
            "threads": 8,
            "min_ram_mb": 2048,
        }
    )


def assert_request(request: dict[str, Any] | None, expected: dict[str, Any]) -> None:
    assert request == expected
    assert request is not None
    ipc.encode(request)


def test_default_model_store_uses_shared_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert paths.model_store_dir() == tmp_path / "astra-voice" / "models"
    assert ModelStore().root == paths.model_store_dir()


@pytest.mark.parametrize("configured", [False, True])
def test_real_store_resolves_committed_revision(
    settings: Settings, tmp_path: Path, configured: bool
) -> None:
    store = ModelStore(root=tmp_path)
    model_id, revision = "installed-model", "r2"
    staging = store.staging_dir(model_id, revision)
    (staging / "model.onnx").write_bytes(b"model data")
    (staging / "tokens.txt").write_text("token\n", encoding="utf-8")
    (staging / "state.json").write_text(
        json.dumps({"layout": "installed-layout", "variant": "int8", "size_bytes": 16}),
        encoding="utf-8",
    )
    directory = store.commit(model_id, revision)
    store.set_current(model_id, revision)
    if not configured:
        settings = Settings()

    request = resolve_model_request(settings, store, store_dir=tmp_path / "unused")

    assert directory == tmp_path / model_id / revision
    assert directory.is_dir()
    assert (directory / "model.onnx").read_bytes() == b"model data"
    assert (directory / "tokens.txt").read_text(encoding="utf-8") == "token\n"
    assert_request(
        request,
        {
            "type": "model.load",
            "id": model_id,
            "revision": revision,
            "dir": str(directory),
            "layout": "installed-layout",
            "variant": "int8",
            "threads": 4 if configured else 2,
            "min_ram_mb": 1024 if configured else 768,
        },
    )


def test_explicit_directory_overrides_store_and_settings(settings: Settings) -> None:
    settings = from_dict({**settings.to_dict(), "model_dir": "~/explicit/model/r3"})
    store = FakeStore(FakeRecord(), error=AssertionError("Хранилище не нужно"))

    request = resolve_model_request(settings, store, store_dir=Path("/store"))

    assert store.calls == 0
    assert_request(
        request,
        {
            "type": "model.load",
            "id": "saved-model",
            "revision": "r1",
            "dir": str(Path("~/explicit/model/r3").expanduser()),
            "layout": "saved-layout",
            "variant": "saved-variant",
            "threads": 4,
            "min_ram_mb": 1024,
        },
    )


def test_explicit_directory_preserves_configured_metadata() -> None:
    settings = from_dict(
        {
            "model_dir": "/explicit/path-model/path-revision",
            "model_id": "configured-model",
            "model_revision": "configured-revision",
        }
    )

    request = resolve_model_request(settings, store_dir=Path("/store"))

    assert request is not None
    assert request["id"] == "configured-model"
    assert request["revision"] == "configured-revision"
    assert request["dir"] == "/explicit/path-model/path-revision"
    ipc.encode(request)


def test_explicit_directory_alone_derives_metadata_from_path() -> None:
    settings = from_dict({"model_dir": "/explicit/path-model/path-revision"})

    request = resolve_model_request(settings, store_dir=Path("/store"))

    assert request is not None
    assert request["id"] == "path-model"
    assert request["revision"] == "path-revision"
    assert request["dir"] == "/explicit/path-model/path-revision"
    ipc.encode(request)


@pytest.mark.parametrize("model_dir", [None, ""])
@pytest.mark.parametrize("configured", [False, True])
def test_store_record_supplies_model(
    settings: Settings, model_dir: str | None, configured: bool
) -> None:
    data = settings.to_dict() if configured else {"threads": 3, "min_ram_mb": 512}
    settings = from_dict({**data, "model_dir": model_dir})
    before = settings.to_dict()
    record = FakeRecord()
    store = FakeStore(record)

    request = resolve_model_request(settings, store, store_dir=Path("/unused"))

    assert store.calls == 1
    assert settings.to_dict() == before
    assert_request(
        request,
        {
            "type": "model.load",
            "id": record.id,
            "revision": record.revision,
            "dir": str(Path(record.dir).expanduser()),
            "layout": record.layout,
            "variant": record.variant,
            "threads": 4 if configured else 3,
            "min_ram_mb": 1024 if configured else 512,
        },
    )


@pytest.mark.parametrize("source", ["absent", "broken", "empty", "error"])
@pytest.mark.parametrize("configured", [False, True])
def test_settings_fallback(
    settings: Settings, caplog: pytest.LogCaptureFixture, source: str, configured: bool
) -> None:
    store = None
    if source == "broken":
        store = FakeStore(FakeRecord(state="broken"))
    elif source == "empty":
        store = FakeStore()
    elif source == "error":
        store = FakeStore(error=RuntimeError("Секретные сведения"))
    if not configured:
        settings = from_dict({})

    with caplog.at_level(logging.WARNING, logger=model_source.__name__):
        request = resolve_model_request(settings, store, store_dir=Path("/custom/store"))

    if configured:
        assert_request(
            request,
            {
                "type": "model.load",
                "id": "saved-model",
                "revision": "r1",
                "dir": "/custom/store/saved-model/r1",
                "layout": "saved-layout",
                "variant": "saved-variant",
                "threads": 4,
                "min_ram_mb": 1024,
            },
        )
    else:
        assert request is None
    if store is not None:
        assert store.calls == 1
    if source == "error":
        assert [(entry.levelno, entry.getMessage()) for entry in caplog.records] == [
            (logging.WARNING, "Не удалось получить выбранную модель из хранилища")
        ]
        assert caplog.records[0].exc_info is None
        assert "Секретные сведения" not in caplog.text
    else:
        assert not caplog.records


@pytest.mark.parametrize("source", ["override", "store", "settings"])
@pytest.mark.parametrize("error_type", [ValueError, TypeError])
def test_build_errors_propagate_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    source: str,
    error_type: type[Exception],
) -> None:
    if source == "override":
        settings = from_dict({**settings.to_dict(), "model_dir": "/explicit/model/r3"})
    store = FakeStore(FakeRecord()) if source == "store" else None
    error = error_type("Ошибка построения")
    monkeypatch.setattr(model_source, "build_model_load", Mock(side_effect=error))

    with pytest.raises(error_type) as caught:
        resolve_model_request(settings, store, store_dir=Path("/store"))

    assert caught.value is error


def test_incomplete_ok_record_does_not_fall_back(settings: Settings) -> None:
    store = FakeStore(FakeRecord(revision=""))

    with pytest.raises(ModelNotConfigured):
        resolve_model_request(settings, store, store_dir=Path("/store"))


class FakeSmokeSupervisor:
    """Выдаёт очередь нужного этапа только при прокачке, без процесса и Qt."""

    def __init__(self) -> None:
        self.responses: dict[str, list[dict[str, Any]]] = {
            "start": [{"type": "hello"}],
            "model.load": [{"type": "model.loaded"}],
            "transcribe.file": [{"type": "result", "text": "Проверка связи"}],
        }
        self.events: deque[dict[str, Any]] = deque()
        self.on_event: Callable[[dict[str, Any]], None] = lambda event: None
        self.sent: list[tuple[dict[str, Any], float]] = []
        self.created = 0
        self.started = 0
        self.stopped = 0
        self.now = 0.0
        self.stage = ""
        self.fail_at = ""
        self.error: Exception = RuntimeError("Сбой воркера")

    def factory(
        self, *, on_event: Callable[[dict[str, Any]], None], use_qt: bool
    ) -> FakeSmokeSupervisor:
        assert use_qt is False
        self.created += 1
        self.on_event = on_event
        self.maybe_fail("factory")
        return self

    def maybe_fail(self, operation: str) -> None:
        if self.fail_at == operation:
            raise self.error

    def start(self) -> None:
        self.started += 1
        self.maybe_fail("start")
        self.stage = "start"
        self.events.extend(self.responses[self.stage])

    def send(self, command: dict[str, Any], *, timeout: float) -> None:
        self.maybe_fail(command["type"])
        self.sent.append((dict(command), timeout))
        self.stage = command["type"]
        self.events.extend(self.responses[self.stage])

    def pump(self, timeout: float) -> None:
        assert 0 < timeout <= 0.05
        self.maybe_fail("pump")
        self.now += timeout
        if self.events:
            self.on_event(self.events.popleft())

    def stop(self) -> None:
        self.stopped += 1
        self.maybe_fail("stop")


@pytest.fixture
def smoke_supervisor() -> FakeSmokeSupervisor:
    return FakeSmokeSupervisor()


@pytest.fixture
def smoke_runner(tmp_path: Path, smoke_supervisor: FakeSmokeSupervisor) -> SmokeRunner:
    wav = tmp_path / "smoke.wav"
    wav.touch()
    return SmokeRunner(
        supervisor_factory=smoke_supervisor.factory,
        wav_path=wav,
        hello_timeout_s=0.1,
        load_timeout_s=0.2,
        transcribe_timeout_s=0.3,
        clock=lambda: smoke_supervisor.now,
    )


def test_smoke_success(smoke_runner: SmokeRunner, smoke_supervisor: FakeSmokeSupervisor) -> None:
    request = MappingProxyType({"type": "model.load", "dir": "/model/r1"})
    smoke_supervisor.responses["start"].insert(0, {"type": "progress"})

    result = smoke_runner(request)

    assert result == SmokeResult(True, "ok")
    assert smoke_supervisor.created == smoke_supervisor.started == smoke_supervisor.stopped == 1
    assert smoke_supervisor.sent == [
        (dict(request), 0.2),
        ({"type": "transcribe.file", "path": str(smoke_runner._wav_path)}, 0.3),
    ]
    assert dict(request) == {"type": "model.load", "dir": "/model/r1"}
    with pytest.raises(FrozenInstanceError):
        result.reason = "другая причина"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("text", "reason"),
    [("погода хорошая", "no-match"), ("", "no-match"), (None, "transcribe-failed")],
)
def test_smoke_without_match(
    smoke_runner: SmokeRunner, smoke_supervisor: FakeSmokeSupervisor, text: Any, reason: str
) -> None:
    smoke_supervisor.responses["transcribe.file"] = [{"type": "result", "text": text}]

    assert smoke_runner.run({"type": "model.load"}) == SmokeResult(False, reason)
    assert smoke_supervisor.stopped == 1


@pytest.mark.parametrize("text", ["STRASSE", "Straße"])
def test_smoke_casefold(tmp_path: Path, smoke_supervisor: FakeSmokeSupervisor, text: str) -> None:
    wav = tmp_path / "smoke.wav"
    wav.touch()
    smoke_supervisor.responses["transcribe.file"] = [{"type": "result", "text": text}]
    runner = SmokeRunner(
        supervisor_factory=smoke_supervisor.factory, wav_path=wav, expect_any=("нет", "Straße")
    )

    assert runner({"type": "model.load"}) == SmokeResult(True, "ok")
    assert smoke_supervisor.stopped == 1


@pytest.mark.parametrize(
    ("stage", "code", "reason"),
    [
        ("start", "worker-start", "worker-failed"),
        ("model.load", "bad-model", "load-failed"),
        ("transcribe.file", "recognition", "transcribe-failed"),
        ("model.load", "load-timeout", "timeout"),
        ("transcribe.file", "timeout", "timeout"),
        ("model.load", "worker-crashed", "worker-failed"),
        ("transcribe.file", "restart-limit", "worker-failed"),
    ],
)
def test_smoke_error_events(
    smoke_runner: SmokeRunner,
    smoke_supervisor: FakeSmokeSupervisor,
    stage: str,
    code: str,
    reason: str,
) -> None:
    smoke_supervisor.responses[stage] = [{"type": "error", "code": code}]

    assert smoke_runner({"type": "model.load"}) == SmokeResult(False, reason)
    assert smoke_supervisor.stopped == 1


@pytest.mark.parametrize("stage", ["start", "model.load", "transcribe.file"])
def test_smoke_timeout(
    smoke_runner: SmokeRunner, smoke_supervisor: FakeSmokeSupervisor, stage: str
) -> None:
    smoke_supervisor.responses[stage] = []

    assert smoke_runner({"type": "model.load"}) == SmokeResult(False, "timeout")
    assert smoke_supervisor.stopped == 1
    assert smoke_supervisor.now == pytest.approx(
        {"start": 0.1, "model.load": 0.25, "transcribe.file": 0.4}[stage]
    )


@pytest.mark.parametrize("stage", ["", "start", "model.load", "transcribe.file"])
def test_smoke_cancel(
    smoke_runner: SmokeRunner, smoke_supervisor: FakeSmokeSupervisor, stage: str
) -> None:
    def cancel() -> bool:
        # Отмена после pump должна иметь приоритет даже перед готовым ответом.
        return smoke_supervisor.stage == stage and not smoke_supervisor.events

    assert smoke_runner({"type": "model.load"}, cancel=cancel) == SmokeResult(False, "cancelled")
    assert smoke_supervisor.stopped == 1


def test_smoke_cancel_event(
    smoke_runner: SmokeRunner, smoke_supervisor: FakeSmokeSupervisor
) -> None:
    smoke_supervisor.responses["transcribe.file"] = [{"type": "cancelled"}]

    assert smoke_runner({"type": "model.load"}) == SmokeResult(False, "cancelled")
    assert smoke_supervisor.stopped == 1


@pytest.mark.parametrize(
    "operation", ["factory", "start", "model.load", "transcribe.file", "pump", "stop"]
)
@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
def test_smoke_worker_failure(
    smoke_runner: SmokeRunner,
    smoke_supervisor: FakeSmokeSupervisor,
    operation: str,
    error_type: type[Exception],
) -> None:
    smoke_supervisor.fail_at = operation
    smoke_supervisor.error = error_type("Сбой воркера")

    assert smoke_runner({"type": "model.load"}) == SmokeResult(False, "worker-failed")
    assert smoke_supervisor.stopped == (0 if operation == "factory" else 1)


def test_smoke_missing_wav(tmp_path: Path, smoke_supervisor: FakeSmokeSupervisor) -> None:
    runner = SmokeRunner(
        supervisor_factory=smoke_supervisor.factory, wav_path=tmp_path / "missing.wav"
    )

    assert runner({"type": "model.load"}) == SmokeResult(False, "no-wav")
    assert smoke_supervisor.created == smoke_supervisor.started == smoke_supervisor.stopped == 0


@pytest.mark.parametrize("outcome", ["ok", "no-match", "error", "exception"])
def test_smoke_keeps_text_private(
    smoke_runner: SmokeRunner,
    smoke_supervisor: FakeSmokeSupervisor,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
) -> None:
    secret = "СВЕРХСЕКРЕТМАРКЕР ЯНТАРНОЕОБЛАКО"
    text = secret + (" Проверка связи" if outcome == "ok" else "")
    event = {"type": "result", "text": text}
    if outcome == "error":
        event = {"type": "error", "code": secret, "message": text}
    elif outcome == "exception":
        smoke_supervisor.fail_at = "pump"
        smoke_supervisor.error = RuntimeError(text)
    smoke_supervisor.responses["transcribe.file"] = [event]

    with caplog.at_level(logging.DEBUG):
        result = smoke_runner({"type": "model.load"})

    assert caplog.records
    for word in [text, *text.split()]:
        assert word.casefold() not in caplog.text.casefold()
        assert word.casefold() not in repr(result).casefold()
        assert word.casefold() not in repr(asdict(result)).casefold()
        assert word.casefold() not in repr(smoke_runner).casefold()
    assert all(record.exc_info is None for record in caplog.records)
    assert set(asdict(result)) == {"ok", "reason"}
    assert smoke_supervisor.stopped == 1


def test_smoke_bundled_wav() -> None:
    wav = Path(__file__).resolve().parents[2] / "data" / "smoke" / "smoke-ru.wav"

    assert wav.is_file()
    assert wav.stat().st_size == 51244
    assert hashlib.sha256(wav.read_bytes()).hexdigest() == (
        "25b2afe1fe248a7b6e7238932e10cd7de97d431ee63415de2cbb2031617880bd"
    )
    with wave.open(str(wav), "rb") as recording:
        assert recording.getframerate() == 16000
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2
        assert recording.getcomptype() == "NONE"
        assert recording.getnframes() == 25600
        assert recording.getnframes() / recording.getframerate() == 1.6
        with wave.open(str(wav.parents[1] / "test" / "test-ru-6s.wav"), "rb") as source:
            assert recording.readframes(25600) == source.readframes(25600)
    assert model_source.SMOKE_WAV_NAME == wav.name
    assert not wav.with_name("smoke-ru-6s.wav").exists()


def test_smoke_resource_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, smoke_supervisor: FakeSmokeSupervisor
) -> None:
    monkeypatch.setenv("ASTRA_VOICE_RESOURCES", str(tmp_path))
    wav = tmp_path / "data" / "smoke" / model_source.SMOKE_WAV_NAME
    wav.parent.mkdir(parents=True)
    wav.touch()

    assert smoke_wav_path() == wav
    runner = SmokeRunner(supervisor_factory=smoke_supervisor.factory)
    assert runner({"type": "model.load"}).ok
    assert smoke_supervisor.sent[-1] == ({"type": "transcribe.file", "path": str(wav)}, 30.0)
    assert smoke_supervisor.stopped == 1


@pytest.mark.parametrize(
    ("text", "matched"),
    [
        ("ПРОВЕРКА микрофона", True),
        ("Качество СвЯзИ", True),
        ("", False),
        ("   ", False),
        ("Другие слова", False),
    ],
)
def test_smoke_matches_defaults(text: str, matched: bool) -> None:
    assert model_source.smoke_matches(text) is matched


@pytest.mark.parametrize(
    ("text", "expected", "matched"),
    [
        ("STRASSE", ("нет", "Straße"), True),
        ("Straße", ["STRASSE"], True),
        ("проверка", (), False),
        ("проверка", ("связи",), False),
    ],
)
def test_smoke_matches_custom_words(text: str, expected: Sequence[str], matched: bool) -> None:
    assert model_source.smoke_matches(text, expected) is matched
