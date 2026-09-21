"""Настройки для QML: немедленное сохранение, откат и живое применение."""

from __future__ import annotations

import errno
import hmac
import logging
import math
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol, cast

from PyQt5.QtCore import QEvent, QObject, Qt, QThread, QUrl, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import QFileDialog

from astra_voice.core import paths
from astra_voice.core.dictation import (
    LEVEL_FAILED,
    TEST_FAILED,
    LevelCallback,
    MicrophoneLevelUpdate,
    MicrophoneTestUpdate,
    TestCallback,
    level_from_dbfs,
)
from astra_voice.core.model_source import SmokeRunner
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings, is_valid_combo
from astra_voice.core.settings import save as settings_save
from astra_voice.core.version import __version__
from astra_voice.models.catalog import CatalogEntry, load_builtin
from astra_voice.models.downloader import Downloader, DownloadError, Progress
from astra_voice.models.installer import Installer, InstallResult, SmokeCheck, SmokeResult
from astra_voice.models.store import ModelStore, StoreError
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient, NetworkError
from astra_voice.platform.hotkey import DEFAULT_CANDIDATES
from astra_voice.security.verify import Verifier, sha256_file
from astra_voice.ui import notify
from astra_voice.ui.formatting import SpeedTracker, format_eta, format_size, format_speed
from astra_voice.worker.audio import AudioDevice, AudioError, list_devices

log = logging.getLogger(__name__)

_SMOKE_FAILURE = "Модель не прошла пробное распознавание на этом компьютере"
_BROKEN_MESSAGE = "Модель не прошла проверку. Попробуйте скачать или установить её заново."
_SELFCHECK_MESSAGE = "Распознавание на этом компьютере не работает. Обратитесь к администратору"
_ENGINE_FAILED_MESSAGE = "Не удалось запустить распознавание. Попробуйте переустановить модель"
_REVOKED_MESSAGE = (
    "Издатель больше не рекомендует эту версию модели. "
    "Не устанавливайте её — скачайте свежую версию."
)
# После таймаута поток и задание должны оставаться живы до выхода run().
_finishing_model_threads: set[tuple[QThread, _ModelJob | _RecheckJob | None]] = set()


class ModelPort(Protocol):
    """Модель для онбординга; операции установки выполняются в рабочем потоке."""

    def recommended(self) -> Any | None: ...

    def entries(self) -> tuple[Any, ...]: ...

    def recheck_entries(self) -> tuple[Any, ...]: ...

    def verify_files(self, entry: Any) -> tuple[bool, str]: ...

    def smoke(self, entry: Any) -> tuple[bool, str]: ...

    def mark_ok(self, model_id: str, revision: str) -> None: ...

    def mark_broken(self, model_id: str, revision: str, reason: str) -> None: ...

    def record_state(self, model_id: str, revision: str) -> str: ...

    def current_ids(self) -> tuple[str, str] | None: ...

    def installed_ids(self) -> tuple[tuple[str, str], ...]: ...

    def set_current(self, model_id: str, revision: str) -> None: ...

    def installed_ok(self) -> bool: ...

    def broken(self) -> bool: ...

    def allowed(self) -> tuple[bool, str]: ...

    def disk_ok(self, size_bytes: int) -> bool: ...

    def ram_ok(self, min_ram_mb: int) -> bool: ...

    def download(
        self, entry: Any, *, progress: Callable[[Progress], None], cancel: threading.Event
    ) -> Path: ...

    def install_from_staging(self, entry: Any) -> Any: ...

    def install_from_path(self, source: Path, entry: Any) -> Any: ...


class _RecheckUnavailable(Exception):
    """Проверка не дала вердикта о модели; пометку recheck нужно сохранить."""


def make_smoke_check(
    runner: SmokeRunner | None = None,
    *,
    cancel: Callable[[], bool] | None = None,
    require_verdict: bool = False,
) -> SmokeCheck:
    """Адаптирует каталог к model.load; речь и внутренние причины не выходят наружу."""
    smoke = runner if runner is not None else SmokeRunner()

    def check(directory: Path, entry: CatalogEntry) -> SmokeResult:
        request = {
            "type": "model.load",
            "id": entry.id,
            "revision": entry.revision,
            "dir": str(directory),
            "layout": entry.layout,
            "variant": entry.variant,
            "threads": 2,
            "min_ram_mb": entry.min_ram_mb,
        }
        try:
            result = smoke(request, cancel=cancel)
        except Exception:
            if require_verdict:
                raise _RecheckUnavailable from None
            log.warning("Не удалось выполнить пробное распознавание", exc_info=True)
            return SmokeResult(ok=False, reason=_SMOKE_FAILURE)
        if result.ok:
            return SmokeResult(ok=True)
        # Отсутствие ответа/пробной записи и сбой запуска воркера не доказывают
        # брак уже установленной модели. Следующий запуск повторит проверку.
        if require_verdict and result.reason not in {
            "load-failed",
            "no-match",
            "transcribe-failed",
        }:
            raise _RecheckUnavailable
        if result.reason in {"load-failed", "worker-failed", "no-wav", "timeout"}:
            reason = _ENGINE_FAILED_MESSAGE
        elif result.reason in {"no-match", "transcribe-failed"}:
            reason = _SELFCHECK_MESSAGE
        else:
            reason = _SMOKE_FAILURE
        return SmokeResult(ok=False, reason=reason)

    return check


class ModelService:
    """Собирает проверенный каталог, хранилище и разрешённую явную загрузку."""

    def __init__(self, settings: Settings, policy: Policy) -> None:
        keyring = paths.data_dir_static() / "keys" / "release.gpg"
        self._catalog = load_builtin(Verifier("catalog", keyring=keyring))
        self._store = ModelStore()
        self._gate = NetworkGate(settings, policy)
        ca_bundle = policy.values.get("ca_bundle")
        http = HttpClient(
            self._gate,
            ca_bundle=Path(ca_bundle) if ca_bundle else None,
            user_agent=f"astra-voice/{__version__} (+https://github.com/ArrivaRUS/astra-voice)",
        )
        self._downloader = Downloader(http, self._store)
        self._cancel = threading.Event()
        self._installer = Installer(
            self._store,
            make_smoke_check(cancel=lambda: self._cancel.is_set()),
            catalog=self._catalog,
        )

    def set_cancel(self, cancel: threading.Event) -> None:
        """Связывает пробное распознавание с отменой текущей попытки установки."""
        self._cancel = cancel

    def recommended(self) -> CatalogEntry | None:
        return next(
            (
                entry
                for entry in self._catalog.entries
                if entry.recommended and not self._catalog.is_revoked(entry.id, entry.revision)
            ),
            None,
        )

    def entries(self) -> tuple[CatalogEntry, ...]:
        entries = tuple(
            entry
            for entry in self._catalog.entries
            if not self._catalog.is_revoked(entry.id, entry.revision)
        )
        recommended = next((entry for entry in entries if entry.recommended), None)
        if recommended is None:
            return entries
        return (recommended, *(entry for entry in entries if entry is not recommended))

    def record_state(self, model_id: str, revision: str) -> str:
        return next(
            (
                record.state
                for record in self._store.records()
                if (record.id, record.revision) == (model_id, revision)
            ),
            "",
        )

    def recheck_entries(self) -> tuple[CatalogEntry, ...]:
        pending = {
            (record.id, record.revision)
            for record in self._store.records()
            if record.recheck is True
        }
        return tuple(entry for entry in self.entries() if (entry.id, entry.revision) in pending)

    def verify_files(self, entry: Any) -> tuple[bool, str]:
        """Сверяет установленный набор; ошибки чтения не становятся вердиктом."""
        directory = self._store._installed(entry.id, entry.revision)
        names = {file.path for file in entry.files}
        for child in directory.iterdir():
            if child.is_symlink() or not child.is_file() or child.name not in names:
                return False, "В папке есть лишние файлы. Оставьте только файлы выбранной модели."
        for file in entry.files:
            source = directory / file.path
            if (
                not file.path
                or file.path in {".", ".."}
                or Path(file.path).name != file.path
                or "\\" in file.path
                or source.is_symlink()
                or not source.resolve().is_relative_to(directory.resolve())
            ):
                return False, "Файлы в папке не подходят для выбранной модели."
            if not source.is_file():
                return False, "В папке не хватает файлов модели. Получите их заново."
            actual = sha256_file(source, cancel=self._cancel.is_set)
            if not hmac.compare_digest(actual, file.sha256) or source.stat().st_size != file.size:
                return False, (
                    "Файлы модели повреждены: контрольная сумма не совпала. Получите их заново."
                )
        return True, ""

    def smoke(self, entry: Any) -> tuple[bool, str]:
        result = make_smoke_check(cancel=self._cancel.is_set, require_verdict=True)(
            self._store._installed(entry.id, entry.revision), entry
        )
        return result.ok, result.reason

    def mark_ok(self, model_id: str, revision: str) -> None:
        self._store.mark_ok(model_id, revision)

    def mark_broken(self, model_id: str, revision: str, reason: str) -> None:
        self._store.mark_broken(model_id, revision, reason)

    def current_ids(self) -> tuple[str, str] | None:
        record = self._store.current()
        return (
            (record.id, record.revision)
            if record is not None and record.recheck is not True
            else None
        )

    def installed_ids(self) -> tuple[tuple[str, str], ...]:
        return tuple((record.id, record.revision) for record in self._store.records())

    def set_current(self, model_id: str, revision: str) -> None:
        self._store.set_current(model_id, revision)

    def installed_ok(self) -> bool:
        return any(
            record.state == "ok" and record.recheck is not True for record in self._store.records()
        )

    def broken(self) -> bool:
        return any(record.state == "broken" for record in self._store.records())

    def allowed(self) -> tuple[bool, str]:
        return self._gate.allowed("download")

    def disk_ok(self, size_bytes: int) -> bool:
        return self._store.disk_ok(size_bytes)

    def disk_missing_bytes(self, size_bytes: int) -> int:
        """Дополнительная деталь для UI: дефицит с тем же запасом, что у ModelStore."""
        directory = self._store.root
        while not directory.exists():
            directory = directory.parent
        return max(0, (size_bytes * 6 + 4) // 5 - shutil.disk_usage(directory).free)

    def ram_ok(self, min_ram_mb: int) -> bool:
        return self._store.ram_ok(min_ram_mb)

    def download(
        self, entry: Any, *, progress: Callable[[Progress], None], cancel: threading.Event
    ) -> Path:
        return self._downloader.download(entry, progress=progress, cancel=cancel)

    def install_from_staging(self, entry: Any) -> InstallResult:
        return self._installer.install_from_staging(entry, cancel=lambda: self._cancel.is_set())

    def install_from_path(self, source: Path, entry: Any) -> InstallResult:
        return self._installer.install_from_path(
            source, entry, cancel=lambda: self._cancel.is_set()
        )


class _ModelJob(QObject):
    """Одна попытка установки. С GUI общается только сигналами и Event отмены."""

    progressed = pyqtSignal(float, float, float)
    staged = pyqtSignal(str)
    finished = pyqtSignal(str, str)

    def __init__(
        self,
        model: ModelPort,
        entry: Any,
        cancel: threading.Event,
        source: Path | None = None,
    ) -> None:
        super().__init__()
        self._model = model
        self._entry = entry
        self._cancel = cancel
        self._source = source
        if isinstance(model, ModelService):
            model.set_cancel(cancel)

    def _progress(self, value: Progress) -> None:
        fraction = value.bytes_done / value.bytes_total if value.bytes_total > 0 else 0.0
        self.progressed.emit(
            max(0.0, min(1.0, fraction)),
            value.speed_bps,
            value.eta_s if value.eta_s is not None else -1.0,
        )

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise DownloadError("cancelled")

    def _execute(self) -> tuple[str, str]:
        self._check_cancel()
        if self._source is None:
            allowed, reason = self._model.allowed()
            if not allowed:
                return "no-network", reason
        if not self._model.disk_ok(self._entry.size_bytes):
            return "no-space", ""
        if self._source is None:
            self.staged.emit("downloading")
            self._model.download(self._entry, progress=self._progress, cancel=self._cancel)
            self._check_cancel()
            self.progressed.emit(1.0, 0.0, -1.0)
        self.staged.emit("verifying")
        self._check_cancel()
        # Installer объединяет проверку файлов, перенос и пробное распознавание
        # в одну операцию; частичные результаты не публикуются.
        self.staged.emit("installing")
        result = (
            self._model.install_from_staging(self._entry)
            if self._source is None
            else self._model.install_from_path(self._source, self._entry)
        )
        if result.state == "ok":
            return "installed", ""
        self._check_cancel()
        if result.reason_code == "selfcheck":
            return "broken", result.reason or _SELFCHECK_MESSAGE
        if result.reason_code == "revoked":
            return "broken", _REVOKED_MESSAGE
        if result.reason_code == "cancelled":
            return "cancelled", "Загрузка отменена. Можно продолжить скачивание."
        # Сохраняем распознавание отказа по месту; оно могло измениться после установки.
        if result.state == "error" and (
            "недостаточно места" in result.reason.casefold()
            or not self._model.disk_ok(self._entry.size_bytes)
        ):
            return "no-space", ""
        return "broken", _BROKEN_MESSAGE

    @pyqtSlot()
    def run(self) -> None:
        state, reason = "error", "Не удалось установить модель. Попробуйте ещё раз."
        try:
            state, reason = self._execute()
        except (DownloadError, StoreError, NetworkError, OSError) as exc:
            code = getattr(exc, "code", "")
            if self._cancel.is_set() or code == "cancelled":
                state, reason = "cancelled", "Загрузка отменена. Можно продолжить скачивание."
            elif code == "bad-path":
                state, reason = "broken", _BROKEN_MESSAGE
            elif code == "disk-full" or (
                isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT)
            ):
                state, reason = "no-space", ""
            elif isinstance(exc, NetworkError) or code in {
                "no-network",
                "host-unreachable",
                "timeout",
                "bad-status",
            }:
                state = "no-network"
                reason = f"Нет доступа к {self._entry.host}. Можно установить модель из папки."
            else:
                state, reason = "broken", _BROKEN_MESSAGE
        except Exception:
            log.warning("Не удалось выполнить установку модели", exc_info=True)
        finally:
            self.finished.emit(state, reason)


class _RecheckJob(QObject):
    """Проверяет файлы и распознавание, не меняя состояние хранилища."""

    finished = pyqtSignal(str, str)

    def __init__(self, model: ModelPort, entry: Any, cancel: threading.Event) -> None:
        super().__init__()
        self._model, self._entry, self._cancel = model, entry, cancel
        if isinstance(model, ModelService):
            model.set_cancel(cancel)

    @pyqtSlot()
    def run(self) -> None:
        state, reason = "cancelled", ""
        try:
            if not self._cancel.is_set():
                ok, reason = self._model.verify_files(self._entry)
                if ok and not self._cancel.is_set():
                    ok, reason = self._model.smoke(self._entry)
                if not self._cancel.is_set():
                    state = "installed" if ok else "failed"
        except Exception:
            # Исключение может содержать пути или речь, но не является отказом модели.
            log.warning("Перепроверка модели отложена до следующего запуска")
        self.finished.emit(state, reason)


class ModelDownloads(QObject):
    """Общая очередь установки и состояния каталога, независимые от окна."""

    modelsChanged = pyqtSignal()
    selectionChanged = pyqtSignal()
    downloadStateChanged = pyqtSignal()
    downloadProgressChanged = pyqtSignal()
    downloadTitleChanged = pyqtSignal()
    modelReadyChanged = pyqtSignal()
    modelStateChanged = pyqtSignal()
    modelNameChanged = pyqtSignal()
    modelHostChanged = pyqtSignal()
    modelSizeBytesChanged = pyqtSignal()
    modelSizeChanged = pyqtSignal()
    modelRamChanged = pyqtSignal()
    progressChanged = pyqtSignal()
    speedChanged = pyqtSignal()
    etaChanged = pyqtSignal()
    modelMessageChanged = pyqtSignal()
    queueFinished = pyqtSignal(bool)

    def __init__(
        self,
        model: ModelPort | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = model
        self._entry = model.recommended() if model is not None else None
        self._entries = model.entries() if model is not None else ()
        self._selected: set[str] = set()
        self._card_states: dict[str, str] = {}
        self._card_messages: dict[str, str] = {}
        self._card_progress: dict[str, float] = {}
        self._queue: list[Any] = []
        self._queue_entries: tuple[Any, ...] = ()
        self._active_entry: Any | None = None
        self._queue_running = False
        self._queue_cancelled = False
        self._queue_successes = 0
        self._queue_completed_bytes = 0
        self._queue_total_bytes = 0
        self._queue_current: tuple[str, str] | None = None
        self._last_failure = "failed"
        self._download_state = "idle"
        self._download_progress = 0.0
        self._download_title = ""
        self._clock = clock
        self._tracker = SpeedTracker()
        self._last_estimate_at = float("-inf")
        self._model_state = "absent"
        self._model_message = ""
        self._progress_value = 0.0
        self._speed = ""
        self._eta = ""
        self._model_thread: QThread | None = None
        self._model_job: _ModelJob | _RecheckJob | None = None
        self._model_cancel = threading.Event()
        self._model_result: tuple[str, str] | None = None
        self._shutting_down = False
        self._recheck_queue: list[Any] = []
        self._rechecking = False
        self._recheck_started = False
        self._recheck_card: tuple[str | None, str | None, float | None] = (None, None, None)
        self._recheck_model_state = ("", "")
        self._initial_model_state()

    @property
    def modelReady(self) -> bool:  # noqa: N802
        return self._model is not None and self._model.installed_ok()

    def active_entry(self) -> Any | None:
        if self._model is None:
            return None
        current = self._model.current_ids()
        for entry in self._entries:
            if (entry.id, entry.revision) == current:
                return entry
        for entry in self._entries:
            if self._model.record_state(entry.id, entry.revision) == "ok":
                return entry
        installed = self._model.installed_ids()
        return next(
            (entry for entry in self._entries if (entry.id, entry.revision) in installed), None
        )

    def active_state(self) -> str:
        entry = self.active_entry()
        if entry is None or self._model is None:
            return "none"
        state = self._card_states.get(entry.id, "")
        if state in {"downloading", "verifying", "failed", "no-space"}:
            return state
        record_state = self._model.record_state(entry.id, entry.revision)
        return "ok" if record_state == "ok" else "broken" if record_state else "none"

    def status_text(self) -> str:
        if self.downloadState in {"idle", "done"}:
            return ""
        return f"{self.downloadTitle} — {self.downloadProgress:.0%}"

    def reinstall_active(self) -> None:
        entry = self.active_entry()
        if entry is not None:
            self._begin_queue((entry,))

    def _badge(self, entry: Any) -> str:
        if self._model is None:
            return ""
        state = self._model.record_state(entry.id, entry.revision)
        if not state:
            return ""
        if state == "ok" and self._model.current_ids() == (entry.id, entry.revision):
            return "active"
        return "installed"

    @property
    def models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": entry.id,
                "name": entry.name,
                "description": entry.description,
                "host": entry.host,
                "recommended": entry.recommended,
                "sizeBytes": entry.size_bytes,
                "sizeText": format_size(entry.size_bytes),
                "ramText": format_size(entry.min_ram_mb * 1_000_000),
                "selected": entry.id in self._selected,
                "badge": self._badge(entry),
                "state": self._card_states.get(
                    entry.id, "installed" if self._badge(entry) else "available"
                ),
                "message": self._card_messages.get(entry.id, ""),
                "progress": self._card_progress.get(entry.id, 0.0),
            }
            for entry in self._entries
        ]

    def _selected_downloads(self) -> tuple[Any, ...]:
        return tuple(
            entry
            for entry in self._entries
            if entry.id in self._selected and not self._badge(entry)
        )

    def _selection_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self._selected_downloads())

    @property
    def selectionSummary(self) -> str:  # noqa: N802
        size = self._selection_bytes()
        return f"Будет скачано {format_size(size)}" if size else ""

    @property
    def selectionFits(self) -> bool:  # noqa: N802
        size = self._selection_bytes()
        return not size or (self._model is not None and self._model.disk_ok(size))

    @property
    def selectionMessage(self) -> str:  # noqa: N802
        return "" if self.selectionFits else self._space_message(self._selection_bytes())

    @property
    def canContinueFromModel(self) -> bool:  # noqa: N802
        return bool((self._selected_downloads() or self.modelReady) and self.selectionFits)

    @property
    def downloadState(self) -> str:  # noqa: N802
        return self._download_state

    @property
    def downloadProgress(self) -> float:  # noqa: N802
        return self._download_progress

    @property
    def downloadTitle(self) -> str:  # noqa: N802
        return self._download_title

    def _set_download_state(self, state: str) -> None:
        title = {
            "idle": "",
            "verifying": "Проверяю модель…",
            "done": "Модель готова",
            "failed": "Не получилось загрузить модель",
            "no-space": "Не хватает места на диске",
        }.get(state, "")
        if state == "downloading" and self._active_entry is not None:
            title = f"Загружается {self._active_entry.name}"
            if len(self._queue_entries) > 1:
                number = len(self._queue_entries) - len(self._queue)
                title += f" · {number} из {len(self._queue_entries)}"
        state_changed = state != self._download_state
        title_changed = title != self._download_title
        self._download_state, self._download_title = state, title
        if state_changed:
            self.downloadStateChanged.emit()
        if title_changed:
            self.downloadTitleChanged.emit()

    def _set_download_progress(self, bytes_done: float) -> None:
        value = (
            max(0.0, min(1.0, bytes_done / self._queue_total_bytes))
            if self._queue_total_bytes
            else 0.0
        )
        if value != self._download_progress:
            self._download_progress = value
            self.downloadProgressChanged.emit()

    def _set_estimates(self, speed: str, eta: str) -> None:
        if speed != self._speed:
            self._speed = speed
            self.speedChanged.emit()
        if eta != self._eta:
            self._eta = eta
            self.etaChanged.emit()

    def _set_card(self, entry: Any, state: str, message: str = "", progress: float = 0.0) -> None:
        self._card_states[entry.id] = state
        self._card_messages[entry.id] = message
        self._card_progress[entry.id] = progress if state == "downloading" else 0.0
        self.modelsChanged.emit()

    def toggleModel(self, model_id: str) -> None:  # noqa: N802
        if self._queue_running or self._model_thread is not None or self._shutting_down:
            return
        entry = next((entry for entry in self._entries if entry.id == model_id), None)
        if entry is None or self._badge(entry):
            return
        self._selected.symmetric_difference_update({model_id})
        self.modelsChanged.emit()
        self.selectionChanged.emit()

    def startSelectedDownloads(self) -> None:  # noqa: N802
        self._begin_queue(self._selected_downloads())

    def retryModel(self, model_id: str) -> None:  # noqa: N802
        entry = next((entry for entry in self._entries if entry.id == model_id), None)
        if entry is not None and self._card_states.get(model_id) in {"failed", "no-space"}:
            self._begin_queue((entry,))

    def cancelDownloads(self) -> None:  # noqa: N802
        if self._rechecking:
            self._model_cancel.set()
            self._recheck_queue.clear()
            return
        if not self._queue_running:
            return
        self._queue_cancelled = True
        self._model_cancel.set()
        for entry in (self._active_entry, *self._queue):
            if entry is not None:
                self._set_card(entry, "available")
        self._queue.clear()
        self._set_download_state("idle")
        self._set_download_progress(0)
        self._set_estimates("", "")
        self.selectionChanged.emit()

    def _begin_queue(self, entries: tuple[Any, ...], source: Path | None = None) -> None:
        if (
            not entries
            or self._model is None
            or self._shutting_down
            or self._queue_running
            or self._model_thread is not None
        ):
            return
        self._queue_running = True
        self._queue_cancelled = False
        self._queue_entries = entries
        self._queue = list(entries)
        self._queue_successes = 0
        self._queue_completed_bytes = 0
        self._queue_total_bytes = sum(entry.size_bytes for entry in entries)
        self._queue_current = self._model.current_ids()
        self._last_failure = "failed"
        now = self._clock()
        self._tracker.reset(now)
        self._tracker.update(0, now)
        self._last_estimate_at = float("-inf")
        self._set_download_progress(0)
        self._set_estimates("", "")
        for entry in entries:
            self._set_card(entry, "queued")
        self._start_model_job(source)

    def _initial_model_state(self) -> None:
        model, entry = self._model, self._entry
        if model is None or entry is None:
            self._set_model_state("absent")
        elif model.record_state(entry.id, entry.revision) == "ok":
            self._set_model_state("installed")
        elif model.record_state(entry.id, entry.revision) == "broken":
            self._set_model_state("broken")
        else:
            allowed, reason = model.allowed()
            if not allowed:
                self._set_model_state("no-network", reason)
            elif not model.disk_ok(entry.size_bytes):
                self._set_model_state("no-space")
            elif not model.ram_ok(entry.min_ram_mb):
                self._set_model_state("no-ram")
            else:
                self._set_model_state("downloadable")

    @property
    def modelState(self) -> str:  # noqa: N802
        return self._model_state

    @property
    def modelName(self) -> str:  # noqa: N802
        return str(self._entry.name) if self._entry is not None else ""

    @property
    def modelHost(self) -> str:  # noqa: N802
        return str(self._entry.host) if self._entry is not None else ""

    @property
    def modelSizeBytes(self) -> int:  # noqa: N802
        return int(self._entry.size_bytes) if self._entry is not None else 0

    @property
    def modelSize(self) -> str:  # noqa: N802
        return format_size(self.modelSizeBytes) if self._entry is not None else ""

    @property
    def modelRam(self) -> str:  # noqa: N802
        # Каталог хранит МБ, а общий форматтер принимает байты.
        return format_size(self._entry.min_ram_mb * 1_000_000) if self._entry is not None else ""

    @property
    def progress(self) -> float:
        return self._progress_value

    @property
    def speed(self) -> str:
        return self._speed

    @property
    def eta(self) -> str:
        return self._eta

    @property
    def modelMessage(self) -> str:  # noqa: N802
        return self._model_message

    def _space_message(self, size_bytes: int | None = None) -> str:
        size_bytes = self.modelSizeBytes if size_bytes is None else size_bytes
        # Минимальный порт знает только да/нет. Сервис умеет дать точный дефицит;
        # для других реализаций не выдаём весь размер модели за недостающее место.
        missing = getattr(self._model, "disk_missing_bytes", None)
        if missing is not None:
            try:
                count = missing(size_bytes)
                if count > 0:
                    size = format_size(count, round_up=True)
                    return f"На диске не хватает {size}. Освободите место."
            except (OSError, StoreError):
                log.warning("Не удалось определить, сколько места не хватает", exc_info=True)
        return (
            f"Недостаточно места на диске для модели {format_size(size_bytes)}. Освободите место."
        )

    def _set_model_state(self, state: str, reason: str = "") -> None:
        messages = {
            "absent": "Каталог моделей недоступен. Попробуйте открыть приложение ещё раз.",
            "downloadable": f"Модель будет скачана с {self.modelHost}, объём — {self.modelSize}.",
            "downloading": f"Скачиваю модель с {self.modelHost}…",
            "verifying": "Проверяю целостность файлов модели…",
            "installing": "Устанавливаю модель и проверяю пробное распознавание…",
            "installed": "Модель установлена и готова к работе.",
            "broken": _BROKEN_MESSAGE,
            "no-network": f"Нет доступа к {self.modelHost}. Можно установить модель из папки.",
            "no-ram": "Может не хватить оперативной памяти. Можно скачать и проверить модель.",
            "cancelled": "Загрузка отменена. Можно продолжить скачивание.",
        }
        message = reason or (
            self._space_message() if state == "no-space" else messages.get(state, _BROKEN_MESSAGE)
        )
        changed = state != self._model_state
        message_changed = message != self._model_message
        self._model_state, self._model_message = state, message
        if changed:
            self.modelStateChanged.emit()
        if message_changed:
            self.modelMessageChanged.emit()

    @pyqtSlot(float, float, float)
    def _model_progressed(self, fraction: float, speed: float, eta: float) -> None:
        if self._queue_cancelled:
            return
        fraction = max(0.0, min(1.0, fraction)) if math.isfinite(fraction) else 0.0
        entry = self._active_entry
        if entry is None or entry == self._entry:
            if fraction != self._progress_value:
                self._progress_value = fraction
                self.progressChanged.emit()
        # Слот старого интерфейса тоже безопасен вне запущенной очереди.
        if entry is None or self._queue_cancelled:
            return
        self._set_card(entry, self._card_states.get(entry.id, "downloading"), progress=fraction)
        bytes_done = self._queue_completed_bytes + fraction * entry.size_bytes
        self._set_download_progress(bytes_done)
        now = self._clock()
        self._tracker.update(int(bytes_done), now)
        if now - self._last_estimate_at >= 1.0:
            self._last_estimate_at = now
            self._set_estimates(
                format_speed(self._tracker.speed_bps),
                format_eta(
                    self._tracker.eta_s(self._queue_total_bytes),
                    self._tracker.elapsed_s,
                    self._tracker.stable,
                ),
            )

    @pyqtSlot(str)
    def _model_staged(self, stage: str) -> None:
        if self._queue_cancelled or self._active_entry is None:
            return
        if self._active_entry == self._entry:
            self._set_model_state(stage)
        state = "verifying" if stage == "installing" else stage
        self._set_card(self._active_entry, state)
        self._set_download_state(state)

    @pyqtSlot(str, str)
    def _model_finished(self, state: str, reason: str) -> None:
        # finished работы предшествует finished потока. Итог публикуется после
        # выхода потока, поэтому «Повторить» сразу готов к новой попытке.
        self._model_result = (state, reason)

    def _keep_current(self, entry: Any) -> None:
        if self._model is None:
            return
        if (
            self._queue_current is not None
            and self._model.record_state(*self._queue_current) == "ok"
        ):
            if self._model.current_ids() != self._queue_current:
                self._model.set_current(*self._queue_current)
        else:
            self._queue_current = (entry.id, entry.revision)

    def _failure_message(self, entry: Any, state: str, reason: str) -> str:
        if state == "no-space":
            return self._space_message(entry.size_bytes)
        if state == "no-network":
            return (
                "Нет доступа к источнику модели. Проверьте подключение или установите её из папки."
            )
        if reason in {_SELFCHECK_MESSAGE, _ENGINE_FAILED_MESSAGE, _REVOKED_MESSAGE}:
            return reason
        return _BROKEN_MESSAGE

    @pyqtSlot()
    def _model_thread_finished(self) -> None:
        # finished приходит до удаления отложенных QObject в рабочем потоке.
        # Держим Python-обёртки до конца этой очистки, прежде чем запускать
        # следующую работу: иначе их деструкторы могут пересечься между потоками.
        if self._model_thread is not None:
            self._model_thread.wait()
        self._model_job = None
        self._model_thread = None
        entry = self._active_entry
        if self._rechecking:
            self._finish_recheck()
            return
        if self._model_result is None or entry is None:
            return
        state, reason = self._model_result
        self._model_result = None
        if state == "installed":
            try:
                self._keep_current(entry)
            except (OSError, StoreError):
                state, reason = "error", _BROKEN_MESSAGE
            else:
                self._queue_successes += 1
                self._queue_completed_bytes += entry.size_bytes
                self._selected.add(entry.id)
                if entry == self._entry and self._progress_value != 1.0:
                    self._progress_value = 1.0
                    self.progressChanged.emit()
        if entry == self._entry:
            self._set_model_state("broken" if state == "error" else state, reason)
        if state == "installed":
            self._set_card(entry, "installed")
        elif self._queue_cancelled or state == "cancelled":
            self._set_card(entry, "available")
        else:
            self._last_failure = "no-space" if state == "no-space" else "failed"
            self._set_card(entry, self._last_failure, self._failure_message(entry, state, reason))
        self._active_entry = None
        if not self._queue_cancelled:
            self._set_download_progress(self._queue_completed_bytes)
        if self._queue and not self._queue_cancelled and not self._shutting_down:
            self._start_model_job()
        else:
            self._queue.clear()
            self._queue_running = False
            terminal = "done" if self._queue_successes else self._last_failure
            self._set_download_state(
                "idle" if self._queue_cancelled or state == "cancelled" else terminal
            )
            self._set_estimates("", "")
        self.modelsChanged.emit()
        self.selectionChanged.emit()
        self.modelReadyChanged.emit()
        if not self._queue_running:
            self.queueFinished.emit(bool(self._queue_successes))
            self._start_next_recheck()

    def start_recheck(self) -> None:
        """Ставит мигрированные ревизии на фоновую проверку после загрузок."""
        if self._shutting_down or self._model is None or self._recheck_started:
            return
        self._recheck_started = True
        try:
            self._recheck_queue = list(self._model.recheck_entries())
        except Exception:
            log.warning("Не удалось получить модели для перепроверки")
            return
        self._start_next_recheck()

    def _start_next_recheck(self) -> None:
        if (
            self._shutting_down
            or self._queue_running
            or self._model_thread is not None
            or self._model is None
            or not self._recheck_queue
        ):
            return
        entry = self._recheck_queue.pop(0)
        self._active_entry = entry
        self._rechecking = True
        self._recheck_card = (
            self._card_states.get(entry.id),
            self._card_messages.get(entry.id),
            self._card_progress.get(entry.id),
        )
        self._recheck_model_state = (self._model_state, self._model_message)
        self._model_cancel = threading.Event()
        self._model_result = None
        thread = QThread(self)
        job = _RecheckJob(self._model, entry, self._model_cancel)
        self._model_thread, self._model_job = thread, job
        job.moveToThread(thread)
        thread.started.connect(job.run)
        job.finished.connect(self._model_finished)
        job.finished.connect(job.deleteLater)
        job.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(self._model_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._set_card(entry, "verifying")
        if entry == self._entry:
            self._set_model_state("verifying")
        self._set_download_progress(0)
        self._set_estimates("", "")
        self._set_download_state("verifying")
        thread.start()

    def _finish_recheck(self) -> None:
        entry, model = self._active_entry, self._model
        state, reason = self._model_result or ("cancelled", "")
        self._model_result = None
        if self._model_cancel.is_set() or self._shutting_down:
            state = "cancelled"
        try:
            if model is not None and entry is not None:
                if state == "installed":
                    model.mark_ok(entry.id, entry.revision)
                    if model.current_ids() is None:
                        model.set_current(entry.id, entry.revision)
                elif state == "failed":
                    reason = reason or _BROKEN_MESSAGE
                    model.mark_broken(entry.id, entry.revision, reason)
        except Exception:
            log.warning("Не удалось сохранить результат перепроверки модели")
            state = "cancelled"
        if entry is not None:
            if state == "cancelled":
                old_state, old_message, old_progress = self._recheck_card
                for mapping, value in (
                    (self._card_states, old_state),
                    (self._card_messages, old_message),
                ):
                    if value is None:
                        mapping.pop(entry.id, None)
                    else:
                        mapping[entry.id] = value
                if old_progress is None:
                    self._card_progress.pop(entry.id, None)
                else:
                    self._card_progress[entry.id] = old_progress
                if entry == self._entry:
                    self._set_model_state(*self._recheck_model_state)
                self.modelsChanged.emit()
            else:
                self._set_card(entry, state, reason)
                if entry == self._entry:
                    self._set_model_state("broken" if state == "failed" else state, reason)
                self.modelReadyChanged.emit()
                self.selectionChanged.emit()
        self._rechecking = False
        self._active_entry = None
        self._set_download_state(
            "idle" if state == "cancelled" else "done" if state == "installed" else "failed"
        )
        self._start_next_recheck()

    def _start_model_job(self, source: Path | None = None) -> None:
        if (
            self._shutting_down
            or self._model_thread is not None
            or self._model is None
            or not self._queue
        ):
            return
        self._active_entry = self._queue.pop(0)
        self._model_cancel = threading.Event()
        self._model_result = None
        thread = QThread(self)
        job = _ModelJob(self._model, self._active_entry, self._model_cancel, source)
        self._model_thread, self._model_job = thread, job
        job.moveToThread(thread)
        thread.started.connect(job.run)
        job.progressed.connect(self._model_progressed)
        job.staged.connect(self._model_staged)
        job.finished.connect(self._model_finished)
        job.finished.connect(job.deleteLater)
        # quit() потокобезопасен. Прямой вызов нужен и при shutdown(), когда GUI
        # ждёт wait() и уже не обрабатывает очередь сигналов.
        job.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(self._model_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._model_staged("downloading" if source is None else "verifying")
        self._model_progressed(0.0, 0.0, -1.0)
        thread.start()

    def download(self) -> None:
        if self._shutting_down or self._model_thread is not None or self._queue_running:
            return
        if self._model_state in {"no-network", "no-space", "no-ram"}:
            self._initial_model_state()
        if self._entry is not None and self._model_state in {
            "downloadable",
            "no-ram",
            "cancelled",
            "broken",
        }:
            self._begin_queue((self._entry,))

    def cancelDownload(self) -> None:  # noqa: N802
        self.cancelDownloads()

    def installFromPath(self, path: str) -> None:  # noqa: N802
        if not path.strip() or self._entry is None:
            return
        url = QUrl(path)
        source = url.toLocalFile() if url.isLocalFile() else path
        self._begin_queue((self._entry,), Path(source).expanduser())

    def shutdown(self) -> None:
        """Отменяет загрузку и пробное распознавание; ждёт поток не дольше пяти секунд."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self._recheck_queue.clear()
        self._model_cancel.set()
        if self._model_thread is not None:
            self._model_thread.quit()
            if not self._model_thread.wait(5000):
                log.warning(
                    "Установка модели не завершилась за 5 секунд после отмены; "
                    "выход из приложения продолжается"
                )
                thread = self._model_thread
                thread.setParent(None)
                pair = (thread, self._model_job)
                _finishing_model_threads.add(pair)
                # После отсоединения потока обработчик finished может не выполниться,
                # если очередь GUI уже не крутится; тогда реестр держит пару до конца процесса.
                thread.finished.connect(lambda: _finishing_model_threads.discard(pair))
                self._model_job = None
                self._model_thread = None


class SettingsApply(Protocol):
    """Живое применение настроек; захват клавиши проверяется до записи."""

    def pill_enabled(self, value: bool) -> None: ...

    def hotkey(self, combo: str, mode: str) -> str:
        """Пустая строка или ok — успех; иначе код отказа захвата."""
        ...

    def device(self, value: str | None) -> None: ...


class SettingsBridge(QObject):
    """Публикует настройки и ошибки записи; каждый сеттер сразу пишет файл."""

    hotkeyChanged = pyqtSignal()
    hotkeyModeChanged = pyqtSignal()
    pillEnabledChanged = pyqtSignal()
    languageChanged = pyqtSignal()
    checkAppUpdatesChanged = pyqtSignal()
    checkModelUpdatesChanged = pyqtSignal()
    autostartChanged = pyqtSignal()
    deviceChanged = pyqtSignal()
    hotkeyStatusChanged = pyqtSignal()
    saveErrorChanged = pyqtSignal()
    modelSelfcheckChanged = pyqtSignal()
    extraChanged = pyqtSignal(str)

    activeModelChanged = pyqtSignal()
    activeModelStateChanged = pyqtSignal()
    downloadStateChanged = pyqtSignal()
    downloadProgressChanged = pyqtSignal()
    downloadTitleChanged = pyqtSignal()
    speedChanged = pyqtSignal()
    etaChanged = pyqtSignal()

    _FIELDS = {
        "hotkey": "hotkey",
        "hotkeyMode": "hotkey_mode",
        "pillEnabled": "pill_enabled",
        "language": "language",
        "checkAppUpdates": "check_app_updates",
        "checkModelUpdates": "check_model_updates",
        "autostart": "autostart",
        "device": "device",
    }

    def __init__(
        self,
        settings: Settings,
        *,
        mirror: Settings | None = None,
        downloads: ModelDownloads | None = None,
        locked: Iterable[str] = (),
        apply: SettingsApply | None = None,
        save: Callable[[Settings], None] = settings_save,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._mirror = mirror
        self._locked = frozenset(locked)
        self._apply = apply
        self._save = save
        self._values = self._read_values()
        self._hotkey_status = "ok"
        self._save_error = ""
        self._model_selfcheck = "idle"
        self._downloads = downloads
        if downloads is not None:
            for name in ("downloadState", "downloadProgress", "downloadTitle", "speed", "eta"):
                getattr(downloads, name + "Changed").connect(getattr(self, name + "Changed"))
            downloads.modelsChanged.connect(self.activeModelChanged)
            downloads.modelsChanged.connect(self.activeModelStateChanged)
            downloads.modelReadyChanged.connect(self.activeModelChanged)
            downloads.modelReadyChanged.connect(self.activeModelStateChanged)

    @pyqtProperty(str, notify=activeModelChanged)
    def activeModelName(self) -> str:  # noqa: N802
        entry = self._downloads.active_entry() if self._downloads is not None else None
        return str(entry.name) if entry is not None else ""

    @pyqtProperty(str, notify=activeModelChanged)
    def activeModelSize(self) -> str:  # noqa: N802
        entry = self._downloads.active_entry() if self._downloads is not None else None
        return format_size(entry.size_bytes) if entry is not None else ""

    @pyqtProperty(str, notify=activeModelStateChanged)
    def activeModelState(self) -> str:  # noqa: N802
        return self._downloads.active_state() if self._downloads is not None else "none"

    @pyqtSlot()
    def reinstallActiveModel(self) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.reinstall_active()

    @pyqtProperty(str, notify=downloadStateChanged)
    def downloadState(self) -> str:  # noqa: N802
        return self._downloads.downloadState if self._downloads is not None else ""

    @pyqtProperty(float, notify=downloadProgressChanged)
    def downloadProgress(self) -> float:  # noqa: N802
        return self._downloads.downloadProgress if self._downloads is not None else 0.0

    @pyqtProperty(str, notify=downloadTitleChanged)
    def downloadTitle(self) -> str:  # noqa: N802
        return self._downloads.downloadTitle if self._downloads is not None else ""

    @pyqtProperty(str, notify=speedChanged)
    def speed(self) -> str:  # noqa: N802
        return self._downloads.speed if self._downloads is not None else ""

    @pyqtProperty(str, notify=etaChanged)
    def eta(self) -> str:  # noqa: N802
        return self._downloads.eta if self._downloads is not None else ""

    def _read_values(self) -> dict[str, str | bool]:
        settings = self._mirror if self._mirror is not None else self._settings
        return {
            name: (settings.extra.get("device") or "")
            if name == "device"
            else getattr(settings, field)
            for name, field in self._FIELDS.items()
        }

    def set_extra(self, key: str, value: object) -> bool:
        """Сохраняет дополнительную настройку; зеркало меняется только после записи."""
        present = key in self._settings.extra
        old = self._settings.extra.get(key)
        self._settings.extra[key] = value
        try:
            self._save(self._settings)
        except OSError:
            if present:
                self._settings.extra[key] = old
            else:
                self._settings.extra.pop(key, None)
            self._save_error = "Не удалось сохранить настройки"
            log.warning("Не удалось сохранить настройки")
            self.saveErrorChanged.emit()
            return False
        if self._mirror is not None:
            self._mirror.extra[key] = value
        if self._save_error:
            self._save_error = ""
            self.saveErrorChanged.emit()
        self.extraChanged.emit(key)
        return True

    def _set_value(self, name: str, value: str | bool) -> None:
        field = self._FIELDS[name]
        if self.is_locked(field):
            log.info("%s: настройка задана администратором", field)
            getattr(self, name + "Changed").emit()
            return
        if self._values[name] == value:
            return
        if (
            (name == "hotkeyMode" and value not in ("ptt", "toggle"))
            or (name == "language" and value not in ("ru", "en"))
            or (name == "hotkey" and not is_valid_combo(cast(str, value)))
        ):
            log.warning("Недопустимое значение настройки %s", name)
            if name == "hotkey":
                self.hotkeyChanged.emit()
            return
        if name == "hotkeyMode" and not is_valid_combo(self.hotkey):
            log.warning("hotkey=%r недопустим, смена режима отменена", self.hotkey)
            self.hotkeyModeChanged.emit()
            return
        device_present = "device" in self._settings.extra
        old = (
            self._settings.extra.get("device")
            if name == "device"
            else getattr(self._settings, field)
        )
        hotkey_apply = self._apply if name in ("hotkey", "hotkeyMode") else None
        if hotkey_apply is not None:
            combo = cast(str, value) if name == "hotkey" else self.hotkey
            mode = cast(str, value) if name == "hotkeyMode" else self.hotkeyMode
            code = hotkey_apply.hotkey(combo, mode)
            if code not in ("", "ok"):
                hotkey_apply.hotkey(self.hotkey, self.hotkeyMode)
                # apply_hotkey может менять тот же Settings, что сохраняет мост.
                setattr(self._settings, field, old)
                self.set_hotkey_status(code)
                getattr(self, name + "Changed").emit()
                return
        if name == "device":
            self._settings.extra["device"] = value or None
        else:
            setattr(self._settings, field, value)
        try:
            self._save(self._settings)
        except OSError:
            if hotkey_apply is not None:
                self.set_hotkey_status(hotkey_apply.hotkey(self.hotkey, self.hotkeyMode) or "ok")
            if name == "device":
                if device_present:
                    self._settings.extra["device"] = old
                else:
                    self._settings.extra.pop("device", None)
            else:
                setattr(self._settings, field, old)
            self._save_error = "Не удалось сохранить настройки"
            log.warning("Не удалось сохранить настройки")
            getattr(self, name + "Changed").emit()
            self.saveErrorChanged.emit()
            return
        # Остальные настройки применяются только после успешной записи.
        if self._mirror is not None:
            if name == "device":
                self._mirror.extra["device"] = value or None
            else:
                setattr(self._mirror, field, value)
        self._values[name] = value
        if self._save_error:
            self._save_error = ""
            self.saveErrorChanged.emit()
        if self._apply is not None:
            if name == "pillEnabled":
                self._apply.pill_enabled(self.pillEnabled)
            elif hotkey_apply is not None:
                self.set_hotkey_status("ok")
            elif name == "device":
                self._apply.device(self.device or None)
        getattr(self, name + "Changed").emit()

    @pyqtProperty("QStringList", constant=True)
    def lockedSettings(self) -> list[str]:  # noqa: N802 — имя свойства для QML
        return sorted(self._locked)

    @pyqtSlot(str, result=bool)
    def is_locked(self, name: str) -> bool:
        return name in self._locked

    # Qt виден mypy как Any: декоратор setter не распознаётся как часть свойства.
    @pyqtProperty(str, notify=hotkeyChanged)
    def hotkey(self) -> str:
        return cast(str, self._values["hotkey"])

    @hotkey.setter  # type: ignore[no-redef]
    def hotkey(self, value: str) -> None:
        self._set_value("hotkey", value)

    @pyqtProperty(str, notify=hotkeyModeChanged)
    def hotkeyMode(self) -> str:  # noqa: N802 — имя свойства для QML
        return cast(str, self._values["hotkeyMode"])

    @hotkeyMode.setter  # type: ignore[no-redef]
    def hotkeyMode(self, value: str) -> None:  # noqa: N802
        self._set_value("hotkeyMode", value)

    @pyqtProperty(bool, notify=pillEnabledChanged)
    def pillEnabled(self) -> bool:  # noqa: N802
        return cast(bool, self._values["pillEnabled"])

    @pillEnabled.setter  # type: ignore[no-redef]
    def pillEnabled(self, value: bool) -> None:  # noqa: N802
        self._set_value("pillEnabled", value)

    @pyqtProperty(str, notify=languageChanged)
    def language(self) -> str:
        return cast(str, self._values["language"])

    @language.setter  # type: ignore[no-redef]
    def language(self, value: str) -> None:
        self._set_value("language", value)

    @pyqtProperty(bool, notify=checkAppUpdatesChanged)
    def checkAppUpdates(self) -> bool:  # noqa: N802
        return cast(bool, self._values["checkAppUpdates"])

    @checkAppUpdates.setter  # type: ignore[no-redef]
    def checkAppUpdates(self, value: bool) -> None:  # noqa: N802
        self._set_value("checkAppUpdates", value)

    @pyqtProperty(bool, notify=checkModelUpdatesChanged)
    def checkModelUpdates(self) -> bool:  # noqa: N802
        return cast(bool, self._values["checkModelUpdates"])

    @checkModelUpdates.setter  # type: ignore[no-redef]
    def checkModelUpdates(self, value: bool) -> None:  # noqa: N802
        self._set_value("checkModelUpdates", value)

    @pyqtProperty(bool, notify=autostartChanged)
    def autostart(self) -> bool:
        return cast(bool, self._values["autostart"])

    @autostart.setter  # type: ignore[no-redef]
    def autostart(self, value: bool) -> None:
        self._set_value("autostart", value)

    @pyqtProperty(str, notify=deviceChanged)
    def device(self) -> str:
        return cast(str, self._values["device"])

    @device.setter  # type: ignore[no-redef]
    def device(self, value: str) -> None:
        self._set_value("device", value)

    @pyqtProperty(str, notify=hotkeyStatusChanged)
    def hotkeyStatus(self) -> str:  # noqa: N802
        return self._hotkey_status

    @pyqtProperty(str, notify=saveErrorChanged)
    def saveError(self) -> str:  # noqa: N802
        return self._save_error

    @pyqtProperty(str, notify=modelSelfcheckChanged)
    def modelSelfcheck(self) -> str:  # noqa: N802
        return self._model_selfcheck

    def set_hotkey_status(self, code: str) -> None:
        if self._hotkey_status != code:
            self._hotkey_status = code
            self.hotkeyStatusChanged.emit()

    def set_model_selfcheck(self, state: str) -> None:
        if self._model_selfcheck != state:
            self._model_selfcheck = state
            self.modelSelfcheckChanged.emit()

    @pyqtSlot()
    def retryHotkey(self) -> None:  # noqa: N802
        """Внутренний повтор для рантайма и тестов.

        Кнопки «Повторить» в интерфейсе нет (решение заказчика 15.09.2026).
        """
        if self._apply is not None:
            self.set_hotkey_status(self._apply.hotkey(self.hotkey, self.hotkeyMode))

    def reload(self) -> None:
        """Перечитывает Settings, уведомляя только об изменённых свойствах."""
        values = self._read_values()
        old, self._values = self._values, values
        for name, value in values.items():
            if value != old[name]:
                getattr(self, name + "Changed").emit()


class OnboardingHost(Protocol):
    """Действия приложения, доступные мастеру первого запуска."""

    def begin_capture(self) -> bool: ...

    def end_capture(self) -> None: ...

    def probe(self, combo: str) -> str: ...

    def free_candidates(self, prefer: list[str]) -> list[str]: ...

    def apply_hotkey(self, combo: str, mode: str) -> str: ...

    def start_level_monitor(self, device: str, callback: LevelCallback) -> bool: ...

    def stop_level_monitor(self) -> None: ...

    def start_test(self, device: str, callback: TestCallback) -> bool: ...

    def subscribe_device_resolved(self, callback: Callable[[str], None] | None) -> str: ...

    def stop_test(self) -> None: ...

    def cancel_test(self) -> None: ...

    def reload_model(self) -> None: ...

    def notify_ready(self, combo: str) -> None: ...

    def hide_window(self) -> None: ...


class OnboardingController(QObject):
    """Пять шагов онбординга; все записи проходят через SettingsBridge."""

    stepChanged = pyqtSignal()
    canFinishChanged = pyqtSignal()
    doneChanged = pyqtSignal()
    languageChanged = pyqtSignal()
    checkAppUpdatesChanged = pyqtSignal()
    checkModelUpdatesChanged = pyqtSignal()
    policyLockedChanged = pyqtSignal()
    hotkeyChanged = pyqtSignal()
    hotkeyModeChanged = pyqtSignal()
    captureStateChanged = pyqtSignal()
    captureMessageChanged = pyqtSignal()
    freeCandidatesChanged = pyqtSignal()
    pendingComboChanged = pyqtSignal()
    modelsChanged = pyqtSignal()
    selectionChanged = pyqtSignal()
    downloadStateChanged = pyqtSignal()
    downloadProgressChanged = pyqtSignal()
    downloadTitleChanged = pyqtSignal()
    modelReadyChanged = pyqtSignal()
    modelStateChanged = pyqtSignal()
    modelNameChanged = pyqtSignal()
    modelHostChanged = pyqtSignal()
    modelSizeBytesChanged = pyqtSignal()
    modelSizeChanged = pyqtSignal()
    modelRamChanged = pyqtSignal()
    progressChanged = pyqtSignal()
    speedChanged = pyqtSignal()
    etaChanged = pyqtSignal()
    modelMessageChanged = pyqtSignal()
    devicesChanged = pyqtSignal()
    deviceChanged = pyqtSignal()
    deviceResolvedChanged = pyqtSignal()
    levelStateChanged = pyqtSignal()
    levelMessageChanged = pyqtSignal()
    levelChanged = pyqtSignal()
    peakChanged = pyqtSignal()
    testPhraseChanged = pyqtSignal()
    testStateChanged = pyqtSignal()
    testTextChanged = pyqtSignal()
    testDurationChanged = pyqtSignal()
    testMessageChanged = pyqtSignal()

    _MESSAGES = {
        "conflict": "Эта комбинация занята другой программой. Можно оставить её или выбрать другую",
        "duplicate": "Эта комбинация уже назначена",
        "not-grabbed": "Не удалось назначить комбинацию. Выберите другую",
    }

    def __init__(
        self,
        bridge: SettingsBridge,
        *,
        settings: Settings,
        host: OnboardingHost | None = None,
        model: ModelPort | None = None,
        downloads: ModelDownloads | None = None,
        status_sink: Callable[[str], None] | None = None,
        dialog_factory: Callable[[], str] = QFileDialog.getExistingDirectory,
        device_provider: Callable[[], list[AudioDevice]] = list_devices,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._settings = settings
        self._host = host
        self._resolved_device = ""
        self._dialog_factory = dialog_factory
        self._devices = [{"id": "", "name": "Системный по умолчанию"}]
        try:
            self._devices.extend(
                {"id": device.name, "name": device.label} for device in device_provider()
            )
        except (AudioError, OSError):
            log.warning("Не удалось получить список микрофонов. Доступен системный по умолчанию.")
        self._owns_downloads = downloads is None
        self._downloads = downloads if downloads is not None else ModelDownloads(model, clock=clock)
        self._shutting_down = False
        self._window_visible: bool | None = None
        self._status_sink = status_sink
        for name in (
            "modelsChanged",
            "selectionChanged",
            "downloadStateChanged",
            "downloadProgressChanged",
            "downloadTitleChanged",
            "modelReadyChanged",
            "modelStateChanged",
            "modelNameChanged",
            "modelHostChanged",
            "modelSizeBytesChanged",
            "modelSizeChanged",
            "modelRamChanged",
            "progressChanged",
            "speedChanged",
            "etaChanged",
            "modelMessageChanged",
        ):
            getattr(self._downloads, name).connect(getattr(self, name))
        self._downloads.modelReadyChanged.connect(self.canFinishChanged)
        self._downloads.queueFinished.connect(self._queue_finished)
        self.downloadTitleChanged.connect(self._publish_download_status)
        self.downloadProgressChanged.connect(self._publish_download_status)
        self.downloadStateChanged.connect(self._publish_download_status)
        self._publish_download_status()
        self._level_state = "idle"
        self._level_message = ""
        self._level_epoch = 0
        self._level_running = False
        self._level_stopping = False
        self._test_after_level = False
        self._level = 0.0
        self._peak = ""
        self._test_state = "idle"
        self._test_text = ""
        self._test_duration = ""
        self._test_message = ""
        self._test_epoch = 0
        step = settings.extra.get("onboarding_step")
        self._step = step if type(step) is int and 1 <= step <= 5 else 1
        self._capture_state = "idle"
        self._capture_hint: str = ""
        self._pending_combo = ""
        self._free_candidates: list[str] = []
        for name in (
            "language",
            "checkAppUpdates",
            "checkModelUpdates",
            "hotkey",
            "hotkeyMode",
            "device",
        ):
            getattr(bridge, name + "Changed").connect(getattr(self, name + "Changed").emit)
        bridge.extraChanged.connect(self._extra_changed)
        self.deviceChanged.connect(self._clear_test)
        if host is not None:
            try:
                self._resolved_device = host.subscribe_device_resolved(self._device_resolved)
            except Exception:
                log.warning("Не удалось подписаться на имя открытого микрофона", exc_info=True)
        if "onboarding_language_set" not in settings.extra:
            self.setProperty(
                "language", "ru" if os.environ.get("LANG", "").startswith("ru") else "en"
            )

    def _extra_changed(self, key: str) -> None:
        if key == "onboarding_model_ready":
            self.canFinishChanged.emit()
            self.modelReadyChanged.emit()
            self.selectionChanged.emit()
        elif key == "onboarding_done":
            self.doneChanged.emit()

    def _queue_finished(self, installed: bool) -> None:
        if installed and self._window_visible is not True and not self._shutting_down:
            try:
                notify.notify_model_installed()
            except Exception:
                log.warning("Не удалось показать уведомление об установке модели")

    def _publish_download_status(self) -> None:
        if self._status_sink is None:
            return
        try:
            self._status_sink(self._downloads.status_text())
        except Exception:
            log.warning("Не удалось обновить состояние загрузки в трее")

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Show:
            self._window_visible = True
        if event.type() in (QEvent.Hide, QEvent.Close):
            self._window_visible = False
            if self._capture_state == "capturing":
                self.cancelCapture()
            self._clear_test()
        return bool(super().eventFilter(obj, event))

    @pyqtProperty(int, notify=stepChanged)
    def step(self) -> int:
        return self._step

    @pyqtProperty(int, constant=True)
    def totalSteps(self) -> int:  # noqa: N802
        return 5

    def _go(self, step: int) -> None:
        if step == self._step or not 1 <= step <= 5:
            return
        if not self._bridge.set_extra("onboarding_step", step):
            return
        if self._capture_state == "capturing":
            self.cancelCapture()
        self._clear_test()
        self._step = step
        self.stepChanged.emit()

    @pyqtSlot()
    def next(self) -> None:
        self._go(self._step + 1)

    @pyqtSlot()
    def back(self) -> None:
        self._go(self._step - 1)

    @pyqtSlot()
    def skip(self) -> None:
        self.next()

    @pyqtProperty(bool, notify=canFinishChanged)
    def canFinish(self) -> bool:  # noqa: N802
        return bool(self.modelReady)

    @pyqtProperty(bool, notify=modelReadyChanged)
    def modelReady(self) -> bool:  # noqa: N802
        if self._model is not None:
            return self._model.installed_ok()
        return self._settings.extra.get("onboarding_model_ready") is True

    @pyqtProperty("QVariantList", notify=modelsChanged)
    def models(self) -> list[dict[str, Any]]:
        return self._downloads.models

    @pyqtProperty(str, notify=selectionChanged)
    def selectionSummary(self) -> str:  # noqa: N802
        return self._downloads.selectionSummary

    @pyqtProperty(bool, notify=selectionChanged)
    def selectionFits(self) -> bool:  # noqa: N802
        return self._downloads.selectionFits

    @pyqtProperty(str, notify=selectionChanged)
    def selectionMessage(self) -> str:  # noqa: N802
        return self._downloads.selectionMessage

    @pyqtProperty(bool, notify=selectionChanged)
    def canContinueFromModel(self) -> bool:  # noqa: N802
        return bool(
            (self._downloads.canContinueFromModel or self.modelReady) and self.selectionFits
        )

    @pyqtProperty(str, notify=downloadStateChanged)
    def downloadState(self) -> str:  # noqa: N802
        return self._downloads.downloadState

    @pyqtProperty(float, notify=downloadProgressChanged)
    def downloadProgress(self) -> float:  # noqa: N802
        return self._downloads.downloadProgress

    @pyqtProperty(str, notify=downloadTitleChanged)
    def downloadTitle(self) -> str:  # noqa: N802
        return self._downloads.downloadTitle

    @pyqtSlot(str)
    def toggleModel(self, model_id: str) -> None:  # noqa: N802
        self._downloads.toggleModel(model_id)

    @pyqtSlot()
    def startSelectedDownloads(self) -> None:  # noqa: N802
        self._downloads.startSelectedDownloads()

    @pyqtSlot(str)
    def retryModel(self, model_id: str) -> None:  # noqa: N802
        self._downloads.retryModel(model_id)

    @pyqtSlot()
    def cancelDownloads(self) -> None:  # noqa: N802
        self._downloads.cancelDownloads()

    @pyqtProperty(str, notify=modelStateChanged)
    def modelState(self) -> str:  # noqa: N802
        return self._downloads.modelState

    @pyqtProperty(str, notify=modelNameChanged)
    def modelName(self) -> str:  # noqa: N802
        return self._downloads.modelName

    @pyqtProperty(str, notify=modelHostChanged)
    def modelHost(self) -> str:  # noqa: N802
        return self._downloads.modelHost

    @pyqtProperty(int, notify=modelSizeBytesChanged)
    def modelSizeBytes(self) -> int:  # noqa: N802
        return self._downloads.modelSizeBytes

    @pyqtProperty(str, notify=modelSizeChanged)
    def modelSize(self) -> str:  # noqa: N802
        return self._downloads.modelSize

    @pyqtProperty(str, notify=modelRamChanged)
    def modelRam(self) -> str:  # noqa: N802
        # Каталог хранит МБ, а общий форматтер принимает байты.
        return self._downloads.modelRam

    @pyqtProperty(float, notify=progressChanged)
    def progress(self) -> float:
        return self._downloads.progress

    @pyqtProperty(str, notify=speedChanged)
    def speed(self) -> str:
        return self._downloads.speed

    @pyqtProperty(str, notify=etaChanged)
    def eta(self) -> str:
        return self._downloads.eta

    @pyqtProperty(str, notify=modelMessageChanged)
    def modelMessage(self) -> str:  # noqa: N802
        return self._downloads.modelMessage

    def _set_model_state(self, state: str, reason: str = "") -> None:
        self._downloads._set_model_state(state, reason)

    @pyqtSlot(float, float, float)
    def _model_progressed(self, fraction: float, speed: float, eta: float) -> None:
        self._downloads._model_progressed(fraction, speed, eta)

    @pyqtSlot(str)
    def _model_staged(self, stage: str) -> None:
        self._downloads._model_staged(stage)

    @pyqtSlot()
    def download(self) -> None:
        self._downloads.download()

    @pyqtSlot()
    def cancelDownload(self) -> None:  # noqa: N802
        self._downloads.cancelDownload()

    @pyqtSlot()
    def pickInstallPath(self) -> None:  # noqa: N802
        path = self._dialog_factory()
        if path:
            self.installFromPath(path)

    @pyqtSlot(str)
    def installFromPath(self, path: str) -> None:  # noqa: N802
        self._downloads.installFromPath(path)

    def shutdown(self) -> None:
        """Останавливает микрофон и собственную очередь; общую завершает приложение."""
        self._shutting_down = True
        if self._host is not None:
            try:
                self._host.subscribe_device_resolved(None)
            except Exception:
                log.warning("Не удалось снять подписку на имя открытого микрофона", exc_info=True)
        self._clear_test()
        if self._owns_downloads:
            self._downloads.shutdown()

    @property
    def _model(self) -> ModelPort | None:
        return self._downloads._model

    @property
    def _model_thread(self) -> QThread | None:
        return self._downloads._model_thread

    @_model_thread.setter
    def _model_thread(self, value: QThread | None) -> None:
        self._downloads._model_thread = value

    @property
    def _model_job(self) -> _ModelJob | _RecheckJob | None:
        return self._downloads._model_job

    @property
    def _model_cancel(self) -> threading.Event:
        return self._downloads._model_cancel

    @property
    def _active_entry(self) -> Any:
        return self._downloads._active_entry

    @_active_entry.setter
    def _active_entry(self, value: Any) -> None:
        self._downloads._active_entry = value

    @property
    def _queue_completed_bytes(self) -> int:
        return self._downloads._queue_completed_bytes

    @_queue_completed_bytes.setter
    def _queue_completed_bytes(self, value: int) -> None:
        self._downloads._queue_completed_bytes = value

    @property
    def _tracker(self) -> SpeedTracker:
        return self._downloads._tracker

    @property
    def _start_model_job(self) -> Callable[..., None]:
        return self._downloads._start_model_job

    @_start_model_job.setter
    def _start_model_job(self, value: Callable[..., None]) -> None:
        self._downloads._start_model_job = value  # type: ignore[method-assign]

    @pyqtProperty("QVariantList", notify=devicesChanged)
    def devices(self) -> list[dict[str, str]]:
        return [dict(device) for device in self._devices]

    @pyqtProperty(str, notify=deviceChanged)
    def device(self) -> str:
        return cast(str, self._bridge.device)

    @device.setter  # type: ignore[no-redef]
    def device(self, value: str) -> None:
        self._bridge.device = value

    @pyqtProperty(str, notify=deviceResolvedChanged)
    def deviceResolved(self) -> str:  # noqa: N802
        return self._resolved_device

    def _device_resolved(self, name: str) -> None:
        if self._shutting_down or name == self._resolved_device:
            return
        self._resolved_device = name
        self.deviceResolvedChanged.emit()

    @pyqtProperty(str, notify=levelStateChanged)
    def levelState(self) -> str:  # noqa: N802
        return self._level_state

    @pyqtProperty(str, notify=levelMessageChanged)
    def levelMessage(self) -> str:  # noqa: N802
        return self._level_message

    def _level_updated(self, update: MicrophoneLevelUpdate) -> None:
        old = (self._level_state, self._level_message, self._level, self._peak)
        self._level_state = update.state
        self._level_message = update.message
        if update.state == "listening" and update.peak_dbfs is not None:
            self._level = level_from_dbfs(update.peak_dbfs)
            if math.isfinite(update.peak_dbfs):
                self._peak = f"{update.peak_dbfs:.0f} дБ".replace("-", "−")
        elif update.state != "listening":
            self._level = 0.0
        new = (self._level_state, self._level_message, self._level, self._peak)
        for name, before, after in zip(
            ("levelState", "levelMessage", "level", "peak"), old, new, strict=True
        ):
            if before != after:
                getattr(self, name + "Changed").emit()

    def _level_host_failed(self) -> None:
        self._test_after_level = False
        self._level_epoch += 1
        self._level_running = False
        self._level_stopping = False
        if self._host is not None:
            try:
                self._host.stop_level_monitor()
            except Exception:
                pass
        self._level_updated(MicrophoneLevelUpdate("error", message=LEVEL_FAILED))

    @pyqtSlot()
    def startLevelMonitor(self) -> None:  # noqa: N802
        if (
            self._host is None
            or self._step != 4
            or self._shutting_down
            or self.done
            or self._level_running
            or self._test_state in ("preparing", "recording", "processing")
        ):
            return
        self._level_epoch += 1
        epoch = self._level_epoch
        self._level_running = True
        self._level_stopping = False

        def receive(update: MicrophoneLevelUpdate) -> None:
            if epoch != self._level_epoch:
                return
            if self._level_stopping and update.state == "listening":
                return
            if update.state in ("idle", "error"):
                self._level_running = False
                self._level_stopping = False
                self._level_epoch += 1
            self._level_updated(update)
            pending = self._test_after_level
            if update.state in ("idle", "error"):
                self._test_after_level = False
            if update.state == "idle" and pending:
                self.startTest()

        try:
            accepted = self._host.start_level_monitor(self.device, receive)
            if not accepted and self._level_running:
                self._level_host_failed()
        except Exception:
            self._level_host_failed()

    def _stop_level(self) -> None:
        if self._host is None or not self._level_running or self._level_stopping:
            return
        self._level_stopping = True
        try:
            self._host.stop_level_monitor()
        except Exception:
            self._level_host_failed()

    @pyqtSlot()
    def stopLevelMonitor(self) -> None:  # noqa: N802
        self._test_after_level = False
        self._stop_level()

    @pyqtProperty(float, notify=levelChanged)
    def level(self) -> float:
        return self._level

    @pyqtProperty(str, notify=peakChanged)
    def peak(self) -> str:
        return self._peak

    @pyqtProperty(str, notify=testPhraseChanged)
    def testPhrase(self) -> str:  # noqa: N802
        return "Сегодня хороший день для прогулки."

    @pyqtProperty(str, notify=testStateChanged)
    def testState(self) -> str:  # noqa: N802
        return self._test_state

    @pyqtProperty(str, notify=testTextChanged)
    def testText(self) -> str:  # noqa: N802
        return self._test_text if self._step == 4 and not self.done else ""

    @pyqtProperty(str, notify=testDurationChanged)
    def testDuration(self) -> str:  # noqa: N802
        return self._test_duration

    @pyqtProperty(str, notify=testMessageChanged)
    def testMessage(self) -> str:  # noqa: N802
        return self._test_message

    def _test_updated(self, update: MicrophoneTestUpdate) -> None:
        """Единственный получатель речи проверки: память шага 4, без журналирования."""
        old = (
            self._test_state,
            self._test_text,
            self._test_duration,
            self._test_message,
            self._level,
            self._peak,
        )
        self._test_state = update.state
        self._test_text = update.text if update.state == "done" and self._step == 4 else ""
        self._test_duration = (
            f"{update.duration_s:.2f} с".replace(".", ",")
            if update.state == "done" and update.duration_s is not None
            else ""
        )
        self._test_message = update.message if update.state in ("error", "preparing") else ""
        if update.state == "recording" and update.peak_dbfs is not None:
            self._level = level_from_dbfs(update.peak_dbfs)
            if math.isfinite(update.peak_dbfs):
                self._peak = f"{update.peak_dbfs:.0f} дБ".replace("-", "−")
        elif update.state != "recording":
            self._level = 0.0
        new = (
            self._test_state,
            self._test_text,
            self._test_duration,
            self._test_message,
            self._level,
            self._peak,
        )
        for name, before, after in zip(
            ("testState", "testText", "testDuration", "testMessage", "level", "peak"),
            old,
            new,
            strict=True,
        ):
            if before != after:
                getattr(self, name + "Changed").emit()

    def _clear_test(self) -> None:
        self.stopLevelMonitor()
        self._test_epoch += 1
        self._test_updated(MicrophoneTestUpdate("idle"))
        if self._peak:
            self._peak = ""
            self.peakChanged.emit()
        if self._host is not None:
            try:
                self._host.cancel_test()
            except Exception:
                # Даже текст исключения может содержать речь — не журналируем его.
                pass

    @pyqtSlot()
    def startTest(self) -> None:  # noqa: N802
        if (
            self._host is None
            or self._step != 4
            or self._shutting_down
            or self.done
            or self._test_state in ("preparing", "recording", "processing")
        ):
            return
        if not self.modelReady:
            message = "Будет доступно после установки модели"
            if self._test_message != message:
                self._test_message = message
                self.testMessageChanged.emit()
            return
        if self._level_running:
            self._test_after_level = True
            self._stop_level()
            return
        self._clear_test()
        epoch = self._test_epoch

        def receive(update: MicrophoneTestUpdate) -> None:
            if epoch != self._test_epoch or self._step != 4 or self._shutting_down:
                return
            if update.state in ("done", "error", "idle"):
                self._test_epoch += 1
            self._test_updated(update)

        try:
            self._host.start_test(self.device, receive)
        except Exception:
            try:
                self._host.cancel_test()
            except Exception:
                # Даже текст исключения может содержать речь — не журналируем его.
                pass
            receive(MicrophoneTestUpdate("error", message=TEST_FAILED))

    @pyqtSlot()
    def stopTest(self) -> None:  # noqa: N802
        if self._host is None or self._test_state not in ("preparing", "recording", "processing"):
            return
        try:
            self._host.stop_test()
        except Exception:
            self._clear_test()
            self._test_updated(MicrophoneTestUpdate("error", message=TEST_FAILED))

    @pyqtProperty(str, notify=languageChanged)
    def language(self) -> str:
        return cast(str, self._bridge.language)

    @language.setter  # type: ignore[no-redef]
    def language(self, value: str) -> None:
        if value not in ("ru", "en") or self._bridge.is_locked("language"):
            return
        self._bridge.language = value
        if self._bridge.language == value:
            self._bridge.set_extra("onboarding_language_set", True)

    @pyqtProperty(bool, notify=checkAppUpdatesChanged)
    def checkAppUpdates(self) -> bool:  # noqa: N802
        return cast(bool, self._bridge.checkAppUpdates)

    @checkAppUpdates.setter  # type: ignore[no-redef]
    def checkAppUpdates(self, value: bool) -> None:  # noqa: N802
        self._bridge.checkAppUpdates = value

    @pyqtProperty(bool, notify=checkModelUpdatesChanged)
    def checkModelUpdates(self) -> bool:  # noqa: N802
        return cast(bool, self._bridge.checkModelUpdates)

    @checkModelUpdates.setter  # type: ignore[no-redef]
    def checkModelUpdates(self, value: bool) -> None:  # noqa: N802
        self._bridge.checkModelUpdates = value

    @pyqtProperty(bool, notify=policyLockedChanged)
    def policyLocked(self) -> bool:  # noqa: N802
        return bool(
            self._bridge.is_locked("check_app_updates")
            or self._bridge.is_locked("check_model_updates")
        )

    @pyqtProperty(str, notify=policyLockedChanged)
    def policyLockedText(self) -> str:  # noqa: N802
        return "Задано администратором" if self.policyLocked else ""

    @pyqtProperty(str, notify=hotkeyChanged)
    def hotkey(self) -> str:
        return cast(str, self._bridge.hotkey)

    @hotkey.setter  # type: ignore[no-redef]
    def hotkey(self, value: str) -> None:
        self._bridge.hotkey = value

    @pyqtProperty(str, notify=hotkeyModeChanged)
    def hotkeyMode(self) -> str:  # noqa: N802
        return cast(str, self._bridge.hotkeyMode)

    @hotkeyMode.setter  # type: ignore[no-redef]
    def hotkeyMode(self, value: str) -> None:  # noqa: N802
        self._bridge.hotkeyMode = value

    @pyqtProperty(str, notify=captureStateChanged)
    def captureState(self) -> str:  # noqa: N802
        return self._capture_state

    @pyqtProperty(str, notify=captureMessageChanged)
    def captureMessage(self) -> str:  # noqa: N802
        return self._capture_hint or self._MESSAGES.get(self._capture_state, "")

    @pyqtProperty("QStringList", notify=freeCandidatesChanged)
    def freeCandidates(self) -> list[str]:  # noqa: N802
        return list(self._free_candidates)

    @pyqtProperty(str, notify=pendingComboChanged)
    def pendingCombo(self) -> str:  # noqa: N802
        return self._pending_combo

    def _set_capture_state(self, state: str) -> None:
        if state != self._capture_state:
            old_message = self.captureMessage
            self._capture_state = state
            self._capture_hint = ""
            self.captureStateChanged.emit()
            if old_message != self.captureMessage:
                self.captureMessageChanged.emit()

    def _set_capture_hint(self, hint: str) -> None:
        old_message = self.captureMessage
        self._capture_hint = hint
        if old_message != self.captureMessage:
            self.captureMessageChanged.emit()

    def _set_pending_combo(self, combo: str) -> None:
        if combo != self._pending_combo:
            self._pending_combo = combo
            self.pendingComboChanged.emit()

    def _end_capture(self) -> None:
        if self._host is not None:
            try:
                self._host.end_capture()
            except Exception:
                log.warning("Не удалось завершить захват клавиатуры", exc_info=True)

    @pyqtSlot()
    def beginCapture(self) -> None:  # noqa: N802
        self._set_capture_hint("")
        self._set_pending_combo("")
        try:
            available = self._host is not None and self._host.begin_capture()
        except Exception:
            log.warning("Не удалось начать захват клавиатуры", exc_info=True)
            available = False
        if not available:
            self._end_capture()
        self._set_capture_state("capturing" if available else "not-grabbed")

    @pyqtSlot(str)
    def endCapture(self, combo: str) -> None:  # noqa: N802
        # XGrabKeyboard снимается ДО любого пробника, сохранения или применения.
        self._end_capture()
        self._set_pending_combo(combo)
        if not combo:
            self._set_capture_state("idle")
            return
        if not is_valid_combo(combo):
            self._set_capture_state("capturing")
            self._set_capture_hint("Добавьте к клавише Ctrl, Alt или Win")
            self.refreshCandidates()
            return
        self._set_capture_state("captured")
        try:
            code = self._host.probe(combo) if self._host is not None else "not-grabbed"
        except Exception:
            log.warning("Не удалось проверить сочетание клавиш", exc_info=True)
            code = "not-grabbed"
        if code == "ok":
            self._save_combo()
        else:
            self._set_capture_state(
                {"busy": "conflict", "duplicate": "duplicate"}.get(code, "not-grabbed")
            )

    @pyqtSlot()
    def cancelCapture(self) -> None:  # noqa: N802
        self._end_capture()
        self._set_capture_hint("")
        self._set_pending_combo("")
        self._set_capture_state("idle")

    def _save_combo(self, *, keep: bool = False) -> None:
        if self._host is None:
            self._set_capture_state("not-grabbed")
            return
        self._bridge.setProperty("hotkey", self._pending_combo)
        if self._bridge.hotkey != self._pending_combo:
            self._set_capture_state("not-grabbed")
            return
        try:
            code = self._host.apply_hotkey(self.hotkey, self.hotkeyMode)
        except Exception:
            log.warning("Не удалось применить сочетание клавиш", exc_info=True)
            code = "not-grabbed"
        # «Оставить» принимает занятость: рантайм продолжает автоматический перезахват.
        if code == "ok" or (keep and code == "busy"):
            self._set_capture_state("success")
        else:
            self._set_capture_state(
                {"busy": "conflict", "duplicate": "duplicate"}.get(code, "not-grabbed")
            )

    @pyqtSlot()
    def keepCombo(self) -> None:  # noqa: N802
        if self._capture_state == "conflict" and self._pending_combo:
            self._save_combo(keep=True)

    @pyqtSlot()
    def refreshCandidates(self) -> None:  # noqa: N802
        try:
            candidates = (
                self._host.free_candidates(list(DEFAULT_CANDIDATES))
                if self._host is not None
                else []
            )
        except Exception:
            log.warning("Не удалось найти свободные сочетания клавиш", exc_info=True)
            candidates = []
            self._set_capture_state("not-grabbed")
        if self._host is None:
            self._set_capture_state("not-grabbed")
        if candidates != self._free_candidates:
            self._free_candidates = list(candidates)
            self.freeCandidatesChanged.emit()

    @pyqtProperty(bool, notify=doneChanged)
    def done(self) -> bool:
        return self._settings.extra.get("onboarding_done") is True

    @pyqtSlot()
    def finish(self) -> None:
        if not self.canFinish or self.done:
            return
        if not self._bridge.set_extra("onboarding_done", True):
            return
        self._clear_test()
        if self._capture_state == "capturing":
            self.cancelCapture()
        if self._host is not None:
            try:
                self._host.reload_model()
            except Exception:
                log.warning("Не удалось перезагрузить модель после онбординга", exc_info=True)
            try:
                self._host.notify_ready(self.hotkey)
            finally:
                self._host.hide_window()
