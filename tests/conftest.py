"""Общая изоляция звука и страж сбора: unit/xvfb без Qt обязаны падать."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping, Set
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from _pytest.nodes import Node
from _pytest.terminal import TerminalReporter

_QT_AVAILABLE = pytest.StashKey[bool]()
_QT_ENV_ERROR = pytest.StashKey[str | None]()
_IGNORED = pytest.StashKey[dict[Path, str]]()
_QT_MODULES = pytest.StashKey[Set[str]]()
_TEST_NEEDS_QT = pytest.StashKey[dict[Path, bool]]()
_PULSE_DIRECTORY = pytest.StashKey[TemporaryDirectory[str]]()
_PULSE_ORIGINAL_ENV = pytest.StashKey[dict[str, str | None]]()
_PULSE_ENV = pytest.StashKey[dict[str, str]]()
_PULSE_PROCESSES = pytest.StashKey[set[tuple[int, int]] | None]()
_PULSE_TEST_PROCESSES = pytest.StashKey[set[tuple[int, int]] | None]()
_QT_REASON = "недоступен PyQt5/QtWidgets (нужен python3-pyqt5)"
_QT_MISSED = "модуль исключён, детект промахнулся — сообщите разработчику"
_QT_ERROR = (
    "Для выбранных тестов нужен пакет python3-pyqt5 (PyQt5 и PyQt5.QtWidgets). "
    "Qt обязателен: выбран маркер unit/xvfb или задана ASTRA_VOICE_REQUIRE_QT=1; "
    "пропуск здесь запрещён."
)
_QML_PACKAGES = (
    "qml-module-qtquick2 qml-module-qtquick-controls2 "
    "qml-module-qtquick-layouts qml-module-qtquick-shapes"
)
# QApplication в процессе pytest нарушает изоляцию тестов настоящего QML.
_QT_PROBE = """
import json
from PyQt5.QtCore import QUrl
from PyQt5.QtGui import QGuiApplication, QImageReader
from PyQt5.QtQml import QQmlComponent, QQmlEngine

app = QGuiApplication([])
svg = 'ok' if b'svg' in QImageReader.supportedImageFormats() else 'missing'
engine = QQmlEngine()
component = QQmlComponent(engine)
component.setData(
    b'import QtQuick 2.15; import QtQuick.Controls 2.15; '
    b'import QtQuick.Layouts 1.15; import QtQuick.Shapes 1.15; QtObject {}',
    QUrl(),
)
root = component.create()
errors = [error.toString() for error in component.errors()]
qml = (
    'error: ' + ('; '.join(errors) or 'не удалось создать QtObject')
    if errors or component.status() == QQmlComponent.Error or root is None
    else 'ok'
)
print('ASTRA_QT_PROBE=' + json.dumps({'svg': svg, 'qml': qml}), flush=True)
"""


def _read_proc_file(path: Path) -> str:
    """Читаем через os, а не через Path.open: тесты подменяют его у себя.

    Сторож работает в setup и teardown каждого теста, в том числе пока чужой
    monkeypatch ещё не откатился (`test_models_downloader` подменяет
    `Path.open` своей сигнатурой). Низкоуровневые вызовы такие подмены не
    задевают, поэтому оснастка не падает из-за них и не рушит весь прогон.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        chunks: list[bytes] = []
        while block := os.read(fd, 4096):
            chunks.append(block)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8", errors="replace")


def pulseaudio_processes(proc_root: Path = Path("/proc")) -> set[tuple[int, int]] | None:
    """Читаем только /proc; время старта защищает от переиспользования pid.

    Сторож обязан деградировать в «не знаю» (None), а не ронять чужой тест:
    он работает и тогда, когда тест ещё держит свои подмены (`test_models_store`
    подменяет `os.open` моком, который бросает `AssertionError`). Поэтому
    наружу не выпускаем ничего, кроме отсутствия ответа.
    """
    try:
        return _scan_pulseaudio_processes(proc_root)
    except Exception:
        return None


def _scan_pulseaudio_processes(proc_root: Path) -> set[tuple[int, int]] | None:
    try:
        entries = os.listdir(proc_root)
    except (OSError, ValueError):
        # На системах без доступного /proc сторож не работает.
        return None
    processes: set[tuple[int, int]] = set()
    for name in entries:
        if not name.isdecimal():
            continue
        try:
            directory = proc_root / name
            if _read_proc_file(directory / "comm").rstrip("\n") != "pulseaudio":
                continue
            stat = _read_proc_file(directory / "stat")
            # После последней скобки идёт поле 3; starttime (поле 22) имеет индекс 19.
            # Имя внутри скобок само может содержать пробелы и скобки.
            _, closing, tail = stat.rpartition(")")
            fields = tail.split()
            if not closing or len(fields) < 20:
                continue
            processes.add((int(name), int(fields[19])))
        except (OSError, ValueError):
            # Процесс мог исчезнуть или стать недоступным между чтениями.
            continue
    return processes


def check_pulseaudio_processes(
    before: set[tuple[int, int]] | None,
    after: set[tuple[int, int]] | None,
    *,
    nodeid: str | None = None,
) -> None:
    """Новые процессы требуют проверки изоляции; их происхождение нам неизвестно."""
    if before is None or after is None:
        return
    appeared = after - before
    if appeared:
        pids = ", ".join(str(pid) for pid, _ in sorted(appeared))
        where = f" Во время теста {nodeid}." if nodeid is not None else " За время сессии."
        pytest.fail(
            f"Появилось новых процессов pulseaudio: {len(appeared)}; pid: {pids}."
            f"{where} Процесс мог быть запущен пользователем или другой программой. "
            "Проверьте изоляцию звука в тесте; "
            "см. PULSE_CLIENTCONFIG/PULSE_SERVER.",
            pytrace=False,
        )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Изолируем всех потомков ещё до сбора тестов и подпроцесса проверки Qt."""
    directory = TemporaryDirectory(prefix="astra-tests-pulse-")
    config.stash[_PULSE_DIRECTORY] = directory
    config.stash[_PULSE_ORIGINAL_ENV] = {
        name: os.environ.get(name) for name in ("PULSE_CLIENTCONFIG", "PULSE_SERVER")
    }
    root = Path(directory.name)
    client_config = root / "client.conf"
    client_config.write_text("autospawn = no\n", encoding="utf-8")
    pulse_env = {
        "PULSE_CLIENTCONFIG": str(client_config),
        "PULSE_SERVER": f"unix:{root / 'no-pulse-server'}",
    }
    config.stash[_PULSE_ENV] = pulse_env
    os.environ.update(pulse_env)
    config.stash[_PULSE_PROCESSES] = pulseaudio_processes()


def pytest_unconfigure(config: pytest.Config) -> None:
    """Возвращаем окружение вызывающей стороны и удаляем временный каталог."""
    directory = config.stash.get(_PULSE_DIRECTORY, None)
    if directory is None:
        return
    try:
        for name, value in config.stash.get(_PULSE_ORIGINAL_ENV, {}).items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    finally:
        directory.cleanup()
        del config.stash[_PULSE_DIRECTORY]


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    # Session-фикстура запускается один раз; исправляем также изменения прошлых тестов.
    pulse_env = item.config.stash.get(_PULSE_ENV, {})
    os.environ.update(pulse_env)
    item.stash[_PULSE_TEST_PROCESSES] = pulseaudio_processes()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item) -> Iterator[None]:
    # Снимок до финализаторов ловит процесс, который они успеют убрать; пока
    # живы подмены самого теста, обход /proc отдаёт «не знаю» — тогда работает
    # только снимок после финализаторов, и прогон из-за этого не падает.
    after_call = pulseaudio_processes()
    try:
        yield
    finally:
        # Снимок после финализаторов ловит также процессы из самого teardown.
        after_teardown = pulseaudio_processes()
        after = (
            None
            if after_call is None and after_teardown is None
            else (after_call or set()) | (after_teardown or set())
        )
        check_pulseaudio_processes(
            item.stash.get(_PULSE_TEST_PROCESSES, None), after, nodeid=item.nodeid
        )


@pytest.fixture(scope="session", autouse=True)
def audio_isolation(pytestconfig: pytest.Config) -> Iterator[None]:
    """Восстанавливаем защиту после сбора и проверяем процессы после всех тестов."""
    pulse_env = pytestconfig.stash.get(_PULSE_ENV, {})
    os.environ.update(pulse_env)
    try:
        yield
    finally:
        os.environ.update(pulse_env)
        check_pulseaudio_processes(
            pytestconfig.stash.get(_PULSE_PROCESSES, None), pulseaudio_processes()
        )


def qt_environment_error(
    returncode: int | None,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    *,
    start_error: str | None = None,
) -> str | None:
    """Разбираем только помеченный результат; предупреждения Qt не часть протокола.

    Без ошибки запуска None вместо кода означает таймаут, даже при готовом результате.
    """

    def output_text(output: str | bytes | None) -> str:
        # При таймауте возможны bytes даже с text=True; диагностика не должна падать.
        return output.decode(errors="replace") if isinstance(output, bytes) else output or ""

    stdout = output_text(stdout)
    stderr = output_text(stderr)
    records = [
        line.removeprefix("ASTRA_QT_PROBE=")
        for line in stdout.splitlines()
        if line.startswith("ASTRA_QT_PROBE=")
    ]
    result: object = None
    if len(records) == 1:
        try:
            result = json.loads(records[0])
        except ValueError:
            pass
    valid = (
        isinstance(result, dict)
        and result.get("svg") in ("ok", "missing")
        and isinstance(result.get("qml"), str)
        and (result["qml"] == "ok" or result["qml"].startswith("error: "))
    )
    problems: list[str] = []
    if start_error is not None:
        problems.append(f"подпроцесс проверки Qt не запустился: {start_error}")
    elif returncode is None:
        problems.append("подпроцесс проверки Qt превысил таймаут")
    elif returncode != 0:
        problems.append(f"подпроцесс проверки Qt завершился с кодом {returncode}")
    if not valid:
        problems.append(
            "нет корректного результата проверки SVG/QML; проверьте пакеты Debian: "
            f"libqt5svg5 {_QML_PACKAGES}"
        )
    elif isinstance(result, dict):
        if result["svg"] == "missing":
            problems.append("нет поддержки SVG — нужен пакет Debian libqt5svg5")
        if result["qml"] != "ok":
            problems.append(f"ошибка QML — нужны пакеты Debian {_QML_PACKAGES}: {result['qml']}")
    if not problems:
        return None
    details = "".join(
        f" Диагностика подпроцесса ({name}): {output.strip()[-2000:]}"
        for name, output in (("stdout", stdout), ("stderr", stderr))
        if output.strip()
    )
    return (
        "Для выбранных тестов нужно рабочее окружение Qt: "
        + "; ".join(problems)
        + "."
        + details
        + " "
        "Qt обязателен: выбран маркер unit/xvfb или задана ASTRA_VOICE_REQUIRE_QT=1; "
        "пропуск здесь запрещён."
    )


def run_qt_environment_probe() -> str | None:
    """Проверяем плагины отдельно, чтобы сбой Qt не уронил сам pytest."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", _QT_PROBE],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
            timeout=20,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as error:
        # str(TimeoutExpired) содержит весь код пробы и заглушает причину сбоя.
        return qt_environment_error(None, error.stdout, error.stderr)
    except OSError as error:
        return qt_environment_error(None, "", "", start_error=str(error))
    return qt_environment_error(result.returncode, result.stdout, result.stderr)


def import_names(text: str, package: str = "") -> set[str]:
    """Импорты на любой глубине AST; from может ссылаться и на подмодуль."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Ошибку исходника должен показать обычный сбор pytest, не скрываем её.
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                if not package:
                    continue
                try:
                    module = importlib.util.resolve_name("." * node.level + module, package)
                except ImportError:
                    continue
            if module:
                names.add(module)
                names.update(f"{module}.{alias.name}" for alias in node.names if alias.name != "*")
    return names


def import_prefixes(names: Set[str]) -> set[str]:
    """Импорт подмодуля также исполняет __init__.py всех родительских пакетов."""
    return {
        ".".join(parts[:end])
        for name in names
        for parts in [name.split(".")]
        for end in range(1, len(parts) + 1)
    }


def qt_modules(package_path: Path) -> set[str]:
    """Строим граф исходников без импорта и распространяем Qt по обратным рёбрам."""
    imports: dict[str, set[str]] = {}
    for path in package_path.rglob("*.py"):
        parts = path.relative_to(package_path.parent).with_suffix("").parts
        is_package = parts[-1] == "__init__"
        module = ".".join(parts[:-1] if is_package else parts)
        package = module if is_package else module.rpartition(".")[0]
        imports[module] = import_prefixes(
            import_names(path.read_text(encoding="utf-8"), package) | {package}
        )

    required = {module for module, names in imports.items() if "PyQt5" in names}
    dependents: dict[str, set[str]] = {module: set() for module in imports}
    for module, names in imports.items():
        for dependency in names & imports.keys():
            dependents[dependency].add(module)
    pending = list(required)
    while pending:
        for module in dependents[pending.pop()] - required:
            required.add(module)
            pending.append(module)
    return required


def needs_qt(text: str, required_modules: Set[str] = frozenset()) -> bool:
    """Тест требует Qt напрямую либо через модуль пакета (включая его родителей)."""
    names = import_prefixes(import_names(text))
    return "PyQt5" in names or bool(names & required_modules)


def qt_required(marker_expression: str, env: Mapping[str, str]) -> bool:
    """Qt обязателен по env или положительному вхождению unit/xvfb в -m.

    Учитываем отрицания, включая скобки и двойное not. Синтаксис выражения
    проверяет сам pytest; имена маркеров сравниваем целиком, без подстрок.
    """
    if env.get("ASTRA_VOICE_REQUIRE_QT") == "1":
        return True
    negations = [False]
    negate = False
    for token in re.findall(r"\(|\)|[^\s()]+", marker_expression):
        if token == "not":
            negate = not negate
        elif token == "(":
            negations.append(negations[-1] ^ negate)
            negate = False
        elif token == ")":
            if len(negations) > 1:
                negations.pop()
            negate = False
        else:
            if token in {"unit", "xvfb"} and not (negations[-1] ^ negate):
                return True
            negate = False
    return False


def qt_available() -> bool:
    """find_spec подмодуля может бросить ImportError, если родителя нет."""
    try:
        return (
            importlib.util.find_spec("PyQt5") is not None
            and importlib.util.find_spec("PyQt5.QtWidgets") is not None
        )
    except (ImportError, ValueError):
        return False


def pytest_sessionstart(session: pytest.Session) -> None:
    config = session.config
    config.stash[_QT_AVAILABLE] = qt_available()
    config.stash[_IGNORED] = {}
    config.stash[_TEST_NEEDS_QT] = {}
    # При наличии Qt анализ вообще не нужен; иначе граф вычисляется один раз за сессию.
    config.stash[_QT_MODULES] = (
        set()
        if config.stash[_QT_AVAILABLE]
        else qt_modules(config.rootpath / "src" / "astra_voice")
    )


def pytest_collection(session: pytest.Session) -> None:
    config = session.config
    if not qt_required(config.option.markexpr, os.environ):
        return
    # Проверяем до обхода файлов: даже явный путь к тесту не обойдёт запрет.
    if not config.stash[_QT_AVAILABLE]:
        raise pytest.UsageError(_QT_ERROR)
    if _QT_ENV_ERROR not in config.stash:
        config.stash[_QT_ENV_ERROR] = run_qt_environment_probe()
    error = config.stash[_QT_ENV_ERROR]
    if error is not None:
        raise pytest.UsageError(error)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    if config.stash[_QT_AVAILABLE]:
        return None
    if collection_path.suffix != ".py" or not collection_path.is_file():
        return None
    patterns = config.getini("python_files")
    if not any(collection_path.match(pattern) for pattern in patterns):
        return None
    cache = config.stash[_TEST_NEEDS_QT]
    if collection_path not in cache:
        cache[collection_path] = needs_qt(
            collection_path.read_text(encoding="utf-8"), config.stash[_QT_MODULES]
        )
    if cache[collection_path]:
        config.stash[_IGNORED][collection_path] = _QT_REASON
        return True
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_exception_interact(
    node: Node,
    call: pytest.CallInfo[object],
    report: pytest.CollectReport | pytest.TestReport,
) -> None:
    """До публикации отчёта сбора превращаем пропущенный детектом Qt в skip."""
    if (
        not isinstance(node, pytest.Module)
        or not isinstance(report, pytest.CollectReport)
        or not report.failed
        or call.excinfo is None
        or qt_required(node.config.option.markexpr, os.environ)
    ):
        return
    # pytest оборачивает ImportError в CollectError; смотрим исходное исключение,
    # а не текст traceback, в котором PyQt5 может встретиться случайно.
    error: BaseException | None = call.excinfo.value
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, ModuleNotFoundError) and (
            (error.name is not None and error.name.split(".")[0] == "PyQt5")
            or (error.name is None and error.args == ("PyQt5",))
        ):
            reason = f"{_QT_REASON}; {_QT_MISSED}"
            node.config.stash[_IGNORED][node.path] = reason
            report.outcome = "skipped"
            report.longrepr = (str(node.path), 0, reason)
            report.result = []
            return
        error = error.__cause__ or error.__context__


def pytest_terminal_summary(terminalreporter: TerminalReporter, config: pytest.Config) -> None:
    ignored = config.stash[_IGNORED]
    if ignored:
        terminalreporter.write_sep(
            "=",
            f"Исключено из сбора модулей: {len(ignored)} — "
            f"{_QT_REASON}; Qt не обязателен в этом запуске",
        )
        for path, reason in sorted(ignored.items()):
            terminalreporter.write_line(f"{os.path.relpath(path, config.rootpath)}: {reason}")
