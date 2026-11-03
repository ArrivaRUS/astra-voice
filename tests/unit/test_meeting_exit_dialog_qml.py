"""T-478 exit dialog in the real Main.qml, offscreen subprocess with the W4 fixture bridge."""

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
from PyQt5.QtCore import QObject, Qt, QUrl
from PyQt5.QtGui import QFontDatabase, QGuiApplication
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtQuick import QQuickWindow
from PyQt5.QtTest import QTest
repo = Path(sys.argv[1])
sys.path.insert(0, str(repo / 'src'))
sys.path.insert(0, str(repo / 'tests'))
sys.path.insert(0, str(repo / 'tests/xvfb'))
from astra_voice.ui.icons import install_icon_provider
from astra_voice.ui.meeting_texts import EXIT_TEXTS
# Fixture classes only; no xvfb test or X server runs here.
from test_meetings_section import ThemeSource
from test_start_dialog import RecordingBridge
QQuickWindow.setSceneGraphBackend('software')
app = QGuiApplication([])
for path in (repo / 'tests/fixtures/fonts/pt-root-ui').glob('*.ttf'):
    assert QFontDatabase.addApplicationFont(str(path)) >= 0
engine = QQmlApplicationEngine()
warnings = []
engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
bridge = RecordingBridge()
bridge.open = False
theme = ThemeSource(False)
engine.rootContext().setContextProperty('themeSource', theme)
engine.rootContext().setContextProperty('meetingBridge', bridge)
install_icon_provider(engine)
engine.load(QUrl.fromLocalFile(str(repo / 'qml/Main.qml')))
assert engine.rootObjects(), warnings
window = engine.rootObjects()[0]
window.hide()
dialog = window.findChild(QObject, 'meetingExitDialog')
assert dialog is not None and not dialog.property('visible')

def ask():
    bridge.exit_confirm = True
    bridge.changed.emit()
    QTest.qWait(50)
    assert dialog.property('visible') and window.isVisible()

def texts():
    return [item.property('text') for item in dialog.findChildren(QObject)
            if item.property('text') and item.metaObject().className().startswith('QQuickText')]

ask()
shown = texts()
for key in ('header', 'title', 'text', 'cancel', 'confirm'):
    assert EXIT_TEXTS[key] in shown, (key, shown)
cancel = dialog.findChild(QObject, 'dialogCancel')
confirm = dialog.findChild(QObject, 'dialogConfirm')
assert cancel.property('activeFocus'), 'focus must start on «Отмена»'
QTest.keyClick(window, Qt.Key_Return)
QTest.qWait(30)
assert bridge.exit_answers == ['cancel'] and not dialog.property('visible')
ask()
QTest.keyClick(window, Qt.Key_Escape)
QTest.qWait(30)
assert bridge.exit_answers == ['cancel', 'cancel']
ask()
confirm.clicked.emit()
QTest.qWait(30)
assert bridge.exit_answers[-1] == 'confirm' and not dialog.property('visible')
# The recording ends by itself while asking: closes without an answer.
ask()
bridge.exit_confirm = False
bridge.changed.emit()
QTest.qWait(50)
assert not dialog.property('visible')
assert bridge.exit_answers == ['cancel', 'cancel', 'confirm']
assert dialog.property('width') == 420
# Review 3/3: closing the window with the question open is «Отмена»; «Выход» asks again.
ask()
window.hide()
QTest.qWait(50)
assert bridge.exit_answers[-1] == 'cancel' and not bridge.exit_confirm
ask()
assert not warnings, warnings
window.hide()
from PyQt5 import sip
sip.delete(engine)
assert not warnings, warnings
"""


def test_exit_dialog_in_main_qml_without_warnings() -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
    env["QT_SCALE_FACTOR"] = "1"
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(REPO)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
