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
from astra_voice.security.verify import Verifier, VerifyResult

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


# Последний проверенный каталог и один отказ: ограниченная память, ключ связывает
# байты каталога, подписи и схему. Отказ не стирает уже проверенные отзывы.
_snapshot_cache: tuple[tuple[str, str, Path, str], Catalog | None] | None = None
_snapshot_rejection_cache: tuple[str, str, Path, str] | None = None


def cached_pending_snapshot(marker: catalog_state.CatalogState | None) -> Catalog | None:
    """Возвращает только проверенный кэш новее отметки, не читая снимок."""
    if _snapshot_cache is None or _snapshot_cache[0][2] != paths.data_dir_static():
        return None
    cached = _snapshot_cache[1]
    if cached is not None and (
        marker is None or (cached.trust_epoch, cached.serial) > (marker.trust_epoch, marker.serial)
    ):
        return cached
    return None


def _snapshot_key(raw: bytes, signature: bytes) -> tuple[str, str, Path, str]:
    root = paths.data_dir_static()
    schema_sha = hashlib.sha256(
        read_private(root / "catalog.schema.json", CATALOG_MAX_BYTES)
    ).hexdigest()
    return hashlib.sha256(raw).hexdigest(), hashlib.sha256(signature).hexdigest(), root, schema_sha


def _remember_verified_snapshot(key: tuple[str, str, Path, str], catalog: Catalog) -> None:
    global _snapshot_cache
    previous = None if _snapshot_cache is None else _snapshot_cache[1]
    if previous is None or (catalog.trust_epoch, catalog.serial) > (
        previous.trust_epoch,
        previous.serial,
    ):
        _snapshot_cache = (key, catalog)


def remember_verified_snapshot(key: tuple[str, str, Path, str], catalog: Catalog) -> None:
    """Запоминает проверенный каталог без I/O, сохраняя старший положительный кэш.

    Вызывающий заранее вычисляет ключ проверенной пары до записи снимка.
    При сбое отката это сохраняет известные отзывы независимо от состояния диска.
    """
    with CATALOG_LOCK:
        _remember_verified_snapshot(key, catalog)


def read_revocation_marker() -> tuple[catalog_state.CatalogState | None, bool]:
    """Читает отметку отзывов: отсутствие допустимо, повреждение — неизвестность."""
    try:
        marker_path = catalog_state.state_path(paths.state_dir())
        try:
            marker_path.lstat()
        except FileNotFoundError:
            return None, False
        marker = catalog_state.read_state(marker_path)
    except (OSError, paths.PathError, ValueError, RecursionError):
        return None, True
    if (
        marker is None
        or re.fullmatch(r"[0-9a-f]{64}", marker.sha256) is None
        or marker.revoked is None
    ):
        return None, True
    return marker, False


class _SnapshotVerifier(Verifier):
    """Сохраняет временные отказы, которые load_builtin обобщает до bad-signature."""

    def __init__(self, verifier: Verifier) -> None:
        self._verifier = verifier
        self.purpose = verifier.purpose

    def verify_detached(
        self, data: Path, sig: Path, *, cancel: Callable[[], bool] | None = None
    ) -> VerifyResult:
        try:
            result = self._verifier.verify_detached(data, sig, cancel=cancel)
        except OSError:
            raise CatalogError("io", "Не удалось проверить сохранённый каталог.") from None
        # У таймаута и отсутствующего gpgv в Verifier пока нет отдельного кода.
        if not result.ok and (
            result.code in {"cancelled", "output-limit"}
            or result.reason == "gpgv не ответил за отведённое время"
            or result.reason.startswith("gpgv не найден:")
            or "файл не найден или не обычный файл:" in result.reason
        ):
            raise CatalogError("io", "Не удалось проверить сохранённый каталог.")
        return result


def _pending_snapshot(
    marker: catalog_state.CatalogState | None,
) -> tuple[Catalog | None, bool]:
    """Проверяет только снимок, который нельзя считать старее JSON-отметки.

    Возвращает (проверенный снимок или None, признак «непроверяемый»).
    Снимок с SHA отметки или с парой не новее неё пропускается без gpgv.
    При отметке снимок с нечитаемой парой тоже пропускается с WARNING:
    принятые отзывы уже в отметке. Без отметки или для снимка новее неё
    неудача проверки означает «отзывы неизвестны»: активация отказывает.
    """
    global _snapshot_cache, _snapshot_rejection_cache
    directory = paths.state_dir() / "model-catalog-updates"
    try:
        raw, signature = read_snapshot(directory)
    except FileNotFoundError:
        return cached_pending_snapshot(marker), False
    except (OSError, ValueError, TypeError, RecursionError, CatalogError):
        return _unreadable_pair(marker)
    digest = hashlib.sha256(raw).hexdigest()
    if marker is not None and digest == marker.sha256:
        return None, False
    try:
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("Неверный формат каталога.")
        epoch, serial = document.get("trust_epoch"), document.get("serial")
        if type(epoch) is not int or type(serial) is not int or epoch < 1 or serial < 1:
            raise ValueError("Неверная пара эпохи и номера каталога.")
    except (ValueError, TypeError, RecursionError):
        return _unreadable_pair(marker)
    try:
        # Непроверенные поля используются только для пропуска кандидата:
        # отозвать версию может лишь результат полной проверки ниже.
        if marker is not None and (epoch, serial) <= (marker.trust_epoch, marker.serial):
            return None, False
        key = _snapshot_key(raw, signature)
        root = key[2]
        if _snapshot_rejection_cache == key:
            raise CatalogError("bad-signature", "Снимок каталога уже не прошёл проверку.")
        if _snapshot_cache is not None and _snapshot_cache[0] == key:
            cached = _snapshot_cache[1]
            if cached is None:
                raise CatalogError("bad-signature", "Снимок каталога уже не прошёл проверку.")
            return cached, False
        try:
            snapshot = verify_catalog(
                raw,
                signature,
                _SnapshotVerifier(Verifier("catalog", keyring=root / "keys" / "release.gpg")),
                schema_root=root,
                require_schema=True,
            )
        except CatalogError as error:
            if error.code in {"bad-signature", "bad-schema", "bad-path", "too-large"}:
                _snapshot_rejection_cache = key
                if _snapshot_cache is None or _snapshot_cache[1] is None:
                    _snapshot_cache = (key, None)
            raise
        _remember_verified_snapshot(key, snapshot)
        _snapshot_rejection_cache = None
        return snapshot, False
    except (OSError, ValueError, TypeError, RecursionError, CatalogError):
        log.warning("Новый снимок каталога не прошёл проверку: отзывы неизвестны.")
        return None, True


def _unreadable_pair(
    marker: catalog_state.CatalogState | None,
) -> tuple[Catalog | None, bool]:
    """Пара снимка не читается: при отметке пропуск, без неё отзывы неизвестны."""
    cached = cached_pending_snapshot(marker)
    if marker is not None:
        log.warning("Сохранённый снимок каталога повреждён и пропущен: действует отметка.")
        return cached, False
    log.warning("Сохранённый снимок каталога повреждён, отметки нет: отзывы неизвестны.")
    return cached, True


def check_revoked(
    model_id: str,
    revision: str,
    catalog: Catalog | None,
    *,
    include_snapshot: bool = False,
) -> None:
    """Объединяет отзывы памяти и отметки; активации также учитывают новый снимок.

    По умолчанию только память и JSON, без gpgv, для GUI и предзагрузки:
    снимок не читается, битая отметка даёт catalog-unavailable.
    Это не барьер активации: activate_revision явно включает новый snapshot.
    Известный отзыв сильнее недоступности другого источника. Непроверяемый
    снимок без отметки или новее неё — catalog-unavailable; старый и (при
    отметке) снимок с нечитаемой парой пропускаются.
    Вызывающий активацию удерживает тот же RLock до записи current.json.
    """
    with CATALOG_LOCK:
        if catalog is not None and catalog.is_revoked(model_id, revision):
            raise StoreError("revoked", "Эта версия модели отозвана. Выберите другую версию.")
        marker, marker_error = read_revocation_marker()
        if marker is not None and marker.revoked is None:
            marker, marker_error = None, True
        if marker is not None and marker.revoked is not None:
            if (model_id, revision) in marker.revoked:
                raise StoreError("revoked", "Эта версия модели отозвана. Выберите другую версию.")
        cached = cached_pending_snapshot(marker)
        if cached is not None and cached.is_revoked(model_id, revision):
            raise StoreError("revoked", "Эта версия модели отозвана. Выберите другую версию.")
        snapshot_error = False
        if include_snapshot:
            snapshot, snapshot_error = _pending_snapshot(marker)
            if snapshot is not None and snapshot.is_revoked(model_id, revision):
                raise StoreError("revoked", "Эта версия модели отозвана. Выберите другую версию.")
        if marker_error:
            raise StoreError("catalog-unavailable", "Состояние каталога повреждено или недоступно.")
        if snapshot_error:
            raise StoreError("catalog-unavailable", "Новый снимок каталога не прошёл проверку.")


def activate_revision(
    store: ModelStore,
    catalog: Catalog | Callable[[], Catalog] | None,
    model_id: str,
    revision: str,
    *,
    previous_revision: str | None = None,
    catalog_peek: Callable[[], Catalog | None] | None = None,
) -> None:
    """Проверяет отзывы и записывает current под общим барьером публикации.

    Provider может выполнять I/O или брать чужие блокировки: всегда вызывается
    вне CATALOG_LOCK. Для изменяемого источника catalog_peek читает лишь текущую
    ссылку из памяти (без I/O/других locks); замена ссылки выполняется под
    CATALOG_LOCK. При смене объекта provider повторяется вне блокировки.
    Автономным установщикам без peek достаточно глобальных marker/snapshot.
    """
    while True:
        try:
            current_catalog = catalog() if callable(catalog) else catalog
        except CatalogError:
            check_revoked(model_id, revision, None, include_snapshot=True)
            raise StoreError("catalog-unavailable", "Каталог моделей недоступен.") from None
        with CATALOG_LOCK:
            if catalog_peek is not None and catalog_peek() is not current_catalog:
                continue
            check_revoked(model_id, revision, current_catalog, include_snapshot=True)
            if previous_revision is None:
                store.set_current(model_id, revision)
            else:
                store.set_current(model_id, revision, previous_revision=previous_revision)
            return


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
