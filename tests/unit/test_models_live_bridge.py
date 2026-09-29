"""Мост раздела «Модели» на живой очереди: состояния карточек и полосы загрузки.

Загрузчик подставной (``helpers.scripted_model_port``), но очередь, рабочий поток,
канал заданий и сигналы — настоящие: так же их видит ``qml/sections/Models.qml``
и полоса загрузки ``qml/Main.qml``. Состояния — спецификация §5.4 (14–17а) и §10.3.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent

from astra_voice.core import settings as settings_mod
from astra_voice.models.downloader import DownloadError
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.model_downloads import ModelDownloads
from helpers.scripted_model_port import FIRST, SECOND, ScriptedModelPort, pump_until

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module", autouse=True)
def qapp() -> QCoreApplication:
    from helpers.qt_app import get_qapplication

    return get_qapplication()


@dataclass
class Live:
    port: ScriptedModelPort
    downloads: ModelDownloads
    bridge: SettingsBridge
    #: Значения ``downloadState`` в момент каждого ``downloadStateChanged`` моста.
    strip_states: list[str]
    #: Потоки, в которых мост посылал ``modelsChanged``.
    signal_threads: set[int]

    def card(self, model_id: str) -> dict[str, Any]:
        rows: list[dict[str, Any]] = [row for row in self.bridge.models if row["id"] == model_id]
        assert len(rows) == 1, model_id
        return rows[0]

    def state(self, model_id: str) -> str:
        return str(self.card(model_id)["state"])

    def idle(self) -> bool:
        return self.downloads._model_thread is None


@pytest.fixture
def live() -> Iterator[Live]:
    port = ScriptedModelPort()
    downloads = ModelDownloads(port)
    bridge = SettingsBridge(settings_mod.from_dict({}), save=Mock(), downloads=downloads)
    rig = Live(port, downloads, bridge, [], set())
    bridge.downloadStateChanged.connect(lambda: rig.strip_states.append(bridge.downloadState))
    bridge.modelsChanged.connect(lambda: rig.signal_threads.add(threading.get_ident()))
    yield rig
    # Ворота открыты до остановки: рабочий поток не ждёт до своего предела.
    port.open_gates()
    downloads.shutdown()
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def assert_gui_thread_only(live: Live) -> None:
    """Сигналы моста — только в GUI; рабочий поток отдаёт значения (уроки 025–026)."""
    assert live.signal_threads == {threading.get_ident()}
    assert live.port.worker_threads
    assert threading.get_ident() not in live.port.worker_threads


def test_queue_download_verify_and_ready_reach_bridge(live: Live) -> None:
    live.bridge.toggleModel(FIRST.id)
    live.bridge.toggleModel(SECOND.id)
    assert live.bridge.selectionSummary == "Будет скачано 370 МБ"

    live.bridge.startSelectedDownloads()
    # Очередь — сразу, без событий рабочего потока (§5.4, 14–15).
    first, second = live.card(FIRST.id), live.card(SECOND.id)
    assert (first["state"], first["canCancel"], first["canDequeue"]) == ("downloading", True, False)
    assert (second["state"], second["canCancel"], second["canDequeue"]) == ("queued", False, True)
    assert live.bridge.downloadState == "downloading"
    assert live.bridge.downloadTitle == "Загружается Первая модель"
    assert live.bridge.downloadCounter == "1 из 2"
    assert live.bridge.selectionLine == "Идёт загрузка"

    # Источник и прогресс приходят из рабочего потока (§10.3, состояние 1).
    pump_until(
        lambda: live.bridge.downloadSource == "Скачиваю с huggingface.co",
        "источник загрузки в мосте",
    )
    pump_until(lambda: live.bridge.downloadProgress > 0, "прогресс загрузки в мосте")
    assert live.bridge.downloadProgress == pytest.approx(113_000_000 / 370_000_000)
    assert live.card(FIRST.id)["progress"] == pytest.approx(0.5)
    assert live.card(FIRST.id)["message"] == ""

    # Установка и самопроверка: отмена недоступна, источника уже нет (§5.4, 16).
    live.port.download_gate.set()
    pump_until(lambda: live.state(FIRST.id) == "verifying", "проверка первой модели")
    assert live.card(FIRST.id)["canCancel"] is False
    assert live.bridge.downloadState == "verifying"
    assert live.bridge.downloadTitle == "Проверяю модель…"
    assert live.bridge.downloadSource == ""
    assert live.state(SECOND.id) == "queued"

    live.port.install_gate.set()
    pump_until(lambda: live.bridge.downloadState == "done" and live.idle(), "готовая очередь")
    assert live.bridge.downloadTitle == "Модель готова"
    assert live.bridge.downloadCounter == ""
    assert [live.state(entry.id) for entry in (FIRST, SECOND)] == ["installed", "installed"]
    assert [live.card(entry.id)["badge"] for entry in (FIRST, SECOND)] == ["active", "installed"]
    assert live.strip_states == ["downloading", "verifying", "downloading", "verifying", "done"]
    assert live.port.download_calls == [FIRST.id, SECOND.id]
    assert_gui_thread_only(live)


@pytest.mark.parametrize("where", ["strip", "card"])
def test_failed_download_retries_from_strip_and_card(live: Live, where: str) -> None:
    live.port.failures[FIRST.id] = DownloadError("no-network")
    live.port.open_gates()
    live.bridge.toggleModel(FIRST.id)
    live.bridge.startSelectedDownloads()
    pump_until(lambda: live.bridge.downloadState == "failed" and live.idle(), "ошибка загрузки")

    card = live.card(FIRST.id)
    assert card["state"] == "failed"
    assert card["message"] == "Не удалось загрузить модель — нет связи с сервером"
    assert (card["canRetry"], card["canCancel"], card["selected"]) == (True, False, False)
    assert live.bridge.downloadTitle == "Не удалось загрузить модель"
    # Прежняя кнопка полосы звала «Скачать выбранное»: запись уже вне выбора.
    live.bridge.startSelectedDownloads()
    assert live.bridge.downloadState == "failed"
    assert live.port.download_calls == [FIRST.id]

    if where == "strip":
        live.bridge.retryFailedDownloads()
    else:
        live.bridge.retryModel(FIRST.id)
    assert live.state(FIRST.id) == "downloading"
    assert live.bridge.downloadState == "downloading"
    pump_until(lambda: live.bridge.downloadState == "done" and live.idle(), "повтор до готовности")
    assert live.state(FIRST.id) == "installed"
    assert live.card(FIRST.id)["message"] == ""
    assert live.port.download_calls == [FIRST.id, FIRST.id]
    assert_gui_thread_only(live)


def test_strip_retry_repeats_every_failed_card_in_catalog_order(live: Live) -> None:
    live.port.failures[FIRST.id] = DownloadError("no-network")
    live.port.failures[SECOND.id] = DownloadError("bad-checksum")
    live.port.open_gates()
    live.bridge.toggleModel(SECOND.id)
    live.bridge.toggleModel(FIRST.id)
    live.bridge.startSelectedDownloads()
    pump_until(lambda: live.bridge.downloadState == "failed" and live.idle(), "обе ошибки")
    assert live.card(SECOND.id)["message"] == (
        "Не удалось загрузить модель — файл не прошёл проверку ни на одном сервере"
    )

    # Держим повтор на загрузке, чтобы увидеть очередь из двух записей.
    live.port.download_gate.clear()
    live.bridge.retryFailedDownloads()
    assert (live.state(FIRST.id), live.state(SECOND.id)) == ("downloading", "queued")
    assert live.bridge.downloadCounter == "1 из 2"
    live.port.download_gate.set()
    pump_until(lambda: live.bridge.downloadState == "done" and live.idle(), "обе модели готовы")
    assert live.port.download_calls == [FIRST.id, SECOND.id, FIRST.id, SECOND.id]


def test_strip_retry_without_failed_cards_does_nothing(live: Live) -> None:
    live.bridge.retryFailedDownloads()
    assert live.bridge.downloadState == "idle"
    assert [live.state(entry.id) for entry in (FIRST, SECOND)] == ["available", "available"]
    bare = SettingsBridge(settings_mod.from_dict({}), save=Mock())
    bare.retryFailedDownloads()
    assert bare.downloadState == ""


@pytest.mark.parametrize("action", ["retry", "cancel"])
def test_no_space_pause_then_retry_or_cancel(live: Live, action: str) -> None:
    live.port.space = False
    live.port.open_gates()
    live.bridge.toggleModel(FIRST.id)
    live.bridge.startSelectedDownloads()
    pump_until(lambda: live.state(FIRST.id) == "paused-no-space" and live.idle(), "пауза по месту")

    # §5.4 17а в карточке и §10.3 состояние 5 в полосе.
    card = live.card(FIRST.id)
    assert card["message"] == "Не хватает места — освободите 13 МБ"
    assert (card["canRetry"], card["canCancel"]) == (True, True)
    assert live.bridge.downloadState == "no-space"
    assert live.bridge.downloadTitle == "Не хватает места на диске"
    assert live.bridge.downloadDetail == "нужно ещё 13 МБ"
    assert live.bridge.selectionLine == "Загрузка на паузе"
    assert live.port.download_calls == []

    # Место не освободилось — повтор оставляет паузу и в сеть не идёт.
    live.bridge.retryModel(FIRST.id)
    assert live.state(FIRST.id) == "paused-no-space"
    assert live.port.download_calls == []

    if action == "retry":
        live.port.space = True
        live.bridge.retryModel(FIRST.id)
        assert live.state(FIRST.id) == "downloading"
        pump_until(
            lambda: live.bridge.downloadState == "done" and live.idle(), "готово после паузы"
        )
        assert live.state(FIRST.id) == "installed"
        assert live.port.discarded == []
    else:
        live.bridge.cancelModel(FIRST.id)
        assert live.state(FIRST.id) == "available"
        assert live.card(FIRST.id)["message"] == ""
        assert live.bridge.downloadState == "idle"
        assert live.port.discarded == [FIRST.id]
        assert live.port.download_calls == []


def test_cancel_queued_and_active_cards(live: Live) -> None:
    live.bridge.toggleModel(FIRST.id)
    live.bridge.toggleModel(SECOND.id)
    live.bridge.startSelectedDownloads()
    pump_until(live.port.downloading.is_set, "загрузка первой модели")

    # «Отмена» у ожидающей карточки убирает её из очереди (§5.4, 15).
    live.bridge.dequeueModel(SECOND.id)
    assert live.state(SECOND.id) == "available"
    assert live.bridge.downloadCounter == ""
    assert live.bridge.downloadState == "downloading"

    # «Отмена» у активной: карточка свободна сразу, частичные файлы удалены (§5.4, 14).
    live.bridge.cancelModel(FIRST.id)
    assert live.state(FIRST.id) == "available"
    pump_until(lambda: live.bridge.downloadState == "idle" and live.idle(), "отмена очереди")
    assert live.card(FIRST.id)["message"] == ""
    assert live.port.discarded == [FIRST.id]
    assert live.port.download_calls == [FIRST.id]
    assert live.bridge.downloadProgress == 0
    assert live.bridge.speed == live.bridge.eta == ""
    assert_gui_thread_only(live)
