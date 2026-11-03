"""Every extracted overlay component loads in a real engine with zero warnings."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]
PROBE = r"""
import sys
from pathlib import Path
from PyQt5.QtCore import QObject, QMetaObject, Qt, QUrl
from PyQt5.QtGui import QGuiApplication, QFontDatabase
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtTest import QTest
from PyQt5 import sip
repo = Path(sys.argv[1])
sys.path.insert(0, str(repo / 'src'))
from astra_voice.ui.icons import install_icon_provider
app = QGuiApplication([])
for path in (repo / 'tests/fixtures/fonts').rglob('*.ttf'):
    assert QFontDatabase.addApplicationFont(str(path)) >= 0
engine = QQmlApplicationEngine()
install_icon_provider(engine)
warnings = []
engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
kind = sys.argv[2]
body = {
    'OverlaySurface': 'surfaceWidth: 340; surfaceHeight: 64; surfaceRadius: 14',
    'MeetingOverlayButton': 'text: "Остановить"; iconName: "stop"; keyboardMode: true',
    'Pill': 'freezeAnimations: true',
}[kind]
qml = ('import QtQuick 2.15\nimport QtQuick.Window 2.15\nimport "qml"\n'
       'import "qml/components"\nWindow { width: 600; height: 200; '
       + kind + ' { objectName: "subject"; x: 18; y: 18; ' + body + ' } }')
engine.loadData(qml.encode(), QUrl.fromLocalFile(str(repo / '_overlay-unit.qml')))
assert engine.rootObjects(), warnings
root = engine.rootObjects()[0].findChild(QObject, 'subject')
assert root is not None
if kind == 'Pill':
    for state in ('hidden', 'loading-model', 'listening', 'listening-silent', 'limit',
                  'processing', 'done', 'clipboard-only', 'empty', 'cancelled', 'error',
                  'disabled', 'command-listening', 'command-delivering', 'command-done',
                  'command-failed', 'command-unknown'):
        root.setProperty('avState', state)
        QMetaObject.invokeMethod(root, 'forceLayout', Qt.DirectConnection)
        QTest.qWait(5)
        assert root.property('active') == (state not in ('hidden', 'disabled')), state
        assert root.property('pillHeight') == 36, state
elif kind == 'MeetingOverlayButton':
    assert root.property('activeFocusOnTab')
    root.setProperty('keyboardMode', False)
    assert not root.property('activeFocusOnTab')
    root.setProperty('enabled', False)
    root.setProperty('keyboardMode', True)
    assert not root.property('activeFocusOnTab')
else:
    assert root.property('surfaceWidth') == 340
    assert root.property('surfaceHeight') == 64
QTest.qWait(10)
assert not warnings, warnings
sip.delete(engine)
assert not warnings, warnings
"""


@pytest.mark.parametrize("kind", ["Pill", "OverlaySurface", "MeetingOverlayButton"])
def test_overlay_component_loads_without_qml_warnings(kind: str) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(REPO), kind],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
