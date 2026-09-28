"""XDG autostart entry for the current user, without desktop or Qt dependencies."""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

_NAME = "astra-voice.desktop"
EXECUTABLE = "/usr/bin/astra-voice"
_ENTRY = b"".join(
    (
        b"[Desktop Entry]\n",
        b"Type=Application\n",
        b"Name=Astra Voice\n",
        f"Exec={EXECUTABLE} --hidden\n".encode(),
        b"Icon=astravoice\n",
        b"NoDisplay=true\n",
        b"X-KDE-autostart-after=panel\n",
        b"X-AstraVoice-Managed=true\n",
    )
)


class AutostartError(OSError):
    """Unable to read or change the user's autostart entry."""


@dataclass(frozen=True)
class AutostartState:
    enabled: bool
    user: Literal["none", "ours", "foreign"]
    system: bool
    system_hidden: bool


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
    if user_data is None:
        user = "none"
    elif _true(_properties(user_data), b"X-AstraVoice-Managed"):
        user = "ours"
    else:
        user = "foreign"
    selected = user_data if user_data is not None else system_data
    enabled = selected is not None and not _true(_properties(selected), b"Hidden")
    return (
        AutostartState(enabled, user, system_data is not None, system_hidden),
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
                _write(path, _ENTRY)
            elif current.user == "ours":
                if system_active:
                    _unlink(path)
                else:
                    _write(path, _ENTRY)
            elif data is not None:
                _write(path, _hidden_lines(data, enable=True))
        elif current.user == "ours":
            if system_active:
                _write(path, _ENTRY + b"Hidden=true\n")
            else:
                _unlink(path)
        elif current.user == "foreign" and data is not None:
            _write(path, _hidden_lines(data, enable=False))
        elif system_active:
            _write(path, _ENTRY + b"Hidden=true\n")
        if _inspect()[0].enabled != enabled:
            raise AutostartError(f"Не удалось установить состояние автозапуска {path}")
    except OSError as exc:
        if isinstance(exc, AutostartError):
            raise
        raise AutostartError(f"Не удалось изменить запись автозапуска {path}: {exc}") from exc
