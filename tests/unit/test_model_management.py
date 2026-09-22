"""Раздел «Модели»: переключение рабочей модели, удаление и «Обновить».

Очередь загрузок здесь не запускается: проверяем решения моста, а не потоки.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QCoreApplication
from PyQt5.QtTest import QSignalSpy

from astra_voice.core import settings as settings_mod
from astra_voice.models.catalog import CatalogEntry
from astra_voice.models.downloader import Progress
from astra_voice.models.store import StoreError
from astra_voice.ui import model_downloads
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.model_downloads import ModelDownloads, ModelService

pytestmark = pytest.mark.unit

GIGAAM = CatalogEntry(
    id="gigaam-v3-rnnt",
    revision="r2",
    name="GigaAM v3 RNN-T",
    description="Русская диктовка с пунктуацией",
    size_bytes=226_000_000,
    min_ram_mb=768,
    layout="nemo-rnnt",
    variant="int8",
    recommended=True,
    host="huggingface.co",
    files=(),
    domestic=True,
)
TONE = replace(
    GIGAAM,
    id="t-one",
    revision="r1",
    name="T-one",
    description="Русская, лёгкая, без пунктуации",
    size_bytes=144_000_000,
    recommended=False,
    domestic=False,
)


@pytest.fixture(scope="module", autouse=True)
def qcore_app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


class FakeManagedPort:
    """Каталог из двух записей и хранилище, которым распоряжается тест."""

    def __init__(self) -> None:
        self.catalog: tuple[CatalogEntry, ...] = (GIGAAM, TONE)
        #: (идентификатор, ревизия) → состояние записи в хранилище.
        self.records: dict[tuple[str, str], str] = {}
        self.current: tuple[str, str] | None = None
        self.removed: list[tuple[str, str]] = []
        self.set_current_error: Exception | None = None
        self.remove_error: Exception | None = None

    # ── то, что спрашивает ModelDownloads ────────────────────────────────
    def recommended(self) -> CatalogEntry:
        return GIGAAM

    def entries(self) -> tuple[CatalogEntry, ...]:
        return self.catalog

    def is_revoked(self, entry: Any) -> bool:
        return False

    def recheck_entries(self) -> tuple[CatalogEntry, ...]:
        return ()

    def verify_files(self, entry: Any) -> tuple[bool, str]:
        raise AssertionError("Перепроверка в этих тестах не запускается")

    def smoke(self, entry: Any) -> tuple[bool, str]:
        raise AssertionError("Перепроверка в этих тестах не запускается")

    def mark_ok(self, model_id: str, revision: str) -> None:
        raise AssertionError("Перепроверка в этих тестах не запускается")

    def mark_broken(self, model_id: str, revision: str, reason: str) -> None:
        raise AssertionError("Перепроверка в этих тестах не запускается")

    def record_state(self, model_id: str, revision: str) -> str:
        return self.records.get((model_id, revision), "")

    def current_ids(self) -> tuple[str, str] | None:
        return self.current

    def installed_ids(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.records)

    def set_current(self, model_id: str, revision: str) -> None:
        if self.set_current_error is not None:
            raise self.set_current_error
        self.current = (model_id, revision)

    def remove(self, model_id: str, revision: str) -> None:
        if self.remove_error is not None:
            raise self.remove_error
        self.removed.append((model_id, revision))
        self.records.pop((model_id, revision), None)
        if self.current == (model_id, revision):
            self.current = None

    def installed_ok(self) -> bool:
        return any(state == "ok" for state in self.records.values())

    def broken(self) -> bool:
        return any(state == "broken" for state in self.records.values())

    def allowed(self) -> tuple[bool, str]:
        return True, ""

    def disk_ok(self, size_bytes: int) -> bool:
        return True

    def disk_missing_bytes(self, size_bytes: int) -> int:
        return 0

    def free_bytes(self) -> int:
        return 42_100_000_000

    def ram_ok(self, min_ram_mb: int) -> bool:
        return True

    def download(
        self,
        entry: Any,
        *,
        progress: Callable[[Progress], None],
        cancel: threading.Event,
    ) -> Path:
        raise AssertionError("Очередь в этих тестах не запускается")

    def install_from_staging(self, entry: Any) -> Any:
        raise AssertionError("Очередь в этих тестах не запускается")

    def install_from_path(self, source: Path, entry: Any) -> Any:
        raise AssertionError("Очередь в этих тестах не запускается")


Rig = tuple[FakeManagedPort, ModelDownloads, list[tuple[Any, ...]]]


@pytest.fixture
def rig() -> Iterator[Rig]:
    port = FakeManagedPort()
    downloads = ModelDownloads(port)
    queued: list[tuple[Any, ...]] = []
    # Загрузку подменяем: поток и сеть к решениям раздела отношения не имеют.
    downloads._begin_queue = lambda entries, source=None: queued.append(entries)  # type: ignore[method-assign]
    yield port, downloads, queued
    downloads.shutdown()


def card(downloads: ModelDownloads, model_id: str) -> dict[str, Any]:
    return next(item for item in downloads.models if item["id"] == model_id)


def test_installed_summary_counts_catalog_entries(rig: Rig) -> None:
    port, downloads, _ = rig
    assert downloads.installedSummary == "Установлено 0 из 2"
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    assert downloads.installedSummary == "Установлено 1 из 2 · 226 МБ на диске"
    port.records[(TONE.id, TONE.revision)] = "ok"
    assert downloads.installedSummary == "Установлено 2 из 2 · 370 МБ на диске"


def test_installed_summary_is_empty_without_catalog() -> None:
    downloads = ModelDownloads(None)
    try:
        assert downloads.installedSummary == ""
        assert downloads.models == []
    finally:
        downloads.shutdown()


def test_domestic_flag_comes_from_catalog(rig: Rig) -> None:
    _, downloads, _ = rig
    assert card(downloads, GIGAAM.id)["domestic"] is True
    assert card(downloads, TONE.id)["domestic"] is False


def test_old_revision_stays_installed_and_offers_update(rig: Rig) -> None:
    """Каталог знает r2, в хранилище r1: модель установлена, но устарела."""
    port, downloads, queued = rig
    port.records[(GIGAAM.id, "r1")] = "ok"
    port.current = (GIGAAM.id, "r1")

    item = card(downloads, GIGAAM.id)
    assert item["badge"] == "active"
    assert item["state"] == "installed"
    assert item["updateAvailable"] is True
    assert downloads.installedSummary == "Установлено 1 из 2 · 226 МБ на диске"

    downloads.updateModel(GIGAAM.id)
    assert queued == [(GIGAAM,)]


def test_update_is_not_offered_for_the_catalog_revision(rig: Rig) -> None:
    port, downloads, queued = rig
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    assert card(downloads, GIGAAM.id)["updateAvailable"] is False
    downloads.updateModel(GIGAAM.id)
    downloads.updateModel("неизвестная")
    assert queued == []


def test_make_model_current_switches_the_active_card(rig: Rig) -> None:
    port, downloads, _ = rig
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.records[(TONE.id, TONE.revision)] = "ok"
    port.current = (GIGAAM.id, GIGAAM.revision)
    changed = QSignalSpy(downloads.modelsChanged)

    downloads.makeModelCurrent(TONE.id)

    assert port.current == (TONE.id, TONE.revision)
    assert card(downloads, TONE.id)["badge"] == "active"
    assert card(downloads, GIGAAM.id)["badge"] == "installed"
    assert len(changed) == 1


@pytest.mark.parametrize("state", ["", "broken"])
def test_make_model_current_refuses_missing_and_broken(rig: Rig, state: str) -> None:
    port, downloads, _ = rig
    if state:
        port.records[(TONE.id, TONE.revision)] = state
    downloads.makeModelCurrent(TONE.id)
    downloads.makeModelCurrent("неизвестная")
    assert port.current is None
    assert card(downloads, TONE.id)["message"] == ""


def test_make_model_current_explains_store_failure(rig: Rig) -> None:
    port, downloads, _ = rig
    port.records[(TONE.id, TONE.revision)] = "ok"
    port.set_current_error = StoreError("broken-store")

    downloads.makeModelCurrent(TONE.id)

    assert port.current is None
    assert card(downloads, TONE.id)["message"] == model_downloads._CURRENT_FAILED_MESSAGE


def test_remove_model_deletes_revision_and_forgets_the_card(rig: Rig) -> None:
    port, downloads, _ = rig
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.records[(TONE.id, TONE.revision)] = "broken"
    port.current = (GIGAAM.id, GIGAAM.revision)
    downloads._card_messages[TONE.id] = "Модель нужно переустановить."
    ready = QSignalSpy(downloads.modelReadyChanged)

    downloads.removeModel(TONE.id)

    assert port.removed == [(TONE.id, TONE.revision)]
    item = card(downloads, TONE.id)
    assert item["badge"] == "" and item["state"] == "available" and item["message"] == ""
    assert downloads.installedSummary == "Установлено 1 из 2 · 226 МБ на диске"
    assert len(ready) == 1


def test_remove_model_removes_the_installed_old_revision(rig: Rig) -> None:
    port, downloads, _ = rig
    port.records[(GIGAAM.id, "r1")] = "ok"

    downloads.removeModel(GIGAAM.id)

    assert port.removed == [(GIGAAM.id, "r1")]


def test_remove_model_refuses_the_working_model(rig: Rig) -> None:
    port, downloads, _ = rig
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.current = (GIGAAM.id, GIGAAM.revision)

    downloads.removeModel(GIGAAM.id)
    downloads.removeModel("неизвестная")

    assert port.removed == []
    assert card(downloads, GIGAAM.id)["badge"] == "active"


def test_remove_model_explains_store_failure(rig: Rig) -> None:
    port, downloads, _ = rig
    port.records[(TONE.id, TONE.revision)] = "ok"
    port.remove_error = StoreError("broken-store")

    downloads.removeModel(TONE.id)

    assert port.removed == []
    assert card(downloads, TONE.id)["message"] == model_downloads._REMOVE_FAILED_MESSAGE


def test_management_waits_for_a_free_queue(rig: Rig) -> None:
    """Пока идёт загрузка или проверка, кнопки установленной карточки молчат."""
    port, downloads, queued = rig
    port.records[(TONE.id, TONE.revision)] = "ok"
    port.records[(GIGAAM.id, "r1")] = "ok"
    downloads._queue_running = True

    downloads.makeModelCurrent(TONE.id)
    downloads.removeModel(TONE.id)
    downloads.updateModel(GIGAAM.id)

    assert port.current is None and port.removed == [] and queued == []


def test_settings_bridge_forwards_model_management() -> None:
    downloads = Mock(spec=ModelDownloads)
    bridge = SettingsBridge(settings_mod.from_dict({}), save=Mock(), downloads=downloads)
    downloads.installedSummary = "Установлено 1 из 12 · 226 МБ на диске"

    assert bridge.installedSummary == "Установлено 1 из 12 · 226 МБ на диске"
    bridge.makeModelCurrent("t-one")
    bridge.removeModel("t-one")
    bridge.updateModel("t-one")

    downloads.makeModelCurrent.assert_called_once_with("t-one")
    downloads.removeModel.assert_called_once_with("t-one")
    downloads.updateModel.assert_called_once_with("t-one")


def test_settings_bridge_without_downloads_is_silent() -> None:
    bridge = SettingsBridge(settings_mod.from_dict({}), save=Mock())

    assert bridge.installedSummary == ""
    bridge.makeModelCurrent("t-one")
    bridge.removeModel("t-one")
    bridge.updateModel("t-one")


def test_model_service_delegates_removal_to_the_store() -> None:
    service = ModelService.__new__(ModelService)
    store = Mock()
    service._store = store

    service.remove("t-one", "r1")

    store.remove.assert_called_once_with("t-one", "r1")
