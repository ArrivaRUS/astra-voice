"""F18.11 UI matrix: real MeetingBridge + Main.qml in one isolated offscreen child."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

PROBE = r"""
import sys, tempfile
from pathlib import Path
from unittest.mock import Mock
from PyQt5 import sip
from PyQt5.QtCore import QObject, QUrl, pyqtProperty
from PyQt5.QtGui import QGuiApplication, QFontDatabase
from PyQt5.QtQml import QQmlApplicationEngine, QQmlExpression
from PyQt5.QtQuick import QQuickWindow
from PyQt5.QtTest import QTest
repo = Path(sys.argv[1])
sys.path[:0] = [str(repo), str(repo / 'src')]
from astra_voice.core.settings import Settings
from astra_voice.meetings.apps import AppClass, AppDefinition
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting
from astra_voice.ui.meeting_bridge import MeetingBridge
from astra_voice.ui.icons import install_icon_provider
from tools.meeting_policy_check import matrix_cases, load_case

class ThemeSource(QObject):
    @pyqtProperty(bool, constant=True)
    def dark(self):
        return False

QQuickWindow.setSceneGraphBackend('software')
app = QGuiApplication([])
for path in (repo / 'tests/fixtures/fonts/pt-root-ui').glob('*.ttf'):
    assert QFontDatabase.addApplicationFont(str(path)) >= 0
theme = ThemeSource()
engine = QQmlApplicationEngine()
warnings = []
engine.warnings.connect(lambda rows: warnings.extend(row.toString() for row in rows))
install_icon_provider(engine)
engine.rootContext().setContextProperty('themeSource', theme)
apps = (AppDefinition('dion', 'DION', ('dion',), (), AppClass.DION),)
bridges = []
window = None
cases = matrix_cases()

def named(name):
    obj = window.findChild(QObject, name)
    assert obj is not None, (current.id, name)
    return obj

def visual_texts(item):
    result = []
    text = item.property('text')
    if isinstance(text, str) and item.property('visible'):
        result.append(text)
    for child in item.childItems():
        result.extend(visual_texts(child))
    return result

with tempfile.TemporaryDirectory(prefix='astra-policy-qml-') as temporary:
    for current in cases:
        source = load_case(current, Path(temporary))
        stored = Settings(**current.user)
        before = stored.to_dict()
        done = Meeting(id='00000000-0000-4000-8000-000000000001', title='Сохранённая встреча',
                       started_at='2026-10-30T12:00:00Z', state=MeetingState.DONE)
        recorded = Meeting(id='00000000-0000-4000-8000-000000000002', title='Записанная встреча',
                           started_at='2026-10-30T12:00:00Z', state=MeetingState.RECORDED)
        bridge = MeetingBridge(stored, policy=source, known_apps=apps,
                               store=Mock(list=Mock(return_value=[done, recorded])),
                               save=Mock(), submit=lambda work: work())
        bridges.append(bridge)
        engine.rootContext().setContextProperty('meetingBridge', bridge)
        if window is None:
            engine.load(QUrl.fromLocalFile(str(repo / 'qml/Main.qml')))
            assert engine.rootObjects(), warnings
            window = engine.rootObjects()[0]
            window.show()
            sidebar = named('settingsSidebar')
            assert sidebar.setProperty('currentIndex', 3)
        else:
            bridge.retry()
        QTest.qWait(25)
        bridge._poll()
        QTest.qWait(10)
        expected = current.expect
        for key, value in expected['bridge'].items():
            actual = getattr(bridge, key)
            if key in {'policyNote', 'policyInvalidText'}:
                actual = bool(actual)
            assert actual == value, (current.id, key, actual, value)
        for object_name, properties in expected['qml'].items():
            for key, value in properties.items():
                assert named(object_name).property(key) == value, (current.id, object_name, key)
        assert stored.to_dict() == before, current.id
        for key, value in expected['locked'].items():
            assert bridge.locked[key] == value, (current.id, key)
        for key, value in expected['settings'].items():
            if key != 'offline':
                assert bridge.settings[key] == value, (current.id, key)
        assert (
            named('meetingPolicyNote').property('visible') ==
                (expected['bridge']['policyNote'] and bridge.enabled)
        ), current.id
        if current.id == 'diar-off-user-disabled':
            assert bridge.policyNote and not bridge.enabled
            assert not named('meetingPolicyNote').property('visible')
            assert not named('meetingSettingsGroups').property('visible')
            assert named('meetingOffEntry').property('visible')
        assert named('meetingOffEntry').property('visible') == (not bridge.enabled), current.id
        assert named('meetingEnable').property('enabled') == bridge.canEnable, current.id
        assert (
            named('meetingPolicyInvalidText').property('visible') ==
                expected['bridge']['policyInvalidText']
        ), current.id
        assert (
            named('meetingOfferToggle').property('enabled') ==
                (not expected['locked']['meeting_offer_on_call'])
        ), current.id
        assert (
            named('meetingOfferToggle').property('checked') ==
                expected['settings']['meeting_offer_on_call']
        ), current.id
        assert (
            named('meetingDiarization').property('enabled') ==
                (not expected['locked']['meeting_diarization'])
        ), current.id
        assert (
            named('meetingDiarization').property('checked') ==
                expected['settings']['meeting_diarization']
        ), current.id
        for name, key in (('meetingNoticeFieldInput', 'meeting_notice_text'),
                          ('meetingAiEndpointInput', 'meeting_ai_endpoint'),
                          ('meetingAiModelInput', 'meeting_ai_model')):
            field = named(name)
            assert field.property('enabled') == (not expected['locked'][key]), (current.id, name)
            assert field.property('text') == expected['settings'][key], (current.id, name)
            assert field.property('opacity') == 1.0, (current.id, name)
            if expected['locked'][key]:
                color = QQmlExpression(engine.contextForObject(field), field, 'Theme.fgDisabled')
                assert field.property('color') == color.evaluate()[0], (current.id, name)
                assert not color.hasError(), color.error().toString()
        assert named('meetingAiKeyInput').property('enabled'), current.id
        retention = named('meetingAudioRetention')
        assert (
            retention.property('enabled') ==
                (not expected['locked']['meeting_audio_retention_days'])
        ), current.id
        days = expected['settings']['meeting_audio_retention_days']
        assert retention.property('currentText') == ('Не хранить' if days == 0 else
               f'{days} ' + ('день' if days == 1 else 'дней')), current.id
        if bridge.enabled:
            assert named('meetingAiOff').property('visible') == bridge.aiDisabled, current.id
            assert (
                named('meetingAiEndpointInput').property('visible') == (not bridge.aiDisabled)
            ), current.id
            assert (
                named('meetingAiAdminAddress').property('visible') == bridge.aiAdminAddress
            ), current.id
            texts = visual_texts(window.contentItem())
            if bridge.aiDisabled:
                assert 'Шаблоны с ИИ отключены администратором' in texts, current.id
                assert not named('meetingAiModelInput').property('visible'), current.id
                assert not named('meetingAiKeyInput').property('visible'), current.id
            if bridge.aiAdminAddress:
                assert 'Адрес задан администратором, модель и ключ — ваши' in texts, current.id
                assert named('meetingAiModelInput').property('visible'), current.id
                assert named('meetingAiKeyInput').property('visible'), current.id
            if bridge.endpointKind in {'plaintext', 'external-admin'}:
                assert bridge.endpointCaption in texts, (current.id, texts)
        if not bridge.canEnable:
            assert named('meetingList').property('visible'), current.id
            assert len(bridge.meetings) == 2, current.id
            saved = next(row for row in bridge.meetings if row['id'] == done.id)
            assert saved['action'] == 'Открыть', current.id
            assert saved['actions'] == ['Сохранить как…', 'Удалить'], current.id
            assert all(row['action'] != 'Расшифровать' and 'Расшифровать' not in row['actions']
                       for row in bridge.meetings), current.id
            visible = visual_texts(window.contentItem())
            assert 'Открыть' in visible and 'Расшифровать' not in visible, current.id
            if bridge.policyInvalidText:
                assert bridge.policyInvalidText in visible, current.id
            else:
                assert bridge.texts['policyOff'] in visible, current.id
        # D1 is the only opt-in; policy cannot grant it on construction or refresh.
        bridge.disableMeetings()
        assert not bridge.enabled, current.id
        bridge.openResponsibility()
        QTest.qWait(10)
        assert named('meetingResponsibility').property('visible') == bridge.canEnable, current.id
        if bridge.canEnable:
            detector = named('meetingResponsibilityDetector')
            assert detector.property('enabled') == expected['detector_allowed'], current.id
            assert not detector.property('checked'), current.id
            if source.raw.get('meeting_record') == 'manual':
                assert named('meetingResponsibilityDetectorCaption').property('text') == (
                    'Недоступно — так настроено администратором вашей организации'), current.id
            bridge.acceptResponsibility(True)
            assert bridge.enabled, current.id
            assert (
                bridge.settings['meeting_offer_on_call'] == expected['detector_allowed']
            ), current.id
        bridge.cancelResponsibility()
        bridge.shutdown()
        assert not warnings, (current.id, warnings)
        print(current.id, flush=True)
    window.hide()
    sip.delete(engine)
assert not warnings, warnings
print(f'ok: {len(cases)} real-bridge policy rows')
"""


def test_real_bridge_policy_matrix_in_main_qml() -> None:
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
        [sys.executable, "-c", PROBE, str(ROOT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "real-bridge policy rows" in result.stdout


def test_mn17_existing_notice_explains_videoconference_mute() -> None:
    from astra_voice.ui.meeting_texts import notice_mute_text

    for app_class, app in (("dion", "DION"), ("browser", "программе видеосвязи")):
        text = notice_mute_text(app_class)
        assert "Ваш микрофон записывается всё время записи" in text
        assert f"выключенный микрофон в {app} на запись не влияет" in text
        assert "нажмите «Пауза»" in text
    qml = (ROOT / "qml/MeetingNotice.qml").read_text(encoding="utf-8")
    assert 'objectName: "meetingNoticeMuteText"' in qml
    assert 'text: root.presentation.muteText || ""' in qml
