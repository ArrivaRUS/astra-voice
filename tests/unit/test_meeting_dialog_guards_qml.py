"""Real meeting dialogs tolerate the older section bridge; offscreen Main.qml."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]

QML_PROBE = r"""
import sys
from pathlib import Path
from PyQt5 import sip
from PyQt5.QtCore import QMetaObject, QObject, QUrl
from PyQt5.QtGui import QFontDatabase, QGuiApplication
from PyQt5.QtQml import QQmlApplicationEngine, QJSValue
from PyQt5.QtQuick import QQuickWindow
from PyQt5.QtTest import QTest

repo, component, object_name = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.path.insert(0, str(repo / 'src'))
sys.path.insert(0, str(repo / 'tests'))
sys.path.insert(0, str(repo / 'tests/xvfb'))
from astra_voice.ui.icons import install_icon_provider
from test_meetings_section import Bridge, ThemeSource

QQuickWindow.setSceneGraphBackend('software')
app = QGuiApplication([])
for path in (repo / 'tests/fixtures/fonts/pt-root-ui').glob('*.ttf'):
    assert QFontDatabase.addApplicationFont(str(path)) >= 0

# Reuse the unchanged old section bridge that originally exposed the regression.
# Do not populate new properties: their absence is the compatibility contract.
bridge, theme = Bridge('success'), ThemeSource(False)
missing = ('saveTemplates', 'saveTemplate', 'savePreview', 'card', 'cardTexts',
           'cardSaveError', 'cardReplaceFile', 'renameTexts', 'deleteTexts')
assert all(bridge.metaObject().indexOfProperty(name) == -1 for name in missing)
assert not hasattr(bridge, 'savePreview')
engine = QQmlApplicationEngine()
warnings = []
engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
install_icon_provider(engine)
engine.rootContext().setContextProperty('themeSource', theme)
engine.rootContext().setContextProperty('meetingBridge', bridge)
engine.load(QUrl.fromLocalFile(str(repo / 'qml/Main.qml')))
assert engine.rootObjects(), warnings
window = engine.rootObjects()[0]
window.show()
sidebar = window.findChild(QObject, 'settingsSidebar')
assert sidebar is not None
assert sidebar.setProperty('currentIndex', 3)
section = None
for _ in range(100):
    QTest.qWait(20)
    section = window.findChild(QObject, 'meetingsSection')
    if section is not None:
        break
assert section is not None, warnings
assert section.property('visible') is True
dialog = section.findChild(QObject, object_name)
assert dialog is not None, object_name
assert QMetaObject.invokeMethod(dialog, 'open') is None
for _ in range(100):
    QTest.qWait(20)
    if dialog.property('opened'):
        break
assert window.isVisible()
assert dialog.property('visible') is True
assert dialog.property('opened') is True
assert not warnings, warnings

if component == 'MeetingSaveDialog':
    def variant(value):
        return value.toVariant() if isinstance(value, QJSValue) else value

    assert variant(dialog.property('templates')) == []
    preview = variant(dialog.property('preview'))
    assert isinstance(preview, dict), preview
    assert not preview.get('kind') and not preview.get('rows'), preview

QMetaObject.invokeMethod(dialog, 'close')
QTest.qWait(30)
window.hide()
sip.delete(engine)
assert not warnings, warnings
print('ok')
"""


@pytest.mark.parametrize(
    ("component", "object_name"),
    [
        ("MeetingSaveDialog", "meetingSaveDialog"),
        ("MeetingRenameDialog", "meetingRenameDialog"),
        ("MeetingDeleteDialog", "meetingDeleteDialog"),
    ],
    ids=["save", "rename", "delete"],
)
def test_meeting_dialog_opens_without_optional_bridge_api(component: str, object_name: str) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "QT_ACCESSIBILITY",
            "AT_SPI_BUS_ADDRESS",
            "XDG_RUNTIME_DIR",
        }
    }
    env.update(
        QT_QPA_PLATFORM="offscreen",
        QT_QUICK_BACKEND="software",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
    )
    result = subprocess.run(
        [sys.executable, "-c", QML_PROBE, str(REPO), component, object_name],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok")
