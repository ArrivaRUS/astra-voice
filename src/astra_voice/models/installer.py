"""Установка проверенных файлов: перенос → пробное распознавание → выбор модели (О2)."""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from astra_voice.models.catalog import ID_RE, CatalogEntry, FileSpec
from astra_voice.models.store import ModelRecord, ModelStore, StoreError
from astra_voice.security.verify import sha256_file
from astra_voice.worker.engine import EngineError, ModelMissingError, check_layout

log = logging.getLogger(__name__)
_COPY_CHUNK = 1 << 20
_CHECKSUM_REASON = "Файлы модели повреждены: контрольная сумма не совпала. Получите их заново."
_MISSING_REASON = "В папке не хватает файлов модели. Получите их заново."
_EXTRA_REASON = "В папке есть лишние файлы. Оставьте только файлы выбранной модели."
_SMOKE_REASON = "Модель не прошла пробное распознавание. Попробуйте установить её заново."
_DISK_REASON = "На диске недостаточно места для установки модели. Освободите место."
_FILES_REASON = "Не удалось прочитать или сохранить файлы модели. Попробуйте ещё раз."


@dataclass(frozen=True)
class SmokeResult:
    """Результат пробного распознавания; причина предназначена для пользователя."""

    ok: bool
    text: str = ""
    reason: str = ""


SmokeCheck = Callable[[Path, CatalogEntry], SmokeResult]


@dataclass(frozen=True)
class InstallResult:
    """Итог установки и запись установленной, в том числе неисправной, модели."""

    state: Literal["ok", "broken", "error"]
    reason: str = ""
    record: ModelRecord | None = None


class _InstallError(Exception):
    """Отказ проверки с понятной пользователю причиной."""


def _file_path(directory: Path, name: str) -> Path:
    """Проверяет границу resolve перед чтением и каждой записью (У6)."""
    target = directory / name
    # Поддерживаемые check_layout раскладки содержат только файлы первого уровня.
    if (
        not name
        or name in {".", ".."}
        or Path(name).name != name
        or "\\" in name
        or not target.resolve().is_relative_to(directory.resolve())
        or target.is_symlink()
    ):
        log.warning("Недопустимый путь файла модели: %r в %s", name, directory)
        raise _InstallError("Файлы в папке не подходят для выбранной модели.")
    return target


def _check_contents(directory: Path, entry: CatalogEntry, *, allow_parts: bool) -> tuple[Path, ...]:
    """Отвергает всё вне списка каталога, включая подкаталоги и ссылки."""
    names = {file.path for file in entry.files}
    parts: list[Path] = []
    for child in directory.iterdir():
        if child.is_symlink() or not child.is_file():
            log.warning("Вместо обычного файла модели обнаружен %s", child)
            raise _InstallError(_EXTRA_REASON)
        if child.name in names:
            continue
        if allow_parts and child.name.endswith(".part"):
            parts.append(_file_path(directory, child.name))
            continue
        log.warning("Лишний файл модели: %s", child)
        raise _InstallError(_EXTRA_REASON)
    for file in entry.files:
        if not _file_path(directory, file.path).is_file():
            log.warning("Отсутствует файл модели: %s / %s", directory, file.path)
            raise _InstallError(_MISSING_REASON)
    return tuple(parts)


def _copy_file(source: Path, target: Path, spec: FileSpec) -> None:
    """Считает sha256 по копируемым блокам и ограничивает фактический размер."""
    digest = hashlib.sha256()
    copied = 0
    with source.open("rb") as reader, target.open("xb") as writer:
        os.fchmod(writer.fileno(), 0o600)
        while block := reader.read(min(_COPY_CHUNK, spec.size - copied + 1)):
            copied += len(block)
            if copied > spec.size:
                log.warning("Файл модели превышает заявленный размер: %s", source)
                raise _InstallError(_CHECKSUM_REASON)
            digest.update(block)
            writer.write(block)
        if not hmac.compare_digest(digest.hexdigest(), spec.sha256) or copied != spec.size:
            log.warning("Не совпали sha256 или размер при копировании %s", source)
            raise _InstallError(_CHECKSUM_REASON)
        writer.flush()
        os.fsync(writer.fileno())


def _error_result(exc: Exception) -> InstallResult:
    log.exception("Не удалось установить модель: %s", exc)
    if isinstance(exc, _InstallError):
        reason = str(exc)
    elif (
        isinstance(exc, StoreError)
        and exc.code == "disk-full"
        or isinstance(exc, OSError)
        and exc.errno in {errno.ENOSPC, errno.EDQUOT}
    ):
        reason = _DISK_REASON
    else:
        # Сообщения файловой системы и хранилища могут содержать внутренние пути.
        reason = _FILES_REASON
    return InstallResult("error", reason)


class Installer:
    """Меняет текущую модель только после успешного пробного распознавания."""

    def __init__(self, store: ModelStore, smoke: SmokeCheck) -> None:
        self._store = store
        self._smoke = smoke

    def _staging_path(self, entry: CatalogEntry) -> Path:
        # staging_dir создаёт папку; здесь отсутствие проверяется без побочных эффектов.
        if ID_RE.fullmatch(entry.id) is None or ID_RE.fullmatch(entry.revision) is None:
            raise _InstallError("Не удалось определить модель. Выберите её в списке.")
        staging = self._store.root / entry.id / f"{entry.revision}.partial"
        if (
            not staging.resolve().is_relative_to(self._store.root.resolve())
            or staging.is_symlink()
            or staging.parent.is_symlink()
        ):
            log.warning("Недопустимый каталог staging: %s", staging)
            raise _InstallError(_FILES_REASON)
        return staging

    def install_from_staging(self, entry: CatalogEntry) -> InstallResult:
        """Проверяет весь набор до переноса, затем выполняет оставшиеся шаги О2."""
        try:
            staging = self._staging_path(entry)
            if not staging.is_dir():
                return InstallResult("error", "Файлы для установки не найдены. Получите их заново.")

            # О2.1: даже проверенные загрузчиком файлы перечитываются перед переносом.
            for file in entry.files:
                source = _file_path(staging, file.path)
                if not source.is_file():
                    log.warning("Отсутствует файл модели: %s", source)
                    raise _InstallError(_MISSING_REASON)
                if not hmac.compare_digest(sha256_file(source), file.sha256):
                    log.warning("Не совпала sha256 файла модели: %s", source)
                    raise _InstallError(_CHECKSUM_REASON)
                if source.stat().st_size != file.size:
                    log.warning("Не совпал размер файла модели: %s", source)
                    raise _InstallError(_CHECKSUM_REASON)

            # О2.2: остатки докачки разрешены, но в установленный набор не попадают.
            for part in _check_contents(staging, entry, allow_parts=True):
                part.unlink()
            try:
                check_layout(staging, entry.layout, entry.variant)
            except (EngineError, ValueError) as exc:
                log.exception("Набор файлов не соответствует раскладке модели")
                reason = (
                    _MISSING_REASON
                    if isinstance(exc, ModelMissingError)
                    else "Файлы в папке не подходят для выбранной модели."
                )
                raise _InstallError(reason) from exc

            # Служебные метаданные добавляем сами и только после проверки состава.
            metadata = _file_path(staging, "state.json")
            try:
                with metadata.open("x", encoding="utf-8") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(
                        {
                            "layout": entry.layout,
                            "variant": entry.variant,
                            "size_bytes": entry.size_bytes,
                        },
                        stream,
                        ensure_ascii=False,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                directory = self._store.commit(entry.id, entry.revision)  # О2.3
            finally:
                # При отказе переноса оставляем набор пригодным для повторной проверки.
                metadata.unlink(missing_ok=True)

            record = ModelRecord(
                entry.id, entry.revision, directory, entry.layout, entry.variant, entry.size_bytes
            )
            try:
                smoke = self._smoke(directory, entry)  # О2.4: уже установленный каталог.
            except Exception:
                log.exception(
                    "Ошибка пробного распознавания модели %s/%s", entry.id, entry.revision
                )
                smoke = SmokeResult(False, reason=_SMOKE_REASON)
            if not smoke.ok:
                reason = smoke.reason or _SMOKE_REASON
                self._store.mark_broken(entry.id, entry.revision, reason)  # О2.5
                return InstallResult(
                    "broken", reason, replace(record, state="broken", reason=reason)
                )
            self._store.set_current(entry.id, entry.revision)  # О2.6
            return InstallResult("ok", "", record)
        except (_InstallError, StoreError, OSError, ValueError, RuntimeError) as exc:
            return _error_result(exc)

    def install_from_path(self, src: Path, entry: CatalogEntry | None) -> InstallResult:
        """Копирует только заявленные файлы, проверяя байты по ходу копирования."""
        if entry is None:
            return InstallResult(
                "error", "Не удалось определить, какая это модель. Выберите её в списке."
            )
        staging: Path | None = None
        try:
            if not src.exists():
                return InstallResult("error", "Выбранная папка не найдена. Выберите её заново.")
            if not src.is_dir():
                # Установка из архивов появится в следующей вехе.
                return InstallResult("error", "Выберите папку с файлами модели.")
            _check_contents(src, entry, allow_parts=False)
            if not self._store.disk_ok(entry.size_bytes):
                return InstallResult("error", _DISK_REASON)
            destination = self._staging_path(entry)
            if src.resolve().is_relative_to(
                destination.resolve()
            ) or destination.resolve().is_relative_to(src.resolve()):
                raise _InstallError("Выберите другую папку с файлами модели.")
            for file in entry.files:
                _file_path(destination, file.path)
            if destination.exists():
                shutil.rmtree(destination)
            staging = self._store.staging_dir(entry.id, entry.revision)
            for file in entry.files:
                source = _file_path(src, file.path)
                target = _file_path(staging, file.path)
                _copy_file(source, target, file)
            return self.install_from_staging(entry)
        except (_InstallError, StoreError, OSError, ValueError, RuntimeError) as exc:
            return _error_result(exc)
        finally:
            if staging is not None and staging.exists():
                try:
                    shutil.rmtree(staging)
                except OSError:
                    log.exception("Не удалось очистить staging после копирования: %s", staging)
