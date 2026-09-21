"""Установленные модели: приватные каталоги, атомарная запись и восстановление."""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from astra_voice.core import paths
from astra_voice.models.catalog import ID_RE

log = logging.getLogger(__name__)
MEMINFO_PATH = Path("/proc/meminfo")
ModelState = Literal["ok", "broken"]


@dataclass(frozen=True)
class ModelRecord:
    """Состояние одной установленной ревизии."""

    id: str
    revision: str
    dir: Path
    layout: str
    variant: str
    size_bytes: int
    state: ModelState = "ok"
    reason: str = ""
    recheck: bool = False
    metadata_ok: bool = True


class StoreError(Exception):
    """Ошибка хранилища со стабильным кодом и сообщением для пользователя."""

    code: str
    message: str

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or {
            "bad-id": "Недопустимый идентификатор модели, ревизия или путь.",
            "not-found": "Ревизия модели не найдена.",
            "disk-full": "На диске недостаточно места.",
            "broken-store": "Не удалось обратиться к хранилищу моделей.",
            "invalid-metadata": (
                "Метаданные модели недоступны или неполны. Модель нужно переустановить."
            ),
        }.get(code, "Ошибка хранилища моделей.")
        super().__init__(self.message)


@contextmanager
def _store_errors() -> Iterator[None]:
    """Преобразует ошибки файловой системы в публичные коды хранилища."""
    try:
        yield
    except OSError as exc:
        code = "disk-full" if exc.errno in {errno.ENOSPC, errno.EDQUOT} else "broken-store"
        raise StoreError(code) from exc
    except paths.PathError as exc:
        raise StoreError("broken-store") from exc


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path: Path, data: dict[str, object]) -> None:
    """Публикует JSON 0600 через соседний временный файл с fsync файла и каталога."""
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Ожидался объект JSON.")
    return cast(dict[str, object], value)


def _metadata(data: dict[str, object]) -> tuple[str, str, int]:
    layout, variant, size = data.get("layout"), data.get("variant"), data.get("size_bytes")
    if not isinstance(layout, str) or not isinstance(variant, str):
        raise ValueError("Не указаны раскладка и вариант модели.")
    if type(size) is not int or size < 0:
        raise ValueError("Неверно указан размер модели.")
    return layout, variant, size


def _state_data(record: ModelRecord) -> dict[str, object]:
    return {
        "state": record.state,
        "reason": record.reason,
        "recheck": record.recheck,
        "layout": record.layout,
        "variant": record.variant,
        "size_bytes": record.size_bytes,
    }


class ModelStore:
    """Хранилище ревизий; создание staging и активация — отдельные операции."""

    def __init__(self, root: Path | None = None) -> None:
        with _store_errors():
            self.root = paths.data_dir() / "models" if root is None else root

    def _checked(self, path: Path) -> Path:
        try:
            if not path.resolve().is_relative_to(self.root.resolve()):
                raise StoreError("bad-id")
        except (OSError, RuntimeError, ValueError) as exc:
            raise StoreError("bad-id") from exc
        return path

    def _revision_path(self, model_id: str, revision: str, suffix: str = "") -> Path:
        # Проверяем оба значения до первого включения пользовательских строк в путь (У6).
        if ID_RE.fullmatch(model_id) is None or ID_RE.fullmatch(revision) is None:
            raise StoreError("bad-id")
        directory = self._checked(self.root / model_id)
        result = self._checked(directory / f"{revision}{suffix}")
        if directory.is_symlink() or result.is_symlink():
            raise StoreError("bad-id", "Каталог модели не должен быть символической ссылкой.")
        return result

    def _state_path(self, model_id: str, revision: str) -> Path:
        return self._revision_path(model_id, revision, ".json")

    def _private_dir(self, directory: Path) -> None:
        """Создаёт также отсутствующих родителей с правами 0700."""
        if not directory.exists() and not directory.is_symlink():
            self._private_dir(directory.parent)
        # Существующих родителей корня не меняем.
        if directory == self.root or directory.is_relative_to(self.root) or not directory.exists():
            paths._ensure_private_dir(directory)

    def staging_dir(self, model_id: str, revision: str) -> Path:
        """Создаёт приватный каталог незавершённой загрузки."""
        directory = self._revision_path(model_id, revision, ".partial")
        with _store_errors():
            self._private_dir(self.root)
            self._private_dir(directory.parent)
            self._private_dir(directory)
        return directory

    def _installed(self, model_id: str, revision: str) -> Path:
        directory = self._revision_path(model_id, revision)
        if not directory.is_dir():
            raise StoreError("not-found")
        return directory

    def _staged_metadata(self, directory: Path) -> tuple[str, str, int]:
        """Берёт метаданные загрузчика; без них размер считается по полезным файлам."""
        try:
            return _metadata(_read_json(self._checked(directory / "state.json")))
        except (OSError, ValueError, RecursionError):
            size = 0
            for parent, dirs, files in os.walk(directory, followlinks=False):
                for name in dirs:
                    self._checked(Path(parent) / name)
                for name in files:
                    file = self._checked(Path(parent) / name)
                    if file != directory / "state.json":
                        size += file.stat().st_size
            return "", "", size

    def commit(self, model_id: str, revision: str) -> Path:
        """Атомарно переносит staging и записывает состояние установленной ревизии."""
        staging = self._revision_path(model_id, revision, ".partial")
        directory = self._revision_path(model_id, revision)
        state_path = self._state_path(model_id, revision)
        with _store_errors():
            if not staging.is_dir():
                raise StoreError("not-found")
            if directory.exists() and not directory.is_dir():
                raise StoreError("broken-store", "Вместо каталога ревизии обнаружен файл.")
            self._private_dir(self.root)
            self._private_dir(staging.parent)
            self._private_dir(staging)
            layout, variant, size = self._staged_metadata(staging)
            self._checked(staging / "state.json").unlink(missing_ok=True)
            _fsync_dir(staging)
            backup = self._checked(directory.with_name(f"{revision}.old-{uuid4().hex}"))
            had_previous = directory.exists()
            if had_previous:
                os.replace(directory, backup)
            try:
                os.replace(staging, directory)
            except OSError:
                if had_previous:
                    os.replace(backup, directory)
                raise
            _fsync_dir(directory.parent)
            record = ModelRecord(model_id, revision, directory, layout, variant, size)
            _write_json(state_path, _state_data(record))
            if had_previous:
                shutil.rmtree(backup)
                _fsync_dir(directory.parent)
        return directory

    def _quarantine_current(self) -> None:
        path = self.root / "current.json"
        backup = path.with_name(f"{path.name}.bak-{int(time.time())}")
        try:
            # Несколько повреждений за секунду не должны затирать предыдущую копию.
            while backup.exists() or backup.is_symlink():
                stamp = int(backup.name.rsplit("-", 1)[1]) + 1
                backup = path.with_name(f"{path.name}.bak-{stamp}")
            os.replace(path, backup)
            _fsync_dir(path.parent)
        except OSError as exc:
            log.warning("Не удалось переименовать испорченный %s: %s", path, exc)
        else:
            log.warning("%s испорчен, переименован в %s.", path, backup)

    def _current_ids(self) -> tuple[str, str] | None:
        try:
            data = _read_json(self._checked(self.root / "current.json"))
            model_id, revision = data.get("id"), data.get("revision")
            if not isinstance(model_id, str) or not isinstance(revision, str):
                raise ValueError("Не указаны модель и ревизия.")
            self._revision_path(model_id, revision)
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.warning("Не удалось прочитать current.json: %s", exc)
            return None
        except (ValueError, StoreError, RecursionError):
            self._quarantine_current()
            return None
        return model_id, revision

    def _record(
        self, model_id: str, revision: str, directory: Path, *, migrate_legacy: bool = True
    ) -> ModelRecord:
        try:
            state_path = self._state_path(model_id, revision)
            legacy = directory / "state.json"
            try:
                if migrate_legacy and not legacy.is_symlink() and legacy.is_file():
                    if state_path.exists():
                        legacy.unlink()
                    else:
                        try:
                            legacy_data = _read_json(legacy)
                        except (ValueError, RecursionError):
                            legacy_data = {}
                        if legacy_data.get("state") == "broken":
                            legacy_data["recheck"] = True
                            # Сначала закрепляем отметку, затем удаляем признак старой раскладки.
                            _write_json(state_path, legacy_data)
                            legacy.unlink()
                        else:
                            legacy.chmod(0o600)
                            os.replace(legacy, state_path)
                    _fsync_dir(directory)
                    _fsync_dir(state_path.parent)
                    log.info("Состояние ревизии перенесено из %s в %s.", legacy, state_path)
            except OSError as exc:
                log.warning("Не удалось перенести состояние ревизии из %s: %s", legacy, exc)
                raise
            if not migrate_legacy and not state_path.exists() and not legacy.is_symlink():
                state_path = legacy
            data = _read_json(state_path)
            layout, variant, size = _metadata(data)
            state, reason = data.get("state"), data.get("reason", "")
            recheck = data.get("recheck", False)
            if (
                state not in ("ok", "broken")
                or not isinstance(reason, str)
                or not isinstance(recheck, bool)
            ):
                raise ValueError("Неверно указано состояние модели.")
            return ModelRecord(
                model_id,
                revision,
                directory,
                layout,
                variant,
                size,
                state,
                reason,
                recheck,
            )
        except FileNotFoundError:
            reason = "Отсутствует файл состояния модели (state.json)."
        except (OSError, ValueError, StoreError, RecursionError):
            reason = "Файл состояния модели (state.json) повреждён или недоступен."
        return ModelRecord(
            model_id, revision, directory, "", "", 0, "broken", reason, metadata_ok=False
        )

    def current(self) -> ModelRecord | None:
        """Возвращает текущую исправную ревизию; порча указателя не мешает старту."""
        ids = self._current_ids()
        if ids is None:
            return None
        try:
            directory = self._installed(*ids)
            record = self._record(*ids, directory)
        except (OSError, StoreError):
            return None
        return (
            record
            if record.state == "ok" and record.recheck is not True and record.metadata_ok
            else None
        )

    def _directories(self) -> Iterator[tuple[str, Path]]:
        if not self.root.exists():
            return
        for model in sorted(self.root.iterdir()):
            if ID_RE.fullmatch(model.name) is None or model.is_symlink() or not model.is_dir():
                continue
            self._checked(model)
            for directory in sorted(model.iterdir()):
                # Служебные суффиксы не входят в лимит длины самой ревизии.
                revision = directory.name.removesuffix(".partial").split(".old-", 1)[0]
                if (
                    ID_RE.fullmatch(revision) is None
                    or directory.is_symlink()
                    or not directory.is_dir()
                ):
                    continue
                yield model.name, self._checked(directory)

    def records(self) -> tuple[ModelRecord, ...]:
        """Перечисляет установленные ревизии, включая ревизии с испорченным состоянием."""
        result: list[ModelRecord] = []
        with _store_errors():
            for model_id, directory in self._directories():
                revision = directory.name
                if revision.endswith(".partial") or ".old-" in revision:
                    continue
                self._revision_path(model_id, revision)
                result.append(self._record(model_id, revision, directory))
        return tuple(result)

    def set_current(self, model_id: str, revision: str) -> None:
        """Атомарно записывает указатель на существующую ревизию."""
        with _store_errors():
            self._installed(model_id, revision)
            self._private_dir(self.root)
            _write_json(
                self._checked(self.root / "current.json"), {"id": model_id, "revision": revision}
            )

    def mark_broken(self, model_id: str, revision: str, reason: str) -> None:
        """Помечает ревизию сломанной, сохраняя метаданные и указатель current.json."""
        with _store_errors():
            directory = self._installed(model_id, revision)
            record = replace(
                self._record(model_id, revision, directory),
                state="broken",
                reason=reason,
                recheck=False,
            )
            _write_json(self._state_path(model_id, revision), _state_data(record))

    def mark_ok(self, model_id: str, revision: str) -> None:
        """Снимает пометку брака и необходимость перепроверки.

        Вызывать только после сверки контрольных сумм и успешного пробного распознавания.
        """
        with _store_errors():
            directory = self._installed(model_id, revision)
            # Отказ не должен менять даже старую раскладку файла состояния.
            record = self._record(model_id, revision, directory, migrate_legacy=False)
            if not record.metadata_ok or not record.layout or not record.variant:
                raise StoreError("invalid-metadata")
            record = replace(record, state="ok", reason="", recheck=False)
            _write_json(self._state_path(model_id, revision), _state_data(record))

    def disk_ok(self, size_bytes: int) -> bool:
        """Проверяет свободное место с запасом 20 %, без округления float."""
        with _store_errors():
            directory = self.root
            while not directory.exists():
                directory = directory.parent
            return shutil.disk_usage(directory).free * 5 >= size_bytes * 6

    def ram_ok(self, min_ram_mb: int) -> bool:
        """Проверяет MemTotal; неизвестный объём памяти не блокирует пользователя."""
        try:
            for line in MEMINFO_PATH.read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if fields and fields[0] == "MemTotal:":
                    if len(fields) != 3 or fields[2] != "kB" or int(fields[1]) < 0:
                        raise ValueError("Неверный формат MemTotal.")
                    return int(fields[1]) >= min_ram_mb * 1024
        except (OSError, ValueError) as exc:
            log.warning("Не удалось определить объём памяти по %s: %s", MEMINFO_PATH, exc)
            return True
        log.warning("В %s отсутствует MemTotal; проверка памяти пропущена.", MEMINFO_PATH)
        return True

    def recover_incomplete(self) -> tuple[str, ...]:
        """Удаляет staging прошлых запусков и старые каталоги, продолжая после ошибок."""
        removed: list[str] = []
        try:
            for model_id, directory in self._directories():
                partial = directory.name.endswith(".partial")
                if not partial and ".old-" not in directory.name:
                    continue
                try:
                    shutil.rmtree(directory)
                except OSError as exc:
                    log.warning("Не удалось удалить незавершённую ревизию %s: %s", directory, exc)
                    continue
                if partial:
                    removed.append(f"{model_id}/{directory.name.removesuffix('.partial')}")
                try:
                    _fsync_dir(directory.parent)
                except OSError as exc:
                    log.warning("Не удалось закрепить удаление %s: %s", directory, exc)
        except (OSError, StoreError) as exc:
            log.warning("Не удалось осмотреть хранилище при восстановлении: %s", exc)
        return tuple(removed)

    def remove(self, model_id: str, revision: str) -> None:
        """Удаляет установленную ревизию и указатель, если он ссылается на неё."""
        with _store_errors():
            directory = self._installed(model_id, revision)
            state_path = self._state_path(model_id, revision)
            was_current = self._current_ids() == (model_id, revision)
            shutil.rmtree(directory)
            state_path.unlink(missing_ok=True)
            _fsync_dir(directory.parent)
            if was_current:
                self._checked(self.root / "current.json").unlink(missing_ok=True)
                _fsync_dir(self.root)
