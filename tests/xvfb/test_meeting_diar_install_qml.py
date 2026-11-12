"""09 / §У.2–У.5: production bridge, layout, keyboard and accessibility (no network)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_meeting_s56b_round2 import BASE, FINISH

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]

SETUP = r"""
from threading import Event
from types import SimpleNamespace
m=b._card_meeting(); m.transcription.diarization=False
rig.store.write(m.id,'meeting.json',m,writer=Writer.GUI)
b.closeMeeting()
b._stored.meeting_diarization=True
b._stored.offline=False
rig.open_card()
wait(lambda: one('meetingDiarizationMissing').isVisible(), 'install row')
def stage(code, reason=''):
    b._diar_code=code; b._diar_reason=reason
    b._diar_percent=42; b._diar_accessible_percent=40
    if code=='installed': b._diar_card_installed=m.id
    b._publish_diar_install()
    QTest.qWait(40)
stage('missing')
b._diar_downloads=SimpleNamespace(cancel=Event(),_thread=None,start=lambda:None,shutdown=lambda:None)
b.diarizationInstallRequested.connect(b._start_diar_install)
"""


def probe(
    tmp_path: Path,
    body: str,
    *,
    dark: bool,
    section: bool = False,
    install: bool = True,
    dpi: int = 96,
) -> None:
    base = BASE
    if section:
        base = base.replace(
            "engine.loadData(b",
            "engine.rootContext().setContextProperty('meetingBridge', b)\nengine.loadData(b",
        ).replace(
            'Button { objectName:"afterCard";',
            'Meetings { objectName:"probeSection"; width:796; visible:false }\n'
            'Button { objectName:"afterCard";',
        )
    base = base.replace(
        "engine.loadData(b",
        "from PyQt5.QtCore import pyqtProperty\n"
        "class Palette(QObject):\n"
        f"    dark=pyqtProperty(bool,lambda self:{dark!r},constant=True)\n"
        "palette=Palette()\nengine.rootContext().setContextProperty('themeSource',palette)\n"
        "engine.loadData(b",
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"}
    }
    env.update(
        QT_QPA_PLATFORM="offscreen",
        QT_QUICK_BACKEND="software",
        QT_FONT_DPI=str(dpi),
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            base + (SETUP if install else "") + body + FINISH,
            str(REPO),
            str(tmp_path),
        ],
        env=env,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("dark", [False, True])
def test_card_geometry_focus_keyboard_and_accessibility(tmp_path: Path, dark: bool) -> None:
    probe(
        tmp_path,
        r"""
button=one('meetingDiarizationInstall')
window.requestActivate()
button.forceActiveFocus(Qt.TabFocusReason)
QTest.keyClick(window,Qt.Key_Return)
wait(lambda: b.diarizationInstall['code']=='downloading', 'enter starts install')
assert button.hasActiveFocus()
stage('downloading')
progress=one('meetingDiarizationCardProgress')
assert (progress.width(),progress.height())==(160,4)
assert progress.property('value')==40
assert progress.property('to')==100
from PyQt5.QtQml import QQmlExpression, QQmlEngine
context=QQmlEngine.contextForObject(progress)
def qml(item, expression):
    exp=QQmlExpression(context,item,expression)
    value,undefined=exp.evaluate()
    assert not exp.hasError(), exp.error().toString()
    return value
assert qml(progress,'Accessible.name')==b.diarizationInstall['progressName']
assert qml(progress,'Accessible.role')==qml(progress,'Accessible.ProgressBar')
assert qml(button,'Accessible.name')==b.diarizationInstall['actionName']
point=lambda i:i.mapToScene(QPointF(0,0))
x=point(button).x(); px=point(progress).x()
assert abs(x-px-progress.width()-8)<0.01
row=one('meetingDiarizationMissing')
assert abs(point(button).x()+button.width()-point(row).x()-row.width())<0.01
assert abs(point(one('meetingCardTable')).y()-point(button).y()-button.height()-10)<0.01
for code in ('verifying','installing','downloading'):
    stage(code)
    assert one('meetingDiarizationInstall') is button
    assert button.hasActiveFocus()
    assert point(button).x()==x and point(progress).x()==px
    if code in ('verifying','installing'):
        assert qml(progress,'contentItem.children[0].width')==progress.width()
QTest.keyClick(window,Qt.Key_Escape)
assert not b._diar_downloads.cancel.is_set()
QTest.keyClick(window,Qt.Key_Space)
assert b._diar_downloads.cancel.is_set()
stage('failed:admin')
assert button.hasActiveFocus()
assert qml(one('meetingDiarizationLabel'),'Accessible.role') == (
    qml(progress,'Accessible.AlertMessage'))
assert qml(button,'Accessible.description')==b.diarizationInstall['cardText']
stage('installed')
wait(lambda: not button.isVisible(), 'installed hides action')
assert any(i.hasActiveFocus() for i in walk(one('meetingCardTableFocus')))
assert one('meetingDiarizationMissing').isVisible()
assert not progress.isVisible()
stage('missing'); button.forceActiveFocus(Qt.TabFocusReason)
stage('broken','revoked')
wait(lambda: not button.isVisible(), 'revoked hides action')
assert any(i.hasActiveFocus() for i in walk(one('meetingCardTableFocus')))
window.setWidth(716); one('probeCard').setProperty('width',672)
stage('failed:admin')
label=one('meetingDiarizationLabel')
assert label.property('lineCount')==2, (label.property('lineCount'),label.width())
""",
        dark=dark,
    )


@pytest.mark.parametrize("dark", [False, True])
def test_section_install_row_and_focus(tmp_path: Path, dark: bool) -> None:
    probe(
        tmp_path,
        r"""
b.closeMeeting()
one('probeCard').setVisible(False)
section=window.findChild(QObject,'probeSection'); section.setVisible(True)
QTest.qWait(80)
row=one('meetingDiarizationModels'); button=one('meetingDiarizationModelsAction')
toggle=one('meetingDiarization')
section.setY(-row.mapToItem(section,QPointF(0,0)).y()+80)
assert row.property('label')=='Модели для разделения по голосам'
assert button.property('small') and button.property('variant')=='secondary'
toggle.forceActiveFocus(Qt.TabFocusReason)
QTest.keyClick(window,Qt.Key_Tab)
assert button.hasActiveFocus()
QTest.keyClick(window,Qt.Key_Tab)
assert not button.hasActiveFocus()
button.forceActiveFocus(Qt.TabFocusReason)
for code in ('downloading','verifying','installing'):
    stage(code)
    assert button.hasActiveFocus()
    p=one('meetingDiarizationProgress')
    assert p.height()==4 and p.width()>160
    assert p.property('value')==40
QTest.keyClick(window,Qt.Key_Escape)
assert not b._diar_downloads.cancel.is_set()
stage('installed')
wait(lambda: not button.isVisible(), 'section success')
assert toggle.hasActiveFocus()
stage('missing'); button.forceActiveFocus(Qt.TabFocusReason)
stage('broken','revoked')
assert toggle.hasActiveFocus()
stage('failed:admin')
sub=[i for i in walk(row) if i.objectName()=='settingRowSub'][0]
icon=[i for i in walk(sub) if i.objectName()=='settingRowSubIcon'][0]
assert icon.y()+1==3
assert row.height()==56 # Canonical 55 plus the existing divider.
stage('offline-user')
assert row.isVisible() and not button.isVisible()
b.setSetting('meeting_diarization',False)
QTest.qWait(40)
assert not row.isVisible()
""",
        dark=dark,
        section=True,
    )


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("dpi", [96, 100])
def test_design_carry_panel_caption_and_merge(tmp_path: Path, dark: bool, dpi: int) -> None:
    probe(
        tmp_path,
        r"""
wait(lambda: len(fields())==3, 'speakers')
captions=[i for i in walk(window.contentItem())
          if i.property('text')=='Ваш микрофон — подпись из настроек']
assert len(captions)==1
caption=captions[0]
assert caption.property('lineCount')==1, (caption.width(),caption.property('paintedWidth'))
assert abs(caption.property('font').pixelSize()*caption.scale()-11.5)<0.001
import math
box=caption.parentItem()
assert box.height()==math.ceil(caption.height()*caption.scale())
assert math.ceil(box.parentItem().parentItem().height())==57
fields()[2].forceActiveFocus(Qt.TabFocusReason)
click(one('meetingMergeOpen'))
pop=window.findChild(QObject,'meetingMergePopover')
wait(lambda: pop.property('opened'), 'merge opened')
cancel=one('meetingMergeCancel')
assert cancel.property('visualFocus'), 'cancel focus in both palettes'
assert pop.property('x')==278, pop.property('x')
surfaces=[i for i in walk(pop.property('background')) if i.property('borderWidth')==1]
assert surfaces, 'overlay owns its visible border'
QTest.keyClick(window,Qt.Key_Escape)
window.setWidth(716); one('probeCard').setProperty('width',672)
QTest.qWait(50)
strip=one('meetingCardStrip')
me=[i for i in walk(strip) if i.property('text')=='Я']
assert len(me)==1
from PyQt5.QtQml import QQmlExpression,QQmlEngine
expression=QQmlExpression(QQmlEngine.contextForObject(me[0]),me[0],'color == Theme.fg')
assert expression.evaluate()[0]
""",
        dark=dark,
        install=False,
        dpi=dpi,
    )


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("view", ["recorded", "needs_model"])
def test_install_result_focus_without_table(tmp_path: Path, dark: bool, view: str) -> None:
    probe(
        tmp_path,
        f"from astra_voice.meetings.events import MeetingState\nview={view!r}\n"
        + r"""
mid='2026-11-01_120001-bbbb'
rig.create(mid,MeetingState(view))
b.closeMeeting(); rig.open_card(mid)
m=b._card_meeting()
stage('missing')
assert b.card['showTable'] is False
assert not window.findChild(QObject,'meetingCardTableFocus').isVisible()
button=one('meetingDiarizationInstall')
back=one('meetingCardBack')
for code,reason in (('installed',''),('broken','revoked')):
    stage('missing')
    button.forceActiveFocus(Qt.TabFocusReason)
    assert button.hasActiveFocus()
    stage(code,reason)
    wait(lambda:not button.isVisible(),'action hidden without table')
    assert back.hasActiveFocus()
    assert window.activeFocusItem().isVisible()
""",
        dark=dark,
    )


@pytest.mark.parametrize("dark", [False, True])
def test_install_inline_geometry_round2(tmp_path: Path, dark: bool) -> None:
    probe(
        tmp_path,
        r"""
from PyQt5.QtGui import QFontMetricsF
label=one('meetingDiarizationLabel')
icon=window.findChild(QObject,'meetingDiarizationStatusIcon')
row=one('meetingDiarizationMissing')
if not b.card['marks']:
    assert row.mapToScene(QPointF(0,0)).y()%1==0.5
for code in ('missing','downloading','installed'):
    stage(code)
    assert label.height()==19.5
    metrics=QFontMetricsF(label.property('font'))
    half_leading=(19.5-metrics.height())/2
    css_top=(28.75-19.5)/2 if code!='installed' else 0
    assert abs(label.y()+0.5-css_top-half_leading)<0.01, (code,label.y(),half_leading)
    if code=='installed':
        baseline=label.y()+label.property('baselineOffset')
        assert abs(icon.y()+12-3-1.5-baseline)<0.01
window.setWidth(716); one('probeCard').setProperty('width',672)
stage('failed:admin')
assert label.property('lineCount')==2
assert abs(icon.y()+1-4)<0.01 # Card alert ink aligns with the first capital, unlike section.
capture=window.contentItem().grabToImage()
wait(lambda:not capture.image().isNull(),'card alert raster')
picture=capture.image()
import math
def red_ink_top(item, width):
    point=item.mapToScene(QPointF(0,0))
    left,top=math.floor(point.x()),math.floor(point.y())
    ink=[]
    for y in range(top,math.ceil(point.y()+min(item.height(),19.5))):
        for x in range(left,math.ceil(point.x()+width)):
            colour=picture.pixelColor(x,y)
            if colour.red()>colour.green()+20 and colour.red()>colour.blue()+20:
                ink.append(y)
    assert ink, item.objectName()
    return min(ink)
assert red_ink_top(icon,12)==red_ink_top(label,8)
""",
        dark=dark,
    )
