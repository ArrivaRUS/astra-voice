"""Страж сбора проверяем подменой доступности Qt, не меняя окружение Python."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

GUARD_PATH = Path(__file__).resolve().parents[1] / "conftest.py"


@pytest.fixture
def guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("collection_guard", GUARD_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("import PyQt5", True),
        ("import os, PyQt5.QtWidgets as widgets", True),
        ("from PyQt5 import sip", True),
        ("from PyQt5.QtWidgets import (\n    QApplication,\n)", True),
        ("def test_gui():\n    from PyQt5 import sip\n", True),
        ("import pathlib", False),
        ("# from PyQt5 import sip\nimport os", False),
        ('text = "import PyQt5"', False),
        ('"""from PyQt5.QtWidgets import QApplication"""', False),
        ("import PyQt5_extra", False),
        ("from .PyQt5 import local", False),
        ("import PyQt5\nthis is invalid python", False),
    ],
)
def test_needs_qt(guard: ModuleType, text: str, expected: bool) -> None:
    assert guard.needs_qt(text) is expected


def write_package(tmp_path: Path, sources: dict[str, str]) -> Path:
    package = tmp_path / "src" / "astra_voice"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for filename, source in sources.items():
        path = package / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return package


@pytest.mark.parametrize(
    "text",
    [
        "import astra_voice.x.y",
        "import os, astra_voice.x.y as alias",
        "from astra_voice.x import y",
        "from astra_voice.x.y import value",
        "def test_local():\n    import astra_voice.x.y",
        "def test_local():\n    from astra_voice.x import y",
    ],
)
@pytest.mark.parametrize(
    "dependency", ["import astra_voice.b", "from astra_voice import b", "from .. import b"]
)
def test_transitive_qt(guard: ModuleType, tmp_path: Path, text: str, dependency: str) -> None:
    package = write_package(
        tmp_path,
        {
            "x/__init__.py": "",
            "x/y.py": dependency,
            "b.py": "from PyQt5 import sip\nraise AssertionError('исходники нельзя импортировать')",
        },
    )
    required = guard.qt_modules(package)
    assert required == {"astra_voice.x.y", "astra_voice.b"}
    assert guard.needs_qt(text, required)


@pytest.mark.parametrize("with_qt", [False, True])
def test_import_cycle(guard: ModuleType, tmp_path: Path, with_qt: bool) -> None:
    package = write_package(
        tmp_path,
        {
            "a.py": "from . import b",
            "b.py": "from . import a\n" + ("import PyQt5" if with_qt else ""),
        },
    )
    required = guard.qt_modules(package)
    assert required == ({"astra_voice.a", "astra_voice.b"} if with_qt else set())
    assert guard.needs_qt("from astra_voice import a", required) is with_qt


@pytest.mark.parametrize(
    "text",
    [
        "import astra_voice.plain",
        "from astra_voice import plain",
        "from astra_voice.plain import value",
        "import astra_voice.x",
        "from astra_voice.x import plain",
        "import astra_voice.x.y_extra",
        "import astra_voice_extra.x.y",
        "# import astra_voice.x.y\ntext = 'from astra_voice.x import y'",
    ],
)
def test_package_without_qt(guard: ModuleType, tmp_path: Path, text: str) -> None:
    package = write_package(
        tmp_path,
        {
            "plain.py": "import pathlib",
            "x/__init__.py": "",
            "x/plain.py": "from .. import plain",
            "x/y.py": "import PyQt5",
            "x/y_extra.py": "",
        },
    )
    assert not guard.needs_qt(text, guard.qt_modules(package))


@pytest.mark.parametrize("initializer", ["import PyQt5", "from . import qt"])
def test_package_initializer(guard: ModuleType, tmp_path: Path, initializer: str) -> None:
    package = write_package(
        tmp_path,
        {"x/__init__.py": initializer, "x/qt.py": "import PyQt5", "x/plain.py": ""},
    )
    required = guard.qt_modules(package)
    assert required == {"astra_voice.x", "astra_voice.x.qt", "astra_voice.x.plain"}
    assert guard.needs_qt("import astra_voice.x.plain", required)
    assert not guard.needs_qt("import astra_voice", required)


def test_analysis_cached_per_session(
    guard: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_package(tmp_path, {"a.py": "import PyQt5"})
    test_path = tmp_path / "test_a.py"
    test_path.write_text("import astra_voice.a", encoding="utf-8")
    config = Mock(stash=pytest.Stash(), rootpath=tmp_path)
    config.getini.return_value = ["test_*.py"]
    analyze = Mock(wraps=guard.qt_modules)
    monkeypatch.setattr(guard, "qt_available", lambda: False)
    monkeypatch.setattr(guard, "qt_modules", analyze)
    guard.pytest_sessionstart(Mock(config=config))
    assert guard.pytest_ignore_collect(test_path, config) is True
    test_path.write_text("raise AssertionError('повторный разбор')", encoding="utf-8")
    assert guard.pytest_ignore_collect(test_path, config) is True
    analyze.assert_called_once_with(tmp_path / "src" / "astra_voice")
    assert len(config.stash[guard._IGNORED]) == 1


@pytest.mark.parametrize(
    ("expression", "env", "expected"),
    [
        ("engine", {"ASTRA_VOICE_REQUIRE_QT": "1"}, True),
        ("", {"ASTRA_VOICE_REQUIRE_QT": "1"}, True),
        ("unit", {}, True),
        ("xvfb", {}, True),
        ("unit or xvfb", {}, True),
        ("engine", {}, False),
        ("", {}, False),
        ("", {"ASTRA_VOICE_REQUIRE_QT": "0"}, False),
        ("", {"ASTRA_VOICE_REQUIRE_QT": "true"}, False),
        ("engine and not unit", {}, False),
        ("not (unit or xvfb)", {}, False),
        ("not unit or xvfb", {}, True),
        ("not not unit", {}, True),
        ("not (engine and not (unit or xvfb))", {}, True),
        ("unit_extra or extra_xvfb", {}, False),
        ("not unit", {"ASTRA_VOICE_REQUIRE_QT": "1"}, True),
    ],
)
def test_qt_required(
    guard: ModuleType, expression: str, env: dict[str, str], expected: bool
) -> None:
    assert guard.qt_required(expression, env) is expected


@pytest.mark.parametrize(
    ("specs", "expected", "calls"),
    [
        ([None], False, 1),
        ([object(), None], False, 2),
        ([object(), object()], True, 2),
        ([object(), ModuleNotFoundError("PyQt5")], False, 2),
    ],
)
def test_qt_available(
    guard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    specs: list[object],
    expected: bool,
    calls: int,
) -> None:
    find_spec = Mock(side_effect=specs)
    monkeypatch.setattr(guard.importlib.util, "find_spec", find_spec)
    assert guard.qt_available() is expected
    assert find_spec.call_count == calls
    assert find_spec.call_args_list[0].args == ("PyQt5",)
    if calls == 2:
        assert find_spec.call_args_list[1].args == ("PyQt5.QtWidgets",)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (0, 'ASTRA_QT_PROBE={"svg":"ok","qml":"ok"}\n', "", ()),
        (
            0,
            'предупреждение\nASTRA_QT_PROBE={"svg":"ok","qml":"ok"}\n',
            "QStandardPaths: предупреждение\nASTRA_QT_PROBE=мусор",
            (),
        ),
        (0, 'ASTRA_QT_PROBE={"svg":"missing","qml":"ok"}', "", ("libqt5svg5",)),
        (
            0,
            'ASTRA_QT_PROBE={"svg":"ok","qml":"error: QtObject is not a type"}',
            "",
            ("qml-module-qtquick-controls2", "QtObject is not a type"),
        ),
        (
            0,
            'ASTRA_QT_PROBE={"svg":"missing","qml":"error: QtObject is not a type"}',
            "",
            (
                "libqt5svg5",
                "qml-module-qtquick2",
                "qml-module-qtquick-controls2",
                "qml-module-qtquick-layouts",
                "qml-module-qtquick-shapes",
            ),
        ),
        (1, "", "ImportError: QtQml", ("кодом 1", "ImportError: QtQml")),
        (-6, 'ASTRA_QT_PROBE={"svg":"ok","qml":"ok"}', "", ("кодом -6",)),
        (0, "мусор", "", ("нет корректного результата",)),
        (0, "", "", ("нет корректного результата",)),
        (0, "ASTRA_QT_PROBE={", "", ("нет корректного результата",)),
        (0, 'ASTRA_QT_PROBE={"svg":"ok"}', "", ("нет корректного результата",)),
        (0, "ASTRA_QT_PROBE=[]", "", ("нет корректного результата",)),
        (0, 'ASTRA_QT_PROBE={"svg":[],"qml":null}', "", ("нет корректного результата",)),
        (
            0,
            'ASTRA_QT_PROBE={"svg":"ok","qml":"неизвестно"}',
            "",
            ("нет корректного результата",),
        ),
        (
            0,
            'ASTRA_QT_PROBE={"svg":"ok","qml":"ok"}\n' * 2,
            "",
            ("нет корректного результата",),
        ),
        (None, "", "", ("таймаут",)),
        (None, 'ASTRA_QT_PROBE={"svg":"ok","qml":"ok"}', "", ("таймаут",)),
        (
            None,
            'ASTRA_QT_PROBE={"svg":"missing","qml":"error: QtObject is not a type"}',
            "",
            ("таймаут", "libqt5svg5", "qml-module-qtquick-controls2", "QtObject is not a type"),
        ),
    ],
)
def test_qt_environment_error(
    guard: ModuleType,
    returncode: int | None,
    stdout: str,
    stderr: str,
    expected: tuple[str, ...],
) -> None:
    error = guard.qt_environment_error(returncode, stdout, stderr)
    if not expected:
        assert error is None
    else:
        assert isinstance(error, str)
        assert error.endswith("пропуск здесь запрещён.")
        for fragment in expected:
            assert fragment in error


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("probe_error", [None, "нет поддержки SVG — нужен libqt5svg5"])
def test_qt_environment_probe_once_when_required(
    guard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    required: bool,
    available: bool,
    probe_error: str | None,
) -> None:
    monkeypatch.delenv("ASTRA_VOICE_REQUIRE_QT", raising=False)
    monkeypatch.setattr(guard, "qt_available", lambda: available)
    run_probe = Mock(return_value=probe_error)
    monkeypatch.setattr(guard, "run_qt_environment_probe", run_probe)
    config = Mock(stash=pytest.Stash(), rootpath=tmp_path)
    config.option.markexpr = "unit" if required else "engine"
    session = Mock(config=config)
    guard.pytest_sessionstart(session)
    # Повторный вызов не должен запускать Qt заново, в том числе после ошибки.
    for _ in range(2):
        if required and (not available or probe_error is not None):
            with pytest.raises(
                pytest.UsageError, match=probe_error if available else "python3-pyqt5"
            ):
                guard.pytest_collection(session)
        else:
            guard.pytest_collection(session)
    if required and available:
        run_probe.assert_called_once_with()
    else:
        run_probe.assert_not_called()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='ASTRA_QT_PROBE={"svg":"ok","qml":"ok"}',
                stderr="предупреждение Qt",
            ),
            None,
        ),
        (subprocess.TimeoutExpired(cmd="probe", timeout=20, stderr=b"Qt"), "таймаут"),
        (
            OSError("не удалось запустить Python"),
            "подпроцесс проверки Qt не запустился: не удалось запустить Python",
        ),
    ],
)
def test_qt_environment_probe_subprocess(
    guard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    outcome: subprocess.CompletedProcess[str] | Exception,
    expected: str | None,
) -> None:
    run = (
        Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
    )
    monkeypatch.setattr(guard.subprocess, "run", run)
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")
    monkeypatch.setenv("QT_QUICK_BACKEND", "opengl")
    error = guard.run_qt_environment_probe()
    if expected is None:
        assert error is None
    else:
        assert isinstance(error, str)
        assert expected in error
        assert error.endswith("пропуск здесь запрещён.")
        if isinstance(outcome, OSError):
            assert "завершился с кодом" not in error
            assert "таймаут" not in error
            assert "libqt5svg5" in error
            assert "qml-module-qtquick-controls2" in error
    run.assert_called_once()
    assert run.call_args.args == ([sys.executable, "-c", guard._QT_PROBE],)
    options = run.call_args.kwargs
    assert options["env"]["QT_QPA_PLATFORM"] == "offscreen"
    assert options["env"]["QT_QUICK_BACKEND"] == "software"
    assert options["capture_output"] is True
    assert options["text"] is True
    assert 0 < options["timeout"] <= 30
    assert os.environ["QT_QPA_PLATFORM"] == "xcb"
    assert os.environ["QT_QUICK_BACKEND"] == "opengl"


@pytest.mark.parametrize(
    ("stdout", "stdout_tail"),
    [
        (None, None),
        ("начало stdout\n" + "x" * 3000 + "\nхвост stdout", "хвост stdout"),
        (b"stdout start\n" + b"x" * 3000 + b"\nstdout tail\xff", "stdout tail\ufffd"),
    ],
)
@pytest.mark.parametrize(
    ("stderr", "stderr_tail"),
    [
        (None, None),
        ("начало stderr\n" + "y" * 3000 + "\nхвост stderr", "хвост stderr"),
        (b"stderr start\n" + b"y" * 3000 + b"\nstderr tail\xff", "stderr tail\ufffd"),
    ],
)
def test_qt_environment_probe_timeout_output(
    guard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str | bytes | None,
    stdout_tail: str | None,
    stderr: str | bytes | None,
    stderr_tail: str | None,
) -> None:
    run = Mock(
        side_effect=subprocess.TimeoutExpired(cmd="probe", timeout=20, output=stdout, stderr=stderr)
    )
    monkeypatch.setattr(guard.subprocess, "run", run)
    error = guard.run_qt_environment_probe()
    assert isinstance(error, str)
    assert "подпроцесс проверки Qt превысил таймаут" in error
    assert error.endswith("пропуск здесь запрещён.")
    assert "libqt5svg5" in error
    assert "qml-module-qtquick-controls2" in error
    for tail in (stdout_tail, stderr_tail):
        if tail is not None:
            assert tail in error
    for head in ("начало stdout", "stdout start", "начало stderr", "stderr start"):
        assert head not in error
    assert "x" * 2001 not in error
    assert "y" * 2001 not in error
    run.assert_called_once()


@pytest.mark.parametrize(
    ("expression", "require_env", "available"),
    [
        ("engine", False, False),
        ("", False, False),
        ("unit", False, False),
        ("xvfb", False, False),
        ("unit or xvfb", False, False),
        ("engine", True, False),
        ("unit", True, True),
    ],
)
def test_collection_in_subprocess(
    tmp_path: Path, expression: str, require_env: bool, available: bool
) -> None:
    """Настоящий сбор: Qt-модуль нельзя импортировать даже до фильтрации по -m."""
    (tmp_path / "conftest.py").write_text(
        GUARD_PATH.read_text(encoding="utf-8")
        + f"\ndef qt_available() -> bool:\n    return {available!r}\n"
        + "\ndef run_qt_environment_probe() -> str | None:\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\npythonpath = src\nmarkers =\n    engine\n    unit\n    xvfb\n",
        encoding="utf-8",
    )
    write_package(
        tmp_path,
        {"a.py": "from . import b", "b.py": "if False:\n    from PyQt5 import sip"},
    )
    (tmp_path / "test_transitive.py").write_text(
        "import astra_voice.a\n"
        + ("" if available else "raise AssertionError('Qt-модуль попал в сбор')\n")
        + "def test_transitive():\n    pass\n",
        encoding="utf-8",
    )
    for directory in ("unit", "xvfb"):
        target = tmp_path / directory
        target.mkdir()
        (target / f"test_qt_{directory}.py").write_text(
            "if False:\n    import PyQt5.QtWidgets\n"
            + ("" if available else "raise AssertionError('Qt-модуль попал в сбор')\n")
            + "def test_qt():\n    pass\n",
            encoding="utf-8",
        )
    (tmp_path / "test_plain.py").write_text(
        "import pytest\n"
        "# import PyQt5\n"
        "text = 'from PyQt5 import sip'\n"
        "@pytest.mark.engine\n@pytest.mark.unit\n@pytest.mark.xvfb\n"
        "def test_plain():\n    pass\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("ASTRA_VOICE_REQUIRE_QT", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    if require_env:
        env["ASTRA_VOICE_REQUIRE_QT"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-m", expression],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    required = require_env or expression in {"unit", "xvfb", "unit or xvfb"}
    if required and not available:
        assert result.returncode == pytest.ExitCode.USAGE_ERROR, output
        assert "python3-pyqt5" in output
        assert "пропуск здесь запрещён" in output
    else:
        assert result.returncode == pytest.ExitCode.OK, output
        assert "1 passed" in output
        if not available:
            assert "Исключено из сбора модулей: 3" in output
            assert "python3-pyqt5" in output
            assert "test_transitive.py: недоступен PyQt5" in output
        else:
            assert "Исключено из сбора" not in output
    assert "Qt-модуль попал в сбор" not in output


@pytest.mark.parametrize(
    ("requirement", "available"),
    [("", False), ("", True), ("unit", True), ("xvfb", True), ("env", True)],
)
@pytest.mark.parametrize(
    ("failure", "qt_missing"),
    [
        ("raise ModuleNotFoundError('PyQt5')", True),
        ("raise ModuleNotFoundError('missing Qt', name='PyQt5')", True),
        ("raise ModuleNotFoundError('missing Qt', name='PyQt5.QtWidgets')", True),
        ("raise ModuleNotFoundError('PyQt5', name='another_package')", False),
        ("raise ModuleNotFoundError('missing', name='PyQt5_extra')", False),
        ("raise ImportError('PyQt5')", False),
    ],
)
def test_collection_fallback(
    tmp_path: Path, requirement: str, available: bool, failure: str, qt_missing: bool
) -> None:
    # Qt формально доступен: обязательный режим должен сохранить именно ошибку
    # сбора, а не закончиться ранней UsageError из pytest_collection.
    (tmp_path / "conftest.py").write_text(
        GUARD_PATH.read_text(encoding="utf-8")
        + f"\ndef qt_available() -> bool:\n    return {available!r}\n"
        + "\ndef run_qt_environment_probe() -> str | None:\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    unit\n    xvfb\n", encoding="utf-8"
    )
    (tmp_path / "test_missed.py").write_text(failure + "\n", encoding="utf-8")
    (tmp_path / "test_plain.py").write_text(
        "import pytest\n@pytest.mark.unit\n@pytest.mark.xvfb\ndef test_plain():\n    pass\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("ASTRA_VOICE_REQUIRE_QT", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    if requirement == "env":
        env["ASTRA_VOICE_REQUIRE_QT"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-m", "" if requirement == "env" else requirement],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    if qt_missing and not requirement:
        assert result.returncode == pytest.ExitCode.OK, output
        assert "1 passed, 1 skipped" in output
        assert "Исключено из сбора модулей: 1" in output
        assert "test_missed.py: недоступен PyQt5" in output
        assert "модуль исключён, детект промахнулся — сообщите разработчику" in output
    else:
        assert result.returncode == pytest.ExitCode.INTERRUPTED, output
        assert "ERROR test_missed.py" in output
        assert "Исключено из сбора" not in output
