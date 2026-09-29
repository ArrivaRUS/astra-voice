"""Небольшой приватный кэш ответов проверок обновлений."""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)
MAX_CACHE_BYTES = 1024 * 1024
MAX_BODY_BYTES = 256 * 1024


def valid_etag(value: str | None) -> bool:
    """Разрешает только короткие видимые ASCII-значения ETag."""
    return value is not None and 0 < len(value) <= 256 and all(33 <= ord(c) <= 126 for c in value)


@dataclass(frozen=True)
class CacheEntry:
    """Ответ источника и ограничения следующей проверки."""

    etag: str | None = None
    body: str | None = None
    stored_at: float | None = None
    rate_limited_until: float | None = None
    backoff_until: float | None = None
    failures: int = 0
    url: str | None = None


def _entry(value: object) -> CacheEntry | None:
    if not isinstance(value, dict) or set(value) != set(CacheEntry.__dataclass_fields__):
        return None
    etag = value["etag"]
    body = value["body"]
    failures = value["failures"]
    url = value["url"]
    if url is not None and not isinstance(url, str):
        return None
    if etag is not None and (not isinstance(etag, str) or not valid_etag(etag)):
        return None
    if body is not None and (
        not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BODY_BYTES
    ):
        return None
    if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
        return None
    for name in ("stored_at", "rate_limited_until", "backoff_until"):
        number = value[name]
        if number is not None and (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
        ):
            return None
    return CacheEntry(**value)


class UpdateCache:
    """Читает и атомарно заменяет update-cache.json."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        if self._path is None:
            from astra_voice.core.paths import state_dir

            self._path = state_dir() / "update-cache.json"
        return self._path

    def _read(self) -> dict[str, CacheEntry]:
        try:
            path = self.path
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("тип файла")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("тип файла")
                raw = os.read(descriptor, MAX_CACHE_BYTES + 1)
            finally:
                os.close(descriptor)
            if len(raw) > MAX_CACHE_BYTES:
                raise ValueError("размер")
            parsed: object = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
                raise ValueError("формат")
            entries: dict[str, CacheEntry] = {}
            for key, value in parsed.items():
                item = _entry(value)
                if item is None:
                    raise ValueError("запись")
                entries[key] = item
            return entries
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError, TypeError, OverflowError):
            log.warning("Кэш проверок недоступен или повреждён")
            return {}

    def get(self, key: str) -> CacheEntry:
        """Возвращает запись или пустую запись при отсутствии кэша."""
        with self._lock:
            return self._read().get(key, CacheEntry())

    def set(self, key: str, entry: CacheEntry) -> None:
        """Сохраняет запись без больших тел и негодных ETag."""
        if not isinstance(key, str) or not key:
            raise ValueError("Пустой ключ кэша")
        if entry.body is not None and len(entry.body.encode("utf-8")) > MAX_BODY_BYTES:
            entry = replace(entry, body=None, etag=None)
        if not valid_etag(entry.etag):
            entry = replace(entry, etag=None)
        with self._lock:
            entries = self._read()
            entries[key] = entry
            raw = json.dumps(
                {name: asdict(item) for name, item in entries.items()},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(raw) > MAX_CACHE_BYTES:
                log.warning("Кэш проверок превышает допустимый размер")
                return
            temporary: Path | None = None
            try:
                path = self.path
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(prefix=".update-cache-", dir=path.parent)
                temporary = Path(name)
                with os.fdopen(descriptor, "wb") as handle:
                    os.fchmod(handle.fileno(), 0o600)
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                log.warning("Не удалось сохранить кэш проверок")
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
