"""SVG image provider and registration guard for every production QML engine."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PyQt5.QtCore import QSize
from PyQt5.QtGui import QColor, QImage

from astra_voice.ui.icons import PATHS, IconProvider, _render_icon

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("size", [16, 20])
@pytest.mark.parametrize("name", sorted(PATHS))
def test_every_icon_renders_ink(name: str, size: int) -> None:
    image, actual = IconProvider().requestImage(f"{name}?c=123456ff&w=1.5", QSize(size, size))
    assert actual == QSize(size, size)
    assert image.format() == QImage.Format_ARGB32_Premultiplied
    assert any(image.pixelColor(x, y).alpha() > 0 for x in range(size) for y in range(size))


@pytest.mark.parametrize(
    ("identifier", "requested", "expected"),
    [
        ("unknown?c=ff0000ff&w=1.5", QSize(16, 16), QSize(16, 16)),
        ("check?c=zz0000ff&w=1.5", QSize(16, 16), QSize(16, 16)),
        ("check?c=ff0000ff&x&w=1.5", QSize(16, 16), QSize(16, 16)),
        ("../?c=ff0000ff&w=1.5", QSize(16, 16), QSize(16, 16)),
        ("check?c=ff0000ff&w=0.4", QSize(16, 16), QSize(16, 16)),
        ("check?c=ff0000ff&w=4.1", QSize(16, 16), QSize(16, 16)),
        ("check?c=ff0000ff&w=NaN", QSize(16, 16), QSize(16, 16)),
        ("check?c=ff0000ff&w=inf", QSize(16, 16), QSize(16, 16)),
        ("check?c=ff0000ff&w=1.5", QSize(100000, 100000), QSize(256, 256)),
        ("check?c=ff0000ff&w=1.5", QSize(0, 0), QSize(16, 16)),
    ],
)
def test_invalid_request_is_blank_and_private(
    identifier: str, requested: QSize, expected: QSize, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="astra_voice.ui.icons"):
        image, actual = IconProvider().requestImage(identifier, requested)
    assert actual == expected
    assert image.size() == expected
    assert image.format() == QImage.Format_ARGB32_Premultiplied
    assert all(
        image.pixelColor(x, y).alpha() == 0
        for x in range(expected.width())
        for y in range(expected.height())
    )
    assert [record.message for record in caplog.records] == ["icon_request_invalid"]
    assert identifier not in caplog.text


def test_cache_hits_and_alpha() -> None:
    _render_icon.cache_clear()
    provider = IconProvider()
    first, _ = provider.requestImage("check?c=ff000080&w=1.5", QSize(16, 16))
    before = _render_icon.cache_info().hits
    second, _ = provider.requestImage("check?c=ff000080&w=1.5", QSize(16, 16))
    assert _render_icon.cache_info().hits == before + 1
    assert first == second
    original_pixel = second.pixelColor(0, 0)
    first.setPixelColor(0, 0, QColor("#00ff00"))
    third, _ = provider.requestImage("check?c=ff000080&w=1.5", QSize(16, 16))
    assert third.pixelColor(0, 0) == original_pixel
    opaque, _ = provider.requestImage("check?c=ff0000ff&w=1.5", QSize(16, 16))
    assert (
        abs(max(second.pixelColor(x, y).alpha() for x in range(16) for y in range(16)) - 128) <= 2
    )
    assert max(opaque.pixelColor(x, y).alpha() for x in range(16) for y in range(16)) == 255


def test_install_is_idempotent_and_survives_gc() -> None:
    source = """
import gc
from PyQt5.QtCore import QSize
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtQml import QQmlEngine
from astra_voice.ui.icons import PROVIDER_ID, install_icon_provider
app = QGuiApplication([])
engine = QQmlEngine()
install_icon_provider(engine)
provider = engine.imageProvider(PROVIDER_ID)
install_icon_provider(engine)
assert engine.imageProvider(PROVIDER_ID) is provider
other = QQmlEngine()
install_icon_provider(other)
assert other.imageProvider(PROVIDER_ID) is not provider
del provider
gc.collect()
image, size = engine.imageProvider(PROVIDER_ID).requestImage(
    'clock?c=112233ff&w=1.5', QSize(20, 20)
)
assert size == QSize(20, 20)
assert not image.isNull()
assert any(image.pixelColor(x, y).alpha() for x in range(20) for y in range(20))
"""
    _run_qt(source)


def _run_qt(source: str) -> str:
    env = os.environ.copy()
    env.update(
        PYTHONPATH=str(REPO / "src"), QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software"
    )
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.mark.parametrize("installed", [False, True])
def test_qml_reports_missing_provider_only_when_absent(installed: bool) -> None:
    source = """
import gc
import json
from pathlib import Path
from PyQt5.QtCore import QUrl, qInstallMessageHandler
from PyQt5.QtQuick import QQuickView
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from astra_voice.ui.icons import install_icon_provider

app = QApplication([])
messages = []
previous = qInstallMessageHandler(lambda _mode, _context, message: messages.append(message))
view = QQuickView()
if INSTALLED:
    install_icon_provider(view.engine())
gc.collect()
view.setSource(QUrl.fromLocalFile(str(Path('qml/components/Icon.qml').resolve())))
root = view.rootObject()
assert root is not None
root.setProperty('name', 'check')
view.show()
QTest.qWait(50)
root.setProperty('name', 'clock')
QTest.qWait(50)
qInstallMessageHandler(previous)
print(json.dumps(messages))
""".replace("INSTALLED", repr(installed))
    messages = json.loads(_run_qt(source))
    if installed:
        assert messages == []
    else:
        assert any("Invalid image provider" in message for message in messages)


def test_every_production_engine_installs_icon_provider() -> None:
    constructors = {"QQmlApplicationEngine", "QQmlEngine", "QQuickView"}
    failures: list[str] = []
    for path in (REPO / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def called_name(node: ast.AST) -> str | None:
            if not isinstance(node, ast.Call):
                return None
            if isinstance(node.func, ast.Name):
                return node.func.id
            if isinstance(node.func, ast.Attribute):
                return node.func.attr
            return None

        calls = [name for node in ast.walk(tree) if (name := called_name(node)) in constructors]
        if calls and not any(
            called_name(node) == "install_icon_provider" for node in ast.walk(tree)
        ):
            failures.append(str(path.relative_to(REPO)))
    assert not failures, f"QML engine without icon provider: {failures}"
