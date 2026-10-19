"""Контракт M7: обновление активной модели, фон, отмена и строка уведомлений.

Подставляется только порт службы моделей. Поток, канал задания, события Qt и
оба публичных моста настоящие; загрузчик/установщик проверяются отдельно.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent

from astra_voice.core import settings as settings_mod
from astra_voice.core.policy import Policy
from astra_voice.models.catalog import Catalog
from astra_voice.models.store import StoreError
from astra_voice.net.gate import NetworkGate
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.model_downloads import ModelDownloads
from astra_voice.ui.updates_bridge import UpdatesBridge
from astra_voice.updates.model_updates import (
    FAILED_MESSAGE,
    REVERT_FAILED_MESSAGE,
    ModelUpdateNotice,
    RevisionUpdateResult,
)
from helpers.qt_app import get_qapplication
from helpers.scripted_model_port import FIRST, SECOND, ScriptedModelPort, pump_until

pytestmark = pytest.mark.unit


class RevisionPort(ScriptedModelPort):
    """Управляемый результат обновления без сети и без подмены потоков."""

    def __init__(self) -> None:
        super().__init__()
        self.catalog_unavailable = False
        self.records = {(FIRST.id, "r0"): "ok", (SECOND.id, "r0"): "ok"}
        self.current = (FIRST.id, "r0")
        self.previous: str | None = None
        self.revision_gate = threading.Event()
        self.revision_started = threading.Event()
        self.cancel_seen = threading.Event()
        self.revision_calls: list[tuple[str, str]] = []
        self.next_failure = ""
        self.next_refusal = ""
        self.revert_failure = ""
        self.activation_started = threading.Event()
        self.activation_gate = threading.Event()
        self.activation_gate.set()
        self.current_writes: list[tuple[str, str, str | None]] = []

    def set_current(
        self, model_id: str, revision: str, *, previous_revision: str | None = None
    ) -> None:
        self.current_writes.append((model_id, revision, previous_revision))
        self.current = (model_id, revision)
        self.previous = previous_revision

    def restore_current(
        self, model_id: str, revision: str, *, previous_revision: str | None
    ) -> None:
        self.set_current(model_id, revision, previous_revision=previous_revision)

    def previous_revision(self) -> str | None:
        return self.previous

    def revision_updater(self, *, on_state: Callable[[str, str], None]) -> ScriptedUpdater:
        return ScriptedUpdater(self, on_state)

    def accept_catalog(self, catalog: Catalog) -> None:
        self.catalog = catalog.entries
        self.catalog_unavailable = False

    def open_gates(self) -> None:
        super().open_gates()
        self.revision_gate.set()
        self.activation_gate.set()


class CatalogUnavailablePort:
    """Порт восстановления: старые данные не доступны до принятия каталога."""

    def __init__(self, port: RevisionPort) -> None:
        self._port = port
        self.catalog_unavailable = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._port, name)

    def recommended(self) -> Any | None:
        return None if self.catalog_unavailable else self._port.recommended()

    def entries(self) -> tuple[Any, ...]:
        return () if self.catalog_unavailable else self._port.entries()

    def accept_catalog(self, catalog: Catalog) -> None:
        self._port.accept_catalog(catalog)
        self.catalog_unavailable = False


class ScriptedUpdater:
    def __init__(self, port: RevisionPort, on_state: Callable[[str, str], None]) -> None:
        self.port, self.on_state = port, on_state

    def apply(self, model_id: str, *, cancel: threading.Event) -> RevisionUpdateResult:
        port = self.port
        port.worker_threads.add(threading.get_ident())
        port.revision_calls.append(("update", model_id))
        self.on_state(model_id, "updating")
        port.revision_started.set()
        deadline = time.monotonic() + 5
        while not port.revision_gate.wait(0.005):
            if cancel.is_set():
                port.cancel_seen.set()
                return RevisionUpdateResult(
                    "update-failed", model_id, "r0", FAILED_MESSAGE, "cancelled"
                )
            if time.monotonic() > deadline:
                raise AssertionError("тест не открыл ворота обновления")
        if reason := port.next_refusal:
            return RevisionUpdateResult("refused", model_id, "r0", reason_code=reason)
        if reason := port.next_failure:
            port.next_failure = ""
            self.on_state(model_id, "update-failed")
            return RevisionUpdateResult("update-failed", model_id, "r0", FAILED_MESSAGE, reason)
        self.on_state(model_id, "activating")
        port.activation_started.set()
        assert port.activation_gate.wait(5)
        entry = next(entry for entry in port.catalog if entry.id == model_id)
        assert port.current is not None
        port.set_current(model_id, entry.revision, previous_revision=port.current[1])
        assert port.current is not None
        port.records[port.current] = "ok"
        return RevisionUpdateResult("updated", model_id, entry.revision)

    def revert_previous(self, model_id: str) -> RevisionUpdateResult:
        port = self.port
        port.worker_threads.add(threading.get_ident())
        port.revision_calls.append(("revert", model_id))
        if reason := port.revert_failure:
            if reason == "revert":
                return RevisionUpdateResult(
                    "update-failed", model_id, "r0", REVERT_FAILED_MESSAGE, reason
                )
            return RevisionUpdateResult("refused", model_id, "r0", reason_code=reason)
        assert port.previous is not None
        port.set_current(model_id, port.previous)
        assert port.current is not None
        return RevisionUpdateResult("updated", model_id, port.current[1])


def make_catalog(port: RevisionPort) -> Catalog:
    return Catalog(serial=2, trust_epoch=1, entries=port.catalog, revoked=())


@dataclass
class RevisionRig:
    port: RevisionPort
    downloads: ModelDownloads
    bridge: SettingsBridge
    signal_threads: set[int]

    def card(self, model_id: str = FIRST.id) -> dict[str, Any]:
        return next(row for row in self.bridge.models if row["id"] == model_id)

    def idle(self) -> bool:
        return self.downloads._model_thread is None


@pytest.fixture(scope="module", autouse=True)
def qapp() -> QCoreApplication:
    return get_qapplication()


@pytest.fixture
def rig() -> Iterator[RevisionRig]:
    port = RevisionPort()
    downloads = ModelDownloads(port)
    bridge = SettingsBridge(settings_mod.from_dict({}), save=Mock(), downloads=downloads)
    rig = RevisionRig(port, downloads, bridge, set())
    bridge.modelsChanged.connect(lambda: rig.signal_threads.add(threading.get_ident()))
    try:
        yield rig
    finally:
        port.open_gates()
        downloads.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_revision_update_state_is_offered_only_for_active_model(rig: RevisionRig) -> None:
    assert rig.card()["state"] == "update-available"
    assert rig.card()["updateAvailable"] is True
    assert rig.card(SECOND.id)["updateAvailable"] is True
    assert rig.card(SECOND.id)["state"] == "installed"
    assert rig.card(SECOND.id)["badge"] == "installed"
    for bridge in (rig.downloads, rig.bridge):
        for signature in (
            "updateModel(QString)",
            "cancelModelUpdate()",
            "revertModelPrevious(QString)",
        ):
            assert bridge.metaObject().indexOfSlot(signature.encode()) >= 0
    rig.bridge.revertModelPrevious(SECOND.id)
    rig.bridge.updateModel("unknown-model")
    assert rig.idle()
    assert rig.port.revision_calls == []


def test_active_update_and_inactive_update_use_different_paths(rig: RevisionRig) -> None:
    assert rig.card()["state"] == "update-available"
    assert rig.card()["badge"] == "active"
    assert rig.card(SECOND.id)["state"] == "installed"
    assert rig.card(SECOND.id)["badge"] == "installed"
    assert rig.card(SECOND.id)["updateAvailable"] is True

    rig.bridge.updateModel(FIRST.id)
    pump_until(rig.port.revision_started.is_set, "обновление активной модели")
    assert rig.port.revision_calls == [("update", FIRST.id)]
    assert rig.downloads._queue == []
    assert rig.downloads._active_entry is None
    assert rig.port.download_calls == []
    rig.port.revision_gate.set()
    pump_until(rig.idle, "завершение обновления активной модели")

    rig.bridge.updateModel(SECOND.id)
    pump_until(rig.port.downloading.is_set, "обычная очередь для неактивной модели")
    assert rig.downloads._queue_running is True
    assert rig.downloads._active_entry == SECOND
    assert rig.port.download_calls == [SECOND.id]
    assert rig.port.revision_calls == [("update", FIRST.id)]
    assert rig.port.current == (FIRST.id, FIRST.revision)
    assert rig.card(SECOND.id)["state"] == "downloading"
    assert rig.card(SECOND.id)["badge"] == "installed"
    rig.port.open_gates()
    pump_until(rig.idle, "завершение обновления неактивной модели")
    assert rig.port.records[(SECOND.id, SECOND.revision)] == "ok"
    assert rig.port.current == (FIRST.id, FIRST.revision)


def test_update_success_refreshes_revision_previous_and_gui_signals(rig: RevisionRig) -> None:
    revisions: list[str] = []
    rig.downloads.modelRevisionChanged.connect(revisions.append)
    assert rig.card()["previousAvailable"] is False
    rig.bridge.updateModel(FIRST.id)
    assert rig.card()["state"] == "updating"
    assert rig.downloads.active_state() == "ok"
    pump_until(rig.port.revision_started.is_set, "рабочий поток обновления")
    # Повторное нажатие не запускает вторую установку.
    rig.bridge.updateModel(FIRST.id)
    assert rig.port.revision_calls == [("update", FIRST.id)]
    assert rig.port.current == (FIRST.id, "r0")
    rig.port.revision_gate.set()
    pump_until(rig.idle, "завершение обновления")
    assert rig.port.current == (FIRST.id, FIRST.revision)
    assert rig.card()["state"] == "ok"
    assert rig.card()["updateAvailable"] is False
    assert rig.card()["previousAvailable"] is True
    assert rig.card()["message"] == ""
    assert revisions == [FIRST.id]
    assert rig.signal_threads == {threading.get_ident()}
    assert rig.port.worker_threads and threading.get_ident() not in rig.port.worker_threads


def test_failed_update_keeps_current_and_can_be_retried(rig: RevisionRig) -> None:
    rig.port.next_failure = "selfcheck"
    rig.port.revision_gate.set()
    rig.bridge.updateModel(FIRST.id)
    pump_until(rig.idle, "отказ проверки ревизии")
    assert rig.port.current == (FIRST.id, "r0")
    assert rig.card()["state"] == "update-failed"
    assert rig.card()["message"] == FAILED_MESSAGE
    assert rig.card()["reason"] == "Модель не прошла самопроверку на этом компьютере"
    assert rig.card()["updateAvailable"] is True
    assert rig.downloads.active_state() == "ok"
    rig.bridge.updateModel(FIRST.id)
    pump_until(rig.idle, "повтор обновления")
    assert rig.card()["state"] == "ok"
    assert rig.card()["reason"] == ""


def test_cancel_reaches_real_worker_event_and_keeps_current(rig: RevisionRig) -> None:
    rig.bridge.updateModel(FIRST.id)
    pump_until(rig.port.revision_started.is_set, "рабочий поток до отмены")
    rig.bridge.cancelModelUpdate()
    pump_until(rig.idle, "выход отменённого задания")
    assert rig.port.cancel_seen.is_set()
    assert rig.port.current == (FIRST.id, "r0")
    assert rig.card()["state"] == "update-failed"
    assert rig.card()["reason"] == "Обновление отменено"
    rig.bridge.cancelModelUpdate()  # отсутствие задания допустимо


def test_revert_is_background_and_previous_only_on_active(rig: RevisionRig) -> None:
    rig.port.previous = "r-1"
    rig.port.records[(FIRST.id, "r-1")] = "ok"
    assert rig.card()["previousAvailable"] is True
    assert rig.card(SECOND.id)["previousAvailable"] is False
    revisions: list[str] = []
    rig.downloads.modelRevisionChanged.connect(revisions.append)
    rig.bridge.revertModelPrevious(FIRST.id)
    pump_until(rig.idle, "откат ревизии")
    assert rig.port.revision_calls == [("revert", FIRST.id)]
    assert rig.port.current == (FIRST.id, "r-1")
    assert rig.card()["state"] == "ok"
    assert rig.card()["previousAvailable"] is False
    assert revisions == [FIRST.id]
    assert rig.signal_threads == {threading.get_ident()}
    assert threading.get_ident() not in rig.port.worker_threads


@pytest.mark.parametrize(
    ("reason", "message", "details"),
    [
        (
            "revoked",
            "Предыдущая версия отозвана издателем",
            "Предыдущая версия отозвана издателем",
        ),
        ("no-previous", "Предыдущей версии нет", "Предыдущей версии нет"),
        (
            "not-installed",
            "Обновлять можно только рабочую модель",
            "Обновлять можно только рабочую модель",
        ),
        ("no-update", "Новая версия модели недоступна", "Новая версия модели недоступна"),
        ("revert", REVERT_FAILED_MESSAGE, "Не удалось вернуть предыдущую версию модели"),
    ],
)
def test_revert_refusal_is_visible_without_changing_current(
    rig: RevisionRig, reason: str, message: str, details: str
) -> None:
    rig.port.previous = "r-1"
    rig.port.revert_failure = reason
    rig.bridge.revertModelPrevious(FIRST.id)
    pump_until(rig.idle, "отказ возврата")
    assert rig.port.current == (FIRST.id, "r0")
    assert rig.card()["state"] == "update-failed"
    assert rig.card()["message"] == message
    assert rig.card()["reason"] == details


@pytest.mark.parametrize(
    ("code", "details"),
    [
        ("checksum", "Файлы скачались с ошибкой"),
        ("selfcheck", "Модель не прошла самопроверку на этом компьютере"),
        ("download", "Не удалось скачать"),
        ("cancelled", "Обновление отменено"),
        ("revoked", "Эта версия отозвана авторами каталога"),
        ("not-installed", "Обновлять можно только рабочую модель"),
        ("no-update", "Новая версия модели недоступна"),
        ("no-previous", "Предыдущей версии нет"),
        ("revert", "Не удалось вернуть предыдущую версию модели"),
        ("unknown", "Не удалось скачать"),
    ],
)
def test_update_failure_details_are_translated(
    rig: RevisionRig, caplog: pytest.LogCaptureFixture, code: str, details: str
) -> None:
    rig.port.next_failure = code
    rig.port.revision_gate.set()
    with caplog.at_level("INFO", logger="astra_voice.ui.model_downloads"):
        rig.bridge.updateModel(FIRST.id)
        pump_until(rig.idle, "русская причина отказа")
    assert rig.card()["message"] == FAILED_MESSAGE
    assert rig.card()["reason"] == details
    assert ("download" if code == "unknown" else code) in caplog.text


def test_new_and_removed_come_from_catalog_snapshot(rig: RevisionRig) -> None:
    rig.port.catalog = (FIRST, replace(SECOND, revision="r0", removed_from_catalog=True))
    rig.downloads.accept_catalog(make_catalog(rig.port), frozenset({FIRST.id}))
    assert rig.card()["isNew"] is True
    removed = rig.card(SECOND.id)
    assert removed["isNew"] is False
    assert removed["state"] == "removed-from-catalog"
    assert removed["updateAvailable"] is False
    assert "Снята с каталога" in removed["hint"]


def test_model_update_notice_is_plain_data_and_clear_is_targeted() -> None:
    bridge = UpdatesBridge()
    notice = ModelUpdateNotice(FIRST.id, "<b>Модель</b>", "r0", "r1")
    bridge.set_model_update(notice)
    assert bridge.modelUpdateText == "Обновление модели: <b>Модель</b>"
    bridge.clear_model_update(SECOND.id)
    assert bridge.modelUpdateText
    bridge.clear_model_update(FIRST.id)
    assert bridge.modelUpdateText == ""


def test_catalog_unavailable_is_not_a_missing_model_and_acceptance_notifies_gui() -> None:
    port = RevisionPort()
    accepted = make_catalog(port)
    downloads = ModelDownloads(CatalogUnavailablePort(port))
    bridge = SettingsBridge(settings_mod.from_dict({}), downloads=downloads, save=Mock())
    notifications: list[bool] = []
    bridge.modelsChanged.connect(lambda: notifications.append(bridge.catalogUnavailable))
    try:
        for owner in (downloads, bridge):
            prop = owner.metaObject().property(
                owner.metaObject().indexOfProperty("catalogUnavailable")
            )
            assert prop.typeName() == "bool"
            assert bytes(prop.notifySignal().name()) == b"modelsChanged"
            assert owner.catalogUnavailable is True
        assert bridge.models == []
        downloads.accept_catalog(accepted, frozenset())
        assert bridge.catalogUnavailable is False
        assert downloads.catalogUnavailable is False
        assert {row["id"] for row in bridge.models} == {FIRST.id, SECOND.id}
        assert notifications == [False]
    finally:
        port.open_gates()
        downloads.shutdown()


@pytest.mark.parametrize(
    ("condition", "models_allowed", "app_allowed"),
    [
        ("enabled", True, True),
        ("app-toggle-off", True, True),
        ("policy-app", True, False),
        ("model-toggle-off", False, True),
        ("offline", False, False),
        ("policy-offline", False, False),
        ("policy-models", False, True),
        ("policy-updates", False, True),
        ("no-checker", False, False),
    ],
)
def test_model_check_capability_uses_model_gate_independently_of_manual_app_check(
    monkeypatch: pytest.MonkeyPatch, condition: str, models_allowed: bool, app_allowed: bool
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    settings = settings_mod.from_dict({"check_model_updates": True})
    policy = Policy()
    if condition == "app-toggle-off":
        settings.check_app_updates = False
    elif condition == "policy-app":
        settings.check_app_updates = False
        policy = Policy(locked_keys=frozenset({"check_app_updates"}))
    elif condition == "model-toggle-off":
        settings.check_model_updates = False
    elif condition == "offline":
        settings.offline = True
    elif condition == "policy-offline":
        policy = Policy(values={"offline": True})
    elif condition == "policy-models":
        policy = Policy(values={"check_model_updates": False})
    elif condition == "policy-updates":
        policy = Policy(values={"updates": "offline"})
    checker = None if condition == "no-checker" else Mock(spec=["check_now", "check_models_now"])
    bridge = UpdatesBridge(checker, refusal=NetworkGate(settings, policy).refusal)
    prop = bridge.metaObject().property(bridge.metaObject().indexOfProperty("canCheckModelsNow"))
    assert prop.typeName() == "bool"
    assert bytes(prop.notifySignal().name()) == b"networkChanged"
    assert bridge.canCheckModelsNow is models_allowed
    assert bridge.canCheckNow is app_allowed
    assert bridge.metaObject().indexOfSlot(b"checkModelsNow()") >= 0
    bridge.checkModelsNow()
    if checker is not None:
        if models_allowed:
            checker.check_models_now.assert_called_once_with()
        else:
            checker.check_models_now.assert_not_called()
        checker.check_now.assert_not_called()
        checker.reset_mock()
    bridge.checkNow()
    if checker is not None:
        if app_allowed:
            checker.check_now.assert_called_once_with()
        else:
            checker.check_now.assert_not_called()
        checker.check_models_now.assert_not_called()


def test_model_check_capability_notifies_when_only_model_toggle_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    settings = settings_mod.from_dict({"check_model_updates": True})
    bridge = UpdatesBridge(Mock(), refusal=NetworkGate(settings, Policy()).refusal)
    notifications: list[bool] = []
    bridge.networkChanged.connect(lambda: notifications.append(bridge.canCheckModelsNow))
    assert bridge.canCheckModelsNow is True
    settings.check_model_updates = False
    bridge.refresh()
    assert bridge.canCheckModelsNow is False
    assert bridge.canCheckNow is True
    settings.check_model_updates = True
    bridge.refresh()
    assert bridge.canCheckModelsNow is True
    assert notifications == [False, True]


def test_checker_notice_is_delivered_through_gui_queue() -> None:
    from astra_voice.app import _bind_updates_bridge, _GuiCalls
    from astra_voice.core.policy import Policy

    checker = Mock()
    gui_calls = _GuiCalls()
    bridge = _bind_updates_bridge(checker, settings_mod.from_dict({}), Policy(), gui_calls, None)
    notice = ModelUpdateNotice(FIRST.id, FIRST.name, "r0", "r1")
    worker = threading.Thread(target=lambda: checker.on_model_update(notice))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert bridge.modelUpdateText == "", "рабочий поток не должен менять QObject"
    pump_until(lambda: bool(bridge.modelUpdateText), "доставка уведомления в GUI")
    assert bridge.modelUpdateText == "Обновление модели: " + FIRST.name


def test_app_checker_factory_connects_model_notices_to_gui(tmp_path: Path) -> None:
    from astra_voice.app import _bind_updates_bridge, _GuiCalls
    from astra_voice.core.policy import Policy
    from astra_voice.models.store import ModelStore
    from astra_voice.updates.checker import create_app_checker

    settings = settings_mod.from_dict({})
    checker = create_app_checker(
        settings,
        Policy(),
        model_store=ModelStore(tmp_path / "models"),
        catalog=make_catalog(RevisionPort()),
    )
    gui_calls = _GuiCalls()
    bridge = _bind_updates_bridge(checker, settings, Policy(), gui_calls, None)
    model_checker = checker._model_checker
    assert model_checker is not None
    notify = model_checker._notify
    assert notify is not None, "create_app_checker должен подключать notify"
    notice = ModelUpdateNotice(FIRST.id, FIRST.name, "r0", "r1")
    worker = threading.Thread(target=notify, args=(notice,))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert bridge.modelUpdateText == ""
    pump_until(lambda: bool(bridge.modelUpdateText), "фабрика → notify → GUI")
    assert bridge.modelUpdateText == "Обновление модели: " + FIRST.name


@pytest.mark.parametrize("revert", [False, True])
@pytest.mark.parametrize("outcome", ["ok", "failed", "raises", "too-slow"])
def test_revision_switches_once_and_restores_both_pointers_on_failure(
    rig: RevisionRig, revert: bool, outcome: str
) -> None:
    switcher = Mock()
    switcher.can_switch_without_pause.return_value = True
    switcher.mem_available_mb.return_value = None
    rig.downloads._switcher = switcher
    switcher.on_switch_finished = rig.downloads._switch_finished
    rig.port.previous = "r-1"
    rig.port.records[(FIRST.id, "r-1")] = "ok"
    rig.port.revision_gate.set()
    if outcome == "raises":
        switcher.switch_model.side_effect = RuntimeError("switch failed")
    revisions: list[str] = []
    rig.downloads.modelRevisionChanged.connect(revisions.append)

    if revert:
        rig.bridge.revertModelPrevious(FIRST.id)
    else:
        rig.bridge.updateModel(FIRST.id)
    pump_until(rig.idle, "установка ревизии перед переключением распознавателя")
    switcher.switch_model.assert_called_once_with(min_ram_mb=FIRST.min_ram_mb, pause=False)
    if outcome != "raises":
        assert rig.card()["state"] == "switching"
        assert len(rig.port.current_writes) == 1, "UI не должен заново записывать current"
        switcher.on_switch_finished(outcome)
    if outcome == "ok":
        assert rig.port.current == (FIRST.id, "r-1" if revert else FIRST.revision)
        assert rig.port.previous == (None if revert else "r0")
        assert rig.card()["state"] == "ok"
        assert revisions == [FIRST.id]
        assert len(rig.port.current_writes) == 1
    else:
        assert rig.port.current == (FIRST.id, "r0")
        assert rig.port.previous == "r-1"
        assert rig.card()["state"] == "update-failed"
        assert rig.card()["message"]
        assert rig.card()["reason"]
        assert revisions == []
        assert len(rig.port.current_writes) == 2


@pytest.mark.parametrize("revert", [False, True])
def test_revision_requests_pause_before_activation(rig: RevisionRig, revert: bool) -> None:
    switcher = Mock()
    switcher.can_switch_without_pause.return_value = False
    rig.downloads._switcher = switcher
    switcher.on_switch_finished = rig.downloads._switch_finished
    rig.port.previous = "r-1"
    rig.port.records[(FIRST.id, "r-1")] = "ok"
    rig.port.revision_gate.set()
    if revert:
        rig.bridge.revertModelPrevious(FIRST.id)
    else:
        rig.bridge.updateModel(FIRST.id)
    assert rig.port.revision_calls == []
    assert rig.port.current == (FIRST.id, "r0")
    assert rig.card()["canSwitchWithPause"] is True
    assert rig.card()["hint"]
    rig.bridge.switchModelWithPause(FIRST.id)
    pump_until(rig.idle, "переключение с согласованной паузой")
    switcher.switch_model.assert_called_once_with(min_ram_mb=FIRST.min_ram_mb, pause=True)
    switcher.on_switch_finished("ok")
    assert rig.card()["state"] == "ok"


def test_activation_boundary_hides_cancel_and_ignores_late_cancel(rig: RevisionRig) -> None:
    rig.port.activation_gate.clear()
    rig.port.revision_gate.set()
    rig.bridge.updateModel(FIRST.id)
    pump_until(rig.port.activation_started.is_set, "граница активации")
    assert rig.card()["canCancel"] is False
    rig.bridge.cancelModelUpdate()
    assert not rig.downloads._model_cancel.is_set()
    rig.port.activation_gate.set()
    pump_until(rig.idle, "активация после границы отмены")
    assert rig.port.current == (FIRST.id, FIRST.revision)


@pytest.mark.parametrize("queue", [False, True])
def test_catalog_is_accepted_immediately_but_cards_wait_for_job(
    rig: RevisionRig, queue: bool
) -> None:
    if queue:
        rig.bridge.updateModel(SECOND.id)
        pump_until(rig.port.downloading.is_set, "очередь загрузки")
    else:
        rig.bridge.updateModel(FIRST.id)
        pump_until(rig.port.revision_started.is_set, "обновление")
    replacement = replace(SECOND, name="Новое имя из каталога")
    catalog = Catalog(serial=3, trust_epoch=1, entries=(FIRST, replacement), revoked=())
    rig.downloads.accept_catalog(catalog, frozenset({SECOND.id}))
    assert rig.port.catalog == catalog.entries
    assert rig.card(SECOND.id)["name"] == SECOND.name
    assert rig.card(SECOND.id)["isNew"] is False
    assert rig.downloads._pending_catalog is not None
    rig.port.open_gates()
    pump_until(rig.idle, "завершение задания с ожидающим каталогом")
    assert rig.card(SECOND.id)["name"] == replacement.name
    assert rig.card(SECOND.id)["isNew"] is True
    assert rig.downloads._pending_catalog is None


@pytest.mark.parametrize("revert", [False, True])
@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("not-installed", "Обновлять можно только рабочую модель"),
        ("no-update", "Новая версия модели недоступна"),
        ("no-previous", "Предыдущей версии нет"),
        ("revoked", ""),
    ],
)
def test_refusal_text_depends_on_operation_without_download_failure(
    rig: RevisionRig, revert: bool, code: str, message: str
) -> None:
    if code == "revoked":
        message = (
            "Предыдущая версия отозвана издателем"
            if revert
            else "Эта версия отозвана авторами каталога"
        )
    if revert:
        rig.port.revert_failure = code
        rig.bridge.revertModelPrevious(FIRST.id)
    else:
        rig.port.next_refusal = code
        rig.port.revision_gate.set()
        rig.bridge.updateModel(FIRST.id)
    pump_until(rig.idle, "отказ операции ревизии")
    assert rig.card()["message"] == message
    assert rig.card()["reason"] == message
    assert "Не удалось скачать" not in str(rig.card())
    assert rig.port.current == (FIRST.id, "r0")


_STORE_WARNING = "Не удалось прочитать или изменить состояние моделей"


def _store_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == _STORE_WARNING]


@pytest.mark.parametrize("revert", [False, True])
@pytest.mark.parametrize("error_type", [OSError, StoreError])
@pytest.mark.parametrize("failed_read", ["current_ids", "previous_revision"])
def test_ib40c_revision_store_read_error_does_not_crash(
    rig: RevisionRig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    revert: bool,
    error_type: type[Exception],
    failed_read: str,
) -> None:
    rig.port.previous = "r-1"
    rig.port.records[(FIRST.id, "r-1")] = "ok"
    rig.port.revision_gate.set()
    assert rig.card()["state"] == "update-available"
    hints = dict(rig.downloads._card_hints)
    hint_kinds = dict(rig.downloads._card_hint_kinds)
    report = Mock(wraps=rig.downloads.report_store_error)
    monkeypatch.setattr(rig.downloads, "report_store_error", report)
    original = getattr(rig.port, failed_read)

    def read(*args: Any, **kwargs: Any) -> Any:
        # Ошибка ровно в чтении указателей перед запуском ревизии: значок
        # карточки читает current_ids раньше и обрабатывает ошибку сам.
        if sys._getframe(1).f_code.co_name == "_start_revision":
            raise error_type("private/path model-id")
        return original(*args, **kwargs)

    monkeypatch.setattr(rig.port, failed_read, read)
    caplog.set_level(logging.WARNING)

    if revert:
        rig.bridge.revertModelPrevious(FIRST.id)
    else:
        rig.bridge.updateModel(FIRST.id)

    report.assert_called_once_with()
    assert len(_store_warnings(caplog)) == 1
    assert all(record.exc_info is None for record in caplog.records)
    assert "private" not in caplog.text and "model-id" not in caplog.text
    assert rig.idle()
    assert rig.downloads._revision_entry is None
    assert rig.downloads._revision_pause_pending is None
    assert rig.downloads._card_hints == hints
    assert rig.downloads._card_hint_kinds == hint_kinds
    assert rig.port.revision_calls == []
    assert rig.port.current_writes == []
    assert rig.port.current == (FIRST.id, "r0")
    assert rig.port.previous == "r-1"


@pytest.mark.parametrize("revert", [False, True])
@pytest.mark.parametrize("error_type", [OSError, StoreError])
@pytest.mark.parametrize("failed_read", ["current_ids", "previous_revision"])
def test_ib40c_pause_pending_survives_store_read_error(
    rig: RevisionRig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    revert: bool,
    error_type: type[Exception],
    failed_read: str,
) -> None:
    switcher = Mock()
    switcher.can_switch_without_pause.return_value = False
    rig.downloads._switcher = switcher
    switcher.on_switch_finished = rig.downloads._switch_finished
    rig.port.previous = "r-1"
    rig.port.records[(FIRST.id, "r-1")] = "ok"
    rig.port.revision_gate.set()
    if revert:
        rig.bridge.revertModelPrevious(FIRST.id)
    else:
        rig.bridge.updateModel(FIRST.id)
    pending = rig.downloads._revision_pause_pending
    assert pending == (FIRST.id, revert)
    hints = dict(rig.downloads._card_hints)
    hint_kinds = dict(rig.downloads._card_hint_kinds)
    assert hints
    caplog.set_level(logging.WARNING)
    with monkeypatch.context() as patch:
        patch.setattr(rig.port, failed_read, Mock(side_effect=error_type("private/path model-id")))
        rig.bridge.switchModelWithPause(FIRST.id)

    assert len(_store_warnings(caplog)) == 1
    assert "private" not in caplog.text and "model-id" not in caplog.text
    assert rig.idle()
    assert rig.downloads._revision_pause_pending == pending
    assert rig.downloads._card_hints == hints
    assert rig.downloads._card_hint_kinds == hint_kinds
    switcher.switch_model.assert_not_called()
    assert rig.port.revision_calls == []
    assert rig.port.current == (FIRST.id, "r0")

    # Состояние не тронуто: после восстановления хранилища пауза работает.
    rig.bridge.switchModelWithPause(FIRST.id)
    pump_until(rig.idle, "переключение с паузой после ошибки хранилища")
    switcher.switch_model.assert_called_once_with(min_ram_mb=FIRST.min_ram_mb, pause=True)
    switcher.on_switch_finished("ok")
    assert rig.card()["state"] == "ok"
