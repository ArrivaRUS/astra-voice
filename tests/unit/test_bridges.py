"""S19: каждое свойство QML сохраняется сразу и откатывается при ошибке."""

from __future__ import annotations

import ast
import gc
import hashlib
import json
import logging
import re
import socket
import stat
import threading
import time
import weakref
from collections.abc import Callable, Collection, Iterator
from dataclasses import replace
from functools import partial
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from unittest.mock import Mock, call

import pytest
import requests
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer, QUrl
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtTest import QSignalSpy

from astra_voice.app import _make_app_info
from astra_voice.core import paths
from astra_voice.core import policy as policy_mod
from astra_voice.core import settings as settings_mod
from astra_voice.core.dictation import (
    LEVEL_TOTAL_LIMIT_S,
    TEST_PREPARING,
    DictationOrchestrator,
    DictationPhase,
    MicrophoneLevelUpdate,
    MicrophoneTestUpdate,
)
from astra_voice.core.model_source import SMOKE_EXPECT_ANY
from astra_voice.core.settings import Settings
from astra_voice.core.version import __version__
from astra_voice.models import catalog_state
from astra_voice.models.catalog import Catalog, CatalogEntry, FileSpec, RevokedEntry
from astra_voice.models.downloader import DownloadError, Progress
from astra_voice.models.installer import InstallResult, ReasonCode
from astra_voice.models.store import ModelStore, StoreError
from astra_voice.net.http import NetworkError
from astra_voice.platform.hotkey import DEFAULT_CANDIDATES
from astra_voice.platform.paste import PasteMode
from astra_voice.platform.session import SessionKind
from astra_voice.platform.sound import MicrophoneState
from astra_voice.ui import model_downloads
from astra_voice.ui.bridges import (
    OnboardingController,
    OnboardingHost,
    SettingsApply,
    SettingsBridge,
)
from astra_voice.ui.hotkey_capture import CaptureHost, HotkeyCapture
from astra_voice.ui.model_downloads import (
    ModelDownloads,
    ModelPort,
    ModelService,
    _ModelJob,
    make_smoke_check,
)
from astra_voice.ui.pill import PillState
from astra_voice.ui.tray_icons import TrayState
from astra_voice.worker import ipc
from astra_voice.worker.audio import AudioDevice, AudioError

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module", autouse=True)
def qcore_app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture(autouse=True)
def desktop_opener(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Ни один тест мостов не может запустить настоящий файловый менеджер."""
    opener = Mock(return_value=True)
    monkeypatch.setattr(QDesktopServices, "openUrl", opener)
    return opener


@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        (" yes ", True),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("no", False),
        ("", False),
        ("мусор", False),
    ],
)
def test_app_info_debug_environment(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool, debug: bool
) -> None:
    monkeypatch.setenv("ASTRA_VOICE_DEBUG", value)

    app_info = _make_app_info(SessionKind.OTHER, "absent", debug=debug)

    assert app_info.debug is (debug or expected)


def test_app_info_show_debug_section_enables_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRA_VOICE_DEBUG", "false")
    app_info = _make_app_info(SessionKind.OTHER, "absent")
    changed = QSignalSpy(app_info.debugChanged)
    sections = QSignalSpy(app_info.showSection)
    events: list[tuple[str, bool]] = []
    app_info.debugChanged.connect(lambda: events.append(("debugChanged", app_info.debug)))
    app_info.showSection.connect(lambda section: events.append((section, app_info.debug)))
    assert app_info.debug is False

    app_info.show_section("debug")

    assert app_info.debug is True
    assert len(changed) == 1
    assert list(sections) == [["debug"]]
    assert events == [("debugChanged", True), ("debug", True)]

    app_info.show_section("debug")

    assert len(changed) == 1
    assert list(sections) == [["debug"], ["debug"]]


def writable_properties() -> list[str]:
    meta = SettingsBridge.staticMetaObject
    return [
        prop.name()
        for index in range(meta.propertyOffset(), meta.propertyCount())
        if (prop := meta.property(index)).isWritable()
    ]


def changed_value(bridge: SettingsBridge, name: str) -> str | bool:
    old = getattr(bridge, name)
    if isinstance(old, bool):
        return not old
    # Новый строковый контрол требует явного тестового значения, иначе тест падает.
    return {
        "hotkey": "Ctrl+Shift+Space",
        "hotkeyMode": "toggle",
        "language": "en",
        "device": "mic",
    }[name]


def field_name(name: str) -> str:
    return re.sub(r"([A-Z])", r"_\1", name).lower()


def set_qt_property(
    bridge: SettingsBridge | OnboardingController, name: str, value: str | bool
) -> None:
    # Стабы Qt описывают свойства как методы; вызываем тот же Python-сеттер.
    setattr(bridge, name, value)


def stored_value(settings: Settings, name: str) -> str | bool | None:
    if name == "device":
        return settings.extra.get("device")
    return cast(str | bool | None, getattr(settings, field_name(name)))


@pytest.mark.parametrize("name", writable_properties())
@pytest.mark.parametrize("with_mirror", [False, True])
def test_each_property_saves_and_notifies_once(name: str, with_mirror: bool) -> None:
    settings = Settings()
    mirror = policy_mod.effective(settings, policy_mod.Policy()) if with_mirror else None
    save = Mock()
    apply = Mock(spec=SettingsApply)
    apply.hotkey.return_value = "ok"
    bridge = SettingsBridge(settings, mirror=mirror, save=save, apply=apply)
    spy = QSignalSpy(getattr(bridge, name + "Changed"))
    value = changed_value(bridge, name)

    def saving(current: Settings) -> None:
        assert current is settings
        assert stored_value(current, name) == value
        if name in ("hotkey", "hotkeyMode"):
            assert apply.mock_calls == [call.hotkey(current.hotkey, current.hotkey_mode)]
        else:
            assert apply.mock_calls == []

    save.side_effect = saving
    assert bridge.setProperty(name, value)
    assert getattr(bridge, name) == value
    assert stored_value(settings, name) == value
    if mirror is not None:
        assert stored_value(mirror, name) == value
    save.assert_called_once_with(settings)
    assert len(spy) == 1
    if name in ("hotkey", "hotkeyMode"):
        apply.hotkey.assert_called_once_with(settings.hotkey, settings.hotkey_mode)

    if name in ("language", "autostart", "checkAppUpdates", "checkModelUpdates"):
        assert apply.mock_calls == []
    save.reset_mock()
    apply.reset_mock()
    bridge.setProperty(name, value)
    save.assert_not_called()
    assert len(spy) == 1
    assert apply.mock_calls == []


def test_defaults_and_readonly_properties() -> None:
    bridge = SettingsBridge(Settings(), save=Mock())
    assert bridge.hotkeyStatus == "ok"
    assert bridge.saveError == ""
    assert bridge.modelSelfcheck == "idle"
    assert bridge.device == ""
    meta = bridge.metaObject()
    for index in range(meta.propertyOffset(), meta.propertyCount()):
        prop = meta.property(index)
        assert prop.hasNotifySignal() or prop.isConstant()
        assert prop.isWritable() == (prop.name() in writable_properties())
    for name in ("hotkeyStatus", "saveError", "modelSelfcheck"):
        assert not bridge.setProperty(name, "changed")
    assert bridge.lockedSettings == []
    assert not bridge.is_locked("hotkey")
    assert not bridge.setProperty("lockedSettings", ["hotkey"])


def test_real_default_save_is_private_atomic_and_roundtrips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    monkeypatch.setattr("astra_voice.core.paths.settings_path", lambda: path)
    settings = Settings(extra={"unrelated": "preserved"})
    bridge = SettingsBridge(settings)
    set_qt_property(bridge, "pillEnabled", False)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert settings_mod.load(path) == settings
    assert json.loads(path.read_text()) == settings.to_dict()
    assert list(tmp_path.iterdir()) == [path]

    # Открытый дескриптор продолжает видеть старый inode после атомарной замены.
    with path.open() as previous:
        set_qt_property(bridge, "language", "en")
        assert json.load(previous)["language"] == "ru"
    assert settings_mod.load(path) == settings
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_real_save_preserves_user_choices_under_policy(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = Settings(extra={"device": "user mic", "unrelated": "preserved"})
    policy = policy_mod.Policy(
        values={"hotkey": "Alt+Space", "hotkey_mode": "toggle", "device": "admin mic"}
    )
    mirror = policy_mod.effective(settings, policy)
    bridge = SettingsBridge(
        settings, mirror=mirror, locked=policy.values, save=partial(settings_mod.save, path=path)
    )
    assert bridge.hotkey == "Alt+Space"
    assert bridge.hotkeyMode == "toggle"
    assert bridge.device == "admin mic"

    set_qt_property(bridge, "language", "en")

    data = json.loads(path.read_text())
    assert data == settings.to_dict()
    assert data["hotkey"] == "Ctrl+Space"
    assert data["hotkey_mode"] == "ptt"
    assert data["device"] == "user mic"
    assert settings_mod.load(path) == settings
    assert settings.language == mirror.language == "en"
    assert mirror.hotkey == "Alt+Space"
    assert mirror.hotkey_mode == "toggle"
    assert mirror.extra["device"] == "admin mic"


@pytest.mark.parametrize("name", writable_properties())
def test_locked_property_rejects_changes_and_notifies(
    name: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings()
    user_bridge = SettingsBridge(settings, save=Mock())
    user_value = getattr(user_bridge, name)
    value = changed_value(user_bridge, name)
    policy = policy_mod.Policy(values={field_name(name): value})
    mirror = policy_mod.effective(settings, policy)
    save = Mock()
    apply = Mock(spec=SettingsApply)
    bridge = SettingsBridge(settings, mirror=mirror, locked=policy.values, save=save, apply=apply)
    original, effective = settings.to_dict(), mirror.to_dict()
    spy = QSignalSpy(getattr(bridge, name + "Changed"))
    seen = []
    getattr(bridge, name + "Changed").connect(lambda: seen.append(getattr(bridge, name)))

    with caplog.at_level(logging.INFO):
        assert bridge.setProperty(name, user_value)
        assert bridge.setProperty(name, value)

    assert getattr(bridge, name) == value
    assert settings.to_dict() == original
    assert mirror.to_dict() == effective
    save.assert_not_called()
    assert apply.mock_calls == []
    assert len(spy) == 2
    assert seen == [value, value]
    assert bridge.saveError == ""
    assert any(
        record.levelno == logging.INFO and "настройка задана администратором" in record.getMessage()
        for record in caplog.records
    )


def test_locked_settings_are_sorted_immutable_and_available_without_mirror() -> None:
    locked = ["pill_enabled", "hotkey", "device", "hotkey"]
    save = Mock()
    bridge = SettingsBridge(Settings(), locked=iter(locked), save=save)
    locked.append("language")
    assert bridge.lockedSettings == ["device", "hotkey", "pill_enabled"]
    bridge.lockedSettings.append("language")
    assert bridge.lockedSettings == ["device", "hotkey", "pill_enabled"]
    assert bridge.is_locked("pill_enabled")
    assert not bridge.is_locked("pillEnabled")
    assert not bridge.is_locked("language")
    assert not bridge.is_locked("unknown")
    set_qt_property(bridge, "pillEnabled", False)
    assert bridge.pillEnabled is True
    save.assert_not_called()


def test_s19_a5_all_writable_properties_reach_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()
    bridge = SettingsBridge(settings, save=partial(settings_mod.save, path=path))
    changed: dict[str, str | bool] = {}
    for name in writable_properties():
        value = changed_value(bridge, name)
        changed[name] = value
        assert bridge.setProperty(name, value)
        loaded = settings_mod.load(path)
        data = json.loads(path.read_text())
        for previous_name, expected in changed.items():
            assert stored_value(loaded, previous_name) == expected
            assert data[field_name(previous_name)] == expected
        assert loaded == settings
        assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("name", writable_properties())
@pytest.mark.parametrize("with_mirror", [False, True])
def test_save_error_rolls_back_value_and_runtime(
    name: str, with_mirror: bool, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings()
    original = settings.to_dict()
    mirror = policy_mod.effective(settings, policy_mod.Policy()) if with_mirror else None
    save = Mock(side_effect=OSError("PRIVATE FILE CONTENT"))
    apply = Mock(spec=SettingsApply)
    apply.hotkey.return_value = "ok"
    bridge = SettingsBridge(settings, mirror=mirror, save=save, apply=apply)
    old = getattr(bridge, name)
    value = changed_value(bridge, name)
    spy = QSignalSpy(getattr(bridge, name + "Changed"))
    error_spy = QSignalSpy(bridge.saveErrorChanged)
    seen = []
    getattr(bridge, name + "Changed").connect(lambda: seen.append(getattr(bridge, name)))
    with caplog.at_level(logging.WARNING):
        bridge.setProperty(name, value)
    assert getattr(bridge, name) == old
    assert settings.to_dict() == original
    if mirror is not None:
        assert mirror.to_dict() == original
    save.assert_called_once_with(settings)
    assert bridge.saveError == "Не удалось сохранить настройки"
    assert len(spy) == len(error_spy) == 1
    assert seen == [old]
    if name in ("hotkey", "hotkeyMode"):
        assert apply.mock_calls == [
            call.hotkey(
                value if name == "hotkey" else settings.hotkey,
                value if name == "hotkeyMode" else settings.hotkey_mode,
            ),
            call.hotkey(settings.hotkey, settings.hotkey_mode),
        ]
    else:
        assert apply.mock_calls == []
    assert "PRIVATE FILE CONTENT" not in caplog.text
    assert any(record.levelno == logging.WARNING for record in caplog.records)

    # Повторный отказ также уведомляет контрол и пользователя.
    bridge.setProperty(name, value)
    assert len(spy) == len(error_spy) == 2
    bridge.setProperty(name, old)
    assert save.call_count == 2
    assert len(spy) == len(error_spy) == 2

    save.side_effect = None
    apply.hotkey.return_value = "ok"
    bridge.setProperty(name, value)
    assert bridge.saveError == ""
    assert len(spy) == len(error_spy) == 3
    bridge.setProperty(name, value)
    assert len(error_spy) == 3


@pytest.mark.parametrize("old", [None, "old mic"])
@pytest.mark.parametrize("mirror_extra", [{}, {"device": None}, {"device": "runtime mic"}])
def test_device_failure_preserves_existing_extra(
    old: str | None, mirror_extra: dict[str, str | None]
) -> None:
    settings = Settings(extra={"device": old})
    mirror = Settings(extra=dict(mirror_extra))
    bridge = SettingsBridge(settings, mirror=mirror, save=Mock(side_effect=OSError()))
    set_qt_property(bridge, "device", "new mic")
    assert settings.extra == {"device": old}
    assert mirror.extra == mirror_extra
    assert bridge.device == (mirror_extra.get("device") or "")


def test_save_failure_preserves_distinct_user_and_runtime_values() -> None:
    settings = Settings(hotkey="Ctrl+Space")
    mirror = Settings(hotkey="Alt+Space")
    bridge = SettingsBridge(settings, mirror=mirror, save=Mock(side_effect=OSError()))
    set_qt_property(bridge, "hotkey", "Ctrl+Shift+Space")
    assert settings.hotkey == "Ctrl+Space"
    assert mirror.hotkey == bridge.hotkey == "Alt+Space"


def test_pill_and_device_apply_after_save() -> None:
    settings = Settings()
    mirror = Settings()
    calls = Mock()
    apply = Mock(spec=SettingsApply)
    calls.attach_mock(apply, "apply")
    save = Mock()
    calls.attach_mock(save, "save")
    bridge = SettingsBridge(settings, mirror=mirror, apply=apply, save=save)

    def applying_device(value: str | None) -> None:
        assert settings.extra["device"] == mirror.extra["device"] == value

    apply.device.side_effect = applying_device
    set_qt_property(bridge, "pillEnabled", False)
    apply.pill_enabled.assert_called_once_with(False)
    assert [c[0] for c in calls.mock_calls] == ["save", "apply.pill_enabled"]
    set_qt_property(bridge, "device", "USB mic")
    apply.device.assert_called_once_with("USB mic")
    set_qt_property(bridge, "device", "")
    apply.device.assert_called_with(None)
    assert settings.extra["device"] is None
    assert mirror.extra["device"] is None
    assert save.call_count == 3


@pytest.mark.parametrize("code", ["", "ok", "busy", "bad-combo", "duplicate", "not-grabbed"])
@pytest.mark.parametrize("name", ["hotkey", "hotkeyMode"])
def test_hotkey_apply_updates_status(name: str, code: str) -> None:
    apply = Mock(spec=SettingsApply)
    apply.hotkey.return_value = code
    settings = Settings()
    bridge = SettingsBridge(settings, apply=apply, save=Mock())
    spy = QSignalSpy(bridge.hotkeyStatusChanged)
    value = changed_value(bridge, name)
    proposed = (
        value if name == "hotkey" else settings.hotkey,
        value if name == "hotkeyMode" else settings.hotkey_mode,
    )
    original = (settings.hotkey, settings.hotkey_mode)
    bridge.setProperty(name, value)
    expected_calls = [call.hotkey(*proposed)]
    if code not in ("", "ok"):
        expected_calls.append(call.hotkey(*original))
    assert apply.mock_calls == expected_calls
    assert bridge.hotkeyStatus == (code or "ok")
    assert len(spy) == (0 if code in ("", "ok") else 1)


@pytest.mark.parametrize("name", ["hotkey", "hotkeyMode"])
@pytest.mark.parametrize("with_mirror", [False, True])
@pytest.mark.parametrize("existing_file", [False, True])
@pytest.mark.parametrize("failure", ["busy", "bad-combo", "duplicate", "not-grabbed", "OSError"])
def test_hotkey_failure_preserves_disk_value_and_previous_grab(
    tmp_path: Path, name: str, with_mirror: bool, existing_file: bool, failure: str
) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()
    mirror = Settings(hotkey="Alt+Space", hotkey_mode="toggle") if with_mirror else None
    runtime_settings = mirror if mirror is not None else settings
    original = settings.to_dict()
    effective = runtime_settings.to_dict()
    old_grab = (runtime_settings.hotkey, runtime_settings.hotkey_mode)
    if existing_file:
        settings_mod.save(settings, path)
    saved = path.read_bytes() if existing_file else None
    save = Mock(wraps=partial(settings_mod.save, path=path))
    if failure == "OSError":
        save.side_effect = OSError("Запись запрещена")
    apply = Mock(spec=SettingsApply)
    calls = Mock()
    calls.attach_mock(apply, "apply")
    calls.attach_mock(save, "save")

    def grab(combo: str, mode: str) -> str:
        # DictationRuntime.apply_hotkey меняет Settings даже при отказе захвата.
        runtime_settings.hotkey, runtime_settings.hotkey_mode = combo, mode
        assert (path.read_bytes() if path.exists() else None) == saved
        return "ok" if (combo, mode) == old_grab or failure == "OSError" else failure

    apply.hotkey.side_effect = grab
    bridge = SettingsBridge(settings, mirror=mirror, apply=apply, save=save)
    old = getattr(bridge, name)
    value = "Ctrl+Shift+Space" if name == "hotkey" else ("ptt" if old == "toggle" else "toggle")
    spy = QSignalSpy(getattr(bridge, name + "Changed"))
    seen: list[str] = []
    getattr(bridge, name + "Changed").connect(lambda: seen.append(getattr(bridge, name)))
    proposed = (
        value if name == "hotkey" else old_grab[0],
        value if name == "hotkeyMode" else old_grab[1],
    )

    assert bridge.setProperty(name, value)

    assert getattr(bridge, name) == old
    assert settings.to_dict() == original
    assert runtime_settings.to_dict() == effective
    assert len(spy) == 1 and seen == [old]
    assert (path.read_bytes() if path.exists() else None) == saved
    assert list(tmp_path.iterdir()) == ([path] if existing_file else [])
    assert apply.hotkey.call_args_list == [call(*proposed), call(*old_grab)]
    if failure == "OSError":
        save.assert_called_once_with(settings)
        assert [c[0] for c in calls.mock_calls] == ["apply.hotkey", "save", "apply.hotkey"]
        assert bridge.saveError == "Не удалось сохранить настройки"
        assert bridge.hotkeyStatus == "ok"
    else:
        save.assert_not_called()
        assert [c[0] for c in calls.mock_calls] == ["apply.hotkey", "apply.hotkey"]
        assert bridge.saveError == ""
        assert bridge.hotkeyStatus == failure


@pytest.mark.parametrize("name", ["hotkey", "hotkeyMode"])
@pytest.mark.parametrize("code", ["", "ok"])
@pytest.mark.parametrize("with_mirror", [False, True])
def test_hotkey_success_grabs_before_saving_exactly_once(
    tmp_path: Path, name: str, code: str, with_mirror: bool
) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()
    mirror = Settings(hotkey="Alt+Space", hotkey_mode="toggle") if with_mirror else None
    runtime_settings = mirror if mirror is not None else settings
    settings_mod.save(settings, path)
    saved = path.read_bytes()
    save = Mock(wraps=partial(settings_mod.save, path=path))
    apply = Mock(spec=SettingsApply)
    calls = Mock()
    calls.attach_mock(apply, "apply")
    calls.attach_mock(save, "save")

    def grab(combo: str, mode: str) -> str:
        assert path.read_bytes() == saved
        runtime_settings.hotkey, runtime_settings.hotkey_mode = combo, mode
        return code

    apply.hotkey.side_effect = grab
    bridge = SettingsBridge(settings, mirror=mirror, apply=apply, save=save)
    bridge.set_hotkey_status("busy")
    value = (
        "Ctrl+Shift+Space"
        if name == "hotkey"
        else ("ptt" if bridge.hotkeyMode == "toggle" else "toggle")
    )
    combo = value if name == "hotkey" else bridge.hotkey
    mode = value if name == "hotkeyMode" else bridge.hotkeyMode
    spy = QSignalSpy(getattr(bridge, name + "Changed"))

    assert bridge.setProperty(name, value)

    assert [c[0] for c in calls.mock_calls] == ["apply.hotkey", "save"]
    apply.hotkey.assert_called_once_with(combo, mode)
    save.assert_called_once_with(settings)
    assert getattr(bridge, name) == stored_value(settings, name) == value
    assert stored_value(runtime_settings, name) == value
    assert settings_mod.load(path) == settings
    assert list(tmp_path.iterdir()) == [path]
    assert len(spy) == 1
    assert bridge.hotkeyStatus == "ok" and bridge.saveError == ""


def test_hotkey_apply_keeps_policy_mode_when_combo_changes() -> None:
    settings = Settings()
    policy = policy_mod.Policy(values={"hotkey_mode": "toggle"})
    mirror = policy_mod.effective(settings, policy)
    apply = Mock(spec=SettingsApply)
    apply.hotkey.return_value = "ok"
    bridge = SettingsBridge(settings, mirror=mirror, locked=policy.values, apply=apply, save=Mock())

    set_qt_property(bridge, "hotkey", "Alt+Space")

    apply.hotkey.assert_called_once_with("Alt+Space", "toggle")
    assert settings.hotkey == mirror.hotkey == "Alt+Space"
    assert settings.hotkey_mode == "ptt"
    assert mirror.hotkey_mode == "toggle"


@pytest.mark.parametrize(
    "combo",
    ["A", "Space", "Shift+A", "", "  sHiFt + A  ", "ControlKey+A", "Ctrl", "Alt+Super", "Ctrl+A+B"],
)
@pytest.mark.parametrize("with_mirror", [False, True])
def test_hotkey_without_modifier_is_rejected(
    combo: str, with_mirror: bool, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings()
    mirror = Settings(hotkey="Alt+Space") if with_mirror else None
    save = Mock()
    apply = Mock(spec=SettingsApply)
    bridge = SettingsBridge(settings, mirror=mirror, save=save, apply=apply)
    bridge.set_hotkey_status("busy")
    original = settings.to_dict()
    old = bridge.hotkey
    changed = QSignalSpy(bridge.hotkeyChanged)
    status = QSignalSpy(bridge.hotkeyStatusChanged)

    with caplog.at_level(logging.WARNING):
        assert bridge.setProperty("hotkey", combo)

    apply.hotkey.assert_not_called()
    save.assert_not_called()
    assert settings.to_dict() == original
    assert bridge.hotkey == old
    if mirror is not None:
        assert mirror.hotkey == old
    assert len(changed) == 1
    assert bridge.hotkeyStatus == "busy" and len(status) == 0
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.WARNING, "Недопустимое значение настройки hotkey")
    ]


@pytest.mark.parametrize(
    "combo",
    [
        "Ctrl+Alt+D",
        " cTrL + A ",
        " CONTROL + A ",
        " aLt + A ",
        " MeTa + A ",
        " SuPeR + A ",
        " WIN + A ",
    ],
)
def test_hotkey_with_modifier_is_applied_and_saved(tmp_path: Path, combo: str) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()
    save = Mock(wraps=partial(settings_mod.save, path=path))
    apply = Mock(spec=SettingsApply)
    apply.hotkey.return_value = "ok"
    bridge = SettingsBridge(settings, save=save, apply=apply)
    changed = QSignalSpy(bridge.hotkeyChanged)

    assert bridge.setProperty("hotkey", combo)

    apply.hotkey.assert_called_once_with(combo, settings.hotkey_mode)
    save.assert_called_once_with(settings)
    assert bridge.hotkey == settings.hotkey == settings_mod.load(path).hotkey == combo
    assert len(changed) == 1


@pytest.mark.parametrize("combo", ["Space", "Return", "A", "Shift+Space", "Ctrl", "Ctrl+A+B"])
@pytest.mark.parametrize("with_mirror", [False, True])
def test_mode_change_does_not_apply_invalid_saved_hotkey(
    combo: str, with_mirror: bool, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(hotkey=combo)
    mirror = Settings(hotkey=combo) if with_mirror else None
    save = Mock()
    apply = Mock(spec=SettingsApply)
    apply.hotkey.return_value = "ok"
    bridge = SettingsBridge(settings, mirror=mirror, save=save, apply=apply)
    original = settings.to_dict()
    changed = QSignalSpy(bridge.hotkeyModeChanged)

    with caplog.at_level(logging.WARNING):
        assert bridge.setProperty("hotkeyMode", "toggle")

    apply.hotkey.assert_not_called()
    save.assert_not_called()
    assert settings.to_dict() == original
    assert bridge.hotkey == combo
    assert bridge.hotkeyMode == "ptt"
    if mirror is not None:
        assert mirror.hotkey == combo and mirror.hotkey_mode == "ptt"
    assert len(changed) == 1
    assert any(
        record.levelno == logging.WARNING and repr(combo) in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("name", ["hotkeyMode", "language"])
@pytest.mark.parametrize("value", ["", "invalid", "PTT", "RU"])
def test_invalid_values_are_ignored(
    name: str, value: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings()
    save = Mock()
    apply = Mock(spec=SettingsApply)
    bridge = SettingsBridge(settings, save=save, apply=apply)
    old = getattr(bridge, name)
    spy = QSignalSpy(getattr(bridge, name + "Changed"))
    with caplog.at_level(logging.WARNING):
        bridge.setProperty(name, value)
    assert getattr(bridge, name) == old
    assert stored_value(settings, name) == old
    save.assert_not_called()
    assert apply.mock_calls == []
    assert len(spy) == 0
    assert any(record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.parametrize(
    ("name", "method", "values"),
    [
        (
            "hotkeyStatus",
            "set_hotkey_status",
            ["busy", "bad-combo", "duplicate", "not-grabbed", "ok"],
        ),
        ("modelSelfcheck", "set_model_selfcheck", ["running", "ok", "failed", "idle"]),
    ],
)
def test_runtime_status_setters(name: str, method: str, values: list[str]) -> None:
    save = Mock()
    bridge = SettingsBridge(Settings(), save=save)
    spy = QSignalSpy(getattr(bridge, name + "Changed"))
    for index, value in enumerate(values, 1):
        getattr(bridge, method)(value)
        assert getattr(bridge, name) == value
        assert len(spy) == index
        getattr(bridge, method)(value)
        assert len(spy) == index
    save.assert_not_called()


@pytest.mark.parametrize("with_mirror", [False, True])
def test_reload_notifies_only_external_changes_without_save_or_apply(with_mirror: bool) -> None:
    settings = Settings()
    mirror = policy_mod.effective(settings, policy_mod.Policy()) if with_mirror else None
    current = mirror if mirror is not None else settings
    save = Mock()
    apply = Mock(spec=SettingsApply)
    bridge = SettingsBridge(settings, mirror=mirror, save=save, apply=apply)
    spies = {name: QSignalSpy(getattr(bridge, name + "Changed")) for name in writable_properties()}
    bridge.reload()
    assert all(len(spy) == 0 for spy in spies.values())
    for name in writable_properties():
        value = changed_value(bridge, name)
        if name == "device":
            current.extra["device"] = value
        else:
            setattr(current, field_name(name), value)
        bridge.reload()
        assert getattr(bridge, name) == value
        assert len(spies[name]) == 1
        bridge.reload()
        assert len(spies[name]) == 1
    assert all(len(spy) == 1 for spy in spies.values())
    current.extra.pop("device")
    bridge.reload()
    assert bridge.device == ""
    assert len(spies["device"]) == 2
    save.assert_not_called()
    assert apply.mock_calls == []


def test_retry_hotkey_uses_current_values_without_saving() -> None:
    apply = Mock(spec=SettingsApply)
    apply.hotkey.side_effect = ["busy", "busy", "ok"]
    save = Mock()
    bridge = SettingsBridge(
        Settings(hotkey="Alt+Space", hotkey_mode="toggle"), apply=apply, save=save
    )
    spy = QSignalSpy(bridge.hotkeyStatusChanged)
    for code, count in [("busy", 1), ("busy", 1), ("ok", 2)]:
        bridge.retryHotkey()
        apply.hotkey.assert_called_with("Alt+Space", "toggle")
        assert bridge.hotkeyStatus == code
        assert len(spy) == count
    save.assert_not_called()


def test_retry_without_runtime_is_noop() -> None:
    bridge = SettingsBridge(Settings(), save=Mock())
    spy = QSignalSpy(bridge.hotkeyStatusChanged)
    bridge.retryHotkey()
    assert bridge.hotkeyStatus == "ok"
    assert len(spy) == 0


def test_runtime_adapter_routes_bridge_changes() -> None:
    from astra_voice.app import _RuntimeSettingsApply

    runtime = Mock(
        spec=[
            "apply_pill_enabled",
            "apply_hotkey",
            "apply_device",
            "microphone_state",
            "raise_microphone_volume",
            "open_sound_settings",
            "restart_sound_service",
            "has_volume_control",
            "has_sound_settings",
            "has_sound_service",
        ]
    )
    runtime.microphone_state.return_value = MicrophoneState()
    runtime.apply_hotkey.return_value = "busy"
    bridge = SettingsBridge(Settings(), apply=_RuntimeSettingsApply(runtime), save=Mock())
    set_qt_property(bridge, "pillEnabled", False)
    runtime.apply_pill_enabled.assert_called_once_with(False)
    set_qt_property(bridge, "hotkey", "Alt+Space")
    assert runtime.apply_hotkey.call_args_list == [
        call("Alt+Space", "ptt"),
        call("Ctrl+Space", "ptt"),
    ]
    assert bridge.hotkey == "Ctrl+Space"
    assert bridge.hotkeyStatus == "busy"
    set_qt_property(bridge, "device", "mic")
    runtime.apply_device.assert_called_once_with("mic")
    set_qt_property(bridge, "device", "")
    runtime.apply_device.assert_called_with(None)


def test_qml_accepts_bridge_after_loading_and_observes_changes() -> None:
    from PyQt5.QtCore import QUrl
    from PyQt5.QtQml import QQmlComponent, QQmlEngine

    from astra_voice.app import _set_context_property

    engine = QQmlEngine()
    component = QQmlComponent(engine)
    component.setData(
        b"import QtQml 2.15; QtObject { "
        b'property bool enabled: typeof settingsBridge !== "undefined" '
        b"&& settingsBridge !== null ? settingsBridge.pillEnabled : false; "
        b'property bool locked: typeof settingsBridge !== "undefined" '
        b'&& settingsBridge !== null ? settingsBridge.is_locked("hotkey") : false; '
        b'property var locks: typeof settingsBridge !== "undefined" '
        b"&& settingsBridge !== null ? settingsBridge.lockedSettings : [] }",
        QUrl(),
    )
    root = component.create()
    assert root is not None, [error.toString() for error in component.errors()]
    assert root.property("enabled") is False
    settings = Settings()
    save = Mock()
    bridge = SettingsBridge(settings, locked=["hotkey"], save=save)
    QQmlEngine.setObjectOwnership(bridge, QQmlEngine.CppOwnership)
    _set_context_property(engine, "settingsBridge", bridge)
    assert engine.rootContext().contextProperty("settingsBridge") is bridge
    assert QQmlEngine.objectOwnership(bridge) == QQmlEngine.CppOwnership
    assert root.property("enabled") is True
    assert root.property("locked") is True
    assert root.property("locks") == ["hotkey"]
    set_qt_property(bridge, "pillEnabled", False)
    assert root.property("enabled") is False
    save.assert_called_once_with(settings)
    # Убираем биндинг до уничтожения локальной Python-обёртки моста.
    _set_context_property(engine, "settingsBridge", None)
    assert root.property("enabled") is False


def test_context_property_ignores_widget_fallback() -> None:
    from PyQt5.QtWidgets import QWidget

    from astra_voice.app import _set_context_property

    widget = Mock(spec=QWidget)
    bridge = SettingsBridge(Settings(), save=Mock())
    _set_context_property(widget, "settingsBridge", bridge)
    assert widget.mock_calls == []


@pytest.mark.parametrize("extra", [{}, {"key": None}, {"key": "old"}])
@pytest.mark.parametrize("shared", [False, True])
def test_set_extra_rollback_and_mirror(
    extra: dict[str, str | None], shared: bool, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(extra=dict(extra))
    mirror = settings if shared else Settings(extra={"key": "runtime"})
    original_mirror = dict(mirror.extra)
    save = Mock(side_effect=OSError("PRIVATE PATH"))
    bridge = SettingsBridge(settings, mirror=mirror, save=save)
    errors = QSignalSpy(bridge.saveErrorChanged)
    changes = QSignalSpy(bridge.extraChanged)
    assert bridge.set_extra("key", True) is False
    assert settings.extra == extra
    assert mirror.extra == original_mirror
    assert len(errors) == 1 and len(changes) == 0
    assert "PRIVATE PATH" not in caplog.text
    save.side_effect = None
    assert bridge.set_extra("key", True) is True
    assert settings.extra["key"] is mirror.extra["key"] is True
    assert bridge.saveError == ""
    assert len(errors) == 2 and list(changes) == [["key"]]


OnboardingRig = tuple[OnboardingController, SettingsBridge, Settings, Mock, Mock]


def test_onboarding_devices_use_audio_labels() -> None:
    devices = [
        AudioDevice(1, "alsa_input.mic", "Микрофон гарнитуры", False),
        AudioDevice(2, "alsa_output.monitor", "Колонки", True),
    ]
    provider = Mock(return_value=devices)
    settings = Settings(extra={"onboarding_language_set": True})
    controller = OnboardingController(
        SettingsBridge(settings, save=Mock()), settings=settings, device_provider=provider
    )

    assert controller.devices == [
        {"id": "", "name": "Системный по умолчанию"},
        {"id": devices[0].name, "name": devices[0].label},
        {"id": devices[1].name, "name": "Колонки (звук системы)"},
    ]
    provider.assert_called_once_with()
    meta = controller.metaObject()
    prop = meta.property(meta.indexOfProperty("devices"))
    assert prop.typeName() == "QVariantList"
    assert not prop.isWritable()
    assert bytes(prop.notifySignal().name()) == b"devicesChanged"
    device_prop = meta.property(meta.indexOfProperty("device"))
    assert device_prop.typeName() == "QString"
    assert device_prop.isWritable()
    assert bytes(device_prop.notifySignal().name()) == b"deviceChanged"


@pytest.mark.parametrize(
    "error", [AudioError("audio-failed", "PRIVATE alsa_input /path"), OSError("PRIVATE /path")]
)
def test_onboarding_devices_enumeration_failure(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(extra={"onboarding_language_set": True})
    with caplog.at_level(logging.WARNING):
        controller = OnboardingController(
            SettingsBridge(settings, save=Mock()),
            settings=settings,
            device_provider=Mock(side_effect=error),
        )

    assert controller.devices == [{"id": "", "name": "Системный по умолчанию"}]
    assert controller.device == ""
    controller.next()
    assert controller.step == 2
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Не удалось получить список микрофонов" in warnings[0].getMessage()
    assert warnings[0].exc_info is None
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("via_onboarding", [False, True])
def test_onboarding_device_real_save_matches_settings_bridge(
    tmp_path: Path, via_onboarding: bool
) -> None:
    path = tmp_path / "settings.json"
    settings = Settings(extra={"onboarding_language_set": True, "unrelated": "preserved"})
    mirror = policy_mod.effective(settings, policy_mod.Policy())
    apply = Mock(spec=SettingsApply)
    bridge = SettingsBridge(
        settings, mirror=mirror, apply=apply, save=partial(settings_mod.save, path=path)
    )
    controller = OnboardingController(bridge, settings=settings, device_provider=lambda: [])
    spy = QSignalSpy(controller.deviceChanged)
    target = controller if via_onboarding else bridge
    assert controller.device == bridge.device == ""

    for index, value in enumerate(("alsa_input.mic", ""), start=1):
        assert target.setProperty("device", value)
        assert controller.device == bridge.device == value
        assert settings.extra["device"] == mirror.extra["device"] == (value or None)
        assert settings_mod.load(path) == settings
        assert json.loads(path.read_text())["device"] == (value or None)
        assert settings.extra["unrelated"] == "preserved"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert list(tmp_path.iterdir()) == [path]
        assert len(spy) == index
        apply.device.assert_called_with(value or None)
        assert target.setProperty("device", value)
        assert len(spy) == index
    # Смена микрофона перечитывает его громкость для строки в «Общих».
    assert apply.mock_calls == [
        call.device("alsa_input.mic"),
        call.microphone_state(),
        call.device(None),
        call.microphone_state(),
    ]


@pytest.mark.parametrize("initial", [None, "old mic"])
def test_onboarding_device_save_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial: str | None
) -> None:
    path = tmp_path / "settings.json"
    settings = Settings(extra={"onboarding_language_set": True})
    if initial is not None:
        settings.extra["device"] = initial
    settings_mod.save(settings, path=path)
    original = path.read_bytes()
    mirror = policy_mod.effective(settings, policy_mod.Policy())
    apply = Mock(spec=SettingsApply)
    bridge = SettingsBridge(
        settings, mirror=mirror, apply=apply, save=partial(settings_mod.save, path=path)
    )
    controller = OnboardingController(bridge, settings=settings, device_provider=lambda: [])
    spy = QSignalSpy(controller.deviceChanged)
    errors = QSignalSpy(bridge.saveErrorChanged)
    seen: list[str] = []
    controller.deviceChanged.connect(lambda: seen.append(controller.device))
    with monkeypatch.context() as patch:
        patch.setattr(
            "astra_voice.core.settings.os.replace", Mock(side_effect=OSError("PRIVATE /path"))
        )
        assert controller.setProperty("device", "new mic")

    assert controller.device == bridge.device == (initial or "")
    assert len(spy) == 1 and seen == [initial or ""]
    assert bridge.saveError == "Не удалось сохранить настройки"
    assert len(errors) == 1
    assert path.read_bytes() == original
    assert settings_mod.load(path) == settings == mirror
    assert list(tmp_path.iterdir()) == [path]
    apply.device.assert_not_called()

    assert controller.setProperty("device", "new mic")
    assert controller.device == bridge.device == "new mic"
    assert settings_mod.load(path).extra["device"] == "new mic"
    assert bridge.saveError == ""
    assert len(spy) == len(errors) == 2
    apply.device.assert_called_once_with("new mic")


@pytest.fixture
def onboarding_rig() -> OnboardingRig:
    settings = settings_mod.from_dict({"onboarding_language_set": True})
    save = Mock()
    apply = Mock(spec=SettingsApply)
    apply.hotkey.return_value = "ok"
    bridge = SettingsBridge(settings, save=save, apply=apply)
    # Ответ применения независим от keep_busy: проверяем поведение, а не сам флаг.
    host = Mock(spec=OnboardingHost)
    host.set_capture_callback = lambda _callback: None
    host.subscribe_device_resolved.return_value = ""
    host.begin_capture.return_value = True
    host.probe.return_value = "ok"
    host.free_candidates.return_value = ["Ctrl+Alt+D"]
    controller = OnboardingController(
        bridge, settings=settings, host=host, device_provider=lambda: []
    )
    host.reset_mock()
    return controller, bridge, settings, host, save


def test_capture_is_shared_by_onboarding_and_settings(onboarding_rig: OnboardingRig) -> None:
    controller, bridge, _, _, _ = onboarding_rig
    assert controller._capture is bridge._capture
    bridge.beginCapture()
    assert controller.captureState == bridge.captureState == "capturing"
    controller.cancelCapture()
    assert controller.captureState == bridge.captureState == "idle"


@pytest.mark.parametrize("screen", ["settings", "onboarding"])
def test_capture_apply_busy_does_not_save(onboarding_rig: OnboardingRig, screen: str) -> None:
    controller, bridge, settings, host, save = onboarding_rig
    apply = cast(Mock, bridge._apply)
    apply.hotkey.return_value = "busy"
    target = bridge if screen == "settings" else controller
    target.beginCapture()
    target.endCapture("Ctrl+Alt+D")
    assert target.captureState == "conflict"
    assert settings.hotkey == bridge.hotkey == "Ctrl+Space"
    assert bridge.hotkeyStatus == "busy"
    save.assert_not_called()
    assert apply.hotkey.call_args_list == [
        call("Ctrl+Alt+D", "ptt"),
        call("Ctrl+Space", "ptt"),
    ]


@pytest.mark.parametrize("screen", ["settings", "onboarding"])
def test_capture_keep_applies_once(onboarding_rig: OnboardingRig, screen: str) -> None:
    controller, bridge, settings, host, save = onboarding_rig
    apply = cast(Mock, bridge._apply)
    apply.hotkey.return_value = "busy"
    host.probe.return_value = "busy"
    target = bridge if screen == "settings" else controller
    target.endCapture("Ctrl+Alt+D")
    apply.hotkey.assert_not_called()
    target.keepCombo()
    apply.hotkey.assert_called_once_with("Ctrl+Alt+D", "ptt")
    save.assert_called_once_with(settings)
    assert target.captureState == "success"
    assert settings.hotkey == bridge.hotkey == "Ctrl+Alt+D"
    assert bridge.hotkeyStatus == "busy"
    assert not bridge.capture.keep_busy


def test_capture_expiry_reaches_gui_queued(onboarding_rig: OnboardingRig) -> None:
    controller, bridge, _, host, save = onboarding_rig
    host.set_capture_callback = Mock()
    gui_thread = threading.get_ident()
    states: list[tuple[str, int]] = []
    controller.captureStateChanged.connect(
        lambda: states.append((controller.captureState, threading.get_ident()))
    )
    controller.beginCapture()
    callback = host.set_capture_callback.call_args.args[0]
    worker = threading.Thread(target=callback, args=("expired", ""))
    worker.start()
    worker.join()
    assert controller.captureState == "capturing"
    QCoreApplication.processEvents()
    assert controller.captureState == bridge.captureState == "not-grabbed"
    assert controller.captureMessage == "Время вышло — нажмите «Изменить» ещё раз"
    assert states == [("capturing", gui_thread), ("not-grabbed", gui_thread)]
    host.end_capture.assert_called_once_with()
    save.assert_not_called()
    controller.beginCapture()
    assert controller.captureState == "capturing"
    assert controller.captureMessage == ""
    callback = host.set_capture_callback.call_args.args[0]
    worker = threading.Thread(target=callback, args=("combo", "Ctrl+Alt+D"))
    worker.start()
    worker.join()
    QCoreApplication.processEvents()
    assert controller.captureState == "success"


def test_capture_callback_attribute_error_is_reported_as_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    host = Mock(spec=CaptureHost)
    host.set_capture_callback.side_effect = AttributeError("Ошибка внутри метода")
    capture = HotkeyCapture(host)
    capture.begin(lambda _combo, _keep: "ok")
    assert capture.state == "not-grabbed"
    host.begin_capture.assert_not_called()
    host.end_capture.assert_called_once_with()
    assert "Не удалось начать захват клавиатуры" in caplog.text
    assert "Хост захвата не поддерживает передачу клавиш" not in caplog.text


def test_onboarding_window_hide_ends_capture_once(onboarding_rig: OnboardingRig) -> None:
    controller, bridge, _, host, _ = onboarding_rig
    window = QObject()
    bridge.capture.attach_window(window)
    controller.attach_window(window)
    controller.beginCapture()
    QCoreApplication.sendEvent(window, QEvent(QEvent.Hide))
    assert controller.captureState == "idle"
    assert controller._window_visible is False
    host.end_capture.assert_called_once_with()


def test_settings_window_hide_releases_capture() -> None:
    host = Mock(spec=CaptureHost)
    host.begin_capture.return_value = True
    bridge = SettingsBridge(Settings(), save=Mock(), capture_host=host)
    window = QObject()
    bridge.capture.attach_window(window)
    bridge.beginCapture()
    assert bridge.captureState == "capturing"

    QCoreApplication.sendEvent(window, QEvent(QEvent.Hide))

    assert bridge.captureState == "idle"
    host.end_capture.assert_called_once_with()


def test_capture_without_host_degrades_and_can_cancel() -> None:
    capture = HotkeyCapture()
    states = QSignalSpy(capture.captureStateChanged)
    capture.begin(lambda _combo, _keep: "ok")
    assert capture.state == "not-grabbed"
    capture.cancel()
    assert capture.state == "idle"
    assert len(states) == 2


def test_window_event_emits_only_lifecycle_events() -> None:
    capture = HotkeyCapture()
    window = QObject()
    capture.attach_window(window)
    seen: list[QEvent.Type] = []
    capture.windowEvent.connect(
        lambda _window, event: seen.append(event.type()), Qt.DirectConnection
    )

    for kind in (
        QEvent.MouseMove,
        QEvent.Show,
        QEvent.Hide,
        QEvent.Close,
        QEvent.WindowStateChange,
        QEvent.KeyPress,
    ):
        capture.eventFilter(window, QEvent(kind))

    assert seen == [QEvent.Show, QEvent.Hide, QEvent.Close, QEvent.WindowStateChange]


def test_settings_capture_without_host_degrades() -> None:
    bridge = SettingsBridge(Settings(), save=Mock())
    bridge.beginCapture()
    assert bridge.captureState == "not-grabbed"
    bridge.endCapture("")
    assert bridge.captureState == "idle"


@pytest.mark.parametrize("probe_code", ["ok", "busy"])
def test_settings_capture_saves_only_free_combo(probe_code: str) -> None:
    settings = Settings()
    save = Mock()
    host = Mock(spec=CaptureHost)
    host.begin_capture.return_value = True
    host.probe.return_value = probe_code
    apply = Mock(spec=SettingsApply)
    apply.hotkey.return_value = "ok"
    bridge = SettingsBridge(settings, save=save, apply=apply, capture_host=host)

    bridge.beginCapture()
    assert bridge.captureState == "capturing"
    bridge.endCapture("Ctrl+Alt+D")
    assert bridge.captureState == ("success" if probe_code == "ok" else "conflict")
    assert (
        bridge.hotkey == settings.hotkey == ("Ctrl+Alt+D" if probe_code == "ok" else "Ctrl+Space")
    )
    host.end_capture.assert_called_once_with()
    if probe_code == "ok":
        apply.hotkey.assert_called_once_with("Ctrl+Alt+D", "ptt")
        save.assert_called_once_with(settings)
    else:
        apply.hotkey.assert_not_called()
        save.assert_not_called()
    bridge.endCapture("")
    assert bridge.captureState == "idle"


def test_settings_keep_busy_combo_uses_hotkey_property() -> None:
    settings = Settings()
    save = Mock()
    host = Mock(spec=CaptureHost)
    host.probe.return_value = "busy"
    apply = Mock(spec=SettingsApply)
    apply.hotkey.return_value = "busy"
    bridge = SettingsBridge(settings, save=save, apply=apply, capture_host=host)

    bridge.endCapture("Ctrl+Alt+D")
    assert bridge.captureState == "conflict"
    bridge.keepCombo()
    assert bridge.captureState == "success"
    assert bridge.hotkey == settings.hotkey == "Ctrl+Alt+D"
    assert bridge.hotkeyStatus == "busy"
    apply.hotkey.assert_called_once_with("Ctrl+Alt+D", "ptt")
    save.assert_called_once_with(settings)


@pytest.mark.parametrize("probe_code", ["ok", "busy"])
def test_watchdog_signal_reaches_gui_thread_queued(
    onboarding_rig: OnboardingRig, probe_code: str
) -> None:
    controller, bridge, _, host, _ = onboarding_rig
    host.probe.return_value = probe_code
    gui_thread = threading.get_ident()
    states: list[tuple[str, int]] = []
    controller.captureStateChanged.connect(
        lambda: states.append((controller.captureState, threading.get_ident()))
    )
    controller.beginCapture()
    worker = threading.Thread(target=lambda: bridge._capture.keyEvent.emit("combo", "Ctrl+Alt+D"))
    worker.start()
    worker.join()
    assert controller.captureState == "capturing"
    QCoreApplication.processEvents()
    assert controller.captureState == ("success" if probe_code == "ok" else "conflict")
    assert states == [
        ("capturing", gui_thread),
        ("captured", gui_thread),
        (controller.captureState, gui_thread),
    ]


def test_onboarding_navigation_persists_every_transition(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = Settings(extra={"onboarding_language_set": True})
    bridge = SettingsBridge(settings, save=partial(settings_mod.save, path=path))
    controller = OnboardingController(bridge, settings=settings, device_provider=lambda: [])
    spy = QSignalSpy(controller.stepChanged)
    assert controller.step == 1 and controller.totalSteps == 5
    controller.back()
    assert not path.exists()
    for action, expected in [
        ("next", 2),
        ("skip", 3),
        ("back", 2),
        ("skip", 3),
        ("next", 4),
        ("next", 5),
    ]:
        getattr(controller, action)()
        assert controller.step == expected
        loaded = settings_mod.load(path)
        assert loaded.extra["onboarding_step"] == expected
        restored = OnboardingController(
            SettingsBridge(loaded, save=Mock()), settings=loaded, device_provider=lambda: []
        )
        assert restored.step == expected
    controller.next()
    controller.skip()
    assert controller.step == 5 and len(spy) == 6


@pytest.mark.parametrize("step", [None, True, False, "3", 3.0, 0, -1, 6, [], {}])
def test_onboarding_bad_saved_step(step: object) -> None:
    settings = Settings(extra={"onboarding_step": step, "onboarding_language_set": True})
    controller = OnboardingController(
        SettingsBridge(settings, save=Mock()), settings=settings, device_provider=lambda: []
    )
    assert controller.step == 1


def test_onboarding_navigation_save_failure(onboarding_rig: OnboardingRig) -> None:
    controller, bridge, settings, host, save = onboarding_rig
    save.side_effect = OSError()
    spy = QSignalSpy(controller.stepChanged)
    controller.next()
    assert controller.step == 1 and len(spy) == 0
    assert "onboarding_step" not in settings.extra
    assert bridge.saveError
    host.assert_not_called()


@pytest.mark.parametrize(
    ("lang", "expected"),
    [("ru_RU.UTF-8", "ru"), ("ru", "ru"), ("en_US", "en"), ("C", "en"), ("", "en")],
)
def test_onboarding_language_autodetection(
    monkeypatch: pytest.MonkeyPatch, lang: str, expected: str
) -> None:
    monkeypatch.setenv("LANG", lang)
    settings = Settings()
    bridge = SettingsBridge(settings, save=Mock())
    controller = OnboardingController(bridge, settings=settings, device_provider=lambda: [])
    assert controller.language == bridge.language == expected
    assert settings.extra["onboarding_language_set"] is True
    set_qt_property(controller, "language", "en" if expected == "ru" else "ru")
    chosen = controller.language
    monkeypatch.setenv("LANG", "ru" if chosen == "en" else "en")
    restored = OnboardingController(bridge, settings=settings, device_provider=lambda: [])
    assert restored.language == chosen


@pytest.mark.parametrize("marker", [True, False, None])
def test_onboarding_preserves_language_when_marker_exists(
    monkeypatch: pytest.MonkeyPatch, marker: bool | None
) -> None:
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    settings = Settings(language="ru", extra={"onboarding_language_set": marker})
    save = Mock()
    controller = OnboardingController(
        SettingsBridge(settings, save=save), settings=settings, device_provider=lambda: []
    )
    assert controller.language == "ru"
    save.assert_not_called()


def test_onboarding_language_failure_does_not_mark_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "en")
    settings = Settings(language="ru")
    bridge = SettingsBridge(settings, save=Mock(side_effect=OSError()))
    controller = OnboardingController(bridge, settings=settings, device_provider=lambda: [])
    assert controller.language == "ru"
    assert "onboarding_language_set" not in settings.extra
    set_qt_property(controller, "language", "xx")
    assert "onboarding_language_set" not in settings.extra


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("language", "en"),
        ("checkAppUpdates", True),
        ("checkModelUpdates", True),
        ("hotkey", "Ctrl+Alt+D"),
        ("hotkeyMode", "toggle"),
    ],
)
def test_onboarding_delegates_and_forwards_signals(
    onboarding_rig: OnboardingRig, name: str, value: str | bool
) -> None:
    controller, bridge, settings, host, save = onboarding_rig
    spy = QSignalSpy(getattr(controller, name + "Changed"))
    assert controller.setProperty(name, value)
    assert getattr(controller, name) == getattr(bridge, name) == value
    assert stored_value(settings, name) == value
    assert len(spy) == 1
    # Изменения извне также должны обновить QML мастера.
    old = SettingsBridge(Settings(), save=Mock()).property(name)
    bridge.setProperty(name, old)
    assert controller.property(name) == old and len(spy) == 2


@pytest.mark.parametrize("locked", [(), ("check_app_updates",), ("check_model_updates",)])
def test_onboarding_network_defaults_and_policy(locked: tuple[str, ...]) -> None:
    settings = Settings(extra={"onboarding_language_set": True})
    bridge = SettingsBridge(settings, locked=locked, save=Mock())
    controller = OnboardingController(bridge, settings=settings, device_provider=lambda: [])
    assert controller.checkAppUpdates is controller.checkModelUpdates is False
    assert controller.policyLocked is bool(locked)
    assert controller.policyLockedText == ("Задано администратором" if locked else "")
    for name, field in [
        ("checkAppUpdates", "check_app_updates"),
        ("checkModelUpdates", "check_model_updates"),
    ]:
        controller.setProperty(name, True)
        assert controller.property(name) is (field not in locked)


@pytest.mark.parametrize(
    ("code", "state", "message"),
    [
        ("ok", "success", ""),
        (
            "busy",
            "conflict",
            "Эта комбинация занята другой программой. Можно оставить её или выбрать другую",
        ),
        ("duplicate", "duplicate", "Эта комбинация уже назначена"),
        ("bad-combo", "not-grabbed", "Не удалось назначить комбинацию. Выберите другую"),
        ("not-grabbed", "not-grabbed", "Не удалось назначить комбинацию. Выберите другую"),
    ],
)
def test_onboarding_capture_states(
    onboarding_rig: OnboardingRig, code: str, state: str, message: str
) -> None:
    controller, bridge, settings, host, save = onboarding_rig
    host.probe.return_value = code
    spy = QSignalSpy(controller.captureStateChanged)
    messages = QSignalSpy(controller.captureMessageChanged)
    pending = QSignalSpy(controller.pendingComboChanged)
    states = []
    controller.captureStateChanged.connect(lambda: states.append(controller.captureState))
    assert controller.captureState == "idle"
    controller.beginCapture()
    assert controller.captureState == "capturing"
    assert controller.captureMessage == ""
    controller.endCapture("Ctrl+Alt+D")
    assert states == ["capturing", "captured", state]
    assert len(spy) == 3 and len(pending) == 1
    assert len(messages) == bool(message)
    assert controller.captureMessage == message
    assert controller.pendingCombo == "Ctrl+Alt+D"
    assert host.mock_calls[:3] == [
        call.begin_capture(),
        call.end_capture(),
        call.probe("Ctrl+Alt+D"),
    ]
    if code == "ok":
        assert settings.hotkey == "Ctrl+Alt+D"
        # Применение объединено: мастер и настройки идут одним путём.
        cast(Mock, bridge._apply).hotkey.assert_called_once_with("Ctrl+Alt+D", "ptt")
        save.assert_called_once()
    else:
        assert settings.hotkey == "Ctrl+Space"
        cast(Mock, bridge._apply).hotkey.assert_not_called()
        save.assert_not_called()
    controller.cancelCapture()
    assert controller.captureState == "idle" and controller.pendingCombo == ""
    assert host.end_capture.call_count == 2


@pytest.mark.parametrize("combo", ["Space", "A", "Shift+A"])
@pytest.mark.parametrize("begin", [False, True])
def test_onboarding_capture_without_modifier_is_rejected(
    onboarding_rig: OnboardingRig, combo: str, begin: bool
) -> None:
    controller, bridge, settings, host, save = onboarding_rig
    original = settings.to_dict()
    if begin:
        controller.beginCapture()
    messages = QSignalSpy(controller.captureMessageChanged)
    candidates = QSignalSpy(controller.freeCandidatesChanged)

    controller.endCapture(combo)

    host.probe.assert_not_called()
    cast(Mock, bridge._apply).hotkey.assert_not_called()
    save.assert_not_called()
    assert settings.to_dict() == original
    assert bridge.hotkey == settings.hotkey == original["hotkey"]
    assert controller.captureState == "capturing"
    assert controller.captureMessage == "Добавьте к клавише Ctrl, Alt или Win"
    assert controller.pendingCombo == combo
    assert controller.freeCandidates == ["Ctrl+Alt+D"]
    assert len(messages) == len(candidates) == 1
    host.end_capture.assert_called_once_with()
    host.free_candidates.assert_called_once_with(list(DEFAULT_CANDIDATES))
    assert host.mock_calls[-2:] == [
        call.end_capture(),
        call.free_candidates(list(DEFAULT_CANDIDATES)),
    ]

    controller.endCapture(combo)

    assert len(messages) == len(candidates) == 1
    host.probe.assert_not_called()
    cast(Mock, bridge._apply).hotkey.assert_not_called()
    save.assert_not_called()
    assert settings.to_dict() == original


def test_onboarding_valid_capture_clears_modifier_hint(onboarding_rig: OnboardingRig) -> None:
    controller, bridge, settings, host, save = onboarding_rig
    controller.beginCapture()
    controller.endCapture("A")
    assert controller.captureMessage
    messages = QSignalSpy(controller.captureMessageChanged)
    host.reset_mock()
    # Применение объединено: мастер и настройки идут одним путём; сохраняем порядок.
    calls = Mock()
    calls.attach_mock(host, "host")
    calls.attach_mock(cast(Mock, bridge._apply), "apply")

    controller.endCapture("Ctrl+Alt+D")

    # Применение объединено: мастер и настройки идут одним путём, вызов ровно один.
    assert calls.mock_calls == [
        call.host.end_capture(),
        call.host.probe("Ctrl+Alt+D"),
        call.apply.hotkey("Ctrl+Alt+D", "ptt"),
    ]
    save.assert_called_once_with(settings)
    assert settings.hotkey == bridge.hotkey == "Ctrl+Alt+D"
    assert controller.captureState == "success"
    assert controller.captureMessage == "" and len(messages) == 1


@pytest.mark.parametrize("action", ["beginCapture", "cancelCapture", "endCapture"])
def test_onboarding_modifier_hint_clears_on_restart_or_cancel(
    onboarding_rig: OnboardingRig, action: str
) -> None:
    controller, bridge, settings, host, save = onboarding_rig
    original = settings.to_dict()
    controller.beginCapture()
    controller.endCapture("Shift+A")
    assert controller.captureMessage
    messages = QSignalSpy(controller.captureMessageChanged)

    for _ in range(2):
        if action == "endCapture":
            controller.endCapture("")
        else:
            getattr(controller, action)()
        assert controller.captureState == ("capturing" if action == "beginCapture" else "idle")
        assert controller.captureMessage == "" and len(messages) == 1
        assert controller.pendingCombo == ""

    host.probe.assert_not_called()
    cast(Mock, bridge._apply).hotkey.assert_not_called()
    save.assert_not_called()
    assert settings.to_dict() == original


@pytest.mark.parametrize("failure", [False, True])
def test_onboarding_end_capture_always_releases(
    onboarding_rig: OnboardingRig, failure: bool
) -> None:
    controller, _, _, host, _ = onboarding_rig
    controller.beginCapture()
    if failure:
        host.probe.side_effect = RuntimeError("PRIVATE ERROR")
    controller.endCapture("Ctrl+Alt+D" if failure else "")
    host.end_capture.assert_called_once_with()
    assert controller.captureState == ("not-grabbed" if failure else "idle")
    if failure:
        assert host.mock_calls.index(call.end_capture()) < host.mock_calls.index(
            call.probe("Ctrl+Alt+D")
        )
    else:
        host.probe.assert_not_called()


@pytest.mark.parametrize("error", [False, True])
def test_onboarding_begin_failure(onboarding_rig: OnboardingRig, error: bool) -> None:
    controller, _, _, host, _ = onboarding_rig
    host.begin_capture.return_value = False
    if error:
        host.begin_capture.side_effect = RuntimeError()
    controller.beginCapture()
    assert controller.captureState == "not-grabbed"
    host.end_capture.assert_called_once_with()


@pytest.mark.parametrize("result", ["ok", "busy"])
def test_onboarding_keep_conflict_is_explicit(onboarding_rig: OnboardingRig, result: str) -> None:
    controller, bridge, settings, host, save = onboarding_rig
    host.probe.return_value = "busy"
    # Применение объединено: мастер и настройки идут одним путём.
    apply = cast(Mock, bridge._apply)
    apply.hotkey.return_value = result
    controller.endCapture("Ctrl+Alt+D")
    assert settings.hotkey == "Ctrl+Space"
    save.assert_not_called()
    controller.keepCombo()
    assert settings.hotkey == bridge.hotkey == "Ctrl+Alt+D"
    # Применение объединено: мастер и настройки идут одним путём, вызов ровно один.
    apply.hotkey.assert_called_once_with("Ctrl+Alt+D", "ptt")
    assert controller.captureState == "success"


def test_onboarding_hotkey_save_failure_rolls_back_apply(onboarding_rig: OnboardingRig) -> None:
    controller, bridge, settings, host, save = onboarding_rig
    save.side_effect = OSError()
    controller.endCapture("Ctrl+Alt+D")
    assert settings.hotkey == "Ctrl+Space"
    assert cast(Mock, bridge._apply).hotkey.call_args_list == [
        call("Ctrl+Alt+D", "ptt"),
        call("Ctrl+Space", "ptt"),
    ]
    assert controller.captureState == "not-grabbed" and bridge.saveError


@pytest.mark.parametrize(
    "result", ["busy", "duplicate", "bad-combo", "not-grabbed", RuntimeError()]
)
def test_onboarding_apply_failure(
    onboarding_rig: OnboardingRig, result: str | RuntimeError
) -> None:
    # Применение объединено: мастер и настройки идут одним путём.
    controller, bridge, _, _, _ = onboarding_rig
    apply = cast(Mock, bridge._apply)
    if isinstance(result, Exception):
        apply.hotkey.side_effect = result
    else:
        apply.hotkey.return_value = result
    controller.endCapture("Ctrl+Alt+D")
    states: dict[str | RuntimeError, str] = {"busy": "conflict", "duplicate": "duplicate"}
    expected = states.get(result, "not-grabbed")
    assert controller.captureState == expected


def test_onboarding_candidates_are_live_and_notify(onboarding_rig: OnboardingRig) -> None:
    controller, _, _, host, _ = onboarding_rig
    spy = QSignalSpy(controller.freeCandidatesChanged)
    assert controller.freeCandidates == []
    controller.refreshCandidates()
    host.free_candidates.assert_called_once_with(list(DEFAULT_CANDIDATES))
    assert controller.freeCandidates == ["Ctrl+Alt+D"] and len(spy) == 1
    controller.freeCandidates.append("busy")
    assert controller.freeCandidates == ["Ctrl+Alt+D"]
    host.free_candidates.return_value = []
    controller.refreshCandidates()
    assert controller.freeCandidates == [] and len(spy) == 2
    host.free_candidates.side_effect = RuntimeError()
    controller.refreshCandidates()
    assert controller.captureState == "not-grabbed"


@pytest.mark.parametrize("ready", [None, False, 1, "true", True])
def test_onboarding_finish_requires_model(
    onboarding_rig: OnboardingRig, ready: str | int | None
) -> None:
    controller, bridge, settings, host, save = onboarding_rig
    ready_spy = QSignalSpy(controller.canFinishChanged)
    done_spy = QSignalSpy(controller.doneChanged)
    bridge.set_extra("onboarding_model_ready", ready)
    assert len(ready_spy) == 1
    save.reset_mock()
    assert controller.canFinish is (ready is True)
    assert controller.done is False
    controller.finish()
    if ready is True:
        assert settings.extra["onboarding_done"] is controller.done is True
        assert host.mock_calls == [
            call.cancel_test(),
            call.reload_model(),
            call.notify_ready(settings.hotkey),
            call.hide_window(),
        ]
        save.assert_called_once_with(settings)
        assert len(done_spy) == 1
        controller.finish()
        assert len(host.mock_calls) == 4
    else:
        assert "onboarding_done" not in settings.extra
        assert host.mock_calls == [] and len(done_spy) == 0
        save.assert_not_called()


def test_onboarding_finish_save_failure(onboarding_rig: OnboardingRig) -> None:
    controller, bridge, _, host, save = onboarding_rig
    assert bridge.set_extra("onboarding_model_ready", True)
    save.side_effect = OSError()
    controller.finish()
    assert controller.done is False
    assert host.mock_calls == []


@pytest.mark.parametrize("reload_fails", [False, True])
def test_onboarding_finish_reloads_model_after_save(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, reload_fails: bool
) -> None:
    path = tmp_path / "settings.json"
    settings = settings_mod.from_dict(
        {"onboarding_language_set": True, "onboarding_model_ready": True}
    )
    host = Mock(spec=OnboardingHost)
    host.subscribe_device_resolved.return_value = ""
    bridge = SettingsBridge(settings, save=partial(settings_mod.save, path=path))
    controller = OnboardingController(
        bridge, settings=settings, host=host, device_provider=lambda: []
    )

    def reload_model() -> None:
        assert settings_mod.load(path).extra["onboarding_done"] is True
        if reload_fails:
            raise RuntimeError("worker start failed")

    host.reload_model.side_effect = reload_model
    controller.finish()
    controller.finish()

    assert controller.done is True
    assert host.mock_calls == [
        call.subscribe_device_resolved(controller._device_resolved),
        call.cancel_test(),
        call.reload_model(),
        call.notify_ready(settings.hotkey),
        call.hide_window(),
    ]
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == int(reload_fails)
    if reload_fails:
        assert "Не удалось перезагрузить модель после онбординга" in warnings[0].message
        assert warnings[0].exc_info is not None


def test_onboarding_without_host() -> None:
    settings = settings_mod.from_dict(
        {"onboarding_language_set": True, "onboarding_model_ready": True}
    )
    controller = OnboardingController(
        SettingsBridge(settings, save=Mock()), settings=settings, device_provider=lambda: []
    )
    controller.beginCapture()
    assert controller.captureState == "not-grabbed"
    controller.endCapture("Ctrl+Alt+D")
    assert controller.captureState == "not-grabbed"
    assert settings.hotkey == "Ctrl+Space"
    controller.refreshCandidates()
    assert controller.freeCandidates == []
    controller.cancelCapture()
    assert controller.captureState == "idle" and controller.pendingCombo == ""
    controller.next()
    assert controller.step == 2
    controller.finish()
    assert controller.done is True


@pytest.mark.parametrize("action", ["next", "back", "skip", "hide", "close"])
def test_onboarding_leaving_capture_releases_keyboard(
    onboarding_rig: OnboardingRig, action: str
) -> None:
    controller, _, _, host, _ = onboarding_rig
    controller.next()
    controller.next()
    controller.beginCapture()
    if action in ("hide", "close"):
        # Применение объединено: мастер и настройки идут одним путём, фильтр общий.
        window = QObject()
        controller.attach_window(window)
        QCoreApplication.sendEvent(
            window, QEvent(QEvent.Hide if action == "hide" else QEvent.Close)
        )
    else:
        getattr(controller, action)()
    host.end_capture.assert_called_once_with()
    assert controller.captureState == "idle"


class FakeModelPort:
    """Ни сети, ни файлов; ожидание отмены будится непосредственно Event."""

    def __init__(self) -> None:
        self.entry = CatalogEntry(
            id="test-model",
            revision="test-revision",
            name="Тестовая модель",
            description="",
            size_bytes=226_000_000,
            min_ram_mb=768,
            layout="test-layout",
            variant="test-variant",
            recommended=True,
            host="models.example",
            files=(),
        )
        self.ready = False
        self.damaged = False
        self.revoked: set[tuple[str, str]] = set()
        self.network = True
        self.space = True
        self.available_bytes = 42_100_000_000
        self.ram = True
        self.download_calls = 0
        self.sources: list[Path] = []
        self.result = InstallResult("ok")
        self.error: Exception | None = None
        self.block = False
        self.download_thread: int | None = None

    def recommended(self) -> CatalogEntry:
        return self.entry

    def entries(self) -> tuple[CatalogEntry, ...]:
        return (self.entry,)

    def is_revoked(self, entry: Any) -> bool:
        return (entry.id, entry.revision) in self.revoked

    def revoked_revision(self, model_id: str, revision: str) -> bool:
        return (model_id, revision) in self.revoked

    def recheck_entries(self) -> tuple[CatalogEntry, ...]:
        return ()

    def verify_files(self, entry: Any) -> tuple[bool, str]:
        raise AssertionError("У тестового порта нет файлов для перепроверки")

    def smoke(self, entry: Any) -> tuple[bool, str]:
        raise AssertionError("У тестового порта нет модели для перепроверки")

    def mark_ok(self, model_id: str, revision: str) -> None:
        raise AssertionError("У тестового порта нет записи для перепроверки")

    def mark_broken(self, model_id: str, revision: str, reason: str) -> None:
        raise AssertionError("У тестового порта нет записи для перепроверки")

    def record_state(self, model_id: str, revision: str) -> str:
        if (model_id, revision) != (self.entry.id, self.entry.revision):
            return ""
        return "ok" if self.ready else "broken" if self.damaged else ""

    def record_size_bytes(self, model_id: str, revision: str) -> int:
        return self.entry.size_bytes

    def current_ids(self) -> tuple[str, str] | None:
        return (self.entry.id, self.entry.revision) if self.ready else None

    def installed_ids(self) -> tuple[tuple[str, str], ...]:
        return ((self.entry.id, self.entry.revision),) if self.ready or self.damaged else ()

    def set_current(self, model_id: str, revision: str) -> None:
        assert (model_id, revision) == (self.entry.id, self.entry.revision)

    def remove(self, model_id: str, revision: str) -> None:
        raise AssertionError("У тестового порта нечего удалять")

    def installed_ok(self) -> bool:
        return self.ready

    def broken(self) -> bool:
        return self.damaged

    def allowed(self) -> tuple[bool, str]:
        return self.network, "Задано администратором: работа без сети" if not self.network else ""

    def disk_ok(self, size_bytes: int) -> bool:
        assert size_bytes == self.entry.size_bytes
        return self.space

    def disk_missing_bytes(self, size_bytes: int) -> int:
        return 12_500_000

    def free_bytes(self) -> int:
        return self.available_bytes

    def ram_ok(self, min_ram_mb: int) -> bool:
        assert min_ram_mb == self.entry.min_ram_mb
        return self.ram

    def mem_total_mb(self) -> float | None:
        return None

    def download(
        self, entry: CatalogEntry, *, progress: Callable[[Progress], None], cancel: threading.Event
    ) -> Path:
        self.download_calls += 1
        self.download_thread = threading.get_ident()
        progress(Progress(113_000_000, 226_000_000, 5_200_000, 25, 1, 1))
        if self.block:
            assert cancel.wait(1), "GUI не передал отмену"
        if cancel.is_set():
            raise DownloadError("cancelled")
        if self.error:
            raise self.error
        return Path("/fake/staging")

    def install_from_staging(self, entry: CatalogEntry) -> InstallResult:
        self.ready = self.result.state == "ok"
        return self.result

    def install_from_path(self, source: Path, entry: CatalogEntry) -> InstallResult:
        self.sources.append(source)
        return self.install_from_staging(entry)


class ModelPortWithMockedAllowed(FakeModelPort):
    allowed: Mock


class ModelPortWithMockedInstall(FakeModelPort):
    install_from_staging: Mock


class ModelFactory(Protocol):
    def __call__(
        self,
        model: ModelPort | None = ...,
        *,
        dialog_factory: Callable[[], str] = ...,
        clock: Callable[[], float] = ...,
        **settings_extra: object,
    ) -> OnboardingController: ...


ModelRig = tuple[FakeModelPort, ModelFactory]


@pytest.fixture
def model_rig() -> Iterator[ModelRig]:
    port = FakeModelPort()
    controllers: list[OnboardingController] = []

    def create(
        model: ModelPort | None = port,
        *,
        dialog_factory: Callable[[], str] = lambda: "",
        clock: Callable[[], float] = time.monotonic,
        **settings_extra: object,
    ) -> OnboardingController:
        settings = Settings(extra={"onboarding_language_set": True, **settings_extra})
        controller = OnboardingController(
            SettingsBridge(settings, save=Mock()),
            settings=settings,
            model=model,
            dialog_factory=dialog_factory,
            clock=clock,
            device_provider=lambda: [],
        )
        controllers.append(controller)
        return controller

    yield port, create
    for controller in controllers:
        controller.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.mark.parametrize(
    "values, expected",
    [
        ({}, "downloadable"),
        ({"entry": None, "ready": True}, "absent"),
        ({"ready": True, "damaged": True, "network": False}, "installed"),
        ({"damaged": True, "network": False}, "broken"),
        ({"network": False, "space": False}, "no-network"),
        ({"space": False, "ram": False}, "no-space"),
        ({"ram": False}, "no-ram"),
    ],
)
def test_model_initial_states(
    model_rig: ModelRig, values: dict[str, bool | None], expected: str
) -> None:
    port, create = model_rig
    for name, value in values.items():
        setattr(port, name, value)
    controller = create()
    assert controller.modelState == expected
    assert controller.canFinish == port.ready
    assert controller.modelMessage
    if expected == "no-network":
        assert controller.modelMessage == port.allowed()[1]
    elif expected == "no-space":
        assert "13 МБ" in controller.modelMessage
    elif expected == "broken":
        assert "не прошла проверку" in controller.modelMessage
        assert "заново" in controller.modelMessage


def test_model_space_message_supports_port_without_missing_bytes(
    model_rig: ModelRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, create = model_rig
    port.space = False
    monkeypatch.delattr(FakeModelPort, "disk_missing_bytes")

    controller = create()

    assert controller.modelState == "no-space"
    assert controller.modelMessage == (
        "Недостаточно места на диске для модели 226 МБ. Освободите место."
    )


def test_model_absent_and_settings_fallback(model_rig: ModelRig) -> None:
    port, create = model_rig
    controller = create(None, onboarding_model_ready=True)
    assert controller.modelState == "absent"
    assert controller.canFinish
    assert (controller.modelName, controller.modelHost, controller.modelSizeBytes) == ("", "", 0)
    assert controller.modelRam == ""
    controller.download()
    controller.installFromPath("/fake/model")
    assert controller._downloads._model_thread is None
    assert not create(port, onboarding_model_ready=True).canFinish


@pytest.mark.parametrize(
    "resource, state", [("network", "no-network"), ("space", "no-space"), ("ram", "no-ram")]
)
def test_model_download_rechecks_recovered_resource(
    model_rig: ModelRig, resource: str, state: str
) -> None:
    port, create = model_rig
    setattr(port, resource, False)
    controller = create()
    assert controller.modelState == state
    setattr(port, resource, True)
    done = QSignalSpy(controller.canFinishChanged)
    controller.download()
    assert done.wait(1000)
    assert controller.modelState == "installed"
    assert port.download_calls == 1


@pytest.mark.parametrize("resource, state", [("network", "no-network"), ("space", "no-space")])
def test_model_download_refreshes_persistent_block_message(
    model_rig: ModelRig, monkeypatch: pytest.MonkeyPatch, resource: str, state: str
) -> None:
    port, create = model_rig
    setattr(port, resource, False)
    controller = create()
    before = controller.modelMessage
    if resource == "network":
        monkeypatch.setattr(port, "allowed", lambda: (False, "Нет доступа к models.example"))
    else:
        monkeypatch.setattr(port, "disk_missing_bytes", lambda size: 1_000_000)
    changed = QSignalSpy(controller.modelMessageChanged)
    controller.download()
    assert controller.modelState == state
    assert controller.modelMessage != before
    assert len(changed) == 1
    assert controller._downloads._model_thread is None
    assert port.download_calls == 0


@pytest.mark.parametrize("initial", ["no-network", "no-space", "no-ram"])
def test_model_download_rechecks_other_resource_blocks(model_rig: ModelRig, initial: str) -> None:
    port, create = model_rig
    controller = create()
    controller._set_model_state(initial)
    port.network = False
    controller.download()
    assert controller.modelState == "no-network"
    assert controller.modelMessage == port.allowed()[1]
    assert controller._downloads._model_thread is None
    assert port.download_calls == 0


def test_model_properties_use_entry_and_have_notify(model_rig: ModelRig) -> None:
    port, create = model_rig
    port.entry = replace(port.entry, size_bytes=123_400_000, host="other.example")
    controller = create()
    assert controller.modelName == port.entry.name
    assert controller.modelHost == "other.example"
    assert controller.modelSizeBytes == port.entry.size_bytes
    assert controller.modelSize == "123 МБ"
    meta = controller.metaObject()
    for name in (
        "modelState",
        "modelName",
        "modelHost",
        "modelSizeBytes",
        "modelSize",
        "modelRam",
        "progress",
        "speed",
        "eta",
        "modelMessage",
    ):
        prop = meta.property(meta.indexOfProperty(name))
        assert prop.hasNotifySignal()
        assert not prop.isWritable()


@pytest.mark.parametrize("min_ram_mb", [415, 768, 1024])
def test_model_ram_uses_catalog_megabytes(model_rig: ModelRig, min_ram_mb: int) -> None:
    port, create = model_rig
    port.entry = replace(port.entry, min_ram_mb=min_ram_mb, size_bytes=123_400_000)
    controller = create()

    assert controller.modelRam == f"{min_ram_mb} МБ"
    assert controller.property("modelRam") == f"{min_ram_mb} МБ"
    meta = controller.metaObject()
    prop = meta.property(meta.indexOfProperty("modelRam"))
    assert prop.typeName() == "QString"
    assert prop.notifySignal().methodSignature() == b"modelRamChanged()"


def test_model_ram_without_catalog_entry(
    model_rig: ModelRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, create = model_rig
    monkeypatch.setattr(port, "recommended", lambda: None)

    assert create().modelRam == ""


@pytest.mark.parametrize("low_ram", [False, True])
def test_model_download_signals_progress_and_thread_completion(
    model_rig: ModelRig, low_ram: bool
) -> None:
    port, create = model_rig
    port.ram = not low_ram
    clock = Mock(side_effect=[0.0, 0.0, 113 / 5.2, 226 / 5.2])
    controller = create(clock=clock)
    states, values = [], []
    controller.modelStateChanged.connect(lambda: states.append(controller.modelState))
    controller.etaChanged.connect(
        lambda: values.append((controller.progress, controller.speed, controller.eta))
    )
    spies = [
        QSignalSpy(getattr(controller, name + "Changed"))
        for name in ("progress", "speed", "eta", "modelMessage")
    ]
    done = QSignalSpy(controller.canFinishChanged)
    controller.download()
    controller.download()
    assert done.wait(1000)
    assert states == ["downloading", "verifying", "installing", "installed"]
    assert (0.5, "5,2 МБ/с", "осталось меньше минуты") in values
    assert all(len(spy) > 0 for spy in spies)
    assert controller.progress == 1
    assert controller.speed == controller.eta == ""
    assert controller.canFinish
    assert controller._downloads._model_thread is None
    assert controller._downloads._model_job is None
    assert port.download_calls == 1
    assert port.download_thread != threading.get_ident()


def test_model_cancel_stops_thread_and_allows_retry(model_rig: ModelRig) -> None:
    port, create = model_rig
    port.block = True
    controller = create()
    # Сигнал прогресса гарантирует, что отмена происходит внутри download.
    controller.progressChanged.connect(controller.cancelDownload)
    done = QSignalSpy(controller.canFinishChanged)
    controller.download()
    assert done.wait(1000)
    assert controller.modelState == "cancelled"
    assert controller._downloads._model_thread is None
    assert not controller.canFinish
    controller.progressChanged.disconnect(controller.cancelDownload)
    port.block = False
    controller.download()
    assert done.wait(1000)
    assert controller.modelState == "installed"
    assert port.download_calls == 2


@pytest.mark.parametrize(
    "error, expected",
    [
        (DownloadError("disk-full"), "no-space"),
        (StoreError("disk-full"), "no-space"),
        (NetworkError("host-unreachable", "SECRET /path/service"), "no-network"),
        (DownloadError("no-network"), "no-network"),
        (DownloadError("bad-checksum"), "broken"),
        (DownloadError("bad-path"), "broken"),
        (RuntimeError("SECRET /path/service"), "broken"),
    ],
)
def test_model_download_errors_are_readable(
    model_rig: ModelRig, error: Exception, expected: str
) -> None:
    port, create = model_rig
    port.error = error
    controller = create()
    done = QSignalSpy(controller.canFinishChanged)
    controller.download()
    assert done.wait(1000)
    assert controller.modelState == expected
    assert controller.modelMessage
    assert "SECRET" not in controller.modelMessage
    assert "/path" not in controller.modelMessage
    assert controller._downloads._model_thread is None


def test_model_bad_download_path_shows_install_failure(model_rig: ModelRig) -> None:
    port, create = model_rig
    port.error = DownloadError("bad-path")
    controller = create()
    done = QSignalSpy(controller.canFinishChanged)
    controller.download()
    assert done.wait(1000)
    assert controller.modelState == "broken"
    assert controller.modelMessage == (
        "Модель не прошла проверку. Попробуйте скачать или установить её заново."
    )
    assert not controller.canFinish
    assert controller._downloads._model_thread is None


@pytest.mark.parametrize("state", ["broken", "error"])
def test_model_failed_install_is_broken_and_retryable(
    model_rig: ModelRig, state: Literal["broken", "error"]
) -> None:
    port, create = model_rig
    port.result = InstallResult(state, "SECRET /path/service")
    controller = create()
    done = QSignalSpy(controller.canFinishChanged)
    controller.download()
    assert done.wait(1000)
    assert controller.modelState == "broken"
    assert "не прошла проверку" in controller.modelMessage
    assert "SECRET" not in controller.modelMessage
    port.result = InstallResult("ok")
    controller.download()
    assert done.wait(1000)
    assert controller.modelState == "installed"


@pytest.mark.parametrize("reason", ["SECRET /path/service", ""])
@pytest.mark.parametrize("reason_code", ["selfcheck", "checksum", "layout"])
@pytest.mark.parametrize("local", [False, True])
def test_model_install_failure_message_uses_reason_code(
    model_rig: ModelRig,
    reason_code: ReasonCode,
    local: bool,
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    port, create = model_rig
    port.result = InstallResult("broken", reason, reason_code=reason_code)
    controller = create()
    displayed: list[str] = []

    def capture_messages() -> None:
        displayed.extend((controller.modelMessage, controller.downloadTitle))
        displayed.extend(card["message"] for card in controller.models)

    for signal in (
        controller.modelMessageChanged,
        controller.modelsChanged,
        controller.downloadTitleChanged,
    ):
        signal.connect(capture_messages)
    done = QSignalSpy(controller.canFinishChanged)
    if local:
        controller.installFromPath("/fake/model")
    else:
        controller.download()
    assert done.wait(1000)
    assert controller.modelState == "broken"
    # Причина установщика проходит тот же белый список, что и сообщение карточки.
    assert controller.modelMessage == (
        "Распознавание на этом компьютере не работает. Обратитесь к администратору"
        if reason_code == "selfcheck" and not reason
        else "Модель не прошла проверку. Попробуйте скачать или установить её заново."
    )
    assert controller.models[0]["message"] == controller.modelMessage
    capture_messages()
    assert displayed
    for text in (*displayed, caplog.text):
        assert "SECRET" not in text and "/path/service" not in text


@pytest.mark.parametrize(
    "reason_code, expected_state, expected_message",
    [
        (
            "revoked",
            "broken",
            "Издатель больше не рекомендует эту версию модели. "
            "Не устанавливайте её — скачайте свежую версию.",
        ),
        ("cancelled", "cancelled", "Загрузка отменена. Можно продолжить скачивание."),
    ],
)
@pytest.mark.parametrize("state", ["broken", "error"])
@pytest.mark.parametrize("local", [False, True])
def test_model_install_revoked_or_cancelled(
    model_rig: ModelRig,
    reason_code: ReasonCode,
    expected_state: str,
    expected_message: str,
    state: Literal["broken", "error"],
    local: bool,
) -> None:
    port, create = model_rig
    port.result = InstallResult(state, "SECRET /path/service", reason_code=reason_code)
    controller = create()
    done = QSignalSpy(controller.canFinishChanged)
    if local:
        controller.installFromPath("/fake/model")
    else:
        controller.download()
    assert done.wait(1000)
    assert controller.modelState == expected_state
    assert controller.modelMessage == expected_message
    assert not controller.canFinish
    assert not controller._downloads._model_cancel.is_set()
    assert controller._downloads._model_thread is None


def test_model_job_unexpected_exception_logs_traceback(caplog: pytest.LogCaptureFixture) -> None:
    port = FakeModelPort()
    port.error = RuntimeError("Installation failed")
    job = _ModelJob(port, port.entry, threading.Event())
    finished = QSignalSpy(job.finished)
    with caplog.at_level(logging.WARNING):
        job.run()
    assert finished[0][0] == "error"
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is not None
    assert caplog.records[0].exc_info[1] is port.error


@pytest.mark.parametrize("source", ["/fake/model", "file:///fake/model"])
def test_model_local_install_never_downloads(
    model_rig: ModelRig, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, create = model_rig
    port.network = False
    controller = create()
    allowed = Mock(side_effect=AssertionError("Не нужен сетевой гейт"))
    monkeypatch.setattr(port, "allowed", allowed)
    done = QSignalSpy(controller.canFinishChanged)
    controller.installFromPath(source)
    controller.installFromPath(source)
    assert done.wait(1000)
    assert controller.modelState == "installed"
    assert port.sources == [Path("/fake/model")]
    assert port.download_calls == 0
    port = cast(ModelPortWithMockedAllowed, port)
    port.allowed.assert_not_called()


@pytest.mark.parametrize("source", ["/fake/модель с пробелами", "file:///fake/model"])
def test_pick_install_path_uses_local_install(
    model_rig: ModelRig, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    port, create = model_rig
    port.network = False
    dialog = Mock(return_value=source)
    controller = create(dialog_factory=dialog)
    install = Mock(wraps=controller.installFromPath)
    monkeypatch.setattr(controller, "installFromPath", install)
    done = QSignalSpy(controller.canFinishChanged)
    assert controller.metaObject().indexOfSlot(b"pickInstallPath()") >= 0

    controller.pickInstallPath()

    dialog.assert_called_once_with()
    install.assert_called_once_with(source)
    assert done.wait(1000)
    assert controller.modelState == "installed"
    assert port.sources == [Path("/fake/model" if source.startswith("file:") else source)]
    assert port.download_calls == 0


def test_pick_install_path_cancel_does_nothing(
    model_rig: ModelRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, create = model_rig
    dialog = Mock(return_value="")
    controller = create(dialog_factory=dialog)
    install = Mock(wraps=controller.installFromPath)
    monkeypatch.setattr(controller, "installFromPath", install)
    changed = QSignalSpy(controller.modelStateChanged)

    controller.pickInstallPath()

    dialog.assert_called_once_with()
    install.assert_not_called()
    assert not changed
    assert controller._downloads._model_thread is None
    assert port.sources == []
    assert port.download_calls == 0


def test_model_shutdown_during_download_needs_no_gui_polling(model_rig: ModelRig) -> None:
    port, create = model_rig
    port.block = True
    controller = create()
    controller.progressChanged.connect(controller.shutdown)
    done = QSignalSpy(controller.canFinishChanged)
    controller.download()
    assert done.wait(1000)
    assert controller.modelState == "cancelled"
    assert controller._downloads._model_thread is None
    controller.shutdown()
    controller.download()
    assert port.download_calls == 1


def test_model_shutdown_times_out_and_logs_warning(
    model_rig: ModelRig, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:

    port, create = model_rig
    controller = create()
    release, started = threading.Event(), threading.Event()

    def install(entry: CatalogEntry) -> InstallResult:
        started.set()
        assert release.wait(15), "Тест не освободил установку"
        return InstallResult("broken", reason_code="selfcheck")

    monkeypatch.setattr(port, "install_from_staging", install)
    controller.download()
    thread = controller._downloads._model_thread
    assert thread is not None
    assert controller._downloads._model_job is not None
    job_ref = weakref.ref(controller._downloads._model_job)
    try:
        assert started.wait(1)
        before = time.monotonic()
        with caplog.at_level(logging.WARNING):
            controller.shutdown()
        elapsed = time.monotonic() - before
        assert 4.9 <= elapsed < 5.5
        assert controller._downloads._model_cancel.is_set()
        assert thread.isRunning()
        assert thread.parent() is None
        assert "не завершилась за 5 секунд" in caplog.text
        assert "выход из приложения продолжается" in caplog.text
        assert (thread, job_ref()) in model_downloads._finishing_model_threads
        assert controller._downloads._model_job is None
        assert controller._downloads._model_thread is None
        controller.shutdown()
        sip.delete(controller)
        gc.collect()
        assert sip.isdeleted(controller)
        assert not sip.isdeleted(thread)
        assert thread.isRunning()
        job = job_ref()
        assert job is not None
        assert not sip.isdeleted(job)
        assert job.thread() is thread
    finally:
        release.set()
        assert thread.wait(1000)
        controller._downloads._model_thread = None
        QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert (thread, job) not in model_downloads._finishing_model_threads


@pytest.mark.parametrize("cancelled", [False, True])
def test_model_job_can_run_synchronously_without_event_polling(cancelled: bool) -> None:
    port = FakeModelPort()
    cancel = threading.Event()
    if cancelled:
        cancel.set()
    job = _ModelJob(port, port.entry, cancel)
    finished, staged, progressed = (
        QSignalSpy(job.finished),
        QSignalSpy(job.staged),
        QSignalSpy(job.progressed),
    )
    job.run()
    assert len(finished) == 1
    assert finished[0][0] == ("cancelled" if cancelled else "installed")
    if not cancelled:
        assert list(staged) == [["downloading"], ["verifying"], ["installing"]]
        assert list(progressed) == [[0.5], [1.0]]
    else:
        assert not progressed


@pytest.mark.parametrize("ok", [True, False])
def test_smoke_adapter_builds_model_load_without_leaking_text(ok: bool) -> None:
    entry = FakeModelPort().entry
    runner = Mock(return_value=Mock(ok=ok, reason="SECRET", text="PRIVATE SPEECH"))
    smoke = make_smoke_check(runner)
    result = smoke(Path("/fake/revision"), entry)
    runner.assert_called_once_with(
        {
            "type": "model.load",
            "id": entry.id,
            "revision": entry.revision,
            "dir": "/fake/revision",
            "layout": entry.layout,
            "variant": entry.variant,
            "threads": 2,
            "min_ram_mb": entry.min_ram_mb,
        },
        cancel=None,
    )
    assert result.ok is ok
    assert result.reason == (
        "" if ok else "Модель не прошла пробное распознавание на этом компьютере"
    )
    assert result.text == ""


def test_model_service_uses_existing_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    import astra_voice.ui.model_downloads as bridges

    settings, policy = Settings(), policy_mod.Policy(values={"ca_bundle": "/fake/ca"})
    entry = FakeModelPort().entry
    catalog = Mock(entries=[entry])
    catalog.is_revoked.return_value = False
    factories = {}
    for name in (
        "Verifier",
        "load_builtin",
        "ModelStore",
        "NetworkGate",
        "HttpClient",
        "Downloader",
        "Installer",
        "SmokeRunner",
    ):
        factory = Mock()
        factories[name] = factory
        monkeypatch.setattr(bridges, name, factory)
    factories["load_builtin"].return_value = catalog
    service: ModelPort = ModelService(settings, policy)
    assert service.recommended() == entry
    factories["Verifier"].assert_called_once_with(
        "catalog",
        keyring=paths.data_dir_static() / "keys" / "release.gpg",
    )
    factories["load_builtin"].assert_called_once_with(
        factories["Verifier"].return_value,
        state_path=catalog_state.state_path(paths.state_dir()),
    )
    gate = factories["NetworkGate"].return_value
    store = factories["ModelStore"].return_value
    factories["NetworkGate"].assert_called_once_with(settings, policy)
    factories["HttpClient"].assert_called_once_with(
        gate,
        ca_bundle=Path("/fake/ca"),
        user_agent=f"astra-voice/{__version__} (+https://github.com/ArrivaRUS/astra-voice)",
    )
    factories["Downloader"].assert_called_once_with(factories["HttpClient"].return_value, store)
    assert factories["Installer"].call_args.args[0] is store
    assert callable(factories["Installer"].call_args.args[1])
    assert factories["Installer"].call_args.kwargs == {"catalog": catalog}
    service.allowed()
    gate.allowed.assert_called_once_with("download")
    store.records.return_value = [Mock(state="broken")]
    assert service.broken()
    assert not service.installed_ok()
    store.records.return_value = [Mock(state="ok")]
    assert service.installed_ok()


@pytest.mark.parametrize("from_path", [False, True])
def test_model_service_rejects_revoked_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, from_path: bool
) -> None:

    contents = {
        "v3_e2e_ctc.int8.onnx": b"\x08\x03\x12\x04test",
        "v3_e2e_ctc_vocab.txt": "а\nб\n".encode(),
        "config.json": b'{"model_type":"gigaam"}',
    }
    entry = CatalogEntry(
        id="gigaam-v3-e2e-ctc-int8",
        revision="rev-new",
        name="Модель для проверки",
        description="Крошечные файлы настоящей раскладки CTC.",
        size_bytes=sum(len(data) for data in contents.values()),
        min_ram_mb=1,
        layout="onnx-asr-gigaam-v3",
        variant="gigaam-v3-e2e-ctc",
        recommended=True,
        host="huggingface.co",
        files=tuple(
            FileSpec(name, hashlib.sha256(data).hexdigest(), len(data), f"/rev-new/{name}")
            for name, data in contents.items()
        ),
    )
    catalog = Catalog(1, 1, (RevokedEntry(entry.id, entry.revision, "Ошибка модели"),), (entry,))
    store = ModelStore(tmp_path / "models")
    source = tmp_path / "source" if from_path else store.staging_dir(entry.id, entry.revision)
    source.mkdir(parents=True, exist_ok=True)
    for name, data in contents.items():
        (source / name).write_bytes(data)
    monkeypatch.setattr(model_downloads, "load_builtin", Mock(return_value=catalog))
    monkeypatch.setattr(model_downloads, "ModelStore", Mock(return_value=store))
    service = ModelService(Settings(), policy_mod.Policy())

    result = (
        service.install_from_path(source, entry)
        if from_path
        else service.install_from_staging(entry)
    )

    assert result.state == "error"
    assert result.reason_code == "revoked"
    assert result.record is None
    assert store.records() == ()
    assert store.current() is None
    assert {file.name: file.read_bytes() for file in source.iterdir()} == contents


@pytest.mark.parametrize("from_path", [False, True])
def test_model_service_cancel_during_checksum_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, from_path: bool
) -> None:
    from astra_voice.security import verify

    contents = {
        "v3_e2e_ctc.int8.onnx": b"\x08" * ((1 << 20) + 1),
        "v3_e2e_ctc_vocab.txt": "а\nб\n".encode(),
        "config.json": b'{"model_type":"gigaam"}',
    }
    entry = replace(
        FakeModelPort().entry,
        size_bytes=sum(len(data) for data in contents.values()),
        layout="onnx-asr-gigaam-v3",
        variant="gigaam-v3-e2e-ctc",
        files=tuple(
            FileSpec(name, hashlib.sha256(data).hexdigest(), len(data), f"/model/{name}")
            for name, data in contents.items()
        ),
    )
    monkeypatch.setattr(
        model_downloads, "load_builtin", lambda verifier, **kwargs: Catalog(1, 1, (), (entry,))
    )
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    service = ModelService(Settings(), policy_mod.Policy())
    store = service._store
    staging = store.staging_dir(entry.id, entry.revision)
    source = tmp_path / "source" if from_path else staging
    source.mkdir(exist_ok=True)
    for name, data in contents.items():
        (source / name).write_bytes(data)

    read_sizes: list[int] = []

    class CancellingReader(BytesIO):
        def read(self, size: int | None = -1, /) -> bytes:
            block = super().read(size)
            read_sizes.append(len(block))
            cancel.set()
            return block

    def open_for_hash(path: Path, mode: str) -> CancellingReader:
        assert path == staging / entry.files[0].path
        assert mode == "rb"
        assert not cancel.is_set(), "Отмена должна происходить во время чтения"
        return CancellingReader(path.read_bytes())

    # Подменяем только источник байтов; Installer и sha256_file выполняются полностью.
    monkeypatch.setattr(verify, "open", open_for_hash, raising=False)
    service._cancel.set()
    for _ in range(2):
        cancel = threading.Event()
        service.set_cancel(cancel)
        read_sizes.clear()

        result = (
            service.install_from_path(source, entry)
            if from_path
            else service.install_from_staging(entry)
        )

        assert cancel.is_set()
        assert len(read_sizes) == 1 and 0 < read_sizes[0] < entry.files[0].size
        assert result.state == "error"
        assert result.reason_code == "cancelled"
        assert result.record is None
        assert store.records() == ()
        assert store.current() is None
        assert not (store.root / entry.id / entry.revision).exists()
        assert {file.name: file.read_bytes() for file in source.iterdir()} == contents
        assert staging.exists() is (not from_path)


def test_model_install_disk_error_survives_space_becoming_available(model_rig: ModelRig) -> None:
    port, create = model_rig
    port.result = InstallResult(
        "error", "На диске недостаточно места для установки модели. Освободите место."
    )
    controller = create()
    done = QSignalSpy(controller.canFinishChanged)
    controller.installFromPath("/fake/model")
    assert done.wait(1000)
    assert controller.modelState == "no-space"
    assert "13 МБ" in controller.modelMessage


@pytest.mark.parametrize(
    "fraction, speed, eta, expected",
    [
        (2, 0, -1, (1.0, "", "")),
        (-1, -1, -1, (0.0, "", "")),
        (0.5, float("nan"), float("inf"), (0.5, "", "")),
        (0.3, 1_234_567, 25.1, (0.3, "", "")),
    ],
)
def test_model_progress_limits_and_unknown_estimates(
    model_rig: ModelRig, fraction: float, speed: float, eta: float, expected: tuple[float, str, str]
) -> None:
    _, create = model_rig
    controller = create()
    controller._model_progressed(fraction, speed, eta)
    assert (controller.progress, controller.speed, controller.eta) == expected


def test_model_job_rechecks_permissions_before_network() -> None:
    port = FakeModelPort()
    port.network = False
    job = _ModelJob(port, port.entry, threading.Event())
    finished = QSignalSpy(job.finished)
    job.run()
    assert list(finished) == [["no-network", port.allowed()[1]]]
    assert port.download_calls == 0


def test_model_job_unknown_progress_and_cancel_before_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = FakeModelPort()
    cancel = threading.Event()
    job = _ModelJob(port, port.entry, cancel)
    install_from_staging = Mock()
    monkeypatch.setattr(port, "install_from_staging", install_from_staging)
    progress = QSignalSpy(job.progressed)
    finished = QSignalSpy(job.finished)
    job._progress(Progress(0, 0, 0, None, 1, 1))
    assert progress[0] == [0.0]
    job.staged.connect(lambda stage: cancel.set() if stage == "verifying" else None)
    job.run()
    assert finished[0][0] == "cancelled"
    port = cast(ModelPortWithMockedInstall, port)
    port.install_from_staging.assert_not_called()


def test_smoke_adapter_exception_does_not_leak_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = Mock(side_effect=RuntimeError("PRIVATE SPEECH"))
    result = make_smoke_check(runner)(Path("/fake/revision"), FakeModelPort().entry)
    assert not result.ok
    assert result.reason == "Модель не прошла пробное распознавание на этом компьютере"
    assert result.text == ""
    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is not None


@pytest.mark.parametrize("local", [False, True])
def test_model_shutdown_cancels_active_smoke_check(
    model_rig: ModelRig, monkeypatch: pytest.MonkeyPatch, local: bool
) -> None:

    port, create = model_rig
    started, stopped = threading.Event(), threading.Event()
    store = Mock()
    store.records.return_value = []
    gate = Mock()
    gate.allowed.return_value = (True, "")
    catalog = Mock(entries=[port.entry])
    catalog.is_revoked.return_value = False
    monkeypatch.setattr(model_downloads, "load_builtin", Mock(return_value=catalog))
    monkeypatch.setattr(model_downloads, "Verifier", Mock())
    monkeypatch.setattr(model_downloads, "ModelStore", Mock(return_value=store))
    monkeypatch.setattr(model_downloads, "NetworkGate", Mock(return_value=gate))
    monkeypatch.setattr(model_downloads, "HttpClient", Mock())
    monkeypatch.setattr(model_downloads, "Downloader", Mock())

    def run_smoke(request: object, *, cancel: Callable[[], bool]) -> Mock:
        assert not cancel(), "Отмена предыдущей попытки не должна попадать в новую"
        started.set()
        deadline = time.monotonic() + 2
        while not cancel() and time.monotonic() < deadline:
            time.sleep(0.005)
        if cancel():
            stopped.set()
        return Mock(ok=False)

    runner = Mock(side_effect=run_smoke)
    monkeypatch.setattr(model_downloads, "SmokeRunner", Mock(return_value=runner))

    def installer_factory(
        store: object, smoke: Callable[..., object], catalog: Catalog | None = None
    ) -> Mock:
        def install(*args: object, cancel: Callable[[], bool]) -> InstallResult:
            smoke(Path("/fake/model"), port.entry)
            return InstallResult("broken", reason_code="selfcheck")

        return Mock(
            install_from_path=Mock(side_effect=install),
            install_from_staging=Mock(side_effect=install),
        )

    monkeypatch.setattr(model_downloads, "Installer", installer_factory)
    service = ModelService(Settings(), policy_mod.Policy())
    monkeypatch.setattr(service, "free_bytes", Mock(return_value=port.available_bytes))
    controller = create(service)
    # Проверка создаётся до привязки Event; попытка должна видеть новый Event.
    service.set_cancel(controller._downloads._model_cancel)
    controller._downloads._model_cancel.set()
    done = QSignalSpy(controller.canFinishChanged)
    if local:
        controller.installFromPath("/fake/model")
    else:
        controller.download()
    assert started.wait(1)
    controller.shutdown()
    assert stopped.is_set()
    assert controller._downloads._model_thread is not None
    assert not controller._downloads._model_thread.isRunning()
    if not done:
        assert done.wait(1000)
    assert controller.modelState == "cancelled"
    runner.assert_called_once()


@pytest.mark.parametrize("service_fails", [False, True])
@pytest.mark.parametrize("loop_fails", [False, True])
def test_model_app_assembly_and_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    service_fails: bool,
    loop_fails: bool,
) -> None:
    # Общий стенд запуска приложения уже подменяет все внешние ресурсы.
    from test_app_shutdown import Rig

    from astra_voice import app as app_mod

    rig = Rig(monkeypatch, tmp_path)
    model = FakeModelPort()
    factory = Mock(return_value=model)
    if service_fails:
        factory.side_effect = RuntimeError("SECRET /catalog/path")
    monkeypatch.setattr(model_downloads, "ModelService", factory)
    original_shutdown = OnboardingController.shutdown
    shutdown_call = Mock()
    rig.calls.attach_mock(shutdown_call, "onboarding_shutdown")

    def shutdown(controller: OnboardingController) -> None:
        shutdown_call()
        original_shutdown(controller)

    monkeypatch.setattr(OnboardingController, "shutdown", shutdown)
    if loop_fails:
        rig.app.exec_.side_effect = RuntimeError("Цикл остановлен")
    with caplog.at_level(logging.WARNING):
        if loop_fails:
            with pytest.raises(RuntimeError, match="Цикл остановлен"):
                app_mod.main([])
        else:
            assert app_mod.main([]) == 7
    factory.assert_called_once_with(rig.settings, policy_mod.Policy())
    properties = dict(
        item.args for item in rig.shell.rootContext().setContextProperty.call_args_list
    )
    controller = properties["onboarding"]
    assert controller._downloads.model is (None if service_fails else model)
    assert controller.modelState == ("absent" if service_fails else "downloadable")
    shutdown_call.assert_called_once_with()
    assert rig.calls.mock_calls.index(call.onboarding_shutdown()) < rig.calls.mock_calls.index(
        call.shutdown()
    )
    rig.cleanup.assert_called_once_with(rig.server, rig.lock)
    if service_fails:
        assert "каталог моделей" in caplog.text
        assert "SECRET" not in caplog.text


class MicrophoneBridgeRig:
    """Настоящие мост и автомат; внешние эффекты заменены наблюдаемыми портами."""

    def __init__(self, *, with_host: bool = True, model_ready: bool = True) -> None:
        self.settings = settings_mod.from_dict(
            {
                "onboarding_step": 4,
                "onboarding_language_set": True,
                "onboarding_model_ready": model_ready,
                "device": "selected-mic",
            }
        )
        self.save = Mock()
        self.bridge = SettingsBridge(self.settings, save=self.save)
        self.commands: list[dict[str, Any]] = []
        self.paste = Mock()
        self.stats = Mock()
        self.pill = Mock()
        self.tray = Mock()
        self.notify = Mock()
        self.generation = 1
        self.resolved_callback: Callable[[str], None] | None = None
        self.recording = Mock()
        self.core = DictationOrchestrator(
            send=self.send,
            generation=lambda: self.generation,
            restart_worker=Mock(),
            pill=self.pill,
            tray=self.tray,
            paste=self.paste,
            active_window=Mock(),
            schedule=Mock(return_value=object()),
            cancel_timer=Mock(),
            hotkey_done=Mock(),
            hotkey_cancel=Mock(),
            hotkey_idle=lambda: True,
            set_recording=self.recording,
            paste_mode=lambda: PasteMode.AUTO,
            record_params=lambda: {"device": "ordinary-mic", "limit_s": 120},
            stats=self.stats,
            on_device_selected=self.notify,
            on_device_changed=self.notify,
            on_device_lost=self.notify,
            on_device_resolved=self.device_resolved,
        )
        self.host = Mock(spec=OnboardingHost)
        self.host.subscribe_device_resolved.side_effect = self.subscribe_device_resolved
        self.host.start_level_monitor.side_effect = self.core.start_level_monitor
        self.host.stop_level_monitor.side_effect = self.core.stop_level_monitor
        self.host.start_test.side_effect = self.core.start_test
        self.host.stop_test.side_effect = self.core.stop_test
        self.host.cancel_test.side_effect = self.core.cancel_test
        self.controller = OnboardingController(
            self.bridge,
            settings=self.settings,
            host=self.host if with_host else None,
            device_provider=lambda: [],
        )

    def subscribe_device_resolved(self, callback: Callable[[str], None] | None) -> str:
        self.resolved_callback = callback
        return self.core.resolved_device

    def device_resolved(self, name: str) -> None:
        if self.resolved_callback is not None:
            self.resolved_callback(name)

    def send(self, message: dict[str, Any], *, timeout: float | None = None) -> None:
        self.commands.extend(ipc.FrameReader().feed(ipc.encode(message)))

    def event(self, kind: str, **fields: object) -> None:
        uid = next(
            msg["utterance_id"] for msg in reversed(self.commands) if msg["type"] == "record.start"
        )
        message = {"type": kind, "utterance_id": uid, **fields}
        if kind == "result":
            message.update(infer_ms=5.0, audio_ms=1000.0)
        event = ipc.FrameReader().feed(ipc.encode(message))[0]
        self.core.on_worker_event({**event, "generation": self.generation})


@pytest.fixture
def microphone_bridge() -> MicrophoneBridgeRig:
    return MicrophoneBridgeRig()


def test_microphone_resolved_device_signal_and_worker_restart(
    microphone_bridge: MicrophoneBridgeRig,
) -> None:
    rig = microphone_bridge
    controller = rig.controller
    spy = QSignalSpy(controller.deviceResolvedChanged)
    assert controller.deviceResolved == ""
    meta = controller.metaObject()
    prop = meta.property(meta.indexOfProperty("deviceResolved"))
    assert prop.typeName() == "QString"
    assert not prop.isWritable()
    assert bytes(prop.notifySignal().name()) == b"deviceResolvedChanged"
    controller.startTest()
    rig.event("audio.ready", device="USB-микрофон")
    assert controller.deviceResolved == "USB-микрофон" and len(spy) == 1
    rig.event("audio.ready", device="USB-микрофон")
    # Повтор от порта тоже не должен создавать лишний сигнал Qt.
    rig.device_resolved("USB-микрофон")
    assert len(spy) == 1
    rig.generation += 1
    rig.core.on_worker_event({"type": "hello", "generation": rig.generation})
    assert controller.deviceResolved == "" and len(spy) == 2
    rig.device_resolved("")
    assert len(spy) == 2
    controller.shutdown()
    rig.host.subscribe_device_resolved.assert_called_with(None)
    assert rig.resolved_callback is None
    rig.device_resolved("Другой микрофон")
    assert controller.deviceResolved == "" and len(spy) == 2


def test_microphone_resolved_device_initial_value() -> None:
    settings = Settings(extra={"onboarding_language_set": True})
    host = Mock(spec=OnboardingHost)
    host.subscribe_device_resolved.return_value = "Встроенный микрофон"
    controller = OnboardingController(
        SettingsBridge(settings, save=Mock()),
        settings=settings,
        host=host,
        device_provider=lambda: [],
    )
    assert controller.deviceResolved == "Встроенный микрофон"
    controller.shutdown()


@pytest.mark.parametrize("operation", ["subscribe", "unsubscribe"])
def test_microphone_resolved_device_host_failure(
    caplog: pytest.LogCaptureFixture, operation: str
) -> None:
    settings = Settings(extra={"onboarding_language_set": True})
    host = Mock(spec=OnboardingHost)
    host.subscribe_device_resolved.return_value = ""
    if operation == "subscribe":
        host.subscribe_device_resolved.side_effect = RuntimeError("Сбой подписки")
    controller = OnboardingController(
        SettingsBridge(settings, save=Mock()),
        settings=settings,
        host=host,
        device_provider=lambda: [],
    )
    assert controller.deviceResolved == ""
    if operation == "unsubscribe":
        host.subscribe_device_resolved.side_effect = RuntimeError("Сбой отписки")
        controller.shutdown()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert caplog.records[0].exc_info is not None


def test_microphone_properties_and_full_cycle(
    microphone_bridge: MicrophoneBridgeRig, caplog: pytest.LogCaptureFixture
) -> None:
    rig = microphone_bridge
    controller = rig.controller
    caplog.set_level(logging.DEBUG)
    assert controller.testState == "idle"
    assert controller.level == 0.0
    assert (
        controller.peak
        == controller.testText
        == controller.testDuration
        == controller.testMessage
        == ""
    )
    assert controller.testPhrase
    assert not any(word in controller.testPhrase.lower() for word in SMOKE_EXPECT_ANY)
    names = ("level", "peak", "testPhrase", "testState", "testText", "testDuration", "testMessage")
    spies = {name: QSignalSpy(getattr(controller, name + "Changed")) for name in names}
    states: list[str] = []
    core_states: list[tuple[DictationPhase, bool]] = []
    controller.testStateChanged.connect(lambda: states.append(controller.testState))
    controller.testStateChanged.connect(
        lambda: core_states.append((rig.core.phase, rig.core.test_active))
    )
    controller.startTest()
    controller.startTest()
    assert controller.testState == "recording"
    assert len(rig.commands) == 1
    assert rig.commands[0]["device"] == controller.device == "selected-mic"
    rig.event("audio.ready", device="USB-микрофон")
    rig.event("level", peak_dbfs=-18.0, rms_dbfs=-25.0)
    assert controller.level == pytest.approx(0.7)
    assert controller.peak == "−18 дБ"
    assert len(spies["level"]) == len(spies["peak"]) == 1
    rig.event("level", peak_dbfs=-18.0, rms_dbfs=-25.0)
    assert len(spies["level"]) == len(spies["peak"]) == 1
    rig.event("silent")
    assert controller.testState == "processing"
    assert controller.level == 0.0
    rig.event("result", text="Личная фраза микрофона", t_ms=310)
    assert states == ["recording", "processing", "done"]
    assert core_states == [
        (DictationPhase.RECORDING, True),
        (DictationPhase.PROCESSING, True),
        (DictationPhase.IDLE, False),
    ]
    assert [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("диктовка: фаза ")
    ] == [
        f"диктовка: фаза {phase.value}"
        for phase in (DictationPhase.RECORDING, DictationPhase.PROCESSING, DictationPhase.IDLE)
    ]
    assert controller.testText == "Личная фраза микрофона"
    assert controller.testDuration == "0,31 с"
    assert controller.testMessage == ""
    assert len(spies["testText"]) == len(spies["testDuration"]) == 1
    assert rig.core.last_text is None
    rig.paste.assert_not_called()
    rig.stats.append.assert_not_called()
    rig.notify.assert_not_called()
    assert "Личная фраза микрофона" not in caplog.text
    assert "Личная фраза микрофона" not in str(rig.settings.to_dict())
    assert "Личная фраза микрофона" not in str(rig.pill.mock_calls + rig.tray.mock_calls)
    rig.save.assert_not_called()
    controller.startTest()
    assert controller.testState == "recording"
    assert controller.testText == controller.testDuration == controller.peak == ""


@pytest.mark.parametrize("dbfs, expected", [(-90.0, 0.0), (-30.0, 0.5), (6.0, 1.0)])
def test_microphone_level_uses_dictation_scale(
    microphone_bridge: MicrophoneBridgeRig, dbfs: float, expected: float
) -> None:
    rig = microphone_bridge
    rig.controller.startTest()
    rig.event("level", peak_dbfs=dbfs, rms_dbfs=-40.0)
    assert rig.controller.level == expected


def test_microphone_preparing_blocks_repeat_start_and_allows_stop(
    microphone_bridge: MicrophoneBridgeRig,
) -> None:
    rig = microphone_bridge

    def prepare(device: str, callback: Callable[[MicrophoneTestUpdate], None]) -> bool:
        callback(MicrophoneTestUpdate("preparing", message=TEST_PREPARING))
        return True

    rig.host.start_test.side_effect = prepare
    rig.controller.startTest()
    assert rig.controller.testState == "preparing"
    assert rig.controller.testMessage == TEST_PREPARING
    assert rig.controller.level == 0.0
    rig.controller.startTest()
    rig.host.start_test.assert_called_once()
    rig.controller.stopTest()
    rig.host.stop_test.assert_called_once_with()
    callback = rig.host.start_test.call_args.args[1]
    callback(MicrophoneTestUpdate("idle"))
    assert rig.controller.testState == "idle"
    assert rig.controller.testMessage == ""
    assert rig.commands == []


def test_microphone_preparing_resets_level_and_recording_clears_message(
    microphone_bridge: MicrophoneBridgeRig,
) -> None:
    controller = microphone_bridge.controller
    controller._test_updated(MicrophoneTestUpdate("recording", peak_dbfs=-18.0))
    assert controller.level > 0.0
    controller._test_updated(
        MicrophoneTestUpdate("preparing", message=TEST_PREPARING, peak_dbfs=-10.0)
    )
    assert controller.level == 0.0
    assert controller.testMessage == TEST_PREPARING
    controller._test_updated(MicrophoneTestUpdate("recording"))
    assert controller.testMessage == ""
    assert controller.level == 0.0


def test_microphone_early_stop_and_cancel(microphone_bridge: MicrophoneBridgeRig) -> None:
    rig = microphone_bridge
    rig.controller.startTest()
    rig.controller.stopTest()
    assert rig.controller.testState == "processing"
    rig.controller.stopTest()
    assert [msg["type"] for msg in rig.commands] == [
        "record.start",
        "record.stop",
        "recognize",
        "record.cancel",
    ]
    rig.event("result", text="Отменённая речь", t_ms=12)
    assert rig.controller.testText == ""
    rig.event("cancelled")
    assert rig.controller.testState == "idle"
    assert rig.controller.testDuration == ""
    rig.stats.append.assert_not_called()
    rig.paste.assert_not_called()


@pytest.mark.parametrize(
    "code", ["audio-no-device", "audio-busy", "audio-failed", "no-model", "engine-failed"]
)
def test_microphone_error_does_not_expose_worker_details(
    microphone_bridge: MicrophoneBridgeRig, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig = microphone_bridge
    caplog.set_level(logging.DEBUG)
    rig.controller.startTest()
    rig.event("error", code=code, message="ЛИЧНОЕ /home/astra/secret alsa_input.usb service.unit")
    assert rig.controller.testState == "error"
    assert rig.controller.testMessage
    assert rig.controller.testText == rig.controller.testDuration == ""
    for forbidden in ("ЛИЧНОЕ", "/home", "alsa_input", "service.unit"):
        assert forbidden not in rig.controller.testMessage
        assert forbidden not in caplog.text
    rig.stats.append.assert_not_called()
    rig.notify.assert_not_called()


@pytest.mark.parametrize("state", ["recording", "processing", "done"])
@pytest.mark.parametrize("leave", ["next", "back", "skip", "hide", "close", "device", "shutdown"])
def test_microphone_text_is_erased_on_leaving_and_late_events_are_ignored(
    microphone_bridge: MicrophoneBridgeRig, state: str, leave: str
) -> None:
    rig = microphone_bridge
    controller = rig.controller
    controller.startTest()
    callback = rig.host.start_test.call_args.args[1]
    if state != "recording":
        controller.stopTest()
    if state == "done":
        rig.event("result", text="Личная речь", t_ms=310)
    if leave in ("hide", "close"):
        controller.eventFilter(controller, QEvent(QEvent.Hide if leave == "hide" else QEvent.Close))
    elif leave == "device":
        controller.setProperty("device", "another-mic")
    else:
        getattr(controller, leave)()
    assert controller.testState == "idle"
    assert controller.testText == controller.testDuration == controller.peak == ""
    assert controller.level == 0.0
    if state != "done":
        assert rig.commands[-1]["type"] == "record.cancel"
    rig.event("result", text="Запоздалая речь", t_ms=310)
    # Даже сохранённый старый callback не должен вернуть текст после повторного входа.
    controller._go(4)
    callback(MicrophoneTestUpdate("done", text="Запоздалая речь", duration_s=0.31))
    assert controller.testText == ""
    assert controller._test_text == ""
    rig.paste.assert_not_called()
    rig.stats.append.assert_not_called()


def test_microphone_without_host_is_idle() -> None:
    rig = MicrophoneBridgeRig(with_host=False)
    rig.controller.startTest()
    rig.controller.stopTest()
    assert rig.controller.testState == "idle"
    assert rig.controller.testText == rig.controller.peak == rig.controller.testDuration == ""
    assert rig.commands == []


def test_microphone_host_exception_is_not_logged(
    microphone_bridge: MicrophoneBridgeRig, caplog: pytest.LogCaptureFixture
) -> None:
    rig = microphone_bridge
    caplog.set_level(logging.DEBUG)
    rig.host.start_test.side_effect = RuntimeError("Личная речь /path alsa_input.foo")
    rig.controller.startTest()
    assert rig.controller.testState == "error"
    assert rig.controller.testMessage == "Не удалось распознать речь. Попробуйте ещё раз."
    assert "Личная речь" not in caplog.text


@pytest.mark.parametrize("indicator", ["tray", "set_recording"])
@pytest.mark.parametrize("leave", ["_clear_test", "back"])
@pytest.mark.parametrize("cancel_fails", [False, True])
def test_microphone_start_failure_cancels_even_from_error_state(
    microphone_bridge: MicrophoneBridgeRig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    indicator: str,
    leave: str,
    cancel_fails: bool,
) -> None:
    rig = microphone_bridge
    caplog.set_level(logging.DEBUG)
    private = "Личная речь /path alsa_input.foo"

    def fail_on_start(value: object) -> None:
        if value is True or value is TrayState.LISTENING:
            raise RuntimeError(private)

    def cancel_test() -> None:
        if cancel_fails and rig.host.cancel_test.call_count == 2:
            raise RuntimeError(private)
        rig.core.cancel_test()

    target, name = (rig.tray, "set_state") if indicator == "tray" else (rig.core, "_set_recording")
    monkeypatch.setattr(target, name, fail_on_start)
    rig.host.cancel_test.side_effect = cancel_test
    rig.controller.startTest()
    assert rig.controller.testState == "error"
    assert rig.controller.testMessage == "Не удалось распознать речь. Попробуйте ещё раз."
    assert rig.host.cancel_test.call_args_list == [call(), call()]
    assert [msg["type"] for msg in rig.commands] == (
        ["record.start"] if cancel_fails else ["record.start", "record.cancel"]
    )
    if not cancel_fails:
        rig.event("cancelled")
        assert not rig.core.test_active
        assert rig.core.phase == DictationPhase.IDLE
        assert rig.controller.testState == "error"
    getattr(rig.controller, leave)()
    assert rig.host.cancel_test.call_args_list == [call(), call(), call()]
    assert [msg["type"] for msg in rig.commands] == ["record.start", "record.cancel"]
    if cancel_fails:
        rig.event("cancelled")
    assert not rig.core.test_active
    assert rig.core.phase == DictationPhase.IDLE
    assert rig.controller.testState == "idle"
    assert private not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("command", ["record.start", "record.stop", "recognize"])
def test_microphone_command_failure_logs_warning_without_exception(
    microphone_bridge: MicrophoneBridgeRig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    command: str,
) -> None:
    rig = microphone_bridge
    caplog.set_level(logging.DEBUG)
    private = "Личная речь /path alsa_input.foo"

    def fail(message: dict[str, Any], *, timeout: float | None = None) -> None:
        if message["type"] == command:
            raise RuntimeError(private)
        rig.send(message, timeout=timeout)

    monkeypatch.setattr(rig.core, "_send", fail)
    rig.controller.startTest()
    if command != "record.start":
        rig.controller.stopTest()
    assert rig.controller.testState == "error"
    assert rig.core.phase == DictationPhase.IDLE
    assert not rig.core.test_active
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert [record.getMessage() for record in warnings] == [
        f"диктовка: отправка команды {command} не удалась"
    ]
    assert all(record.exc_info is None for record in caplog.records)
    assert private not in caplog.text


def _microphone_log_violations(
    source: str,
    private_roots: Collection[str] = ("self._test_text", "self.testText"),
    log_prefixes: Collection[str] = ("log.",),
) -> list[int]:
    """Как гейт notify: AST, раскрытие псевдонимов и цепочек присваиваний."""
    # ИБ-13: проверяем явный поток приватных значений в аргументы журнала, включая
    # log.exception(text) и exc_info=RuntimeError(text). Неявный поток через текущее
    # исключение (log.exception("сбой"), exc_info=True/err/(type(err), err, tb))
    # остаётся в поведенческих тестах, перечисленных ниже.
    #
    # Проверенный вариант запрета этих форм в функции, где приватное имя читается,
    # присваивается или приходит параметром, ложно отвергает OnboardingController
    # .__init__: self._test_text = "" соседствует с exc_info=True при ошибке
    # host.subscribe_device_resolved. Runtime.subscribe_device_resolved лишь
    # сохраняет callback и возвращает имя устройства; распознанной речи здесь нет.
    # test_microphone_resolved_device_host_failure намеренно требует traceback.
    # Исключить пустую инициализацию означало бы ослабить правило присваиваний,
    # а исключить конструктор по имени — скрыть будущую настоящую утечку.
    # Разрешение безопасного кортежа не решает этот случай: здесь exc_info=True.
    #
    # DictationOrchestrator._safe_ui, напротив, не обращается к приватным именам
    # и подменяет исключение свежим RuntimeError(message) со служебным сообщением,
    # сохраняя только error.__traceback__. Запрет любого exc_info сломал бы и его;
    # наличие приватных имён в других методах не делает этот метод чувствительным.
    # Для точного общего запрета нужен анализ происхождения исключений и вызовов,
    # которого этот небольшой AST-сторож не выполняет. Не вводим разрешений по
    # именам функций и не меняем законное журналирование ради прохождения гейта.
    #
    # Неявные утечки проверяют тесты этого файла:
    # - test_microphone_host_exception_is_not_logged: приватная строка из ошибки
    #   start_test отсутствует в caplog.text;
    # - test_microphone_start_failure_cancels_even_from_error_state: ошибки старта
    #   и отмены не оставляют ни приватной строки, ни record.exc_info;
    # - test_microphone_command_failure_logs_warning_without_exception: ошибки
    #   record.start/record.stop/recognize не оставляют приватной строки, и
    #   assert all(record.exc_info is None for record in caplog.records).
    # Эти поведенческие проверки обязательны вместе с AST-гейтом: его зелёный
    # результат сам по себе не гарантирует отсутствие утечки через исключение.
    nodes = list(ast.walk(ast.parse(source)))
    aliases: dict[str, str] = {}
    private = set(private_roots)
    prefixes = tuple(log_prefixes)

    def name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{name(node.value)}.{node.attr}"
        return ""

    for node in nodes:
        if isinstance(node, ast.arg) and isinstance(node.annotation, ast.Name):
            if node.annotation.id == "MicrophoneTestUpdate":
                private.add(node.arg)

    def sensitive(node: ast.AST) -> bool:
        return any(name(child) in private for child in ast.walk(node))

    for _ in range(len(nodes)):
        added = False
        for node in nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                target_name = name(target)
                if sensitive(node.value) and target_name and target_name not in private:
                    private.add(target_name)
                    added = True
                value_name = name(node.value)
                if (
                    isinstance(target, ast.Name)
                    and value_name.startswith(prefixes)
                    and target.id not in aliases
                ):
                    aliases[target.id] = value_name
                    added = True
        if not added:
            break
    return [
        node.lineno
        for node in nodes
        if isinstance(node, ast.Call) and name(node.func).startswith(prefixes)
        if any(sensitive(arg) for arg in [*node.args, *(kw.value for kw in node.keywords)])
    ]


@pytest.mark.parametrize(
    "path, private_roots, log_prefixes",
    [
        ("ui/bridges.py", {"self._test_text", "self.testText"}, {"log."}),
        ("core/dictation.py", {"self._last_text", "self._test_text", "text"}, {"self._log."}),
    ],
)
def test_microphone_text_never_reaches_log_ast(
    path: str, private_roots: set[str], log_prefixes: set[str]
) -> None:
    source = Path(__file__).resolve().parents[2] / "src/astra_voice" / path
    assert (
        _microphone_log_violations(source.read_text(encoding="utf-8"), private_roots, log_prefixes)
        == []
    )


@pytest.mark.parametrize(
    "body",
    [
        'log.info("%s", update.text)',
        'phrase = update.text\nlog.debug(f"{phrase}")',
        'phrase = update.text\ncopy = phrase\nwrite = log.warning\nwrite("%s", copy)',
        'log.error("failure", extra={"text": self.testText})',
        "log.debug(self._test_text)",
        'log.info("%s", update)',
        "text = update.text\nlog.exception(text)",
        'log.exception("%r", update.text)',
        'log.warning("failure", exc_info=RuntimeError(update.text))',
        'log.warning("failure", exc_info=(RuntimeError, RuntimeError(update.text), tb))',
        'log.warning("failure", exc_info=(RuntimeError, RuntimeError(repr(update.text)), tb))',
        'log.warning("failure", exc_info=(RuntimeError, RuntimeError(update.text[:20]), tb))',
        'log.warning("failure", exc_info=RuntimeError(update.text.encode()))',
    ],
)
@pytest.mark.parametrize("logger", ["log", "self._log"])
def test_microphone_privacy_ast_gate_detects_leaks(body: str, logger: str) -> None:
    body = body.replace("log.", f"{logger}.")
    source = "def receive(self, update: MicrophoneTestUpdate):\n" + "\n".join(
        "    " + line for line in body.splitlines()
    )
    assert _microphone_log_violations(source, log_prefixes={f"{logger}."})


@pytest.mark.parametrize(
    "source",
    [
        'def receive(update: MicrophoneTestUpdate):\n    log.warning("failure")',
        'def receive(update: MicrophoneTestUpdate):\n    log.warning("failure", exc_info=False)',
        'def receive(update: MicrophoneTestUpdate):\n    log.warning("failure", exc_info=None)',
        "def receive(update: MicrophoneTestUpdate):\n"
        '    log.warning("failure", exc_info=(RuntimeError, RuntimeError("failure"), tb))',
        "def receive(update: MicrophoneTestUpdate):\n"
        '    message = "failure"\n'
        "    log.warning(message, exc_info=(RuntimeError, RuntimeError(message), tb))",
        'def report():\n    log.exception("failure")',
        'def report():\n    log.warning("failure", exc_info=True)',
        "def helper(self, callback, message: str):\n"
        "    try:\n"
        "        callback()\n"
        "    except Exception as error:\n"
        "        log.warning(message,\n"
        "            exc_info=(RuntimeError, RuntimeError(message), error.__traceback__))\n",
    ],
)
@pytest.mark.parametrize("logger", ["log", "self._log"])
def test_microphone_privacy_ast_gate_allows_safe_logging(source: str, logger: str) -> None:
    # Вариант _safe_ui назван helper: безопасность не зависит от имени функции.
    source = source.replace("log.", f"{logger}.")
    assert _microphone_log_violations(source, log_prefixes={f"{logger}."}) == []


@pytest.mark.parametrize("root", ["text", "self._last_text", "self._test_text"])
def test_microphone_privacy_ast_gate_tracks_dictation_roots(root: str) -> None:
    source = (
        "def _result(self, text: str):\n"
        f"    phrase = {root}\n"
        "    copy: str = phrase\n"
        "    write = self._log.warning\n"
        "    emit = write\n"
        '    emit("%s", copy)\n'
    )
    assert _microphone_log_violations(source, {root}, {"self._log."}) == [6]


class QueueModelPort(FakeModelPort):
    """Две модели с отдельными записями и управляемым завершением загрузок."""

    def __init__(self) -> None:
        super().__init__()
        self.entry = replace(self.entry, size_bytes=100_000_000)
        self.second = replace(
            self.entry, id="second", name="Вторая модель", recommended=False, size_bytes=300_000_000
        )
        self.catalog = (self.entry, self.second)
        self.records: dict[tuple[str, str], str] = {}
        self.current: tuple[str, str] | None = None
        self.visits: list[str] = []
        self.installs: list[str] = []
        self.errors: dict[str, Exception] = {}
        self.releases = {entry.id: threading.Event() for entry in self.catalog}
        self.active = 0
        self.max_active = 0
        self.available_bytes = 1_000_000_000
        self.restore_calls: list[tuple[str, str]] = []

    def entries(self) -> tuple[CatalogEntry, ...]:
        return self.catalog

    def record_state(self, model_id: str, revision: str) -> str:
        return self.records.get((model_id, revision), "")

    def current_ids(self) -> tuple[str, str] | None:
        return self.current

    def installed_ids(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.records)

    def set_current(self, model_id: str, revision: str) -> None:
        assert self.record_state(model_id, revision) == "ok"
        self.current = (model_id, revision)
        self.restore_calls.append(self.current)

    def installed_ok(self) -> bool:
        return "ok" in self.records.values()

    def disk_ok(self, size_bytes: int) -> bool:
        return self.disk_missing_bytes(size_bytes) == 0

    def disk_missing_bytes(self, size_bytes: int) -> int:
        return max(0, (size_bytes * 6 + 4) // 5 - self.available_bytes)

    def download(
        self, entry: CatalogEntry, *, progress: Callable[[Progress], None], cancel: threading.Event
    ) -> Path:
        self.visits.append(entry.id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            progress(Progress(entry.size_bytes // 2, entry.size_bytes, 0, None, 1, 1))
            if self.block:
                assert cancel.wait(1), "Не пришла отмена"
            else:
                assert self.releases[entry.id].wait(2), "Тест не разрешил завершить загрузку"
            if cancel.is_set():
                raise DownloadError("cancelled")
            if entry.id in self.errors:
                raise self.errors[entry.id]
            return Path("/fake/staging")
        finally:
            self.active -= 1

    def install_from_staging(self, entry: CatalogEntry) -> InstallResult:
        self.installs.append(entry.id)
        self.records[(entry.id, entry.revision)] = "ok"
        # Как настоящий установщик: каждая установка заменяет рабочую модель.
        self.current = (entry.id, entry.revision)
        return InstallResult("ok")


def select_queue(controller: OnboardingController, port: QueueModelPort) -> None:
    # Порядок кликов не должен влиять на порядок каталога.
    controller.toggleModel(port.second.id)
    controller.toggleModel(port.entry.id)


@pytest.mark.parametrize("model_id", ["../x", "", "unknown", "revoked"])
def test_model_toggle_rejects_unavailable_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_id: str
) -> None:
    first = FakeModelPort().entry
    revoked = replace(first, id="revoked", recommended=False)
    service = ModelService.__new__(ModelService)
    service._catalog = Catalog(
        1, 1, (RevokedEntry(revoked.id, revoked.revision, "revoked"),), (first, revoked)
    )
    service._store = ModelStore(tmp_path / "models")
    monkeypatch.setattr(service, "allowed", lambda: (True, ""))
    monkeypatch.setattr(service, "disk_ok", lambda size: True)
    monkeypatch.setattr(service, "ram_ok", lambda size: True)
    downloads = ModelDownloads(service)
    downloads.toggleModel(first.id)
    before = downloads.models
    changed = QSignalSpy(downloads.selectionChanged)
    create_job = Mock(side_effect=AssertionError("Недоступный id не создаёт задание"))
    monkeypatch.setattr("astra_voice.ui.model_downloads._ModelJob", create_job)
    downloads.toggleModel(model_id)
    downloads.retryModel(model_id)
    assert downloads.models == before
    assert len(changed) == 0
    assert downloads._selected == {first.id}
    assert downloads._model_thread is None and not downloads._queue_running
    assert downloads._queue == []
    create_job.assert_not_called()


@pytest.mark.parametrize("state", ["available", "queued", "downloading", "verifying", "installed"])
def test_model_retry_only_accepts_failed_cards(
    model_rig: ModelRig, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    port, create = model_rig
    controller = create()
    controller._downloads._set_card(port.entry, state)
    create_job = Mock(side_effect=AssertionError("Исправная карточка не создаёт задание"))
    monkeypatch.setattr("astra_voice.ui.model_downloads._ModelJob", create_job)
    controller.retryModel(port.entry.id)
    assert controller.models[0]["state"] == state
    assert controller._downloads._model_thread is None
    assert port.download_calls == 0
    create_job.assert_not_called()


@pytest.mark.parametrize("error", [OSError("SECRET /path/service"), StoreError("broken-store")])
def test_model_current_failure_keeps_installation_and_continues_queue(
    model_rig: ModelRig,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, create = model_rig
    port = QueueModelPort()
    previous = ("previous", "r1")
    port.records[previous] = "ok"
    port.current = previous
    set_current = Mock(side_effect=error)
    monkeypatch.setattr(port, "set_current", set_current)
    controller = create(port)
    select_queue(controller, port)
    ready = QSignalSpy(controller.canFinishChanged)
    finished = QSignalSpy(controller._downloads.queueFinished)
    port.releases[port.entry.id].set()
    controller.startSelectedDownloads()
    assert ready.wait(1000)
    message = "Модель установлена. Сделать её рабочей не удалось — попробуйте переустановить."
    assert controller.modelState == "installed"
    assert controller.modelMessage == message
    card = controller.models[0]
    assert card["state"] == "installed" and card["message"] == message
    assert card["badge"] in {"installed", "active"}
    assert controller.models[1]["state"] == "downloading"
    port.releases[port.second.id].set()
    assert finished.wait(1000)
    assert list(finished) == [[True]]
    assert port.installs == [port.entry.id, port.second.id]
    assert set_current.call_args_list == [call(*previous), call(*previous)]
    assert controller.downloadState == "done" and controller.downloadProgress == 1.0
    assert all(
        card["state"] == "installed" and card["message"] == message for card in controller.models
    )
    assert controller.models[0]["badge"] == "installed"
    assert "SECRET" not in caplog.text and "/path/service" not in caplog.text


def test_installed_revoked_model_is_shown_as_recalled(model_rig: ModelRig) -> None:
    """Отозванную установленную модель карточка объясняет, а рабочей она не станет."""
    port, create = model_rig
    port.revoked.add((port.entry.id, port.entry.revision))
    port.ready = True
    controller = create()

    card = controller.models[0]
    assert card["state"] == "failed"
    assert card["message"] == model_downloads._REVOKED_MESSAGE
    downloads = controller._downloads
    assert downloads.active_entry() is None
    assert downloads.active_state() == "none"
    assert downloads.can_install() is True


def test_measurements_cache_refreshes_on_signal(
    model_rig: ModelRig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    port, create = model_rig
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    controller = create()
    downloads = controller._downloads
    assert not downloads.models[0]["ramMeasured"]
    path = tmp_path / "measurements.json"
    path.write_text(
        json.dumps(
            {
                f"{port.entry.id}@{port.entry.revision}": {
                    "ram_mb": 600,
                    "threads": 2,
                    "build": __version__,
                }
            }
        )
    )
    assert not downloads.models[0]["ramMeasured"]
    downloads.measurements_changed()
    assert downloads.models[0]["ramMb"] == 600
    assert downloads.models[0]["ramMeasured"]


def test_model_cards_exact_keys_and_selection(model_rig: ModelRig) -> None:
    port, create = model_rig
    controller = create()
    assert controller.models == [
        {
            "id": port.entry.id,
            "name": "Тестовая модель",
            "description": "",
            "host": "models.example",
            "recommended": True,
            "sizeBytes": 226_000_000,
            "sizeText": "226 МБ",
            "ramText": "768 МБ",
            "ramMb": 768,
            "ramMeasured": False,
            "speedKind": "no_data",
            "speedText": "",
            "speedValue": 0.0,
            "qualityValue": None,
            "selected": False,
            "badge": "",
            "state": "available",
            "message": "",
            "hint": "",
            "hintKind": "",
            "canSwitchWithPause": False,
            "progress": 0.0,
            "vendor": "",
            "domestic": False,
            "updateAvailable": False,
            "tags": [],
            "metrics": [
                {
                    "kind": "quality",
                    "label": "Качество",
                    "text": "",
                    "fill": 0.0,
                    "hasData": False,
                    "measured": False,
                },
                {
                    "kind": "speed",
                    "label": "Скорость",
                    "text": "",
                    "fill": 0.0,
                    "hasData": False,
                    "measured": False,
                },
            ],
        }
    ]
    assert controller.selectionSummary == controller.selectionMessage == ""
    assert controller.selectionFits
    assert not controller.canContinueFromModel
    changed = QSignalSpy(controller.selectionChanged)
    cards = QSignalSpy(controller.modelsChanged)
    controller.toggleModel("unknown")
    assert len(changed) == len(cards) == 0
    controller.toggleModel(port.entry.id)
    assert controller.models[0]["selected"]
    assert controller.selectionSummary == "Будет скачано 226 МБ"
    assert controller.canContinueFromModel
    controller.toggleModel(port.entry.id)
    assert not controller.models[0]["selected"]
    assert controller.selectionSummary == ""
    assert len(changed) == len(cards) == 2
    # Возвращается копия, QML не может изменить состояние контроллера.
    controller.models[0]["selected"] = True
    assert not controller.models[0]["selected"]


@pytest.mark.parametrize("state, badge", [("ok", "active"), ("broken", "installed")])
def test_model_cards_installed_cannot_be_selected(
    model_rig: ModelRig, state: str, badge: str
) -> None:
    port, create = model_rig
    port.ready, port.damaged = state == "ok", state == "broken"
    controller = create()
    controller.toggleModel(port.entry.id)
    assert controller.models[0]["badge"] == badge
    assert not controller.models[0]["selected"]
    assert (
        controller.canContinueFromModel
        == controller.modelReady
        == controller.canFinish
        == port.ready
    )
    controller.startSelectedDownloads()
    assert port.download_calls == 0


def test_model_selection_total_space_and_inactive_badge(model_rig: ModelRig) -> None:
    _, create = model_rig
    port = QueueModelPort()
    controller = create(port)
    select_queue(controller, port)
    assert controller.selectionSummary == "Будет скачано 400 МБ"
    port.available_bytes = 466_999_999
    assert not controller.selectionFits
    assert controller.selectionMessage == "На диске не хватает 14 МБ. Освободите место."
    assert not controller.canContinueFromModel
    port.available_bytes = 480_000_000
    assert controller.selectionFits
    assert controller.selectionMessage == ""
    port.records[(port.entry.id, port.entry.revision)] = "ok"
    assert controller.models[0]["badge"] == "installed"
    assert controller.selectionSummary == "Будет скачано 300 МБ"
    controller.toggleModel(port.second.id)
    assert controller.selectionSummary == ""
    assert controller.canContinueFromModel
    assert controller.modelReady and controller.canFinish


def test_model_queue_serial_bytes_titles_and_first_current(model_rig: ModelRig) -> None:
    _, create = model_rig
    port = QueueModelPort()
    controller = create(port)
    select_queue(controller, port)
    titles: list[str] = []
    controller.downloadTitleChanged.connect(lambda: titles.append(controller.downloadTitle))
    progress = QSignalSpy(controller.downloadProgressChanged)
    finished = QSignalSpy(controller.canFinishChanged)
    controller.startSelectedDownloads()
    first_thread = controller._downloads._model_thread
    assert first_thread is not None
    assert controller.models[1]["state"] == "queued"
    controller.startSelectedDownloads()
    controller.toggleModel(port.entry.id)
    assert all(row["selected"] for row in controller.models)
    assert progress.wait(1000)
    assert port.visits == [port.entry.id]
    assert controller.models[0]["progress"] == 0.5
    assert controller.downloadProgress == 0.125
    assert controller.downloadTitle == "Загружается Тестовая модель · 1 из 2"
    port.releases[port.entry.id].set()
    assert finished.wait(1000)
    assert not first_thread.isRunning()
    assert (
        controller._downloads._model_thread is not None
        and controller._downloads._model_thread is not first_thread
    )
    if controller.models[1]["progress"] == 0:
        assert progress.wait(1000)
    assert controller.downloadProgress == 0.625
    assert controller.downloadTitle == "Загружается Вторая модель · 2 из 2"
    # Старые свойства по-прежнему описывают первую модель.
    assert controller.modelState == "installed"
    assert controller.modelName == port.entry.name
    assert controller.progress == 1.0
    port.releases[port.second.id].set()
    assert finished.wait(1000)
    assert port.visits == port.installs == [port.entry.id, port.second.id]
    assert port.max_active == 1
    assert controller.downloadState == "done"
    assert controller.downloadTitle == "Модель готова"
    assert controller.downloadProgress == 1.0
    assert "Проверяю модель…" in titles
    assert port.current == (port.entry.id, port.entry.revision)
    assert port.restore_calls == [port.current]
    assert [row["badge"] for row in controller.models] == ["active", "installed"]
    assert all(row["state"] == "installed" and row["progress"] == 0 for row in controller.models)
    assert controller.selectionSummary == ""
    assert controller.modelReady and controller.canFinish and controller.canContinueFromModel
    controller.startSelectedDownloads()
    assert controller._downloads._model_thread is None


def test_model_queue_failure_continues_and_retries(model_rig: ModelRig) -> None:
    _, create = model_rig
    port = QueueModelPort()
    port.errors[port.entry.id] = DownloadError("bad-path")
    controller = create(port)
    select_queue(controller, port)
    finished = QSignalSpy(controller.canFinishChanged)
    port.releases[port.entry.id].set()
    controller.startSelectedDownloads()
    assert finished.wait(1000)
    assert controller.models[0]["state"] == "failed"
    assert (
        controller.models[0]["message"]
        == "Модель не прошла проверку. Попробуйте скачать или установить её заново."
    )
    controller.retryModel(port.entry.id)
    assert controller._downloads._active_entry == port.second
    port.releases[port.second.id].set()
    assert finished.wait(1000)
    assert controller.downloadState == "done"
    assert controller.downloadProgress == 0.75
    assert controller.models[1]["state"] == "installed"
    port.errors.clear()
    controller.retryModel(port.entry.id)
    assert controller.downloadTitle == "Загружается Тестовая модель"
    assert finished.wait(1000)
    assert port.visits == [port.entry.id, port.second.id, port.entry.id]
    assert port.current == (port.second.id, port.second.revision)
    assert controller.models[0]["message"] == ""
    assert controller.models[0]["state"] == "installed"
    controller.retryModel(port.entry.id)
    assert controller._downloads._model_thread is None


@pytest.mark.parametrize("no_space", [False, True])
def test_model_queue_all_failed_titles(model_rig: ModelRig, no_space: bool) -> None:
    port, create = model_rig
    port.error = OSError(28 if no_space else 5, "private/file")
    controller = create()
    controller.toggleModel(port.entry.id)
    done = QSignalSpy(controller.canFinishChanged)
    controller.startSelectedDownloads()
    assert done.wait(1000)
    assert controller.downloadState == ("no-space" if no_space else "failed")
    assert controller.downloadTitle == (
        "Не хватает места на диске" if no_space else "Не получилось загрузить модель"
    )
    row = controller.models[0]
    assert row["state"] == controller.downloadState
    assert row["message"] and "private" not in row["message"]
    assert not controller.modelReady and not controller.canFinish


def test_model_queue_cancel_clears_pending_and_ignores_late_progress(model_rig: ModelRig) -> None:
    _, create = model_rig
    port = QueueModelPort()
    port.block = True
    controller = create(port)
    select_queue(controller, port)
    progress = QSignalSpy(controller.downloadProgressChanged)
    finished = QSignalSpy(controller.canFinishChanged)
    controller.startSelectedDownloads()
    assert progress.wait(1000)
    controller.cancelDownloads()
    controller.cancelDownloads()
    controller._model_progressed(1, 50, 10)
    controller._model_staged("verifying")
    assert controller.downloadState == "idle"
    assert controller.downloadTitle == ""
    assert controller.downloadProgress == 0
    assert all(row["state"] == "available" and row["selected"] for row in controller.models)
    assert finished.wait(1000)
    assert port.visits == [port.entry.id]
    assert not port.installs
    assert controller._downloads._model_thread is None
    assert controller.downloadState == "idle"
    assert controller.models[0]["message"] == ""
    assert controller.selectionSummary == "Будет скачано 400 МБ"
    controller.toggleModel(port.second.id)
    assert not controller.models[1]["selected"]


def test_model_local_install_counts_as_selection(model_rig: ModelRig) -> None:
    port, create = model_rig
    controller = create()
    ready = QSignalSpy(controller.modelReadyChanged)
    controller.installFromPath("/fake/model")
    assert ready.wait(1000)
    assert port.download_calls == 0
    assert controller.models[0]["selected"]
    assert controller.models[0]["state"] == "installed"
    assert controller.canContinueFromModel and controller.modelReady and controller.canFinish
    assert controller.downloadTitle == "Модель готова"
    assert not controller.done


def test_model_queue_empty_catalog_and_fallback(model_rig: ModelRig) -> None:
    _, create = model_rig
    controller = create(None)
    for _ in range(2):
        controller.toggleModel("missing")
        controller.startSelectedDownloads()
        controller.retryModel("missing")
        controller.cancelDownloads()
        controller.installFromPath("/fake/model")
    assert controller.models == []
    assert controller.selectionFits
    assert not controller.canContinueFromModel
    assert controller.downloadState == "idle" and controller.downloadTitle == ""
    assert controller.downloadProgress == 0
    ready = QSignalSpy(controller.modelReadyChanged)
    controller._bridge.set_extra("onboarding_model_ready", True)
    assert len(ready) == 1
    assert controller.modelReady and controller.canFinish and controller.canContinueFromModel


def test_model_queue_estimates_use_clock_and_throttle(
    model_rig: ModelRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, create = model_rig
    port = QueueModelPort()
    clock = Mock(return_value=100.0)
    controller = create(port, clock=clock)
    # Здесь проверяются только вычисления: потоки покрыты тестами очереди выше.
    monkeypatch.setattr(controller._downloads, "_start_model_job", lambda source=None: None)
    select_queue(controller, port)
    controller.startSelectedDownloads()
    controller._downloads._active_entry = port.entry
    transitions: list[tuple[str, str, str]] = []
    controller.downloadStateChanged.connect(
        lambda: transitions.append((controller.downloadState, controller.speed, controller.eta))
    )
    controller._model_staged("downloading")
    assert transitions == [("downloading", "", "считаю…")]
    assert controller.speed == "" and controller.eta == "считаю…"
    controller._model_progressed(0, 999_000_000, 0)
    assert controller.speed == "" and controller.eta == "считаю…"
    speed = QSignalSpy(controller.speedChanged)
    eta = QSignalSpy(controller.etaChanged)
    clock.return_value = 100.5
    controller._model_progressed(0.005, 999_000_000, 0)
    assert controller.speed == "" and controller.eta == "считаю…"
    assert len(speed) == len(eta) == 0
    clock.return_value = 105.0
    controller._model_progressed(0.05, 0, -1)
    assert controller.speed == "1,0 МБ/с"
    assert controller.eta == "осталось ~7 мин"
    speed = QSignalSpy(controller.speedChanged)
    eta = QSignalSpy(controller.etaChanged)
    progress = QSignalSpy(controller.downloadProgressChanged)
    clock.return_value = 105.5
    controller._model_progressed(0.055, float("nan"), float("inf"))
    assert controller.downloadProgress == 5_500_000 / 400_000_000
    assert len(progress) == 1 and len(speed) == len(eta) == 0
    assert controller._downloads._tracker.elapsed_s == 5.5
    clock.return_value = 106.0
    controller._model_progressed(0.12, 0, -1)
    assert controller.speed == "2,0 МБ/с"
    assert controller.eta == "осталось ~3 мин"
    assert len(speed) == len(eta) == 1
    # Следующая модель продолжает тот же счётчик байтов и времени.
    controller._downloads._queue_completed_bytes = port.entry.size_bytes
    controller._downloads._active_entry = port.second
    clock.return_value = 110.0
    controller._model_progressed(0.1, 0, -1)
    assert controller.downloadProgress == 0.325
    assert controller._downloads._tracker.elapsed_s == 10.0
    assert controller.speed == "25,0 МБ/с"
    assert controller.eta == "осталось меньше минуты"


def test_model_service_catalog_order_and_store_proxies() -> None:
    first = FakeModelPort().entry
    other = replace(first, id="other", recommended=False)
    revoked = replace(other, id="revoked")
    last = replace(other, id="last")
    service = ModelService.__new__(ModelService)
    service._catalog = Catalog(
        1, 1, (RevokedEntry(revoked.id, revoked.revision, ""),), (other, revoked, first, last)
    )
    store = Mock()
    store.records.return_value = ()
    service._store = store
    assert service.entries() == (first, other, last)
    store.records.return_value = [Mock(id=first.id, revision=first.revision, state="broken")]
    assert service.record_state(first.id, first.revision) == "broken"
    assert service.record_state(first.id, "missing") == ""
    store.current.return_value = None
    assert service.current_ids() is None
    store.current.return_value = Mock(id=first.id, revision=first.revision)
    assert service.current_ids() == (first.id, first.revision)
    service.set_current(first.id, first.revision)
    store.set_current.assert_called_once_with(first.id, first.revision)


def test_model_service_installed_ids_preserve_all_records_and_order() -> None:
    service = ModelService.__new__(ModelService)
    store = Mock()
    service._store = store
    store.records.return_value = (
        Mock(id="second", revision="r2", state="broken"),
        Mock(id="first", revision="r1", state="ok"),
        Mock(id="second", revision="r1", state="pending"),
    )
    assert service.installed_ids() == (("second", "r2"), ("first", "r1"), ("second", "r1"))
    store.records.assert_called_once_with()
    store.records.return_value = ()
    assert service.installed_ids() == ()


def test_model_new_properties_and_slots_have_qt_contract(model_rig: ModelRig) -> None:
    _, create = model_rig
    controller = create()
    meta = controller.metaObject()
    for name in (
        "models",
        "selectionSummary",
        "selectionFits",
        "selectionMessage",
        "canContinueFromModel",
        "downloadState",
        "downloadProgress",
        "downloadTitle",
        "modelReady",
    ):
        prop = meta.property(meta.indexOfProperty(name))
        assert prop.isValid() and not prop.isWritable() and prop.hasNotifySignal()
    assert meta.property(meta.indexOfProperty("models")).typeName() == "QVariantList"
    for signature in (
        b"toggleModel(QString)",
        b"startSelectedDownloads()",
        b"retryModel(QString)",
        b"cancelDownloads()",
    ):
        assert meta.indexOfSlot(signature) >= 0


@pytest.mark.parametrize("previous_state", ["ok", "broken"])
def test_model_queue_preserves_previous_usable_current(
    model_rig: ModelRig, previous_state: str
) -> None:
    _, create = model_rig
    port = QueueModelPort()
    previous = ("previous-model", "previous-revision")
    port.records[previous] = previous_state
    port.current = previous
    controller = create(port)
    select_queue(controller, port)
    finished = QSignalSpy(controller.canFinishChanged)
    port.releases[port.entry.id].set()
    controller.startSelectedDownloads()
    assert finished.wait(1000)
    expected = previous if previous_state == "ok" else (port.entry.id, port.entry.revision)
    assert port.current == expected
    port.releases[port.second.id].set()
    assert finished.wait(1000)
    assert port.current == expected
    assert controller.downloadState == "done"


@pytest.mark.parametrize("last_code", ["disk-full", "bad-path"])
def test_model_queue_terminal_failure_uses_last_reason(model_rig: ModelRig, last_code: str) -> None:
    _, create = model_rig
    port = QueueModelPort()
    port.errors = {
        port.entry.id: DownloadError("disk-full"),
        port.second.id: DownloadError(last_code),
    }
    controller = create(port)
    select_queue(controller, port)
    finished = QSignalSpy(controller.canFinishChanged)
    port.releases[port.entry.id].set()
    controller.startSelectedDownloads()
    assert finished.wait(1000)
    assert controller.models[0]["state"] == "no-space"
    assert controller.models[0]["message"]
    assert controller.downloadState == "downloading"
    port.releases[port.second.id].set()
    assert finished.wait(1000)
    assert port.visits == [port.entry.id, port.second.id]
    assert controller.downloadState == ("no-space" if last_code == "disk-full" else "failed")
    assert controller.downloadProgress == 0
    assert not controller.modelReady


@pytest.mark.parametrize("dbfs, expected", [(-90.0, 0.0), (-30.0, 0.5), (6.0, 1.0)])
def test_level_monitor_without_model_is_separate(dbfs: float, expected: float) -> None:
    rig = MicrophoneBridgeRig(model_ready=False)
    controller = rig.controller
    spies = [
        QSignalSpy(getattr(controller, name + "Changed"))
        for name in ("testState", "testText", "testDuration", "testMessage")
    ]
    state_spy = QSignalSpy(controller.levelStateChanged)
    controller.startLevelMonitor()
    controller.startLevelMonitor()
    assert [msg["type"] for msg in rig.commands] == ["record.start"]
    assert rig.commands[0]["device"] == "selected-mic"
    assert rig.commands[0]["limit_s"] == 60.0
    assert controller.levelState == "listening"
    assert not rig.core.test_active
    rig.recording.assert_called_with(True)
    rig.tray.set_state.assert_called_with(TrayState.LISTENING)
    rig.event("level", peak_dbfs=dbfs, rms_dbfs=-30.0)
    assert controller.level == expected
    assert controller.peak == f"{dbfs:.0f} дБ".replace("-", "−")
    rig.event("silent")
    rig.event("result", text="Личная фраза", t_ms=1)
    assert controller.testState == "idle"
    assert controller.testText == controller.testDuration == controller.testMessage == ""
    assert all(len(spy) == 0 for spy in spies)
    controller.stopLevelMonitor()
    assert [msg["type"] for msg in rig.commands] == ["record.start", "record.cancel"]
    rig.recording.assert_called_with(False)
    rig.tray.set_state.assert_called_with(TrayState.IDLE)
    rig.event("cancelled")
    assert controller.levelState == "idle" and controller.level == 0.0
    assert len(state_spy) == 2
    assert rig.pill.show_state.call_args_list == [
        call(PillState.LISTENING),
        call(PillState.LISTENING, level=expected),
    ]
    rig.pill.hide.assert_called_once_with()
    rig.paste.assert_not_called()
    rig.stats.append.assert_not_called()


@pytest.mark.parametrize("code", ["audio-no-device", "audio-busy", "audio-failed", "unknown"])
def test_level_monitor_plain_error(code: str, caplog: pytest.LogCaptureFixture) -> None:
    rig = MicrophoneBridgeRig(model_ready=False)
    rig.controller.startLevelMonitor()
    rig.event("error", code=code, message="ALSA /private/path hw:7 Личная речь")
    assert rig.controller.levelState == "error"
    assert rig.controller.levelMessage
    assert not any(part in rig.controller.levelMessage for part in ("ALSA", "/", "hw:", code))
    assert rig.commands[-1]["type"] == "record.cancel"
    rig.event("cancelled")
    assert rig.controller.levelState == "error"
    assert rig.controller.testState == "idle"
    rig.recording.assert_called_with(False)
    assert "Личная речь" not in caplog.text


class LevelWindow(QObject):
    """Состояние окна без QWidget, QWindow, дисплея и нативных событий."""

    def __init__(self, visible: bool = True) -> None:
        super().__init__()
        self.visible = visible
        self.state = int(Qt.WindowNoState)

    def isVisible(self) -> bool:  # noqa: N802
        return self.visible

    def windowState(self) -> int:  # noqa: N802
        return self.state


@pytest.mark.parametrize("initially_visible", [False, True])
@pytest.mark.parametrize("visible, minimized", [(False, False), (True, True), (True, False)])
def test_level_start_reads_current_attached_window(
    initially_visible: bool, visible: bool, minimized: bool
) -> None:
    rig = MicrophoneBridgeRig()
    window = LevelWindow(initially_visible)
    rig.controller.attach_window(window)
    assert rig.controller._window_visible is initially_visible
    # Событие ещё не доставлено: сохранённая отметка может быть устаревшей.
    window.visible = visible
    window.state = int(Qt.WindowMinimized if minimized else Qt.WindowNoState)
    rig.controller.startLevelMonitor()
    if not visible or minimized:
        rig.host.start_level_monitor.assert_not_called()
        assert rig.commands == []
        assert rig.controller.levelState == "idle"
    else:
        rig.host.start_level_monitor.assert_called_once()
        assert rig.controller.levelState == "listening"
    rig.controller.shutdown()


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("event", [QEvent.WindowStateChange, QEvent.Hide, QEvent.Close])
def test_window_events_stop_level_monitor_once(attached: bool, event: QEvent.Type) -> None:
    rig = MicrophoneBridgeRig()
    window = LevelWindow()
    if attached:
        rig.controller.attach_window(window)
    else:
        window.installEventFilter(rig.controller)
    rig.controller.startLevelMonitor()
    if event == QEvent.WindowStateChange:
        # Максимизация измерению не мешает.
        window.state = int(Qt.WindowMaximized)
        QCoreApplication.sendEvent(window, QEvent(event))
        rig.host.stop_level_monitor.assert_not_called()
        window.state = int(Qt.WindowMinimized)
    else:
        window.visible = False
    for following in (event, event, QEvent.Hide, QEvent.Close):
        QCoreApplication.sendEvent(window, QEvent(following))
    rig.host.stop_level_monitor.assert_called_once_with()
    assert [message["type"] for message in rig.commands] == ["record.start", "record.cancel"]
    rig.event("cancelled")
    assert not rig.core.level_active
    assert rig.controller.levelState == "idle"
    rig.recording.assert_called_with(False)
    rig.controller.shutdown()
    rig.host.stop_level_monitor.assert_called_once_with()


def test_attached_object_without_window_methods_is_supported() -> None:
    rig = MicrophoneBridgeRig()
    window = QObject()
    rig.controller.attach_window(window)
    QCoreApplication.sendEvent(window, QEvent(QEvent.WindowStateChange))
    rig.controller.startLevelMonitor()
    rig.host.start_level_monitor.assert_called_once()
    rig.controller.shutdown()


@pytest.mark.parametrize("visible", [False, True])
def test_attached_window_initial_visibility_controls_model_notification(
    monkeypatch: pytest.MonkeyPatch, visible: bool
) -> None:
    from astra_voice.ui import notify

    rig = MicrophoneBridgeRig()
    window = LevelWindow(visible)
    notification = Mock()
    monkeypatch.setattr(notify, "notify_model_installed", notification)
    rig.controller.attach_window(window)
    rig.controller._downloads.queueFinished.emit(True)
    assert notification.call_count == int(not visible)
    rig.controller.shutdown()


def test_level_limit_message_is_idle_and_clears_on_explicit_start() -> None:
    rig = MicrophoneBridgeRig()
    callbacks: dict[int, Callable[[], None]] = {}

    def schedule(delay: int, callback: Callable[[], None]) -> object:
        callbacks[delay] = callback
        return object()

    rig.core._schedule = schedule
    rig.controller.startLevelMonitor()
    callbacks[int(LEVEL_TOTAL_LIMIT_S * 1000)]()
    rig.event("cancelled")
    assert rig.controller.levelState == "idle"
    assert rig.controller.levelMessage == (
        "Проверка микрофона остановлена. Нажмите, чтобы продолжить"
    )
    assert rig.controller.level == 0
    assert rig.host.start_level_monitor.call_count == 1
    rig.controller.startLevelMonitor()
    assert rig.controller.levelState == "listening"
    assert rig.controller.levelMessage == ""
    assert rig.host.start_level_monitor.call_count == 2
    rig.controller.shutdown()


@pytest.mark.parametrize("leave", ["next", "back", "hide", "close", "shutdown", "device"])
def test_level_monitor_leaving_closes_microphone(leave: str) -> None:
    rig = MicrophoneBridgeRig(model_ready=False)
    controller = rig.controller
    controller.startLevelMonitor()
    if leave in ("hide", "close"):
        controller.eventFilter(controller, QEvent(QEvent.Hide if leave == "hide" else QEvent.Close))
    elif leave == "device":
        controller.setProperty("device", "other")
    else:
        getattr(controller, leave)()
    assert rig.commands[-1]["type"] == "record.cancel"
    rig.recording.assert_called_with(False)
    rig.event("cancelled")
    assert controller.levelState == "idle"
    assert not rig.core.level_active


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_level_monitor_host_exception_closes_microphone(
    operation: str, caplog: pytest.LogCaptureFixture
) -> None:
    rig = MicrophoneBridgeRig(model_ready=False)

    def fail_start(device: str, callback: Any) -> None:
        rig.core.start_level_monitor(device, callback)
        raise RuntimeError("Личная речь ALSA /path")

    def fail_stop() -> None:
        rig.core.stop_level_monitor()
        raise RuntimeError("Личная речь ALSA /path")

    if operation == "start":
        rig.host.start_level_monitor.side_effect = fail_start
    rig.controller.startLevelMonitor()
    if operation == "stop":
        rig.host.stop_level_monitor.side_effect = fail_stop
        rig.controller.stopLevelMonitor()
    assert rig.commands[-1]["type"] == "record.cancel"
    assert rig.controller.levelState == "error"
    assert rig.controller.levelMessage == "Не удалось проверить микрофон. Попробуйте ещё раз."
    assert rig.controller.testState == "idle"
    rig.recording.assert_called_with(False)
    assert "Личная речь" not in caplog.text


def test_start_test_requires_model_without_touching_host() -> None:
    rig = MicrophoneBridgeRig(model_ready=False)
    rig.host.reset_mock()
    rig.controller.startTest()
    assert rig.controller.testState == "idle"
    assert rig.controller.testMessage == "Будет доступно после установки модели"
    assert rig.commands == [] and rig.host.mock_calls == []


@pytest.mark.parametrize("synchronous", [False, True])
def test_start_test_waits_for_level_idle_once(synchronous: bool) -> None:
    rig = MicrophoneBridgeRig()
    rig.controller.startLevelMonitor()
    original_send = rig.core._send

    def send(message: dict[str, Any], **kwargs: Any) -> None:
        original_send(message, **kwargs)
        if message["type"] == "record.cancel":
            rig.event("cancelled")

    if synchronous:
        rig.core._send = send
    for _ in range(3):
        rig.controller.startTest()
    if not synchronous:
        rig.host.start_test.assert_not_called()
        assert rig.controller.testState == "idle"
        rig.event("cancelled")
    rig.host.start_test.assert_called_once()
    assert [msg["type"] for msg in rig.commands] == [
        "record.start",
        "record.cancel",
        "record.start",
    ]
    assert rig.controller.testState == "recording"
    rig.recording.assert_called_with(True)
    rig.tray.set_state.assert_called_with(TrayState.LISTENING)


@pytest.mark.parametrize("leave", ["back", "stopLevelMonitor", "shutdown"])
def test_pending_test_after_level_is_cancelled_on_leaving(leave: str) -> None:
    rig = MicrophoneBridgeRig()
    rig.controller.startLevelMonitor()
    rig.controller.startTest()
    getattr(rig.controller, leave)()
    rig.event("cancelled")
    rig.host.start_test.assert_not_called()


def test_unavailable_test_does_not_stop_level_monitor() -> None:
    rig = MicrophoneBridgeRig(model_ready=False)
    rig.controller.startLevelMonitor()
    rig.event("level", peak_dbfs=-18, rms_dbfs=-30)
    rig.host.reset_mock()
    rig.controller.startTest()
    assert rig.controller.testState == "idle"
    assert rig.controller.testMessage == "Будет доступно после установки модели"
    assert rig.controller.levelState == "listening"
    assert rig.controller.level == pytest.approx(0.7)
    assert rig.host.mock_calls == []
    assert [message["type"] for message in rig.commands] == ["record.start"]


class SharedDownloadsRig:
    def __init__(self) -> None:
        from astra_voice.ui.model_downloads import ModelDownloads

        self.port = QueueModelPort()
        self.settings = Settings(extra={"onboarding_language_set": True})
        self.downloads = ModelDownloads(self.port)
        self.bridge = SettingsBridge(self.settings, downloads=self.downloads, save=Mock())
        self.status = Mock()
        self.controller = OnboardingController(
            self.bridge,
            settings=self.settings,
            downloads=self.downloads,
            status_sink=self.status,
            device_provider=lambda: [],
        )


@pytest.fixture
def shared_downloads(monkeypatch: pytest.MonkeyPatch) -> Iterator[SharedDownloadsRig]:
    from astra_voice.ui import notify

    monkeypatch.setattr(notify, "notify_model_installed", Mock())
    rig = SharedDownloadsRig()
    yield rig
    for release in rig.port.releases.values():
        release.set()
    if not sip.isdeleted(rig.controller):
        rig.controller.shutdown()
    rig.downloads.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.mark.parametrize("state", ["none", "ok", "broken"])
def test_settings_active_model(shared_downloads: SharedDownloadsRig, state: str) -> None:
    rig = shared_downloads
    entry = rig.port.second  # Рабочая модель может отличаться от рекомендованной.
    if state != "none":
        rig.port.current = (entry.id, entry.revision)
        rig.port.records[rig.port.current] = state
    assert rig.bridge.activeModelName == ("" if state == "none" else entry.name)
    assert rig.bridge.activeModelSize == ("" if state == "none" else "300 МБ")
    assert rig.bridge.activeModelState == state
    assert rig.bridge.canReinstall is (state != "none")
    if state == "none":
        rig.bridge.reinstallActiveModel()
        assert rig.downloads.downloadState == "idle"
        assert rig.port.visits == []


@pytest.mark.parametrize(
    ("first_state", "second_state", "current_index", "expected_index", "expected_state"),
    [
        ("ok", "ok", 1, 1, "ok"),
        ("ok", "ok", None, 0, "ok"),
        ("broken", "ok", None, 1, "ok"),
        ("broken", "broken", None, 0, "broken"),
        ("", "pending", None, 1, "broken"),
        ("", "", 1, 1, "none"),
        ("", "", None, None, "none"),
    ],
)
def test_settings_active_model_fallback_order(
    shared_downloads: SharedDownloadsRig,
    first_state: str,
    second_state: str,
    current_index: int | None,
    expected_index: int | None,
    expected_state: str,
) -> None:
    rig = shared_downloads
    entries = rig.port.catalog
    # Порядок записей хранилища противоположен порядку каталога.
    for entry, state in reversed(tuple(zip(entries, (first_state, second_state), strict=True))):
        if state:
            rig.port.records[(entry.id, entry.revision)] = state
    if current_index is not None:
        entry = entries[current_index]
        rig.port.current = (entry.id, entry.revision)
    expected = entries[expected_index] if expected_index is not None else None
    assert rig.downloads.active_entry() == expected
    assert rig.bridge.activeModelState == expected_state
    assert rig.bridge.canReinstall is (expected_state != "none")


def test_settings_active_model_without_catalog(shared_downloads: SharedDownloadsRig) -> None:
    rig = shared_downloads
    entry = rig.port.entry
    rig.port.current = (entry.id, entry.revision)
    rig.port.records[rig.port.current] = "broken"
    rig.downloads._entries = ()
    assert rig.bridge.activeModelName == rig.bridge.activeModelSize == ""
    assert rig.bridge.activeModelState == "none"
    rig.bridge.reinstallActiveModel()
    assert rig.port.visits == []
    assert rig.bridge.downloadState == "idle"


@pytest.mark.parametrize("state", ["ok", "broken", "recheck"])
@pytest.mark.parametrize("has_current", [False, True])
def test_settings_uses_store_when_catalog_unavailable(
    tmp_path: Path, state: str, has_current: bool
) -> None:
    store = ModelStore(tmp_path / "SECRET" / "models")
    staged = store.staging_dir("saved-model", "r1")
    (staged / "state.json").write_text(
        json.dumps({"layout": "layout", "variant": "variant", "size_bytes": 42_000_000})
    )
    store.commit("saved-model", "r1")
    if has_current:
        store.set_current("saved-model", "r1")
    if state == "broken":
        store.mark_broken("saved-model", "r1", "SECRET /path/service")
    elif state == "recheck":
        state_path = store.root / "saved-model" / "r1.json"
        data = json.loads(state_path.read_text())
        data["recheck"] = True
        state_path.write_text(json.dumps(data))
    before = {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    downloads = ModelDownloads(None, store=store)
    bridge = SettingsBridge(Settings(), downloads=downloads, save=Mock())
    assert bridge.activeModelState == "catalog-unavailable"
    assert bridge.activeModelName == "saved-model"
    assert bridge.activeModelSize == "42 МБ"
    assert not bridge.canReinstall
    assert not downloads.modelReady
    assert downloads.models == []
    bridge.reinstallActiveModel()
    bridge.cancelDownloads()
    assert downloads._model_thread is None and not downloads._queue_running
    assert downloads.downloadState == "idle"
    assert {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()} == before


def test_settings_catalog_unavailable_prefers_current_then_usable_record(tmp_path: Path) -> None:
    store = ModelStore(tmp_path / "models")
    for model_id in ("a-broken", "b-ok", "c-current"):
        staged = store.staging_dir(model_id, "r1")
        (staged / "model.bin").write_bytes(b"model")
        store.commit(model_id, "r1")
    store.mark_broken("a-broken", "r1", "broken")
    bridge = SettingsBridge(Settings(), downloads=ModelDownloads(None, store=store), save=Mock())
    assert bridge.activeModelName == "b-ok"
    store.set_current("c-current", "r1")
    assert bridge.activeModelName == "c-current"
    assert bridge.activeModelState == "catalog-unavailable"


def test_settings_catalog_unavailable_empty_store(tmp_path: Path) -> None:
    downloads = ModelDownloads(None, store=ModelStore(tmp_path / "models"))
    bridge = SettingsBridge(Settings(), downloads=downloads, save=Mock())
    assert bridge.activeModelState == "none"
    assert bridge.activeModelName == bridge.activeModelSize == ""
    assert not bridge.canReinstall
    bridge.reinstallActiveModel()
    assert downloads._model_thread is None


def test_settings_catalog_ignores_fallback_store() -> None:
    port = FakeModelPort()
    port.ready = True
    store = Mock(spec=ModelStore)
    store.current.side_effect = AssertionError("При доступном каталоге хранилище не читается")
    store.records.side_effect = store.current.side_effect
    downloads = ModelDownloads(port, store=store)
    bridge = SettingsBridge(Settings(), downloads=downloads, save=Mock())
    assert bridge.activeModelState == "ok"
    assert bridge.activeModelName == port.entry.name
    assert bridge.activeModelSize == "226 МБ"
    assert bridge.canReinstall
    store.current.assert_not_called()
    store.records.assert_not_called()


@pytest.mark.parametrize("initial", ["ok", "broken"])
def test_settings_cancel_downloads_cancels_reinstall(
    shared_downloads: SharedDownloadsRig, initial: str
) -> None:
    rig = shared_downloads
    entry = rig.port.second
    ids = (entry.id, entry.revision)
    rig.port.current = ids
    rig.port.records[ids] = initial
    rig.port.block = True
    progress = QSignalSpy(rig.bridge.downloadProgressChanged)
    finished = QSignalSpy(rig.downloads.queueFinished)
    rig.bridge.reinstallActiveModel()
    assert progress.wait(1000)
    assert not rig.bridge.canReinstall
    rig.bridge.cancelDownloads()
    assert rig.downloads._model_cancel.is_set()
    assert not rig.bridge.canReinstall  # Поток ещё завершает отменённое задание.
    assert finished.wait(1000)
    assert list(finished) == [[False]]
    assert rig.port.visits == [entry.id]
    assert rig.port.installs == []
    assert rig.port.records == {ids: initial}
    assert rig.port.current == ids
    assert rig.downloads._model_thread is None and not rig.downloads._queue_running
    assert rig.bridge.downloadState == rig.controller.downloadState == "idle"
    assert rig.bridge.canReinstall
    rig.status.assert_called_with("")


@pytest.mark.parametrize("error", [None, "bad-path", "disk-full"])
def test_reinstall_broken_model_without_current(
    shared_downloads: SharedDownloadsRig, monkeypatch: pytest.MonkeyPatch, error: str | None
) -> None:
    rig = shared_downloads
    entry = rig.port.second
    ids = (entry.id, entry.revision)
    rig.port.records[ids] = "broken"
    assert rig.port.current_ids() is None
    assert rig.bridge.activeModelName == entry.name
    assert rig.bridge.activeModelSize == "300 МБ"
    assert rig.bridge.activeModelState == "broken"
    download = Mock(wraps=rig.port.download)
    monkeypatch.setattr(rig.port, "download", download)
    if error is not None:
        rig.port.errors[entry.id] = DownloadError(error)
    progress = QSignalSpy(rig.bridge.downloadProgressChanged)
    finished = QSignalSpy(rig.downloads.queueFinished)
    states: list[str] = []
    rig.bridge.activeModelStateChanged.connect(lambda: states.append(rig.bridge.activeModelState))

    rig.bridge.reinstallActiveModel()

    assert progress.wait(1000)
    download.assert_called_once()
    assert download.call_args.args == (entry,)
    assert rig.port.visits == [entry.id]
    assert rig.port.records == {ids: "broken"}
    assert rig.bridge.activeModelState == rig.bridge.downloadState == "downloading"
    assert rig.bridge.downloadTitle == f"Загружается {entry.name}"
    rig.port.releases[entry.id].set()
    assert finished.wait(1000)
    if error is None:
        assert rig.port.installs == [entry.id]
        assert rig.port.records == {ids: "ok"}
        assert "verifying" in states
        assert rig.bridge.activeModelState == "ok"
        assert rig.bridge.downloadState == "done"
    else:
        assert rig.port.records == {ids: "broken"}
        expected = "no-space" if error == "disk-full" else "failed"
        assert rig.bridge.activeModelState == rig.bridge.downloadState == expected


def test_settings_model_without_downloads() -> None:
    bridge = SettingsBridge(Settings(), save=Mock())
    assert bridge.activeModelName == bridge.activeModelSize == ""
    assert bridge.activeModelState == "none"
    assert not bridge.canReinstall
    assert bridge.downloadState == ""
    assert bridge.downloadProgress == 0.0
    assert bridge.downloadTitle == bridge.speed == bridge.eta == ""
    bridge.reinstallActiveModel()
    bridge.cancelDownloads()
    meta = bridge.metaObject()
    for name in (
        "activeModelName",
        "activeModelSize",
        "activeModelState",
        "canReinstall",
        "downloadState",
        "downloadProgress",
        "downloadTitle",
        "speed",
        "eta",
    ):
        prop = meta.property(meta.indexOfProperty(name))
        assert prop.isValid() and prop.hasNotifySignal() and not prop.isWritable()
    assert meta.indexOfSlot(b"reinstallActiveModel()") >= 0
    assert meta.indexOfSlot(b"cancelDownloads()") >= 0
    prop = meta.property(meta.indexOfProperty("canReinstall"))
    assert prop.typeName() == "bool"
    assert prop.notifySignal().methodSignature() == b"activeModelStateChanged()"


@pytest.mark.parametrize("initial", ["ok", "broken"])
def test_reinstall_shares_progress_and_preserves_existing_model(
    shared_downloads: SharedDownloadsRig, initial: str
) -> None:
    rig = shared_downloads
    entry = rig.port.second
    rig.port.current = (entry.id, entry.revision)
    rig.port.records[rig.port.current] = initial
    states: list[str] = []
    can_reinstall: list[bool] = []
    rig.bridge.activeModelStateChanged.connect(lambda: states.append(rig.bridge.activeModelState))
    rig.bridge.activeModelStateChanged.connect(
        lambda: can_reinstall.append(rig.bridge.canReinstall)
    )
    settings_progress = QSignalSpy(rig.bridge.downloadProgressChanged)
    wizard_progress = QSignalSpy(rig.controller.downloadProgressChanged)
    finished = QSignalSpy(rig.downloads.queueFinished)
    rig.bridge.reinstallActiveModel()
    assert not rig.bridge.canReinstall
    assert settings_progress.wait(1000)
    rig.bridge.reinstallActiveModel()
    assert rig.port.visits == [entry.id]
    assert rig.port.records[rig.port.current] == initial
    assert rig.bridge.activeModelState == "downloading"
    assert rig.controller.downloadProgress == rig.bridge.downloadProgress == 0.5
    assert len(settings_progress) == len(wizard_progress)
    for name in ("downloadState", "downloadProgress", "downloadTitle", "speed", "eta"):
        assert getattr(rig.bridge, name) == getattr(rig.controller, name)
    rig.status.assert_any_call(f"Загружается {entry.name} — 50%")
    rig.port.releases[entry.id].set()
    assert finished.wait(1000)
    assert list(finished) == [[True]]
    assert rig.bridge.activeModelState == "ok"
    assert "verifying" in states and states[-1] == "ok"
    assert rig.port.installs == [entry.id]
    assert rig.controller.downloadState == rig.bridge.downloadState == "done"
    assert rig.controller.downloadProgress == rig.bridge.downloadProgress == 1.0
    assert rig.bridge.canReinstall
    assert False in can_reinstall and can_reinstall[-1] is True
    rig.status.assert_called_with("")


@pytest.mark.parametrize("event", [QEvent.Hide, QEvent.Close])
def test_window_leaving_does_not_cancel_download(
    shared_downloads: SharedDownloadsRig, event: QEvent.Type
) -> None:
    rig = shared_downloads
    entry = rig.port.entry
    progress = QSignalSpy(rig.bridge.downloadProgressChanged)
    finished = QSignalSpy(rig.downloads.queueFinished)
    rig.controller.toggleModel(entry.id)
    rig.controller.startSelectedDownloads()
    assert progress.wait(1000)
    rig.controller.eventFilter(rig.controller, QEvent(QEvent.Show))
    rig.controller.eventFilter(rig.controller, QEvent(event))
    assert rig.downloads._queue_running
    assert not rig.downloads._model_cancel.is_set()
    rig.port.releases[entry.id].set()
    assert finished.wait(1000)
    assert list(finished) == [[True]]
    assert rig.bridge.activeModelName == entry.name
    assert rig.bridge.activeModelState == "ok"


@pytest.mark.parametrize("visibility", ["unknown", "shown", "hidden", "closed", "reshown"])
@pytest.mark.parametrize("succeeds", [False, True])
def test_model_installed_notification_uses_visibility_at_queue_end(
    shared_downloads: SharedDownloadsRig, visibility: str, succeeds: bool
) -> None:
    from astra_voice.ui import notify

    rig = shared_downloads
    progress = QSignalSpy(rig.downloads.downloadProgressChanged)
    finished = QSignalSpy(rig.downloads.queueFinished)
    if not succeeds:
        rig.port.errors[rig.port.entry.id] = DownloadError("bad-path")
    rig.controller.download()
    assert progress.wait(1000)
    events = {
        "unknown": [],
        "shown": [QEvent.Show],
        "hidden": [QEvent.Show, QEvent.Hide],
        "closed": [QEvent.Show, QEvent.Close],
        "reshown": [QEvent.Show, QEvent.Hide, QEvent.Show],
    }
    for event in events[visibility]:
        rig.controller.eventFilter(rig.controller, QEvent(event))
    rig.port.releases[rig.port.entry.id].set()
    assert finished.wait(1000)
    assert list(finished) == [[succeeds]]
    assert cast(Mock, notify.notify_model_installed).call_count == int(
        succeeds and visibility in {"unknown", "hidden", "closed"}
    )


def test_notification_exception_does_not_break_queue(
    shared_downloads: SharedDownloadsRig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from astra_voice.ui import notify

    rig = shared_downloads
    monkeypatch.setattr(
        notify, "notify_model_installed", Mock(side_effect=RuntimeError("PRIVATE SPEECH"))
    )
    finished = QSignalSpy(rig.downloads.queueFinished)
    rig.port.releases[rig.port.entry.id].set()
    rig.controller.download()
    assert finished.wait(1000)
    assert rig.controller.modelReady
    assert rig.bridge.downloadState == "done"
    assert "PRIVATE SPEECH" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("code, expected", [("bad-path", "failed"), ("disk-full", "no-space")])
def test_reinstall_failure_is_shared(
    shared_downloads: SharedDownloadsRig, code: str, expected: str
) -> None:
    rig = shared_downloads
    entry = rig.port.entry
    rig.port.current = (entry.id, entry.revision)
    rig.port.records[rig.port.current] = "ok"
    rig.port.errors[entry.id] = DownloadError(code)
    rig.port.releases[entry.id].set()
    finished = QSignalSpy(rig.downloads.queueFinished)
    rig.bridge.reinstallActiveModel()
    assert finished.wait(1000)
    assert list(finished) == [[False]]
    assert rig.bridge.activeModelState == expected
    assert rig.bridge.downloadState == rig.controller.downloadState == expected
    assert rig.port.records[rig.port.current] == "ok"


def test_shared_downloads_outlive_controller(shared_downloads: SharedDownloadsRig) -> None:
    rig = shared_downloads
    entry = rig.port.entry
    progress = QSignalSpy(rig.bridge.downloadProgressChanged)
    finished = QSignalSpy(rig.downloads.queueFinished)
    rig.controller.download()
    assert progress.wait(1000)
    rig.controller.shutdown()
    assert not rig.downloads._model_cancel.is_set()
    sip.delete(rig.controller)
    rig.port.releases[entry.id].set()
    assert finished.wait(1000)
    assert rig.bridge.activeModelState == "ok"
    assert rig.bridge.downloadState == "done"


@pytest.mark.parametrize("onboarding_done", [False, True])
def test_app_uses_one_download_queue_and_shuts_it_down_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, onboarding_done: bool
) -> None:
    from test_app_shutdown import Rig

    from astra_voice import app as app_mod

    rig = Rig(monkeypatch, tmp_path)
    rig.settings.extra["onboarding_done"] = onboarding_done
    port = FakeModelPort()
    service_factory = Mock(return_value=port)
    download_factory = Mock(wraps=model_downloads.ModelDownloads)
    stop = Mock()
    shutdown = model_downloads.ModelDownloads.shutdown

    def stop_downloads(downloads: model_downloads.ModelDownloads) -> None:
        stop(downloads)
        shutdown(downloads)

    monkeypatch.setattr(model_downloads, "ModelService", service_factory)
    monkeypatch.setattr(model_downloads.ModelDownloads, "shutdown", stop_downloads)
    monkeypatch.setattr(model_downloads, "ModelDownloads", download_factory)

    def event_loop() -> int:
        properties = dict(
            item.args for item in rig.shell.rootContext().setContextProperty.call_args_list
        )
        bridge = properties["settingsBridge"]
        downloads = bridge._downloads
        assert downloads is not None
        assert ("onboarding" in properties) is not onboarding_done
        if not onboarding_done:
            assert properties["onboarding"]._downloads is downloads
        downloads._active_entry = port.entry
        downloads._queue_total_bytes = port.entry.size_bytes
        downloads._set_download_state("downloading")
        downloads._set_download_progress(port.entry.size_bytes * 0.42)
        rig.runtime.tray.set_download_status.assert_called_with("Загружается Тестовая модель — 42%")
        assert bridge.downloadProgress == 0.42
        return 7

    rig.app.exec_.side_effect = event_loop
    assert app_mod.main([]) == 7
    service_factory.assert_called_once()
    download_factory.assert_called_once_with(
        port, store=rig.factory.call_args.kwargs["model_store"], settings=rig.settings
    )
    stop.assert_called_once()


def test_queue_finished_reports_partial_success_once(shared_downloads: SharedDownloadsRig) -> None:
    rig = shared_downloads
    rig.port.errors[rig.port.second.id] = DownloadError("bad-path")
    for release in rig.port.releases.values():
        release.set()
    select_queue(rig.controller, rig.port)
    finished = QSignalSpy(rig.downloads.queueFinished)
    rig.controller.startSelectedDownloads()
    assert finished.wait(1000)
    assert list(finished) == [[True]]
    assert rig.port.visits == [rig.port.entry.id, rig.port.second.id]
    assert rig.port.current == (rig.port.entry.id, rig.port.entry.revision)
    assert rig.bridge.activeModelState == "ok"
    assert rig.bridge.downloadState == "done"


@pytest.mark.parametrize(
    "code, expected",
    [
        ("ok", ""),
        *[
            (code, "Не удалось запустить распознавание. Попробуйте переустановить модель")
            for code in ("load-failed", "worker-failed", "no-wav", "timeout")
        ],
        *[
            (code, "Распознавание на этом компьютере не работает. Обратитесь к администратору")
            for code in ("no-match", "transcribe-failed")
        ],
        *[
            (code, "Модель не прошла пробное распознавание на этом компьютере")
            for code in ("cancelled", "unknown")
        ],
    ],
)
def test_smoke_adapter_maps_reason_codes(code: str, expected: str) -> None:
    runner = Mock(return_value=Mock(ok=code == "ok", reason=code, text="PRIVATE SPEECH"))
    result = make_smoke_check(runner)(Path("/private/model"), FakeModelPort().entry)
    assert result.ok is (code == "ok")
    assert result.reason == expected
    assert result.text == ""


@pytest.mark.parametrize(
    "reason",
    ["", "Не удалось запустить распознавание. Попробуйте переустановить модель"],
)
def test_model_job_preserves_installer_selfcheck_reason(reason: str) -> None:
    port = FakeModelPort()
    port.result = InstallResult("broken", reason=reason, reason_code="selfcheck")
    job = _ModelJob(port, port.entry, threading.Event(), Path("/private/model"))
    assert job._execute() == (
        "broken",
        reason or "Распознавание на этом компьютере не работает. Обратитесь к администратору",
    )


@pytest.mark.parametrize("local", [False, True])
def test_model_install_engine_failure_reaches_card(model_rig: ModelRig, local: bool) -> None:
    port, create = model_rig
    reason = "Не удалось запустить распознавание. Попробуйте переустановить модель"
    port.result = InstallResult("broken", reason, reason_code="selfcheck")
    controller = create()
    done = QSignalSpy(controller.canFinishChanged)
    if local:
        controller.installFromPath("/fake/model")
    else:
        controller.download()
    assert done.wait(1000)
    assert controller.modelMessage == reason
    assert controller.models[0]["state"] == "failed"
    assert controller.models[0]["message"] == reason


class RecheckRig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

        contents = {
            "v3_e2e_ctc.int8.onnx": b"verified model contents",
            "v3_e2e_ctc_vocab.txt": "а\nб\n".encode(),
            "config.json": b'{"model_type":"gigaam"}',
        }
        self.entry = replace(
            FakeModelPort().entry,
            layout="onnx-asr-gigaam-v3",
            variant="gigaam-v3-e2e-ctc",
            size_bytes=sum(len(content) for content in contents.values()),
            files=tuple(
                FileSpec(name, hashlib.sha256(content).hexdigest(), len(content), f"/{name}")
                for name, content in contents.items()
            ),
        )
        self.service = ModelService.__new__(ModelService)
        self.service._catalog = Catalog(1, 1, (), (self.entry,))
        self.service._store = ModelStore(tmp_path / "models")
        self.service._cancel = threading.Event()
        self.directory = self.service._store.root / self.entry.id / self.entry.revision
        self.directory.mkdir(parents=True)
        for name, content in contents.items():
            (self.directory / name).write_bytes(content)
        # Старый установщик оставлял состояние внутри установленного набора.
        (self.directory / "state.json").write_text(
            json.dumps(
                {
                    "state": "broken",
                    "reason": "старый отказ",
                    "layout": self.entry.layout,
                    "variant": self.entry.variant,
                    "size_bytes": self.entry.size_bytes,
                }
            )
        )
        self.started, self.release = threading.Event(), threading.Event()
        self.code = "ok"
        self.error: Exception | None = None
        self.worker_thread: int | None = None

        def run(request: object, *, cancel: Callable[[], bool]) -> Mock:
            self.worker_thread = threading.get_ident()
            self.started.set()
            deadline = time.monotonic() + 2
            while not self.release.wait(0.005):
                if cancel():
                    return Mock(ok=False, reason="cancelled")
                assert time.monotonic() < deadline, "Тест не завершил перепроверку"
            if self.error is not None:
                raise self.error
            return Mock(ok=self.code == "ok", reason=self.code, text="PRIVATE SPEECH")

        self.runner = Mock(side_effect=run)
        monkeypatch.setattr(model_downloads, "SmokeRunner", Mock(return_value=self.runner))
        self.ok = Mock(wraps=self.service.mark_ok)
        self.broken = Mock(wraps=self.service.mark_broken)
        self.current = Mock(wraps=self.service.set_current)
        monkeypatch.setattr(self.service, "mark_ok", self.ok)
        monkeypatch.setattr(self.service, "mark_broken", self.broken)
        monkeypatch.setattr(self.service, "set_current", self.current)
        self.verify = Mock(wraps=self.service.verify_files)
        monkeypatch.setattr(self.service, "verify_files", self.verify)
        self.downloads = model_downloads.ModelDownloads(self.service)
        self.ready = QSignalSpy(self.downloads.modelReadyChanged)

    def finish(self) -> None:
        self.release.set()
        deadline = time.monotonic() + 2
        while self.downloads._model_thread is not None:
            QCoreApplication.processEvents()
            assert time.monotonic() < deadline, "Перепроверка не завершилась"
            time.sleep(0.001)


@pytest.fixture
def recheck_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RecheckRig]:
    rig = RecheckRig(tmp_path, monkeypatch)
    yield rig
    rig.downloads.shutdown()
    rig.release.set()
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.mark.parametrize("has_current", [False, True])
def test_recheck_success_selects_only_when_no_current(
    recheck_rig: RecheckRig, has_current: bool
) -> None:
    rig = recheck_rig
    if has_current:
        store = rig.service._store
        staged = store.staging_dir("other", "r1")
        (staged / "model.bin").write_bytes(b"working model")
        store.commit("other", "r1")
        store.set_current("other", "r1")
    rig.downloads.start_recheck()
    assert rig.started.wait(1)
    assert rig.worker_thread != threading.get_ident()
    assert rig.downloads.downloadState == "verifying"
    assert rig.downloads.downloadTitle == "Проверяю модель…"
    assert rig.downloads.status_text() == "Проверяю модель…"
    assert rig.downloads.models[0]["state"] == "verifying"
    assert rig.downloads.models[0]["message"] == ""
    assert rig.downloads.modelState == "verifying"
    rig.ok.assert_not_called()
    rig.finish()
    rig.ok.assert_called_once_with(rig.entry.id, rig.entry.revision)
    rig.broken.assert_not_called()
    if has_current:
        rig.current.assert_not_called()
        assert rig.service.current_ids() == ("other", "r1")
    else:
        rig.current.assert_called_once_with(rig.entry.id, rig.entry.revision)
        assert rig.service.current_ids() == (rig.entry.id, rig.entry.revision)
    assert rig.service.recheck_entries() == ()
    assert rig.downloads.models[0]["state"] == "installed"
    assert len(rig.ready) == 1


@pytest.mark.parametrize("failure", ["checksum", "load-failed", "no-match", "transcribe-failed"])
def test_recheck_confirmed_failure_marks_broken(recheck_rig: RecheckRig, failure: str) -> None:
    rig = recheck_rig
    if failure == "checksum":
        (rig.directory / rig.entry.files[0].path).write_bytes(b"corruption")
        expected = "Файлы модели повреждены: контрольная сумма не совпала. Получите их заново."
    else:
        rig.code = failure
        expected = (
            "Не удалось запустить распознавание. Попробуйте переустановить модель"
            if failure == "load-failed"
            else "Распознавание на этом компьютере не работает. Обратитесь к администратору"
        )
    rig.downloads.start_recheck()
    rig.finish()
    rig.ok.assert_not_called()
    rig.current.assert_not_called()
    rig.broken.assert_called_once_with(rig.entry.id, rig.entry.revision, expected)
    assert rig.service.recheck_entries() == ()
    assert rig.downloads.models[0]["state"] == "failed"
    assert rig.downloads.models[0]["message"] == expected
    if failure == "checksum":
        rig.runner.assert_not_called()


@pytest.mark.parametrize("failure", ["corrupt", "missing", "store-error"])
def test_recheck_mark_ok_rejection_fails_without_store_writes_or_downloads(
    recheck_rig: RecheckRig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    from astra_voice.models import store as store_mod

    rig = recheck_rig
    store = rig.service._store
    rig.downloads.start_recheck()
    assert rig.started.wait(1)
    # Метаданные могут стать недоступны уже после постановки на перепроверку.
    state_path = rig.directory.with_suffix(".json")
    if failure == "corrupt":
        state_path.write_bytes(b"{")
    elif failure == "missing":
        state_path.unlink()
    else:
        rig.ok.side_effect = StoreError("invalid-metadata", "PRIVATE SPEECH /private/model")
    before = {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    write = Mock(wraps=store_mod._write_json)
    monkeypatch.setattr(store_mod, "_write_json", write)
    start_queue = Mock(wraps=rig.downloads._begin_queue)
    monkeypatch.setattr(rig.downloads, "_begin_queue", start_queue)

    rig.finish()
    QCoreApplication.processEvents()

    rig.ok.assert_called_once_with(rig.entry.id, rig.entry.revision)
    rig.broken.assert_not_called()
    rig.current.assert_not_called()
    write.assert_not_called()
    assert {path: path.read_bytes() for path in store.root.rglob("*") if path.is_file()} == before
    card = rig.downloads.models[0]
    assert card["state"] == "failed"
    assert card["message"] == "Модель нужно переустановить."
    assert rig.downloads.modelState == "broken"
    assert rig.downloads.modelMessage == card["message"]
    assert rig.downloads.downloadState == "failed"
    assert not rig.downloads.modelReady
    start_queue.assert_not_called()
    assert not rig.downloads._queue_running and not rig.downloads._queue
    assert rig.downloads._model_thread is None
    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (logging.WARNING, "Хранилище отклонило успешный результат перепроверки модели")
    ]
    assert caplog.records[0].exc_info is None
    assert "PRIVATE SPEECH" not in caplog.text and "/private/model" not in caplog.text


@pytest.mark.parametrize("result", ["ok", "checksum", "no-match", "metadata"])
def test_recheck_never_uses_network_or_starts_download_queue(
    recheck_rig: RecheckRig, monkeypatch: pytest.MonkeyPatch, result: str
) -> None:
    rig = recheck_rig
    assert rig.service._store.records()[0].recheck is True
    network_calls = [
        Mock(side_effect=AssertionError("Перепроверка обратилась к сети")) for _ in range(3)
    ]
    monkeypatch.setattr(socket, "getaddrinfo", network_calls[0])
    monkeypatch.setattr(socket.socket, "connect", network_calls[1])
    monkeypatch.setattr(requests, "get", network_calls[2])
    download = Mock(side_effect=AssertionError("Перепроверка запустила загрузку"))
    monkeypatch.setattr(rig.service, "download", download)
    start_queue = Mock(wraps=rig.downloads._begin_queue)
    monkeypatch.setattr(rig.downloads, "_begin_queue", start_queue)
    if result == "checksum":
        (rig.directory / rig.entry.files[0].path).write_bytes(b"corruption")
    elif result == "no-match":
        rig.code = "no-match"

    rig.downloads.start_recheck()
    if result == "metadata":
        assert rig.started.wait(1)
        rig.directory.with_suffix(".json").write_bytes(b"{")
    rig.finish()
    QCoreApplication.processEvents()

    assert [probe.call_count for probe in network_calls] == [0, 0, 0]
    download.assert_not_called()
    start_queue.assert_not_called()
    assert not rig.downloads._queue_running and not rig.downloads._queue
    assert rig.downloads._model_thread is None
    assert rig.downloads.downloadState == ("done" if result == "ok" else "failed")
    assert rig.downloads.models[0]["state"] == ("installed" if result == "ok" else "failed")


@pytest.mark.parametrize(
    "failure",
    ["cancel", "shutdown", "exception", "worker-failed", "no-wav", "timeout", "cancelled"],
)
def test_recheck_unavailable_preserves_record_and_card(
    recheck_rig: RecheckRig, failure: str
) -> None:
    rig = recheck_rig
    previous = rig.service._store.records()
    card = rig.downloads.models[0]
    model_state = (rig.downloads.modelState, rig.downloads.modelMessage)
    rig.downloads.start_recheck()
    assert rig.started.wait(1)
    if failure == "cancel":
        rig.downloads.cancelDownloads()
    elif failure == "shutdown":
        rig.downloads.shutdown()
        assert rig.downloads._model_thread is not None
        assert not rig.downloads._model_thread.isRunning()
    elif failure == "exception":
        rig.error = RuntimeError("PRIVATE SPEECH /private/model")
    else:
        rig.code = failure
    rig.finish()
    assert rig.ok.mock_calls == rig.broken.mock_calls == rig.current.mock_calls == []
    assert rig.service._store.records() == previous
    assert rig.service.recheck_entries() == (rig.entry,)
    assert rig.downloads.models[0] == card
    assert (rig.downloads.modelState, rig.downloads.modelMessage) == model_state
    assert rig.downloads.downloadState == "idle"
    assert not rig.ready


def test_recheck_read_exception_does_not_write(recheck_rig: RecheckRig) -> None:
    rig = recheck_rig
    rig.verify.side_effect = PermissionError("/private/model")
    rig.downloads.start_recheck()
    rig.finish()
    assert rig.ok.mock_calls == rig.broken.mock_calls == rig.current.mock_calls == []
    rig.runner.assert_not_called()
    assert rig.service.recheck_entries() == (rig.entry,)


def test_recheck_shutdown_cancels_hashing(
    recheck_rig: RecheckRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.models import installer
    from astra_voice.security.verify import HashCancelledError

    rig = recheck_rig
    started, stopped = threading.Event(), threading.Event()

    def hash_file(path: Path, *, cancel: Callable[[], bool]) -> str:
        assert path == rig.directory / rig.entry.files[0].path
        assert threading.get_ident() != threading.main_thread().ident
        started.set()
        deadline = time.monotonic() + 2
        while not cancel():
            assert time.monotonic() < deadline
            time.sleep(0.001)
        stopped.set()
        raise HashCancelledError

    monkeypatch.setattr(installer, "sha256_file", hash_file)
    rig.downloads.start_recheck()
    assert started.wait(1)
    rig.downloads.shutdown()
    assert stopped.is_set()
    rig.finish()
    assert rig.ok.mock_calls == rig.broken.mock_calls == rig.current.mock_calls == []
    rig.runner.assert_not_called()
    assert rig.service.recheck_entries() == (rig.entry,)


def test_recheck_shutdown_discards_result_waiting_for_gui(recheck_rig: RecheckRig) -> None:
    rig = recheck_rig
    rig.release.set()
    rig.downloads.start_recheck()
    thread = rig.downloads._model_thread
    assert thread is not None and thread.wait(1000)
    # Поток уже проверил модель, но его сигналы ещё не обработаны GUI.
    rig.downloads.shutdown()
    rig.finish()
    assert rig.ok.mock_calls == rig.broken.mock_calls == rig.current.mock_calls == []
    assert rig.service.recheck_entries() == (rig.entry,)


@pytest.mark.parametrize(
    "damage", ["extra", "missing", "symlink", "directory", "size", "checksum", "layout"]
)
def test_recheck_verifies_entire_installed_contents(recheck_rig: RecheckRig, damage: str) -> None:
    rig = recheck_rig
    assert rig.service.recheck_entries() == (rig.entry,)
    assert not (rig.directory / "state.json").exists()
    assert rig.service.verify_files(rig.entry) == (True, "")
    model_file = rig.directory / rig.entry.files[0].path
    if damage == "extra":
        (rig.directory / "unexpected").touch()
    elif damage == "missing":
        model_file.unlink()
    elif damage == "symlink":
        model_file.rename(rig.directory.parent / "outside")
        model_file.symlink_to(rig.directory.parent / "outside")
    elif damage == "directory":
        (rig.directory / "subdir").mkdir()
    elif damage == "size":
        rig.entry = replace(
            rig.entry, files=(replace(rig.entry.files[0], size=999), *rig.entry.files[1:])
        )
    elif damage == "layout":
        rig.entry = replace(rig.entry, variant="unknown-variant")
    else:
        model_file.write_bytes(b"X" * model_file.stat().st_size)
    ok, reason = rig.service.verify_files(rig.entry)
    assert not ok and reason
    assert all(
        value not in reason
        for value in (model_file.name, str(rig.directory), "unexpected", "unknown-variant")
    )


@pytest.mark.parametrize(
    "verdict",
    [(True, ""), (False, "Файлы в папке не подходят для выбранной модели.")],
)
def test_model_service_returns_installer_verdict(
    recheck_rig: RecheckRig, monkeypatch: pytest.MonkeyPatch, verdict: tuple[bool, str]
) -> None:

    rig = recheck_rig
    verify = Mock(return_value=verdict)
    monkeypatch.setattr(model_downloads, "verify_installed", verify)

    assert rig.service.verify_files(rig.entry) == verdict
    verify.assert_called_once_with(rig.directory, rig.entry, cancel=rig.service._cancel.is_set)


def test_installed_revoked_entry_stays_visible_but_never_ready(
    recheck_rig: RecheckRig,
) -> None:
    """Установленную отозванную ревизию показываем, но готовой моделью не считаем."""
    rig = recheck_rig
    rig.service._store.mark_ok(rig.entry.id, rig.entry.revision)
    assert rig.service.installed_ok() is True
    rig.service._catalog = Catalog(
        1, 1, (RevokedEntry(rig.entry.id, rig.entry.revision, "битый экспорт"),), (rig.entry,)
    )
    assert rig.service.is_revoked(rig.entry) is True
    assert rig.service.entries() == (rig.entry,)
    assert rig.service.recommended() is None
    assert rig.service.installed_ok() is False


def test_revoked_entry_that_is_not_installed_is_hidden(recheck_rig: RecheckRig) -> None:
    rig = recheck_rig
    other = replace(rig.entry, id="other-model")
    rig.service._catalog = Catalog(
        1, 1, (RevokedEntry(other.id, other.revision, "битый экспорт"),), (rig.entry, other)
    )
    assert rig.service.entries() == (rig.entry,)


def test_recheck_entries_match_revision_and_exclude_revoked(recheck_rig: RecheckRig) -> None:
    rig = recheck_rig
    other = replace(rig.entry, revision="uninstalled")
    rig.service._catalog = Catalog(1, 1, (), (other, rig.entry))
    assert rig.service.recheck_entries() == (rig.entry,)
    rig.service._catalog = Catalog(
        1, 1, (RevokedEntry(rig.entry.id, rig.entry.revision, "отозвана"),), (other, rig.entry)
    )
    assert rig.service.recheck_entries() == ()


def test_recheck_waits_for_download_queue_and_runs_one_at_a_time(
    shared_downloads: SharedDownloadsRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = shared_downloads
    port, downloads = rig.port, rig.downloads
    entered, release = threading.Event(), threading.Event()
    visits: list[str] = []

    def verify(entry: CatalogEntry) -> tuple[bool, str]:
        assert port.active == 0
        visits.append(entry.id)
        entered.set()
        assert release.wait(2)
        return True, ""

    monkeypatch.setattr(port, "recheck_entries", lambda: port.catalog)
    monkeypatch.setattr(port, "verify_files", verify)
    monkeypatch.setattr(port, "smoke", lambda entry: (True, ""))
    mark_ok = Mock()
    monkeypatch.setattr(port, "mark_ok", mark_ok)
    select_queue(rig.controller, port)
    finished = QSignalSpy(downloads.queueFinished)
    rig.controller.startSelectedDownloads()
    downloads.start_recheck()
    downloads.start_recheck()
    assert not visits
    for event in port.releases.values():
        event.set()
    assert finished.wait(1000)
    assert entered.wait(1)
    try:
        assert visits == [port.entry.id]
        assert downloads.downloadState == "verifying"
        assert downloads.models[0]["message"] == ""
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while downloads._model_thread is not None:
        QCoreApplication.processEvents()
        assert time.monotonic() < deadline
        time.sleep(0.001)
    assert visits == [port.entry.id, port.second.id]
    assert mark_ok.call_args_list == [call(entry.id, entry.revision) for entry in port.catalog]


_REENTRANT_MODEL_SIGNALS = (
    "modelsChanged",
    "modelReadyChanged",
    "selectionChanged",
    "downloadStateChanged",
    "queueFinished",
)


def finish_downloads(downloads: ModelDownloads) -> None:
    deadline = time.monotonic() + 2
    while downloads._model_thread is not None or downloads._queue_running:
        QCoreApplication.processEvents()
        assert time.monotonic() < deadline, "Очередь моделей не завершилась"
        time.sleep(0.001)
    assert downloads._active_entry is None
    assert downloads._model_result is None
    assert downloads.downloadState in {"idle", "done", "failed", "no-space"}


def exercise_reentrant_model_start(
    rig: SharedDownloadsRig,
    monkeypatch: pytest.MonkeyPatch,
    signal_name: str,
    phase: str,
    action: str,
) -> None:
    downloads, port = rig.downloads, rig.port
    # Первая модель есть, но повреждена; вторая выбрана для новой загрузки.
    port.records[(port.entry.id, port.entry.revision)] = "broken"
    port.errors[port.entry.id] = DownloadError("bad-path")
    downloads.toggleModel(port.second.id)
    for release in port.releases.values():
        release.set()
    monkeypatch.setattr(port, "recheck_entries", lambda: (port.entry,))
    monkeypatch.setattr(port, "verify_files", lambda entry: (False, "Ошибка проверки"))
    monkeypatch.setattr(port, "mark_broken", Mock())
    before: list[tuple[bool, Any, Any, str]] = []
    started: list[tuple[bool, Any, Any]] = []
    finished = QSignalSpy(downloads.queueFinished)

    def receive(*args: object) -> None:
        if before or downloads._model_thread is not None:
            return
        before.append(
            (
                downloads._rechecking,
                downloads._active_entry,
                downloads._model_result,
                downloads.downloadState,
            )
        )
        port.errors.clear()
        if action == "startSelectedDownloads":
            downloads.startSelectedDownloads()
        elif action == "retryModel":
            downloads.retryModel(port.entry.id)
        elif action == "reinstallActiveModel":
            rig.bridge.reinstallActiveModel()
        else:
            downloads.installFromPath("/fake/model")
        started.append((downloads._queue_running, downloads._active_entry, downloads._model_thread))

    signal = getattr(downloads, signal_name)
    signal.connect(receive)
    if phase == "recheck":
        downloads.start_recheck()
        finish_downloads(downloads)
        if signal_name == "queueFinished":
            # Перепроверка не эмитит queueFinished; проверяем первую очередь после неё.
            assert before == []
            downloads.download()
    else:
        downloads.download()
    finish_downloads(downloads)
    signal.disconnect(receive)
    assert len(before) == 1
    assert before[0] == (False, None, None, "failed")
    assert len(started) == 1
    running, entry, thread = started[0]
    assert running and thread is not None
    assert entry == (port.second if action == "startSelectedDownloads" else port.entry)
    assert downloads.downloadState == "done"
    assert port.max_active == bool(port.visits)
    assert port.installs == [entry.id]
    assert list(finished) == (
        [[False], [True]] if phase == "queue" or signal_name == "queueFinished" else [[True]]
    )

    # После повторного входа запускаем ещё одну настоящую очередь через публичный слот.
    # Освобождаем вторую модель в фейковом хранилище, чтобы она требовала загрузки.
    port.records.pop((port.second.id, port.second.revision), None)
    previous = len(port.visits)
    downloads.startSelectedDownloads()
    assert downloads._queue_running and downloads._model_thread is not None
    finish_downloads(downloads)
    assert port.visits[previous:] == [port.second.id]
    assert downloads.downloadState == "done"


@pytest.mark.parametrize("signal_name", _REENTRANT_MODEL_SIGNALS)
@pytest.mark.parametrize("phase", ["recheck", "queue"])
def test_model_signals_can_synchronously_start_selected_downloads(
    shared_downloads: SharedDownloadsRig,
    monkeypatch: pytest.MonkeyPatch,
    signal_name: str,
    phase: str,
) -> None:
    exercise_reentrant_model_start(
        shared_downloads, monkeypatch, signal_name, phase, "startSelectedDownloads"
    )


@pytest.mark.parametrize("signal_name", _REENTRANT_MODEL_SIGNALS)
@pytest.mark.parametrize("phase", ["recheck", "queue"])
@pytest.mark.parametrize("action", ["retryModel", "reinstallActiveModel", "installFromPath"])
def test_model_signals_can_synchronously_retry_or_reinstall(
    shared_downloads: SharedDownloadsRig,
    monkeypatch: pytest.MonkeyPatch,
    signal_name: str,
    phase: str,
    action: str,
) -> None:
    exercise_reentrant_model_start(shared_downloads, monkeypatch, signal_name, phase, action)


@pytest.mark.parametrize("missing", ["result", "entry", "both"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_model_thread_finished_recovers_inconsistent_queue(
    shared_downloads: SharedDownloadsRig,
    caplog: pytest.LogCaptureFixture,
    missing: str,
    cancelled: bool,
) -> None:
    downloads, port = shared_downloads.downloads, shared_downloads.port
    downloads.toggleModel(port.entry.id)
    downloads._queue_running = True
    downloads._queue_cancelled = cancelled
    downloads._queue_entries = port.catalog
    downloads._queue = [port.second]
    downloads._active_entry = None if missing in {"entry", "both"} else port.entry
    downloads._model_result = None if missing in {"result", "both"} else ("error", "PRIVATE SPEECH")
    downloads._set_card(port.entry, "downloading")
    downloads._set_card(port.second, "queued")
    downloads._set_download_state("downloading")
    finished = QSignalSpy(downloads.queueFinished)

    downloads._model_thread_finished()

    assert not downloads._queue_running and not downloads._queue
    assert not downloads._queue_entries
    assert downloads._active_entry is None
    assert downloads._model_result is None
    assert downloads.downloadState == ("idle" if cancelled else "failed")
    assert list(finished) == [[False]]
    assert "очередь сброшена" in caplog.text
    assert "PRIVATE SPEECH" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    port.releases[port.entry.id].set()
    downloads.startSelectedDownloads()
    assert downloads._model_thread is not None
    finish_downloads(downloads)
    assert port.visits == [port.entry.id]
    assert downloads.downloadState == "done"


def test_begin_queue_rejects_recheck_without_thread(shared_downloads: SharedDownloadsRig) -> None:
    downloads, port = shared_downloads.downloads, shared_downloads.port
    downloads.toggleModel(port.second.id)
    downloads._rechecking = True
    downloads._active_entry = port.entry
    downloads._model_result = ("installed", "")
    changed = QSignalSpy(downloads.modelsChanged)
    downloads.startSelectedDownloads()
    downloads.installFromPath("/fake/model")
    downloads.toggleModel(port.second.id)
    assert not downloads._queue_running and not downloads._queue
    assert downloads._active_entry is port.entry
    assert downloads._model_result == ("installed", "")
    assert downloads._model_thread is None
    assert not changed and not port.visits
    downloads._rechecking = False
    downloads._active_entry = None
    downloads._model_result = None
    port.releases[port.second.id].set()
    downloads.startSelectedDownloads()
    finish_downloads(downloads)
    assert port.visits == [port.second.id]


def test_model_thread_finished_wait_is_bounded_and_keeps_job_until_cleanup(
    shared_downloads: SharedDownloadsRig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    downloads, port = shared_downloads.downloads, shared_downloads.port
    port.releases[port.entry.id].set()
    downloads.download()
    thread, job = downloads._model_thread, downloads._model_job
    assert thread is not None and job is not None
    assert thread.wait(1000)
    wait = Mock(side_effect=[False, True])
    monkeypatch.setattr(thread, "wait", wait)
    retry = Mock()
    monkeypatch.setattr(QTimer, "singleShot", retry)
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
    assert wait.call_args_list == [call(5000)]
    assert "не завершился за 5 секунд" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert downloads._model_thread is thread and downloads._model_job is job
    assert downloads._queue_running
    downloads.startSelectedDownloads()
    assert downloads._model_thread is thread
    assert not sip.isdeleted(thread)
    assert retry.call_count == 1
    delay, callback = retry.call_args.args
    assert delay == 100
    callback()
    assert wait.call_args_list == [call(5000), call(5000)]
    assert downloads._model_thread is downloads._model_job is None
    assert not downloads._queue_running
    assert downloads.downloadState == "done"


@pytest.mark.parametrize("blocked", ["step", "shutdown"])
@pytest.mark.parametrize("state", ["listening", "idle", "error"])
def test_level_receive_ignores_shutdown_and_other_steps(
    blocked: str, state: Literal["listening", "idle", "error"]
) -> None:
    rig = MicrophoneBridgeRig()
    controller = rig.controller
    controller.startLevelMonitor()
    receive = rig.host.start_level_monitor.call_args.args[1]
    if blocked == "step":
        controller._step = 5
    else:
        controller._shutting_down = True
    before = (
        controller.levelState,
        controller.levelMessage,
        controller.level,
        controller._level_epoch,
    )
    spies = [
        QSignalSpy(getattr(controller, name))
        for name in ("levelStateChanged", "levelMessageChanged", "levelChanged", "peakChanged")
    ]
    controller._test_after_level = True
    receive(MicrophoneLevelUpdate(state, peak_dbfs=-10, message="Позднее событие"))
    assert before == (
        controller.levelState,
        controller.levelMessage,
        controller.level,
        controller._level_epoch,
    )
    assert all(not spy for spy in spies)
    rig.host.start_test.assert_not_called()
    controller.shutdown()
    rig.event("cancelled")


@pytest.mark.parametrize("visible", [None, False, True])
def test_level_monitor_requires_window_not_explicitly_hidden(visible: bool | None) -> None:
    rig = MicrophoneBridgeRig()
    controller = rig.controller
    if visible is not None:
        controller.eventFilter(controller, QEvent(QEvent.Show if visible else QEvent.Hide))
    rig.host.reset_mock()
    controller.startLevelMonitor()
    if visible is False:
        rig.host.start_level_monitor.assert_not_called()
        assert rig.commands == []
        assert controller.levelState == "idle"
    else:
        rig.host.start_level_monitor.assert_called_once()
        assert [message["type"] for message in rig.commands] == ["record.start"]
        controller.shutdown()
        rig.event("cancelled")


@pytest.mark.parametrize(
    "signal_name",
    [
        "modelsChanged",
        "downloadStateChanged",
        "downloadProgressChanged",
        "downloadTitleChanged",
        "progressChanged",
        "speedChanged",
        "etaChanged",
    ],
)
def test_model_signal_cancel_is_idempotent_and_cannot_restore_stale_progress(
    shared_downloads: SharedDownloadsRig, monkeypatch: pytest.MonkeyPatch, signal_name: str
) -> None:
    downloads, port = shared_downloads.downloads, shared_downloads.port
    port.block = True
    clock = Mock(return_value=100.0)
    monkeypatch.setattr(downloads, "_clock", clock)
    select_queue(shared_downloads.controller, port)
    calls: list[str] = []

    def cancel() -> None:
        calls.append(downloads.downloadState)
        # Оставляем обработчик подключённым и во время уведомлений самой отмены.
        downloads.cancelDownloads()
        downloads.cancelDownload()
        if downloads._queue_running:
            downloads.startSelectedDownloads()

    signal = getattr(downloads, signal_name)
    signal.connect(cancel)
    downloads.startSelectedDownloads()
    clock.return_value = 105.0
    downloads._model_progressed(0.5, 0, -1)
    finish_downloads(downloads)
    signal.disconnect(cancel)
    assert calls
    assert downloads.downloadState == "idle"
    assert downloads.downloadProgress == 0
    assert downloads.speed == downloads.eta == ""
    assert all(card["state"] == "available" for card in downloads.models)
    assert not port.installs and not downloads._queue
    assert port.second.id not in port.visits
    port.block = False
    for release in port.releases.values():
        release.set()
    downloads.startSelectedDownloads()
    finish_downloads(downloads)
    assert port.installs == [port.entry.id, port.second.id]
    assert downloads.downloadState == "done"


@pytest.mark.parametrize("phase", ["recheck", "queue"])
def test_model_start_signal_can_shutdown_started_thread(
    shared_downloads: SharedDownloadsRig, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    downloads, port = shared_downloads.downloads, shared_downloads.port
    port.block = True
    monkeypatch.setattr(port, "recheck_entries", lambda: (port.entry,))
    monkeypatch.setattr(port, "verify_files", lambda entry: (True, ""))
    monkeypatch.setattr(port, "smoke", lambda entry: (True, ""))
    monkeypatch.setattr(port, "mark_ok", Mock())
    calls: list[bool] = []

    def shutdown() -> None:
        calls.append(downloads._model_thread is not None)
        downloads.shutdown()

    downloads.modelsChanged.connect(shutdown)
    if phase == "recheck":
        downloads.start_recheck()
    else:
        downloads.download()
    finish_downloads(downloads)
    assert calls[0]
    assert downloads._shutting_down
    assert not downloads._rechecking
    assert downloads.downloadState == "idle"
    downloads.startSelectedDownloads()
    downloads.installFromPath("/fake/model")
    assert downloads._model_thread is None and not downloads._queue_running


@pytest.mark.parametrize("root_exists", [False, True])
def test_model_service_free_bytes_uses_store_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_exists: bool
) -> None:
    service = ModelService.__new__(ModelService)
    service._store = ModelStore(tmp_path / "missing-parent" / "models")
    if root_exists:
        service._store.root.mkdir(parents=True)
    usage = Mock(return_value=Mock(free=42_100_000_000))
    monkeypatch.setattr("astra_voice.ui.model_downloads.shutil.disk_usage", usage)
    assert service.free_bytes() == 42_100_000_000
    usage.assert_called_once_with(service._store.root if root_exists else tmp_path)
    assert service._store.root.exists() is root_exists
    usage.return_value.free = 234_000_001
    assert service.disk_missing_bytes(300_000_000) == 125_999_999


def test_free_space_text_updates_selection_and_each_download(
    shared_downloads: SharedDownloadsRig,
) -> None:
    rig = shared_downloads
    changes = QSignalSpy(rig.controller.freeSpaceTextChanged)
    assert rig.controller.freeSpaceText == "свободно на диске 1,0 ГБ"
    rig.port.available_bytes = 42_100_000_000
    select_queue(rig.controller, rig.port)
    assert rig.controller.freeSpaceText == "свободно на диске 42,1 ГБ"
    assert len(changes) == 1
    rig.controller.startSelectedDownloads()
    completed = QSignalSpy(rig.controller.modelReadyChanged)
    rig.port.available_bytes = 40_000_000_000
    rig.port.releases[rig.port.entry.id].set()
    assert completed.wait(1000)
    assert rig.controller.downloadState == "downloading"
    assert rig.controller.freeSpaceText == "свободно на диске 40,0 ГБ"
    assert len(changes) == 2
    rig.port.available_bytes = 512_000_000
    rig.port.releases[rig.port.second.id].set()
    assert completed.wait(1000)
    assert rig.controller.downloadState == "done"
    assert rig.controller.freeSpaceText == "свободно на диске 512 МБ"
    assert len(changes) == 3


@pytest.mark.parametrize("error", [OSError("SECRET /path"), StoreError("broken-store")])
def test_free_space_text_clears_on_error(
    model_rig: ModelRig, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    port, create = model_rig
    controller = create()
    assert controller.freeSpaceText == "свободно на диске 42,1 ГБ"
    changes = QSignalSpy(controller.freeSpaceTextChanged)
    monkeypatch.setattr(port, "free_bytes", Mock(side_effect=error))
    controller.toggleModel(port.entry.id)
    assert controller.freeSpaceText == ""
    assert len(changes) == 1
    assert create().freeSpaceText == ""


def test_free_space_text_without_catalog_or_optional_port_method(
    model_rig: ModelRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, create = model_rig
    assert create(None).freeSpaceText == ""
    monkeypatch.delattr(FakeModelPort, "free_bytes")
    assert create().freeSpaceText == ""


@pytest.mark.parametrize("via_settings", [False, True])
@pytest.mark.parametrize("service_store", [False, True])
def test_open_models_folder_uses_store_root_and_injected_opener(
    shared_downloads: SharedDownloadsRig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desktop_opener: Mock,
    via_settings: bool,
    service_store: bool,
) -> None:
    rig = shared_downloads
    store = ModelStore(tmp_path / "SECRET models #1")
    store.root.mkdir()
    if service_store:
        monkeypatch.setattr(rig.port, "_store", store, raising=False)
    else:
        rig.downloads._store = store
    target = rig.bridge if via_settings else rig.controller
    opener = Mock(return_value=True)
    monkeypatch.setattr(target, "_open_url", opener)
    target.openModelsFolder()
    opener.assert_called_once_with(QUrl.fromLocalFile(str(store.root)))
    assert opener.call_args.args[0].toLocalFile() == str(store.root)
    desktop_opener.assert_not_called()
    meta = target.metaObject()
    for index in range(meta.propertyOffset(), meta.propertyCount()):
        prop = meta.property(index)
        if prop.typeName() == "QString":
            assert str(store.root) not in prop.read(target)


@pytest.mark.parametrize("via_settings", [False, True])
def test_open_models_folder_default_opener_is_replaceable(
    shared_downloads: SharedDownloadsRig, tmp_path: Path, desktop_opener: Mock, via_settings: bool
) -> None:
    rig = shared_downloads
    rig.downloads._store = ModelStore(tmp_path)
    target = rig.bridge if via_settings else rig.controller
    target.openModelsFolder()
    desktop_opener.assert_called_once_with(QUrl.fromLocalFile(str(tmp_path)))


@pytest.mark.parametrize("via_settings", [False, True])
@pytest.mark.parametrize("unavailable", ["no-store", "missing", "file", "os-error", "store-error"])
def test_open_models_folder_unavailable_is_private_noop(
    shared_downloads: SharedDownloadsRig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    desktop_opener: Mock,
    via_settings: bool,
    unavailable: str,
) -> None:
    rig = shared_downloads
    root = tmp_path / "SECRET models"
    if unavailable != "no-store":
        rig.downloads._store = ModelStore(root)
    if unavailable == "file":
        root.write_text("not a directory")
    if unavailable in {"os-error", "store-error"}:
        error = (
            OSError(f"SECRET {root}") if unavailable == "os-error" else StoreError("broken-store")
        )
        monkeypatch.setattr(Path, "is_dir", Mock(side_effect=error))
    target = rig.bridge if via_settings else rig.controller
    with caplog.at_level(logging.DEBUG, logger="astra_voice.ui"):
        target.openModelsFolder()
    desktop_opener.assert_not_called()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG
    assert caplog.records[0].exc_info is None
    assert "SECRET" not in caplog.text and str(tmp_path) not in caplog.text
    if unavailable == "missing":
        assert not root.exists()


def test_settings_new_members_without_downloads(desktop_opener: Mock) -> None:
    bridge = SettingsBridge(Settings(), save=Mock())
    assert bridge.downloadDetail == bridge.activeModelMessage == ""
    assert not bridge.canInstall
    bridge.installRecommendedModel()
    bridge.openModelsFolder()
    desktop_opener.assert_not_called()


@pytest.mark.parametrize(
    "state", ["idle", "downloading", "verifying", "done", "failed", "no-space"]
)
def test_download_detail_only_in_no_space(
    shared_downloads: SharedDownloadsRig, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    rig = shared_downloads
    missing = Mock(return_value=125_999_999)
    monkeypatch.setattr(rig.port, "disk_missing_bytes", missing)
    rig.downloads._no_space_size_bytes = rig.port.second.size_bytes
    rig.downloads._set_download_state(state)
    expected = "нужно ещё 126 МБ" if state == "no-space" else ""
    assert rig.bridge.downloadDetail == rig.controller.downloadDetail == expected
    if state == "no-space":
        missing.assert_called_once_with(rig.port.second.size_bytes)
    else:
        missing.assert_not_called()


def test_download_detail_tracks_failed_entry_and_clears_on_retry(
    shared_downloads: SharedDownloadsRig,
) -> None:
    rig = shared_downloads
    rig.port.available_bytes = 234_000_001
    rig.controller.toggleModel(rig.port.second.id)
    settings_changes = QSignalSpy(rig.bridge.downloadDetailChanged)
    wizard_changes = QSignalSpy(rig.controller.downloadDetailChanged)
    details: list[tuple[str, str]] = []
    rig.bridge.downloadDetailChanged.connect(
        lambda: details.append((rig.bridge.downloadState, rig.bridge.downloadDetail))
    )
    finished = QSignalSpy(rig.downloads.queueFinished)
    rig.controller.startSelectedDownloads()
    assert finished.wait(1000)
    assert rig.bridge.downloadState == "no-space"
    assert rig.bridge.downloadDetail == rig.controller.downloadDetail == "нужно ещё 126 МБ"
    assert details == [("no-space", "нужно ещё 126 МБ")]
    rig.port.available_bytes = 1_000_000_000
    rig.port.releases[rig.port.second.id].set()
    rig.controller.retryModel(rig.port.second.id)
    assert rig.bridge.downloadDetail == rig.controller.downloadDetail == ""
    assert finished.wait(1000)
    assert rig.bridge.downloadState == "done"
    assert len(settings_changes) == len(wizard_changes) == 2


@pytest.mark.parametrize("missing", ["method", "os-error", "store-error"])
def test_download_detail_unavailable_is_empty(
    shared_downloads: SharedDownloadsRig, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    rig = shared_downloads
    if missing == "method":
        monkeypatch.setattr(rig.port, "disk_missing_bytes", None)
    else:
        error = OSError("SECRET") if missing == "os-error" else StoreError("broken-store")
        monkeypatch.setattr(rig.port, "disk_missing_bytes", Mock(side_effect=error))
    rig.downloads._set_download_state("no-space")
    assert rig.bridge.downloadDetail == rig.controller.downloadDetail == ""


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("none", ""),
        ("ok", ""),
        ("broken", "Файлы модели повреждены. Переустановите модель"),
        ("failed", "Не удалось загрузить модель"),
        ("no-space", "Не хватает места на диске"),
        ("catalog-unavailable", "Не удалось проверить список моделей"),
        ("downloading", ""),
        ("verifying", ""),
    ],
)
def test_active_model_message_all_states(
    shared_downloads: SharedDownloadsRig, state: str, expected: str
) -> None:
    rig = shared_downloads
    entry = rig.port.entry
    if state != "none":
        rig.port.records[(entry.id, entry.revision)] = "ok" if state == "ok" else "broken"
    if state == "catalog-unavailable":
        store = Mock(spec=ModelStore)
        store.current.return_value = entry
        rig.downloads._store = store
        rig.downloads._model = None
    elif state in {"downloading", "verifying", "failed", "no-space"}:
        rig.downloads._set_card(entry, state)
    assert rig.bridge.activeModelState == state
    assert rig.bridge.activeModelMessage == expected


@pytest.mark.parametrize(
    "blocker",
    [
        "none",
        "catalog",
        "entries",
        "recommendation",
        "installed",
        "broken",
        "queue",
        "recheck",
        "thread",
        "shutdown",
    ],
)
def test_can_install_and_recommended_slot_guards(
    shared_downloads: SharedDownloadsRig, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    rig = shared_downloads
    with monkeypatch.context() as patch:
        if blocker == "catalog":
            patch.setattr(rig.downloads, "_model", None)
        elif blocker == "entries":
            patch.setattr(rig.downloads, "_entries", ())
        elif blocker == "recommendation":
            patch.setattr(rig.downloads, "_entry", None)
        elif blocker in {"installed", "broken"}:
            # Запись без current тоже показывается как активная в настройках.
            rig.port.records[(rig.port.second.id, rig.port.second.revision)] = (
                "ok" if blocker == "installed" else "broken"
            )
        elif blocker in {"queue", "recheck", "shutdown"}:
            name = {
                "queue": "_queue_running",
                "recheck": "_rechecking",
                "shutdown": "_shutting_down",
            }
            patch.setattr(rig.downloads, name[blocker], True)
        elif blocker == "thread":
            patch.setattr(rig.downloads, "_model_thread", Mock())
        begin = Mock()
        patch.setattr(rig.downloads, "_begin_queue", begin)
        assert rig.bridge.canInstall is (blocker == "none")
        rig.bridge.installRecommendedModel()
        if blocker == "none":
            begin.assert_called_once_with((rig.port.entry,))
        else:
            begin.assert_not_called()


def test_install_recommended_uses_shared_queue_and_notifies(
    shared_downloads: SharedDownloadsRig,
) -> None:
    rig = shared_downloads
    rig.controller.toggleModel(rig.port.second.id)
    assert rig.bridge.canInstall
    states: list[bool] = []
    rig.bridge.activeModelStateChanged.connect(lambda: states.append(rig.bridge.canInstall))
    finished = QSignalSpy(rig.downloads.queueFinished)
    rig.bridge.installRecommendedModel()
    assert not rig.bridge.canInstall
    assert rig.downloads._queue_entries == (rig.port.entry,)
    assert rig.controller.downloadState == rig.bridge.downloadState == "downloading"
    rig.bridge.installRecommendedModel()
    rig.port.releases[rig.port.entry.id].set()
    assert finished.wait(1000)
    assert rig.port.visits == rig.port.installs == [rig.port.entry.id]
    assert not rig.bridge.canInstall
    assert states and all(value is False for value in states)
    assert rig.bridge.activeModelState == "ok"


def test_can_install_returns_after_failed_download(shared_downloads: SharedDownloadsRig) -> None:
    rig = shared_downloads
    rig.port.errors[rig.port.entry.id] = DownloadError("bad-path")
    states: list[bool] = []
    rig.bridge.activeModelStateChanged.connect(lambda: states.append(rig.bridge.canInstall))
    finished = QSignalSpy(rig.downloads.queueFinished)
    rig.bridge.installRecommendedModel()
    rig.port.releases[rig.port.entry.id].set()
    assert finished.wait(1000)
    assert rig.bridge.canInstall
    assert states[0] is False and states[-1] is True


def test_added_screen_members_have_readonly_qt_contract(
    shared_downloads: SharedDownloadsRig,
) -> None:
    rig = shared_downloads
    for target, properties, slots in (
        (
            rig.controller,
            {
                "freeSpaceText": ("QString", "freeSpaceTextChanged"),
                "downloadDetail": ("QString", "downloadDetailChanged"),
            },
            (b"openModelsFolder()",),
        ),
        (
            rig.bridge,
            {
                "downloadDetail": ("QString", "downloadDetailChanged"),
                "activeModelMessage": ("QString", "activeModelStateChanged"),
                "canInstall": ("bool", "activeModelStateChanged"),
            },
            (b"openModelsFolder()", b"installRecommendedModel()"),
        ),
    ):
        meta = target.metaObject()
        for name, (type_name, signal) in properties.items():
            prop = meta.property(meta.indexOfProperty(name))
            assert prop.isValid() and not prop.isWritable()
            assert prop.typeName() == type_name
            assert bytes(prop.notifySignal().name()).decode() == signal
        for slot in slots:
            assert meta.indexOfSlot(slot) >= 0


def test_free_space_text_handles_service_disk_usage_error(
    model_rig: ModelRig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, create = model_rig
    service = ModelService.__new__(ModelService)
    service._store = ModelStore(tmp_path / "models")
    monkeypatch.setattr(port, "free_bytes", service.free_bytes)
    usage = Mock(side_effect=OSError("SECRET /disk"))
    monkeypatch.setattr("astra_voice.ui.model_downloads.shutil.disk_usage", usage)
    controller = create()
    assert controller.freeSpaceText == ""
    usage.assert_called_once_with(tmp_path)
    assert not service._store.root.exists()


def test_settings_devices_default_until_refresh() -> None:
    """До refreshDevices() в «Общих» только системный по умолчанию; поставщик не зовётся."""
    provider = Mock(return_value=[AudioDevice(1, "alsa_input.mic", "Микрофон", False)])
    bridge = SettingsBridge(Settings(), save=Mock(), device_provider=provider)
    assert bridge.devices == [{"id": "", "name": "Системный по умолчанию"}]
    provider.assert_not_called()


def test_settings_refresh_devices_lists_microphones() -> None:
    """2026-09-22: в «Общих» нельзя было выбрать микрофон — список берётся из моста."""
    devices = [
        AudioDevice(1, "alsa_input.mic", "Микрофон гарнитуры", False),
        AudioDevice(2, "alsa_output.monitor", "Колонки", True),
    ]
    provider = Mock(return_value=devices)
    bridge = SettingsBridge(Settings(), save=Mock(), device_provider=provider)
    changed = Mock()
    bridge.devicesChanged.connect(changed)
    bridge.refreshDevices()
    assert bridge.devices == [
        {"id": "", "name": "Системный по умолчанию"},
        {"id": "alsa_input.mic", "name": "Микрофон гарнитуры"},
        {"id": "alsa_output.monitor", "name": "Колонки (звук системы)"},
    ]
    changed.assert_called_once()
    bridge.refreshDevices()
    changed.assert_called_once()  # без изменений сигнала нет


def test_settings_refresh_devices_failure_keeps_default(caplog: pytest.LogCaptureFixture) -> None:
    provider = Mock(side_effect=AudioError("audio-failed", "нет службы"))
    bridge = SettingsBridge(Settings(), save=Mock(), device_provider=provider)
    with caplog.at_level("WARNING", logger="astra_voice.ui"):
        bridge.refreshDevices()
    assert bridge.devices == [{"id": "", "name": "Системный по умолчанию"}]
    assert any("список микрофонов" in record.getMessage() for record in caplog.records)
