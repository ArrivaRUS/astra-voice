"""XDG autostart entry for the current user, without desktop or Qt dependencies."""

from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from astra_voice.core import paths

log = logging.getLogger(__name__)

_NAME = "astra-voice.desktop"
DEB_EXECUTABLE = "/usr/bin/astra-voice"
# Совместимость: исполняемый файл трека .deb и исходников. Путь по треку — executable().
EXECUTABLE = DEB_EXECUTABLE
# Символы, из-за которых аргумент Exec нужно брать в кавычки (Desktop Entry, «Exec key»).
_EXEC_RESERVED = frozenset(" \t\n\"'\\><~|&;$*?#()`")


class AutostartError(OSError):
    """Unable to read or change the user's autostart entry."""


class AutostartUnavailableError(AutostartError):
    """AppImage работает без установки: запись автозапуска не пишется (arch/appimage.md §3)."""


def executable() -> str:
    """Что запускает автозапуск в этом треке (arch/appimage.md §3).

    ``.deb`` и исходники — ``/usr/bin/astra-voice`` (как в v0.1); установленная
    копия AppImage — ``<data_dir>/app/current/AppRun``; AppImage без установки —
    отказ: запись на ``$APPIMAGE``, монтирование или распаковку не пишется никогда.
    """
    kind = paths.install_kind()
    if kind is paths.InstallKind.APPIMAGE_INSTALLED:
        # T1-01.10: MJ-1 — строгая проверка пути в paths.check_appimage_launcher().
        return str(paths.check_appimage_launcher(paths.appimage_current_apprun()))
    if kind is paths.InstallKind.APPIMAGE_PORTABLE:
        raise AutostartUnavailableError("Автозапуск доступен после установки программы")
    return DEB_EXECUTABLE


def _exec_argument(path: str) -> str:
    """Путь как аргумент ключа Exec: кавычки при необходимости, ``%%`` (MN-2)."""
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in path):
        raise AutostartError("Путь к программе содержит управляющие символы")
    if not _EXEC_RESERVED.intersection(path):
        return path.replace("%", "%%")
    quoted = "".join("\\" + char if char in '"`$\\' else char for char in path)
    # Общее правило строк .desktop применяется поверх правила кавычек: «\» → «\\».
    return '"' + quoted.replace("\\", "\\\\").replace("%", "%%") + '"'


def entry_bytes(executable_path: str | None = None) -> bytes:
    """Наша запись автозапуска; для ``.deb`` — байт в байт как в v0.1."""
    path = executable() if executable_path is None else executable_path
    lines = [
        b"[Desktop Entry]\n",
        b"Type=Application\n",
        b"Name=Astra Voice\n",
        f"Exec={_exec_argument(path)} --hidden\n".encode(),
    ]
    if path != DEB_EXECUTABLE and not _EXEC_RESERVED.intersection(path) and "%" not in path:
        # Тихий пропуск, если пользователь удалил app/ (arch/appimage.md §3).
        lines.append(f"TryExec={path}\n".encode())
    lines += [
        b"Icon=astravoice\n",
        b"NoDisplay=true\n",
        b"X-KDE-autostart-after=panel\n",
        b"X-AstraVoice-Managed=true\n",
    ]
    return b"".join(lines)


_ENTRY = entry_bytes(DEB_EXECUTABLE)


@dataclass(frozen=True)
class AutostartState:
    enabled: bool
    user: Literal["none", "ours", "foreign"]
    system: bool
    system_hidden: bool
    target: Literal["ours-this", "ours-other", "foreign", "system", "none"] = field(
        default="none", compare=False
    )


def _paths() -> tuple[Path, list[Path]]:
    configured_home = Path(os.environ.get("XDG_CONFIG_HOME") or "")
    home = configured_home if configured_home.is_absolute() else Path.home() / ".config"
    dirs = os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"
    system_dirs = [
        Path(directory) for directory in dirs.split(":") if Path(directory).is_absolute()
    ]
    if not system_dirs:
        system_dirs = [Path("/etc/xdg")]
    return home / "autostart" / _NAME, [
        directory / "autostart" / _NAME for directory in system_dirs
    ]


def _group_line(line: bytes) -> bytes:
    return line.removeprefix(b"\xef\xbb\xbf").strip()


def _properties(data: bytes) -> dict[bytes, bytes]:
    properties: dict[bytes, bytes] = {}
    in_entry = False
    for line in data.splitlines():
        stripped = _group_line(line)
        if stripped.startswith(b"[") and stripped.endswith(b"]"):
            in_entry = stripped == b"[Desktop Entry]"
        elif in_entry and b"=" in line and not stripped.startswith(b"#"):
            key, value = line.split(b"=", 1)
            properties[key.strip()] = value.strip()
    return properties


def _true(properties: dict[bytes, bytes], key: bytes) -> bool:
    return properties.get(key, b"").strip().lower() in (b"true", b"1", b"yes", b"on")


def _exec_program(value: bytes) -> str | None:
    """Первое слово Exec: строковые escape, затем кавычки и %% (Desktop Entry §7)."""
    try:
        command = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    escapes = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    command = re.sub(r"\\([sntr\\])", lambda match: escapes[match[1]], command).lstrip()
    quoted = command.startswith('"')
    index = 1 if quoted else 0
    program: list[str] = []
    while index < len(command):
        char = command[index]
        if quoted and char == '"':
            index += 1
            if index < len(command) and command[index] not in " \t\r\n":
                return None
            break
        if not quoted and char in " \t\r\n":
            break
        if quoted and char == "\\":
            index += 1
            if index == len(command) or command[index] not in '"`$\\':
                return None
            char = command[index]
        elif (not quoted and char in _EXEC_RESERVED) or (quoted and char in "`$"):
            return None
        if char == "%":
            index += 1
            if index == len(command) or command[index] != "%":
                return None
        program.append(char)
        index += 1
    else:
        if quoted:
            return None
    return "".join(program) or None


def _read(path: Path) -> bytes | None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("Не удалось проверить запись автозапуска %s: %s", path, exc)
        raise
    try:
        if stat.S_ISLNK(mode):
            mode = os.stat(path).st_mode
        if not stat.S_ISREG(mode):
            raise AutostartError(f"Необычный тип записи автозапуска: {path}")
        with path.open("rb") as file:
            data = file.read(65537)
        if len(data) > 65536:
            raise AutostartError(f"Слишком большая запись автозапуска: {path}")
        return data
    except OSError as exc:
        log.warning("Не удалось прочитать запись автозапуска %s: %s", path, exc)
        raise


def _inspect() -> tuple[AutostartState, Path, bytes | None]:
    user_path, system_paths = _paths()
    user_data = _read(user_path)
    system_data = next((data for path in system_paths if (data := _read(path)) is not None), None)
    system_hidden = system_data is not None and _true(_properties(system_data), b"Hidden")
    user: Literal["none", "ours", "foreign"]
    target: Literal["ours-this", "ours-other", "foreign", "system", "none"]
    if user_data is None:
        user = "none"
        target = "system" if system_data is not None else "none"
    elif _true(_properties(user_data), b"X-AstraVoice-Managed"):
        user = "ours"
        target = "ours-other"
        try:
            if _exec_program(_properties(user_data).get(b"Exec", b"")) == executable():
                target = "ours-this"
        except AutostartUnavailableError:
            pass
        except paths.PathError as exc:
            # Строгая проверка пути программы отказала: запись наша, но на эту копию
            # не указывает достоверно — «ours-other», чтение состояния не падает.
            log.warning("Не удалось проверить путь программы для автозапуска: %s", exc)
    else:
        user = "foreign"
        target = "foreign"
    selected = user_data if user_data is not None else system_data
    enabled = selected is not None and not _true(_properties(selected), b"Hidden")
    return (
        AutostartState(enabled, user, system_data is not None, system_hidden, target),
        user_path,
        user_data,
    )


def state() -> AutostartState:
    """Read the effective XDG entry without changing any files."""
    try:
        return _inspect()[0]
    except OSError as exc:
        raise AutostartError(f"Не удалось прочитать запись автозапуска: {exc}") from exc


def _hidden_lines(data: bytes, *, enable: bool) -> bytes:
    lines = data.splitlines(keepends=True)
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    in_entry = False
    found = False
    result: list[bytes] = []
    for line in lines:
        stripped = _group_line(line)
        if stripped.startswith(b"[") and stripped.endswith(b"]"):
            if in_entry and not enable and not found:
                result.append(b"Hidden=true" + newline)
            in_entry = stripped == b"[Desktop Entry]"
            found = False
        key = line.split(b"=", 1)[0].strip() if b"=" in line else b""
        if in_entry and key == b"Hidden" and not stripped.startswith(b"#"):
            if enable:
                # A line added to a file without a final newline took its separator
                # from the preceding line; remove it as well on the reverse edit.
                if not (line.endswith(b"\n") or line.endswith(b"\r")) and result:
                    result[-1] = result[-1].removesuffix(b"\r\n").removesuffix(b"\n")
                continue
            if not found:
                ending = (
                    b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
                )
                result.append(b"Hidden=true" + ending)
                found = True
            continue
        result.append(line)
    if in_entry and not enable and not found:
        if result and not result[-1].endswith((b"\n", b"\r")):
            result.append(newline)
            result.append(b"Hidden=true")
        else:
            result.append(b"Hidden=true" + newline)
    return b"".join(result)


def _write(path: Path, data: bytes) -> None:
    created_directory = not path.parent.exists()
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if created_directory:
        path.parent.chmod(0o755)
    mode = os.lstat(path).st_mode & 0o777 if os.path.lexists(path) else 0o644
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{_NAME}.", delete=False
        ) as file:
            temporary = file.name
            file.write(data)
            file.flush()
            os.fchmod(file.fileno(), mode)
            os.fsync(file.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def _sync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        log.warning("Не удалось синхронизировать каталог автозапуска %s", directory, exc_info=True)


def _unlink(path: Path) -> None:
    path.unlink()
    _sync_directory(path.parent)


def remove_ours() -> bool:
    """Удаляет только нашу обычную запись при снятии регистрации (§4)."""
    path = _paths()[0]
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return False
    except FileNotFoundError:
        return False
    data = _read(path)
    if data is None or not _true(_properties(data), b"X-AstraVoice-Managed"):
        return False
    _unlink(path)
    return True


def _line_ending(line: bytes) -> bytes:
    return (
        b"\r\n" if line.endswith(b"\r\n") else line[-1:] if line.endswith((b"\n", b"\r")) else b""
    )


def _retarget_lines(data: bytes, path: str) -> bytes:
    """Меняем только Exec/TryExec в основной секции, сохраняя оформление (§3)."""
    exec_line = f"Exec={_exec_argument(path)} --hidden".encode()
    try_line = (
        f"TryExec={path}".encode()
        if path != DEB_EXECUTABLE and not _EXEC_RESERVED.intersection(path) and "%" not in path
        else None
    )
    lines = data.splitlines(keepends=True)
    exec_indices: set[int] = set()
    try_indices: set[int] = set()
    in_entry = False
    for index, line in enumerate(lines):
        stripped = _group_line(line)
        if stripped.startswith(b"[") and stripped.endswith(b"]"):
            in_entry = stripped == b"[Desktop Entry]"
        elif in_entry and b"=" in line and not stripped.startswith(b"#"):
            key = line.split(b"=", 1)[0].strip()
            if key == b"Exec":
                exec_indices.add(index)
            elif key == b"TryExec":
                try_indices.add(index)
    if not exec_indices:
        raise AutostartError("В записи автозапуска не указана программа")
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    result: list[bytes] = []
    for index, line in enumerate(lines):
        ending = _line_ending(line)
        if index in exec_indices:
            result.append(exec_line + ending)
            if try_line is not None and not try_indices:
                if not ending:
                    result[-1] += newline
                result.append(try_line + ending)
        elif index in try_indices:
            if try_line is not None:
                result.append(try_line + ending)
        else:
            result.append(line)
    updated = b"".join(result)
    if not _line_ending(data):
        # Удалённый последний TryExec не должен добавлять финальный перевод строки.
        updated = updated.removesuffix(_line_ending(updated))
    return updated


def retarget(executable_path: str | None = None) -> bool:
    """Перенацелить нашу запись при register/unregister, никогда при старте (§3)."""
    path = _paths()[0]
    try:
        target = executable() if executable_path is None else executable_path
        if target != DEB_EXECUTABLE and target != str(
            paths.check_appimage_launcher(paths.appimage_current_apprun())
        ):
            raise AutostartError("Недопустимая программа для автозапуска")
        _exec_argument(target)
        if path.is_symlink():
            # Как remove_ours(): ссылку пользователя не трогаем, регистрация идёт дальше.
            log.warning("Запись автозапуска — симлинк, оставляю как есть: %s", path.name)
            return False
        data = _read(path)
        if data is None or not _true(_properties(data), b"X-AstraVoice-Managed"):
            return False
        updated = _retarget_lines(data, target)
        if updated == data:
            return False
        _write(path, updated)
        return True
    except OSError as exc:
        if isinstance(exc, AutostartError):
            raise
        raise AutostartError(f"Не удалось изменить запись автозапуска {path}: {exc}") from exc


def set_enabled(enabled: bool) -> None:
    """Change only the user's XDG override, preserving foreign entry content."""
    path = _paths()[0]
    try:
        if os.path.islink(path):
            log.warning("Пользовательская запись автозапуска — симлинк: %s", path)
            raise AutostartError(f"Запись автозапуска является симлинком: {path}")
        current, path, data = _inspect()
        if current.enabled == enabled:
            return
        system_active = current.system and not current.system_hidden
        if enabled:
            if current.user == "none":
                _write(path, entry_bytes())
            elif current.user == "ours":
                if system_active:
                    _unlink(path)
                else:
                    _write(path, entry_bytes())
            elif data is not None:
                _write(path, _hidden_lines(data, enable=True))
        elif current.user == "ours":
            if system_active:
                _write(path, entry_bytes() + b"Hidden=true\n")
            else:
                _unlink(path)
        elif current.user == "foreign" and data is not None:
            _write(path, _hidden_lines(data, enable=False))
        elif system_active:
            _write(path, entry_bytes() + b"Hidden=true\n")
        if _inspect()[0].enabled != enabled:
            raise AutostartError(f"Не удалось установить состояние автозапуска {path}")
    except OSError as exc:
        if isinstance(exc, AutostartError):
            raise
        raise AutostartError(f"Не удалось изменить запись автозапуска {path}: {exc}") from exc
