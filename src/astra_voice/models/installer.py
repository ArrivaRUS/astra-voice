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

from astra_voice.models.catalog import ID_RE, Catalog, CatalogEntry, FileSpec
from astra_voice.models.store import ModelRecord, ModelStore, StoreError
from astra_voice.security.verify import HashCancelledError
from astra_voice.security.verify import sha256_file as sha256_file
from astra_voice.worker.engine import EngineError, ModelMissingError, check_layout

log = logging.getLogger(__name__)
_COPY_CHUNK = 1 << 20
_CHECKSUM_REASON = "Файлы модели повреждены: контрольная сумма не совпала. Получите их заново."
_MISSING_REASON = "В папке не хватает файлов модели. Получите их заново."
_EXTRA_REASON = "В папке есть лишние файлы. Оставьте только файлы выбранной модели."
_LAYOUT_REASON = "Файлы в папке не подходят для выбранной модели."
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
ReasonCode = Literal["", "checksum", "layout", "selfcheck", "disk", "cancelled", "revoked"]


@dataclass(frozen=True)
class InstallResult:
    """Итог установки и запись установленной, в том числе неисправной, модели."""

    state: Literal["ok", "broken", "error"]
    reason: str = ""
    record: ModelRecord | None = None
    reason_code: ReasonCode = ""


class _InstallError(Exception):
    """Отказ проверки с понятной пользователю причиной."""

    def __init__(self, reason: str, reason_code: ReasonCode) -> None:
        super().__init__(reason)
        self.reason_code = reason_code


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
        raise _InstallError(_LAYOUT_REASON, "layout")
    return target


def _check_contents(directory: Path, entry: CatalogEntry, *, allow_parts: bool) -> tuple[Path, ...]:
    """Отвергает всё вне списка каталога, включая подкаталоги и ссылки."""
    names = {file.path for file in entry.files}
    parts: list[Path] = []
    for child in directory.iterdir():
        if child.is_symlink() or not child.is_file():
            log.warning("Вместо обычного файла модели обнаружен %s", child)
            raise _InstallError(_EXTRA_REASON, "layout")
        if child.name in names:
            continue
        if allow_parts and child.name.endswith(".part"):
            parts.append(_file_path(directory, child.name))
            continue
        log.warning("Лишний файл модели: %s", child)
        raise _InstallError(_EXTRA_REASON, "layout")
    for file in entry.files:
        if not _file_path(directory, file.path).is_file():
            log.warning("Отсутствует файл модели: %s / %s", directory, file.path)
            raise _InstallError(_MISSING_REASON, "layout")
    return tuple(parts)


def _check_files(
    directory: Path, entry: CatalogEntry, *, cancel: Callable[[], bool] | None = None
) -> None:
    """Перечитывает файлы каталога, сверяя sha256 и фактический размер."""
    for file in entry.files:
        source = _file_path(directory, file.path)
        if not source.is_file():
            log.warning("Отсутствует файл модели: %s", source)
            raise _InstallError(_MISSING_REASON, "layout")
        actual = sha256_file(source, cancel=cancel)
        if not hmac.compare_digest(actual, file.sha256):
            log.warning("Не совпала sha256 файла модели: %s", source)
            raise _InstallError(_CHECKSUM_REASON, "checksum")
        if source.stat().st_size != file.size:
            log.warning("Не совпал размер файла модели: %s", source)
            raise _InstallError(_CHECKSUM_REASON, "checksum")


def _check_model_layout(directory: Path, entry: CatalogEntry) -> None:
    try:
        check_layout(directory, entry.layout, entry.variant)
    except (EngineError, ValueError) as exc:
        log.exception("Набор файлов не соответствует раскладке модели")
        reason = _MISSING_REASON if isinstance(exc, ModelMissingError) else _LAYOUT_REASON
        raise _InstallError(reason, "layout") from exc


def verify_installed(
    directory: Path, entry: CatalogEntry, *, cancel: Callable[[], bool] | None = None
) -> tuple[bool, str]:
    """Проверяет состав, байты и раскладку без остатков докачки.

    Отмена и ошибки чтения пробрасываются: они не доказывают повреждение модели.
    Причина отказа проверки предназначена для пользователя и не содержит путей.
    """
    try:
        _check_contents(directory, entry, allow_parts=False)
        _check_files(directory, entry, cancel=cancel)
        _check_model_layout(directory, entry)
    except _InstallError as exc:
        return False, str(exc)
    return True, ""


def _check_cancel(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise _InstallError("Установка отменена.", "cancelled")


def _copy_file(
    source: Path, target: Path, spec: FileSpec, *, cancel: Callable[[], bool] | None = None
) -> None:
    """Считает sha256 по копируемым блокам и ограничивает фактический размер."""
    _check_cancel(cancel)
    digest = hashlib.sha256()
    copied = 0
    with source.open("rb") as reader, target.open("xb") as writer:
        os.fchmod(writer.fileno(), 0o600)
        while block := reader.read(min(_COPY_CHUNK, spec.size - copied + 1)):
            copied += len(block)
            if copied > spec.size:
                log.warning("Файл модели превышает заявленный размер: %s", source)
                raise _InstallError(_CHECKSUM_REASON, "checksum")
            digest.update(block)
            writer.write(block)
            _check_cancel(cancel)
        if not hmac.compare_digest(digest.hexdigest(), spec.sha256) or copied != spec.size:
            log.warning("Не совпали sha256 или размер при копировании %s", source)
            raise _InstallError(_CHECKSUM_REASON, "checksum")
        writer.flush()
        os.fsync(writer.fileno())


def _error_result(exc: Exception) -> InstallResult:
    log.exception("Не удалось установить модель: %s", exc)
    reason_code: ReasonCode = ""
    if isinstance(exc, _InstallError):
        reason = str(exc)
        reason_code = exc.reason_code
    elif (
        isinstance(exc, StoreError)
        and exc.code == "disk-full"
        or isinstance(exc, OSError)
        and exc.errno in {errno.ENOSPC, errno.EDQUOT}
    ):
        reason = _DISK_REASON
        reason_code = "disk"
    else:
        # Сообщения файловой системы и хранилища могут содержать внутренние пути.
        reason = _FILES_REASON
        if isinstance(exc, StoreError) and exc.code == "bad-id":
            reason_code = "layout"
    return InstallResult("error", reason, reason_code=reason_code)


class Installer:
    """Меняет текущую модель только после успешного пробного распознавания."""

    def __init__(
        self, store: ModelStore, smoke: SmokeCheck, catalog: Catalog | None = None
    ) -> None:
        self._store = store
        self._smoke = smoke
        self._catalog = catalog

    def _check_revoked(self, entry: CatalogEntry) -> None:
        if self._catalog is not None and self._catalog.is_revoked(entry.id, entry.revision):
            raise _InstallError(
                "Эта версия модели отозвана. Выберите другую версию или модель.", "revoked"
            )

    def _staging_path(self, entry: CatalogEntry) -> Path:
        # staging_dir создаёт папку; здесь отсутствие проверяется без побочных эффектов.
        if ID_RE.fullmatch(entry.id) is None or ID_RE.fullmatch(entry.revision) is None:
            raise _InstallError("Не удалось определить модель. Выберите её в списке.", "layout")
        staging = self._store.root / entry.id / f"{entry.revision}.partial"
        if (
            not staging.resolve().is_relative_to(self._store.root.resolve())
            or staging.is_symlink()
            or staging.parent.is_symlink()
        ):
            log.warning("Недопустимый каталог staging: %s", staging)
            raise _InstallError(_FILES_REASON, "layout")
        return staging

    def install_from_staging(
        self, entry: CatalogEntry, *, cancel: Callable[[], bool] | None = None
    ) -> InstallResult:
        """Проверяет весь набор до переноса, затем выполняет оставшиеся шаги О2."""
        try:
            self._check_revoked(entry)
            staging = self._staging_path(entry)
            if not staging.is_dir():
                return InstallResult(
                    "error",
                    "Файлы для установки не найдены. Получите их заново.",
                    reason_code="layout",
                )

            # О2.1: даже проверенные загрузчиком файлы перечитываются перед переносом.
            try:
                _check_files(staging, entry, cancel=cancel)
            except HashCancelledError as exc:
                raise _InstallError("Установка отменена.", "cancelled") from exc

            # О2.2: остатки докачки разрешены, но в установленный набор не попадают.
            for part in _check_contents(staging, entry, allow_parts=True):
                part.unlink()
            _check_model_layout(staging, entry)

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
                    "broken",
                    reason,
                    replace(record, state="broken", reason=reason),
                    reason_code="selfcheck",
                )
            self._store.set_current(entry.id, entry.revision)  # О2.6
            return InstallResult("ok", "", record)
        except (_InstallError, StoreError, OSError, ValueError, RuntimeError) as exc:
            return _error_result(exc)

    def install_from_path(
        self, src: Path, entry: CatalogEntry | None, *, cancel: Callable[[], bool] | None = None
    ) -> InstallResult:
        """Копирует только заявленные файлы, проверяя байты по ходу копирования."""
        if entry is None:
            return InstallResult(
                "error", "Не удалось определить, какая это модель. Выберите её в списке."
            )
        staging: Path | None = None
        try:
            self._check_revoked(entry)
            if not src.exists():
                return InstallResult(
                    "error",
                    "Выбранная папка не найдена. Выберите её заново.",
                    reason_code="layout",
                )
            if not src.is_dir():
                # Установка из архивов появится в следующей вехе.
                return InstallResult(
                    "error", "Выберите папку с файлами модели.", reason_code="layout"
                )
            _check_contents(src, entry, allow_parts=False)
            if not self._store.disk_ok(entry.size_bytes):
                return InstallResult("error", _DISK_REASON, reason_code="disk")
            destination = self._staging_path(entry)
            if src.resolve().is_relative_to(
                destination.resolve()
            ) or destination.resolve().is_relative_to(src.resolve()):
                raise _InstallError("Выберите другую папку с файлами модели.", "layout")
            for file in entry.files:
                _file_path(destination, file.path)
            if destination.exists():
                shutil.rmtree(destination)
            staging = self._store.staging_dir(entry.id, entry.revision)
            for file in entry.files:
                source = _file_path(src, file.path)
                target = _file_path(staging, file.path)
                _copy_file(source, target, file, cancel=cancel)
            return self.install_from_staging(entry, cancel=cancel)
        except (_InstallError, StoreError, OSError, ValueError, RuntimeError) as exc:
            return _error_result(exc)
        finally:
            if staging is not None and staging.exists():
                try:
                    shutil.rmtree(staging)
                except OSError:
                    log.exception("Не удалось очистить staging после копирования: %s", staging)
