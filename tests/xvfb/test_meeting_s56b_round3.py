"""Design round 3: actual Qt typography, geometry and layered borders (offscreen)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_meeting_s56b_round2 import BASE, FINISH

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]


def run_probe(tmp_path: Path, body: str, *, dark: bool = False) -> None:
    base = BASE
    if dark:
        base = base.replace(
            "engine.loadData(b",
            """from PyQt5.QtCore import pyqtProperty
class DarkTheme(QObject):
    dark = pyqtProperty(bool, lambda self: True, constant=True)
palette = DarkTheme()
engine.rootContext().setContextProperty('themeSource', palette)
engine.loadData(b""",
        )
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
        [sys.executable, "-c", base + body + FINISH, str(REPO), str(tmp_path)],
        env=env,
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_fonts_and_merge_fit_the_viewport(tmp_path: Path) -> None:
    run_probe(
        tmp_path,
        r"""
from PyQt5.QtGui import QFont
wait(lambda: len(fields())==3, 'fields')
digits = [i for i in walk(window.contentItem()) if i.parentItem() is not None
          and i.parentItem().property('num') == '1' and i.property('text') == '1']
assert digits and all(i.property('font').weight() == QFont.Bold for i in digits)
assert all(i.property('font').weight() == QFont.Medium for i in items('meetingCardWho'))
window.setHeight(552)
QTest.qWait(50)
fields()[2].forceActiveFocus(Qt.TabFocusReason)
click(one('meetingMergeOpen'))
pop = window.findChild(QObject,'meetingMergePopover')
wait(lambda: pop.property('opened'), 'merge')
assert pop.property('height') == 216, pop.property('height')
wait(lambda: 0 <= pop.property('y') <= window.height()-pop.property('height'),
     'popover stays inside resized viewport')
button = one('meetingMergeConfirm')
assert button.mapToScene(QPointF(0,button.height())).y() <= window.height()
QTest.keyClick(window,Qt.Key_Escape)
wait(lambda: not pop.property('opened'), 'closed')
""",
    )


def test_progress_suffix_disabled_field_border_and_install_alignment(tmp_path: Path) -> None:
    run_probe(
        tmp_path,
        r"""
wait(lambda: len(fields())==3, 'fields')
m = b._card_meeting(); m.state='transcribing'
b._card_builder.speakers=None
b._card_builder.load(transcript.utterances,complete=False)
b.changed.emit()
QTest.qWait(50)
assert one('meetingProgressNames').property('text') == ' · имена спикеров уточняются до конца'
field = fields()[0]
assert not field.isEnabled()
grab = window.contentItem().grabToImage()
wait(lambda: not grab.image().isNull(), 'software capture')
image = grab.image()
point = field.mapToScene(QPointF(0,field.height()/2))
colour=image.pixelColor(round(point.x()),round(point.y())).name()
assert colour == '#21252f', (colour,point.x(),point.y())
b.closeMeeting()
m=rig.meeting(); m.transcription.diarization=False
rig.store.write(m.id,'meeting.json',m,writer=Writer.GUI)
b._stored.meeting_diarization=True
rig.open_card()
QTest.qWait(40)
row=one('meetingDiarizationMissing'); button=one('meetingDiarizationInstall')
assert abs(button.x()+button.width()-row.width()) < 0.01
button_bottom=button.mapToScene(QPointF(0,button.height())).y()
table_top=one('meetingCardTable').mapToScene(QPointF(0,0)).y()
assert abs(table_top-button_bottom-10) < 0.01, (table_top,button_bottom)
""",
        dark=True,
    )


def test_drawer_foreground_border_and_shadow_canvas_extent(tmp_path: Path) -> None:
    run_probe(
        tmp_path,
        r"""
wait(lambda: len(fields())==3, 'fields')
window.setWidth(716); window.setHeight(520)
one('probeCard').setProperty('width',672)
QTest.qWait(30)
click(one('meetingCardNames'))
drawer=window.findChild(QObject,'meetingParticipantsDrawer')
wait(lambda: drawer.property('opened'), 'drawer')
border=one('meetingParticipantsBorder')
assert border.width()==1 and border.height()==520
grab=window.contentItem().grabToImage()
wait(lambda: not grab.image().isNull(), 'drawer software capture')
image=grab.image()
point=border.mapToScene(QPointF(0,0))
for y in (1,100,300,500):
    assert image.pixelColor(round(point.x()),round(point.y()+y)).name() == '#d7deea'
shadows=[i for i in walk(window.contentItem()) if i.objectName()=='overlayShadowCanvas'
         and i.parentItem().property('shadowX') == -12]
assert len(shadows)==1
assert shadows[0].x()==-46 and shadows[0].width()==380
QTest.keyClick(window,Qt.Key_Escape)
wait(lambda: not drawer.property('opened'), 'closed')
""",
    )
