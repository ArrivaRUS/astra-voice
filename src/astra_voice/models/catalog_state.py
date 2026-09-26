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
# При indent=2 первая пара добавляет 186 байт, следующие — по 184
# (два идентификатора по 64 ASCII-символа). Сохраняем прежние 4096 байт
# для остальных полей и добавляем ровно место для 1024 пар.
STATE_MAX_BYTES = 4096 + 186 + 1023 * 184
STALE_MESSAGE = "Список моделей устарел: программа уже получала более свежий список."


@dataclass(frozen=True)
class CatalogState:
    """Принятый каталог: эпоха доверия, серийный номер и sha256 файла."""

    trust_epoch: int
    serial: int
    sha256: str
    revoked: tuple[tuple[str, str], ...] | None = None


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
    revoked: tuple[tuple[str, str], ...] | None = None
    if "revoked" in document:
        value = document["revoked"]
        # Локальный импорт исключает цикл: catalog импортирует этот модуль.
        from astra_voice.models.catalog import ID_RE

        if (
            isinstance(value, list)
            and len(value) <= 1024
            and all(
                isinstance(item, dict)
                and set(item) == {"model_id", "revision"}
                and type(item["model_id"]) is str
                and type(item["revision"]) is str
                and ID_RE.fullmatch(item["model_id"]) is not None
                and ID_RE.fullmatch(item["revision"]) is not None
                for item in value
            )
        ):
            revoked = tuple((item["model_id"], item["revision"]) for item in value)
        else:
            log.warning("Снимок отозванных версий в состоянии каталога повреждён.")
    return CatalogState(trust_epoch=trust_epoch, serial=serial, sha256=sha256, revoked=revoked)


def write_state(path: Path, state: CatalogState) -> None:
    """Публикует состояние 0600 через соседний временный файл (как в store)."""
    document: dict[str, object] = {
        "trust_epoch": state.trust_epoch,
        "serial": state.serial,
        "sha256": state.sha256,
    }
    if state.revoked is not None:
        document["revoked"] = [
            {"model_id": model_id, "revision": revision} for model_id, revision in state.revoked
        ]
    payload = (
        json.dumps(
            document,
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
    if applied is not None and (candidate.trust_epoch, candidate.serial, candidate.sha256) == (
        applied.trust_epoch,
        applied.serial,
        applied.sha256,
    ):
        if applied.revoked == candidate.revoked:
            return False
    write_state(path, candidate)
    return True
