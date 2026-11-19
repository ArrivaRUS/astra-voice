"""Focused regressions for QML round 2: focus fallback and a long participants drawer."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_meeting_s56b import PROBE

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
BASE = PROBE.split("wait(lambda: len(fields())==3,")[0]
BASE = BASE.replace(
    "import QtQuick.Window 2.15", "import QtQuick.Window 2.15\nimport QtQuick.Controls 2.15"
)
BASE = BASE.replace(
    "width:796; bridge:cardBridge } }",
    "width:796; bridge:cardBridge }\n"
    'Button { objectName:"afterCard"; x:700; y:740; text:"After"; activeFocusOnTab:true } }',
)
FINISH = """
assert not warnings, warnings
b.shutdown()
window.hide()
from PyQt5 import sip
sip.delete(engine)
print('ok')
"""


def run_probe(tmp_path: Path, body: str) -> None:
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
        [sys.executable, "-c", BASE + body + FINISH, str(REPO), str(tmp_path)],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok")


@pytest.mark.parametrize("state", ["policy-off", "transcribing", "no-diarization"])
def test_table_tab_continues_native_chain_without_editable_names(
    tmp_path: Path, state: str
) -> None:
    run_probe(
        tmp_path,
        "\nstate = "
        + repr(state)
        + r"""
wait(lambda: len(fields())==3, 'ready fields')
if state == 'policy-off':
    b.refreshPolicy(Policy(raw={'meeting_record':'off'}))
else:
    current = b._card_meeting()
    b._card_builder.speakers = None
    if state == 'transcribing':
        current.state = 'transcribing'
        live = transcript.utterances
    else:
        current.transcription.diarization = False
        live = [replace(u, speaker='others') if u.speaker != 'me' else u
                for u in transcript.utterances]
    b._card_builder.load(live, complete=state != 'transcribing')
    b.changed.emit()
QTest.qWait(50)
assert not b.card['namesEditable']
assert not any(f.isEnabled() for f in fields())
table = one('meetingCardTableFocus')
table.forceActiveFocus(Qt.TabFocusReason)
assert table.hasActiveFocus()
QTest.keyClick(window, Qt.Key_Tab)
wait(lambda: one('afterCard').hasActiveFocus(), 'Tab must leave table via native chain: '+state)
""",
    )


def test_long_drawer_reveals_tab_targets_and_accepts_mouse_wheel(tmp_path: Path) -> None:
    run_probe(
        tmp_path,
        r"""
from copy import deepcopy
from PyQt5.QtCore import QPoint
from PyQt5.QtGui import QWheelEvent
names = SpeakerMap(labels={'S'+str(i): Speaker(name='Имя '+str(i)) for i in range(1,11)},
                   transcript_generation=transcript.meeting_generation)
long_rows = [replace(transcript.utterances[1], id='u'+str(i), seq=i-1, speaker='S'+str(i),
                     start_ms=i*10000, end_ms=i*10000+5000) for i in range(1,11)]
b._speakers = names
b._saved_speakers = deepcopy(names)
b._card_transcript = replace(transcript, utterances=long_rows)
b._card_builder.speakers = names
b._card_builder.load(long_rows, complete=True)
b.changed.emit()
assert b.card['namesEditable']
window.setWidth(716); window.setHeight(520)
one('probeCard').setProperty('width',672)
QTest.qWait(50)
click(one('meetingCardNames'))
drawer = window.findChild(QObject,'meetingParticipantsDrawer')
wait(lambda: drawer.property('opened'), 'long drawer opens')
wait(lambda: len(fields())==10, 'ten fields')
def inside(item):
    point = item.mapToScene(QPointF(0,0))
    return point.y() >= 0 and point.y()+item.height() <= window.height()
for index in range(10):
    wait(lambda: fields()[index].hasActiveFocus(), 'Tab reaches S'+str(index+1))
    wait(lambda: inside(fields()[index]), 'focused S'+str(index+1)+' is fully visible')
    QTest.keyClick(window, Qt.Key_Tab)
wait(lambda: one('meetingMergeOpen').hasActiveFocus() and inside(one('meetingMergeOpen')),
     'merge button visible by Tab')
scroll = window.findChild(QObject,'meetingParticipantsScroll')
assert scroll is not None
scroll.setProperty('contentY',0)
QTest.qWait(30)
point = QPointF(window.width()-100,window.height()/2)
wheel = QWheelEvent(point,point,QPoint(0,0),QPoint(0,-3600),Qt.NoButton,Qt.NoModifier,
                    Qt.NoScrollPhase,False)
app.sendEvent(window,wheel)
wait(lambda: inside(fields()[-1]), 'mouse wheel exposes last name')
wait(lambda: not scroll.property('moving'), 'wheel scroll settles')
QTest.qWait(50)
click(fields()[-1])
wait(lambda: fields()[-1].hasActiveFocus(), 'mouse focuses last visible name')
click(one('meetingMergeOpen'))
pop = window.findChild(QObject,'meetingMergePopover')
wait(lambda: pop.property('opened'), 'mouse opens merge after scrolling')
QTest.keyClick(window,Qt.Key_Escape)
wait(lambda: not pop.property('opened'), 'close merge')
QTest.keyClick(window,Qt.Key_Escape)
wait(lambda: not drawer.property('opened'), 'close drawer')
""",
    )


def test_panel_titles_two_line_quotes_and_d4_number_circles(tmp_path: Path) -> None:
    run_probe(
        tmp_path,
        r"""
wait(lambda: len(fields())==3, 'ready speakers')
b._card_builder.first_utterances = {key: replace(row,text='Длинная первая реплика участника ' * 8)
    for key,row in b._card_builder.first_utterances.items()}
b.changed.emit()
QTest.qWait(40)
def verify_panel():
    labels = [i.property('text') for i in items('meetingParticipantLabel')]
    assert labels == ['Я','Спикер 1','Спикер 2','Спикер 3'], labels
    quotes = [i for i in items('meetingParticipantQuote') if i.property('text').startswith('«')]
    assert len(quotes)==3
    assert all(1 <= q.property('lineCount') <= 2
               and q.property('text').endswith('»') for q in quotes)
    assert all(q.property('truncated') for q in quotes)
    assert all(q.property('maximumLineCount') == 2 for q in quotes)
verify_panel()
# 1024/900 and both instantiated panel layouts use the same roles/quote truncation.
one('probeCard').setProperty('width',672)
QTest.qWait(50)
click(one('meetingCardNames'))
drawer = window.findChild(QObject,'meetingParticipantsDrawer')
wait(lambda: drawer.property('opened'), 'drawer')
verify_panel()
QTest.keyClick(window,Qt.Key_Escape)
wait(lambda: not drawer.property('opened'), 'closed drawer')
one('probeCard').setProperty('width',796)
QMetaObject.invokeMethod(one('probeCard'),'openSaveDialog')
dialog = window.findChild(QObject,'meetingSaveDialog')
wait(lambda: dialog.property('opened'), 'D4')
assert not items('meetingSavePreviewHeader')
expected = [r['num'] for r in b.savePreview()['rows'] if r.get('num')]
assert expected
numbers = [i.property('num') for i in items('meetingSavePreviewNumber')]
assert numbers==expected, (numbers,expected)
QMetaObject.invokeMethod(dialog,'close')
""",
    )


def test_loaded_named_meeting_drawer_fields_accept_tab_and_edits(tmp_path: Path) -> None:
    """Use the actual disk loader, including its legacy map generation binding."""
    run_probe(
        tmp_path,
        r"""
long_rows = [replace(transcript.utterances[1], id='u'+str(i), seq=i-1, speaker='S'+str(i),
                     start_ms=i*10000, end_ms=i*10000+5000) for i in range(1,11)]
saved_text = replace(transcript, utterances=long_rows,
                     transcript_revision=transcript.transcript_revision+1)
saved_names = SpeakerMap(labels={'S'+str(i): Speaker(name='Имя '+str(i))
                                for i in range(1,11)})
rig.store.write(meeting.id, 'transcript.json', saved_text, writer=Writer.TRANSCRIBE)
rig.store.write(meeting.id, 'speakers.json', saved_names, writer=Writer.GUI)
b.retryCard()
while rig.tasks:
    rig.tasks.pop(0)()
b._card_poll()
assert b.card['namesEditable'] and b._card_meeting().rediar is None
assert b._speakers.transcript_generation == saved_text.meeting_generation
window.setWidth(716); window.setHeight(520)
one('probeCard').setProperty('width',672)
QTest.qWait(50)
click(one('meetingCardNames'))
drawer = window.findChild(QObject,'meetingParticipantsDrawer')
wait(lambda: drawer.property('opened'), 'loaded drawer opens')
wait(lambda: len(fields())==10, 'ten loaded name fields')
for index in range(10):
    field = fields()[index]
    wait(lambda: field.hasActiveFocus(), 'Tab reaches loaded S'+str(index+1))
    assert field.isEnabled()
    if index == 0:
        QTest.keyClick(window, Qt.Key_A, Qt.ControlModifier)
        QTest.keyClick(window, Qt.Key_Z)
        wait(lambda: b.card['participants'][1]['name']=='z', 'loaded name edits')
        assert field.hasActiveFocus()
        assert b.flushSpeakerWrites()
        assert rig.store.read(meeting.id, 'speakers.json').labels['S1'].name == 'z'
        assert b.cardRows.get(0)['who']=='z'
        assert 'z' in b.savePreviewText('txt')
    QTest.keyClick(window, Qt.Key_Tab)
wait(lambda: one('meetingMergeOpen').hasActiveFocus(), 'loaded names Tab reaches merge')
QTest.keyClick(window, Qt.Key_Escape)
wait(lambda: not drawer.property('opened'), 'close loaded drawer')
""",
    )
