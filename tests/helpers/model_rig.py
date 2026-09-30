"""Тестовый порт моделей и стенд контроллера онбординга для тестов загрузчика.

Общие для unit (test_bridges, test_model_job_threads), нагрузочного прогона
(tests/stress) и подпроцессных сценариев (model_job_lifetime): раньше они
импортировали всё это из модуля тестов test_bridges.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol
from unittest.mock import Mock

from PyQt5.QtCore import QCoreApplication, QEvent

from astra_voice.core.settings import Settings
from astra_voice.models.catalog import CatalogEntry
from astra_voice.models.downloader import DownloadError, Progress
from astra_voice.models.installer import InstallResult
from astra_voice.ui.bridges import OnboardingController, SettingsBridge
from astra_voice.ui.model_downloads import ModelPort


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
        self.discarded: list[str] = []
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

    def catalog_rtfx(self, model_id: str) -> float | None:
        return None

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

    def refusal(self) -> str:
        return "" if self.network else "admin"

    def disk_ok(self, size_bytes: int) -> bool:
        assert size_bytes == self.entry.size_bytes
        return self.space

    def remaining_bytes(self, entry: CatalogEntry) -> int:
        return entry.size_bytes

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
        self,
        entry: CatalogEntry,
        *,
        progress: Callable[[Progress], None],
        cancel: threading.Event,
        source: Callable[[str], None] | None = None,
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

    def discard_staging(self, entry: CatalogEntry) -> None:
        self.discarded.append(entry.id)

    def install_from_staging(self, entry: CatalogEntry) -> InstallResult:
        self.ready = self.result.state == "ok"
        return self.result

    def install_from_path(self, source: Path, entry: CatalogEntry) -> InstallResult:
        self.sources.append(source)
        return self.install_from_staging(entry)


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


def model_rig_session() -> Iterator[ModelRig]:
    """Порт и фабрика контроллеров онбординга; после единственного yield — уборка.

    Фикстура model_rig в test_bridges — обёртка над этим генератором; нагрузочный
    прогон вызывает его напрямую на каждой итерации.
    """
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
