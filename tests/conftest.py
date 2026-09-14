"""Общий страж сбора: движку Qt не нужен, а unit/xvfb без него обязаны падать."""

from __future__ import annotations

import ast
import importlib.util
import os
import re
from collections.abc import Mapping, Set
from pathlib import Path

import pytest
from _pytest.nodes import Node
from _pytest.terminal import TerminalReporter

_QT_AVAILABLE = pytest.StashKey[bool]()
_IGNORED = pytest.StashKey[dict[Path, str]]()
_QT_MODULES = pytest.StashKey[Set[str]]()
_TEST_NEEDS_QT = pytest.StashKey[dict[Path, bool]]()
_QT_REASON = "недоступен PyQt5/QtWidgets (нужен python3-pyqt5)"
_QT_MISSED = "модуль исключён, детект промахнулся — сообщите разработчику"
_QT_ERROR = (
    "Для выбранных тестов нужен пакет python3-pyqt5 (PyQt5 и PyQt5.QtWidgets). "
    "Qt обязателен: выбран маркер unit/xvfb или задана ASTRA_VOICE_REQUIRE_QT=1; "
    "пропуск здесь запрещён."
)


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
    if not config.stash[_QT_AVAILABLE] and qt_required(config.option.markexpr, os.environ):
        # Проверяем до обхода файлов: даже явный путь к тесту не обойдёт запрет.
        raise pytest.UsageError(_QT_ERROR)


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
