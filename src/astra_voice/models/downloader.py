"""Последовательная загрузка файлов модели в staging с проверкой размера и SHA-256."""

from __future__ import annotations

import errno
import hashlib
import hmac
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from astra_voice.models.catalog import CatalogEntry, FileSpec
from astra_voice.models.store import ModelStore, StoreError
from astra_voice.net.http import HttpClient, NetworkError

log = logging.getLogger(__name__)
_CHUNK_SIZE = 65536
_FILE_DEADLINE_S = 6 * 60 * 60
_PROGRESS_INTERVAL_S = 0.2
_SPEED_WINDOW_S = 5.0
_CONTENT_RANGE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", re.IGNORECASE)
# Сетевые отказы одного источника: пробуем следующий. Повреждение, отмена,
# выключенная сеть и запрещённый источник смены источника не оправдывают.
_MIRROR_CODES = frozenset({"host-unreachable", "timeout", "bad-status"})


@dataclass(frozen=True)
class Progress:
    """Общий прогресс модели; номер текущего файла начинается с единицы."""

    bytes_done: int
    bytes_total: int
    speed_bps: float
    eta_s: float | None
    file_index: int
    file_count: int


class DownloadError(Exception):
    """Ошибка загрузки со стабильным кодом и понятным сообщением."""

    code: str
    message: str

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or {
            "no-network": "Нет доступа к сети.",
            "host-unreachable": "Не удалось связаться с сервером.",
            "bad-checksum": "Файл повреждён: контрольная сумма не совпадает.",
            "too-large": "Размер не совпадает: сервер прислал слишком много данных.",
            "cancelled": "Загрузка отменена.",
            "disk-full": "На диске недостаточно места для загрузки модели.",
            "bad-status": "Сервер прислал неподходящий ответ. Попробуйте загрузить ещё раз.",
            "timeout": "Время ожидания загрузки истекло.",
            "not-allowed": "Источник загрузки не разрешён.",
            "bad-path": "Путь к файлу выходит за пределы каталога загрузки.",
        }.get(code, "Не удалось загрузить модель.")
        super().__init__(self.message)


def _check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise DownloadError("cancelled")


def _checked_path(staging: Path, path: Path) -> Path:
    """Проверяет также существующие символические ссылки до создания подкаталогов."""
    try:
        root, target = staging.resolve(), path.resolve()
        if target == root or not target.is_relative_to(root):
            raise DownloadError("bad-path")
    except (OSError, RuntimeError, ValueError) as exc:
        raise DownloadError("bad-path") from exc
    return path


def _staging_size(staging: Path) -> int:
    size = 0

    def on_error(error: OSError) -> None:
        raise error

    for parent, directories, files in os.walk(staging, onerror=on_error, followlinks=False):
        for name in directories:
            _checked_path(staging, Path(parent) / name)
        for name in files:
            path = _checked_path(staging, Path(parent) / name)
            size += path.stat().st_size
    return size


def _range_matches(value: str, offset: int, size: int) -> bool:
    match = _CONTENT_RANGE.fullmatch(value.strip())
    if match is None:
        return False
    try:
        start, end, total = (int(number) for number in match.groups())
    except ValueError:
        return False
    return start == offset and total == size and start <= end < total


def _ready(path: Path, file: FileSpec, cancel: threading.Event) -> bool:
    if not path.is_file() or path.stat().st_size != file.size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_CHUNK_SIZE):
            _check_cancel(cancel)
            digest.update(block)
    return hmac.compare_digest(digest.hexdigest(), file.sha256)


def _fsync_dir(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass
class _Budget:
    """Фактический объём всех файлов, включая остатки предыдущей попытки."""

    used: int
    limit: int

    def check(self, additional: int) -> None:
        if self.used + additional > self.limit:
            raise DownloadError("too-large", "Размер файлов загрузки превышает допустимый объём.")


class _Reporter:
    def __init__(self, entry: CatalogEntry, callback: Callable[[Progress], None]) -> None:
        self.done = 0
        self._entry = entry
        self._callback = callback
        self._started = time.monotonic()
        self._last_report = self._started
        self._samples: deque[tuple[float, int]] = deque()
        self._window_bytes = 0

    def written(self, count: int) -> None:
        self.done += count
        self._samples.append((time.monotonic(), count))
        self._window_bytes += count

    def report(self, index: int, *, finished: bool = False) -> None:
        now = time.monotonic()
        while self._samples and self._samples[0][0] <= now - _SPEED_WINDOW_S:
            self._window_bytes -= self._samples.popleft()[1]
        if not finished and now - self._last_report < _PROGRESS_INTERVAL_S:
            return
        elapsed = min(now - self._started, _SPEED_WINDOW_S)
        speed = self._window_bytes / elapsed if elapsed > 0 else 0.0
        eta = max(0, self._entry.size_bytes - self.done) / speed if speed > 0 else None
        self._last_report = now
        try:
            self._callback(
                Progress(
                    self.done, self._entry.size_bytes, speed, eta, index, len(self._entry.files)
                )
            )
        except Exception:
            log.exception("Не удалось сообщить о ходе загрузки модели.")


class Downloader:
    """Проверяет файлы в staging; установку и проверку ОЗУ выполняет вызывающий."""

    def __init__(self, http: HttpClient, store: ModelStore) -> None:
        self._http = http
        self._store = store

    def download(
        self,
        entry: CatalogEntry,
        *,
        progress: Callable[[Progress], None],
        cancel: threading.Event,
    ) -> Path:
        """Возвращает staging после проверки всех файлов, сохраняя докачку при обрыве."""
        try:
            if not self._store.disk_ok(entry.size_bytes):
                raise DownloadError("disk-full")
            _check_cancel(cancel)
            staging = self._store.staging_dir(entry.id, entry.revision)
            destinations = [
                (
                    _checked_path(staging, staging / file.path),
                    _checked_path(staging, staging / (file.path + ".part")),
                )
                for file in entry.files
            ]
            budget = _Budget(_staging_size(staging), entry.size_bytes * 2)
            reporter = _Reporter(entry, progress)
            for index, (file, (target, part)) in enumerate(
                zip(entry.files, destinations, strict=True), start=1
            ):
                _check_cancel(cancel)
                # Повторная проверка непосредственно перед созданием и открытием файлов.
                _checked_path(staging, target)
                _checked_path(staging, part)
                try:
                    budget.check(0)
                    if _ready(target, file, cancel):
                        reporter.done += file.size
                    else:
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        self._download_from_sources(
                            entry, file, part, budget, reporter, index, cancel
                        )
                        replaced_size = target.stat().st_size if target.exists() else 0
                        os.replace(part, target)
                        budget.used -= replaced_size
                        _fsync_dir(target.parent)
                except DownloadError as exc:
                    if exc.code in {"too-large", "bad-checksum"}:
                        part.unlink(missing_ok=True)
                    raise
                reporter.report(index, finished=True)
            return staging
        except NetworkError as exc:
            # В том числе not-allowed: политика источников остаётся ответственностью HTTP.
            raise DownloadError(exc.code, exc.message) from None
        except StoreError as exc:
            raise DownloadError(exc.code, exc.message) from exc
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise DownloadError("disk-full") from exc
            raise

    def _download_from_sources(
        self,
        entry: CatalogEntry,
        file: FileSpec,
        part: Path,
        budget: _Budget,
        reporter: _Reporter,
        index: int,
        cancel: threading.Event,
    ) -> None:
        """Перебирает источники каталога по порядку; проверки байт у всех одни.

        Хост в журнал и на экран не выносим: пользователю он ничего не говорит.
        """
        sources = (entry.host, *entry.mirrors)
        done_before = reporter.done
        for number, host in enumerate(sources, start=1):
            try:
                self._download_file(
                    "https://" + host + file.url_path,
                    file,
                    part,
                    budget,
                    reporter,
                    index,
                    cancel,
                )
            except (DownloadError, NetworkError) as exc:
                if exc.code not in _MIRROR_CODES or number == len(sources):
                    raise
                log.warning(
                    "Источник %d из %d не ответил (%s); пробуем следующий",
                    number,
                    len(sources),
                    exc.code,
                )
                # Следующая попытка сама посчитает уже загруженную часть файла.
                reporter.done = done_before
                continue
            return

    def _download_file(
        self,
        url: str,
        file: FileSpec,
        part: Path,
        budget: _Budget,
        reporter: _Reporter,
        index: int,
        cancel: threading.Event,
    ) -> None:
        deadline_at = time.monotonic() + _FILE_DEADLINE_S
        digest = hashlib.sha256()
        existing = part.stat().st_size if part.exists() else 0
        offset = existing if 0 < existing < file.size else 0
        with part.open("r+b" if part.exists() else "w+b") as stream:
            if offset:
                remaining = offset
                while remaining:
                    _check_cancel(cancel)
                    block = stream.read(min(_CHUNK_SIZE, remaining))
                    if not block:
                        raise DownloadError("bad-checksum", "Сохранённая часть файла неполная.")
                    digest.update(block)
                    remaining -= len(block)
            else:
                stream.truncate(0)
                budget.used -= existing

            for attempt in range(2):
                _check_cancel(cancel)
                with self._http.get_stream(
                    url,
                    range_from=offset or None,
                    deadline_s=max(0.0, deadline_at - time.monotonic()),
                    cancel=cancel,
                ) as response:
                    valid_range = response.status == 206 and _range_matches(
                        response.headers.get("Content-Range", ""), offset, file.size
                    )
                    if response.status != 200 and not valid_range:
                        # Неподходящий ответ нельзя читать или приписывать к старой части.
                        response.close()
                        if attempt or not 200 <= response.status < 300:
                            raise DownloadError("bad-status")
                        stream.seek(0)
                        stream.truncate(0)
                        budget.used -= offset
                        offset = 0
                        digest = hashlib.sha256()
                        continue
                    if response.status == 200 and offset:
                        stream.seek(0)
                        stream.truncate(0)
                        budget.used -= offset
                        offset = 0
                        digest = hashlib.sha256()
                    reporter.done += offset
                    written = offset
                    for block in response.iter_chunks(_CHUNK_SIZE, limit=file.size - offset + 1):
                        _check_cancel(cancel)
                        # Проверяем фактические байты до записи, независимо от Content-Length.
                        # Даже первый лишний байт не попадёт на диск; остаток ответа не читаем.
                        if written + len(block) > file.size:
                            raise DownloadError("too-large")
                        budget.check(len(block))
                        count = stream.write(block)
                        digest.update(block[:count])
                        written += count
                        budget.used += count
                        reporter.written(count)
                        reporter.report(index)
                    _check_cancel(cancel)
                    if written != file.size:
                        raise DownloadError(
                            "bad-checksum",
                            "Файл загружен не полностью. Попробуйте загрузить ещё раз.",
                        )
                    if not hmac.compare_digest(digest.hexdigest(), file.sha256):
                        raise DownloadError("bad-checksum")
                    stream.flush()
                    os.fsync(stream.fileno())
                    return
