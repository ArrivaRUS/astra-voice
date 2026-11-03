"""Card A «Документ» (E13.M4) in QML: states, MN-1, incremental rows, keyboard; offscreen.

A separate process keeps QML engine teardown out of unrelated Qt tests.
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
from PyQt5.QtCore import QObject, QPointF, Qt, QUrl
from PyQt5.QtGui import QFontDatabase, QGuiApplication
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QTest
repo = Path(sys.argv[1])
sys.path.insert(0, str(repo))
sys.path.insert(0, str(repo / 'src'))
from tools import render_meeting_card as rc
from astra_voice.meetings.model import Transcript, Utterance
from astra_voice.meetings.templates import visible_utterances
from astra_voice.ui.icons import install_icon_provider
rc.set_timezone()
QQuickWindow.setSceneGraphBackend('software')
app = QGuiApplication([])
for path in (repo / 'tests/fixtures/fonts/pt-root-ui').glob('*.ttf'):
    assert QFontDatabase.addApplicationFont(str(path)) >= 0
bridge = rc.make_bridge()
engine = QQmlApplicationEngine()
install_icon_provider(engine)
warnings = []
engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
engine.rootContext().setContextProperty('cardBridge', bridge)
engine.loadData(b'''import QtQuick 2.15
import QtQuick.Window 2.15
import "qml"
import "qml/sections"
Window { width: 840; height: 900; visible: true
    MeetingCard { objectName: "probeCard"; x: 22; y: 12; width: 796; bridge: cardBridge } }''',
    QUrl.fromLocalFile(str(repo / '_probe-card.qml')))
assert engine.rootObjects(), warnings
window = engine.rootObjects()[0]
window.requestActivate()
QTest.qWait(50)
card = window.findChild(QQuickItem, 'probeCard')
states = rc.states()

def show(name, **changes):
    fixture = {**states[name], **changes}
    bridge.show(fixture['meeting'], fixture.get('utterances'), snapshot=fixture.get('snapshot'),
                failure=fixture.get('failure', ''))
    QTest.qWait(30)

def walk(item):
    # Repeater delegates are visual children only: walk childItems, not QObject children.
    yield item
    for child in item.childItems():
        yield from walk(child)

def named(name):
    return [item for item in walk(card) if item.objectName() == name]

def one(name):
    found = named(name) or [card.findChild(QObject, name)]
    assert found[0] is not None, name
    return found[0]

def visible(name):
    return one(name).property('visible') is True

def rows():
    return named('meetingCardRow')

# Ready, 1024: table + panel «Участники» 232; no strip, no tail, no panel message.
show('ready-1024')
assert visible('meetingCardTable') and visible('meetingCardParticipants')
assert not visible('meetingCardStrip') and not visible('meetingCardTail')
assert not visible('meetingCardPanel') and not visible('meetingCardProgress')
assert visible('meetingCardSave') and visible('meetingCardMore')
assert one('meetingCardParticipants').property('width') == 232
assert len(rows()) == bridge.cardRows.rowCount() == 12  # 9 replies + 3 timed marks
assert one('meetingCardBadge').property('text') == 'Готово'
# Column widths 76 / 124 / rest (tokens.md §3.3): text column 302 at 1024.
speech = [item for item in named('meetingCardText') if item.property('visible')]
assert speech and all(abs(item.property('width') - 302) < 0.01 for item in speech)
assert [i.property('text') for i in named('meetingCardMarkText') if i.property('text')] == [
    'Ваш микрофон не записывался: 0:12:03–0:12:40',
    'Пауза 0:31:10 · 2 мин 52 с',
    'Пауза: экран был заблокирован 0:50:00 · 12 мин',
]

# 900: strip above a full-width table, panel hidden (spec §5.2, point 760).
card.setProperty('width', 672)
QTest.qWait(30)
assert visible('meetingCardStrip') and not visible('meetingCardParticipants')
assert abs(one('meetingCardTable').property('width') - 672) < 0.01
assert one('meetingCardNames').property('enabled') is False
card.setProperty('width', 796)
QTest.qWait(30)

# MN-1: reply text is literal; echo and empty replies never become rows.
raw = '<b>Жирный</b> & <img src="file:///etc/passwd"> <a href="x">ссылка</a>'
utterances = [
    Utterance(id='u00000', seq=0, start_ms=1000, end_ms=2000, text=raw, speaker='others',
              track='far'),
    Utterance(id='u00001', seq=1, start_ms=3000, end_ms=4000, text='эхо', speaker='me', echo=True),
    Utterance(id='u00002', seq=2, start_ms=5000, end_ms=6000, text='  ', speaker='me'),
    Utterance(id='u00003', seq=3, start_ms=7000, end_ms=8000, text='Я тут.', speaker='me'),
]
shown = visible_utterances(Transcript(meeting_id=rc.MEETING_ID, utterances=utterances))
show('ready-1024', utterances=shown)
texts = [item for item in named('meetingCardText') if item.property('visible')]
literal = [item for item in texts if item.property('text') == raw]
assert len(literal) == 1 and literal[0].property('textFormat') == 0  # Text.PlainText
assert literal[0].property('lineCount') >= 1
assert [item.property('text') for item in texts] == [raw, 'Я тут.']
assert not visible('meetingCardTail')

# Transcribing (В4): progress card, tail row, no save/menu, names later; rows appended in place.
show('transcribing-1024')
assert visible('meetingCardProgress') and visible('meetingCardTail')
assert visible('meetingCardNamesLater')
assert not visible('meetingCardSave') and not visible('meetingCardMore')
assert one('meetingCardTailText').property('text') == 'Дальше — по мере расшифровки'
assert one('meetingCardProgressDone').property('text') == '0:29:04'
assert one('meetingCardBadge').property('text') == 'Расшифровка · 40 %'
first = rows()[0]
created = card.property('rowsCreated')
before = bridge.cardRows.rowCount()
for seq in range(7, 207):
    bridge.builder.extend([Utterance(id=f'u{seq:05d}', seq=seq, start_ms=seq * 4000,
                                     end_ms=seq * 4000 + 500, text=f'реплика {seq}',
                                     speaker='others', track='far')])
QTest.qWait(50)
added = bridge.cardRows.rowCount() - before
assert added == 201, added  # 200 replies + the microphone mark at 0:12:03
assert card.property('rowsCreated') == created + added
# The first delegate survives the appends (no reset of the table).
assert rows()[0].property('accessibleName') == first.property('accessibleName')
assert len(rows()) == bridge.cardRows.rowCount()

# Message panels instead of the table (06, spec §4.7).
expected = {
    'cancelled': ('Расшифровка отменена', ['Расшифровать', 'Удалить встречу…']),
    'no-model': ('Для расшифровки встреч нужна модель GigaAM v3',
                 ['Установить', 'Установить из файла…']),
    'wait-memory': ('Расшифровка подождёт: сейчас мало свободной памяти', ['Отменить']),
    'error': ('Распознавание на этом компьютере не работает', ['Повторить', 'Подробнее']),
    'interrupted': ('Запись встречи прервалась. Сохранено ', ['Расшифровать', 'Удалить…']),
    'empty': ('В записи не нашлось речи', ['Удалить встречу…']),
}
for name, (title, buttons) in expected.items():
    show(name)
    assert visible('meetingCardPanel'), name
    assert not visible('meetingCardTable') and not visible('meetingCardTail'), name
    assert not visible('meetingCardSave'), name
    assert one('meetingCardPanelTitle').property('text') == title, name
    labels = [b.property('text') for b in named('meetingCardPanelButton')
              if b.property('visible')]
    assert labels == buttons, (name, labels)
show('no-model')
install = [b for b in named('meetingCardPanelButton') if b.property('text') == 'Установить'][0]
bridge.actions.clear()
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                 install.mapToScene(QPointF(install.width() / 2, install.height() / 2)).toPoint())
assert bridge.actions == ['Установить'], bridge.actions

# D4 «Сохранить как — <название>» (M4: «Стенограмма», .md/.txt); the file is written in Python.
show('ready-1024')
save = one('meetingCardSave')
QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                 save.mapToScene(QPointF(save.width() / 2, save.height() / 2)).toPoint())
QTest.qWait(50)
dialog = window.findChild(QObject, 'meetingSaveDialog')
assert dialog.property('opened') is True
assert dialog.property('heading') == 'Сохранить как — Планёрка по бюджету'
assert dialog.property('width') == 760
assert dialog.property('previewRows').toVariant().__len__() == 6
fmt = window.findChild(QObject, 'meetingSaveFormat')
fmt.setProperty('currentIndex', 1)
bridge.save_result = 'error'
bridge.save_error = 'Не удалось сохранить файл: папка недоступна для записи'
dialog.accept()
QTest.qWait(80)
assert bridge.saves == ['txt']
assert dialog.property('opened') is True  # back to D4 with the error line
error = window.findChild(QObject, 'meetingSaveError')
assert error.property('visible') and error.property('body') == bridge.save_error
bridge.save_result = 'saved'
dialog.accept()
QTest.qWait(80)
assert bridge.saves == ['txt', 'txt'] and dialog.property('opened') is False
bridge.save_error = ''
# The chosen file exists (K5): the card's own question, focus on «Отмена»; Esc → D4.
replace = window.findChild(QObject, 'meetingReplaceDialog')
bridge.save_result, bridge.replace_file = 'exists', 'итог.md'
dialog.open()
QTest.qWait(50)
dialog.accept()
QTest.qWait(80)
assert replace.property('opened') is True and dialog.property('opened') is False
assert replace.property('message') == 'Файл «итог.md» уже существует. Заменить?'
assert window.activeFocusItem().property('text') == 'Отмена'
QTest.keyClick(window, Qt.Key_Escape)
QTest.qWait(80)
assert bridge.actions[-1] == 'keep' and replace.property('opened') is False
assert dialog.property('opened') is True
bridge.replace_file = 'итог.md'
dialog.accept()
QTest.qWait(80)
replace.accept()  # «Заменить»
QTest.qWait(80)
assert bridge.actions[-1] == 'replace'
assert replace.property('opened') is False and dialog.property('opened') is False
bridge.save_result = 'saved'

# Keyboard (spec §5.2/§7): «← Все встречи» → «Сохранить как…» → «⋯» → table (one stop, arrows).
show('ready-1024')
back = one('meetingCardBack')
back.forceActiveFocus(Qt.TabFocusReason)
assert back.property('activeFocus')
QTest.keyClick(window, Qt.Key_Tab)
assert one('meetingCardSave').property('activeFocus')
QTest.keyClick(window, Qt.Key_Tab)
more = one('meetingCardMore')
assert more.property('activeFocus')
QTest.keyClick(window, Qt.Key_Tab)
focused = [item for item in rows() if item.property('activeFocus')]
assert len(focused) == 1 and focused[0] is rows()[0]
QTest.keyClick(window, Qt.Key_Down)
assert rows()[1].property('activeFocus') and card.property('currentRow') == 1
QTest.keyClick(window, Qt.Key_Backtab)
assert more.property('activeFocus')
# Esc closes the «⋯» menu and returns focus to the button that opened it.
QTest.keyClick(window, Qt.Key_Space)
menu = one('meetingCardMenu')
QTest.qWait(30)
assert menu.property('opened') is True
QTest.keyClick(window, Qt.Key_Escape)
QTest.qWait(30)
assert menu.property('opened') is False and more.property('activeFocus')
QTest.keyClick(window, Qt.Key_Return)
QTest.keyClick(window, Qt.Key_Escape)
back.forceActiveFocus(Qt.TabFocusReason)
QTest.keyClick(window, Qt.Key_Return)
assert bridge.actions[-1] != 'close'  # back is a signal to the section, not a bridge call
assert not warnings, warnings
print('ok')
"""


def test_meeting_card_qml_states_rows_and_keyboard() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"}
    }
    env.update(
        QT_QPA_PLATFORM="offscreen",
        QT_QUICK_BACKEND="software",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
    )
    result = subprocess.run(
        [sys.executable, "-c", QML_PROBE, str(REPO)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok")


MAIN_PROBE = r"""
import sys
from pathlib import Path
from PyQt5.QtCore import QMetaObject, QObject, QPointF, Qt, QUrl
from PyQt5.QtGui import QFontDatabase, QGuiApplication
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtQuick import QQuickItem, QQuickWindow
from PyQt5.QtTest import QTest
repo, root = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(repo))
sys.path.insert(0, str(repo / 'src'))
sys.path.insert(0, str(repo / 'tests'))
sys.path.insert(0, str(repo / 'tests/xvfb'))
from tools import render_meeting_card as rc
from astra_voice.core.settings import Settings
from astra_voice.meetings.model import Transcript
from astra_voice.meetings.store import MeetingStore, Writer
from astra_voice.ui.icons import install_icon_provider
from astra_voice.ui.meeting_bridge import MeetingBridge
from test_meetings_section import ThemeSource
rc.set_timezone()
store = MeetingStore(root / 'meetings')
store.create(rc.fixture_meeting())
store.write(rc.MEETING_ID, 'transcript.json',
            Transcript(meeting_id=rc.MEETING_ID, utterances=rc.fixture_utterances()),
            writer=Writer.TRANSCRIBE)
QQuickWindow.setSceneGraphBackend('software')
app = QGuiApplication([])
for path in (repo / 'tests/fixtures/fonts/pt-root-ui').glob('*.ttf'):
    assert QFontDatabase.addApplicationFont(str(path)) >= 0
bridge = MeetingBridge(Settings(meetings_enabled=True), store=store, known_apps=())
engine = QQmlApplicationEngine()
warnings = []
engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
engine.rootContext().setContextProperty('themeSource', ThemeSource(False))
engine.rootContext().setContextProperty('meetingBridge', bridge)
install_icon_provider(engine)
engine.load(QUrl.fromLocalFile(str(repo / 'qml/Main.qml')))
assert engine.rootObjects(), warnings
window = engine.rootObjects()[0]
window.show()
sidebar = window.findChild(QObject, 'settingsSidebar')
assert sidebar.setProperty('currentIndex', 3)

def wait(check, label):
    for _ in range(200):
        QTest.qWait(20)
        if check():
            return
    raise AssertionError(label)

def walk(node):
    yield node
    for child in node.childItems():
        yield from walk(child)

def item(name):
    # Repeater delegates are visual children only.
    return next((node for node in walk(window.contentItem()) if node.objectName() == name), None)

wait(lambda: len(bridge.meetings) == 1 and item('meetingRowOpen') is not None, 'list')
header = item('sectionHeader')
assert header.isVisible()
def open_card():
    # Under CPU load (CI) the first synthetic click right after the section loads
    # is occasionally dropped: click the row again until the card opens, same budget.
    opener = item('meetingRowOpen')
    if not bridge.cardOpen and opener is not None:
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                         opener.mapToScene(QPointF(40, opener.height() / 2)).toPoint())
    return bridge.cardOpen and bridge.card.get('view') == 'ready'

wait(open_card, 'card')
card = item('meetingCard')
assert card.isVisible() and not header.isVisible()
assert not item('meetingList').isVisible()
assert bridge.cardRows.rowCount() == 12
wait(lambda: item('meetingCardBack').hasActiveFocus(), 'focus on «Все встречи»')
QTest.keyClick(window, Qt.Key_Return)
wait(lambda: not bridge.cardOpen, 'back')
assert header.isVisible() and item('meetingList').isVisible() and not card.isVisible()
# Leaving the section closes an open card.
bridge.openMeeting(rc.MEETING_ID)
wait(lambda: bridge.cardOpen, 'reopen')
assert sidebar.setProperty('currentIndex', 0)
wait(lambda: not bridge.cardOpen, 'closed with the section')
# List row «Сохранить как…»: the card opens and D4 follows once the text is loaded.
assert sidebar.setProperty('currentIndex', 3)
wait(lambda: item('meetingRowOpen') is not None, 'list again')
bridge.requestMeetingAction(rc.MEETING_ID, 'Сохранить как…')
save_dialog = window.findChild(QObject, 'meetingSaveDialog')
wait(lambda: save_dialog.property('opened') is True, 'D4 from the list')
assert bridge.card.get('view') == 'ready'
QMetaObject.invokeMethod(save_dialog, 'close')
wait(lambda: save_dialog.property('opened') is False, 'D4 closed')
bridge.shutdown()
window.hide()
from PyQt5 import sip
sip.delete(engine)
assert not warnings, warnings
print('ok')
"""


def test_meeting_card_opens_from_the_list_inside_the_settings_window(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DISPLAY", "WAYLAND_DISPLAY", "QT_ACCESSIBILITY", "AT_SPI_BUS_ADDRESS"}
    }
    env.update(
        QT_QPA_PLATFORM="offscreen",
        QT_QUICK_BACKEND="software",
        DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
    )
    result = subprocess.run(
        [sys.executable, "-c", MAIN_PROBE, str(REPO), str(tmp_path)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok")
