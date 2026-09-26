"""Раздел «Модели»: переключение рабочей модели, удаление и «Обновить».

Очередь загрузок здесь не запускается: проверяем решения моста, а не потоки.
"""

from __future__ import annotations

import logging
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
from astra_voice.core.model_source import RevocationUnknown
from astra_voice.models.catalog import CatalogEntry, FileSpec
from astra_voice.models.downloader import Progress, remaining_bytes
from astra_voice.models.store import ModelRecord, ModelState, ModelStore, StoreError
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
        self.total_ram_mb: float | None = None
        self.discarded: list[str] = []
        self.space = True
        self.missing_bytes = 0
        self.staged_store: ModelStore | None = None

    # ── то, что спрашивает ModelDownloads ────────────────────────────────
    def recommended(self) -> CatalogEntry:
        return GIGAAM

    def entries(self) -> tuple[CatalogEntry, ...]:
        return self.catalog

    def is_revoked(self, entry: Any) -> bool:
        return False

    def revoked_revision(self, model_id: str, revision: str) -> bool:
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

    def record_size_bytes(self, model_id: str, revision: str) -> int:
        if (model_id, revision) == (TONE.id, TONE.revision):
            return 101_000_000
        return 226_000_000

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
        return self.space

    def remaining_bytes(self, entry: CatalogEntry) -> int:
        return (
            remaining_bytes(self.staged_store, entry)
            if self.staged_store is not None
            else entry.size_bytes
        )

    def disk_missing_bytes(self, size_bytes: int) -> int:
        return self.missing_bytes

    def free_bytes(self) -> int:
        return 42_100_000_000

    def ram_ok(self, min_ram_mb: int) -> bool:
        return True

    def mem_total_mb(self) -> float | None:
        return self.total_ram_mb

    def download(
        self,
        entry: Any,
        *,
        progress: Callable[[Progress], None],
        cancel: threading.Event,
        source: Callable[[str], None] | None = None,
    ) -> Path:
        raise AssertionError("Очередь в этих тестах не запускается")

    def discard_staging(self, entry: Any) -> None:
        self.discarded.append(entry.id)

    def install_from_staging(self, entry: Any) -> Any:
        raise AssertionError("Очередь в этих тестах не запускается")

    def install_from_path(self, source: Path, entry: Any) -> Any:
        raise AssertionError("Очередь в этих тестах не запускается")


Rig = tuple[FakeManagedPort, ModelDownloads, list[tuple[Any, ...]]]


class FakeSwitchPort:
    """Сохраняет запрос и отдаёт ответ вручную, как рантайм после загрузки."""

    def __init__(self, *, enough_memory: bool) -> None:
        self.enough_memory = enough_memory
        self.available_mb: float | None = None
        self.checked: list[int] = []
        self.calls: list[tuple[int, bool]] = []
        self.on_switch_finished: Callable[[str], None] | None = None

    def can_switch_without_pause(self, min_ram_mb: int) -> bool:
        self.checked.append(min_ram_mb)
        return self.enough_memory

    def mem_available_mb(self) -> float | None:
        return self.available_mb

    def switch_model(self, *, min_ram_mb: int, pause: bool = False) -> None:
        self.calls.append((min_ram_mb, pause))

    def finish(self, result: str) -> None:
        assert self.on_switch_finished is not None
        self.on_switch_finished(result)


def switched_rig(enough_memory: bool) -> tuple[FakeManagedPort, ModelDownloads, FakeSwitchPort]:
    port = FakeManagedPort()
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.records[(TONE.id, TONE.revision)] = "ok"
    port.current = (GIGAAM.id, GIGAAM.revision)
    switcher = FakeSwitchPort(enough_memory=enough_memory)
    return port, ModelDownloads(port, switcher=switcher), switcher


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


def manual_queue(port: FakeManagedPort) -> tuple[ModelDownloads, list[str]]:
    """Запускает решения очереди без рабочих потоков и сети."""
    downloads = ModelDownloads(port)
    started: list[str] = []

    def start(source: Path | None = None) -> None:
        if not downloads._queue:
            return
        entry = downloads._queue.pop(0)
        downloads._active_entry = entry
        started.append(entry.id)
        downloads._set_card(entry, "downloading")
        downloads._set_download_state("downloading")

    downloads._start_model_job = start  # type: ignore[method-assign]
    return downloads, started


def test_dynamic_queue_fifo_dequeue_and_card_fields() -> None:
    port = FakeManagedPort()
    third = replace(TONE, id="third", name="Третья")
    port.catalog = (GIGAAM, TONE, third)
    downloads, started = manual_queue(port)
    try:
        for row in downloads.models:
            assert {
                "queuePosition",
                "sourceText",
                "failReason",
                "canCancel",
                "canRetry",
                "canDequeue",
            } <= row.keys()
        downloads.toggleModel(GIGAAM.id)
        downloads.toggleModel(third.id)
        downloads.startSelectedDownloads()
        assert started == [GIGAAM.id]
        assert card(downloads, third.id)["queuePosition"] == 1
        downloads._model_progressed(0.5)
        downloads.toggleModel(TONE.id)
        downloads.startSelectedDownloads()
        assert [entry.id for entry in downloads._queue] == [third.id, TONE.id]
        assert downloads.downloadTitle == "Загружается GigaAM v3 RNN-T · 1 из 3"
        assert card(downloads, TONE.id)["queuePosition"] == 2
        assert downloads.downloadProgress == 0.5 * GIGAAM.size_bytes / (
            GIGAAM.size_bytes + TONE.size_bytes + third.size_bytes
        )
        downloads.dequeueModel(third.id)
        assert card(downloads, third.id)["state"] == "available"
        assert card(downloads, TONE.id)["queuePosition"] == 1
        assert card(downloads, TONE.id)["canDequeue"]
        assert downloads._queue_total_bytes == GIGAAM.size_bytes + TONE.size_bytes
        assert downloads.downloadProgress == 0.5 * GIGAAM.size_bytes / (
            GIGAAM.size_bytes + TONE.size_bytes
        )
    finally:
        downloads.shutdown()


def test_cancel_one_and_cancel_all_discard_staging() -> None:
    port = FakeManagedPort()
    downloads, started = manual_queue(port)
    try:
        downloads.toggleModel(GIGAAM.id)
        downloads.toggleModel(TONE.id)
        downloads.startSelectedDownloads()
        downloads.cancelModel(GIGAAM.id)
        assert card(downloads, GIGAAM.id)["state"] == "available"
        downloads._model_finished("cancelled", "")
        downloads._model_thread_finished()
        assert port.discarded == [GIGAAM.id]
        assert started == [GIGAAM.id, TONE.id]
        downloads.cancelDownloads()
        downloads._model_finished("cancelled", "")
        downloads._model_thread_finished()
        assert port.discarded == [GIGAAM.id, TONE.id]
    finally:
        downloads.shutdown()


def test_pause_retry_preserves_staging_until_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(FakeManagedPort, "remaining_bytes")
    port = FakeManagedPort()
    downloads, started = manual_queue(port)
    try:
        downloads.toggleModel(GIGAAM.id)
        downloads.toggleModel(TONE.id)
        downloads.startSelectedDownloads()
        port.space = False
        port.missing_bytes = 12_500_000
        downloads._model_finished("no-space", "")
        downloads._model_thread_finished()
        assert downloads.downloadState == "no-space"
        assert card(downloads, GIGAAM.id)["state"] == "paused-no-space"
        assert card(downloads, GIGAAM.id)["message"] == (
            "Не хватает места на диске — освободите 13 МБ"
        )
        assert card(downloads, TONE.id)["state"] == "queued"
        downloads.retryModel(GIGAAM.id)
        assert started == [GIGAAM.id] and port.discarded == []
        port.space = True
        downloads.retryModel(GIGAAM.id)
        assert started == [GIGAAM.id, GIGAAM.id]
        assert port.discarded == []
    finally:
        downloads.shutdown()


def test_pause_retry_uses_part_file_remainder(tmp_path: Path) -> None:
    port = FakeManagedPort()
    entry = replace(
        GIGAAM,
        files=(FileSpec("model.bin", "0" * 64, GIGAAM.size_bytes, "model.bin"),),
    )
    port.catalog = (entry, TONE)
    store = ModelStore(tmp_path / "models")
    port.staged_store = store
    part = store.staging_dir(entry.id, entry.revision) / "model.bin.part"
    with part.open("wb") as stream:
        stream.truncate(203_400_000)
    available = 15_000_000
    port.disk_ok = lambda size: (size * 6 + 4) // 5 <= available  # type: ignore[assignment]
    port.disk_missing_bytes = lambda size: max(  # type: ignore[assignment]
        0, (size * 6 + 4) // 5 - available
    )
    downloads, started = manual_queue(port)
    try:
        downloads.toggleModel(entry.id)
        downloads.startSelectedDownloads()
        downloads._model_finished("no-space", "")
        downloads._model_thread_finished()
        assert remaining_bytes(store, entry) == 22_600_000
        assert card(downloads, entry.id)["message"] == (
            "Не хватает места на диске — освободите 13 МБ"
        )
        assert downloads.downloadDetail == "нужно ещё 13 МБ"
        downloads.retryModel(entry.id)
        assert started == [entry.id]
        available = 28_000_000
        assert not port.disk_ok(entry.size_bytes)
        downloads.retryModel(entry.id)
        assert started == [entry.id, entry.id]
        assert part.exists()
    finally:
        downloads.shutdown()


@pytest.mark.parametrize(
    "kind, text",
    [
        ("hf", "Скачиваю с huggingface.co"),
        ("github", "Скачиваю с github.com"),
        ("corp", "Скачиваю с корпоративного сервера"),
    ],
)
def test_source_text_and_progress_stay_safe(kind: str, text: str) -> None:
    port = FakeManagedPort()
    downloads, _started = manual_queue(port)
    try:
        downloads.toggleModel(GIGAAM.id)
        downloads.startSelectedDownloads()
        downloads._model_progressed(0.5)
        before = downloads.downloadProgress
        downloads._model_sourced(kind)
        downloads._model_progressed(0.4)
        assert downloads.downloadProgress == before
        assert downloads.downloadSource == card(downloads, GIGAAM.id)["sourceText"] == text
        downloads._model_staged("verifying")
        assert downloads.downloadSource == card(downloads, GIGAAM.id)["sourceText"] == ""
    finally:
        downloads.shutdown()


@pytest.mark.parametrize(
    "code, message",
    [
        ("no-network", "Не удалось загрузить модель — нет связи с сервером"),
        ("not-allowed", "Не удалось загрузить модель — источник запрещён настройками"),
        ("bad-sha", "Файл не прошёл проверку — загруженное удалено"),
        ("no-space", "Не удалось загрузить модель — не хватает места на диске"),
        ("timeout", "Не удалось загрузить модель — сервер не отвечает"),
        ("server", "Не удалось загрузить модель — сервер ответил ошибкой"),
    ],
)
def test_download_failure_reason_is_closed(code: str, message: str) -> None:
    port = FakeManagedPort()
    downloads, _started = manual_queue(port)
    try:
        downloads.toggleModel(GIGAAM.id)
        downloads.startSelectedDownloads()
        downloads._model_finished(f"failed:{code}", "SECRET /private/path")
        downloads._model_thread_finished()
        row = card(downloads, GIGAAM.id)
        assert (row["state"], row["failReason"], row["message"]) == ("failed", code, message)
        assert row["canRetry"]
        assert port.discarded == ([GIGAAM.id] if code == "bad-sha" else [])
    finally:
        downloads.shutdown()


def test_service_forwards_source_and_discards_one_revision() -> None:
    service = ModelService.__new__(ModelService)
    service._downloader = Mock()
    service._store = Mock()
    progress = Mock()
    source = Mock()
    cancel = threading.Event()
    service.download(GIGAAM, progress=progress, cancel=cancel, source=source)
    service._downloader.download.assert_called_once_with(
        GIGAAM, progress=progress, cancel=cancel, source=source
    )
    service.discard_staging(GIGAAM)
    service._store.discard_staging.assert_called_once_with(GIGAAM.id, GIGAAM.revision)


def test_hint_fields_are_present_without_switcher(rig: Rig) -> None:
    _, downloads, _ = rig
    for item in downloads.models:
        assert item["hint"] == ""
        assert item["hintKind"] == ""
        assert item["memoryShortage"] is False
        assert item["canSwitchWithPause"] is False
        assert item["canReinstall"] is True


def test_unknown_revocation_hint_on_active_model_card() -> None:
    port = FakeManagedPort()
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.current = (GIGAAM.id, GIGAAM.revision)
    unknown = False
    downloads = ModelDownloads(port, revocation_unknown=lambda: unknown)
    try:
        unknown = True
        downloads.revocation_unknown_changed()
        item = card(downloads, GIGAAM.id)
        assert item["hint"] == "Не удалось проверить список отозванных версий"
        assert item["hintKind"] == "warning"
        port.records[(TONE.id, TONE.revision)] = "ok"
        downloads.makeModelCurrent(TONE.id)
        assert card(downloads, GIGAAM.id)["hint"] == ""
        assert card(downloads, TONE.id)["hint"] == "Не удалось проверить список отозванных версий"
        assert card(downloads, TONE.id)["hintKind"] == "warning"
    finally:
        downloads.shutdown()


def test_unknown_revocation_hint_on_initial_active_model_card() -> None:
    port = FakeManagedPort()
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.current = (GIGAAM.id, GIGAAM.revision)
    downloads = ModelDownloads(port, revocation_unknown=lambda: True)
    try:
        assert card(downloads, GIGAAM.id)["hint"] == "Не удалось проверить список отозванных версий"
        assert card(downloads, GIGAAM.id)["hintKind"] == "warning"
    finally:
        downloads.shutdown()


def test_unknown_revocation_hint_survives_switching_card_update() -> None:
    port = FakeManagedPort()
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.records[(TONE.id, TONE.revision)] = "ok"
    port.current = (GIGAAM.id, GIGAAM.revision)
    switcher = FakeSwitchPort(enough_memory=True)
    downloads = ModelDownloads(port, switcher=switcher, revocation_unknown=lambda: True)
    try:
        downloads.makeModelCurrent(TONE.id)
        assert card(downloads, GIGAAM.id)["hint"] == ""
        assert card(downloads, TONE.id)["hint"] == "Не удалось проверить список отозванных версий"
        switcher.finish("ok")
        assert card(downloads, TONE.id)["hintKind"] == "warning"
    finally:
        downloads.shutdown()


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
        assert downloads.installedCount == 0
        assert downloads.models == []
    finally:
        downloads.shutdown()


@pytest.mark.parametrize("catalog_failure", ["missing", "store_error", "os_error"])
@pytest.mark.parametrize("action", ["make_direct", "make", "pause"])
def test_snapshot_revocation_blocks_current_change_when_catalog_unavailable(
    catalog_failure: str, action: str
) -> None:
    port = FakeManagedPort()
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.records[(TONE.id, TONE.revision)] = "ok"
    port.current = (GIGAAM.id, GIGAAM.revision)
    if catalog_failure == "missing":
        port.is_revoked = None  # type: ignore[assignment]
    else:
        error = StoreError("catalog") if catalog_failure == "store_error" else OSError("catalog")
        port.is_revoked = Mock(side_effect=error)  # type: ignore[method-assign]
    set_current = Mock(wraps=port.set_current)
    port.set_current = set_current  # type: ignore[method-assign]
    switcher = FakeSwitchPort(enough_memory=True)
    checked: list[tuple[str, str]] = []

    def revoked(model_id: str, revision: str) -> bool:
        checked.append((model_id, revision))
        return (model_id, revision) == (TONE.id, TONE.revision)

    downloads = ModelDownloads(
        port, switcher=None if action == "make_direct" else switcher, revoked_check=revoked
    )
    try:
        item = card(downloads, TONE.id)
        assert item["state"] == "failed"
        assert item["message"] == model_downloads._REVOKED_MESSAGE
        if action in {"make", "make_direct"}:
            downloads.makeModelCurrent(TONE.id)
        else:
            downloads.switchModelWithPause(TONE.id)
        assert (TONE.id, TONE.revision) in checked
        assert port.current == (GIGAAM.id, GIGAAM.revision)
        set_current.assert_not_called()
        assert switcher.calls == []
        assert card(downloads, TONE.id)["state"] == "failed"
    finally:
        downloads.shutdown()


@pytest.mark.parametrize("action", ["make", "pause"])
def test_snapshot_without_revocation_allows_switch(action: str) -> None:
    port = FakeManagedPort()
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.records[(TONE.id, TONE.revision)] = "ok"
    port.current = (GIGAAM.id, GIGAAM.revision)
    port.is_revoked = None  # type: ignore[assignment]
    switcher = FakeSwitchPort(enough_memory=True)
    downloads = ModelDownloads(port, switcher=switcher, revoked_check=lambda _id, _rev: False)
    try:
        if action == "make":
            downloads.makeModelCurrent(TONE.id)
            assert switcher.calls == [(TONE.min_ram_mb, False)]
        else:
            downloads.switchModelWithPause(TONE.id)
            assert switcher.calls == [(TONE.min_ram_mb, True)]
        assert port.current == (TONE.id, TONE.revision)
    finally:
        downloads.shutdown()


def test_snapshot_checks_installed_revision_before_switch() -> None:
    port = FakeManagedPort()
    old_revision = "r0"
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.records[(TONE.id, old_revision)] = "ok"
    port.current = (GIGAAM.id, GIGAAM.revision)
    port.is_revoked = None  # type: ignore[assignment]
    checked: list[tuple[str, str]] = []

    def revoked(model_id: str, revision: str) -> bool:
        checked.append((model_id, revision))
        return (model_id, revision) == (TONE.id, old_revision)

    downloads = ModelDownloads(port, revoked_check=revoked)
    try:
        assert card(downloads, TONE.id)["state"] == "failed"
        downloads.makeModelCurrent(TONE.id)
        assert port.current == (GIGAAM.id, GIGAAM.revision)
        assert (TONE.id, old_revision) in checked
        assert (TONE.id, TONE.revision) not in checked
    finally:
        downloads.shutdown()


@pytest.mark.parametrize("with_switcher", [False, True])
def test_catalog_revokes_installed_old_revision_without_snapshot(with_switcher: bool) -> None:
    port = FakeManagedPort()
    old_revision = "r0"
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.records[(TONE.id, old_revision)] = "ok"
    port.current = (GIGAAM.id, GIGAAM.revision)
    revoked_revision = Mock(
        side_effect=lambda model_id, revision: (model_id, revision) == (TONE.id, old_revision)
    )
    port.revoked_revision = revoked_revision  # type: ignore[method-assign]
    set_current = Mock(wraps=port.set_current)
    port.set_current = set_current  # type: ignore[method-assign]
    switcher = FakeSwitchPort(enough_memory=True)
    downloads = ModelDownloads(port, switcher=switcher if with_switcher else None)
    try:
        assert card(downloads, TONE.id)["state"] == "failed"
        revoked_revision.assert_any_call(TONE.id, old_revision)
        downloads.makeModelCurrent(TONE.id)
        assert port.current == (GIGAAM.id, GIGAAM.revision)
        set_current.assert_not_called()
        assert switcher.calls == []
    finally:
        downloads.shutdown()


def test_revocation_failures_warn_once_per_source(caplog: pytest.LogCaptureFixture) -> None:
    port = FakeManagedPort()
    port.is_revoked = Mock(side_effect=StoreError("catalog unavailable"))  # type: ignore[method-assign]

    def failed_snapshot(_model_id: str, _revision: str) -> bool:
        raise RevocationUnknown("snapshot unavailable")

    downloads = ModelDownloads(port, revoked_check=failed_snapshot)
    try:
        with caplog.at_level(logging.DEBUG, logger=model_downloads.__name__):
            for _ in range(3):
                card(downloads, TONE.id)
                downloads.modelsChanged.emit()
        warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert [record.message for record in warnings] == [
            "Не удалось проверить отзыв версии модели",
            "Не удалось проверить отзыв версии модели по снимку",
        ]
        assert all(record.exc_info is not None for record in warnings)
        assert any(record.levelno == logging.DEBUG for record in caplog.records)
    finally:
        downloads.shutdown()


@pytest.mark.parametrize("error", [RevocationUnknown, OSError])
def test_snapshot_check_failure_keeps_previous_behavior(error: type[Exception]) -> None:
    port = FakeManagedPort()
    port.records[(TONE.id, TONE.revision)] = "ok"
    port.is_revoked = None  # type: ignore[assignment]

    def failed_check(_model_id: str, _revision: str) -> bool:
        raise error("snapshot unavailable")

    downloads = ModelDownloads(port, revoked_check=failed_check)
    try:
        assert card(downloads, TONE.id)["state"] == "installed"
        downloads.makeModelCurrent(TONE.id)
        assert port.current == (TONE.id, TONE.revision)
    finally:
        downloads.shutdown()


def test_installed_count_matches_summary(rig: Rig) -> None:
    """Цифра значка сайдбара — то же число, что первое число счётчика шапки."""
    port, downloads, _ = rig
    assert downloads.installedCount == 0
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    assert downloads.installedCount == 1
    port.records[(TONE.id, TONE.revision)] = "broken"
    assert downloads.installedCount == 2
    assert downloads.installedSummary.startswith("Установлено 2 из 2")


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
    assert card(downloads, TONE.id)["message"] == (
        "Файлы модели не читаются — переустановите" if state == "broken" else ""
    )


def test_make_model_current_explains_store_failure(rig: Rig) -> None:
    port, downloads, _ = rig
    port.records[(TONE.id, TONE.revision)] = "ok"
    port.set_current_error = StoreError("broken-store")

    downloads.makeModelCurrent(TONE.id)

    assert port.current is None
    assert card(downloads, TONE.id)["message"] == model_downloads._CURRENT_FAILED_MESSAGE


def test_switch_without_pause_when_memory_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    port, downloads, switcher = switched_rig(True)
    set_current = Mock(wraps=port.set_current)
    monkeypatch.setattr(port, "set_current", set_current)
    try:
        downloads.makeModelCurrent(TONE.id)
        assert switcher.checked == [TONE.min_ram_mb]
        assert switcher.calls == [(TONE.min_ram_mb, False)]
        assert port.current == (TONE.id, TONE.revision)
        assert card(downloads, TONE.id)["state"] == "switching"
        downloads.removeModel(TONE.id)
        assert port.removed == []

        switcher.finish("ok")
        set_current.assert_called_once_with(TONE.id, TONE.revision)
        assert card(downloads, TONE.id)["state"] == "installed"
        assert card(downloads, TONE.id)["badge"] == "active"
        downloads.makeModelCurrent(TONE.id)
        assert switcher.calls == [(TONE.min_ram_mb, False)]
    finally:
        downloads.shutdown()


def test_switch_offers_pause_when_free_memory_is_low() -> None:
    port, downloads, switcher = switched_rig(False)
    try:
        downloads.makeModelCurrent(TONE.id)
        item = card(downloads, TONE.id)
        assert port.current == (GIGAAM.id, GIGAAM.revision)
        assert switcher.calls == []
        assert item["canSwitchWithPause"] is True
        assert item["hintKind"] == "warning"
        assert "свободной памяти" in item["hint"]
        assert "5 секунд" in item["hint"]
        assert item["message"] == ""

        downloads.switchModelWithPause(TONE.id)
        assert switcher.calls == [(TONE.min_ram_mb, True)]
        assert port.current == (TONE.id, TONE.revision)
        assert card(downloads, TONE.id)["state"] == "switching"
        assert card(downloads, TONE.id)["canSwitchWithPause"] is False
        switcher.finish("ok")
    finally:
        downloads.shutdown()


def test_failed_switch_restores_previous_working_model() -> None:
    port, downloads, switcher = switched_rig(True)
    try:
        downloads.makeModelCurrent(TONE.id)
        switcher.finish("failed")

        assert port.current == (GIGAAM.id, GIGAAM.revision)
        assert card(downloads, GIGAAM.id)["badge"] == "active"
        item = card(downloads, TONE.id)
        assert item["state"] == "installed"
        assert item["message"] == "Не удалось загрузить модель. Рабочая модель не изменилась."
    finally:
        downloads.shutdown()


def test_failed_switch_reports_confirmed_memory_shortage() -> None:
    port, downloads, switcher = switched_rig(True)
    lighter = replace(TONE, min_ram_mb=300)
    port.catalog = (GIGAAM, lighter)
    downloads._entries = port.catalog
    port.current = (lighter.id, lighter.revision)
    switcher.available_mb = 600
    try:
        downloads.makeModelCurrent(GIGAAM.id)
        switcher.finish("failed")

        item = card(downloads, GIGAAM.id)
        assert port.current == (lighter.id, lighter.revision)
        assert item["message"] == "Недостаточно памяти — нужно около 768 МБ"
        assert item["hint"] == ""
        assert item["hintKind"] == ""
        assert item["memoryShortage"] is False
    finally:
        downloads.shutdown()


def test_failed_switch_with_enough_available_memory_keeps_general_error() -> None:
    port, downloads, switcher = switched_rig(True)
    switcher.available_mb = TONE.min_ram_mb
    try:
        downloads.makeModelCurrent(TONE.id)
        switcher.finish("failed")
        item = card(downloads, TONE.id)
        assert item["message"] == model_downloads._SWITCH_FAILED_MESSAGE
        assert item["hint"] == ""
    finally:
        downloads.shutdown()


def test_failed_switch_without_lighter_model_has_no_hint() -> None:
    port = FakeManagedPort()
    port.catalog = (GIGAAM,)
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    switcher = FakeSwitchPort(enough_memory=True)
    switcher.available_mb = 100
    downloads = ModelDownloads(port, switcher=switcher)
    try:
        downloads.makeModelCurrent(GIGAAM.id)
        switcher.finish("failed")
        item = card(downloads, GIGAAM.id)
        assert item["message"] == "Недостаточно памяти — нужно около 768 МБ"
        assert item["hint"] == ""
        assert item["hintKind"] == ""
    finally:
        downloads.shutdown()


def test_failed_first_switch_suggests_lighter_catalog_model() -> None:
    port = FakeManagedPort()
    port.catalog = (GIGAAM, replace(TONE, min_ram_mb=300))
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    switcher = FakeSwitchPort(enough_memory=True)
    switcher.available_mb = 600
    downloads = ModelDownloads(port, switcher=switcher)
    try:
        downloads.makeModelCurrent(GIGAAM.id)
        switcher.finish("failed")
        item = card(downloads, GIGAAM.id)
        assert item["message"] == "Недостаточно памяти — нужно около 768 МБ"
        assert item["hint"] == "Попробуйте более лёгкую модель: T-one"
        assert item["hintKind"] == "warning"
        assert item["memoryShortage"] is True
    finally:
        downloads.shutdown()


def test_failed_switch_skips_broken_lighter_model() -> None:
    port = FakeManagedPort()
    lighter = replace(TONE, min_ram_mb=300)
    fallback = replace(TONE, id="fallback", revision="r2", name="Другая модель", min_ram_mb=400)
    port.catalog = (GIGAAM, lighter, fallback)
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.records[(lighter.id, lighter.revision)] = "broken"
    switcher = FakeSwitchPort(enough_memory=True)
    switcher.available_mb = 600
    downloads = ModelDownloads(port, switcher=switcher)
    try:
        downloads.makeModelCurrent(GIGAAM.id)
        switcher.finish("failed")
        assert card(downloads, GIGAAM.id)["hint"] == "Попробуйте более лёгкую модель: Другая модель"
    finally:
        downloads.shutdown()


def test_failed_switch_store_error_still_notifies_and_keeps_message() -> None:
    port = FakeManagedPort()
    lighter = replace(TONE, min_ram_mb=300)
    port.catalog = (GIGAAM, lighter)
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.records[(lighter.id, lighter.revision)] = "ok"
    original = port.record_state
    switcher = FakeSwitchPort(enough_memory=True)
    switcher.available_mb = 100
    downloads = ModelDownloads(port, switcher=switcher)
    failed_once = False

    def flaky_state(model_id: str, revision: str) -> str:
        nonlocal failed_once
        if model_id == lighter.id and not failed_once:
            failed_once = True
            raise StoreError("unavailable")
        return original(model_id, revision)

    port.record_state = Mock(side_effect=flaky_state)  # type: ignore[method-assign]
    changed = QSignalSpy(downloads.modelsChanged)
    try:
        downloads.makeModelCurrent(GIGAAM.id)
        switcher.finish("failed")
        item = card(downloads, GIGAAM.id)
        assert len(changed) >= 2
        assert downloads._switching_entry is None
        assert item["state"] != "switching"
        assert item["message"]
        assert item["hint"] == ""
        assert item["hintKind"] == ""
    finally:
        downloads.shutdown()


@pytest.mark.parametrize("result", ["ok", "failed"])
def test_recheck_waits_for_switch_to_finish(result: str) -> None:
    port, downloads, switcher = switched_rig(True)
    pending = TONE
    downloads._recheck_queue = [pending]
    try:
        downloads.makeModelCurrent(TONE.id)
        assert downloads._switching_entry is not None
        downloads._start_next_recheck()
        assert downloads._recheck_queue == [pending]
        assert downloads._rechecking is False

        started: list[Any] = []

        def start_pending() -> None:
            started.extend(downloads._recheck_queue)
            downloads._recheck_queue.clear()

        downloads._start_next_recheck = start_pending  # type: ignore[method-assign]
        switcher.finish(result)
        assert started == [pending]
        assert downloads._switching_entry is None
    finally:
        downloads.shutdown()


def test_failed_switch_memory_reader_error_keeps_general_message() -> None:
    port, downloads, switcher = switched_rig(True)
    switcher.mem_available_mb = Mock(side_effect=RuntimeError("private data"))  # type: ignore[method-assign]
    try:
        downloads.makeModelCurrent(TONE.id)
        switcher.finish("failed")
        item = card(downloads, TONE.id)
        assert item["message"] == model_downloads._SWITCH_FAILED_MESSAGE
        assert item["memoryShortage"] is False
    finally:
        downloads.shutdown()


def test_removed_catalog_model_stays_manageable(rig: Rig) -> None:
    port, downloads, queued = rig
    removed = replace(
        TONE,
        id="old-model",
        revision="r1",
        name="old-model",
        min_ram_mb=GIGAAM.min_ram_mb,
        removed_from_catalog=True,
    )
    port.catalog = (*port.catalog, removed)
    downloads._entries = port.catalog
    port.records[(removed.id, removed.revision)] = "ok"
    item = card(downloads, removed.id)
    assert item["state"] == "removed-from-catalog"
    assert item["name"] == removed.id
    assert item["badge"] == "installed"
    assert item["message"] == ""
    assert item["hint"] == "Снята с каталога — обновлений не будет"
    assert item["hintKind"] == "info"
    assert item["memoryShortage"] is False
    assert item["canReinstall"] is False
    assert item["updateAvailable"] is False
    assert item["ramText"] == ""
    assert item["ramMb"] == 0
    assert item["tags"] == []
    assert all(not metric["hasData"] for metric in item["metrics"])
    downloads.updateModel(removed.id)
    assert queued == []
    downloads.makeModelCurrent(removed.id)
    assert port.current == (removed.id, removed.revision)
    assert card(downloads, removed.id)["badge"] == "active"
    port.current = None
    downloads.removeModel(removed.id)
    assert port.removed == [(removed.id, removed.revision)]


def test_removed_catalog_card_shows_local_memory_measurement(rig: Rig) -> None:
    port, downloads, _ = rig
    removed = replace(
        TONE, id="old-model", revision="r1", name="old-model", removed_from_catalog=True
    )
    port.catalog = (*port.catalog, removed)
    downloads._entries = port.catalog
    port.records[(removed.id, removed.revision)] = "ok"
    downloads._measurements_cache = {"old-model@r1": {"threads": 2, "ram_mb": 512}}

    item = card(downloads, removed.id)

    assert item["ramMeasured"] is True
    assert item["ramMb"] == 512
    assert item["ramText"] == "512 МБ"


def test_removed_catalog_shortage_message_has_no_estimate() -> None:
    port = FakeManagedPort()
    removed = replace(
        TONE,
        id="old-model",
        revision="r1",
        name="old-model",
        min_ram_mb=GIGAAM.min_ram_mb,
        removed_from_catalog=True,
    )
    lighter = replace(TONE, min_ram_mb=300)
    port.catalog = (lighter, removed)
    port.records[(removed.id, removed.revision)] = "ok"
    switcher = FakeSwitchPort(enough_memory=True)
    switcher.available_mb = 100
    downloads = ModelDownloads(port, switcher=switcher)
    try:
        downloads.makeModelCurrent(removed.id)
        switcher.finish("failed")
        item = card(downloads, removed.id)
        assert item["message"] == "Недостаточно памяти для этой модели"
        assert item["hint"] == "Попробуйте более лёгкую модель: T-one"
        assert item["hintKind"] == "warning"
    finally:
        downloads.shutdown()


def test_service_appends_only_non_revoked_models_missing_from_catalog() -> None:
    service = ModelService.__new__(ModelService)
    record = ModelRecord("old-model", "r1", Path("/unused"), "layout", "int8", 42_000_000)
    revoked = replace(record, id="revoked-model")
    service._store = Mock(records=Mock(return_value=[record, revoked]))
    service._catalog = Mock(entries=(GIGAAM,))
    service._catalog.is_revoked.side_effect = lambda model_id, _revision: (
        model_id == "revoked-model"
    )

    entries = service.entries()

    assert [entry.id for entry in entries] == [GIGAAM.id, record.id]
    assert entries[-1].removed_from_catalog is True
    assert entries[-1].name == record.id
    assert entries[-1].size_bytes == record.size_bytes
    assert entries[-1].min_ram_mb == GIGAAM.min_ram_mb


@pytest.mark.parametrize(
    ("current_revision", "states", "expected"),
    [
        ("r2", ("ok", "ok"), "r2"),
        (None, ("broken", "ok"), "r2"),
    ],
)
def test_service_selects_one_removed_revision(
    current_revision: str | None, states: tuple[ModelState, ModelState], expected: str
) -> None:
    service = ModelService.__new__(ModelService)
    r1 = ModelRecord("old-model", "r1", Path("/unused"), "layout", "int8", 41_000_000, states[0])
    r2 = replace(r1, revision="r2", size_bytes=42_000_000, state=states[1])
    service._store = Mock()
    service._store.records.return_value = [r1, r2]
    service._store.current.return_value = r2 if current_revision == "r2" else None
    service._catalog = Mock(entries=(GIGAAM,))
    service._catalog.is_revoked.return_value = False

    entries = service.entries()

    assert [entry.id for entry in entries] == [GIGAAM.id, "old-model"]
    assert entries[1].revision == expected
    assert entries[1].name == "old-model"


def test_removed_model_two_revisions_have_one_manageable_card() -> None:
    service = ModelService.__new__(ModelService)
    r1 = ModelRecord("old-model", "r1", Path("/unused"), "layout", "int8", 41_000_000)
    r2 = replace(r1, revision="r2", size_bytes=42_000_000)
    service._store = Mock()
    service._store.records.return_value = [r1, r2]
    service._store.current.return_value = r1
    service._catalog = Mock(entries=(GIGAAM,))
    service._catalog.is_revoked.return_value = False
    port = FakeManagedPort()
    port.catalog = service.entries()
    port.records[(r1.id, r1.revision)] = "ok"
    port.records[(r2.id, r2.revision)] = "ok"
    downloads = ModelDownloads(port)
    try:
        cards = [item for item in downloads.models if item["id"] == r1.id]
        assert len(cards) == 1
        assert cards[0]["name"] == r1.id
        assert downloads.installedSummary.startswith("Установлено 1 из 2")
        downloads.makeModelCurrent(r1.id)
        assert port.current == (r1.id, r1.revision)
        port.current = None
        downloads.removeModel(r1.id)
        assert port.removed == [(r1.id, r1.revision)]
        assert port.records[(r2.id, r2.revision)] == "ok"
    finally:
        downloads.shutdown()


def test_removed_catalog_model_uses_fallback_memory_and_skips_recheck() -> None:
    service = ModelService.__new__(ModelService)
    record = ModelRecord(
        "old-model", "r1", Path("/unused"), "layout", "int8", 42_000_000, recheck=True
    )
    service._store = Mock(records=Mock(return_value=[record]))
    service._catalog = Mock(entries=())
    service._catalog.is_revoked.return_value = False

    entries = service.entries()

    assert entries[0].min_ram_mb == 768
    assert service.recheck_entries() == ()


def test_removed_catalog_switch_succeeds_and_bridge_hides_raw_id() -> None:
    port = FakeManagedPort()
    removed = replace(
        TONE, id="old-model", revision="r1", name="old-model", removed_from_catalog=True
    )
    port.catalog = (GIGAAM, removed)
    port.records[(removed.id, removed.revision)] = "ok"
    switcher = FakeSwitchPort(enough_memory=True)
    downloads = ModelDownloads(port, switcher=switcher)
    bridge = SettingsBridge(settings_mod.from_dict({}), downloads=downloads, save=Mock())
    try:
        downloads.makeModelCurrent(removed.id)
        assert card(downloads, removed.id)["state"] == "switching"
        switcher.finish("ok")
        assert port.current == (removed.id, removed.revision)
        assert card(downloads, removed.id)["badge"] == "active"
        assert card(downloads, removed.id)["state"] == "removed-from-catalog"
        assert bridge.activeModelName == "Установленная модель"
        queued = Mock()
        downloads._begin_queue = queued  # type: ignore[method-assign]
        assert downloads.can_reinstall() is False
        downloads.reinstall_active()
        queued.assert_not_called()
    finally:
        downloads.shutdown()


def test_removed_catalog_switch_failure_keeps_message() -> None:
    port = FakeManagedPort()
    removed = replace(TONE, id="old-model", revision="r1", removed_from_catalog=True)
    port.catalog = (GIGAAM, removed)
    port.records[(removed.id, removed.revision)] = "ok"
    switcher = FakeSwitchPort(enough_memory=True)
    downloads = ModelDownloads(port, switcher=switcher)
    try:
        downloads.makeModelCurrent(removed.id)
        switcher.finish("failed")
        item = card(downloads, removed.id)
        assert item["message"] == model_downloads._FIRST_SWITCH_FAILED_MESSAGE
        assert item["hint"] == ""
    finally:
        downloads.shutdown()


def test_removed_catalog_remove_failure_keeps_message(rig: Rig) -> None:
    port, downloads, _ = rig
    removed = replace(TONE, id="old-model", revision="r1", removed_from_catalog=True)
    port.catalog = (*port.catalog, removed)
    downloads._entries = port.catalog
    port.records[(removed.id, removed.revision)] = "ok"
    port.remove_error = OSError("private path")

    downloads.removeModel(removed.id)

    item = card(downloads, removed.id)
    assert item["message"] == model_downloads._REMOVE_FAILED_MESSAGE
    assert item["hint"] == ""


def test_removed_catalog_broken_card_only_offers_removal(rig: Rig) -> None:
    port, downloads, queued = rig
    removed = replace(TONE, id="old-model", revision="r1", removed_from_catalog=True)
    port.catalog = (*port.catalog, removed)
    downloads._entries = port.catalog
    port.records[(removed.id, removed.revision)] = "broken"

    item = card(downloads, removed.id)
    assert item["state"] == "broken"
    assert item["canReinstall"] is False
    assert item["message"] == "Файлы модели не читаются"
    downloads._card_states[removed.id] = "verifying"
    assert card(downloads, removed.id)["state"] == "verifying"
    downloads._card_states[removed.id] = "removed-from-catalog"
    assert card(downloads, removed.id)["state"] == "broken"
    downloads.reinstallModel(removed.id)
    assert queued == []


def test_removed_catalog_pause_hint_takes_priority() -> None:
    port = FakeManagedPort()
    removed = replace(TONE, id="old-model", revision="r1", removed_from_catalog=True)
    port.catalog = (GIGAAM, removed)
    port.records[(removed.id, removed.revision)] = "ok"
    switcher = FakeSwitchPort(enough_memory=False)
    downloads = ModelDownloads(port, switcher=switcher)
    try:
        downloads.makeModelCurrent(removed.id)
        item = card(downloads, removed.id)
        assert item["canSwitchWithPause"] is True
        assert item["hint"] == model_downloads._SWITCH_PAUSE_HINT
        assert item["hintKind"] == "warning"
    finally:
        downloads.shutdown()


def test_failed_first_switch_does_not_claim_previous_model() -> None:
    port = FakeManagedPort()
    port.records[(TONE.id, TONE.revision)] = "ok"
    switcher = FakeSwitchPort(enough_memory=True)
    downloads = ModelDownloads(port, switcher=switcher)
    try:
        downloads.makeModelCurrent(TONE.id)
        switcher.finish("failed")
        assert card(downloads, TONE.id)["message"] == (
            "Не удалось загрузить модель. Попробуйте ещё раз."
        )
    finally:
        downloads.shutdown()
    assert switcher.on_switch_finished is None


def test_active_model_drops_pause_hint_and_reads_memory_once(rig: Rig) -> None:
    port, downloads, _ = rig
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.current = (GIGAAM.id, GIGAAM.revision)
    downloads._card_hints[GIGAAM.id] = model_downloads._SWITCH_PAUSE_HINT
    read = Mock(return_value=1000.0)
    port.mem_total_mb = read  # type: ignore[method-assign]

    models = downloads.models
    active = next(item for item in models if item["id"] == GIGAAM.id)
    assert active["hint"] == ""
    assert active["canSwitchWithPause"] is False
    read.assert_called_once_with()


def test_low_total_memory_is_warning_not_error(rig: Rig) -> None:
    port, downloads, _ = rig
    port.total_ram_mb = 1024
    item = card(downloads, TONE.id)
    assert item["state"] == "available"
    assert item["message"] == ""
    assert item["hintKind"] == "warning"
    assert "может не хватить" in item["hint"]


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
    assert item["hint"] == "Освобождено 101 МБ на диске"
    assert item["hintKind"] == "info"
    assert downloads.installedSummary == "Установлено 1 из 2 · 226 МБ на диске"
    assert len(ready) == 1


def test_remove_model_removes_the_installed_old_revision(rig: Rig) -> None:
    port, downloads, _ = rig
    port.records[(GIGAAM.id, "r1")] = "ok"

    downloads.removeModel(GIGAAM.id)

    assert port.removed == [(GIGAAM.id, "r1")]


def test_remove_old_revision_uses_its_saved_size(rig: Rig) -> None:
    port, downloads, _ = rig
    port.records[(GIGAAM.id, "r1")] = "ok"
    port.record_size_bytes = Mock(return_value=91_000_000)  # type: ignore[method-assign]

    downloads.removeModel(GIGAAM.id)

    port.record_size_bytes.assert_called_once_with(GIGAAM.id, "r1")
    assert card(downloads, GIGAAM.id)["hint"] == "Освобождено 91 МБ на диске"


def test_remove_model_refuses_the_working_model(rig: Rig) -> None:
    port, downloads, _ = rig
    port.records[(GIGAAM.id, GIGAAM.revision)] = "ok"
    port.current = (GIGAAM.id, GIGAAM.revision)
    port.record_size_bytes = Mock(side_effect=AssertionError("active removal reached storage"))  # type: ignore[method-assign]
    changed = QSignalSpy(downloads.modelsChanged)
    before = card(downloads, GIGAAM.id)

    downloads.removeModel(GIGAAM.id)
    downloads.removeModel("неизвестная")

    assert len(changed) == 0
    assert card(downloads, GIGAAM.id) == before
    port.record_size_bytes.assert_not_called()
    assert port.removed == []
    assert card(downloads, GIGAAM.id)["badge"] == "active"


def test_broken_installed_model_can_be_reinstalled(rig: Rig) -> None:
    port, downloads, queued = rig
    port.records[(TONE.id, TONE.revision)] = "broken"
    assert card(downloads, TONE.id)["state"] == "broken"
    assert card(downloads, TONE.id)["badge"] == "installed"

    downloads.reinstallModel(TONE.id)
    assert queued == [(TONE,)]


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
    downloads.installedCount = 1

    assert bridge.installedSummary == "Установлено 1 из 12 · 226 МБ на диске"
    assert bridge.installedCount == 1
    bridge.makeModelCurrent("t-one")
    bridge.switchModelWithPause("t-one")
    bridge.removeModel("t-one")
    bridge.reinstallModel("t-one")
    bridge.updateModel("t-one")

    downloads.makeModelCurrent.assert_called_once_with("t-one")
    downloads.switchModelWithPause.assert_called_once_with("t-one")
    downloads.removeModel.assert_called_once_with("t-one")
    downloads.reinstallModel.assert_called_once_with("t-one")
    downloads.updateModel.assert_called_once_with("t-one")


def test_settings_bridge_without_downloads_is_silent() -> None:
    bridge = SettingsBridge(settings_mod.from_dict({}), save=Mock())

    assert bridge.installedSummary == ""
    assert bridge.installedCount == 0
    bridge.makeModelCurrent("t-one")
    bridge.switchModelWithPause("t-one")
    bridge.removeModel("t-one")
    bridge.reinstallModel("t-one")
    bridge.updateModel("t-one")


def test_model_service_delegates_removal_to_the_store() -> None:
    service = ModelService.__new__(ModelService)
    store = Mock()
    service._store = store

    service.remove("t-one", "r1")

    store.remove.assert_called_once_with("t-one", "r1")


def test_model_service_reads_installed_revision_size() -> None:
    service = ModelService.__new__(ModelService)
    store = Mock()
    store.records.return_value = [
        Mock(id="t-one", revision="r1", size_bytes=91_000_000),
        Mock(id="t-one", revision="r2", size_bytes=144_000_000),
    ]
    service._store = store

    assert service.record_size_bytes("t-one", "r1") == 91_000_000


def test_model_service_reads_total_memory_from_store() -> None:
    service = ModelService.__new__(ModelService)
    store = Mock()
    store.mem_total_mb.return_value = 8192.0
    service._store = store

    assert service.mem_total_mb() == 8192.0
    store.mem_total_mb.assert_called_once_with()
