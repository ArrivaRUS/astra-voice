"""Установка AppImage в домашнюю папку (``arch/appimage.md`` §1, §12).

``.AppImage`` — носитель и установщик: первый запуск копирует бандл в
``<data_dir>/app/<KEY>/`` и переключает симлинк ``app/current``. Один код для
самоустановки (0.2), удаления и будущего самообновления (v1.0).

Раскладка ``app/``: каталоги ``<KEY>`` (не больше двух — ``current`` и
``previous``, решение заказчика В1), симлинки ``current``/``previous``,
``.install.lock``. Чистка трогает только ``app/<KEY>`` и свои ``app/.tmp-*``;
модели, журналы и настройки лежат вне ``app/`` и не затрагиваются никогда.

Без Qt: модуль вызывается из ``bootstrap.py selfinstall`` до запуска GUI.
"""

from __future__ import annotations

import ctypes
import fcntl
import logging
import math
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from astra_voice.core import childenv, paths

log = logging.getLogger(__name__)

# Грамматика KEY — одна, в paths (метка MN-1 там же).
KEY_RE = paths.APPIMAGE_KEY_RE

TMP_PREFIX = ".tmp-"
LOCK_NAME = ".install.lock"
RUNNING_KEY_NAME = "running-key"
MIB = 1024 * 1024
MIN_FREE_BYTES = 350 * MIB
LOCK_TIMEOUT_S = 180.0
LOCK_POLL_S = 0.1
SMOKE_TIMEOUT_S = 60.0
BUILD_INFO_LIMIT = 4096
RUNNING_KEY_LIMIT = 256
# То же, что проверяет installed_ok() в AppRun.
REQUIRED_FILES = (
    paths.APPIMAGE_LAUNCHER,
    "opt/python3.11/bin/python3.11",
    "usr/lib/astra-voice/bootstrap.py",
    paths.BUILD_MARKER,
)
EXECUTABLE_FILES = (paths.APPIMAGE_LAUNCHER, "opt/python3.11/bin/python3.11")
# Служебные флаги не устанавливают программу (спайк §7.10). Тот же список — в AppRun.
SERVICE_FLAGS = frozenset({"--version", "--help", "-h", "--stats", "--selfinstall-status"})

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
# Коды отказов T1 (arch/appimage.md §1); задействуются 01.10.
EXIT_ROOT = 3
EXIT_UNSAFE_SOURCE = 4
# Установка не выполнялась (служебный флаг или ASTRA_VOICE_PORTABLE=1).
EXIT_SKIPPED = 10

USAGE = "usage: bootstrap.py selfinstall <каталог-бандла> [аргументы программы]\n"


class UserInstallError(RuntimeError):
    """Установка не выполнена; текст — для пользователя."""

    exit_code = EXIT_FAILED


class NotEnoughSpaceError(UserInstallError):
    pass


class LockTimeoutError(UserInstallError):
    pass


class SmokeError(UserInstallError):
    pass


class RootRefusedError(UserInstallError):
    exit_code = EXIT_ROOT


class UnsafeSourceError(UserInstallError):
    exit_code = EXIT_UNSAFE_SOURCE


@dataclass(frozen=True)
class BuildInfo:
    """Содержимое ``.astra-voice-build`` бандла."""

    version: str
    build_id: str
    raw: bytes

    @property
    def key(self) -> str:
        return f"{self.version}-{self.build_id}"


@dataclass(frozen=True)
class InstallResult:
    key: str
    target: Path
    copied: bool
    previous: str | None


Smoke = Callable[[Path, BuildInfo], None]


def is_key(name: str) -> bool:
    """Имя каталога копии ``<версия>-<build_id>`` (маска чистки)."""
    return paths.is_appimage_key(name)


def tilde(text: object) -> str:
    """Домашний каталог в сообщениях — как ``~`` (У69)."""
    value = str(text)
    home = str(Path.home())
    if home not in ("", "/"):
        value = value.replace(home + "/", "~/")
    return value


def refuse_root() -> None:
    """Отказ работы от root — точка расширения; до 01.10 пропускает."""
    # T1-01.10: MJ-3 — при os.geteuid() == 0 RootRefusedError: «Версия AppImage не
    # запускается от имени root. Управляйте ею под учётной записью пользователя»,
    # код 3, ни одного файла не меняем (T-179).
    return None


def check_source(src: Path) -> None:
    """Проверка каталога-источника до копирования — точка расширения; до 01.10 пропускает.

    Вызывается до создания ``app/``, чтобы отказ не оставлял следов (T-175).
    """
    # T1-01.10: BL-1 — режим В (appimage_extracted_*): HERE и родители до $TMPDIR/$HOME
    # по os.lstat — не ссылка, каталог, владелец текущий uid, нет записи группе/миру
    # (mode & 0o022); режим Б — корень монтирования fuse* с ro,nosuid,nodev по
    # /proc/self/mountinfo. Иначе UnsafeSourceError («Небезопасная временная папка…
    # TMPDIR="$HOME/.cache" …»), код 4, app/ не создаётся (T-175).
    return None


def should_install(args: Sequence[str], env: Mapping[str, str] | None = None) -> bool:
    """Нужна ли самоустановка при таких аргументах программы и окружении."""
    source = os.environ if env is None else env
    if source.get(paths.PORTABLE_ENV, "").strip() == "1":
        return False
    return not any(arg in SERVICE_FLAGS for arg in args)


def read_build_info(root: Path) -> BuildInfo:
    """Читает ``.astra-voice-build`` без следования по ссылке и с лимитом размера."""
    path = root / paths.BUILD_MARKER
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise UserInstallError("Не найдены сведения о сборке Astra Voice.") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise UserInstallError("Повреждены сведения о сборке Astra Voice.")
        raw = os.read(fd, BUILD_INFO_LIMIT + 1)
    finally:
        os.close(fd)
    if len(raw) > BUILD_INFO_LIMIT:
        raise UserInstallError("Повреждены сведения о сборке Astra Voice.")
    fields: dict[str, str] = {}
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise UserInstallError("Повреждены сведения о сборке Astra Voice.") from exc
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and name in ("VERSION", "BUILD_ID"):
            fields.setdefault(name, value)
    info = BuildInfo(fields.get("VERSION", ""), fields.get("BUILD_ID", ""), raw)
    if not is_key(info.key):
        raise UserInstallError("Повреждены сведения о сборке Astra Voice.")
    return info


def _is_real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def installed_ok(target: Path, info: BuildInfo) -> bool:
    """Целая установленная копия этой сборки (аналог ``installed_ok`` в AppRun)."""
    if not _is_real_dir(target):
        return False
    try:
        if not stat.S_ISREG(os.lstat(target / paths.INSTALLED_MARKER).st_mode):
            return False
        for name in REQUIRED_FILES:
            if not (target / name).is_file():
                return False
        for name in EXECUTABLE_FILES:
            if not os.access(target / name, os.X_OK):
                return False
        return (target / paths.BUILD_MARKER).read_bytes() == info.raw
    except OSError:
        return False


def free_bytes(path: Path) -> int:
    """Свободное место для пользователя на томе ``path`` (или ближайшего предка)."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    info = os.statvfs(probe)
    return info.f_bavail * info.f_frsize


def check_free_space(path: Path, required: int = MIN_FREE_BYTES) -> None:
    free = free_bytes(path)
    if free < required:
        need = max(1, math.ceil((required - free) / MIB))
        raise NotEnoughSpaceError(f"Недостаточно места в домашней папке: нужно ещё {need} МБ.")


def running_key_path() -> Path:
    return paths.runtime_dir() / RUNNING_KEY_NAME


def read_running_key() -> str | None:
    """KEY работающей копии (пишет программа при старте) — только как строка."""
    # T1-01.10: MN-8 — чтение с O_NOFOLLOW и лимитом, значение принимается только по
    # KEY_RE; сравнивается как строка с именами app/<KEY>, путь из содержимого не
    # строится никогда (unit в наборе userinstall).
    try:
        with running_key_path().open("rb") as file:
            data = file.read(RUNNING_KEY_LIMIT)
    except (OSError, paths.PathError):
        return None
    value = data.decode("ascii", "replace").strip()
    return value or None


def write_running_key(key: str) -> None:
    """Отмечает работающую копию, чтобы чистка её не удалила (Р5)."""
    if not is_key(key):
        raise ValueError(f"не KEY копии: {key!r}")
    path = running_key_path()
    temporary = path.with_name(f".{RUNNING_KEY_NAME}.{secrets.token_hex(6)}")
    temporary.write_text(key + "\n", encoding="ascii")
    os.replace(temporary, path)


def _sync_filesystem(path: Path) -> None:
    """Сбрасывает на диск данные и метаданные тома, где лежит ``path`` (``syncfs(2)``).

    Копия — тысячи файлов и каталогов; ``fsync`` каждого — тысячи коммитов журнала
    ext4, а пропустить каталог легко. ``syncfs`` одним вызовом дожидается записи
    всех файлов, каталогов и записей в каталогах этого тома и с Linux 5.8 сообщает
    об ошибках записи. ``os.sync()`` — только запасной путь (libc без ``syncfs``):
    он ошибок не возвращает и сбрасывает все тома.
    """
    try:
        syncfs = ctypes.CDLL(None, use_errno=True).syncfs
    except (OSError, AttributeError):
        os.sync()
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        if syncfs(fd) != 0:
            error = ctypes.get_errno()
            raise UserInstallError(
                f"Не удалось записать файлы Astra Voice на диск: {os.strerror(error)}"
            )
    finally:
        os.close(fd)


def _sync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        log.warning("Не удалось синхронизировать каталог %s", tilde(directory), exc_info=True)


@contextmanager
def _install_lock(app: Path, timeout: float) -> Iterator[None]:
    """``flock`` на ``app/.install.lock``: снимается вместе с процессом, зависших нет."""
    fd = os.open(app / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        "Не удалось дождаться окончания другой установки Astra Voice."
                    ) from None
                time.sleep(LOCK_POLL_S)
        yield
    finally:
        os.close(fd)


def smoke_test(copy: Path, info: BuildInfo) -> None:
    """Смоук ``<копия>/AppRun --version`` до переключения ``current``.

    Сломанная сборка не становится текущей (это и есть атомарная замена v1.0).
    """
    expected = f"{paths.APP_NAME} {info.version}"
    try:
        proc = subprocess.run(
            [str(copy / paths.APPIMAGE_LAUNCHER), "--version"],
            cwd=copy,
            env=childenv.clean_env(private_tmp=True),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=SMOKE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Смоук новой копии не запустился: %s", tilde(exc))
        raise SmokeError("Новая копия Astra Voice не запустилась, установка отменена.") from exc
    output = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or output != expected:
        log.warning(
            "Смоук новой копии: код %s, вывод %r, ошибки %r",
            proc.returncode,
            output[:200],
            tilde(proc.stderr.decode("utf-8", "replace"))[-500:],
        )
        raise SmokeError("Новая копия Astra Voice не запустилась, установка отменена.")


def _copy_verified(src: Path, app: Path, target: Path, info: BuildInfo, smoke: Smoke) -> None:
    """Копия во временный каталог рядом → маркер → сброс на диск → смоук → rename.

    ``copytree`` не делает ``fsync``, а ``rename`` и ``current`` сбрасываются на
    диск сразу; без ``syncfs`` до ``rename`` жёсткий сброс питания мог бы оставить
    текущей копию с файлами нулевой длины.
    """
    src_text = os.fspath(src)

    def ignore(directory: str, names: list[str]) -> list[str]:
        return [paths.INSTALLED_MARKER] if directory == src_text else []

    temporary = Path(tempfile.mkdtemp(prefix=f"{TMP_PREFIX}{info.key}.", dir=app))
    renamed = False
    try:
        try:
            shutil.copytree(src, temporary, symlinks=True, dirs_exist_ok=True, ignore=ignore)
            (temporary / paths.INSTALLED_MARKER).write_bytes(b"")
        except (OSError, shutil.Error) as exc:
            raise UserInstallError(
                f"Не удалось скопировать файлы Astra Voice: {tilde(exc)}"
            ) from exc
        _sync_filesystem(temporary)
        smoke(temporary, info)
        stale: Path | None = None
        if os.path.lexists(target):
            # Повреждённая копия того же KEY: убираем в сторону, новая встаёт атомарно.
            stale = app / f"{TMP_PREFIX}{info.key}.stale-{secrets.token_hex(6)}"
            os.rename(target, stale)
        os.rename(temporary, target)
        renamed = True
        _sync_directory(app)
        if stale is not None:
            _remove_entry(stale)
    finally:
        if not renamed:
            shutil.rmtree(temporary, ignore_errors=True)


def _link_key(link: Path) -> str | None:
    """KEY, на который указывает наш симлинк, или ``None``."""
    try:
        info = os.lstat(link)
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode):
        raise UserInstallError(f"{tilde(link)} — не ссылка; установка остановлена.")
    target = os.readlink(link)
    return target if is_key(target) else None


def _replace_link(app: Path, name: str, key: str) -> None:
    """Атомарно: ``symlink(tmp)`` + ``replace(tmp, app/name)``; цель — имя, не путь."""
    temporary = app / f"{TMP_PREFIX}link-{secrets.token_hex(6)}"
    os.symlink(key, temporary)
    try:
        os.replace(temporary, app / name)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def _switch_current(app: Path, key: str) -> str | None:
    old = _link_key(app / paths.APPIMAGE_CURRENT)
    previous = _link_key(app / paths.APPIMAGE_PREVIOUS)
    if old is not None and old != key and _is_real_dir(app / old):
        _replace_link(app, paths.APPIMAGE_PREVIOUS, old)
        previous = old
    _replace_link(app, paths.APPIMAGE_CURRENT, key)
    _sync_directory(app)
    return previous


def _remove_entry(path: Path) -> bool:
    """Удаляет каталог (``rmtree`` не идёт по ссылкам) или ссылку; остальное не трогает."""
    try:
        mode = os.lstat(path).st_mode
        if stat.S_ISDIR(mode):
            shutil.rmtree(path)
        elif stat.S_ISLNK(mode) and path.name.startswith(TMP_PREFIX):
            os.unlink(path)
        else:
            log.warning("Не удаляю %s: это не каталог копии", path.name)
            return False
    except OSError as exc:
        log.warning("Не удалось удалить %s: %s", path.name, tilde(exc))
        return False
    return True


def _drop_abandoned_tmp(app: Path) -> list[str]:
    """Под замком: брошенные ``.tmp-*`` прерванных установок (иначе вечное «Недостаточно места»)."""
    return _cleanup(app, (), only_tmp=True)


def _cleanup(app: Path, keep: Iterable[str | None], *, only_tmp: bool = False) -> list[str]:
    keep_names = {name for name in keep if name}
    removed: list[str] = []
    with os.scandir(app) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        if name in keep_names:
            continue
        if not (name.startswith(TMP_PREFIX) or (is_key(name) and not only_tmp)):
            continue
        if is_key(name) and not _is_real_dir(app / name):
            log.warning("Не удаляю %s: это не каталог копии", name)
            continue
        if _remove_entry(app / name):
            removed.append(name)
    return removed


def switch_current(key: str) -> str | None:
    """``previous := current`` (если был и ≠ key), ``current := key``; возвращает previous."""
    if not is_key(key):
        raise ValueError(f"не KEY копии: {key!r}")
    app = paths.appimage_app_dir()
    with _install_lock(app, LOCK_TIMEOUT_S):
        return _switch_current(app, key)


def cleanup() -> list[str]:
    """Удаляет копии, кроме ``current``, ``previous`` и работающей; возвращает имена."""
    app = paths.appimage_app_dir()
    with _install_lock(app, LOCK_TIMEOUT_S):
        return _cleanup(app, _keep_names(app))


def _keep_names(app: Path) -> set[str | None]:
    return {
        _link_key(app / paths.APPIMAGE_CURRENT),
        _link_key(app / paths.APPIMAGE_PREVIOUS),
        read_running_key(),
    }


def install_from_dir(
    src: Path,
    *,
    smoke: Smoke | None = None,
    lock_timeout: float = LOCK_TIMEOUT_S,
    min_free: int = MIN_FREE_BYTES,
) -> InstallResult:
    """Устанавливает бандл ``src`` как ``app/<KEY>`` и делает его текущим.

    Порядок (``arch/appimage.md`` §1): отказ от root и проверка источника (до
    ``app/``) → сведения о сборке → ``flock`` → повторная проверка «уже
    установлено» → уборка брошенных ``.tmp-*`` → место → копия в
    ``app/.tmp-<KEY>.*`` → ``.installed-ok`` → ``syncfs`` → смоук → ``rename`` →
    ``previous := current`` → ``current := KEY`` → чистка.
    Запуск файла всегда делает его версию текущей (старый файл — откат).
    """
    refuse_root()
    src = Path(src)
    check_source(src)
    info = read_build_info(src)
    app = paths.appimage_app_dir()
    target = app / info.key
    if not os.path.lexists(app):
        # Первая установка: место проверяем до создания app/ — при нехватке не
        # остаётся ничего. Если app/ уже есть, проверка — под замком после уборки.
        check_free_space(app, min_free)
    paths.data_dir()
    paths.ensure_private_dir(app)
    with _install_lock(app, lock_timeout):
        copied = False
        if not installed_ok(target, info):
            _drop_abandoned_tmp(app)
            check_free_space(app, min_free)
            _copy_verified(src, app, target, info, smoke if smoke is not None else smoke_test)
            copied = True
        previous = _switch_current(app, info.key)
        removed = _cleanup(app, {info.key, previous, read_running_key()})
    if removed:
        log.info("Удалены старые копии: %s", ", ".join(removed))
    return InstallResult(info.key, target, copied, previous)


def selfinstall_main(argv: Sequence[str], env: Mapping[str, str] | None = None) -> int:
    """``bootstrap.py selfinstall <каталог-бандла> [аргументы программы]``.

    Код 0 — копия установлена и текущая, 10 — установка не нужна (служебный флаг
    или ``ASTRA_VOICE_PORTABLE=1``), иначе — ошибка с сообщением в stderr.
    """
    if not argv or not argv[0].startswith("/"):
        sys.stderr.write(USAGE)
        return EXIT_USAGE
    source, app_args = Path(argv[0]), list(argv[1:])
    try:
        # Отказ от root — до любых решений, даже при служебных флагах (MJ-3).
        refuse_root()
        if not should_install(app_args, env):
            return EXIT_SKIPPED
        install_from_dir(source)
    except UserInstallError as exc:
        sys.stderr.write(f"{exc}\n")
        return exc.exit_code
    except (OSError, paths.PathError) as exc:
        sys.stderr.write(f"Не удалось установить Astra Voice в домашнюю папку: {tilde(exc)}\n")
        return EXIT_FAILED
    return EXIT_OK
