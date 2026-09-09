"""Каждый QML грузится без единого предупреждения.

Гейт M1: `qml/**/*.qml` компилируется и создаётся, `Main.qml` поднимается целиком
в `QQmlApplicationEngine` — перехваченный поток сообщений Qt должен остаться пустым.
Предупреждение вида «Cannot specify anchors for items inside Row» или несуществующее
свойство здесь именно ловится, а не тонет в консоли.

Запуск: `xvfb-run -a pytest -m xvfb` либо `QT_QPA_PLATFORM=offscreen pytest -m xvfb`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
QML_DIR = REPO / "qml"
QML_FILES = sorted(QML_DIR.rglob("*.qml"))

pytestmark = pytest.mark.xvfb

# Стиль контролов: Fly навязывает свой — Default обязателен (plans.md M1, Fly 06-fly-misc-env).
os.environ["QT_QUICK_CONTROLS_STYLE"] = "Default"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qt() -> Iterator[dict[str, Any]]:
    """Приложение Qt + перехват сообщений. Без QtQuick тест не имеет смысла — skip."""
    pytest.importorskip("PyQt5.QtQuick", reason="нужен python3-pyqt5.qtquick")
    from PyQt5.QtCore import QUrl, qInstallMessageHandler
    from PyQt5.QtGui import QGuiApplication
    from PyQt5.QtQml import QQmlEngine

    messages: list[str] = []

    def handler(_mode: Any, _context: Any, text: str) -> None:
        messages.append(text)

    previous = qInstallMessageHandler(handler)
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlEngine()
    try:
        yield {"app": app, "engine": engine, "messages": messages, "url": QUrl.fromLocalFile}
    finally:
        qInstallMessageHandler(previous)


def test_qml_tree_is_not_empty() -> None:
    assert QML_FILES, "не найдено ни одного qml/**/*.qml"


@pytest.mark.parametrize("qml_file", QML_FILES, ids=lambda p: str(p.relative_to(QML_DIR)))
def test_qml_compiles_and_instantiates_without_warnings(qml_file: Path, qt: dict[str, Any]) -> None:
    from PyQt5.QtQml import QQmlComponent

    messages: list[str] = qt["messages"]
    start = len(messages)

    component = QQmlComponent(qt["engine"], qt["url"](str(qml_file)))
    errors = [error.toString() for error in component.errors()]
    assert not errors, f"{qml_file}: ошибки компиляции\n" + "\n".join(errors)
    assert component.status() == QQmlComponent.Ready, f"{qml_file}: статус {component.status()}"

    text = qml_file.read_text(encoding="utf-8")
    # Синглтон нельзя создать напрямую; Main.qml поднимается отдельным тестом целиком.
    if "pragma Singleton" not in text and qml_file.name != "Main.qml":
        assert component.create() is not None, f"{qml_file}: объект не создался"

    fresh = messages[start:]
    assert not fresh, f"{qml_file}: предупреждения QML\n" + "\n".join(fresh)


def test_main_window_loads_without_warnings(qt: dict[str, Any]) -> None:
    """Полная загрузка окна: сайдбар, раздел «Общие» и строка-статус вместе."""
    from PyQt5.QtCore import QTimer
    from PyQt5.QtQml import QQmlApplicationEngine

    messages: list[str] = qt["messages"]
    start = len(messages)

    engine = QQmlApplicationEngine()
    engine.load(qt["url"](str(QML_DIR / "Main.qml")))
    assert engine.rootObjects(), "Main.qml не создал корневой объект"

    # Даём биндингам и анимациям отработать — часть предупреждений всплывает не сразу.
    QTimer.singleShot(400, qt["app"].quit)
    qt["app"].exec_()

    fresh = messages[start:]
    assert not fresh, "предупреждения при загрузке Main.qml\n" + "\n".join(fresh)

    window = engine.rootObjects()[0]
    assert window.property("minimumWidth") == 900
    assert window.property("minimumHeight") == 620


def test_qmllint_is_clean_when_available() -> None:
    """qmllint — дополнительный гейт; на машине разработчика бинаря может не быть."""
    binary = shutil.which("qmllint")
    if binary is None or subprocess.run(
        [binary, "--help"], capture_output=True, text=True
    ).returncode != 0:
        pytest.skip("qmllint не установлен (в системе только симлинк qtchooser)")

    failures: list[str] = []
    for qml_file in QML_FILES:
        done = subprocess.run([binary, str(qml_file)], capture_output=True, text=True)
        if done.returncode != 0:
            failures.append(f"{qml_file}: {done.stdout.strip()} {done.stderr.strip()}")
    assert not failures, "\n".join(failures)
