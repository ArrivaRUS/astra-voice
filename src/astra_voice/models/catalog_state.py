"""Состояние применённого каталога и защита от отката (PRD §7.3, US-6.6).

Программа помнит, какой список моделей она уже принимала: `trust_epoch`,
`serial` и sha256 самого файла. Более старый список не применяется — иначе
подменой файла можно вернуть отозванную ревизию модели. Состояние лежит в
`~/.local/state/astra-voice/catalog-state.json`, а не в очищаемом `~/.cache`.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

STATE_FILE_NAME = "catalog-state.json"
STATE_MAX_BYTES = 4096
STALE_MESSAGE = "Список моделей устарел: программа уже получала более свежий список."


@dataclass(frozen=True)
class CatalogState:
    """Принятый каталог: эпоха доверия, серийный номер и sha256 файла."""

    trust_epoch: int
    serial: int
    sha256: str


def state_path(directory: Path) -> Path:
    """Путь файла состояния внутри каталога состояния программы."""
    return directory / STATE_FILE_NAME


def read_state(path: Path) -> CatalogState | None:
    """Читает состояние; повреждённый или чужой файл считается отсутствующим."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > STATE_MAX_BYTES:
            log.warning("Состояние каталога не является обычным файлом или слишком велико.")
            return None
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        log.warning("Не удалось прочитать состояние каталога.")
        return None
    try:
        document: object = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        log.warning("Состояние каталога повреждено и будет перезаписано.")
        return None
    if not isinstance(document, dict):
        log.warning("Состояние каталога повреждено и будет перезаписано.")
        return None
    trust_epoch = document.get("trust_epoch")
    serial = document.get("serial")
    sha256 = document.get("sha256")
    if (
        type(trust_epoch) is not int
        or type(serial) is not int
        or trust_epoch < 1
        or serial < 1
        or not isinstance(sha256, str)
        or len(sha256) != 64
    ):
        log.warning("Состояние каталога неполно и будет перезаписано.")
        return None
    return CatalogState(trust_epoch=trust_epoch, serial=serial, sha256=sha256)


def write_state(path: Path, state: CatalogState) -> None:
    """Публикует состояние 0600 через соседний временный файл (как в store)."""
    payload = (
        json.dumps(
            {"trust_epoch": state.trust_epoch, "serial": state.serial, "sha256": state.sha256},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def is_newer(candidate: CatalogState, applied: CatalogState | None) -> bool:
    """Сравнивает пару (trust_epoch, serial); равная пара — только те же байты."""
    if applied is None:
        return True
    if candidate.trust_epoch != applied.trust_epoch:
        return candidate.trust_epoch > applied.trust_epoch
    if candidate.serial != applied.serial:
        return candidate.serial > applied.serial
    return hmac.compare_digest(candidate.sha256, applied.sha256)


def apply_state(path: Path, candidate: CatalogState) -> bool:
    """Принимает каталог не старше применённого; True — состояние переписано.

    Возвращает False, если тот же каталог уже применён (перезаписывать нечего).
    Отказ — :class:`ValueError` с текстом для строки состояния.
    """
    applied = read_state(path)
    if not is_newer(candidate, applied):
        log.warning(
            "Каталог отвергнут как устаревший: получено (%d, %d), применено (%s).",
            candidate.trust_epoch,
            candidate.serial,
            "нет" if applied is None else f"{applied.trust_epoch}, {applied.serial}",
        )
        raise ValueError(STALE_MESSAGE)
    if applied is not None and candidate == applied:
        return False
    write_state(path, candidate)
    return True
