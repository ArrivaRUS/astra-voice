"""Единый проверенный каталог и watermark для всех потребителей моделей."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from astra_voice.core import paths
from astra_voice.models import catalog_state
from astra_voice.models.catalog import CATALOG_MAX_BYTES, Catalog, CatalogError, load_builtin
from astra_voice.models.store import ModelStore, StoreError
from astra_voice.net.update_cache import read_private, write_private
from astra_voice.security.verify import Verifier

log = logging.getLogger(__name__)
SNAPSHOT_FILE_NAME = "catalog-snapshot.json"
SNAPSHOT_MAX_BYTES = 2 * 1024 * 1024
SIGNATURE_MAX_BYTES = 64 * 1024
# Общий для загрузчика и публикации source: сравнение и запись watermark
# не могут перемежаться внутри процесса с принятием другого каталога.
CATALOG_LOCK = threading.RLock()


class ModelActivator(Protocol):
    def __call__(
        self, model_id: str, revision: str, *, previous_revision: str | None = None
    ) -> None: ...


def check_revoked(model_id: str, revision: str, catalog: Catalog | None) -> None:
    """Объединяет отзывы памяти, отметки и подписанного снимка; отказ закрытый.

    Снимок публикуется раньше отметки: даже при отказе записи отметки его
    отзывы уже обязательны. Отсутствие файлов допустимо для автономных
    установщиков; существующий непроверяемый источник не означает пустой список.
    Вызывающий активацию удерживает тот же RLock до записи current.json.
    """
    with CATALOG_LOCK:
        revoked = catalog is not None and catalog.is_revoked(model_id, revision)
        marker_path = catalog_state.state_path(paths.state_dir())
        try:
            marker_path.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise StoreError("catalog-unavailable", "Состояние каталога недоступно.") from None
        else:
            try:
                marker = catalog_state.read_state(marker_path)
            except (OSError, ValueError, RecursionError):
                marker = None
            if (
                marker is None
                or re.fullmatch(r"[0-9a-f]{64}", marker.sha256) is None
                or marker.revoked is None
            ):
                raise StoreError("catalog-unavailable", "Состояние каталога повреждено.")
            revoked |= (model_id, revision) in marker.revoked
        directory = paths.state_dir() / "model-catalog-updates"
        try:
            (directory / SNAPSHOT_FILE_NAME).lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise StoreError("catalog-unavailable", "Снимок каталога недоступен.") from None
        else:
            try:
                raw, signature = read_snapshot(directory)
                root = paths.data_dir_static()
                snapshot = verify_catalog(
                    raw,
                    signature,
                    Verifier("catalog", keyring=root / "keys" / "release.gpg"),
                    schema_root=root,
                    require_schema=True,
                )
            except (OSError, ValueError, TypeError, RecursionError, CatalogError):
                raise StoreError("catalog-unavailable", "Снимок каталога не проверен.") from None
            revoked |= snapshot.is_revoked(model_id, revision)
        if revoked:
            raise StoreError("revoked", "Эта версия модели отозвана. Выберите другую версию.")


def activate_revision(
    store: ModelStore,
    catalog: Catalog | Callable[[], Catalog] | None,
    model_id: str,
    revision: str,
    *,
    previous_revision: str | None = None,
) -> None:
    """Совместимый барьер для службы и автономных установщиков/обновляторов."""
    with CATALOG_LOCK:
        try:
            current_catalog = catalog() if callable(catalog) else catalog
        except CatalogError:
            raise StoreError("catalog-unavailable", "Каталог моделей недоступен.") from None
        check_revoked(model_id, revision, current_catalog)
        if previous_revision is None:
            store.set_current(model_id, revision)
        else:
            store.set_current(model_id, revision, previous_revision=previous_revision)


def state_of(catalog: Catalog, raw: bytes) -> catalog_state.CatalogState:
    """Связывает проверенный каталог с точными подписанными байтами."""
    return catalog_state.CatalogState(
        trust_epoch=catalog.trust_epoch,
        serial=catalog.serial,
        sha256=hashlib.sha256(raw).hexdigest(),
        revoked=tuple((entry.model_id, entry.revision) for entry in catalog.revoked),
        # load_builtin уже связал этот SHA256 с точными байтами схемы клиента.
        schema_id=json.loads(raw)["schema_sha256"][:16],
    )


def verify_catalog(
    raw: bytes,
    signature: bytes,
    verifier: Verifier,
    *,
    schema_root: Path,
    require_schema: bool | None = None,
) -> Catalog:
    """Проверяет пару тем же загрузчиком и текущей схемой, не меняя state.

    Приватная временная копия связывает проверку подписи и строгий разбор
    с одними байтами; исходный snapshot может атомарно заменяться параллельно.
    """
    if len(raw) > CATALOG_MAX_BYTES or len(signature) > SIGNATURE_MAX_BYTES:
        raise CatalogError("too-large", "Каталог или подпись слишком велики.")
    try:
        with tempfile.TemporaryDirectory(prefix="catalog-check-") as name:
            root = Path(name).resolve()
            write_private(root / "catalog.json", raw)
            write_private(root / "catalog.json.sig", signature)
            try:
                schema = read_private(schema_root / "catalog.schema.json", CATALOG_MAX_BYTES)
            except (OSError, ValueError):
                # Порядок отказов загрузчика: подпись, JSON, затем схема.
                schema = b""
            write_private(root / "catalog.schema.json", schema)
            return load_builtin(verifier, root=root, require_schema=require_schema)
    except OSError:
        raise CatalogError("io", "Не удалось проверить сохранённый каталог.") from None


def read_snapshot(snapshot_dir: Path) -> tuple[bytes, bytes]:
    """Читает одну атомарную пару raw/signature с ограничением размера."""
    document = json.loads(read_private(snapshot_dir / SNAPSHOT_FILE_NAME, SNAPSHOT_MAX_BYTES))
    if (
        not isinstance(document, dict)
        or set(document) != {"raw", "signature"}
        or not isinstance(document["raw"], str)
        or not isinstance(document["signature"], str)
    ):
        raise ValueError("Неверный формат снимка каталога.")
    raw = base64.b64decode(document["raw"], validate=True)
    signature = base64.b64decode(document["signature"], validate=True)
    if len(raw) > CATALOG_MAX_BYTES or len(signature) > SIGNATURE_MAX_BYTES:
        raise CatalogError("too-large", "Каталог или подпись слишком велики.")
    return raw, signature


def load_effective(
    verifier: Verifier,
    *,
    builtin_root: Path | None = None,
    snapshot_dir: Path | None = None,
    state_path: Path | None = None,
    require_schema: bool | None = None,
) -> Catalog:
    """Загружает действующий каталог, сериализуя принятие с remote source."""
    with CATALOG_LOCK:
        return _load_effective(
            verifier,
            builtin_root=builtin_root,
            snapshot_dir=snapshot_dir,
            state_path=state_path,
            require_schema=require_schema,
        )


def _load_effective(
    verifier: Verifier,
    *,
    builtin_root: Path | None,
    snapshot_dir: Path | None,
    state_path: Path | None,
    require_schema: bool | None,
) -> Catalog:
    """Выбирает старший проверенный каталог и применяет глобальный анти-откат.

    Кандидаты проверяются без изменения state: старый встроенный каталог
    не должен мешать восстановлению нового удалённого после перезапуска.
    Повреждённый snapshot игнорируется, но watermark при этом не сбрасывается.
    Равная пара с разными байтами сохраняет уже принятый SHA; без отметки
    выбирается встроенный. apply_state запрещает конфликт с третьим SHA.
    """
    root = paths.data_dir_static() if builtin_root is None else builtin_root
    directory = (
        paths.state_dir() / "model-catalog-updates" if snapshot_dir is None else snapshot_dir
    )
    state = catalog_state.state_path(paths.state_dir()) if state_path is None else state_path
    try:
        state.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise CatalogError("state-unavailable", "Состояние каталога недоступно.") from None
    else:
        try:
            marker = catalog_state.read_state(state)
        except (OSError, ValueError, RecursionError):
            marker = None
        if marker is None or re.fullmatch(r"[0-9a-f]{64}", marker.sha256) is None:
            # Повреждение отметки не разрешает локальному каталогу стереть
            # историю отзывов. Восстановление допускается только через refresh.
            raise CatalogError("state-invalid", "Состояние каталога повреждено.")
    try:
        raw = read_private(root / "catalog.json", CATALOG_MAX_BYTES)
        chosen = load_builtin(verifier, root=root, require_schema=require_schema)
        if read_private(root / "catalog.json", CATALOG_MAX_BYTES) != raw:
            raise CatalogError("bad-signature", "Каталог изменился во время проверки подписи.")
    except (OSError, ValueError):
        raise CatalogError("io", "Не удалось прочитать встроенный каталог.") from None
    candidate = state_of(chosen, raw)
    try:
        remote_raw, signature = read_snapshot(directory)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, RecursionError, CatalogError):
        log.warning("Сохранённый снимок каталога повреждён или недоступен и пропущен.")
    else:
        try:
            remote = verify_catalog(
                remote_raw, signature, verifier, schema_root=root, require_schema=True
            )
        except CatalogError:
            log.warning("Сохранённый снимок каталога не прошёл проверку и пропущен.")
        else:
            remote_state = state_of(remote, remote_raw)
            builtin_pair = (candidate.trust_epoch, candidate.serial)
            remote_pair = (remote_state.trust_epoch, remote_state.serial)
            if remote_pair > builtin_pair:
                chosen, candidate = remote, remote_state
            elif remote_pair == builtin_pair and remote_state.sha256 != candidate.sha256:
                marker = catalog_state.read_state(state)
                if (
                    marker is not None
                    and (marker.trust_epoch, marker.serial) == remote_pair
                    and marker.sha256 == remote_state.sha256
                ):
                    chosen, candidate = remote, remote_state
                log.warning(
                    "Снимок каталога имеет ту же пару эпохи и номера, но другой SHA256; "
                    "выбор ограничен глобальной отметкой принятого каталога."
                )
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        catalog_state.apply_state(state, candidate)
    except ValueError as error:
        raise CatalogError("stale-catalog", str(error)) from None
    except OSError:
        raise CatalogError("io", "Не удалось сохранить принятый каталог.") from None
    return chosen
