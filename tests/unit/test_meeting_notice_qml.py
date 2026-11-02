"""W3 «Предупредите участников»: QML contract and native window, offscreen subprocesses.

Separate processes keep QML engine teardown out of unrelated Qt tests.
"""

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
from PyQt5.QtCore import QObject, QMetaObject, Qt, QUrl
from PyQt5.QtGui import QFontDatabase, QGuiApplication
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtQuick import QQuickWindow
from PyQt5.QtTest import QSignalSpy, QTest
repo = Path(sys.argv[1])
sys.path.insert(0, str(repo / 'src'))
from astra_voice.ui.icons import install_icon_provider
from astra_voice.ui.meeting_texts import W3_TEXTS, notice_mute_text
QQuickWindow.setSceneGraphBackend('software')
app = QGuiApplication([])
for path in (repo / 'tests/fixtures/fonts/pt-root-ui').glob('*.ttf'):
    assert QFontDatabase.addApplicationFont(str(path)) >= 0
engine = QQmlApplicationEngine()
install_icon_provider(engine)
warnings = []
engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
engine.loadData(b'''import QtQuick 2.15
import QtQuick.Window 2.15
import "qml"
Window { width: 420; height: 400; visible: true
    MeetingNotice { id: notice; x: 36; y: 36 } }''',
    QUrl.fromLocalFile(str(repo / '_probe-w3.qml')))
assert engine.rootObjects(), warnings
window = engine.rootObjects()[0]
root = window.findChild(QObject, 'meetingNotice')
root.setProperty('texts', dict(W3_TEXTS))
base = {'visible': True, 'text': 'Я веду запись и расшифровку этой встречи для протокола.',
        'admin': False, 'copyState': 'default', 'muteText': notice_mute_text('dion')}

def show(**changes):
    root.setProperty('presentation', {**base, **changes})
    QMetaObject.invokeMethod(root, 'forceLayout', Qt.DirectConnection)
    QTest.qWait(20)

def text_of(name):
    item = root.findChild(QObject, name)
    return item.property('text')

# tokens.md §5.3 «h 264.9 ЗАМЕР»; refs/04-recording: admin 266.7, copy-failed 283.0.
for changes, height, admin, label in [
    ({}, 264.9, False, W3_TEXTS['copy']),
    ({'copyState': 'copied'}, 264.9, False, W3_TEXTS['copied']),
    ({'text': 'Встреча записывается для протокола.', 'admin': True}, 266.7, True, W3_TEXTS['copy']),
    ({'copyState': 'failed'}, 283.0, False, W3_TEXTS['copyFailed']),
]:
    show(**changes)
    assert root.property('surfaceWidth') == 340
    actual = root.property('surfaceHeight')
    assert abs(actual - height) < 0.1, (changes, actual)
    assert root.findChild(QObject, 'meetingNoticeAdmin').property('visible') is admin
    assert text_of('meetingNoticeCopyText') == label
    assert text_of('meetingNoticeTitle') == W3_TEXTS['title']
    copy = root.findChild(QObject, 'meetingNoticeCopy')
    assert copy.property('width') <= 310
assert 'в DION на запись' in text_of('meetingNoticeMuteText')

# Untrusted administrator text is shown literally, never as markup.
raw = '<b>Запись</b> & <img src="file:///etc/passwd"> "1"\nвторая строка'
show(text=raw, admin=True)
edit = root.findChild(QObject, 'meetingNoticeText')
plain = edit.getText(0, edit.property('length'))
# <br> lays out as U+2028; everything else is the literal administrator text.
assert plain.replace(chr(0x2028), chr(10)) == raw, plain
assert edit.property('lineCount') >= 2

copied = QSignalSpy(root.copyClicked)
closed = QSignalSpy(root.closeClicked)
keyboard = QSignalSpy(root.keyboardRequested)
show()
root.setProperty('keyboardMode', True)
QMetaObject.invokeMethod(root, 'focusFirstControl', Qt.DirectConnection)
copy = root.findChild(QObject, 'meetingNoticeCopy')
assert copy.property('activeFocus')
QTest.keyClick(window, Qt.Key_Return)
assert len(copied) == 1
QTest.keyClick(window, Qt.Key_Tab)
assert root.findChild(QObject, 'meetingNoticeClose').property('activeFocus')
QTest.keyClick(window, Qt.Key_Space)
assert len(closed) == 1
QTest.keyClick(window, Qt.Key_Escape)
assert len(closed) == 2
# Selection outside keyboard mode asks for focus (manual Ctrl+C after copy-failed).
root.setProperty('keyboardMode', False)
edit.select(0, 3)
assert len(keyboard) == 1
# Review 3/4: manual Ctrl+C goes to the text/plain path, never TextEdit's rich copy.
manual = QSignalSpy(root.copySelectionRequested)
root.setProperty('keyboardMode', True)
QMetaObject.invokeMethod(root, 'focusText', Qt.DirectConnection)
edit.select(0, 3)
QTest.keyClick(window, Qt.Key_C, Qt.ControlModifier)
assert len(manual) == 1 and manual[0][0] == 'Я в', list(manual)
published = QGuiApplication.clipboard().mimeData()
formats = published.formats() if published is not None else []
assert 'text/html' not in formats, formats
assert not warnings, warnings
"""

NATIVE_PROBE = r"""
import sys
from pathlib import Path
from PyQt5.QtCore import QObject, QPoint, QRect, Qt, pyqtSignal
from PyQt5.QtGui import QCloseEvent, QFontDatabase, QGuiApplication, QRegion
from PyQt5.QtQuick import QQuickView, QQuickWindow
from PyQt5.QtTest import QTest
from PyQt5 import sip
repo = Path(sys.argv[1])
sys.path.insert(0, str(repo / 'src'))
from astra_voice.ui.meeting_notice import MeetingNotice
QQuickWindow.setSceneGraphBackend('software')
app = QGuiApplication([])
for path in (repo / 'tests/fixtures/fonts/pt-root-ui').glob('*.ttf'):
    assert QFontDatabase.addApplicationFont(str(path)) >= 0

class Recording(QObject):
    changed = pyqtSignal()
    participantsRequested = pyqtSignal()
    overlay = {'visible': False, 'active': False, 'state': 'hidden'}
    active_meeting_id = ''
    notice_app_class = 'dion'

class Overlay(QObject):
    geometryChanged = pyqtSignal()
    rect = None
    def surface_rect(self):
        return self.rect

warnings = []
copies = []
outcome = [True]

def make_view():
    view = QQuickView()
    view.engine().warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
    return view

def clipboard(text):
    copies.append(text)
    return outcome[0]

recording, overlay = Recording(), Overlay()
owner = MeetingNotice(recording, overlay, notice=lambda: ('Текст встречи', False),
                      clipboard=clipboard, view_factory=make_view)
view, root = owner._view, owner._view.rootObject()

def start(mid):
    recording.overlay = {'visible': True, 'active': True, 'state': 'recording'}
    recording.active_meeting_id = mid
    recording.changed.emit()
    QTest.qWait(30)

try:
    assert not view.isVisible() and not owner.is_open
    # Not recording: neither W2 «💬» nor the tray opens W3.
    recording.participantsRequested.emit()
    assert not view.isVisible()
    area = view.screen().availableGeometry()
    overlay.rect = QRect(area.x() + 300, area.y() + 12, 482, 36)
    start('m1')
    assert view.isVisible() and owner.is_open, 'W3 opens with every new recording'
    assert view.flags() & Qt.WindowDoesNotAcceptFocus
    assert view.flags() & Qt.WindowStaysOnTopHint
    body = QRect(view.x() + 36, view.y() + 36, 340, view.height() - 36 * 2 - 14)
    assert body.y() == overlay.rect.bottom() + 1 + 8, (body, overlay.rect)
    assert abs(body.center().x() - overlay.rect.center().x()) <= 1
    assert view.mask() == QRegion(36, 36, 340, view.height() - 86) or view.mask().isEmpty()
    # W2 dragged: W3 follows.
    overlay.rect = overlay.rect.translated(40, 100)
    overlay.geometryChanged.emit()
    assert view.y() + 36 == overlay.rect.bottom() + 1 + 8
    # Copy: text/plain via the injected clipboard, «Скопировано» for 1.2 s.
    root.copyClicked.emit()
    assert copies == ['Текст встречи'] and owner.copy_state == 'copied'
    assert root.property('presentation').toVariant()['copyState'] == 'copied'
    QTest.qWait(1100)
    assert owner.copy_state == 'copied'
    QTest.qWait(300)
    assert owner.copy_state == 'default'
    outcome[0] = False
    root.copyClicked.emit()
    assert owner.copy_state == 'failed'
    QTest.qWait(1400)
    assert owner.copy_state == 'failed', 'manual copy hint stays'
    # «×» closes; the same recording does not reopen by itself.
    root.closeClicked.emit()
    assert not view.isVisible() and not owner.is_open
    recording.changed.emit()
    assert not view.isVisible()
    # «💬» / tray reopen in the default state.
    recording.participantsRequested.emit()
    assert view.isVisible() and owner.copy_state == 'default'
    # Manual Ctrl+C (review 3/4) reuses the text/plain publication.
    root.copySelectionRequested.emit('a\u2028b')
    assert copies[-1] == 'a\nb' and owner.copy_state == 'default'
    # Explicit keyboard (tray): focusable while open; FocusOut/close releases.
    owner.activate_keyboard()
    assert not (view.flags() & Qt.WindowDoesNotAcceptFocus)
    assert root.property('keyboardMode')
    owner.release_keyboard()
    assert view.flags() & Qt.WindowDoesNotAcceptFocus
    # Window-manager close == «×».
    app.sendEvent(view, QCloseEvent())
    assert not owner.is_open and not view.isVisible()
    recording.participantsRequested.emit()
    # Recording stops → W3 closes and stays closed.
    recording.overlay = {'visible': True, 'active': True, 'state': 'stopping'}
    recording.changed.emit()
    assert not view.isVisible()
    recording.participantsRequested.emit()
    assert not view.isVisible()
    recording.overlay = {'visible': False, 'active': False, 'state': 'hidden'}
    recording.active_meeting_id = ''
    recording.changed.emit()
    # Next recording opens it again.
    start('m2')
    assert view.isVisible()
    # No W2 surface: top-centre fallback under the default W2 row.
    overlay.rect = None
    overlay.geometryChanged.emit()
    assert view.y() + 36 == area.y() + 12 + 36 + 8
    assert not warnings, warnings
finally:
    owner.shutdown()
    assert not view.isVisible()
    recording.participantsRequested.emit()
    recording.changed.emit()
    assert not view.isVisible()
    sip.delete(owner)
    app.processEvents()
assert not warnings, warnings
"""


def _run(probe: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-c", probe, str(REPO)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_w3_qml_states_geometry_escaping_and_keyboard_without_warnings() -> None:
    result = _run(QML_PROBE)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_w3_opens_under_w2_copies_and_closes_with_recording() -> None:
    result = _run(NATIVE_PROBE)
    assert result.returncode == 0, result.stdout + result.stderr
