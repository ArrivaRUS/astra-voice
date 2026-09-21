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

На ALSE 1.8 системный client.conf целиком закомментирован, а единственный
01-enable-autospawn.conf в его .d — висячая ссылка на
/run/pulseaudio-enable-autospawn. Системные настройки не теряем; системный .d,
способный вернуть autospawn, намеренно не подключаем. Существующие настройки
пользователя сохраняем через .include между двумя строками autospawn = no.
Парсер поддерживает .include, но не .ifexists/.nofail: если подключаемый файл
исчезнет, разбор прервётся, однако первый запрет уже будет применён; последний
запрет перебивает autospawn = yes при успешном подключении.

PULSE_SERVER, PULSE_SINK, PULSE_SOURCE и PULSE_COOKIE приоритетнее client.conf.
Их сохраняем: в частности, PULSE_SERVER НИКОГДА не меняем, чтобы администратор
мог направить клиента к нужному серверу, а тесты — к несуществующему сокету.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from astra_voice.core import paths

logger = logging.getLogger(__name__)


def _write_config(path: Path, payload: bytes) -> None:
    """Атомарно публикует приватный файл через временный файл в том же каталоге."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def deny_pulse_autospawn() -> None:
    """Подменяет client.conf через PULSE_CLIENTCONFIG, запрещая autospawn.

    Уже заданную непустую переменную уважаем. Иначе сохраняем пользовательские
    настройки через .include, но исключаем системный файл и его опасный .d:
    на ALSE 1.8 действующих настроек в системном client.conf нет. Наш файл
    заменяет основной client.conf; дополнения ищутся только в <наш файл>.d.
    PULSE_SERVER никогда не меняем: его приоритет сохраняет выбор сервера
    администратором и изоляцию тестов. Сбой путей или записи только журналируем.
    """
    if os.environ.get("PULSE_CLIENTCONFIG"):
        logger.debug("PULSE_CLIENTCONFIG уже задан; сохраняем настройку окружения.")
        return

    try:
        config_home = os.environ.get("XDG_CONFIG_HOME")
        user_config = (
            (Path(config_home) if config_home else Path.home() / ".config")
            / "pulse"
            / "client.conf"
        ).absolute()
        lines = [
            "# Создано Astra Voice; править бессмысленно: файл будет перезаписан.",
            "autospawn = no",
        ]
        if any(char in str(user_config) for char in "#;\n\r"):
            logger.debug("Не подключаем client.conf: путь содержит символы #, ;, LF или CR.")
        elif user_config.is_file():
            lines.append(f".include {user_config}")
        lines.append("autospawn = no")
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        path = (paths.cache_dir() / "pulse-client.conf").absolute()
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != payload:
            _write_config(path, payload)
        os.environ["PULSE_CLIENTCONFIG"] = str(path)
    except (OSError, RuntimeError):
        # RuntimeError: paths.PathError или невозможность определить Path.home().
        logger.warning("Не удалось запретить автозапуск звукового сервера.", exc_info=True)
