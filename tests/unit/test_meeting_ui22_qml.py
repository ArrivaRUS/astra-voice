"""Offscreen UI paths for participant text and the recording dialog."""

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
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QObject, QPointF, Qt, QUrl, qVersion
from PyQt5.QtGui import QClipboard, QWindow
from PyQt5.QtQml import QQmlApplicationEngine, QQmlEngine, QQmlExpression
from PyQt5.QtQuick import QQuickWindow
from PyQt5.QtTest import QSignalSpy, QTest

repo, tmp = map(Path, sys.argv[1:3])
sys.path[:0] = [str(repo / part) for part in ('src', 'tests', 'tests/unit')]

from astra_voice import app
from astra_voice.core.settings import Settings
from astra_voice.platform import x11
from astra_voice.platform.session import SessionKind
from astra_voice.ui import meeting_notice
from astra_voice.ui.icons import install_icon_provider
from astra_voice.ui.meeting_overlay import MeetingOverlay
from helpers.qt_app import get_qapplication
from test_meeting_overlay import start
from test_meeting_recording_bridge import Rig

QQuickWindow.setSceneGraphBackend('software')
qt = get_qapplication()
mode = sys.argv[3]
settings = (Settings(meetings_enabled=True, meeting_invite_text='Встреча\u2028для протокола')
            if mode == 'settings' else None)
rig = Rig(tmp, settings=settings)
bridge = rig.bridge


def clipboard_text():
    mime = qt.clipboard().mimeData(QClipboard.Clipboard)
    assert mime.formats() == ['text/plain']
    return mime.text()


def assert_copy_label(button, label):
    context = QQmlEngine.contextForObject(button)
    for expression in ('Accessible.name', 'ToolTip.text'):
        assert QQmlExpression(context, button, expression).evaluate() == (label, False)


def load_main():
    engine = QQmlApplicationEngine()
    info = app._make_app_info(SessionKind.KDE, 'ok')
    install_icon_provider(engine)
    warnings = []

    def record_warnings(rows):
        # Qt 5.15.8 with Debian's fonts emits this footer warning on the
        # pre-UI Main.qml (34f281b) too, before any meeting action.
        footer_warning = 'QML QQuickItem: Binding loop detected for property "implicitHeight"'
        for row in rows:
            if (qVersion() == '5.15.8' and row.url().fileName() == 'AvDialog.qml'
                    and row.description() == footer_warning):
                continue
            warnings.append(row.toString())

    engine.warnings.connect(record_warnings)
    engine.rootContext().setContextProperty('appInfo', info)
    engine.rootContext().setContextProperty('meetingBridge', bridge)
    engine.load(QUrl.fromLocalFile(str(repo / 'qml/Main.qml')))
    assert engine.rootObjects(), warnings
    return engine, info, warnings


def check_settings(shell, info):
    info.show_section(app.SECTION_MEETINGS)
    qt.processEvents()
    section = shell.findChild(QObject, 'meetingsSection')
    assert section is not None
    notice_row = section.findChild(QObject, 'meetingNoticeField')
    invite_row = section.findChild(QObject, 'meetingInviteField')
    notice = section.findChild(QObject, 'meetingNoticeFieldInput')
    invite = invite_row.property('field')
    notice_copy = section.findChild(QObject, 'meetingNoticeFieldCopy')
    invite_copy = section.findChild(QObject, 'meetingInviteFieldCopy')
    for field, button in ((notice, notice_copy), (invite, invite_copy)):
        assert button.property('visible') and button.property('text') == ''
        assert button.mapToScene(QPointF()).x() >= field.mapToScene(QPointF(field.width(), 0)).x()
        assert_copy_label(button, 'Скопировать')
    assert not any(
        child.property('iconName') == 'copy' and child.property('text') == 'Скопировать'
        for child in section.findChildren(QObject)
    )
    assert notice_row.property('copyLabel') == invite_row.property('copyLabel') == 'Скопировать'
    assert invite.objectName() == 'Input'
    assert invite.property('text') == 'Встреча для протокола'

    for key, button in [('meeting_notice_text', notice_copy), ('meeting_invite_text', invite_copy)]:
        button.clicked.emit()
        assert clipboard_text() == bridge.settings[key]
        assert button.property('iconName') == 'check'
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(meeting_notice, 'copy_plain_text', lambda _: False)
            button.clicked.emit()
        assert button.property('iconName') == 'copy', key
        button.clicked.emit()
        assert button.property('iconName') == 'check'
    for button in (notice_copy, invite_copy):
        if button.property('iconName') == 'check':
            assert QSignalSpy(button.iconNameChanged).wait(1800)
        assert button.property('iconName') == 'copy'

    assert notice.property('wrapMode') == 0
    assert invite.property('wrapMode') != 0
    bridge.setSetting('meeting_invite_text', 'Short')
    qt.processEvents()
    single_line_height = invite.height()
    original_notice = bridge.settings['meeting_notice_text']

    long_text = 'Meeting recording for the protocol. ' * 10 + 'End.'
    invite.forceActiveFocus(Qt.TabFocusReason)
    assert invite.property('activeFocus')
    QTest.keyClick(shell, Qt.Key_A, Qt.ControlModifier)
    qt.clipboard().setText(long_text)
    QTest.keyClick(shell, Qt.Key_V, Qt.ControlModifier)
    qt.processEvents()
    assert invite.property('text') == long_text
    assert invite.height() > single_line_height
    assert invite.height() >= (
        invite.property('contentHeight') + invite.property('topPadding')
        + invite.property('bottomPadding')
    )
    QTest.keyClick(shell, Qt.Key_Return)
    assert bridge.settings['meeting_invite_text'] == long_text
    assert bridge.settings['meeting_notice_text'] == original_notice
    assert invite.property('text').splitlines() == [long_text]
    invite_copy.clicked.emit()
    assert clipboard_text() == long_text

    for key, field in (
        ('meeting_notice_text', notice),
        ('meeting_invite_text', invite),
    ):
        field.forceActiveFocus(Qt.TabFocusReason)
        for index, separator in enumerate(('\n', '\r', '\r\n', '\u2028', '\u2029', '\r\n\n\u2028')):
            first = f'First {index}'
            QTest.keyClick(shell, Qt.Key_A, Qt.ControlModifier)
            qt.clipboard().setText(first + separator + 'Second')
            QTest.keyClick(shell, Qt.Key_V, Qt.ControlModifier)
            QTest.keyClick(shell, Qt.Key_Return)
            expected = first + ' Second'
            assert getattr(bridge._stored, key) == expected, (key, repr(separator))
            assert bridge.settings[key] == expected

    bridge.renameRequested.emit('test-id', 'Original name')
    qt.processEvents()
    dialogs = section.findChildren(QObject, 'meetingRenameDialog')
    rename = next(dialog for dialog in dialogs if dialog.property('visible'))
    name = rename.findChild(QObject, 'meetingRenameInput')
    assert name.property('text') == 'Original name'
    assert name.property('wrapMode') == 0 and name.property('maximumLength') == 120
    assert not rename.findChild(QObject, 'meetingRenameCopy')


def check_start(shell, info, initial_visibility):
    tray = Mock()
    focuser = app._WindowFocuser(shell)
    show_meetings = app._wire_meeting_windows(bridge, focuser, info)
    disconnect = app._wire_tray_meetings(tray, bridge, show_meetings)
    dialog = shell.findChild(QWindow, 'meetingStartWindow')
    focus = Mock(return_value=True)

    def activate(request):
        timeout = QSignalSpy(focuser._timer.timeout)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(x11, 'focus_window', focus)
            request()
            assert dialog.isVisible()
            assert timeout or timeout.wait(1000)

    try:
        if initial_visibility == 'minimized':
            shell.showMinimized()
        qt.processEvents()
        original_visibility = shell.visibility()
        expected = QWindow.Minimized if initial_visibility == 'minimized' else QWindow.Hidden
        assert original_visibility == expected
        activate(tray.on_meeting_record)
        assert shell.visibility() == original_visibility
        assert dialog.isExposed() and dialog.transientParent() is None
        focus.assert_called_once_with(int(dialog.winId()))
        assert bridge.startCanStart

        assert dialog.isActive()
        QTest.keyClick(dialog, Qt.Key_Down)
        assert bridge.selectedSource == 'all'
        before = (rig.recording._preview_serial, bridge.startOptions, bridge.startWarnings)
        activate(tray.on_meeting_record)
        assert shell.visibility() == original_visibility
        assert focus.call_count == 2
        assert bridge.selectedSource == 'all' and bridge.startCanStart
        assert (rig.recording._preview_serial, bridge.startOptions, bridge.startWarnings) == before
        activate(tray.on_meeting_open)
        qt.processEvents()
        assert shell.isVisible() and dialog.transientParent() == shell
        assert focus.call_count == 3 and focus.call_args.args == (int(dialog.winId()),)
        assert qt.focusWindow() == dialog and bridge.selectedSource == 'all'
        assert (rig.recording._preview_serial, bridge.startOptions, bridge.startWarnings) == before
        QTest.keyClick(qt.focusWindow(), Qt.Key_Escape)
        qt.processEvents()
        assert not dialog.isVisible()

        section_button = shell.findChild(QObject, 'meetingRecord')
        assert section_button.property('enabled')
        activate(section_button.clicked.emit)
        QTest.keyClick(dialog, Qt.Key_Down)
        assert bridge.selectedSource == 'all'
        before = rig.recording._preview_serial
        activate(section_button.clicked.emit)
        assert focus.call_count == 5 and focus.call_args.args == (int(dialog.winId()),)
        assert bridge.selectedSource == 'all' and rig.recording._preview_serial == before
        bridge.closeStartDialog()
        qt.processEvents()
        assert not dialog.isVisible() and shell.isVisible()
    finally:
        focuser.stop()
        disconnect()


def check_overlay_copy():
    overlay = MeetingOverlay(rig.recording, notice=bridge.notice)
    notice = meeting_notice.MeetingNotice(rig.recording, overlay, notice=bridge.notice)
    bridge.noticeKeyboardRequested.connect(notice.activate_keyboard)
    bridge.overlayKeyboardRequested.connect(overlay.activate_keyboard)
    tray = Mock()
    disconnect = app._wire_tray_meetings(tray, bridge, Mock())
    try:
        start(rig)
        qt.processEvents()
        assert not notice.is_open and not notice._view.isVisible()
        button = overlay._root.findChild(QObject, 'meetingParticipants')
        assert_copy_label(button, 'Скопировать текст для участников')
        assert button.property('iconName') == 'copy'
        assert overlay._view.flags() & Qt.WindowDoesNotAcceptFocus
        assert (overlay._view.flags() & Qt.WindowType_Mask) == Qt.Window

        bridge.setSetting('meeting_notice_text', 'Новое сообщение для встречи')
        button.clicked.emit()
        assert clipboard_text() == bridge.notice()[0]
        assert button.property('iconName') == 'check' and not notice.is_open

        tray.on_meeting_keyboard()
        qt.processEvents()
        for _ in range(2):
            QTest.keyClick(overlay._view, Qt.Key_Tab)
        assert button.property('activeFocus')
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(meeting_notice, 'copy_plain_text', lambda _: False)
            QTest.keyClick(overlay._view, Qt.Key_Return)
        assert button.property('iconName') == 'copy' and not notice.is_open

        bridge.setSetting('meeting_notice_text', 'Текст для клавиатурного копирования')
        QTest.keyClick(overlay._view, Qt.Key_Return)
        assert clipboard_text() == bridge.notice()[0]
        assert button.property('iconName') == 'check' and not notice.is_open
        expired = QSignalSpy(button.iconNameChanged)
        assert expired.wait(1800)
        assert button.property('iconName') == 'copy'

        tray.on_meeting_notice()
        qt.processEvents()
        assert notice.is_open and notice._view.isVisible()
        assert notice._root.property('keyboardMode')
        notice.copy()
        assert clipboard_text() == bridge.notice()[0]
    finally:
        disconnect()
        notice.shutdown()
        overlay.shutdown()
        sip.delete(notice)
        sip.delete(overlay)


try:
    if mode == 'overlay':
        check_overlay_copy()
    else:
        engine, info, warnings = load_main()
        shell = engine.rootObjects()[0]
        try:
            if mode == 'settings':
                check_settings(shell, info)
            else:
                check_start(shell, info, mode)
            assert not warnings, warnings
        finally:
            shell.hide()
            sip.delete(engine)
finally:
    bridge.shutdown()
    qt.processEvents()
"""


def run_probe(mode: str, tmp_path: Path) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software")
    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent"
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"):
        env.pop(key, None)
    process = subprocess.run(
        [sys.executable, "-c", PROBE, str(REPO), str(tmp_path), mode],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=25,
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr


def test_settings_copy_buttons_and_visually_wrapped_invite(tmp_path: Path) -> None:
    run_probe("settings", tmp_path)


@pytest.mark.parametrize("mode", ["hidden", "minimized"])
def test_tray_and_section_raise_w4_preserving_preview_and_shell(mode: str, tmp_path: Path) -> None:
    run_probe(mode, tmp_path)


def test_w2_copies_without_opening_w3_and_tray_still_opens_w3(tmp_path: Path) -> None:
    run_probe("overlay", tmp_path)
