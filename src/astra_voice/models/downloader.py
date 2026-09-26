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
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

from astra_voice.models.catalog import ID_RE, CatalogEntry, FileSpec
from astra_voice.models.store import ModelStore, StoreError
from astra_voice.net.http import HttpClient, NetworkError

log = logging.getLogger(__name__)
_CHUNK_SIZE = 65536
_FILE_DEADLINE_S = 6 * 60 * 60
_PROGRESS_INTERVAL_S = 0.2
_SPEED_WINDOW_S = 5.0
INACTIVITY_TIMEOUT_S = 30.0
_CONTENT_RANGE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", re.IGNORECASE)
# Сетевые отказы одного источника: пробуем следующий. Повреждение, отмена,
# выключенная сеть и запрещённый источник смены источника не оправдывают.
_MIRROR_CODES = frozenset({"host-unreachable", "timeout", "bad-status", "short-read"})


def fail_reason(code: str) -> str:
    """Возвращает только публичную причину отказа, без текста исключения."""
    if code == "cancelled":
        raise ValueError("Отмена не является причиной отказа.")
    if code in {"bad-checksum", "too-large"}:
        return "bad-sha"
    if code == "disk-full":
        return "no-space"
    if code in {"no-network", "host-unreachable", "short-read"}:
        return "no-network"
    if code in {"not-allowed", "bad-path"}:
        return "not-allowed"
    if code == "timeout":
        return "timeout"
    return "server"


def corp_origin(base: str | None, *, warn: bool = True) -> str | None:
    if base is None:
        return None
    try:
        parts = urlsplit(base)
        if not parts.netloc.isascii():
            raise ValueError
        host = parts.hostname
        port = parts.port
        if (
            parts.scheme != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.netloc.endswith(":")
            or "%" in parts.netloc
            or "%" in parts.path
            or "?" in base
            or "#" in base
            or "\\" in base
            or any(ord(char) <= 32 or ord(char) == 127 for char in base)
            or any(segment in {".", ".."} for segment in unquote(parts.path).split("/"))
            or port == 0
        ):
            raise ValueError
    except ValueError:
        if warn:
            log.warning("Корпоративный источник не используется: некорректный адрес.")
        return None
    return base.rstrip("/")


def _source_kind(host: str) -> str:
    name = host.split(":", 1)[0].lower()
    if name in {"huggingface.co", "hf.co"} or name.endswith((".huggingface.co", ".hf.co")):
        return "hf"
    return "github"


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
            "short-read": "Соединение оборвалось до конца загрузки файла.",
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


def _checked_file_path(path: str) -> str:
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise DownloadError("bad-path")
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


def _remaining_model_bytes(staging: Path, entry: CatalogEntry, cancel: threading.Event) -> int:
    if not staging.is_dir():
        return entry.size_bytes
    remaining = entry.size_bytes
    for file in entry.files:
        _check_cancel(cancel)
        path = _checked_file_path(file.path)
        target = _checked_path(staging, staging / path)
        part = _checked_path(staging, staging / (path + ".part"))
        if target.is_file() and target.stat().st_size == file.size:
            remaining -= file.size
        elif part.is_file():
            size = part.stat().st_size
            if 0 < size < file.size:
                remaining -= size
    return max(0, remaining)


def remaining_bytes(
    store: ModelStore, entry: CatalogEntry, *, cancel: threading.Event | None = None
) -> int:
    """Остаток загрузки с учётом готовых файлов и .part, без создания staging."""
    if ID_RE.fullmatch(entry.id) is None or ID_RE.fullmatch(entry.revision) is None:
        raise StoreError("bad-id")
    directory = store.root / entry.id
    staging = directory / f"{entry.revision}.partial"
    if directory.is_symlink() or staging.is_symlink():
        raise StoreError("bad-id", "Каталог модели не должен быть символической ссылкой.")
    return _remaining_model_bytes(staging, entry, cancel or threading.Event())


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
        self._last_emitted = 0

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
        visible_done = max(self._last_emitted, self.done)
        eta = max(0, self._entry.size_bytes - visible_done) / speed if speed > 0 else None
        self._last_report = now
        self._last_emitted = visible_done
        try:
            self._callback(
                Progress(
                    visible_done,
                    self._entry.size_bytes,
                    speed,
                    eta,
                    index,
                    len(self._entry.files),
                )
            )
        except Exception:
            log.warning("Не удалось сообщить о ходе загрузки модели.")


class Downloader:
    """Проверяет файлы в staging; установку и проверку ОЗУ выполняет вызывающий."""

    def __init__(
        self, http: HttpClient, store: ModelStore, *, corp_base: str | None = None
    ) -> None:
        self._http = http
        self._store = store
        self._corp_base = corp_origin(corp_base, warn=False)

    def download(
        self,
        entry: CatalogEntry,
        *,
        progress: Callable[[Progress], None],
        cancel: threading.Event,
        source: Callable[[str], None] | None = None,
    ) -> Path:
        """Возвращает staging; место повторно проверяется перед записью каждые 0,5 с."""
        try:
            if not self._store.disk_ok(remaining_bytes(self._store, entry, cancel=cancel)):
                raise DownloadError("disk-full")
            _check_cancel(cancel)
            staging = self._store.staging_dir(entry.id, entry.revision)
            destinations = [
                (
                    _checked_path(staging, staging / _checked_file_path(file.path)),
                    _checked_path(staging, staging / (_checked_file_path(file.path) + ".part")),
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
                            entry, file, part, budget, reporter, index, cancel, source
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
        source: Callable[[str], None] | None,
    ) -> None:
        """Перебирает источники каталога по порядку; проверки байт у всех одни.

        Хост в журнал и на экран не выносим: пользователю он ничего не говорит.
        """
        sources = [
            (_source_kind(host), "https://" + host + file.url_path)
            for host in (entry.host, *entry.mirrors)
        ]
        if self._corp_base is not None:
            suffix = "/".join(quote(part, safe="") for part in file.path.split("/"))
            sources.insert(
                0,
                (
                    "corp",
                    self._corp_base
                    + "/"
                    + quote(entry.id, safe="")
                    + "/"
                    + quote(entry.revision, safe="")
                    + "/"
                    + suffix,
                ),
            )
        done_before = reporter.done
        for number, (kind, url) in enumerate(sources, start=1):
            _check_cancel(cancel)
            if source is not None:
                try:
                    source(kind)
                except Exception:
                    log.warning("Не удалось сообщить об источнике загрузки модели.")
            try:
                resumed = part.exists() and 0 < part.stat().st_size < file.size
                for attempt in range(2 if resumed else 1):
                    try:
                        self._download_file(
                            url,
                            file,
                            part,
                            budget,
                            reporter,
                            index,
                            cancel,
                            entry.size_bytes,
                            scope="corp" if kind == "corp" else "public",
                        )
                        break
                    except DownloadError as error:
                        if error.code in {"bad-checksum", "too-large"}:
                            self._discard_bad_part(part, budget)
                        if error.code != "bad-checksum" or not resumed or attempt:
                            raise
                        log.warning(
                            "Источник %d из %d (%s) отдал файл с неверной контрольной суммой; "
                            "повторяем с начала",
                            number,
                            len(sources),
                            kind,
                        )
                        reporter.done = done_before
            except (DownloadError, NetworkError) as exc:
                if exc.code == "bad-checksum":
                    log.warning(
                        "Источник %d из %d (%s) отдал файл с неверной контрольной суммой",
                        number,
                        len(sources),
                        kind,
                    )
                if exc.code not in _MIRROR_CODES | {"bad-checksum", "too-large"} or number == len(
                    sources
                ):
                    raise
                if exc.code != "bad-checksum":
                    log.warning(
                        "Источник %d из %d (%s) не ответил: %s %s; пробуем следующий",
                        number,
                        len(sources),
                        kind,
                        exc.code,
                        exc.status
                        if isinstance(exc, NetworkError) and exc.status is not None
                        else "-",
                    )
                # Следующая попытка сама посчитает уже загруженную часть файла.
                reporter.done = done_before
                continue
            return

    @staticmethod
    def _discard_bad_part(part: Path, budget: _Budget) -> None:
        if part.exists():
            budget.used -= part.stat().st_size
            part.unlink()

    def _download_file(
        self,
        url: str,
        file: FileSpec,
        part: Path,
        budget: _Budget,
        reporter: _Reporter,
        index: int,
        cancel: threading.Event,
        model_size: int,
        *,
        scope: Literal["public", "corp"] = "public",
    ) -> None:
        deadline_at = time.monotonic() + _FILE_DEADLINE_S
        digest = hashlib.sha256()
        existing = part.stat().st_size if part.exists() else 0
        offset = existing if 0 < existing < file.size else 0
        last_disk_check = float("-inf")
        with part.open("r+b" if part.exists() else "w+b") as stream:
            if offset:
                remaining = offset
                while remaining:
                    _check_cancel(cancel)
                    block = stream.read(min(_CHUNK_SIZE, remaining))
                    if not block:
                        raise DownloadError("short-read", "Сохранённая часть файла неполная.")
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
                    idle_timeout_s=INACTIVITY_TIMEOUT_S,
                    scope=scope,
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
                        now = time.monotonic()
                        if now - last_disk_check >= 0.5:
                            if not self._store.disk_ok(max(0, model_size - reporter.done)):
                                raise DownloadError("disk-full")
                            last_disk_check = now
                        count = stream.write(block)
                        digest.update(block[:count])
                        written += count
                        budget.used += count
                        reporter.written(count)
                        reporter.report(index)
                    _check_cancel(cancel)
                    if written < file.size:
                        raise DownloadError(
                            "short-read",
                            "Файл загружен не полностью. Попробуйте загрузить ещё раз.",
                        )
                    if not hmac.compare_digest(digest.hexdigest(), file.sha256):
                        raise DownloadError("bad-checksum")
                    stream.flush()
                    os.fsync(stream.fileno())
                    return
