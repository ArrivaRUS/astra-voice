"""W2's public QML contract in an isolated, software-only Qt process."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]

PROBE = r'''
import re
import sys
from pathlib import Path
from PyQt5.QtCore import QObject, QPoint, QMetaObject, Qt, QUrl, pyqtProperty, pyqtSignal
from PyQt5.QtGui import QGuiApplication, QFontDatabase, QFontInfo, QFontMetricsF
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtQuick import QQuickWindow
from PyQt5.QtTest import QTest

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo / "src"))
from astra_voice.ui.icons import install_icon_provider
from astra_voice.ui.meeting_texts import W2_TEXTS

QQuickWindow.setSceneGraphBackend("software")
app = QGuiApplication([])
for path in (repo / "tests/fixtures/fonts").rglob("*.ttf"):
    assert QFontDatabase.addApplicationFont(str(path)) >= 0, path


class Bridge(QObject):
    changed = pyqtSignal()
    value = {}
    overlay = pyqtProperty("QVariantMap", lambda self: self.value, notify=changed)
    overlayTexts = pyqtProperty("QVariantMap", lambda self: W2_TEXTS, constant=True)


class ThemeSource(QObject):
    changed = pyqtSignal()
    value = False
    dark = pyqtProperty(bool, lambda self: self.value, notify=changed)


theme = ThemeSource()
bridge = Bridge()
engine = QQmlApplicationEngine()
install_icon_provider(engine)
warnings = []
engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
engine.rootContext().setContextProperty("meetingBridge", bridge)
engine.rootContext().setContextProperty("themeSource", theme)
engine.loadData(
    b"""
import QtQuick 2.15
import QtQuick.Window 2.15
import "qml"
Window {
    width: overlay.pillWidth + 36
    height: overlay.pillHeight + 42
    visible: true
    color: "transparent"
    MeetingOverlay {
        id: overlay
        x: 18; y: 18
        presentation: meetingBridge.overlay
        texts: meetingBridge.overlayTexts
        freezeAnimations: true
    }
}
""",
    QUrl.fromLocalFile(str(repo / "_w2-unit.qml")),
)
assert engine.rootObjects(), warnings
window = engine.rootObjects()[0]
root = window.findChild(QObject, "meetingOverlay")
assert root is not None


def item(name):
    result = root.findChild(QObject, name)
    assert result is not None, name
    return result


def update(state="recording", **fields):
    bridge.value = dict(
        visible=True,
        state=state,
        elapsed="0:42:10",
        secondary="",
        secondaryAction="",
        pausedMinutes=3,
        canResume=True,
        canExtend=False,
        microphoneName="",
        timerTooltip="На паузе 3 мин. Через 60 мин паузы встреча сохранится",
    )
    bridge.value.update(fields)
    bridge.changed.emit()
    QMetaObject.invokeMethod(root, "forceLayout", Qt.DirectConnection)
    QTest.qWait(25)
    assert not warnings, warnings


def visual(node):
    yield node
    for child in node.childItems():
        yield from visual(child)


def texts():
    return " ".join(
        str(node.property("text") or "")
        for node in visual(root)
        if node.property("visible") and node.metaObject().indexOfProperty("text") >= 0
    )


def click(name):
    target = item(name)
    assert target.property("visible") and target.property("enabled"), name
    p = target.mapToScene(
        __import__("PyQt5.QtCore", fromlist=["QPointF"]).QPointF(
            target.width() / 2, target.height() / 2
        )
    )
    QTest.mouseClick(window, Qt.LeftButton, pos=QPoint(round(p.x()), round(p.y())))
    app.processEvents()


mode = sys.argv[2]
if mode == "states":
    cases = [
        ("recording", {}, "Запись встречи"),
        ("paused", {}, "Пауза"),
        (
            "paused-lock",
            {"secondary": "Экран был заблокирован — продолжите вручную"},
            "Экран был заблокирован",
        ),
        (
            "mic-paused",
            {
                "secondary": "Ваш голос не записывается: микрофон сменился",
                "secondaryAction": "resume-microphone",
                "microphoneName": "Встроенный микрофон",
            },
            "Продолжить с „Встроенный микрофон“",
        ),
        (
            "mic-paused",
            {
                "secondary": ("Ваш голос не записывается: микрофон сменился на «Гарнитура "
                              "Jabra Evolve2 65 USB Headset Microphone»"),
                "secondaryAction": "resume-microphone",
                "microphoneName": "Гарнитура Jabra Evolve2 65 USB Headset Microphone",
            },
            "Продолжить с другим микрофоном",
        ),
        (
            "limit-soon",
            {
                "secondary": "Остановится через 4:48",
                "secondaryAction": "extend",
                "canExtend": True,
            },
            "Продлить на час",
        ),
        (
            "limit-soon",
            {
                "secondary": "Остановится через 4:48",
                "secondaryAction": "extend",
                "canExtend": False,
            },
            "Остановится через 4:48",
        ),
        (
            "app-closed",
            {"secondary": "DION закрылся — пишется только микрофон"},
            "DION закрылся",
        ),
        (
            "recording",
            {
                "secondary": ("Запись стояла на паузе 0:00:14: программа не отвечала. "
                              "Отметка — в расшифровке")
            },
            "программа не отвечала",
        ),
        ("stopping", {}, "Сохраняю запись…"),
        (
            "error",
            {
                "secondary": "Запись остановлена: звуковая служба не отвечает. Записанное сохранено"
            },
            "Запись остановлена",
        ),
    ]
    for state, fields, expected in cases:
        update(state, **fields)
        root.setProperty("compact", False)
        QTest.qWait(20)
        assert expected in texts(), (state, expected, texts())
        assert root.property("visible")
        assert QFontInfo(item("meetingTimer").property("font")).fixedPitch()
        assert (140 if state in ("stopping", "error") else 340) <= root.property("pillWidth") <= 524
        assert 36 <= root.property("pillHeight") <= 120, (state, root.property("pillHeight"))
        assert item("meetingStop").property("visible") == (state not in ("stopping", "error"))
        assert item("meetingExtend").property("visible") == bool(fields.get("canExtend"))
        if state == "limit-soon":
            assert any(node.property("name") == "clock" and node.property("visible")
                       for node in visual(root))
        if state not in ("stopping", "error"):
            for name in (
                "meetingPause",
                "meetingStop",
                "meetingParticipants",
                "meetingCollapse",
            ):
                target = item(name)
                p = target.mapToItem(
                    root, __import__("PyQt5.QtCore", fromlist=["QPointF"]).QPointF(0, 0)
                )
                assert 0 <= p.x() < root.property("pillWidth"), (state, name, p.x())
                assert p.x() + target.width() <= root.property("pillWidth") + 0.1, (state, name)
        root.setProperty("compact", True)
        QTest.qWait(20)
        if state in ("stopping", "error"):
            assert abs(root.property("pillWidth") - (162 if state == "stopping" else 480)) <= 1
            assert expected in texts(), (state, texts())
        else:
            assert abs(root.property("pillWidth") - 141.6) < 0.01
            assert root.property("pillHeight") == 36
            assert not item("meetingStop").property("visible")
            assert not item("meetingSecondary").property("visible")
        assert root.property("visible")
    bridge.value = {"visible": False, "state": "hidden"}
    bridge.changed.emit()
    app.processEvents()
    assert not root.property("visible")
    assert not warnings, warnings
elif mode == "geometry":
    # Body bounds measured from original G3 section 3 PNGs, not shadow/canvas bounds.
    # PT Root UI fixtures + DejaVu Sans Mono fallback, scale 1.
    cases = [
        ("recording", {}, (482, 36)),
        ("paused", {}, (464, 36)),
        ("limit-soon", dict(elapsed="1:55:12", secondary="Остановится через 4:48",
                            secondaryAction="extend", canExtend=True), (482, 71)),
        ("mic-paused", dict(elapsed="0:12:40",
                            secondary="Ваш голос не записывается: микрофон сменился",
                            secondaryAction="resume-microphone",
                            microphoneName="Встроенный микрофон"), (482, 96)),
        ("app-closed", dict(elapsed="1:05:21",
                            secondary="DION закрылся — пишется только микрофон"), (482, 63)),
        ("paused-lock", dict(secondary="Экран был заблокирован — продолжите вручную"),
                            (464, 63)),
        ("mic-paused", dict(elapsed="0:12:40",
                            secondary="Ваш голос не записывается: микрофон сменился на «Гарнитура "
                                      "Jabra Evolve2 65 USB Headset Microphone»",
                            secondaryAction="resume-microphone",
                            microphoneName="Гарнитура Jabra Evolve2 65 USB Headset Microphone"),
                            (482, 120)),
        ("recording", dict(elapsed="0:51:16",
                           secondary="Запись стояла на паузе 0:00:14: индикатор записи не "
                                     "отображался. Отметка — в расшифровке"), (482, 83)),
        ("recording", dict(elapsed="0:51:16",
                           secondary="Запись стояла на паузе 0:00:14: программа не отвечала. "
                                     "Отметка — в расшифровке"), (482, 83)),
        ("notify-fallback", dict(elapsed="0:51:02",
                                secondary="Встреча сохранена. Делаю расшифровку…"), (482, 64)),
        ("stopping", {}, (162, 36)),
        ("error", dict(secondary="Запись остановлена: звуковая служба не отвечает. "
                                 "Записанное сохранено"), (480, 36)),
    ]
    for dark in (False, True):
        theme.value = dark
        theme.changed.emit()
        for state, fields, expected in cases:
            root.setProperty("compact", False)
            update(state, **fields)
            actual = (root.property("pillWidth"), root.property("pillHeight"))
            assert all(abs(a - e) <= 1 for a, e in zip(actual, expected)), (
                dark, state, actual, expected)
            assert item("meetingGrip").property("visible"), (dark, state)
            if fields.get("secondary") and state != "error":
                text = item("meetingSecondaryText")
                assert abs(text.property("font").pixelSize() * text.property("scale") - 12.5) < .01
                assert text.property("text") == fields["secondary"]
                assert "…" not in text.property("text") or state == "notify-fallback"
            if state == "notify-fallback":
                assert not item("meetingExtend").property("visible")
                assert not item("meetingResumeMicrophone").property("visible")
            if state in ("stopping", "error"):
                root.setProperty("compact", True)
                QTest.qWait(20)
                assert actual == (root.property("pillWidth"), root.property("pillHeight"))
                assert item("meetingGrip").property("visible")
                assert not item("meetingTimer").property("visible")
                continue
            root.setProperty("compact", True)
            QTest.qWait(20)
            assert abs(root.property("pillWidth") - 141.6) < .01
            assert root.property("pillHeight") == 36
        root.setProperty("compact", False)
        update()
        title = item("meetingTitle")
        assert abs(title.property("font").pixelSize() * title.property("scale") - 12.5) < .01
        buttons = [node for node in visual(item("meetingPause"))
                   if node.metaObject().indexOfProperty("text") >= 0
                   and node.metaObject().indexOfProperty("font") >= 0]
        assert buttons
        for node in buttons:
            assert abs(node.property("font").pixelSize() * node.property("scale") - 12.5) < .01
        timer = item("meetingTimer")
        font = timer.property("font")
        assert font.pixelSize() == 13
        assert QFontInfo(font).fixedPitch()
        assert QFontInfo(font).family() == "DejaVu Sans Mono"
        advances = [QFontMetricsF(font).horizontalAdvance(str(digit)) for digit in range(10)]
        assert max(advances) - min(advances) < .01, advances
        for compact in (False, True):
            root.setProperty("compact", compact)
            widths = []
            for digit in range(10):
                update(elapsed=f"{digit}:{digit}{digit}:{digit}{digit}")
                widths.append((root.property("pillWidth"), timer.property("width")))
            assert all(abs(w - widths[0][0]) < .01 and abs(t - widths[0][1]) < .01
                       for w, t in widths), widths
    assert not warnings, warnings
elif mode == "spinner":
    update("recording")
    spinner = item("meetingStoppingSpinner")
    rotation = item("meetingStoppingRotation")
    assert not spinner.property("visible")
    assert not rotation.property("running")
    update("stopping")
    assert spinner.property("visible")
    assert not rotation.property("running"), "renderer's freezeAnimations must stop animation"
    frozen_angle = spinner.property("rotation")
    QTest.qWait(180)
    assert spinner.property("rotation") == frozen_angle
    root.setProperty("freezeAnimations", False)
    QTest.qWait(80)
    assert rotation.property("running")
    angle = spinner.property("rotation")
    QTest.qWait(180)
    assert abs(spinner.property("rotation") - angle) > 1, "stopping arc must rotate"
    # The status symbol is an arc; the previous refresh arrow must not remain visible.
    assert not any(node.property("visible") and node.property("name") == "refresh"
                   for node in visual(root))
    update("stopping", visible=False)
    assert not spinner.property("visible")
    assert not rotation.property("running"), "hidden stopping overlay must not animate"
    update("stopping")
    assert rotation.property("running")
    root.setProperty("freezeAnimations", True)
    QTest.qWait(40)
    angle = spinner.property("rotation")
    QTest.qWait(180)
    assert spinner.property("rotation") == angle
    assert not rotation.property("running")
    root.setProperty("freezeAnimations", False)
    update("recording")
    assert not spinner.property("visible")
    assert not rotation.property("running")
    angle = spinner.property("rotation")
    QTest.qWait(180)
    assert spinner.property("rotation") == angle
    for state in ("paused", "paused-lock", "mic-paused", "limit-soon", "error", "hidden"):
        update(state)
        assert not spinner.property("visible"), state
        assert not rotation.property("running"), state
    assert not warnings, warnings
elif mode == "actions":
    received = []
    for name in (
        "pauseClicked",
        "resumeClicked",
        "stopClicked",
        "participantsClicked",
        "extendClicked",
        "resumeMicrophoneClicked",
        "releaseKeyboardRequested",
    ):
        getattr(root, name).connect(lambda key=name: received.append(key))
    update()
    for control, signal in [
        ("meetingPause", "pauseClicked"),
        ("meetingStop", "stopClicked"),
        ("meetingParticipants", "participantsClicked"),
    ]:
        click(control)
        assert received.pop() == signal
    update("paused")
    click("meetingPause")
    assert received.pop() == "resumeClicked"
    update(
        "paused-lock", canResume=False, secondary="Экран был заблокирован — продолжите вручную"
    )
    assert not item("meetingPause").property("enabled")
    assert item("meetingStop").property("enabled")
    update(
        "limit-soon",
        secondary="Остановится через 4:48",
        secondaryAction="extend",
        canExtend=True,
    )
    click("meetingExtend")
    assert received.pop() == "extendClicked"
    update(
        "mic-paused",
        secondary="Ваш голос не записывается",
        secondaryAction="resume-microphone",
        microphoneName="Микрофон",
    )
    click("meetingResumeMicrophone")
    assert received.pop() == "resumeMicrophoneClicked"
    click("meetingCollapse")
    assert root.property("compact") and root.property("visible")
    click("meetingCollapse")
    assert not root.property("compact")
    update()
    for name in ("meetingPause", "meetingStop", "meetingParticipants", "meetingCollapse"):
        assert not item(name).property("activeFocusOnTab")
    window.requestActivate()
    root.setProperty("keyboardMode", True)
    QMetaObject.invokeMethod(root, "focusFirstControl", Qt.DirectConnection)
    QTest.qWait(20)
    assert item("meetingPause").property("activeFocus")
    for key in (Qt.Key_Return, Qt.Key_Space):
        QTest.keyClick(window, key)
        assert received.pop() == "pauseClicked"
    QTest.keyClick(window, Qt.Key_Tab)
    assert item("meetingStop").property("activeFocus")
    QTest.keyClick(window, Qt.Key_Return)
    assert received.pop() == "stopClicked"
    QTest.keyClick(window, Qt.Key_Escape)
    assert root.property("compact") and root.property("visible")
    assert received.pop() == "releaseKeyboardRequested"
    assert received == [], received
    assert not warnings, warnings
elif mode == "fake-contract":
    from astra_voice.core.settings import Settings
    from astra_voice.ui.meeting_bridge import MeetingBridge

    sys.path.insert(0, str(repo / "tests"))
    sys.path.insert(0, str(repo / "tests/xvfb"))
    # Import fixture classes only: no xvfb test, window fixture or X server is run.
    from test_meetings_section import Bridge as SectionBridge
    from test_start_dialog import RecordingBridge

    actual = MeetingBridge(Settings())
    actual_meta = actual.metaObject()
    for fixture in (bridge, SectionBridge("empty"), RecordingBridge()):
        fake_meta = fixture.metaObject()
        for index in range(fake_meta.propertyCount()):
            prop = fake_meta.property(index)
            actual_index = actual_meta.indexOfProperty(prop.name())
            assert actual_index >= 0, prop.name()
            assert actual_meta.property(actual_index).typeName() == prop.typeName(), prop.name()
        for index in range(fake_meta.methodOffset(), fake_meta.methodCount()):
            signature = fake_meta.method(index).methodSignature()
            assert actual_meta.indexOfMethod(signature) >= 0, bytes(signature)
    allowed = {actual_meta.property(i).name() for i in range(actual_meta.propertyCount())}
    allowed.update(bytes(actual_meta.method(i).name()).decode()
                   for i in range(actual_meta.methodCount()))
    for filename in (
        "Main.qml", "StartDialog.qml", "sections/Meetings.qml",
        "components/ResponsibilityDialog.qml", "MeetingOverlay.qml",
        "components/MeetingOverlayButton.qml", "components/OverlaySurface.qml",
    ):
        source = (repo / "qml" / filename).read_text()
        aliases = "meetings|meetingBridge" if filename == "Main.qml" else "meeting|meetingBridge"
        if filename in ("sections/Meetings.qml", "components/ResponsibilityDialog.qml"):
            aliases = "bridge|meetingBridge"
        pattern = r"(?:" + aliases + r")\.([A-Za-z_][A-Za-z_0-9]*)"
        for name in re.findall(pattern, source):
            assert name in allowed, (filename, name)
    actual.shutdown()
    assert not warnings, warnings
window.hide()
from PyQt5 import sip
sip.delete(engine)
assert not warnings, warnings
'''


def run_probe(mode: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
    env["QT_SCALE_FACTOR"] = "1"
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-c", PROBE, str(REPO), mode],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("mode", ["states", "geometry", "actions", "fake-contract", "spinner"])
def test_w2_qml_contract_without_warnings(mode: str) -> None:
    result = run_probe(mode)
    assert result.returncode == 0, result.stdout + result.stderr


NATIVE_SETUP = r"""
import sys
from pathlib import Path
from PyQt5.QtCore import QObject, QMetaObject, Qt, pyqtSignal
from PyQt5.QtGui import QGuiApplication, QFontDatabase
from PyQt5.QtQuick import QQuickView, QQuickWindow
from PyQt5.QtTest import QTest
from PyQt5 import sip
repo = Path(sys.argv[1])
sys.path.insert(0, str(repo / 'src'))
from astra_voice.ui.meeting_overlay import MeetingOverlay
QQuickWindow.setSceneGraphBackend('software')
app = QGuiApplication([])
for path in (repo / 'tests/fixtures/fonts').rglob('*.ttf'):
    assert QFontDatabase.addApplicationFont(str(path)) >= 0

class Recording(QObject):
    changed = pyqtSignal()
    overlay = dict(visible=True, active=True, state='recording', elapsed='0:42:10',
                   secondary='', secondaryAction='', canResume=True, canExtend=False,
                   microphoneName='', paused=False)
    actions = []
    def overlay_action(self, action):
        self.actions.append(action)
    def unbind_indicators(self):
        pass

warnings = []

def make_view():
    view = QQuickView()
    view.engine().warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
    return view

recording = Recording()
owner = MeetingOverlay(recording, view_factory=make_view)
view = owner._view
root = view.rootObject()
"""

NATIVE_PROBE = (
    NATIVE_SETUP
    + r"""
try:
    QTest.qWait(30)
    assert view.isVisible()
    assert view.flags() & Qt.WindowDoesNotAcceptFocus
    assert view.flags() & Qt.WindowStaysOnTopHint
    area = view.screen().availableGeometry()
    assert abs(view.x() - (area.x() + (area.width() - view.width()) // 2)) <= 1
    assert view.y() + 18 == area.y() + 12
    original = view.position()
    root.dragRequested.emit(50, 20)
    assert view.position().x() == original.x() + 50
    assert view.position().y() == original.y() + 20
    root.dragRequested.emit(-100000, -100000)
    assert view.x() + 18 >= area.left()
    assert view.y() + 18 >= area.top()
    for state in ('recording', 'paused', 'error'):
        recording.overlay.update(state=state, paused=state == 'paused')
        recording.changed.emit()
        owner.activate_keyboard()
        QTest.qWait(20)
        assert not (view.flags() & Qt.WindowDoesNotAcceptFocus)
        assert view.isVisible()
        view.close()
        app.processEvents()
        assert view.isVisible(), state
    recording.overlay.update(state='paused', paused=True)
    recording.changed.emit()
    owner.activate_keyboard()
    QTest.qWait(20)
    QMetaObject.invokeMethod(root, 'focusFirstControl', Qt.DirectConnection)
    QTest.keyClick(view, Qt.Key_Escape)
    assert root.property('compact')
    assert view.isVisible()
    assert view.flags() & Qt.WindowDoesNotAcceptFocus
    assert not root.property('keyboardMode')
    for signal, action in [('pauseClicked', 'pause'), ('resumeClicked', 'resume'),
                           ('stopClicked', 'stop'), ('participantsClicked', 'participants'),
                           ('extendClicked', 'extend')]:
        getattr(root, signal).emit()
        assert recording.actions.pop() == action
    assert not warnings, warnings
    recording.overlay.update(visible=False, active=False, state='hidden')
    recording.changed.emit()
    assert not view.isVisible()
finally:
    owner.shutdown()
    assert not view.isVisible()
    sip.delete(owner)
    app.processEvents()
assert not warnings, warnings
"""
)


def test_native_w2_close_drag_and_explicit_keyboard_contract() -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", NATIVE_PROBE, str(REPO)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


MASK_PROBE = (
    NATIVE_SETUP
    + r"""
from math import ceil
from PyQt5.QtCore import QPoint, QRect
from PyQt5.QtGui import QRegion


def assert_surface_mask():
    QTest.qWait(80)
    width = ceil(float(root.property('pillWidth')))
    height = ceil(float(root.property('pillHeight')))
    mask = view.mask()
    assert mask == QRegion(QRect(18, 18, width, height)), (
        recording.overlay['state'], root.property('compact'), mask.boundingRect(), width, height
    )
    assert not mask.contains(QPoint(10, 10)), 'shadow must pass clicks through'
    assert mask.contains(QPoint(18, 18))
    assert mask.contains(QPoint(18 + width - 1, 18 + height - 1))
    assert not mask.contains(QPoint(18 + width, 18 + height - 1))
    assert not mask.contains(QPoint(18 + width - 1, 18 + height))


try:
    for state, secondary in [
        ('recording', ''),
        ('paused-lock', 'Экран был заблокирован — продолжите вручную'),
        ('mic-paused', 'Микрофон сменился. ' * 12),
        ('stopping', ''),
        ('error', 'Ошибка записи'),
    ]:
        recording.overlay.update(state=state, secondary=secondary)
        recording.changed.emit()
        for compact in (False, True):
            root.setProperty('compact', compact)
            assert_surface_mask()
            owner.activate_keyboard()
            assert_surface_mask()
            owner.release_keyboard()
            assert_surface_mask()
    assert not warnings, warnings
finally:
    owner.shutdown()
    sip.delete(owner)
    app.processEvents()
assert not warnings, warnings
"""
)


def test_native_w2_input_mask_tracks_surface_and_keyboard_remaps() -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", MASK_PROBE, str(REPO)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
