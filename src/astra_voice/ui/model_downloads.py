"""Загрузка, установка и перепроверка моделей для интерфейса.

Здесь живёт всё, что относится к очереди моделей: доступ к каталогу и
хранилищу (`ModelService`), рабочие задания в отдельных потоках (`_ModelJob`,
`_RecheckJob`) и сам объект очереди (`ModelDownloads`), на который смотрят и
мастер первого запуска, и раздел «Общие». Очередь не знает про окно: она
переживает его закрытие и продолжает работу в трее.
"""

from __future__ import annotations

import errno
import logging
import math
import shutil
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from PyQt5.QtCore import (
    QObject,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtSignal,
    pyqtSlot,
)

from astra_voice.core import paths
from astra_voice.core.model_source import SmokeRunner
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.core.version import __version__
from astra_voice.models import catalog_state
from astra_voice.models.catalog import CatalogEntry, load_builtin
from astra_voice.models.downloader import Downloader, DownloadError, Progress
from astra_voice.models.installer import (
    Installer,
    InstallResult,
    SmokeCheck,
    SmokeResult,
    verify_installed,
)
from astra_voice.models.store import ModelStore, StoreError
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient, NetworkError
from astra_voice.security.verify import Verifier
from astra_voice.ui.formatting import (
    SpeedTracker,
    clean_display_name,
    format_eta,
    format_rtfx,
    format_size,
    format_space,
    format_speed,
    format_wer,
)

log = logging.getLogger(__name__)

_SMOKE_FAILURE = "Модель не прошла пробное распознавание на этом компьютере"
_BROKEN_MESSAGE = "Модель не прошла проверку. Попробуйте скачать или установить её заново."
_SELFCHECK_MESSAGE = "Распознавание на этом компьютере не работает. Обратитесь к администратору"
_ENGINE_FAILED_MESSAGE = "Не удалось запустить распознавание. Попробуйте переустановить модель"
_REVOKED_MESSAGE = (
    "Издатель больше не рекомендует эту версию модели. "
    "Не устанавливайте её — скачайте свежую версию."
)
_CURRENT_FAILED_MESSAGE = (
    "Модель установлена. Сделать её рабочей не удалось — попробуйте переустановить."
)
_REMOVE_FAILED_MESSAGE = "Не удалось удалить модель. Попробуйте ещё раз."
_SAFE_MODEL_MESSAGES = frozenset(
    {
        _BROKEN_MESSAGE,
        _SELFCHECK_MESSAGE,
        _ENGINE_FAILED_MESSAGE,
        _REVOKED_MESSAGE,
        _CURRENT_FAILED_MESSAGE,
        _REMOVE_FAILED_MESSAGE,
        "Не удалось установить модель. Попробуйте ещё раз.",
        "Модель нужно переустановить.",
        "В папке есть лишние файлы. Оставьте только файлы выбранной модели.",
        "Файлы в папке не подходят для выбранной модели.",
        "В папке не хватает файлов модели. Получите их заново.",
        "Файлы модели повреждены: контрольная сумма не совпала. Получите их заново.",
        "Не удалось проверить правила администратора: работа без сети",
        "Задано администратором: работа без сети",
        "Включена работа без сети",
    }
)
_PUNCTUATION_TAG = "с пунктуацией"
_DOMESTIC_TAG = "отечественная"
_QUALITY_LABEL = "Качество"
_SPEED_LABEL = "Скорость"


def _metric(entry: Any, name: str) -> Any | None:
    """Достаёт одну метрику записи, не полагаясь на её наличие у подставных объектов."""
    return getattr(getattr(entry, "metrics", None), name, None)


def _metric_row(label: str, text: str, fill: float) -> dict[str, Any]:
    """Строка полоски карточки; measured — признак замера на этом компьютере."""
    return {
        "label": label,
        "text": text,
        "fill": max(0.0, min(1.0, fill)),
        "hasData": bool(text),
        "measured": False,
    }


def entry_tags(entry: Any) -> list[str]:
    """Готовые подписи карточки: язык, пунктуация, лицензия с вендором, происхождение."""
    tags: list[str] = []
    language_tag = getattr(entry, "language_tag", "")
    if language_tag:
        tags.append(language_tag)
    if getattr(entry, "punctuation", False):
        tags.append(_PUNCTUATION_TAG)
    license_name = getattr(entry, "license", "")
    vendor_short = getattr(entry, "vendor_short", "")
    if license_name and vendor_short:
        tags.append(f"{license_name} · {vendor_short}")
    elif license_name or vendor_short:
        tags.append(license_name or vendor_short)
    if getattr(entry, "domestic", False):
        tags.append(_DOMESTIC_TAG)
    return tags


def entry_metrics(entry: Any, best_wer: float, best_rtfx: float) -> list[dict[str, Any]]:
    """Качество и скорость записи; доля заливки — относительно лучшего в каталоге."""
    wer = _metric(entry, "wer_ru")
    rtfx = _metric(entry, "rtfx")
    wer_value = getattr(wer, "value", 0.0) if wer is not None else 0.0
    rtfx_value = getattr(rtfx, "value", 0.0) if rtfx is not None else 0.0
    return [
        _metric_row(
            _QUALITY_LABEL,
            format_wer(wer_value) if wer_value > 0 else "",
            best_wer / wer_value if wer_value > 0 and best_wer > 0 else 0.0,
        ),
        _metric_row(
            _SPEED_LABEL,
            format_rtfx(rtfx_value) if rtfx_value > 0 else "",
            rtfx_value / best_rtfx if rtfx_value > 0 and best_rtfx > 0 else 0.0,
        ),
    ]


def catalog_best(entries: Iterable[Any]) -> tuple[float, float]:
    """Лучшие цифры каталога: наименьший WER и наибольшая скорость."""
    wer_values = [
        value for entry in entries if (value := getattr(_metric(entry, "wer_ru"), "value", 0.0)) > 0
    ]
    rtfx_values = [
        value for entry in entries if (value := getattr(_metric(entry, "rtfx"), "value", 0.0)) > 0
    ]
    return (min(wer_values) if wer_values else 0.0, max(rtfx_values) if rtfx_values else 0.0)


# После таймаута поток и задание должны оставаться живы до выхода run().
_finishing_model_threads: set[tuple[QThread, _ModelJob | _RecheckJob | None]] = set()


class ModelPort(Protocol):
    """Модель для онбординга; операции установки выполняются в рабочем потоке."""

    def recommended(self) -> Any | None: ...

    def entries(self) -> tuple[Any, ...]: ...

    def is_revoked(self, entry: Any) -> bool: ...

    def recheck_entries(self) -> tuple[Any, ...]: ...

    def verify_files(self, entry: Any) -> tuple[bool, str]: ...

    def smoke(self, entry: Any) -> tuple[bool, str]: ...

    def mark_ok(self, model_id: str, revision: str) -> None: ...

    def mark_broken(self, model_id: str, revision: str, reason: str) -> None: ...

    def record_state(self, model_id: str, revision: str) -> str: ...

    def current_ids(self) -> tuple[str, str] | None: ...

    def installed_ids(self) -> tuple[tuple[str, str], ...]: ...

    def set_current(self, model_id: str, revision: str) -> None: ...

    def remove(self, model_id: str, revision: str) -> None: ...

    def installed_ok(self) -> bool: ...

    def broken(self) -> bool: ...

    def allowed(self) -> tuple[bool, str]: ...

    def disk_ok(self, size_bytes: int) -> bool: ...

    def disk_missing_bytes(self, size_bytes: int) -> int: ...

    def free_bytes(self) -> int: ...

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
        self._catalog = load_builtin(
            Verifier("catalog", keyring=keyring),
            state_path=catalog_state.state_path(paths.state_dir()),
        )
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

    def is_revoked(self, entry: Any) -> bool:
        """Издатель отозвал именно эту ревизию модели."""
        return self._catalog.is_revoked(entry.id, entry.revision)

    def entries(self) -> tuple[CatalogEntry, ...]:
        # Отозванную ревизию не предлагаем, но уже установленную показываем:
        # иначе человек не увидит, почему модель перестала работать.
        try:
            installed = {(record.id, record.revision) for record in self._store.records()}
        except (OSError, StoreError):
            log.warning("Не удалось прочитать установленные модели для списка каталога")
            installed = set()
        entries = tuple(
            entry
            for entry in self._catalog.entries
            if not self._catalog.is_revoked(entry.id, entry.revision)
            or (entry.id, entry.revision) in installed
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
        # Отозванную ревизию не перепроверяем: её всё равно нельзя сделать рабочей.
        return tuple(
            entry
            for entry in self.entries()
            if (entry.id, entry.revision) in pending and not self.is_revoked(entry)
        )

    def verify_files(self, entry: Any) -> tuple[bool, str]:
        """Сверяет установленный набор; ошибки чтения не становятся вердиктом."""
        directory = self._store._installed(entry.id, entry.revision)
        return verify_installed(directory, entry, cancel=self._cancel.is_set)

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

    def remove(self, model_id: str, revision: str) -> None:
        self._store.remove(model_id, revision)

    def installed_ok(self) -> bool:
        return any(
            record.state == "ok"
            and record.recheck is not True
            and not self._catalog.is_revoked(record.id, record.revision)
            for record in self._store.records()
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

    def free_bytes(self) -> int:
        """Прочитать свободное место, не создавая каталог моделей."""
        directory = self._store.root
        while not directory.exists():
            parent = directory.parent
            if parent == directory:
                raise FileNotFoundError("Хранилище моделей недоступно")
            directory = parent
        return shutil.disk_usage(directory).free

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

    progressed = pyqtSignal(float)
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
        self.progressed.emit(max(0.0, min(1.0, fraction)))

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
            self.progressed.emit(1.0)
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
    freeSpaceTextChanged = pyqtSignal()
    downloadStateChanged = pyqtSignal()
    downloadProgressChanged = pyqtSignal()
    downloadTitleChanged = pyqtSignal()
    downloadDetailChanged = pyqtSignal()
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
        store: ModelStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = model
        self._store = store
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
        self._download_detail = ""
        self._no_space_size_bytes = 0
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
        self._update_depth = 0
        self._pending_notifications: dict[str, tuple[object, ...]] = {}
        self._free_space_text = ""
        self._initial_model_state()
        self._refresh_free_space_text()
        # selectionChanged публикуется также после завершения каждой модели в очереди.
        self.selectionChanged.connect(self._refresh_free_space_text)

    @property
    def model(self) -> ModelPort | None:
        """Доступ к службе моделей для мостов: очередь владеет ею одна."""
        return self._model

    @contextmanager
    def _update(self) -> Iterator[None]:
        """Публикует переход целиком: прямой обработчик Qt может вызвать любой слот."""
        self._update_depth += 1
        try:
            yield
        finally:
            self._update_depth -= 1
            if not self._update_depth:
                pending, self._pending_notifications = self._pending_notifications, {}
                # После первого emit нельзя менять поля завершённого перехода:
                # обработчик уже мог запустить или отменить следующую работу.
                for name, args in pending.items():
                    getattr(self, name).emit(*args)

    def _notify(self, name: str, *args: object) -> None:
        if self._update_depth:
            self._pending_notifications[name] = args
        else:
            getattr(self, name).emit(*args)

    @property
    def modelReady(self) -> bool:  # noqa: N802
        return self._model is not None and self._model.installed_ok()

    def _is_revoked(self, entry: Any) -> bool:
        """Спрашивает порт об отзыве; порт без этого метода отзывов не знает."""
        checker = getattr(self._model, "is_revoked", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(entry))
        except (OSError, StoreError):
            log.warning("Не удалось проверить отзыв версии модели")
            return False

    def _card_state(self, entry: Any) -> str:
        state = self._card_states.get(entry.id)
        if state is not None:
            return state
        if self._is_revoked(entry):
            return "failed"
        return "installed" if self._badge(entry) else "available"

    def _card_message(self, entry: Any) -> str:
        message = self._card_messages.get(entry.id, "")
        if message:
            return message
        return _REVOKED_MESSAGE if self._is_revoked(entry) else ""

    def active_entry(self) -> Any | None:
        if self._model is None:
            if self._store is None:
                return None
            try:
                current_record = self._store.current()
                if current_record is not None:
                    return current_record
                records = self._store.records()
            except (OSError, StoreError):
                log.warning("Не удалось прочитать установленные модели")
                return None
            return next(
                (
                    record
                    for record in records
                    if record.state == "ok" and not record.recheck and record.metadata_ok
                ),
                records[0] if records else None,
            )
        current = self._model.current_ids()
        # Отозванная ревизия рабочей быть не может: её место занимает замена.
        usable = tuple(entry for entry in self._entries if not self._is_revoked(entry))
        for entry in usable:
            if (entry.id, entry.revision) == current:
                return entry
        for entry in usable:
            if self._model.record_state(entry.id, entry.revision) == "ok":
                return entry
        installed = self._model.installed_ids()
        return next((entry for entry in usable if (entry.id, entry.revision) in installed), None)

    def active_state(self) -> str:
        entry = self.active_entry()
        if entry is None:
            return "none"
        if self._model is None:
            return "catalog-unavailable"
        state = self._card_states.get(entry.id, "")
        if state in {"downloading", "verifying", "failed", "no-space"}:
            return state
        record_state = self._model.record_state(entry.id, entry.revision)
        return "ok" if record_state == "ok" else "broken" if record_state else "none"

    def status_text(self) -> str:
        if self.downloadState in {"idle", "done"}:
            return ""
        if self._rechecking:
            return "Проверяю модель…"
        return f"{self.downloadTitle} — {self.downloadProgress:.0%}"

    def can_reinstall(self) -> bool:
        if (
            self._model is None
            or self._queue_running
            or self._rechecking
            or self._model_thread is not None
            or self._shutting_down
        ):
            return False
        entry = self.active_entry()
        return entry is not None and bool(self._model.record_state(entry.id, entry.revision))

    def reinstall_active(self) -> None:
        if not self.can_reinstall():
            return
        entry = self.active_entry()
        if entry is not None:
            self._begin_queue((entry,))

    def can_install(self) -> bool:
        """Разрешить первую установку только при доступной рекомендации и очереди."""
        return (
            self._model is not None
            and self._entry is not None
            and self._entry in self._entries
            and not self._queue_running
            and not self._rechecking
            and self._model_thread is None
            and not self._shutting_down
            and self.active_entry() is None
        )

    def install_recommended(self) -> None:
        if self.can_install():
            self._begin_queue((self._entry,))

    def open_models_folder(self, opener: Callable[[QUrl], bool]) -> None:
        """Открыть корень хранилища без создания каталогов и публикации пути в QML."""
        try:
            store = self._store
            if store is None:
                store = getattr(self._model, "_store", None)
            if store is None or not store.root.is_dir():
                log.debug("Каталог моделей недоступен для открытия")
                return
            opener(QUrl.fromLocalFile(str(store.root)))
        except (OSError, StoreError):
            log.debug("Не удалось открыть каталог моделей")

    def _installed_ids(self) -> tuple[tuple[str, str], ...]:
        """Что лежит в хранилище; недоступное хранилище не считается установкой."""
        if self._model is None:
            return ()
        try:
            return self._model.installed_ids()
        except (OSError, StoreError):
            log.warning("Не удалось прочитать список установленных моделей")
            return ()

    def _installed_revision(
        self, entry: Any, installed: tuple[tuple[str, str], ...] | None = None
    ) -> str:
        """Ревизия этой модели в хранилище: своя из каталога, иначе прежняя.

        Каталог хранит одну — самую свежую — ревизию каждой модели. Если
        установлена другая, модель всё равно установлена, просто старой версии:
        именно на этом держится «Обновить» в карточке.
        """
        known = self._installed_ids() if installed is None else installed
        if (entry.id, entry.revision) in known:
            return str(entry.revision)
        return next((revision for model_id, revision in known if model_id == entry.id), "")

    def _update_available(
        self, entry: Any, installed: tuple[tuple[str, str], ...] | None = None
    ) -> bool:
        """Каталог знает ревизию новее установленной."""
        revision = self._installed_revision(entry, installed)
        return bool(revision) and revision != entry.revision

    def _badge(self, entry: Any, installed: tuple[tuple[str, str], ...] | None = None) -> str:
        if self._model is None:
            return ""
        revision = self._installed_revision(entry, installed)
        if not revision:
            return ""
        state = self._model.record_state(entry.id, revision)
        if not state:
            return ""
        if state == "ok" and self._model.current_ids() == (entry.id, revision):
            return "active"
        return "installed"

    def _refresh_free_space_text(self) -> None:
        size = ""
        free_bytes = getattr(self._model, "free_bytes", None)
        if callable(free_bytes):
            try:
                size = format_space(free_bytes())
            except (OSError, StoreError):
                log.debug("Не удалось определить свободное место на диске")
        text = f"свободно на диске {size}" if size else ""
        if text != self._free_space_text:
            self._free_space_text = text
            self.freeSpaceTextChanged.emit()

    @property
    def freeSpaceText(self) -> str:  # noqa: N802
        return self._free_space_text

    @property
    def models(self) -> list[dict[str, Any]]:
        best_wer, best_rtfx = catalog_best(self._entries)
        installed = self._installed_ids()
        return [
            {
                "id": entry.id,
                "name": clean_display_name(entry.name),
                "description": entry.description,
                "host": entry.host,
                "recommended": entry.recommended,
                "sizeBytes": entry.size_bytes,
                "sizeText": format_size(entry.size_bytes),
                "ramText": format_size(entry.min_ram_mb * 1_000_000),
                "selected": entry.id in self._selected,
                "badge": self._badge(entry, installed),
                "state": self._card_state(entry),
                "message": self._card_message(entry),
                "progress": self._card_progress.get(entry.id, 0.0),
                "vendor": getattr(entry, "vendor", ""),
                "domestic": bool(getattr(entry, "domestic", False)),
                "updateAvailable": self._update_available(entry, installed),
                "tags": entry_tags(entry),
                "metrics": entry_metrics(entry, best_wer, best_rtfx),
            }
            for entry in self._entries
        ]

    @property
    def installedSummary(self) -> str:  # noqa: N802
        """Счётчик шапки раздела: сколько записей каталога уже стоит и сколько занимают."""
        if not self._entries:
            return ""
        installed = self._installed_ids()
        entries = [entry for entry in self._entries if self._badge(entry, installed)]
        total = f"Установлено {len(entries)} из {len(self._entries)}"
        size = sum(entry.size_bytes for entry in entries)
        # Цифра каталога — та же, что в строке «Занимает места» каждой карточки.
        return f"{total} · {format_size(size)} на диске" if size else total

    def _selected_downloads(self) -> tuple[Any, ...]:
        installed = self._installed_ids()
        return tuple(
            entry
            for entry in self._entries
            if entry.id in self._selected and not self._badge(entry, installed)
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

    @property
    def downloadDetail(self) -> str:  # noqa: N802
        return self._download_detail

    def _space_detail(self) -> str:
        missing = getattr(self._model, "disk_missing_bytes", None)
        if callable(missing):
            try:
                size = format_size(missing(self._no_space_size_bytes), round_up=True)
                if size:
                    return f"нужно ещё {size}"
            except (OSError, StoreError):
                log.debug("Не удалось определить дефицит места на диске")
        return ""

    def _set_download_state(self, state: str) -> None:
        with self._update():
            detail = self._space_detail() if state == "no-space" else ""
            if detail != self._download_detail:
                self._download_detail = detail
                self._notify("downloadDetailChanged")
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
            title = clean_display_name(title)
            state_changed = state != self._download_state
            title_changed = title != self._download_title
            self._download_state, self._download_title = state, title
            if state == "downloading" and not self._eta:
                self._set_estimates("", format_eta(float("inf"), 0.0, False))
            if state_changed:
                self._notify("downloadStateChanged")
            if title_changed:
                self._notify("downloadTitleChanged")

    def _set_download_progress(self, bytes_done: float) -> None:
        value = (
            max(0.0, min(1.0, bytes_done / self._queue_total_bytes))
            if self._queue_total_bytes
            else 0.0
        )
        if value != self._download_progress:
            self._download_progress = value
            self._notify("downloadProgressChanged")

    def _set_estimates(self, speed: str, eta: str) -> None:
        if self._download_state == "downloading" and not eta:
            eta = format_eta(float("inf"), 0.0, False)
        speed_changed, eta_changed = speed != self._speed, eta != self._eta
        self._speed, self._eta = speed, eta
        if speed_changed:
            self._notify("speedChanged")
        if eta_changed:
            self._notify("etaChanged")

    def _set_card(self, entry: Any, state: str, message: str = "", progress: float = 0.0) -> None:
        self._card_states[entry.id] = state
        self._card_messages[entry.id] = message
        self._card_progress[entry.id] = progress if state == "downloading" else 0.0
        self._notify("modelsChanged")

    def toggleModel(self, model_id: str) -> None:  # noqa: N802
        if (
            self._queue_running
            or self._rechecking
            or self._model_thread is not None
            or self._shutting_down
        ):
            return
        entry = next((entry for entry in self._entries if entry.id == model_id), None)
        if entry is None or self._badge(entry):
            return
        self._selected.symmetric_difference_update({model_id})
        self._notify("modelsChanged")
        self._notify("selectionChanged")

    def startSelectedDownloads(self) -> None:  # noqa: N802
        self._begin_queue(self._selected_downloads())

    def retryModel(self, model_id: str) -> None:  # noqa: N802
        entry = next((entry for entry in self._entries if entry.id == model_id), None)
        if entry is not None and self._card_states.get(model_id) in {"failed", "no-space"}:
            self._begin_queue((entry,))

    def _idle(self) -> bool:
        """Действия с установленными моделями не пересекаются с очередью и проверкой."""
        return not (
            self._model is None
            or self._queue_running
            or self._rechecking
            or self._model_thread is not None
            or self._shutting_down
        )

    def _managed_entry(self, model_id: str) -> Any | None:
        """Запись каталога, которой можно управлять прямо сейчас."""
        if not self._idle():
            return None
        return next((entry for entry in self._entries if entry.id == model_id), None)

    def makeModelCurrent(self, model_id: str) -> None:  # noqa: N802
        """Делает установленную ревизию рабочей; сломанную рабочей не делаем."""
        entry = self._managed_entry(model_id)
        if entry is None or self._model is None:
            return
        revision = self._installed_revision(entry)
        if not revision or self._model.record_state(entry.id, revision) != "ok":
            return
        with self._update():
            try:
                self._model.set_current(entry.id, revision)
            except (OSError, StoreError):
                log.warning("Не удалось сделать модель рабочей")
                self._set_card(entry, self._card_state(entry), _CURRENT_FAILED_MESSAGE)
                return
            self._card_messages.pop(entry.id, None)
            self._notify("modelsChanged")
            self._notify("modelReadyChanged")
            self._notify("selectionChanged")

    def removeModel(self, model_id: str) -> None:  # noqa: N802
        """Удаляет установленную ревизию; рабочую модель удалить нельзя."""
        entry = self._managed_entry(model_id)
        if entry is None or self._model is None:
            return
        revision = self._installed_revision(entry)
        if not revision or self._badge(entry) == "active":
            return
        with self._update():
            try:
                self._model.remove(entry.id, revision)
            except (OSError, StoreError):
                log.warning("Не удалось удалить модель")
                self._set_card(entry, self._card_state(entry), _REMOVE_FAILED_MESSAGE)
                return
            self._selected.discard(entry.id)
            self._card_states.pop(entry.id, None)
            self._card_messages.pop(entry.id, None)
            self._card_progress.pop(entry.id, None)
            self._notify("modelsChanged")
            self._notify("modelReadyChanged")
            self._notify("selectionChanged")

    def updateModel(self, model_id: str) -> None:  # noqa: N802
        """Ставит в очередь ревизию из каталога, когда установлена прежняя."""
        entry = self._managed_entry(model_id)
        if entry is not None and self._update_available(entry):
            self._begin_queue((entry,))

    def cancelDownloads(self) -> None:  # noqa: N802
        with self._update():
            if self._rechecking:
                self._model_cancel.set()
                self._recheck_queue.clear()
                return
            if not self._queue_running or self._queue_cancelled:
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
            self._notify("selectionChanged")

    def _begin_queue(self, entries: tuple[Any, ...], source: Path | None = None) -> None:
        with self._update():
            if (
                not entries
                or self._model is None
                or self._shutting_down
                or self._queue_running
                or self._rechecking
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
            self._last_estimate_at = now
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
        return clean_display_name(str(self._entry.name)) if self._entry is not None else ""

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

    def _model_messages(self) -> dict[str, str]:
        return {
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

    def _safe_model_message(self, reason: str) -> str:
        if reason in _SAFE_MODEL_MESSAGES or reason in self._model_messages().values():
            return reason
        if reason == self._space_message():
            return reason
        return _BROKEN_MESSAGE

    def _set_model_state(self, state: str, reason: str = "") -> None:
        message = (
            self._safe_model_message(reason)
            if reason
            else (
                self._space_message()
                if state == "no-space"
                else self._model_messages().get(state, _BROKEN_MESSAGE)
            )
        )
        changed = state != self._model_state
        message_changed = message != self._model_message
        self._model_state, self._model_message = state, message
        if changed:
            self._notify("modelStateChanged")
        if message_changed:
            self._notify("modelMessageChanged")

    @pyqtSlot(float)
    @pyqtSlot(float, float, float)
    def _model_progressed(self, fraction: float, speed: float = 0.0, eta: float = -1.0) -> None:
        with self._update():
            if self._queue_cancelled:
                return
            fraction = max(0.0, min(1.0, fraction)) if math.isfinite(fraction) else 0.0
            entry = self._active_entry
            if entry is None or entry == self._entry:
                if fraction != self._progress_value:
                    self._progress_value = fraction
                    self._notify("progressChanged")
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
        with self._update():
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
        return self._safe_model_message(reason)

    @pyqtSlot()
    def _model_thread_finished(self) -> None:
        # finished приходит до удаления отложенных QObject в рабочем потоке.
        # Держим Python-обёртки до конца этой очистки, прежде чем запускать
        # следующую работу: иначе их деструкторы могут пересечься между потоками.
        thread = self._model_thread
        if thread is not None:
            if not thread.wait(5000):
                log.warning("Поток установки модели не завершился за 5 секунд; ожидаем очистку")
                # Не удаляем обёртки и не запускаем следующую работу до выхода
                # run()/очистки TLS. GUI между ограниченными ожиданиями свободен.
                QTimer.singleShot(
                    100,
                    lambda: self._model_thread_finished() if self._model_thread is thread else None,
                )
                return
            thread.deleteLater()
        self._model_job = None
        self._model_thread = None
        entry = self._active_entry
        if self._queue_running and (self._model_result is None or entry is None):
            self._reset_broken_queue()
            self._start_next_recheck()
            return
        if self._rechecking:
            self._finish_recheck()
            return
        if self._model_result is None or entry is None:
            return
        with self._update():
            state, reason = self._model_result
            self._model_result = None
            if state == "installed":
                try:
                    self._keep_current(entry)
                except (OSError, StoreError):
                    reason = _CURRENT_FAILED_MESSAGE
                self._queue_successes += 1
                self._queue_completed_bytes += entry.size_bytes
                self._selected.add(entry.id)
                if entry == self._entry and self._progress_value != 1.0:
                    self._progress_value = 1.0
                    self._notify("progressChanged")
            if entry == self._entry:
                self._set_model_state("broken" if state == "error" else state, reason)
            if state == "installed":
                self._set_card(
                    entry, "installed", self._safe_model_message(reason) if reason else ""
                )
            elif self._queue_cancelled or state == "cancelled":
                self._set_card(entry, "available")
            else:
                self._last_failure = "no-space" if state == "no-space" else "failed"
                if state == "no-space":
                    self._no_space_size_bytes = entry.size_bytes
                self._set_card(
                    entry, self._last_failure, self._failure_message(entry, state, reason)
                )
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
            self._notify("modelsChanged")
            self._notify("selectionChanged")
            self._notify("modelReadyChanged")
            if not self._queue_running:
                self._notify("queueFinished", bool(self._queue_successes))
        self._start_next_recheck()

    def _reset_broken_queue(self) -> None:
        """Освобождает очередь без результата/задания, чтобы слоты снова работали."""
        with self._update():
            entries = self._queue_entries
            cancelled = self._queue_cancelled or self._shutting_down
            self._model_cancel.set()
            self._queue.clear()
            self._queue_entries = ()
            self._queue_running = False
            self._active_entry = None
            self._model_result = None
            self._rechecking = False
            self._recheck_card = (None, None, None)
            self._recheck_model_state = ("", "")
            self._queue_cancelled = False
            self._queue_successes = 0
            self._queue_completed_bytes = 0
            self._queue_total_bytes = 0
            self._queue_current = None
            self._last_failure = "failed"
            self._tracker.reset(self._clock())
            self._last_estimate_at = float("-inf")
            log.warning("Несогласованное состояние очереди моделей; очередь сброшена")
            for entry in entries:
                if self._card_states.get(entry.id) in {"queued", "downloading", "verifying"}:
                    self._set_card(entry, "available" if cancelled else "failed")
            if self._entry in entries:
                self._set_model_state("cancelled" if cancelled else "broken")
            if self._progress_value:
                self._progress_value = 0.0
                self._notify("progressChanged")
            self._set_download_state("idle" if cancelled else "failed")
            self._set_download_progress(0)
            self._set_estimates("", "")
            self._notify("selectionChanged")
            self._notify("modelReadyChanged")
            self._notify("queueFinished", False)

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
        with self._update():
            if (
                self._shutting_down
                or self._queue_running
                or self._rechecking
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
            self._set_card(entry, "verifying")
            if entry == self._entry:
                self._set_model_state("verifying")
            self._set_download_progress(0)
            self._set_estimates("", "")
            self._set_download_state("verifying")
            thread.start()

    def _finish_recheck(self) -> None:
        with self._update():
            entry, model = self._active_entry, self._model
            state, reason = self._model_result or ("cancelled", "")
            old_card, old_model_state = self._recheck_card, self._recheck_model_state
            self._rechecking = False
            self._active_entry = None
            self._model_result = None
            self._recheck_card = (None, None, None)
            self._recheck_model_state = ("", "")
            if self._model_cancel.is_set() or self._shutting_down:
                state = "cancelled"
            try:
                if model is not None and entry is not None:
                    if state == "installed":
                        try:
                            model.mark_ok(entry.id, entry.revision)
                        except StoreError:
                            log.warning(
                                "Хранилище отклонило успешный результат перепроверки модели"
                            )
                            state, reason = "failed", "Модель нужно переустановить."
                        else:
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
                    old_state, old_message, old_progress = old_card
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
                        self._set_model_state(*old_model_state)
                    self._notify("modelsChanged")
                else:
                    self._set_card(entry, state, reason)
                    if entry == self._entry:
                        self._set_model_state("broken" if state == "failed" else state, reason)
                    self._notify("modelReadyChanged")
                    self._notify("selectionChanged")
            self._set_download_state(
                "idle" if state == "cancelled" else "done" if state == "installed" else "failed"
            )
        self._start_next_recheck()

    def _start_model_job(self, source: Path | None = None) -> None:
        with self._update():
            if (
                self._shutting_down
                or self._rechecking
                or self._queue_cancelled
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
            self._model_staged("downloading" if source is None else "verifying")
            self._model_progressed(0.0, 0.0, -1.0)
            thread.start()

    def download(self) -> None:
        with self._update():
            if (
                self._shutting_down
                or self._model_thread is not None
                or self._queue_running
                or self._rechecking
            ):
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
        self.cancelDownloads()
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
                thread.finished.connect(thread.deleteLater)
                self._model_job = None
                self._model_thread = None
