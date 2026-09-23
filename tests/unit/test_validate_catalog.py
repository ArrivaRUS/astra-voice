"""CLI каталога без настоящих весов, воркера и пользовательского хранилища."""

from __future__ import annotations

import json
import os
import runpy
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from astra_voice.models.catalog import CatalogEntry
from astra_voice.models.store import ModelStore

pytestmark = pytest.mark.unit


@pytest.fixture
def validate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("QT_QPA_PLATFORM", os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    monkeypatch.setattr(sys, "path", sys.path.copy())
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / "tools/validate"))
    return cast(dict[str, Any], namespace["main"].__globals__)


def entry(model_id: str) -> CatalogEntry:
    return CatalogEntry(
        model_id,
        "r1",
        model_id,
        "",
        1,
        768,
        "onnx-asr-gigaam-v3",
        "gigaam-v3-e2e-rnnt",
        False,
        "huggingface.co",
        (),
    )


def install(store: ModelStore, model_id: str) -> None:
    staging = store.staging_dir(model_id, "r1")
    (staging / "weights.onnx").write_bytes(b"stub")
    store.commit(model_id, "r1")


def prepare(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    models: tuple[CatalogEntry, ...],
) -> ModelStore:
    monkeypatch.setitem(validate, "load_catalog", lambda: SimpleNamespace(entries=models))
    wav = tmp_path / "smoke.wav"
    wav.write_bytes(b"stub")
    monkeypatch.setitem(validate, "smoke_wav_path", lambda: wav)
    return ModelStore(tmp_path / "store")


def test_arguments(validate: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    main = validate["main"]
    for arguments in (["catalog"], ["catalog", "--all", "--switch"]):
        with pytest.raises(SystemExit) as error:
            main(arguments)
        assert error.value.code == 2
    with pytest.raises(SystemExit) as help_exit:
        main(["catalog", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "--all" in help_text and "--switch" in help_text and "--json" in help_text
    assert "для --switch игнорируется" in help_text
    for arguments in (
        ["catalog", "--all", "--from", "a@r1"],
        ["catalog", "--all", "--to", "b@r1"],
        ["catalog", "--all", "--timeout", "0"],
        ["catalog", "--all", "--timeout", "nan"],
    ):
        with pytest.raises(SystemExit, match="2"):
            main(arguments)


def test_format_catalog_table(validate: dict[str, Any]) -> None:
    rows = [
        {
            "model": "a@r1",
            "layout": "onnx",
            "load_ms": 10,
            "infer_ms": 250,
            "rtfx": 6.4,
            "text": True,
            "match": True,
            "status": "ок",
        },
        {
            "model": "b@r1",
            "layout": "onnx",
            "load_ms": None,
            "infer_ms": None,
            "rtfx": None,
            "text": False,
            "match": None,
            "status": "не установлена",
        },
    ]
    rendered = validate["format_catalog_table"](rows)
    for heading in (
        "модель",
        "раскладка",
        "загрузка, мс",
        "распознавание, мс",
        "RTFx",
        "текст",
        "эталон",
        "итог",
    ):
        assert heading in rendered
    assert "6.4" in rendered and "да" in rendered and "нет" in rendered


def test_uninstalled_is_skipped(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = prepare(validate, monkeypatch, tmp_path, (entry("a"), entry("b")))
    install(store, "a")
    monkeypatch.setitem(
        validate,
        "run_catalog_probe",
        lambda *args: {
            "ok": True,
            "load_ms": 10,
            "infer_ms": 250,
            "audio_s": 1.6,
            "text_len": 5,
            "match": True,
            "transcript": "secret utterance",
        },
    )
    assert validate["main"](["catalog", "--all", "--store", str(store.root), "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["rows"][1]["status"] == "не установлена"
    assert "secret utterance" not in output and "transcript" not in output
    assert payload["rows"][0]["rtfx"] == 6.4
    assert payload["rows"][0]["match"] is True


@pytest.mark.parametrize(
    ("probe", "status"),
    [
        ({"ok": False, "error": "таймаут"}, "таймаут"),
        ({"ok": False, "error": "неверный ответ процесса"}, "ошибка"),
    ],
)
def test_probe_failure(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    probe: dict[str, Any],
    status: str,
) -> None:
    store = prepare(validate, monkeypatch, tmp_path, (entry("a"),))
    install(store, "a")
    monkeypatch.setitem(validate, "run_catalog_probe", lambda *args: probe)
    assert validate["main"](["catalog", "--all", "--store", str(store.root), "--json"]) == 1
    assert status in json.loads(capsys.readouterr().out)["rows"][0]["status"]


def test_no_installed(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = prepare(validate, monkeypatch, tmp_path, (entry("a"),))
    monkeypatch.setitem(validate, "run_catalog_probe", lambda *args: pytest.fail("probe called"))
    assert validate["main"](["catalog", "--all", "--store", str(store.root)]) == 2
    assert "не установлена" in capsys.readouterr().out


def test_missing_wav_skips_installed(
    validate: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = prepare(validate, monkeypatch, tmp_path, (entry("a"),))
    install(store, "a")
    monkeypatch.setitem(validate, "smoke_wav_path", lambda: tmp_path / "absent.wav")
    code, facts = validate["catalog_all"](SimpleNamespace(timeout=1), (entry("a"),), store)
    assert code == 2
    assert facts["rows"][0]["status"] == "пропущена"


def test_runner_exception_does_not_stop_next_model(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = prepare(validate, monkeypatch, tmp_path, (entry("a"), entry("b")))
    install(store, "a")
    install(store, "b")
    called: list[str] = []

    def probe(directory: Path, *args: object) -> dict[str, Any]:
        called.append(directory.parent.name)
        if directory.parent.name == "a":
            raise TimeoutError
        return {"ok": True, "load_ms": 10, "infer_ms": 250, "audio_s": 1.6, "text_len": 5}

    monkeypatch.setitem(validate, "run_catalog_probe", probe)
    assert validate["main"](["catalog", "--all", "--store", str(store.root), "--json"]) == 1
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert called == ["a", "b"]
    assert [row["status"] for row in rows] == ["таймаут", "ок"]


def test_switch_pair_and_too_few(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = (entry("a"), entry("b"), entry("c"))
    store = prepare(validate, monkeypatch, tmp_path, entries)
    install(store, "a")
    store.set_current("a", "r1")
    assert validate["main"](["catalog", "--switch", "--store", str(store.root)]) == 2
    assert "Нужно минимум 2 установленные модели" in capsys.readouterr().out
    install(store, "b")
    selected = validate["choose_catalog_pair"](
        entries,
        store.records(),
        store.current(),
        None,
        None,
    )
    assert selected is not None
    assert (selected[0].id, selected[2].id) == ("a", "b")
    assert (
        validate["choose_catalog_pair"](
            entries,
            store.records(),
            store.current(),
            ("a", "r1"),
            ("a", "r1"),
        )
        is None
    )
    assert validate["catalog_switch_exit_code"]("ok", True, True) == 0
    assert validate["catalog_switch_exit_code"]("ok", False, True) == 1


def test_runtime_stubs_construct_without_worker(
    validate: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PyQt5.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    store = ModelStore(tmp_path / "store")

    class FakeSupervisor:
        generation = 1
        state = "new"
        process = None

        def __init__(self, **kwargs: object) -> None:
            pass

        def stop(self) -> None:
            self.state = "stopped"

    monkeypatch.setattr("astra_voice.runtime.notify", validate["CatalogNotify"]())
    runtime = validate["make_catalog_runtime"](
        store, supervisor_factory=lambda **kwargs: FakeSupervisor(**kwargs)
    )
    assert isinstance(runtime.supervisor, FakeSupervisor)
    runtime.shutdown()
    assert runtime.supervisor.state == "stopped"


@pytest.mark.parametrize(("result", "expected"), [("ok", 0), ("failed", 1)])
@pytest.mark.parametrize("original", ["c", None])
def test_switch_uses_runtime_callback_and_restores_original_current(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result: str,
    expected: int,
    original: str | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = (entry("a"), entry("b"), entry("c"))
    store = prepare(validate, monkeypatch, tmp_path, entries)
    for model in ("a", "b", "c"):
        install(store, model)
    if original is not None:
        store.set_current(original, "r1")
    before = (store.root / "current.json").read_bytes() if original is not None else None
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.stopped = False

        def poll(self) -> int | None:
            return 0 if self.stopped else None

    class FakeRuntime:
        def __init__(self) -> None:
            self._selfcheck = "ok"
            self._loaded_model = {"model_id": "a", "revision": "r1"}
            self.old = FakeProcess(101)
            self.supervisor = SimpleNamespace(process=self.old)
            self.on_switch_finished: Any = None
            self.closed = False

        def start(self) -> None:
            assert all(signal.getsignal(sig) != handler for sig, handler in handlers.items())
            current = store.current()
            assert current is not None and current.id == "a"

        def can_switch_without_pause(self, min_ram_mb: int) -> bool:
            return True

        def switch_model(self, *, min_ram_mb: int, pause: bool) -> None:
            assert min_ram_mb == entries[1].min_ram_mb
            assert pause is False
            current = store.current()
            assert current is not None and current.id == "b"
            self.old.stopped = True
            self.supervisor = SimpleNamespace(process=FakeProcess(102))
            self._loaded_model = {"model_id": "b", "revision": "r1"}
            self.on_switch_finished(result)

        def shutdown(self) -> None:
            self.supervisor.process.stopped = True
            self.closed = True

    runtime = FakeRuntime()
    monkeypatch.setitem(validate, "make_catalog_runtime", lambda store: runtime)
    args = SimpleNamespace(from_ref=("a", "r1"), to_ref=("b", "r1"))
    code, facts = validate["catalog_switch"](args, entries, store)
    assert code == expected
    assert facts["result"] == result
    assert facts["old_stopped"] is True
    assert facts["current_is_b"] is True
    assert facts["loaded_is_b"] is True
    assert facts["new_process"] is True
    assert facts["new_pid"] == 102
    assert facts["workers_stopped"] is True
    assert runtime.closed
    current_path = store.root / "current.json"
    if before is None:
        assert not current_path.exists()
    else:
        assert current_path.read_bytes() == before
    assert all(signal.getsignal(sig) == handler for sig, handler in handlers.items())
    captured = capsys.readouterr()
    assert "На время проверки меняю выбранную модель" in captured.err
    assert "На время проверки меняю выбранную модель" not in captured.out


def test_switch_start_exception_is_safe_and_restores_selection(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = (entry("a"), entry("b"), entry("c"))
    store = prepare(validate, monkeypatch, tmp_path, entries)
    for model in ("a", "b", "c"):
        install(store, model)
    store.set_current("c", "r1")
    before = (store.root / "current.json").read_bytes()

    class FailingRuntime:
        supervisor = SimpleNamespace(process=None)

        def start(self) -> None:
            raise PermissionError(
                13, "Permission denied", "/home/astra/.local/share/astra-voice/models/x"
            )

        def shutdown(self) -> None:
            pass

    monkeypatch.setitem(validate, "make_catalog_runtime", lambda store: FailingRuntime())
    for output_format in ([], ["--json"]):
        code = validate["main"](
            [
                "catalog",
                "--switch",
                "--store",
                str(store.root),
                "--from",
                "a@r1",
                "--to",
                "b@r1",
                *output_format,
            ]
        )
        assert code == 2
        output = capsys.readouterr().out
        assert "PermissionError" in output
        assert "/home" not in output and "Permission denied" not in output
        assert (store.root / "current.json").read_bytes() == before


def test_switch_timeout_is_failure_and_restores_selection(
    validate: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entries = (entry("a"), entry("b"), entry("c"))
    store = prepare(validate, monkeypatch, tmp_path, entries)
    for model in ("a", "b", "c"):
        install(store, model)
    store.set_current("c", "r1")
    current_path = store.root / "current.json"
    before = current_path.read_bytes()

    class FakeProcess:
        pid = 101
        stopped = False

        def poll(self) -> int | None:
            return 0 if self.stopped else None

    class FakeRuntime:
        _selfcheck = "ok"
        _loaded_model = {"model_id": "a", "revision": "r1"}
        on_switch_finished: Any = None

        def __init__(self) -> None:
            self.supervisor = SimpleNamespace(process=FakeProcess())
            self.switch_called = False

        def start(self) -> None:
            pass

        def can_switch_without_pause(self, min_ram_mb: int) -> bool:
            return True

        def switch_model(self, *, min_ram_mb: int, pause: bool) -> None:
            self.switch_called = True

        def shutdown(self) -> None:
            self.supervisor.process.stopped = True

    runtime = FakeRuntime()
    monkeypatch.setitem(validate, "make_catalog_runtime", lambda store: runtime)
    monkeypatch.setitem(validate, "CATALOG_SWITCH_TIMEOUT_S", 0.01)
    code, facts = validate["catalog_switch"](
        SimpleNamespace(from_ref=("a", "r1"), to_ref=("b", "r1")), entries, store
    )
    assert runtime.switch_called
    assert code == facts["exit_code"] == 1
    assert facts["result"] == "failed"
    assert facts["explanation"] == "Таймаут переключения модели"
    assert current_path.read_bytes() == before


@pytest.mark.parametrize("via_handler", [False, True], ids=["direct", "sigterm-handler"])
def test_switch_start_interrupt_restores_selection_and_handlers(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    via_handler: bool,
) -> None:
    entries = (entry("a"), entry("b"), entry("c"))
    store = prepare(validate, monkeypatch, tmp_path, entries)
    for model in ("a", "b", "c"):
        install(store, model)
    store.set_current("c", "r1")
    current_path = store.root / "current.json"
    before = current_path.read_bytes()
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}

    class FakeRuntime:
        supervisor = SimpleNamespace(process=None)

        def start(self) -> None:
            assert all(signal.getsignal(sig) != handler for sig, handler in handlers.items())
            if via_handler:
                cast(Any, signal.getsignal(signal.SIGTERM))(signal.SIGTERM, None)
            raise KeyboardInterrupt

        def shutdown(self) -> None:
            pass

    monkeypatch.setitem(validate, "make_catalog_runtime", lambda store: FakeRuntime())
    code, facts = validate["catalog_switch"](
        SimpleNamespace(from_ref=("a", "r1"), to_ref=("b", "r1")), entries, store
    )
    assert code == facts["exit_code"] == 2
    assert facts["explanation"] == "KeyboardInterrupt"
    assert current_path.read_bytes() == before
    assert all(signal.getsignal(sig) == handler for sig, handler in handlers.items())


def test_loaded_model_controls_switch_result(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    entries = (entry("a"), entry("b"))
    store = prepare(validate, monkeypatch, tmp_path, entries)
    for model in ("a", "b"):
        install(store, model)

    class Process:
        pid = 101
        stopped = False

        def poll(self) -> int | None:
            return 0 if self.stopped else None

    class Runtime:
        _selfcheck = "ok"
        _loaded_model = {"model_id": "a", "revision": "r1"}
        on_switch_finished: Any = None

        def __init__(self) -> None:
            self.old = Process()
            self.supervisor = SimpleNamespace(process=self.old)

        def start(self) -> None:
            pass

        def can_switch_without_pause(self, min_ram_mb: int) -> bool:
            return True

        def switch_model(self, *, min_ram_mb: int, pause: bool) -> None:
            self.old.stopped = True
            self.supervisor = SimpleNamespace(process=Process())
            self.supervisor.process.pid = 102
            self.on_switch_finished("ok")

        def shutdown(self) -> None:
            self.supervisor.process.stopped = True

    monkeypatch.setitem(validate, "make_catalog_runtime", lambda store: Runtime())
    code, facts = validate["catalog_switch"](
        SimpleNamespace(from_ref=("a", "r1"), to_ref=("b", "r1")), entries, store
    )
    assert code != 0
    assert facts["result"] == "ok"
    assert facts["current_is_b"] is True
    assert facts["loaded_is_b"] is False


def test_manual_model_setting_rejects_before_store_write(
    validate: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entries = (entry("a"), entry("b"))
    store = prepare(validate, monkeypatch, tmp_path, entries)
    for model in ("a", "b"):
        install(store, model)
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"schema_version": 1, "model_dir": "/tmp/manual"}))
    monkeypatch.setattr(validate["paths"], "settings_path", lambda: settings_path)
    code, facts = validate["catalog_switch"](
        SimpleNamespace(from_ref=("a", "r1"), to_ref=("b", "r1")), entries, store
    )
    assert code == 2
    assert facts["explanation"] == "В настройках задана модель вручную"
    assert not (store.root / "current.json").exists()


@pytest.mark.parametrize("reference", ["current", "from", "to"])
def test_recheck_revision_rejected_without_changing_current(
    validate: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reference: str
) -> None:
    entries = (entry("a"), entry("b"), entry("c"))
    store = prepare(validate, monkeypatch, tmp_path, entries)
    for model in ("a", "b", "c"):
        install(store, model)
    recheck_id = {"current": "c", "from": "a", "to": "b"}[reference]
    state = store.root / recheck_id / "r1.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["recheck"] = True
    state.write_text(json.dumps(data), encoding="utf-8")
    store.set_current("c", "r1")
    before = (store.root / "current.json").read_bytes()
    code, facts = validate["catalog_switch"](
        SimpleNamespace(from_ref=("a", "r1"), to_ref=("b", "r1")), entries, store
    )
    assert code == 2
    assert facts["explanation"] == f"Модель {recheck_id}@r1 нужно перепроверить"
    assert (store.root / "current.json").read_bytes() == before


def test_live_store_lock_refusal(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from PyQt5 import QtCore

    class BusyLock:
        def __init__(self, path: str) -> None:
            pass

        def tryLock(self, timeout: int) -> bool:
            assert timeout == 100
            return False

        def unlock(self) -> None:
            pass

    monkeypatch.setattr(QtCore, "QLockFile", BusyLock)
    monkeypatch.setitem(validate, "load_catalog", lambda: pytest.fail("catalog read"))
    assert validate["main"](["catalog", "--switch", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["explanation"] == (
        "Закройте Astra Voice и повторите проверку"
    )


def test_explicit_live_store_uses_lock_and_other_store_does_not(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from PyQt5 import QtCore

    live_store = tmp_path / "live"
    other_store = tmp_path / "other"
    monkeypatch.setattr(validate["paths"], "model_store_dir", lambda: live_store)
    calls: list[str] = []

    class BusyLock:
        def __init__(self, path: str) -> None:
            calls.append(path)

        def tryLock(self, timeout: int) -> bool:
            assert timeout == validate["paths"].LOCK_TIMEOUT_MS
            return False

    monkeypatch.setattr(QtCore, "QLockFile", BusyLock)
    monkeypatch.setitem(validate, "load_catalog", lambda: SimpleNamespace(entries=()))
    assert validate["main"](["catalog", "--switch", "--store", str(live_store), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["explanation"] == (
        "Закройте Astra Voice и повторите проверку"
    )
    assert len(calls) == 1
    assert validate["main"](["catalog", "--switch", "--store", str(other_store), "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["explanation"] != (
        "Закройте Astra Voice и повторите проверку"
    )
    assert len(calls) == 1


def test_validate_does_not_import_app() -> None:
    source = (Path(__file__).resolve().parents[2] / "tools/validate").read_text(encoding="utf-8")
    assert "astra_voice.app" not in source


@pytest.mark.parametrize("original", ["c", None])
@pytest.mark.parametrize("error_kind", ["oserror", "exception"])
@pytest.mark.parametrize("output_format", [[], ["--json"]], ids=["text", "json"])
def test_restore_failure_is_safe_and_hides_path(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    original: str | None,
    error_kind: str,
    output_format: list[str],
) -> None:
    entries = (entry("a"), entry("b"), entry("c"))
    store = prepare(validate, monkeypatch, tmp_path, entries)
    for model in ("a", "b", "c"):
        install(store, model)
    if original is not None:
        store.set_current(original, "r1")

    class FailingRuntime:
        supervisor = SimpleNamespace(process=None)

        def start(self) -> None:
            raise RuntimeError

        def shutdown(self) -> None:
            pass

    monkeypatch.setitem(validate, "make_catalog_runtime", lambda store: FailingRuntime())
    real_replace = os.replace
    real_unlink = Path.unlink
    secret_path = "/private/secret/model.json"

    def raise_restore_error() -> None:
        if error_kind == "oserror":
            raise PermissionError(13, "denied", secret_path)
        raise RuntimeError(secret_path)

    def fail_restore(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(source).name.startswith(".current.json.restore-"):
            raise_restore_error()
        real_replace(source, target)

    def fail_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == store.root / "current.json":
            raise_restore_error()
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(os, "replace", fail_restore)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    code = validate["main"](
        [
            "catalog",
            "--switch",
            "--store",
            str(store.root),
            "--from",
            "a@r1",
            "--to",
            "b@r1",
            *output_format,
        ]
    )
    assert code == 2
    output = capsys.readouterr().out
    expected = (
        f"Не удалось вернуть прежний выбор модели ({original}@r1) — выберите её в настройках"
        if original is not None
        else "Не удалось вернуть прежний выбор модели (не выбрана) — выберите её в настройках"
    )
    if output_format:
        assert json.loads(output)["explanation"] == expected
    else:
        assert expected in output
    assert secret_path not in output and "denied" not in output


def test_interrupt_during_restore_does_not_escape(
    validate: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entries = (entry("a"), entry("b"))
    store = prepare(validate, monkeypatch, tmp_path, entries)
    for model in ("a", "b"):
        install(store, model)
    store.set_current("a", "r1")
    real_replace = os.replace

    def interrupt_restore(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(source).name.startswith(".current.json.restore-"):
            raise KeyboardInterrupt
        real_replace(source, target)

    class FailingRuntime:
        supervisor = SimpleNamespace(process=None)

        def start(self) -> None:
            raise KeyboardInterrupt

        def shutdown(self) -> None:
            pass

    monkeypatch.setitem(validate, "make_catalog_runtime", lambda store: FailingRuntime())
    monkeypatch.setattr(os, "replace", interrupt_restore)
    code, facts = validate["catalog_switch"](
        SimpleNamespace(from_ref=("a", "r1"), to_ref=("b", "r1")), entries, store
    )
    assert code == facts["exit_code"] == 2
    assert facts["explanation"] == "KeyboardInterrupt"


@pytest.mark.parametrize("failure_mode", ["counter", "idle-request"])
def test_load_a_fails_early_and_restores_current(
    validate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_mode: str,
) -> None:
    entries = (entry("a"), entry("b"), entry("c"))
    store = prepare(validate, monkeypatch, tmp_path, entries)
    for model in ("a", "b", "c"):
        install(store, model)
    store.set_current("c", "r1")
    current_path = store.root / "current.json"
    before = current_path.read_bytes()

    class FailedRuntime:
        _selfcheck = "idle"
        _loaded_model: dict[str, str] = {}
        _loading_model = False
        supervisor = SimpleNamespace(process=None)

        def __init__(self) -> None:
            self._model_load_failures = 0
            self._model_load_request: dict[str, str] | None = None

        def start(self) -> None:
            if failure_mode == "counter":
                self._model_load_failures = 1
            else:
                self._model_load_request = {"id": "a"}

        def switch_model(self, *, min_ram_mb: int, pause: bool) -> None:
            pytest.fail("switch_model called")

        def shutdown(self) -> None:
            pass

    monkeypatch.setitem(validate, "make_catalog_runtime", lambda store: FailedRuntime())
    started = time.monotonic()
    code, facts = validate["catalog_switch"](
        SimpleNamespace(from_ref=("a", "r1"), to_ref=("b", "r1")), entries, store
    )
    assert time.monotonic() - started < 1
    assert code == facts["exit_code"] == 2
    assert facts["explanation"] == "Загрузка модели A не удалась"
    assert current_path.read_bytes() == before
