"""M7 QML interactions against the real asynchronous MeetingBridge; isolated offscreen."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]

PROBE = r'''
import sys
from pathlib import Path
from dataclasses import replace
from PyQt5.QtCore import QObject, QMetaObject, QPointF, Qt, QUrl
from PyQt5.QtGui import QFontDatabase
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QTest, QSignalSpy
repo, work = map(Path, sys.argv[1:])
sys.path[:0] = [str(repo), str(repo/'src'), str(repo/'tests'), str(repo/'tests/unit')]
from helpers.qt_app import get_qapplication
from test_meeting_card_bridge import SaveRig
from test_meeting_speakers import data
from astra_voice.meetings.model import SpeakerMap, Speaker
from astra_voice.meetings.store import Writer, MeetingStoreError
from astra_voice.ui.icons import install_icon_provider
from astra_voice.core.policy import Policy
from astra_voice.meetings.policy import parse_policy
app = get_qapplication()
for path in (repo/'tests/fixtures/fonts/pt-root-ui').glob('*.ttf'):
    assert QFontDatabase.addApplicationFont(str(path)) >= 0
rig = SaveRig(work)
meeting, transcript, names = data()
names.labels['S3'] = Speaker()
transcript.utterances.append(replace(transcript.utterances[0], id='u4', seq=4,
    start_ms=250000, end_ms=310000, text='Похожий звук', echo=True))
rig.store.create(meeting)
rig.store.write(meeting.id, 'transcript.json', transcript, writer=Writer.TRANSCRIBE)
rig.store.write(meeting.id, 'speakers.json', names, writer=Writer.GUI)
rig.open_card()
b = rig.bridge
assert b.card['namesEditable']
engine = QQmlApplicationEngine()
warnings = []
engine.warnings.connect(lambda rows: warnings.extend(r.toString() for r in rows))
engine.rootContext().setContextProperty('cardBridge', b)
install_icon_provider(engine)
engine.loadData(b"""import QtQuick 2.15
import QtQuick.Window 2.15
import "qml"
import "qml/sections"
Window { width:840; height:800; visible:true
  Rectangle { anchors.fill:parent; color:Theme.bgApp
    MeetingCard { objectName:"probeCard"; x:22; y:12; width:796; bridge:cardBridge } }
}""", QUrl.fromLocalFile(str(repo/'_s56b.qml')))
assert engine.rootObjects(), warnings
window = engine.rootObjects()[0]
window.requestActivate()
def walk(item):
    yield item
    for child in item.childItems(): yield from walk(child)
def items(name, visible=True):
    return [i for i in walk(window.contentItem())
            if i.objectName()==name and (not visible or i.isVisible())]
def one(name): return items(name)[0]
def wait(check, label):
    for _ in range(200):
        QTest.qWait(10)
        if check(): return
    raise AssertionError((label, warnings))
def click(item):
    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
        item.mapToScene(QPointF(item.width()/2,item.height()/2)).toPoint())
def fields(): return items('meetingSpeakerName')
wait(lambda: len(fields())==3, 'three speaker fields')
window.requestActivate()
wait(window.isActive, 'active window')
table_focus = one('meetingCardTableFocus')
table_focus.forceActiveFocus(Qt.TabFocusReason)
QTest.keyClick(window, Qt.Key_Tab)
wait(lambda: fields()[0].hasActiveFocus(), 'table Tab reaches first name')
QTest.keyClick(window, Qt.Key_Backtab)
wait(lambda: table_focus.hasActiveFocus(), 'first name Backtab reaches table')
field = fields()[1]
assert field.property('speakerId')=='S2'
spy = QSignalSpy(b.speakerOperationFinished)
resets = QSignalSpy(b.cardRows.modelReset)
field.forceActiveFocus(Qt.TabFocusReason)
for key, value in [(Qt.Key_A, 'a'), (Qt.Key_B, 'ab'), (Qt.Key_C, 'abc')]:
    QTest.keyClick(window, key)
    wait(lambda: b.card['participants'][2]['name']==value, 'per-key rename')
    assert fields()[1] is field and field.hasActiveFocus()
    wait(lambda: any(i.property('text')==value for i in items('meetingCardWho')), 'table rename')
assert b.flushSpeakerWrites()
assert len(spy)==3 and not resets
# Immediate validation refusal: a control character injected by the editing signal.
field.setProperty('text', 'bad\nname')
QMetaObject.invokeMethod(field, 'textEdited')
assert field.property('text')=='abc'
assert one('meetingSpeakerError').property('text')==b.speakerOperation['text']
# Late disk refusal must also restore a still-focused field.
original_write = rig.store.write
def fail(mid, filename, *args, **kw):
    if filename=='speakers.json': raise MeetingStoreError('private error')
    return original_write(mid, filename, *args, **kw)
rig.store.write = fail
QTest.keyClick(window, Qt.Key_D)
wait(lambda: b.speakerOperation['code']=='save-error', 'async disk refusal')
assert field.property('text')=='abc' and field.hasActiveFocus()
assert one('meetingSpeakerError').property('text')==b.speakerOperation['text']
rig.store.write = original_write
# Source is the focused speaker. Escape/Enter on Cancel return to the opener.
fields()[2].forceActiveFocus(Qt.TabFocusReason)
click(one('meetingMergeOpen'))
pop = window.findChild(QObject,'meetingMergePopover')
wait(lambda: pop.property('opened'), 'merge popover')
wait(lambda: one('meetingMergeCancel').hasActiveFocus(), 'safe cancel focus')
assert one('meetingMergeTarget').property('count')==2
QTest.keyClick(window, Qt.Key_Return)
wait(lambda: not pop.property('opened'), 'default Enter cancels merge')
assert len(b.card['participants'])==4
click(one('meetingMergeOpen'))
wait(lambda: pop.property('opened'), 'merge for Escape')
QTest.keyClick(window, Qt.Key_Escape)
wait(lambda: not pop.property('opened'), 'Escape closes merge')
assert one('meetingMergeOpen').hasActiveFocus()
click(one('meetingMergeOpen'))
wait(lambda: pop.property('opened'), 'merge reopened')
click(one('meetingMergeConfirm'))
wait(lambda: b.speakerOperation['code']=='merged', 'merge persisted')
wait(lambda: len(fields())==2 and one('meetingSpeakerToast').isVisible(), 'merge result')
assert 'Спикер 3 объединён' in one('meetingSpeakerToast').property('message')
QTest.qWait(4100)
assert not items('meetingSpeakerToast')
# Marks are plain, have no Tab stop; original text stays in the table.
flags = items('meetingCardFlag')
assert any(i.property('text')=='возможное эхо' for i in flags)
assert all(not i.property('activeFocusOnTab') for i in flags)
# D4 open/close lifecycle, count, mouse + Space, Stats visibility.
card = one('probeCard')
QMetaObject.invokeMethod(card,'openSaveDialog')
dialog = window.findChild(QObject,'meetingSaveDialog')
wait(lambda: dialog.property('opened') and b.savePreviewOpen, 'save preview open')
check = one('meetingSaveExcludeEcho')
assert not check.property('checked') and b.saveEchoCount==1
click(check)
wait(lambda: b.saveExcludeEcho and check.property('checked'), 'mouse checkbox')
assert not any(r.get('textMuted') for r in b.savePreview()['rows'])
check.forceActiveFocus(Qt.TabFocusReason)
QTest.keyClick(window, Qt.Key_Space)
assert not b.saveExcludeEcho and not check.property('checked')
b.saveTemplate='stats'
QTest.qWait(40)
assert not items('meetingSaveExcludeEcho')
assert one('meetingSaveExcludedEcho').property('text')==b.savePreview()['excludedEcho']
b.saveTemplate='stenogram'
b.setSaveExcludeEcho(True)
QTest.keyClick(window, Qt.Key_Escape)
wait(lambda: not dialog.property('opened') and not b.savePreviewOpen, 'save preview closed')
QMetaObject.invokeMethod(card,'openSaveDialog')
wait(lambda: dialog.property('opened'), 'reopen save')
assert not b.saveExcludeEcho
QMetaObject.invokeMethod(dialog,'close')
wait(lambda: not b.savePreviewOpen,'close flag')
# Narrow drawer: first field, Escape restores the triggering button.
card.setProperty('width',672)
QTest.qWait(40)
one('meetingCardMore').forceActiveFocus(Qt.TabFocusReason)
QTest.keyClick(window, Qt.Key_Tab)
wait(lambda: one('meetingCardTableFocus').hasActiveFocus(), 'narrow header Tab reaches table')
QTest.keyClick(window, Qt.Key_Tab)
wait(lambda: one('meetingCardNames').hasActiveFocus(), 'narrow table Tab reaches names')
QTest.keyClick(window, Qt.Key_Backtab)
wait(lambda: one('meetingCardTableFocus').hasActiveFocus(), 'narrow names Backtab reaches table')
QTest.keyClick(window, Qt.Key_Tab)
QTest.keyClick(window, Qt.Key_Return)
drawer = window.findChild(QObject,'meetingParticipantsDrawer')
wait(lambda: drawer.property('opened'), 'drawer opens')
assert any(f.hasActiveFocus() for f in fields())
QTest.keyClick(window, Qt.Key_Escape)
wait(lambda: not drawer.property('opened'), 'drawer Escape')
wait(lambda: one('meetingCardNames').hasActiveFocus(), 'drawer return focus')
card.setProperty('width',796)
# Policy-off leaves saved names visible, operations and fields unavailable.
b.refreshPolicy(Policy(raw={'meeting_record':'off'}))
QTest.qWait(30)
assert fields() and all(not f.isEnabled() for f in fields())
assert any(i.property('text')=='Спикер 2' for i in items('meetingParticipantLabel'))
assert any(f.property('text')=='abc' for f in fields())
# transcribing: anonymous speaker fields disabled, precise hint, no merge action.
b.refreshPolicy(Policy(raw={}))
m = rig.meeting(); m.state = 'transcribing'
rig.store.write(meeting.id, 'meeting.json', m, writer=Writer.GUI)
b._snapshot = lambda _meeting: {'utterances':[u.to_dict() for u in transcript.utterances]}
rig.open_card()
QTest.qWait(40)
assert fields() and all(not f.isEnabled() for f in fields())
assert not items('meetingMergeOpen')
assert one('meetingCardNamesLater').property('text')==b.card['namesHint']
# Missing models: bridge supplies the hint and receives the install request.
b.closeMeeting()
m = rig.meeting()
m.state='done'; m.transcription.diarization=False
rig.store.write(meeting.id, 'meeting.json', m, writer=Writer.GUI)
b._stored.meeting_diarization=True
rig.open_card()
QTest.qWait(40)
install = QSignalSpy(b.diarizationInstallRequested)
assert one('meetingDiarizationMissing').isVisible()
click(one('meetingDiarizationInstall'))
assert len(install)==1
QMetaObject.invokeMethod(card,'openSaveDialog')
wait(lambda: b.savePreviewOpen, 'last save open')
b.closeMeeting()
wait(lambda: not b.savePreviewOpen and not dialog.property('opened'), 'card close closes D4')
assert not warnings,warnings
b.shutdown()
window.hide()
from PyQt5 import sip
sip.delete(engine)
print('ok')
'''


def test_s56b_qml_interactions(tmp_path: Path) -> None:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"}
    }
    env.update(
        QT_QPA_PLATFORM="offscreen",
        QT_QUICK_BACKEND="software",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
    )
    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(REPO), str(tmp_path)],
        env=env,
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok")
