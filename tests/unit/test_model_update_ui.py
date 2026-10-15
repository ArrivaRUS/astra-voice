"""Контракт M7: обновление активной модели, фон, отмена и строка уведомлений.

Подставляется только порт службы моделей. Поток, канал задания, события Qt и
оба публичных моста настоящие; загрузчик/установщик проверяются отдельно.
"""

from __future__ import annotations

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
from astra_voice.models.catalog import Catalog
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
        self.records = {(FIRST.id, "r0"): "ok", (SECOND.id, "r0"): "ok"}
        self.current = (FIRST.id, "r0")
        self.previous: str | None = None
        self.revision_gate = threading.Event()
        self.revision_started = threading.Event()
        self.cancel_seen = threading.Event()
        self.revision_calls: list[tuple[str, str]] = []
        self.next_failure = ""
        self.revert_failure = ""

    def previous_revision(self) -> str | None:
        return self.previous

    def revision_updater(self, *, on_state: Callable[[str, str], None]) -> ScriptedUpdater:
        return ScriptedUpdater(self, on_state)

    def accept_catalog(self, catalog: Catalog) -> None:
        self.catalog = catalog.entries

    def open_gates(self) -> None:
        super().open_gates()
        self.revision_gate.set()


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
        if reason := port.next_failure:
            port.next_failure = ""
            self.on_state(model_id, "update-failed")
            return RevisionUpdateResult("update-failed", model_id, "r0", FAILED_MESSAGE, reason)
        entry = next(entry for entry in port.catalog if entry.id == model_id)
        assert port.current is not None
        port.previous = port.current[1]
        port.current = (model_id, entry.revision)
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
        port.current = (model_id, port.previous)
        port.previous = None
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
            "Эта версия отозвана авторами каталога",
        ),
        ("no-previous", "Предыдущая версия модели недоступна", "Не удалось скачать"),
        ("not-installed", "Обновлять можно только рабочую модель", "Не удалось скачать"),
        ("no-update", "Новая версия модели недоступна", "Не удалось скачать"),
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
