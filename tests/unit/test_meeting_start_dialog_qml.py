"""W4 consumes the settings Qt property contract without opening a window."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]


def test_start_dialog_observes_locked_settings_without_qml_warnings() -> None:
    # A separate offscreen process keeps QML engine teardown out of unrelated Qt tests.
    # This fake deliberately has only properties, matching FakeSettings/SettingsBridge.
    probe = """
import sys
from pathlib import Path
from PyQt5.QtCore import QObject, QUrl, pyqtProperty, pyqtSignal
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtQml import QQmlApplicationEngine
sys.path.insert(0, str(Path(sys.argv[1]) / 'src'))
from astra_voice.ui.icons import install_icon_provider

class Settings(QObject):
    changed = pyqtSignal()
    locks = []
    device = pyqtProperty(str, lambda self: '', constant=True)
    devices = pyqtProperty('QVariantList', lambda self: [], constant=True)
    lockedSettings = pyqtProperty('QStringList', lambda self: self.locks, notify=changed)

app = QGuiApplication([])
settings = Settings()
engine = QQmlApplicationEngine()
install_icon_provider(engine)
warnings = []
engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
engine.rootContext().setContextProperty('settingsBridge', settings)
engine.load(QUrl.fromLocalFile(str(Path(sys.argv[1]) / 'qml/StartDialog.qml')))
assert engine.rootObjects(), warnings
window = engine.rootObjects()[0]
assert not window.property('visible')
selector = window.findChild(QObject, 'meetingStartMicrophone')
assert selector is not None
for locks, expected in [([], True), (['device'], False), (['other'], True), ([], True)]:
    settings.locks = locks
    settings.changed.emit()
    app.processEvents()
    assert selector.property('enabled') == expected, (locks, warnings)
window.setProperty('meeting', {
    'startTexts': {}, 'startOptions': [], 'startWarnings': [], 'startState': 'starting',
    'startError': '', 'recordingAvailable': False, 'startDialogOpen': False,
    'selectedSource': 'room', 'startSpeakers': False, 'startCanStart': False,
})
app.processEvents()
assert not selector.property('enabled'), warnings
assert not warnings, warnings
"""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
    for key in ("DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", probe, str(REPO)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
