"""Подставной загрузчик моделей для живой связки «очередь → мост → карточка».

Сети и файлов нет, но очередь работает по-настоящему: задание идёт в рабочем
потоке ``ModelDownloads``, события доходят до GUI через канал задания и
пробуждение Qt, а карточки и полоса загрузки читают их через ``SettingsBridge``.
Тест придерживает загрузку и установку событиями ``threading.Event``, чтобы
разглядеть промежуточные состояния: «Загружается», «Проверяю…», пауза по месту.

Рабочий поток получает от GUI только значения и сам Qt не трогает (уроки 025–026).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from PyQt5.QtCore import QCoreApplication, QEventLoop

from astra_voice.models.catalog import CatalogEntry
from astra_voice.models.downloader import DownloadError, Progress
from astra_voice.models.installer import InstallResult

FIRST = CatalogEntry(
    id="live-first",
    revision="r1",
    name="Первая модель",
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
SECOND = replace(
    FIRST,
    id="live-second",
    name="Вторая модель",
    description="Русская, лёгкая",
    size_bytes=144_000_000,
    recommended=False,
)

#: Рабочий поток никогда не ждёт дольше: зависший тест не держит процесс.
_WORKER_WAIT_S = 10.0


class ScriptedModelPort:
    """``ModelPort`` без сети: исход каждой попытки задаёт тест."""

    def __init__(self, entries: tuple[CatalogEntry, ...] = (FIRST, SECOND)) -> None:
        self.catalog = entries
        #: (идентификатор, ревизия) → состояние записи в хранилище.
        self.records: dict[tuple[str, str], str] = {}
        self.current: tuple[str, str] | None = None
        self.space = True
        #: Место для рабочего потока, если оно расходится с тем, что видел выбор:
        #: диск заполнился между «Скачать выбранное» и началом загрузки.
        self.job_space: bool | None = None
        self.missing_bytes = 12_500_000
        self.source_kind = "hf"
        #: Одноразовые отказы загрузки: идентификатор → ошибка.
        self.failures: dict[str, DownloadError] = {}
        # Закрытые ворота держат рабочий поток на загрузке и на установке.
        self.download_gate = threading.Event()
        self.install_gate = threading.Event()
        #: Рабочий поток отдал прогресс и ждёт ворот загрузки.
        self.downloading = threading.Event()
        self.download_calls: list[str] = []
        self.discarded: list[str] = []
        self.worker_threads: set[int] = set()

    # ── управление из теста ──────────────────────────────────────────────
    def open_gates(self) -> None:
        self.download_gate.set()
        self.install_gate.set()

    # ── то, что спрашивает ModelDownloads ────────────────────────────────
    def recommended(self) -> CatalogEntry:
        return self.catalog[0]

    def entries(self) -> tuple[CatalogEntry, ...]:
        return self.catalog

    def is_revoked(self, entry: Any) -> bool:
        return False

    def revoked_revision(self, model_id: str, revision: str) -> bool:
        return False

    def catalog_rtfx(self, model_id: str) -> float | None:
        return None

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
        entry = next((entry for entry in self.catalog if entry.id == model_id), None)
        return entry.size_bytes if entry is not None else 0

    def current_ids(self) -> tuple[str, str] | None:
        return self.current

    def installed_ids(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.records)

    def set_current(self, model_id: str, revision: str) -> None:
        self.current = (model_id, revision)

    def remove(self, model_id: str, revision: str) -> None:
        raise AssertionError("Удаление в этих тестах не запускается")

    def installed_ok(self) -> bool:
        return any(state == "ok" for state in self.records.values())

    def broken(self) -> bool:
        return any(state == "broken" for state in self.records.values())

    def allowed(self) -> tuple[bool, str]:
        return True, ""

    def refusal(self) -> str:
        return ""

    def disk_ok(self, size_bytes: int) -> bool:
        if self.job_space is not None and threading.current_thread() is not threading.main_thread():
            return self.job_space
        return self.space

    def remaining_bytes(self, entry: Any) -> int:
        return int(entry.size_bytes)

    def disk_missing_bytes(self, size_bytes: int) -> int:
        return self.missing_bytes

    def free_bytes(self) -> int:
        return 42_100_000_000

    def ram_ok(self, min_ram_mb: int) -> bool:
        return True

    def mem_total_mb(self) -> float | None:
        return None

    def download(
        self,
        entry: Any,
        *,
        progress: Callable[[Progress], None],
        cancel: threading.Event,
        source: Callable[[str], None] | None = None,
    ) -> Path:
        self.worker_threads.add(threading.get_ident())
        self.download_calls.append(entry.id)
        if source is not None:
            source(self.source_kind)
        half = entry.size_bytes // 2
        progress(Progress(half, entry.size_bytes, 5_200_000.0, 22.0, 1, 1))
        self.downloading.set()
        _wait_gate(self.download_gate, cancel)
        failure = self.failures.pop(entry.id, None)
        if failure is not None:
            raise failure
        progress(Progress(entry.size_bytes, entry.size_bytes, 5_200_000.0, 0.0, 1, 1))
        return Path("/nonexistent/staging") / str(entry.id)

    def discard_staging(self, entry: Any) -> None:
        self.discarded.append(entry.id)

    def install_from_staging(self, entry: Any) -> InstallResult:
        self.worker_threads.add(threading.get_ident())
        # У установки нет отмены: ворота держат её до команды теста.
        _wait_gate(self.install_gate, None)
        self.records[(entry.id, entry.revision)] = "ok"
        # Как у хранилища: первая исправная модель становится рабочей.
        if self.current is None:
            self.current = (entry.id, entry.revision)
        return InstallResult("ok")

    def install_from_path(self, source: Path, entry: Any) -> InstallResult:
        raise AssertionError("Установка из папки в этих тестах не запускается")


def _wait_gate(gate: threading.Event, cancel: threading.Event | None) -> None:
    deadline = time.monotonic() + _WORKER_WAIT_S
    while not gate.wait(0.005):
        if cancel is not None and cancel.is_set():
            raise DownloadError("cancelled")
        if time.monotonic() > deadline:
            raise DownloadError("timeout")


def pump_until(predicate: Callable[[], bool], what: str, timeout_s: float = 5.0) -> None:
    """Крутит события GUI, пока условие не выполнится; сообщения задания приходят так же."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"не дождались: {what}")
        QCoreApplication.processEvents(QEventLoop.AllEvents, 20)
        time.sleep(0.002)
