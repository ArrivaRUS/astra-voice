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
from typing import Literal

from astra_voice.core import childenv, paths
from astra_voice.platform import autostart

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
    "keylib.sh",
    "opt/python3.11/bin/python3.11",
    "usr/lib/astra-voice/bootstrap.py",
    paths.BUILD_MARKER,
)
EXECUTABLE_FILES = (paths.APPIMAGE_LAUNCHER, "opt/python3.11/bin/python3.11")
# Служебные флаги не устанавливают программу (спайк §7.10). Тот же список — в AppRun.
# Снятие регистрации тоже не устанавливает: сначала поставить, чтобы тут же снять, — нелепо.
SERVICE_FLAGS = frozenset(
    {"--version", "--help", "-h", "--stats", "--selfinstall-status", "--unregister", "--uninstall"}
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
# Коды отказов T1 (arch/appimage.md §1).
EXIT_ROOT = 3
EXIT_UNSAFE_SOURCE = 4
# Установка не выполнялась (служебный флаг или ASTRA_VOICE_PORTABLE=1).
EXIT_SKIPPED = 10

USAGE = "usage: bootstrap.py selfinstall <каталог-бандла> [аргументы программы]\n"
ROOT_REFUSED_MESSAGE = (
    "Версия AppImage не работает от имени администратора. Запустите её от обычного пользователя"
)


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


@dataclass(frozen=True)
class RegisterResult:
    """Меню: записано, совпало или чужое; значки: записаны/пропущены."""

    menu: Literal["written", "unchanged", "foreign"]
    icons_written: tuple[Path, ...]
    icons_skipped: tuple[Path, ...]
    autostart: bool


@dataclass(frozen=True)
class UnregisterResult:
    menu: bool
    icons: tuple[Path, ...]
    autostart: bool


@dataclass(frozen=True)
class RemoveResult:
    unregistered: UnregisterResult
    removed: tuple[str, ...]
    kept: tuple[str, ...]


@dataclass(frozen=True)
class InstallStatus:
    current: str | None
    previous: str | None
    running: str | None
    installed: tuple[str, ...]
    menu: Literal["ours", "foreign", "none"]
    icons: bool
    autostart: str


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
    """Запрещает работу AppImage от root до изменения файлов."""
    if paths.install_kind().is_appimage and os.geteuid() == 0:
        raise RootRefusedError(ROOT_REFUSED_MESSAGE)


def check_source(src: Path) -> None:
    """Гигиена режима В (распаковка), без отказов и изменений файлов.

    Вызывается до создания ``app/`` (T-175). Защита от чужой распаковки — вне
    бандла, см. decisions/log.md 30.09 (BL-1a, вариант б).
    """
    # Гигиена режима В (распаковка); защита от чужой распаковки — вне бандла,
    # см. decisions/log.md 30.09 (BL-1a, вариант б).
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


def _read_running_key(path: Path) -> str | None:
    """Проверяет открытый файл; содержимое используется только как строка KEY."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_size > RUNNING_KEY_LIMIT
            ):
                return None
            data = os.read(fd, RUNNING_KEY_LIMIT)
        finally:
            os.close(fd)
        value = data.decode("ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value if is_key(value) else None


def read_running_key() -> str | None:
    """Первый валидный KEY работающей копии по порядку runtime-каталогов."""
    for directory in paths.existing_runtime_dirs():
        key = _read_running_key(directory / RUNNING_KEY_NAME)
        if key is not None:
            return key
    return None


def read_running_keys() -> frozenset[str]:
    """Все валидные KEY работающих копий, включая метки в запасном каталоге."""
    return frozenset(
        key
        for directory in paths.existing_runtime_dirs()
        if (key := _read_running_key(directory / RUNNING_KEY_NAME)) is not None
    )


def write_running_key(key: str) -> None:
    """Отмечает работающую копию, чтобы чистка её не удалила (Р5)."""
    if not is_key(key):
        raise ValueError(f"не KEY копии: {key!r}")
    path = paths.runtime_dir() / RUNNING_KEY_NAME
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
    """Удаляет каталог копии или свою временную запись; по ссылкам не идёт."""
    try:
        mode = os.lstat(path).st_mode
        if stat.S_ISDIR(mode):
            shutil.rmtree(path)
        elif path.name.startswith(TMP_PREFIX) and (stat.S_ISLNK(mode) or stat.S_ISREG(mode)):
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
    """Удаляет копии, кроме ``current``, ``previous`` и работающих; возвращает имена."""
    app = paths.appimage_app_dir()
    with _install_lock(app, LOCK_TIMEOUT_S):
        return _cleanup(app, _keep_names(app))


def _keep_names(app: Path) -> set[str | None]:
    return {
        _link_key(app / paths.APPIMAGE_CURRENT),
        _link_key(app / paths.APPIMAGE_PREVIOUS),
        *read_running_keys(),
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
        removed = _cleanup(app, {info.key, previous, *read_running_keys()})
    if removed:
        log.info("Удалены старые копии: %s", ", ".join(removed))
    return InstallResult(info.key, target, copied, previous)


_MENU_TEMPLATE = """[Desktop Entry]
Type=Application
Version=1.0
Name=Astra Voice
Name[en]=Astra Voice
GenericName=Голосовой ввод
GenericName[en]=Voice input
Comment=Офлайн-распознавание речи: сказали — текст появился в активном окне
Comment[en]=Offline speech to text: speak, and the text lands in the active window
{command}Icon=astravoice
Terminal=false
Categories=Utility;Accessibility;
Keywords=диктовка;речь;распознавание;микрофон;voice;dictation;speech;
StartupNotify=true
StartupWMClass=astra-voice
X-GNOME-UsesNotifications=true
X-AstraVoice-Managed=true
X-AppImage-Version={version}
"""


def menu_entry_bytes(launcher: Path, version: str) -> bytes:
    """Шаблон data/astra-voice.desktop с установленным launcher (§4)."""
    path = str(launcher)
    command = f"Exec={autostart._exec_argument(path)}\n"
    if not autostart._EXEC_RESERVED.intersection(path) and "%" not in path:
        command += f"TryExec={path}\n"
    return _MENU_TEMPLATE.format(command=command, version=version).encode("utf-8")


def _desktop_paths() -> tuple[Path, Path]:
    data = paths.appimage_app_dir().parent.parent
    return data / "applications/astra-voice.desktop", data / "icons/hicolor"


def _menu_state(path: Path) -> Literal["ours", "foreign", "none"]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "none"
    if stat.S_ISREG(mode):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as file:
            if stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                data = file.read()
                if autostart._properties(data).get(b"X-AstraVoice-Managed") == b"true":
                    return "ours"
    return "foreign"


def _icon_files(root: Path, *, links: bool = False) -> Iterator[Path]:
    """Только hicolor/*/apps/astravoice.{png,svg}; не идём по ссылкам каталогов."""
    if not _is_real_dir(root):
        return
    for size in sorted(root.iterdir()):
        apps = size / "apps"
        if not _is_real_dir(size) or not _is_real_dir(apps):
            continue
        for name in ("astravoice.png", "astravoice.svg"):
            icon = apps / name
            try:
                mode = icon.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISREG(mode) or (links and stat.S_ISLNK(mode)):
                yield icon


def _public_directory(path: Path) -> None:
    """Создаёт каталоги меню/значков 0755, не меняя права существующих."""
    if os.path.lexists(path):
        if not _is_real_dir(path):
            raise UserInstallError(f"Не удалось добавить запись: {tilde(path)} — не папка.")
        return
    _public_directory(path.parent)
    path.mkdir(mode=0o755, exist_ok=True)
    path.chmod(0o755)


def _write_desktop_file(path: Path, data: bytes) -> bool:
    """Атомарная запись рядом с целью; совпавшие байты сохраняют mtime (§4)."""
    _public_directory(path.parent)
    if os.path.lexists(path):
        if not stat.S_ISREG(path.lstat().st_mode):
            log.warning("Пропущен необычный файл %s", tilde(path))
            return False
        if path.read_bytes() == data:
            return False
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as file:
            temporary = file.name
            file.write(data)
            file.flush()
            os.fchmod(file.fileno(), 0o644)
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if temporary is not None and os.path.lexists(temporary):
            os.unlink(temporary)
    return True


def register() -> RegisterResult:
    """Добавляет установленную копию в меню, перенацеливает наш автозапуск (§2–4)."""
    refuse_root()
    app = paths.appimage_app_dir()
    try:
        key = _link_key(app / paths.APPIMAGE_CURRENT) if _is_real_dir(app) else None
        valid = key is not None and _is_real_dir(app / key)
        if valid and key is not None:
            valid = stat.S_ISREG((app / key / paths.INSTALLED_MARKER).lstat().st_mode)
    except (OSError, UserInstallError):
        valid = False
    if not valid or key is None:
        raise UserInstallError("Программа ещё не установлена в домашнюю папку.")
    launcher = paths.check_appimage_launcher(paths.appimage_current_apprun())
    copy = app / key
    info = read_build_info(copy)
    data = menu_entry_bytes(launcher, info.version)
    menu, icons = _desktop_paths()
    menu_result: Literal["written", "unchanged", "foreign"]
    if _menu_state(menu) == "foreign":
        log.warning("Чужая запись меню сохранена: %s", tilde(menu))
        menu_result = "foreign"
    else:
        menu_result = "written" if _write_desktop_file(menu, data) else "unchanged"
    written: list[Path] = []
    skipped: list[Path] = []
    try:
        _copy_icons(copy, icons, written, skipped)
    except (OSError, UserInstallError) as exc:
        log.warning("Значки программы не добавлены: %s", tilde(exc))
    # Автозапуск перенацеливается независимо от значков.
    retargeted = autostart.retarget()
    return RegisterResult(menu_result, tuple(written), tuple(skipped), retargeted)


def _copy_icons(copy: Path, icons: Path, written: list[Path], skipped: list[Path]) -> None:
    """Значки приложения — по возможности: сбой одного значка не мешает остальным."""
    source = copy
    for part in ("usr", "share", "icons", "hicolor"):
        source /= part
        if not _is_real_dir(source):
            return
    for icon in _icon_files(source):
        destination = icons / icon.relative_to(source)
        try:
            fd = os.open(icon, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    continue
                changed = _write_desktop_file(destination, file.read())
        except (OSError, UserInstallError) as exc:
            log.warning("Значок %s не добавлен: %s", destination.name, tilde(exc))
            continue
        (written if changed else skipped).append(destination)


def unregister(*, deb_executable: Path | None = None) -> UnregisterResult:
    """Снимает только нашу регистрацию, возвращая автозапуск пакету при наличии (§4)."""
    refuse_root()
    if deb_executable is None:
        deb_executable = paths.SYSTEM_EXECUTABLE
    menu, icons = _desktop_paths()
    removed_menu = _menu_state(menu) == "ours"
    if removed_menu:
        menu.unlink()
        _sync_directory(menu.parent)
    removed_icons: list[Path] = []
    if _is_real_dir(icons.parent):
        for icon in _icon_files(icons, links=True):
            icon.unlink()
            _sync_directory(icon.parent)
            removed_icons.append(icon)
    changed = (
        autostart.retarget(autostart.DEB_EXECUTABLE)
        if deb_executable.exists() and os.access(deb_executable, os.X_OK)
        else autostart.remove_ours()
    )
    return UnregisterResult(removed_menu, tuple(removed_icons), changed)


def remove_program(*, keep: str | None = None) -> RemoveResult:
    """Удаляет только копии в app/ под замком (§4).

    Работающие копии (``running-key``) не удаляются никогда (Р5); ``keep`` добавляет
    к ним ещё одну копию, а не заменяет защиту. Оставленные копии удаляет вызывающий
    при выходе.
    """
    unregistered = unregister()
    app = paths.appimage_app_dir()
    if not _is_real_dir(app):
        return RemoveResult(unregistered, (), ())
    with _install_lock(app, LOCK_TIMEOUT_S):
        protected = {*read_running_keys(), keep}
        kept = tuple(
            sorted(
                entry.name
                for entry in app.iterdir()
                if entry.name in protected and is_key(entry.name) and _is_real_dir(entry)
            )
        )
        removed = _cleanup(app, kept)
        for name in (paths.APPIMAGE_CURRENT, paths.APPIMAGE_PREVIOUS):
            link = app / name
            if link.is_symlink():
                link.unlink()
                removed.append(name)
        _sync_directory(app)
    for name in removed:
        log.info("Удалено: %s", tilde(app / name))
    return RemoveResult(unregistered, tuple(removed), kept)


def status() -> InstallStatus:
    """Состояние установки без создания файлов и каталогов (§4)."""
    app = paths.appimage_app_dir()
    current = previous = None
    installed: tuple[str, ...] = ()
    menu_state: Literal["ours", "foreign", "none"] = "none"
    icons_present = False
    try:
        if _is_real_dir(app):
            installed = tuple(
                sorted(p.name for p in app.iterdir() if is_key(p.name) and _is_real_dir(p))
            )
            current = _link_key(app / paths.APPIMAGE_CURRENT)
            previous = _link_key(app / paths.APPIMAGE_PREVIOUS)
    except (OSError, UserInstallError) as exc:
        log.warning("Не удалось проверить копии программы: %s", tilde(exc))
    menu, icons = _desktop_paths()
    try:
        menu_state = _menu_state(menu)
        icons_present = (
            _is_real_dir(icons.parent) and next(_icon_files(icons, links=True), None) is not None
        )
    except OSError as exc:
        log.warning("Не удалось проверить меню и значки: %s", tilde(exc))
    try:
        target = autostart.state().target
    except (OSError, paths.PathError) as exc:
        log.warning("Не удалось проверить автозапуск: %s", tilde(exc))
        target = "none"
    return InstallStatus(
        current, previous, read_running_key(), installed, menu_state, icons_present, target
    )


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
