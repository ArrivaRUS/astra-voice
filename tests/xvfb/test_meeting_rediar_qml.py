"""§Р.1–Р.5, reference 10: isolated QML state/focus/confirmation regressions."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_meeting_s56b_round2 import BASE, FINISH

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]


def probe(tmp_path: Path, body: str, *, section: bool = False, dark: bool = False) -> None:
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
    base = BASE.replace(
        "from PyQt5.QtGui import QFontDatabase",
        "from PyQt5.QtGui import QFontDatabase, QFont\n"
        "QFont.insertSubstitution('PT Mono','DejaVu Sans Mono')",
    )
    if dark:
        base = base.replace(
            "engine.loadData(b",
            "from PyQt5.QtCore import pyqtProperty\n"
            "class Palette(QObject):\n"
            "    dark=pyqtProperty(bool,lambda self:True,constant=True)\n"
            "palette=Palette()\n"
            "engine.rootContext().setContextProperty('themeSource',palette)\nengine.loadData(b",
        )
    if section:
        base = base.replace(
            "engine.loadData(b",
            "engine.rootContext().setContextProperty('meetingBridge',b)\nengine.loadData(b",
        )
        base = base.replace(
            'Button { objectName:"afterCard";',
            'Meetings { objectName:"probeSection"; width:796; visible:false }\n'
            'Button { objectName:"afterCard";',
        )
    result = subprocess.run(
        [sys.executable, "-c", base + body + FINISH, str(REPO), str(tmp_path)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr


MATRIX = r"""
from astra_voice.ui.meeting_texts import redo_presentation, REDO_TEXTS
original=b._redo_snapshot
state=dict(original(b._card_meeting()))
b._redo_snapshot=lambda m: dict(state)
def stage(code, percent=35, installed=False):
    state.update(redo_presentation(code,percent,installed=installed,date='19.11'))
    busy=code in ['queued','wait-memory','running','reconcile','recovery_error']
    keep=code in ['failed','failed-again','cancelled']
    divide=code=='offer' or code in ['kept-7','kept-30','kept-retained']
    key=('cancel' if busy and code!='recovery_error'
         else 'again' if keep else 'divide' if divide else '')
    state.update(busy=busy,canKeep=keep,canStart=not busy,canCancel=busy and code!='recovery_error',
        action=REDO_TEXTS.get(key,''),actionName=REDO_TEXTS.get(key+'Name',''),
        actionDescription=state['text'],keepAction='Оставить так' if keep else '',
        menuVisible=True,menuEnabled=not busy)
    b.changed.emit();QTest.qWait(25)
"""


def test_all_codes_focus_controls_and_narrow_geometry(tmp_path: Path) -> None:
    probe(
        tmp_path,
        MATRIX
        + r"""
card=one('probeCard'); window.setWidth(716);window.setHeight(520);card.setWidth(672)
for code in ['failed','queued','wait-memory','running','reconcile','cancelled','failed-again',
             'no-audio','done','redo-failed','offer','kept-deleted','kept-7','kept-30',
             'kept-retained','kept-pending','recovery_error','']:
    stage(code)
    row=window.findChild(QObject,'meetingDiarizationRedo')
    assert row.property('visible')==bool(state['text']),code
    assert window.findChild(QObject,'meetingRedoLabel').property('text')==state['text'],code
    assert bool(items('meetingRedoProgress'))==state['showProgress'],code
    assert bool(items('meetingRedoKeep'))==state['canKeep'],code
    assert bool(items('meetingRedoIcon'))==bool(state['icon']),code
    if state['icon']:
        assert one('meetingRedoIcon').property('name')==state['icon'],code
    assert bool(items('meetingRedoAction'))==bool(state['action']),code
    if state['action']:
        button=one('meetingRedoAction')
        assert button.width()>=button.implicitWidth(),code
        assert button.x()+button.width()<=672,code
    if state['showProgress']:
        progress=one('meetingRedoProgress')
        assert (progress.width(),progress.height())==(160,4)
    if state['busy']:
        assert not items('meetingSpeakerName')
        assert not items('meetingMergeOpen')
        assert not one('meetingCardNames').isEnabled()
        assert b.card['badge']['kind']=='warning'
        if items('meetingCardNamesLater'):
            assert one('meetingCardNamesLater').property('text')==REDO_TEXTS['namesLater']
stage('failed')
button=one('meetingRedoAction');button.forceActiveFocus(Qt.TabFocusReason)
for code in ['running','reconcile','failed-again']:
    stage(code)
    assert one('meetingRedoAction') is button and button.hasActiveFocus(),code
    assert bool(items('meetingRedoKeep'))==(code=='failed-again')
# Native Tab order, including the two independent sm controls.
one('meetingCardMore').forceActiveFocus(Qt.TabFocusReason)
QTest.keyClick(window,Qt.Key_Tab);assert one('meetingRedoKeep').hasActiveFocus()
QTest.keyClick(window,Qt.Key_Tab);assert button.hasActiveFocus()
QTest.keyClick(window,Qt.Key_Tab);assert one('meetingCardTableFocus').hasActiveFocus()
# Updating the row never recreates or refocuses the table.
resets=QSignalSpy(b.cardRows.modelReset);created=card.property('rowsCreated')
stage('running');stage('reconcile',92)
assert not resets and card.property('rowsCreated')==created
assert one('meetingCardTableFocus').hasActiveFocus()
button.forceActiveFocus(Qt.TabFocusReason)
stage('done');wait(lambda:one('meetingCardTableFocus').hasActiveFocus(),'terminal focus')
""",
    )


def test_dialog_names_escape_and_stale_target(tmp_path: Path) -> None:
    probe(
        tmp_path,
        MATRIX
        + r"""
stage('failed')
state.update(needsWarning=True,warningText=REDO_TEXTS['warningText']
             +' Сейчас заданы: Ирина Соколова, Олег Ли.')
b.changed.emit();QTest.qWait(20)
calls=[]
def start_redo(mid,generation,confirmed=False):
    calls.append((mid,generation,confirmed));stage('running');return True
b._start_redo=start_redo
card=one('probeCard');more=one('meetingCardMore')
def open_dialog():
    click(more);wait(lambda:items('meetingCardRedoMenu'),'menu');QTest.qWait(50)
    click(one('meetingCardRedoMenu'));wait(lambda:items('meetingRedoDialogCancel'),'dialog')
    wait(lambda:one('meetingRedoDialogCancel').hasActiveFocus(),'safe focus')
    assert one('meetingRedoDialogCancel').property('visualFocus')
    assert 'Ирина Соколова, Олег Ли' in one('meetingRedoWarningNames').property('text')
    h=one('meetingRedoWarningTitle');n=one('meetingRedoWarningNames')
    c=one('meetingRedoWarningCaption')
    assert abs(h.property('font').pixelSize()*h.scale()-13.5)<0.01
    assert abs(h.property('lineHeight')*h.scale()-20.25)<0.01
    assert abs(h.parentItem().height()-h.property('lineCount')*20.25)<0.01
    assert abs(n.y()-h.parentItem().height()-8)<0.01
    assert abs(c.y()-n.y()-n.height()-8)<0.01
open_dialog()
QTest.keyClick(window,Qt.Key_Escape)
wait(lambda:not items('meetingRedoDialogCancel'),'escape')
assert more.hasActiveFocus() and not calls
open_dialog()
QTest.keyClick(window,Qt.Key_Return)
wait(lambda:not items('meetingRedoDialogCancel'),'safe enter')
assert not calls
for change in ['generation','meeting']:
    open_dialog()
    dialog=window.findChild(QObject,'meetingRediarizeDialog')
    if change=='generation':state['expectedGeneration']+=1
    else: state['meetingId']='other-meeting'
    b.changed.emit();QTest.qWait(30)
    QMetaObject.invokeMethod(dialog,'submit')
    assert not calls and not dialog.property('visible')
# Accepted confirmation uses exactly the captured pair.
open_dialog()
expected=(state['meetingId'],state['expectedGeneration'],True)
click(one('meetingRedoDialogConfirm'))
wait(lambda:len(calls)==1,'confirmed')
assert calls==[expected]
wait(lambda:one('meetingRedoAction').hasActiveFocus(),'accepted focus')
""",
    )


def test_installed_offer_has_no_empty_frame(tmp_path: Path) -> None:
    probe(
        tmp_path,
        r"""
from astra_voice.meetings.events import ProcessingStatus
from astra_voice.ui.meeting_texts import REDO_TEXTS
m=b._card_meeting();m.transcription.diarization=False
m.processing_status=ProcessingStatus.SUCCEEDED
m.audio.retain_days=7;m.audio.present=True
m.transcription.finished_at='2026-11-12T12:00:00Z'
rig.store.write(m.id,'meeting.json',m,writer=Writer.GUI)
b._diar_models_provider=lambda: None
b._stored.meeting_diarization=True;b.refreshDiarizationModels()
b.changed.emit();QTest.qWait(25)
assert one('meetingDiarizationMissing').isVisible()
assert not items('meetingDiarizationRedo')
b._diar_models_provider=lambda: {'segmentation_dir':'/trusted/seg','embedder_dir':'/trusted/emb'}
b._diar_card_installed=m.id
b._diar_code='installed';b._publish_diar_install()
# Snapshot is switched atomically before the next rendered frame.
assert not b.card['diarizationInstallVisible']
assert b.card['diarizationRedo']['code']=='offer'
QTest.qWait(25)
assert not items('meetingDiarizationMissing')
assert one('meetingRedoLabel').property('text')==REDO_TEXTS['offer-installed']
assert one('meetingRedoIcon').property('name')=='check'
assert one('meetingRedoAction').property('text')=='Разделить по голосам'
""",
    )


def test_wide_panel_accessibility_and_throttled_progress(tmp_path: Path) -> None:
    probe(
        tmp_path,
        MATRIX
        + r"""
from PyQt5.QtQml import QQmlExpression, QQmlEngine
context=QQmlEngine.contextForObject(one('probeCard'))
def qml(item,expression):
    exp=QQmlExpression(context,item,expression)
    value,undefined=exp.evaluate()
    assert not exp.hasError(),exp.error().toString()
    return value
stage('failed')
button=one('meetingRedoAction')
assert qml(button,'Accessible.name')==REDO_TEXTS['againName']
assert qml(button,'Accessible.description')==state['text']
keep=one('meetingRedoKeep')
assert qml(keep,'Accessible.description')==state['keepDescription']
button.forceActiveFocus(Qt.TabFocusReason)
stage('running')
assert not fields() and not items('meetingMergeOpen')
assert one('meetingCardNamesLater').property('text')==REDO_TEXTS['namesLater']
caption=one('meetingCardNamesLater')
assert abs(caption.property('font').pixelSize()*caption.scale()-11.5)<0.001
assert [i.property('text') for i in items('meetingParticipantLabel')]==['Я','Собеседники']
assert qml(button,'Accessible.name')==REDO_TEXTS['cancelName']
p=one('meetingRedoProgress')
assert qml(p,'Accessible.role')==qml(p,'Accessible.ProgressBar')
assert qml(p,'Accessible.name')==REDO_TEXTS['progressName']
assert p.property('from')==0 and p.property('to')==100
badge_mono=one('meetingCardBadge').findChild(QObject,'meetingBadgeMono')
assert badge_mono.isVisible() and badge_mono.property('font').family()=='PT Mono'
v=p.property('value');stage('reconcile',92)
assert p.property('value')==v
wait(lambda:p.property('value')==92,'throttled value')
label=one('meetingRedoLabel')
assert qml(label,'Accessible.role')==qml(label,'Accessible.StaticText')
state['canCancel']=False;b.changed.emit();QTest.qWait(30)
stage('failed-again')
assert button.hasActiveFocus()
assert qml(label,'Accessible.role')==qml(label,'Accessible.AlertMessage')
# Escape never calls the cancellation slot.
cancelled=[]
rig.jobs.cancel=lambda mid:cancelled.append(mid)
stage('running');button.forceActiveFocus(Qt.TabFocusReason)
QTest.keyClick(window,Qt.Key_Escape);assert not cancelled
""",
    )


def test_list_menu_dialog_and_disabled_redo(tmp_path: Path) -> None:
    probe(
        tmp_path,
        MATRIX
        + r"""
stage('failed')
state.update(needsWarning=True,warningText='Сейчас заданы: Ирина Соколова, Олег Ли.')
b.closeMeeting()
one('probeCard').setVisible(False)
section=window.findChild(QObject,'probeSection');section.setVisible(True)
rig.refresh()
QTest.qWait(60)
more=one('meetingMore')
section.setY(-more.mapToItem(section,QPointF(0,0)).y()+100)
click(more);QTest.qWait(70)
assert one('meetingListRedoMenu').isEnabled()
click(one('meetingListRedoMenu'));QTest.qWait(60)
while rig.tasks:rig.tasks.pop(0)()
b._card_poll();QTest.qWait(80)
assert not section.property('cardOpen')
wait(lambda:items('meetingRedoDialogCancel'),'list dialog')
assert one('meetingRedoDialogCancel').hasActiveFocus()
QTest.keyClick(window,Qt.Key_Escape);QTest.qWait(50)
assert not b.cardOpen and one('meetingMore').hasActiveFocus()
stage('running')
click(one('meetingMore'));QTest.qWait(70)
assert not one('meetingListRedoMenu').isEnabled()
assert any(i.property('text')=='Имена участников' and not i.isEnabled()
           for i in items('meetingListMenuAction'))
QTest.keyClick(window,Qt.Key_Escape)
""",
        section=True,
    )


@pytest.mark.parametrize("button_name", ["meetingRedoDialogCancel", "meetingRedoDialogConfirm"])
def test_long_warning_keyboard_scroll_and_safe_reopen(tmp_path: Path, button_name: str) -> None:
    probe(
        tmp_path,
        MATRIX
        + "\nbutton_name="
        + repr(button_name)
        + r"""
window.setHeight(588)
stage('failed')
state.update(needsWarning=True,warningText=REDO_TEXTS['warningText']+' Сейчас заданы: '+
    ', '.join('Участник с очень длинными фамилией и именем '+str(i) for i in range(40))+'.')
b.changed.emit();QTest.qWait(25)
card=one('probeCard');more=one('meetingCardMore')
for opening in range(2):
    QMetaObject.invokeMethod(card,'requestRedo',Qt.DirectConnection,
        __import__('PyQt5.QtCore',fromlist=['Q_ARG']).Q_ARG('QVariant',None))
    wait(lambda:items('meetingRedoDialogCancel'),'long dialog')
    cancel=one('meetingRedoDialogCancel')
    wait(lambda:cancel.hasActiveFocus() and cancel.property('visualFocus'),'visible safe focus')
    if not opening:
        QTest.keyClick(window,Qt.Key_Escape)
        wait(lambda:not items('meetingRedoDialogCancel'),'close first')
dialog=window.findChild(QObject,'meetingRediarizeDialog')
body=dialog.property('contentItem');end=body.property('contentHeight')-body.height()
assert end>400
button=one(button_name);button.forceActiveFocus(Qt.TabFocusReason)
for key in [Qt.Key_PageDown,Qt.Key_End,Qt.Key_Down]:
    QTest.keyClick(window,Qt.Key_Home);assert body.property('contentY')==0
    QTest.keyClick(window,key)
    assert body.property('contentY')>0,(button_name,key)
    for step in range(100):
        if body.property('contentY')>=end-0.1:break
        QTest.keyClick(window,key)
    assert abs(body.property('contentY')-end)<0.1,(button_name,key)
    QTest.keyClick(window,Qt.Key_Home);assert body.property('contentY')==0
other='meetingRedoDialogConfirm' if button_name.endswith('Cancel') else 'meetingRedoDialogCancel'
QTest.keyClick(window,Qt.Key_Tab);assert one(other).hasActiveFocus()
QTest.keyClick(window,Qt.Key_Tab);assert button.hasActiveFocus()
# Both labels use the same layout, hence an identical baseline in scene space.
a=one('meetingRedoCancelLabel');c=one('meetingRedoConfirmLabel')
assert abs(a.mapToScene(QPointF(0,a.property('baselineOffset'))).y()-
           c.mapToScene(QPointF(0,c.property('baselineOffset'))).y())<0.01
QTest.keyClick(window,Qt.Key_Escape)
""",
    )


def test_list_redo_read_failure_reveals_card_and_retry(tmp_path: Path) -> None:
    probe(
        tmp_path,
        r"""
b._diar_models_provider=lambda: {'segmentation_dir':'/trusted/seg','embedder_dir':'/trusted/emb'}
b._stored.meeting_diarization=True;b.refreshDiarizationModels()
b.closeMeeting();one('probeCard').setVisible(False)
section=window.findChild(QObject,'probeSection');section.setVisible(True)
rig.refresh();QTest.qWait(50)
more=one('meetingMore');section.setY(-more.mapToItem(section,QPointF(0,0)).y()+100)
original_read=rig.store.read
broken=[True]
def read(mid,filename):
    if broken[0] and filename=='transcript.json':raise MeetingStoreError('read-failed')
    return original_read(mid,filename)
rig.store.read=read
click(more);QTest.qWait(50)
assert one('meetingListRedoMenu').isEnabled()
click(one('meetingListRedoMenu'));QTest.qWait(30)
while rig.tasks:rig.tasks.pop(0)()
b._card_poll();QTest.qWait(60)
assert b.card['view']=='read-error' and b.cardOpen
assert not section.property('listRedoActive') and section.property('cardOpen')
assert one('meetingCardReadError').isVisible()
assert one('meetingCardReadRetry').isVisible()
# Opening the same meeting again does not leave it hidden behind the list.
mid=b.card['id'];b.openMeeting(mid)
while rig.tasks:rig.tasks.pop(0)()
b._card_poll();QTest.qWait(40)
assert section.property('cardOpen') and one('meetingCardReadRetry').isVisible()
broken[0]=False
click(one('meetingCardReadRetry'))
while rig.tasks:rig.tasks.pop(0)()
b._card_poll();QTest.qWait(40)
assert b.card['view']=='ready' and section.property('cardOpen')
""",
        section=True,
    )


@pytest.mark.parametrize("dark", [False, True])
def test_review_inline_geometry_subtitle_caption_and_menu(tmp_path: Path, dark: bool) -> None:
    probe(
        tmp_path,
        MATRIX
        + "\nwant_border="
        + repr("#2c3545" if dark else "#d7deea")
        + r"""
from PyQt5.QtGui import QFontMetricsF
stage('failed')
label=one('meetingRedoLabel');button=one('meetingRedoAction')
line_top=(max(label.height(),button.height())-label.height())/2
half=max(0,(19.5-QFontMetricsF(label.property('font')).height())/2)
assert abs(label.y()-(line_top+half-0.5))<0.01
assert abs(one('meetingRedoIcon').y()-(line_top+3))<0.01
stage('offer',installed=True)
label=one('meetingRedoLabel');icon=one('meetingRedoIcon')
assert icon.property('name')=='check'
assert abs(icon.y()-(label.y()+label.property('baselineOffset')-12+4.5))<0.01
stage('running')
caption=one('meetingRedoNamesCaption');text=one('meetingCardNamesLater')
assert text.y()==9 and text.x()==12
assert abs(text.property('lineHeight')*text.scale()-17.25)<0.01
assert caption.childItems()[0].height()==1
stage('failed')
click(one('meetingCardMore'));QTest.qWait(70)
menu=window.findChild(QObject,'meetingCardMenu')
assert menu.property('padding')==6
assert menu.property('height')==147,menu.property('height')
separator=one('meetingRedoMenuSeparator')
assert separator.height()==9
assert separator.property('contentItem').height()==1
assert separator.property('contentItem').width()==212
menu_content=menu.property('contentItem')
for name in ['meetingCardRenameAction','meetingCardNamesAction',
             'meetingCardRedoMenu','meetingCardDeleteAction','meetingRedoMenuSeparator']:
    item=one(name).property('contentItem')
    y=item.mapToItem(menu_content,QPointF(0,0)).y()
    assert abs(y-round(y))<0.01,(name,y)
surface=menu.property('background').childItems()[0]
assert surface.property('borderColor').name()==want_border
assert surface.property('shadowRadius')==34 and surface.property('shadowY')==12
QTest.keyClick(window,Qt.Key_Escape)
window.setWidth(716);card=one('probeCard');card.setWidth(672)
# A fixed two-line subtitle at the real 900-window content width.
card_data=dict(b.card)
card_data['subtitle']={'prefix':'09.10.2026 · 10:00–11:12 · ',
    'mono':'1:12:40','suffix':' · Звонок в DION · звук хранится до разделения'}
b._card_cache=card_data;b.cardChanged.emit()
QTest.qWait(30)
subtitle=one('meetingCardSubtitle')
visible=[c for c in subtitle.childItems() if c.isVisible() and c.property('text')]
assert ''.join(c.property('text') for c in visible)==''.join(
    b.card['subtitle'].get(k,'') for k in ['prefix','mono','suffix'])
assert all('span' not in c.property('text') for c in visible)
assert subtitle.height()==39,(subtitle.height(),subtitle.width(),
    [(c.property('text'),c.x(),c.y(),c.width()) for c in visible])
assert visible[-1].property('text')=='разделения'
clock=next(c for c in visible if c.property('font').family()=='PT Mono')
assert clock.property('text')=='1:12:40'
separator_word=visible[visible.index(clock)+1]
assert separator_word.property('text')==' · '
assert separator_word.property('font').family()=='PT Root UI'
assert visible[-1].y()>0
assert abs(visible[-1].mapToScene(QPointF(0,0)).y()-
           visible[0].mapToScene(QPointF(0,0)).y()-20)<0.01
row=one('meetingDiarizationRedo')
assert abs(row.mapToItem(card,QPointF(0,0)).y()-
           round(row.mapToItem(card,QPointF(0,0)).y()))<0.01
""",
        dark=dark,
    )


def test_r4_no_button_rows_match_install_line_height(tmp_path: Path) -> None:
    probe(
        tmp_path,
        MATRIX
        + r"""
for code in ['done','no-audio','redo-failed','kept-deleted']:
    stage(code)
    label=one('meetingRedoLabel');row=one('meetingDiarizationRedo')
    table=one('meetingCardTable')
    assert not items('meetingRedoAction') and not items('meetingRedoKeep')
    assert abs(row.height()-label.height()-10)<0.01,code
    assert row.height()==29.5,code
    assert abs(table.mapToScene(QPointF(0,0)).y()-row.mapToScene(QPointF(0,0)).y()
               -row.height())<0.01,code
""",
    )
