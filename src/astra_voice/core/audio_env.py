"""Запрет автозапуска PulseAudio для libpulse и наследующих окружение подпроцессов.

GUI и воркер получают устройства через pactl и напрямую через libpulse; у
pa_simple_new нет флага PA_CONTEXT_NOAUTOSPAWN. Если pipewire-pulse.socket
неактивен, даже оставшийся мёртвый сокет не мешает libpulse запустить
установленный pulseaudio, который конфликтует с PipeWire. Поэтому используем
PULSE_CLIENTCONFIG: pa_open_config_file открывает только указанный файл,
ЗАМЕНЯЯ основной client.conf, а не дополняя его. Пользовательский client.conf
и /etc/pulse/client.conf автоматически больше не читаются. Каталог дополнений
парсер строит от имени открытого файла (формат "%s.d" в libpulsecommon):
это <наш файл>.d, а не /etc/pulse/client.conf.d.

На ALSE 1.8 штатный client.conf целиком закомментирован, а единственный
01-enable-autospawn.conf в его .d — висячая ссылка на
/run/pulseaudio-enable-autospawn. Однако администратор может задать в основном
файле default-server. Через .include сохраняем пользовательский client.conf,
а при его отсутствии — /etc/pulse/client.conf. Системный .d, способный вернуть
autospawn, намеренно не подключаем. Первая и последняя строки нашего файла —
autospawn = no; .include находится между ними. Без обоих исходных файлов
остаётся только пара запретов.
Парсер поддерживает .include, но не .ifexists/.nofail: если подключаемый файл
исчезнет, разбор прервётся, однако первый запрет уже будет применён; последний
запрет перебивает autospawn = yes при успешном подключении.

Чужой непустой PULSE_CLIENTCONFIG уважаем. Наш путь перепроверяем при каждом
вызове: кэш могли удалить или подменить. Читаем своим дескриптором без перехода
по ссылкам и без блокировки на FIFO, только обычный файл не больше 64 КиБ.
Несовпадение заменяем приватным файлом 0600; fsync файла и каталога закрепляет
атомарную публикацию на диске.
Каталог на месте файла отодвигаем в приватный соседний каталог, сохраняя его
содержимое без чтения и рекурсивного удаления.

PULSE_SERVER, PULSE_SINK, PULSE_SOURCE и PULSE_COOKIE приоритетнее client.conf.
Их сохраняем: в частности, PULSE_SERVER НИКОГДА не меняем, чтобы администратор
мог направить клиента к нужному серверу, а тесты — к несуществующему сокету.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path

from astra_voice.core import paths

logger = logging.getLogger(__name__)
SYSTEM_CLIENT_CONFIG = Path("/etc/pulse/client.conf")
_CONFIG_NAME = "pulse-client.conf"
_MAX_CONFIG_SIZE = 64 * 1024


def _read_config(path: Path) -> bytes | None:
    """Не доверяем типу и размеру записи в кэше и никогда не читаем через ссылку."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > _MAX_CONFIG_SIZE
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                return None
            current = os.read(fd, _MAX_CONFIG_SIZE)
            # Файл мог вырасти между первой проверкой и чтением.
            if os.fstat(fd).st_size > _MAX_CONFIG_SIZE:
                return None
            return current
        finally:
            os.close(fd)
    except OSError:
        return None


def _write_config(path: Path, payload: bytes) -> None:
    """Публикует файл 0600 через соседний временный файл с fsync файла и каталога."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(temporary, path)
        except IsADirectoryError:
            # rename не заменяет каталог файлом. Отодвигаем даже непустой каталог,
            # не читая его содержимое и не удаляя возможные пользовательские данные.
            displaced = Path(tempfile.mkdtemp(prefix=f".{path.name}.replaced-", dir=path.parent))
            try:
                os.replace(path, displaced / path.name)
            except OSError:
                displaced.rmdir()
                raise
            os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def deny_pulse_autospawn() -> None:
    """Подменяет client.conf через PULSE_CLIENTCONFIG, запрещая autospawn.

    Чужую непустую переменную уважаем, наш файл перепроверяем и восстанавливаем.
    Сохраняем пользовательские настройки через .include; если файла нет,
    подключаем системный client.conf, но не его опасный .d. Наш файл заменяет
    основной client.conf; дополнения ищутся только в <наш файл>.d.
    PULSE_SERVER никогда не меняем: его приоритет сохраняет выбор сервера
    администратором и изоляцию тестов. Сбой путей или записи только журналируем.
    """
    configured = os.environ.get("PULSE_CLIENTCONFIG")
    # Заведомо чужой файл: даже создавать каталог кэша незачем.
    if configured and Path(configured).name != _CONFIG_NAME:
        logger.debug("PULSE_CLIENTCONFIG уже задан; сохраняем настройку окружения.")
        return

    try:
        path = (paths.cache_dir() / _CONFIG_NAME).absolute()
        if configured and configured != str(path):
            logger.debug("PULSE_CLIENTCONFIG уже задан; сохраняем настройку окружения.")
            return
        config_home = os.environ.get("XDG_CONFIG_HOME")
        user_config = (
            (Path(config_home) if config_home else Path.home() / ".config")
            / "pulse"
            / "client.conf"
        ).absolute()
        include = user_config if user_config.exists() else SYSTEM_CLIENT_CONFIG
        lines = ["autospawn = no"]
        if any(char in str(include) for char in "#;\n\r"):
            logger.debug("Не подключаем client.conf: путь содержит символы #, ;, LF или CR.")
        elif include.is_file():
            lines.append(f".include {include}")
        lines.append("autospawn = no")
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        current = _read_config(path)
        if current != payload:
            _write_config(path, payload)
        os.environ["PULSE_CLIENTCONFIG"] = str(path)
    except (OSError, RuntimeError):
        # RuntimeError: paths.PathError или невозможность определить Path.home().
        logger.warning("Не удалось запретить автозапуск звукового сервера.", exc_info=True)
