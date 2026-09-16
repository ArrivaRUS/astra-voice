"""S19: каждое свойство QML сохраняется сразу и откатывается при ошибке."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Literal, Protocol, cast
from unittest.mock import Mock, call

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent, QThread
from PyQt5.QtTest import QSignalSpy

from astra_voice.core import paths
from astra_voice.core import policy as policy_mod
from astra_voice.core import settings as settings_mod
from astra_voice.core.settings import Settings
from astra_voice.core.version import __version__
from astra_voice.models.catalog import Catalog, CatalogEntry, FileSpec, RevokedEntry
from astra_voice.models.downloader import DownloadError, Progress
from astra_voice.models.installer import InstallResult, ReasonCode
from astra_voice.models.store import ModelStore, StoreError
from astra_voice.net.http import NetworkError
from astra_voice.platform.hotkey import DEFAULT_CANDIDATES
from astra_voice.ui.bridges import (
    ModelPort,
    ModelService,
    OnboardingController,
    OnboardingHost,
    SettingsApply,
    SettingsBridge,
    _ModelJob,
    make_smoke_check,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module", autouse=True)
def qcore_app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


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

    runtime = Mock(spec=["apply_pill_enabled", "apply_hotkey", "apply_device"])
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


@pytest.fixture
def onboarding_rig() -> OnboardingRig:
    settings = Settings(extra={"onboarding_language_set": True})
    save = Mock()
    bridge = SettingsBridge(settings, save=save)
    host = Mock(spec=OnboardingHost)
    host.begin_capture.return_value = True
    host.probe.return_value = "ok"
    host.apply_hotkey.return_value = "ok"
    host.free_candidates.return_value = ["Ctrl+Alt+D"]
    controller = OnboardingController(bridge, settings=settings, host=host)
    return controller, bridge, settings, host, save


def test_onboarding_navigation_persists_every_transition(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = Settings(extra={"onboarding_language_set": True})
    bridge = SettingsBridge(settings, save=partial(settings_mod.save, path=path))
    controller = OnboardingController(bridge, settings=settings)
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
        restored = OnboardingController(SettingsBridge(loaded, save=Mock()), settings=loaded)
        assert restored.step == expected
    controller.next()
    controller.skip()
    assert controller.step == 5 and len(spy) == 6


@pytest.mark.parametrize("step", [None, True, False, "3", 3.0, 0, -1, 6, [], {}])
def test_onboarding_bad_saved_step(step: object) -> None:
    settings = Settings(extra={"onboarding_step": step, "onboarding_language_set": True})
    controller = OnboardingController(SettingsBridge(settings, save=Mock()), settings=settings)
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
    controller = OnboardingController(bridge, settings=settings)
    assert controller.language == bridge.language == expected
    assert settings.extra["onboarding_language_set"] is True
    set_qt_property(controller, "language", "en" if expected == "ru" else "ru")
    chosen = controller.language
    monkeypatch.setenv("LANG", "ru" if chosen == "en" else "en")
    restored = OnboardingController(bridge, settings=settings)
    assert restored.language == chosen


@pytest.mark.parametrize("marker", [True, False, None])
def test_onboarding_preserves_language_when_marker_exists(
    monkeypatch: pytest.MonkeyPatch, marker: bool | None
) -> None:
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    settings = Settings(language="ru", extra={"onboarding_language_set": marker})
    save = Mock()
    controller = OnboardingController(SettingsBridge(settings, save=save), settings=settings)
    assert controller.language == "ru"
    save.assert_not_called()


def test_onboarding_language_failure_does_not_mark_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANG", "en")
    settings = Settings(language="ru")
    bridge = SettingsBridge(settings, save=Mock(side_effect=OSError()))
    controller = OnboardingController(bridge, settings=settings)
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
    controller = OnboardingController(bridge, settings=settings)
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
        host.apply_hotkey.assert_called_once_with("Ctrl+Alt+D", "ptt")
        save.assert_called_once()
    else:
        assert settings.hotkey == "Ctrl+Space"
        host.apply_hotkey.assert_not_called()
        save.assert_not_called()
    controller.cancelCapture()
    assert controller.captureState == "idle" and controller.pendingCombo == ""
    assert host.end_capture.call_count == 2


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
    host.apply_hotkey.return_value = result
    controller.endCapture("Ctrl+Alt+D")
    assert settings.hotkey == "Ctrl+Space"
    save.assert_not_called()
    controller.keepCombo()
    assert settings.hotkey == bridge.hotkey == "Ctrl+Alt+D"
    host.apply_hotkey.assert_called_once_with("Ctrl+Alt+D", "ptt")
    assert controller.captureState == "success"


def test_onboarding_hotkey_save_failure_does_not_apply(onboarding_rig: OnboardingRig) -> None:
    controller, bridge, settings, host, save = onboarding_rig
    save.side_effect = OSError()
    controller.endCapture("Ctrl+Alt+D")
    assert settings.hotkey == "Ctrl+Space"
    host.apply_hotkey.assert_not_called()
    assert controller.captureState == "not-grabbed" and bridge.saveError


@pytest.mark.parametrize(
    "result", ["busy", "duplicate", "bad-combo", "not-grabbed", RuntimeError()]
)
def test_onboarding_apply_failure(
    onboarding_rig: OnboardingRig, result: str | RuntimeError
) -> None:
    controller, _, _, host, _ = onboarding_rig
    if isinstance(result, Exception):
        host.apply_hotkey.side_effect = result
    else:
        host.apply_hotkey.return_value = result
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
        assert host.mock_calls == [call.notify_ready(settings.hotkey), call.hide_window()]
        save.assert_called_once_with(settings)
        assert len(done_spy) == 1
        controller.finish()
        assert len(host.mock_calls) == 2
    else:
        assert "onboarding_done" not in settings.extra
        assert host.mock_calls == [] and len(done_spy) == 0
        save.assert_not_called()


def test_onboarding_finish_save_failure(onboarding_rig: OnboardingRig) -> None:
    controller, _, settings, host, save = onboarding_rig
    settings.extra["onboarding_model_ready"] = True
    save.side_effect = OSError()
    controller.finish()
    assert controller.done is False
    assert host.mock_calls == []


def test_onboarding_without_host() -> None:
    settings = Settings(extra={"onboarding_language_set": True, "onboarding_model_ready": True})
    controller = OnboardingController(SettingsBridge(settings, save=Mock()), settings=settings)
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
        controller.eventFilter(
            controller, QEvent(QEvent.Hide if action == "hide" else QEvent.Close)
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
        self.network = True
        self.space = True
        self.ram = True
        self.download_calls = 0
        self.sources: list[Path] = []
        self.result = InstallResult("ok")
        self.error: Exception | None = None
        self.block = False
        self.download_thread: int | None = None

    def recommended(self) -> CatalogEntry:
        return self.entry

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

    def ram_ok(self, min_ram_mb: int) -> bool:
        assert min_ram_mb == self.entry.min_ram_mb
        return self.ram

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
        self, model: ModelPort | None = ..., **settings_extra: object
    ) -> OnboardingController: ...


ModelRig = tuple[FakeModelPort, ModelFactory]


@pytest.fixture
def model_rig() -> Iterator[ModelRig]:
    port = FakeModelPort()
    controllers: list[OnboardingController] = []

    def create(model: ModelPort | None = port, **settings_extra: object) -> OnboardingController:
        settings = Settings(extra={"onboarding_language_set": True, **settings_extra})
        controller = OnboardingController(
            SettingsBridge(settings, save=Mock()),
            settings=settings,
            model=model,
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


def test_model_absent_and_settings_fallback(model_rig: ModelRig) -> None:
    port, create = model_rig
    controller = create(None, onboarding_model_ready=True)
    assert controller.modelState == "absent"
    assert controller.canFinish
    assert (controller.modelName, controller.modelHost, controller.modelSizeBytes) == ("", "", 0)
    controller.download()
    controller.installFromPath("/fake/model")
    assert controller._model_thread is None
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
    assert controller._model_thread is None
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
    assert controller._model_thread is None
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
        "progress",
        "speed",
        "eta",
        "modelMessage",
    ):
        prop = meta.property(meta.indexOfProperty(name))
        assert prop.hasNotifySignal()
        assert not prop.isWritable()


@pytest.mark.parametrize("low_ram", [False, True])
def test_model_download_signals_progress_and_thread_completion(
    model_rig: ModelRig, low_ram: bool
) -> None:
    port, create = model_rig
    port.ram = not low_ram
    controller = create()
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
    assert (0.5, "5,2 МБ/с", "осталось ~25 с") in values
    assert all(len(spy) > 0 for spy in spies)
    assert controller.progress == 1
    assert controller.speed == controller.eta == ""
    assert controller.canFinish
    assert controller._model_thread is None
    assert controller._model_job is None
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
    assert controller._model_thread is None
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
    assert controller._model_thread is None


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
    assert controller._model_thread is None


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


@pytest.mark.parametrize("reason_code", ["selfcheck", "checksum", "layout"])
@pytest.mark.parametrize("local", [False, True])
def test_model_install_failure_message_uses_reason_code(
    model_rig: ModelRig, reason_code: ReasonCode, local: bool
) -> None:
    port, create = model_rig
    port.result = InstallResult("broken", "SECRET /path/service", reason_code=reason_code)
    controller = create()
    done = QSignalSpy(controller.canFinishChanged)
    if local:
        controller.installFromPath("/fake/model")
    else:
        controller.download()
    assert done.wait(1000)
    assert controller.modelState == "broken"
    assert controller.modelMessage == (
        "Распознавание на этом компьютере не работает. Обратитесь к администратору"
        if reason_code == "selfcheck"
        else "Модель не прошла проверку. Попробуйте скачать или установить её заново."
    )


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
    assert not controller._model_cancel.is_set()
    assert controller._model_thread is None


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


def test_model_shutdown_during_download_needs_no_gui_polling(model_rig: ModelRig) -> None:
    port, create = model_rig
    port.block = True
    controller = create()
    controller.progressChanged.connect(controller.shutdown)
    done = QSignalSpy(controller.canFinishChanged)
    controller.download()
    assert done.wait(1000)
    assert controller.modelState == "cancelled"
    assert controller._model_thread is None
    controller.shutdown()
    controller.download()
    assert port.download_calls == 1


def test_model_shutdown_times_out_and_logs_warning(
    model_rig: ModelRig, caplog: pytest.LogCaptureFixture
) -> None:
    _, create = model_rig
    controller = create()
    release, started = threading.Event(), threading.Event()

    class StuckThread(QThread):
        def run(self) -> None:
            started.set()
            release.wait(10)

    thread = StuckThread(controller)
    controller._model_thread = thread
    thread.start()
    try:
        assert started.wait(1)
        before = time.monotonic()
        with caplog.at_level(logging.WARNING):
            controller.shutdown()
        elapsed = time.monotonic() - before
        assert 4.9 <= elapsed < 5.5
        assert controller._model_cancel.is_set()
        assert thread.isRunning()
        assert thread.parent() is None
        assert "не завершилась за 5 секунд" in caplog.text
        assert "выход из приложения продолжается" in caplog.text
    finally:
        release.set()
        assert thread.wait(1000)
        controller._model_thread = None


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
        assert progressed[0] == [0.5, 5_200_000.0, 25.0]


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
    import astra_voice.ui.bridges as bridges

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
    factories["load_builtin"].assert_called_once_with(factories["Verifier"].return_value)
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
    from astra_voice.ui import bridges

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
    monkeypatch.setattr(bridges, "load_builtin", Mock(return_value=catalog))
    monkeypatch.setattr(bridges, "ModelStore", Mock(return_value=store))
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
        (0.3, 1_234_567, 25.1, (0.3, "1,2 МБ/с", "осталось ~26 с")),
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
    assert progress[0] == [0.0, 0.0, -1.0]
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
    from astra_voice.ui import bridges

    port, create = model_rig
    started, stopped = threading.Event(), threading.Event()
    store = Mock()
    store.records.return_value = []
    gate = Mock()
    gate.allowed.return_value = (True, "")
    catalog = Mock(entries=[port.entry])
    catalog.is_revoked.return_value = False
    monkeypatch.setattr(bridges, "load_builtin", Mock(return_value=catalog))
    monkeypatch.setattr(bridges, "Verifier", Mock())
    monkeypatch.setattr(bridges, "ModelStore", Mock(return_value=store))
    monkeypatch.setattr(bridges, "NetworkGate", Mock(return_value=gate))
    monkeypatch.setattr(bridges, "HttpClient", Mock())
    monkeypatch.setattr(bridges, "Downloader", Mock())

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
    monkeypatch.setattr(bridges, "SmokeRunner", Mock(return_value=runner))

    def installer_factory(
        store: object, smoke: Callable[..., object], catalog: Catalog | None = None
    ) -> Mock:
        def install(*args: object) -> InstallResult:
            smoke(Path("/fake/model"), port.entry)
            return InstallResult("broken", reason_code="selfcheck")

        return Mock(
            install_from_path=Mock(side_effect=install),
            install_from_staging=Mock(side_effect=install),
        )

    monkeypatch.setattr(bridges, "Installer", installer_factory)
    service = ModelService(Settings(), policy_mod.Policy())
    controller = create(service)
    # Проверка создаётся до привязки Event; попытка должна видеть новый Event.
    service.set_cancel(controller._model_cancel)
    controller._model_cancel.set()
    done = QSignalSpy(controller.canFinishChanged)
    if local:
        controller.installFromPath("/fake/model")
    else:
        controller.download()
    assert started.wait(1)
    controller.shutdown()
    assert stopped.is_set()
    assert controller._model_thread is not None
    assert not controller._model_thread.isRunning()
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
    from astra_voice.ui import bridges

    rig = Rig(monkeypatch, tmp_path)
    model = FakeModelPort()
    factory = Mock(return_value=model)
    if service_fails:
        factory.side_effect = RuntimeError("SECRET /catalog/path")
    monkeypatch.setattr(bridges, "ModelService", factory)
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
    assert controller._model is (None if service_fails else model)
    assert controller.modelState == ("absent" if service_fails else "downloadable")
    shutdown_call.assert_called_once_with()
    assert rig.calls.mock_calls.index(call.onboarding_shutdown()) < rig.calls.mock_calls.index(
        call.shutdown()
    )
    rig.cleanup.assert_called_once_with(rig.server, rig.lock)
    if service_fails:
        assert "каталог моделей" in caplog.text
        assert "SECRET" not in caplog.text
