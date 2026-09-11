"""Unit-проверки раскладок, сканера ONNX и утилит движка (T-10)."""

from __future__ import annotations

import logging
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import numpy.typing as npt
import pytest

from astra_voice.worker import engine as engine_module
from astra_voice.worker.engine import (
    LAYOUTS,
    CancelToken,
    EngineError,
    EngineUnavailableError,
    ExternalDataError,
    ExtraFileError,
    LoadResult,
    LoadTimeoutError,
    ModelMissingError,
    OnnxAsrEngine,
    TranscribeResult,
    _find_sessions,
    _RecognitionCancelled,
    active_sessions,
    check_layout,
    layout_files,
    make_engine,
    run_with_deadline,
)
from astra_voice.worker.onnx_scan import collect_external_locations, scan_model_dir

pytestmark = pytest.mark.unit

LAYOUT = "onnx-asr-gigaam-v3"
VARIANT = "gigaam-v3-e2e-ctc"
ONNX = "v3_e2e_ctc.int8.onnx"
REQUIRED = (ONNX, "v3_e2e_ctc_vocab.txt", "config.json")
VARIANTS = (
    (
        "gigaam-v3-e2e-rnnt",
        (
            "v3_e2e_rnnt_encoder.int8.onnx",
            "v3_e2e_rnnt_decoder.int8.onnx",
            "v3_e2e_rnnt_joint.int8.onnx",
            "v3_e2e_rnnt_vocab.txt",
            "config.json",
        ),
        "v3_e2e_rnnt.yaml",
    ),
    (VARIANT, REQUIRED, "v3_e2e_ctc.yaml"),
    (
        "gigaam-v3-rnnt",
        (
            "v3_rnnt_encoder.int8.onnx",
            "v3_rnnt_decoder.int8.onnx",
            "v3_rnnt_joint.int8.onnx",
            "v3_vocab.txt",
            "config.json",
        ),
        "v3_rnnt.yaml",
    ),
)


def _varint(value: int) -> bytes:
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def _field(number: int, value: bytes | int) -> bytes:
    """Минимальный protobuf: wire 0 (varint) или wire 2 (length-delimited)."""
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _tensor(location: str | None = None) -> bytes:
    """Кодирует тензор с необязательной ссылкой на внешние данные."""
    tensor = _field(14, int(location is not None))
    if location is not None:
        entry = _field(1, b"location") + _field(2, location.encode("utf-8"))
        tensor += _field(13, entry)
    return tensor


def _model(location: str | None = None, *, depth: int = 0) -> bytes:
    graph = _field(5, _tensor(location))  # GraphProto.initializer -> TensorProto
    for _ in range(depth):
        attribute = _field(6, graph)  # AttributeProto.g -> GraphProto
        node = _field(5, attribute)  # NodeProto.attribute -> AttributeProto
        graph = _field(1, node)  # GraphProto.node -> NodeProto
    return _field(7, graph)  # ModelProto.graph -> GraphProto


def _write_layout(model_dir: Path, files: Sequence[str] = REQUIRED) -> Path:
    model_dir.mkdir()
    for name in files:
        (model_dir / name).write_bytes(_model() if name.endswith(".onnx") else b"")
    return model_dir


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    return _write_layout(tmp_path / "model")


@pytest.mark.parametrize(("variant", "required", "optional"), VARIANTS)
@pytest.mark.parametrize("with_optional", [False, True])
def test_valid_layout(
    tmp_path: Path,
    variant: str,
    required: tuple[str, ...],
    optional: str,
    with_optional: bool,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected = [*required, *([optional] if with_optional else [])]
    directory = _write_layout(tmp_path / "model", expected)

    check_layout(directory, LAYOUT, variant)
    assert layout_files(directory, LAYOUT, variant) == expected
    scan_model_dir(directory, expected)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert not caplog.records


@pytest.mark.parametrize("extra", ["v3_e2e_ctc.onnx", "v3_rnnt_encoder.int8.onnx"])
def test_extra_file(model_dir: Path, tmp_path: Path, extra: str) -> None:
    (model_dir / extra).write_bytes(_model())
    with pytest.raises(ExtraFileError) as exc:
        check_layout(model_dir, LAYOUT, VARIANT)
    assert exc.value.code == "extra-file"
    assert str(tmp_path) not in str(exc.value)


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_non_regular_layout_entry(model_dir: Path, tmp_path: Path, kind: str) -> None:
    # Даже разрешённое имя не делает каталог или симлинк обычным файлом.
    entry = model_dir / "config.json"
    entry.unlink()
    if kind == "directory":
        entry.mkdir()
    else:
        target = tmp_path / "config.json"
        target.write_bytes(b"")
        entry.symlink_to(target)
    with pytest.raises(ExtraFileError) as exc:
        check_layout(model_dir, LAYOUT, VARIANT)
    assert exc.value.code == "extra-file"
    assert str(tmp_path) not in str(exc.value)


@pytest.mark.parametrize("missing", REQUIRED)
def test_missing_required_file(model_dir: Path, tmp_path: Path, missing: str) -> None:
    (model_dir / missing).unlink()
    with pytest.raises(ModelMissingError) as exc:
        check_layout(model_dir, LAYOUT, VARIANT)
    assert exc.value.code == "model-missing"
    assert str(tmp_path) not in str(exc.value)


def test_missing_model_directory(tmp_path: Path) -> None:
    with pytest.raises(ModelMissingError) as exc:
        check_layout(tmp_path / "absent", LAYOUT, VARIANT)
    assert exc.value.code == "model-missing"
    assert str(tmp_path) not in str(exc.value)


@pytest.mark.parametrize(("layout", "variant"), [("unknown", VARIANT), (LAYOUT, "unknown")])
def test_unknown_layout_or_variant(model_dir: Path, layout: str, variant: str) -> None:
    with pytest.raises(ValueError):
        check_layout(model_dir, layout, variant)


def test_unknown_engine() -> None:
    with pytest.raises(ValueError):
        make_engine("что-то")


@pytest.mark.parametrize("location", ["../secret.bin", "/etc/passwd", "", "a\x00b"])
def test_unsafe_external_location(model_dir: Path, tmp_path: Path, location: str) -> None:
    (tmp_path / "secret.bin").write_bytes(b"private")
    (model_dir / ONNX).write_bytes(_model(location))
    with pytest.raises(ExternalDataError) as exc:
        scan_model_dir(model_dir, REQUIRED)
    assert exc.value.code == "external-data"
    assert str(tmp_path) not in str(exc.value)


def test_external_data_in_local_function(
    model_dir: Path, blocked_runtimes: _BlockRuntimeImports
) -> None:
    """Воспроизводит обход У7 через тензор в теле локальной функции."""
    attribute = _field(5, _tensor("../../../etc/shadow"))  # AttributeProto.t
    node = _field(5, attribute)  # NodeProto.attribute
    function = _field(7, node)  # FunctionProto.node
    (model_dir / ONNX).write_bytes(_model() + _field(25, function))

    with pytest.raises(ExternalDataError, match="Не удалось проверить файлы модели") as exc:
        scan_model_dir(model_dir, REQUIRED)
    assert exc.value.code == "external-data"
    with pytest.raises(ExternalDataError, match="Не удалось проверить файлы модели") as exc:
        make_engine(LAYOUT).load(model_dir, LAYOUT, VARIANT, threads=1)
    assert exc.value.code == "external-data"
    assert "локальные функции" in str(exc.value.__cause__)
    assert blocked_runtimes.attempts == []


@pytest.mark.parametrize("field_number", [20, 25], ids=["training_info", "functions"])
@pytest.mark.parametrize(
    "payload",
    [b"", _field(1, b""), b"\x80", 0],
    ids=["empty", "message", "malformed-body", "wrong-wire"],
)
def test_unsupported_model_field(model_dir: Path, field_number: int, payload: bytes | int) -> None:
    """Само присутствие поля запрещено независимо от его содержимого."""
    (model_dir / ONNX).write_bytes(_model() + _field(field_number, payload))
    reason = "обучающие графы" if field_number == 20 else "локальные функции"
    with pytest.raises(ExternalDataError, match="Не удалось проверить файлы модели") as exc:
        scan_model_dir(model_dir, REQUIRED)
    assert reason in str(exc.value.__cause__)
    assert exc.value.code == "external-data"


@pytest.mark.parametrize(
    "field_path",
    [
        (7, 5),  # GraphProto.initializer
        (7, 15, 1),  # GraphProto.sparse_initializer.values
        (7, 15, 2),  # GraphProto.sparse_initializer.indices
        (7, 1, 5, 5),  # AttributeProto.t
        (7, 1, 5, 6, 5),  # AttributeProto.g.initializer
        (7, 1, 5, 10),  # AttributeProto.tensors
        (7, 1, 5, 11, 5),  # AttributeProto.graphs.initializer
        (7, 1, 5, 22, 1),  # AttributeProto.sparse_tensor.values
        (7, 1, 5, 22, 2),  # AttributeProto.sparse_tensor.indices
        (7, 1, 5, 23, 1),  # AttributeProto.sparse_tensors.values
        (7, 1, 5, 23, 2),  # AttributeProto.sparse_tensors.indices
    ],
)
def test_external_data_in_all_tensor_fields(model_dir: Path, field_path: tuple[int, ...]) -> None:
    """Каждая ветка схемы должна обнаруживать внешние данные тензора."""
    location = "../../../etc/shadow"
    payload = _tensor(location)
    for number in reversed(field_path):
        payload = _field(number, payload)
    (model_dir / ONNX).write_bytes(payload)

    assert collect_external_locations(model_dir / ONNX) == [location]
    with pytest.raises(ExternalDataError) as exc:
        scan_model_dir(model_dir, REQUIRED)
    assert exc.value.code == "external-data"


@pytest.mark.parametrize("with_attribute", [False, True])
def test_valid_model_without_unsupported_fields(
    model_dir: Path,
    with_attribute: bool,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Обычная модель проходит молча; номера полей учитываются только по схеме."""
    payload = _model()
    if with_attribute:
        # AttributeProto.type = 20 допустим; strings = 9 не содержит TensorProto.
        attribute = _field(20, 8) + _field(9, b"ordinary string")
        # Байты с тегами 20 и 25 внутри строки не являются полями ModelProto.
        attribute += _field(9, _field(20, 0) + _field(25, 0))
        payload += _field(7, _field(1, _field(5, attribute)))
    (model_dir / ONNX).write_bytes(payload)

    assert collect_external_locations(model_dir / ONNX) == []
    scan_model_dir(model_dir, REQUIRED)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert not caplog.records


def test_external_file_not_in_layout(model_dir: Path) -> None:
    (model_dir / "unlisted.bin").write_bytes(b"")
    (model_dir / ONNX).write_bytes(_model("unlisted.bin"))
    with pytest.raises(ExternalDataError) as exc:
        scan_model_dir(model_dir, REQUIRED)
    assert exc.value.code == "external-data"


def test_external_symlink_escapes_model_dir(model_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.bin"
    outside.write_bytes(b"private")
    (model_dir / "weights.bin").symlink_to(outside)
    (model_dir / ONNX).write_bytes(_model("weights.bin"))
    # Имя разрешено явно: отказ должен быть из-за выхода симлинка за границу.
    with pytest.raises(ExternalDataError) as exc:
        scan_model_dir(model_dir, [*REQUIRED, "weights.bin"])
    assert exc.value.code == "external-data"
    assert str(tmp_path) not in str(exc.value)


@pytest.mark.parametrize("depth", [0, 1, 3])
def test_allowed_external_data(model_dir: Path, depth: int) -> None:
    (model_dir / "weights.bin").write_bytes(b"")
    (model_dir / ONNX).write_bytes(_model("weights.bin", depth=depth))
    assert collect_external_locations(model_dir / ONNX) == ["weights.bin"]
    scan_model_dir(model_dir, [*REQUIRED, "weights.bin"])


@pytest.mark.parametrize("depth", [1, 3])
def test_external_data_in_nested_subgraph(model_dir: Path, depth: int) -> None:
    (model_dir / ONNX).write_bytes(_model("../secret.bin", depth=depth))
    # Доказываем обнаружение ссылки, а не случайный отказ парсера фикстуры.
    assert collect_external_locations(model_dir / ONNX) == ["../secret.bin"]
    with pytest.raises(ExternalDataError) as exc:
        scan_model_dir(model_dir, REQUIRED)
    assert exc.value.code == "external-data"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x80",  # Обрезанный varint тега.
        b"\x3a\x80",  # Обрезанный varint длины графа.
        _model()[:-1],  # Объявленная длина выходит за EOF.
        b"\x00",  # Нулевой номер поля.
        b"\x0f",  # Недопустимый wire type.
        b"\x80" * 10 + b"\x00",  # Переполнение varint.
        _field(7, 1),  # graph должен быть сообщением, не varint.
        _field(7, _field(5, b"\x70\x80")),  # Обрезанный TensorProto.data_location.
    ],
    ids=["empty", "tag", "length", "body", "field-zero", "wire", "overflow", "schema", "tensor"],
)
def test_malformed_protobuf(model_dir: Path, payload: bytes) -> None:
    (model_dir / ONNX).write_bytes(payload)
    with pytest.raises(ExternalDataError) as exc:
        scan_model_dir(model_dir, REQUIRED)
    assert exc.value.code == "external-data"


def test_excessive_subgraph_depth(model_dir: Path) -> None:
    # Генератор итеративный; внешних ссылок нет, отказ вызван только глубиной.
    (model_dir / ONNX).write_bytes(_model(depth=80))
    with pytest.raises(ExternalDataError, match="Не удалось проверить файлы модели") as exc:
        scan_model_dir(model_dir, REQUIRED)
    assert exc.value.code == "external-data"
    assert "Глубина" in str(exc.value.__cause__)
    assert not isinstance(exc.value.__cause__, RecursionError)


class _BlockRuntimeImports(MetaPathFinder):
    def __init__(self) -> None:
        self.attempts: list[str] = []

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname.split(".")[0] in {"onnxruntime", "onnx_asr"}:
            self.attempts.append(fullname)
            raise ImportError(f"Test blocks {fullname}")
        return None


@pytest.fixture
def blocked_runtimes(monkeypatch: pytest.MonkeyPatch) -> _BlockRuntimeImports:
    for name in tuple(sys.modules):
        if name.split(".")[0] in {"onnxruntime", "onnx_asr"}:
            monkeypatch.delitem(sys.modules, name)
    blocker = _BlockRuntimeImports()
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    return blocker


@pytest.mark.parametrize("missing", ["onnxruntime", "onnx_asr"])
def test_unavailable_runtime(
    model_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_runtimes: _BlockRuntimeImports,
    missing: str,
) -> None:
    if missing == "onnxruntime":
        # onnx_asr импортируется первым: пропускаем его без реального пакета.
        monkeypatch.setitem(sys.modules, "onnx_asr", ModuleType("onnx_asr"))
    with pytest.raises(EngineUnavailableError) as exc:
        make_engine(LAYOUT).load(model_dir, LAYOUT, VARIANT, threads=1)
    assert exc.value.code == "engine-unavailable"
    assert isinstance(exc.value.__cause__, ImportError)
    assert blocked_runtimes.attempts == [missing]


def test_active_sessions_without_runtime(blocked_runtimes: _BlockRuntimeImports) -> None:
    assert "onnxruntime" not in sys.modules
    assert active_sessions() == 0
    assert blocked_runtimes.attempts == []


def test_cancel_token() -> None:
    token = CancelToken()
    assert token.cancelled is False
    token.cancel()
    assert token.cancelled is True
    token.cancel()
    assert token.cancelled is True


def test_cancel_token_from_another_thread() -> None:
    token = CancelToken()
    assert token.cancelled is False
    thread = threading.Thread(target=token.cancel, daemon=True)
    thread.start()
    thread.join(timeout=0.5)
    assert not thread.is_alive()
    assert token.cancelled is True


def _unexpected_timeout() -> None:
    pytest.fail("Быстрая функция не должна достигать таймаута")


def test_deadline_returns_result() -> None:
    result = object()
    assert run_with_deadline(lambda: result, 0.2, _unexpected_timeout) is result


def test_deadline_propagates_exception() -> None:
    error = ValueError("failure in worker")

    def fail() -> None:
        raise error

    with pytest.raises(ValueError) as exc:
        run_with_deadline(fail, 0.2, _unexpected_timeout)
    assert exc.value is error


def test_deadline_raises_timeout_error() -> None:
    release = threading.Event()
    finished = threading.Event()
    error = LoadTimeoutError("test deadline")

    def work() -> None:
        try:
            release.wait(timeout=0.5)
        finally:
            finished.set()

    try:
        with pytest.raises(LoadTimeoutError) as exc:
            run_with_deadline(work, 0.01, lambda: error)
        assert exc.value is error
        assert exc.value.code == "load-timeout"
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(timeout=0.5)


def test_deadline_none_handler_waits_for_completion() -> None:
    release = threading.Event()
    finished = threading.Event()
    timeout_calls: list[bool] = []
    result = object()

    def work() -> object:
        try:
            assert release.wait(timeout=0.5)
            time.sleep(0.02)
            return result
        finally:
            finished.set()

    def on_timeout() -> None:
        timeout_calls.append(True)
        assert not finished.is_set()
        release.set()

    try:
        assert run_with_deadline(work, 0.01, on_timeout) is result
        assert timeout_calls == [True]
        assert finished.is_set()
    finally:
        release.set()
        assert finished.wait(timeout=0.5)


TEXT = "Настоящий распознанный текст"


class _FakeFail(Exception):
    """Ошибка ORT без зависимости от установленного рантайма."""


class _FakeRunOptions:
    """Позволяет дождаться установки terminate сторожем без sleep."""

    def __init__(self) -> None:
        self.terminated = threading.Event()

    @property
    def terminate(self) -> bool:
        return self.terminated.is_set()

    @terminate.setter
    def terminate(self, value: bool) -> None:
        if value:
            self.terminated.set()
        else:
            self.terminated.clear()


class _FakeSession:
    """Шаг инференса с управляемым завершением или ошибкой."""

    def __init__(self) -> None:
        self.calls = 0
        self.on_run: Callable[[_FakeRunOptions], None] = lambda options: None

    def run(self, names: object, feed: object, run_options: _FakeRunOptions | None = None) -> str:
        self.calls += 1
        assert run_options is not None, "Обёртка должна передать опции отмены"
        self.on_run(run_options)
        return TEXT


class _FakeAdapter:
    """Хранит сессии под именами, которых движок заранее не знает."""

    def __init__(self, sessions: Sequence[_FakeSession] = ()) -> None:
        self.asr = SimpleNamespace(**{f"unexpected_{i}": s for i, s in enumerate(sessions)})
        self.operation: Callable[[], str] = lambda: TEXT

    def recognize(self, audio: object, *, sample_rate: int) -> str:
        assert sample_rate == 16000
        return self.operation()


class _FakeVendorAdapter(_FakeAdapter):
    """Предоставляет опции патча и сбрасывает их при выходе из контекста."""

    def __init__(self, sessions: Sequence[_FakeSession] = ()) -> None:
        super().__init__(sessions)
        self.options = _FakeRunOptions()
        self.entered = False
        self.exited = False

    @contextmanager
    def cancellable(self) -> Iterator[_FakeRunOptions]:
        self.entered = True
        try:
            yield self.options
        finally:
            self.options.terminate = False
            self.exited = True


@pytest.fixture
def fake_runtime(
    monkeypatch: pytest.MonkeyPatch, blocked_runtimes: _BlockRuntimeImports
) -> Iterator[None]:
    """Подменяет только модули ORT; импорт настоящего рантайма запрещён."""
    runtime = ModuleType("onnxruntime")
    vars(runtime).update(InferenceSession=_FakeSession, RunOptions=_FakeRunOptions)
    state = ModuleType("onnxruntime.capi.onnxruntime_pybind11_state")
    vars(state)["Fail"] = _FakeFail
    monkeypatch.setitem(sys.modules, "onnxruntime", runtime)
    monkeypatch.setitem(sys.modules, "onnxruntime.capi", ModuleType("onnxruntime.capi"))
    monkeypatch.setitem(sys.modules, state.__name__, state)
    yield
    assert blocked_runtimes.attempts == []


def _load_fake_engine(
    monkeypatch: pytest.MonkeyPatch, adapter: _FakeAdapter, variant: str = VARIANT
) -> tuple[OnnxAsrEngine, LoadResult]:
    """Проходит настоящий load, подменяя только создание адаптера."""

    def load_model(
        model_dir: Path, layout: str, variant: str, threads: int
    ) -> tuple[_FakeAdapter, str]:
        return adapter, "onnx-asr fake/onnxruntime fake"

    engine = OnnxAsrEngine()
    monkeypatch.setattr(engine, "_load_model", load_model)
    return engine, engine.load(Path("."), LAYOUT, variant, threads=2)


class _FakeModelLoadingError(Exception):
    """Повторяет базовый тип ошибок загрузки onnx-asr без импорта рантайма."""


class _FakeModelFileNotFoundError(_FakeModelLoadingError, FileNotFoundError):
    """Повторяет иерархию ModelFileNotFoundError."""


class _FakeMoreThanOneModelFileFoundError(_FakeModelLoadingError, OSError):
    """Повторяет иерархию MoreThanOneModelFileFoundError."""


class _FakeInvalidModelTypeInConfigError(_FakeModelLoadingError, ValueError):
    """Ошибка конфигурации модели не является ValueError нашего контракта."""


def _mock_adapter_failure(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Подменяет зависимости загрузчика и сбой непосредственно в create_asr."""

    class FakeManager:
        def __init__(self, **kwargs: object) -> None:
            pass

        def create_asr(self, name: str, directory: Path, **kwargs: object) -> None:
            raise error

    for name, attributes in (
        ("onnx_asr", {"__version__": "fake"}),
        ("onnx_asr.loader", {"Manager": FakeManager}),
        ("onnx_asr.utils", {"ModelLoadingError": _FakeModelLoadingError}),
        ("onnx_asr.preprocessors", {}),
        ("onnx_asr.preprocessors.resampler", {"Resampler": object}),
    ):
        module = ModuleType(name)
        vars(module).update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(
        sys.modules["onnxruntime"],
        "SessionOptions",
        lambda: SimpleNamespace(add_session_config_entry=lambda key, value: None),
        raising=False,
    )


def _assert_path_only_in_debug(caplog: pytest.LogCaptureFixture, path: Path) -> None:
    """Проверяет подробности DEBUG и отсутствие пути на остальных уровнях."""
    formatter = logging.Formatter()
    assert any(
        record.levelno == logging.DEBUG and str(path) in formatter.format(record)
        for record in caplog.records
    )
    assert all(
        str(path) not in formatter.format(record)
        for record in caplog.records
        if record.levelno >= logging.INFO
    )


@pytest.mark.parametrize(
    "error_type",
    [
        _FakeModelFileNotFoundError,
        _FakeMoreThanOneModelFileFoundError,
        _FakeInvalidModelTypeInConfigError,
        FileNotFoundError,
        RuntimeError,
        _FakeFail,
    ],
)
def test_adapter_creation_failure_hides_path(
    model_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_runtime: None,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
) -> None:
    """Сбой сборки модели сохраняет причину только для отладки и код model-missing."""
    error = error_type(f"Не удалось прочитать {model_dir / ONNX}")
    _mock_adapter_failure(monkeypatch, error)
    caplog.set_level(logging.DEBUG, logger=engine_module.__name__)
    with pytest.raises(ModelMissingError) as exc:
        make_engine(LAYOUT).load(model_dir, LAYOUT, VARIANT, threads=1)
    assert exc.value.code == "model-missing"
    assert str(tmp_path) not in str(exc.value)
    assert ONNX not in str(exc.value)
    assert exc.value.__cause__ is error
    _assert_path_only_in_debug(caplog, model_dir)


@pytest.mark.parametrize("kind", ["extra", "missing", "external"])
def test_model_validation_details_only_in_debug(
    model_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    """Путь каталога и внешняя абсолютная ссылка не выходят в пользовательский текст."""
    caplog.set_level(logging.DEBUG, logger=engine_module.__name__)
    error_type: type[EngineError]
    if kind == "extra":
        (model_dir / "private-name.txt").touch()
        error_type = ExtraFileError
    elif kind == "missing":
        (model_dir / "config.json").unlink()
        error_type = ModelMissingError
    else:
        (model_dir / ONNX).write_bytes(_model(str(model_dir / "secret.bin")))
        error_type = ExternalDataError
    with pytest.raises(error_type) as exc:
        make_engine(LAYOUT).load(model_dir, LAYOUT, VARIANT, threads=1)
    assert exc.value.code == error_type.code
    assert str(tmp_path) not in str(exc.value)
    assert "private-name.txt" not in str(exc.value)
    _assert_path_only_in_debug(caplog, model_dir)


@pytest.mark.parametrize("vendor", [False, True])
@pytest.mark.parametrize("stage", ["sessions", "context", "recognize"])
def test_recognition_failure_hides_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_runtime: None,
    caplog: pytest.LogCaptureFixture,
    vendor: bool,
    stage: str,
) -> None:
    """Сбои сессий, контекста отмены и распознавания не раскрывают путь."""
    session = _FakeSession()
    adapter = _FakeVendorAdapter([session]) if vendor else _FakeAdapter([session])
    engine, _ = _load_fake_engine(monkeypatch, adapter)
    error = RuntimeError(f"Ошибка рантайма: {tmp_path / ONNX}")

    def fail(*args: object, **kwargs: object) -> str:
        raise error

    if stage == "sessions":
        monkeypatch.setattr(engine_module, "_find_sessions", fail)
    elif stage == "context":
        if vendor:
            monkeypatch.setattr(adapter, "cancellable", fail)
        else:
            monkeypatch.setattr(sys.modules["onnxruntime"], "RunOptions", fail)
    else:
        adapter.operation = fail
    caplog.set_level(logging.DEBUG, logger=engine_module.__name__)
    with pytest.raises(EngineUnavailableError) as exc:
        engine.transcribe(np.zeros(16, dtype=np.float32), CancelToken())
    assert exc.value.code == "engine-unavailable"
    assert str(tmp_path) not in str(exc.value)
    assert exc.value.__cause__ is error
    assert "run" not in vars(session)
    if stage == "recognize" and isinstance(adapter, _FakeVendorAdapter):
        assert adapter.exited
    _assert_path_only_in_debug(caplog, tmp_path)


@pytest.mark.parametrize("stage", ["load", "recognize"])
@pytest.mark.parametrize(
    "error_type",
    [
        EngineError,
        ExtraFileError,
        ExternalDataError,
        ModelMissingError,
        LoadTimeoutError,
        EngineUnavailableError,
        ValueError,
    ],
)
def test_own_errors_propagate_unchanged(
    model_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_runtime: None,
    stage: str,
    error_type: type[Exception],
) -> None:
    """Наши ошибки и ValueError контракта сохраняют тип и исходный объект."""
    error = error_type("Ошибка контракта")
    with pytest.raises(error_type) as exc:
        if stage == "load":
            _mock_adapter_failure(monkeypatch, error)
            make_engine(LAYOUT).load(model_dir, LAYOUT, VARIANT, threads=1)
        else:
            adapter = _FakeAdapter([_FakeSession()])

            def fail() -> str:
                raise error

            adapter.operation = fail
            engine, _ = _load_fake_engine(monkeypatch, adapter)
            engine.transcribe(np.zeros(16, dtype=np.float32), CancelToken())
    assert exc.value is error


def test_load_preserves_internal_cancellation(
    model_dir: Path, monkeypatch: pytest.MonkeyPatch, fake_runtime: None
) -> None:
    """Защитная обёртка загрузки не подменяет внутренний сигнал отмены."""
    error = _RecognitionCancelled()
    _mock_adapter_failure(monkeypatch, error)
    with pytest.raises(_RecognitionCancelled) as exc:
        make_engine(LAYOUT).load(model_dir, LAYOUT, VARIANT, threads=1)
    assert exc.value is error


@pytest.mark.parametrize("late_error", [False, True])
def test_load_timeout_unload_releases_late_model(
    monkeypatch: pytest.MonkeyPatch, fake_runtime: None, late_error: bool
) -> None:
    """Выгрузка ждёт опоздавший поток и освобождает модель даже при его ошибке."""
    engine = OnnxAsrEngine()
    release = threading.Event()
    unloading = threading.Event()
    unloaded = threading.Event()
    models: list[weakref.ReferenceType[_FakeAdapter]] = []

    def load_model(
        model_dir: Path, layout: str, variant: str, threads: int
    ) -> tuple[_FakeAdapter, str]:
        adapter = _FakeAdapter([_FakeSession()])
        models.append(weakref.ref(adapter))
        if len(models) == 1:
            assert release.wait(timeout=2)
            if late_error:
                raise ValueError("Поздняя ошибка после создания сессий")
        return adapter, "fake"

    def unload() -> None:
        unloading.set()
        engine.unload()
        unloaded.set()

    monkeypatch.setattr(engine, "_load_model", load_model)
    monkeypatch.setattr(engine_module, "LOAD_TIMEOUT_S", 0.01)
    monkeypatch.setattr(engine_module, "UNLOAD_TIMEOUT_S", 0.5)
    unloader = threading.Thread(target=unload, daemon=True)
    try:
        # Сохраняем traceback таймаута: он не должен удерживать позднюю модель.
        with pytest.raises(LoadTimeoutError) as exc:
            engine.load(Path("."), LAYOUT, VARIANT, threads=2)
        assert exc.value.code == "load-timeout"
        assert len(models) == 1 and models[0]() is not None
        with pytest.raises(EngineUnavailableError, match=r"загрузка.*unload\(\)") as retry:
            engine.load(Path("."), LAYOUT, VARIANT, threads=2)
        assert retry.value.code == "engine-unavailable"
        assert len(models) == 1
        with pytest.raises(EngineUnavailableError):
            engine.transcribe(np.zeros(16, dtype=np.float32), CancelToken())

        unloader.start()
        assert unloading.wait(timeout=0.5)
        assert not unloaded.wait(timeout=0.02)
        release.set()
        unloader.join(timeout=1)
        assert not unloader.is_alive() and unloaded.is_set()
        assert models[0]() is None
        assert active_sessions() == 0

        monkeypatch.setattr(engine_module, "LOAD_TIMEOUT_S", 0.5)
        assert engine.load(Path("."), LAYOUT, VARIANT, threads=2).sessions == 1
        assert len(models) == 2
        result = engine.transcribe(np.zeros(16, dtype=np.float32), CancelToken())
        assert result.text == TEXT and not result.cancelled
        engine.unload()
        assert models[1]() is None
    finally:
        release.set()
        if unloader.ident is not None:
            unloader.join(timeout=1)
        engine.unload()


def test_unload_timeout_keeps_load_poisoned(
    monkeypatch: pytest.MonkeyPatch, fake_runtime: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Истечение предела выгрузки сохраняет запрет новой загрузки и пишет error."""
    engine = OnnxAsrEngine()
    release = threading.Event()
    finished = threading.Event()
    models: list[weakref.ReferenceType[_FakeAdapter]] = []

    def load_model(
        model_dir: Path, layout: str, variant: str, threads: int
    ) -> tuple[_FakeAdapter, str]:
        adapter = _FakeAdapter([_FakeSession()])
        models.append(weakref.ref(adapter))
        try:
            assert release.wait(timeout=2)
            return adapter, "fake"
        finally:
            finished.set()

    monkeypatch.setattr(engine, "_load_model", load_model)
    monkeypatch.setattr(engine_module, "LOAD_TIMEOUT_S", 0.01)
    monkeypatch.setattr(engine_module, "UNLOAD_TIMEOUT_S", 0.02)
    try:
        with pytest.raises(LoadTimeoutError):
            engine.load(Path("."), LAYOUT, VARIANT, threads=2)
        started = time.perf_counter()
        engine.unload()
        assert time.perf_counter() - started < 0.5
        assert not finished.is_set()
        assert models[0]() is not None
        assert any(
            record.levelname == "ERROR"
            and "сессии остались у осиротевшего потока" in record.message
            and "процесс должен перезапустить супервизор" in record.message
            for record in caplog.records
        )
        with pytest.raises(EngineUnavailableError, match=r"unload\(\)"):
            engine.load(Path("."), LAYOUT, VARIANT, threads=2)
        assert len(models) == 1

        release.set()
        assert finished.wait(timeout=0.5)
        # Само завершение потока не снимает пометку: нужен успешный unload.
        with pytest.raises(EngineUnavailableError):
            engine.load(Path("."), LAYOUT, VARIANT, threads=2)
        engine.unload()
        assert models[0]() is None
        assert active_sessions() == 0
        monkeypatch.setattr(engine_module, "LOAD_TIMEOUT_S", 0.5)
        assert engine.load(Path("."), LAYOUT, VARIANT, threads=2).sessions == 1
    finally:
        release.set()
        monkeypatch.setattr(engine_module, "UNLOAD_TIMEOUT_S", 0.5)
        engine.unload()


@pytest.mark.parametrize(
    ("variant", "expected"),
    [("gigaam-v3-e2e-rnnt", 3), ("gigaam-v3-rnnt", 3), (VARIANT, 1)],
)
def test_expected_layout_sessions(variant: str, expected: int) -> None:
    assert LAYOUTS[LAYOUT][variant].sessions == expected


def test_find_sessions_in_preprocessors() -> None:
    first, second, third = _FakeSession(), _FakeSession(), _FakeSession()
    adapter = _FakeAdapter([first])
    adapter.asr._preprocessors = {"duplicate": first, "features": second, "noise": object()}
    vars(adapter)["resampler"] = SimpleNamespace(_preprocessors={8000: third})
    assert _find_sessions(adapter, _FakeSession) == [first, second, third]


@pytest.mark.parametrize("vendor", [False, True])
def test_cancel_between_runs_with_unexpected_session_names(
    monkeypatch: pytest.MonkeyPatch, fake_runtime: None, vendor: bool
) -> None:
    session = _FakeSession()
    original = session.run
    adapter = _FakeVendorAdapter([session]) if vendor else _FakeAdapter([session])
    token = CancelToken()

    def recognize() -> str:
        assert session.run(None, {}) == TEXT
        token.cancel()
        return session.run(None, {})

    adapter.operation = recognize
    engine, loaded = _load_fake_engine(monkeypatch, adapter)
    mode = "vendor-patch" if vendor else "fallback-wrapper"
    assert f"cancel={mode}" in loaded.engine_version
    result = engine.transcribe(np.zeros(16, dtype=np.float32), token)
    assert result.cancelled and result.text == ""
    assert session.calls == 1
    assert session.run == original
    assert "run" not in vars(session)
    if isinstance(adapter, _FakeVendorAdapter):
        assert adapter.entered and adapter.exited


def test_unavailable_cancel_preserves_text(
    monkeypatch: pytest.MonkeyPatch, fake_runtime: None, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = _FakeAdapter()
    token = CancelToken()

    def recognize() -> str:
        token.cancel()
        return TEXT

    adapter.operation = recognize
    engine, loaded = _load_fake_engine(monkeypatch, adapter)
    assert "cancel=unavailable" in loaded.engine_version
    assert loaded.sessions == 0
    assert any(
        record.levelname == "ERROR" and "распознавание нельзя прервать" in record.message
        for record in caplog.records
    )
    result = engine.transcribe(np.zeros(16, dtype=np.float32), token)
    assert token.cancelled
    assert result.text == TEXT and not result.cancelled


@pytest.mark.parametrize(
    ("asr_count", "resampler_count", "extra_count"), [(1, 0, 0), (3, 7, 0), (3, 0, 1)]
)
def test_load_logs_session_count_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    fake_runtime: None,
    caplog: pytest.LogCaptureFixture,
    asr_count: int,
    resampler_count: int,
    extra_count: int,
) -> None:
    adapter = _FakeAdapter([_FakeSession() for _ in range(asr_count)])
    vars(adapter)["resampler"] = SimpleNamespace(
        _preprocessors={i: _FakeSession() for i in range(resampler_count)}
    )
    extra = [_FakeSession() for _ in range(extra_count)]
    _, loaded = _load_fake_engine(monkeypatch, adapter, "gigaam-v3-e2e-rnnt")
    found = asr_count + resampler_count
    actual = found + len(extra)
    assert loaded.sessions == actual
    assert any(
        record.levelname == "ERROR"
        and f"ожидалось 3, найдено по типу {found}, active_sessions()={actual}" in record.message
        for record in caplog.records
    )


def test_cancel_after_recognition_preserves_text(
    monkeypatch: pytest.MonkeyPatch, fake_runtime: None
) -> None:
    adapter = _FakeAdapter([_FakeSession()])
    engine, _ = _load_fake_engine(monkeypatch, adapter)
    recognize = engine._recognize

    def cancel_after_recognition(
        audio: npt.NDArray[np.float32], cancel: CancelToken
    ) -> tuple[str, bool]:
        result = recognize(audio, cancel)
        cancel.cancel()
        return result

    monkeypatch.setattr(engine, "_recognize", cancel_after_recognition)
    token = CancelToken()
    result = engine.transcribe(np.zeros(16, dtype=np.float32), token)
    assert token.cancelled
    assert result.text == TEXT and not result.cancelled


def test_cancel_during_last_run_preserves_text(
    monkeypatch: pytest.MonkeyPatch, fake_runtime: None
) -> None:
    session = _FakeSession()
    adapter = _FakeAdapter([session])
    token = CancelToken()

    def finish(options: _FakeRunOptions) -> None:
        token.cancel()
        assert options.terminated.wait(timeout=1)

    session.on_run = finish
    adapter.operation = lambda: session.run(None, {})
    engine, _ = _load_fake_engine(monkeypatch, adapter)
    result = engine.transcribe(np.zeros(16, dtype=np.float32), token)
    assert token.cancelled
    assert result.text == TEXT and not result.cancelled


@pytest.mark.parametrize("vendor", [False, True])
def test_ort_cancel_does_not_depend_on_error_text(
    monkeypatch: pytest.MonkeyPatch, fake_runtime: None, vendor: bool
) -> None:
    session = _FakeSession()
    adapter = _FakeVendorAdapter([session]) if vendor else _FakeAdapter([session])
    token = CancelToken()

    def fail_after_termination(options: _FakeRunOptions) -> None:
        token.cancel()
        assert options.terminated.wait(timeout=1)
        raise _FakeFail("Выполнение остановлено: другая формулировка ORT")

    session.on_run = fail_after_termination
    adapter.operation = lambda: session.run(None, {})
    engine, _ = _load_fake_engine(monkeypatch, adapter)
    result = engine.transcribe(np.zeros(16, dtype=np.float32), token)
    assert result.cancelled and result.text == ""
    assert "run" not in vars(session)
    if isinstance(adapter, _FakeVendorAdapter):
        assert adapter.exited and not adapter.options.terminate


@pytest.mark.parametrize("message", ["Ошибка вычисления", "terminate flag"])
def test_ort_fail_without_our_termination_is_wrapped(
    monkeypatch: pytest.MonkeyPatch, fake_runtime: None, message: str
) -> None:
    session = _FakeSession()
    adapter = _FakeAdapter([session])
    error = _FakeFail(message)

    def fail(options: _FakeRunOptions) -> None:
        raise error

    session.on_run = fail
    adapter.operation = lambda: session.run(None, {})
    engine, _ = _load_fake_engine(monkeypatch, adapter)
    token = CancelToken()
    with pytest.raises(EngineUnavailableError) as exc:
        engine.transcribe(np.zeros(16, dtype=np.float32), token)
    assert exc.value.code == "engine-unavailable"
    assert exc.value.__cause__ is error
    assert message not in str(exc.value)
    assert not token.cancelled
    assert "run" not in vars(session)


@pytest.mark.parametrize("with_session", [False, True])
def test_infer_timeout_without_interruption_preserves_text(
    monkeypatch: pytest.MonkeyPatch,
    fake_runtime: None,
    caplog: pytest.LogCaptureFixture,
    with_session: bool,
) -> None:
    session = _FakeSession()
    adapter = _FakeAdapter([session] if with_session else [])
    token = CancelToken()

    def finish() -> str:
        assert token.wait(timeout=1)
        return TEXT

    def finish_run(options: _FakeRunOptions) -> None:
        assert token.wait(timeout=1)

    if with_session:
        session.on_run = finish_run
        adapter.operation = lambda: session.run(None, {})
    else:
        adapter.operation = finish
    engine, _ = _load_fake_engine(monkeypatch, adapter)
    monkeypatch.setattr(engine_module, "INFER_TIMEOUT_S", 0.01)
    result = engine.transcribe(np.zeros(16, dtype=np.float32), token)
    assert token.cancelled
    assert result.text == TEXT and not result.cancelled
    assert any(
        record.levelname == "WARNING" and "превысило" in record.message for record in caplog.records
    )


@pytest.mark.parametrize("vendor", [False, True])
def test_unresponsive_infer_timeout_releases_lock(
    monkeypatch: pytest.MonkeyPatch, fake_runtime: None, vendor: bool
) -> None:
    """Игнорирование отмены ограничивает ожидание и запрещает повторный инференс."""
    session = _FakeSession()
    adapter = _FakeVendorAdapter([session]) if vendor else _FakeAdapter([session])
    release = threading.Event()
    entered = threading.Event()
    token = CancelToken()

    def ignore_cancel(options: _FakeRunOptions) -> None:
        entered.set()
        assert options.terminated.wait(timeout=1)
        assert release.wait(timeout=2)

    session.on_run = ignore_cancel
    adapter.operation = lambda: session.run(None, {})
    engine, _ = _load_fake_engine(monkeypatch, adapter)
    monkeypatch.setattr(engine_module, "INFER_TIMEOUT_S", 0.01)
    monkeypatch.setattr(engine_module, "CANCEL_TIMEOUT_S", 0.03)
    monkeypatch.setattr(engine_module, "UNLOAD_TIMEOUT_S", 0.02)
    audio = np.zeros(16, dtype=np.float32)
    retry_errors: list[EngineUnavailableError] = []

    def retry_transcribe() -> None:
        try:
            engine.transcribe(audio, CancelToken())
        except EngineUnavailableError as error:
            retry_errors.append(error)

    retry = threading.Thread(target=retry_transcribe, daemon=True)
    try:
        started = time.perf_counter()
        with pytest.raises(EngineUnavailableError, match="не отвечает на отмену") as exc:
            engine.transcribe(audio, token)
        assert time.perf_counter() - started < 0.5
        assert exc.value.code == "engine-unavailable"
        assert "перезапуск воркера" in str(exc.value)
        assert token.cancelled and entered.is_set()
        assert session.calls == 1

        # Другой поток не может повторно войти в RLock, оставленный владельцем.
        retry.start()
        retry.join(timeout=0.5)
        assert not retry.is_alive()
        assert len(retry_errors) == 1
        assert retry_errors[0].code == "engine-unavailable"
        assert session.calls == 1
        with pytest.raises(EngineUnavailableError):
            engine.load(Path("."), LAYOUT, VARIANT, threads=2)

        engine.unload()
        with pytest.raises(EngineUnavailableError):
            engine.transcribe(audio, CancelToken())
        release.set()
        monkeypatch.setattr(engine_module, "UNLOAD_TIMEOUT_S", 0.5)
        engine.unload()
        assert "run" not in vars(session)
        if isinstance(adapter, _FakeVendorAdapter):
            assert adapter.exited
        engine.load(Path("."), LAYOUT, VARIANT, threads=2)
        adapter.operation = lambda: TEXT
        monkeypatch.setattr(engine_module, "INFER_TIMEOUT_S", 0.5)
        result = engine.transcribe(audio, CancelToken())
        assert result.text == TEXT and not result.cancelled
    finally:
        release.set()
        monkeypatch.setattr(engine_module, "UNLOAD_TIMEOUT_S", 0.5)
        engine.unload()
        if retry.ident is not None:
            retry.join(timeout=1)


def test_cancel_before_start_does_not_recognize(
    monkeypatch: pytest.MonkeyPatch, fake_runtime: None
) -> None:
    adapter = _FakeAdapter()
    adapter.operation = lambda: pytest.fail("Распознавание не должно начинаться")
    engine, _ = _load_fake_engine(monkeypatch, adapter)
    token = CancelToken()
    token.cancel()
    assert engine.transcribe(np.zeros(16, dtype=np.float32), token) == TranscribeResult(
        "", 0.0, True
    )
