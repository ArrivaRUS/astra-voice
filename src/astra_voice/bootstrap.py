"""Единая точка входа всех процессов Astra Voice (синтез-план §7, И7).

Запускается как скрипт, а не импортируется:

    /usr/bin/python3 -I /usr/lib/astra-voice/bootstrap.py app [аргументы]

Два расположения файла:

* установленное — ``/usr/lib/astra-voice/bootstrap.py`` рядом с каталогом
  ``astra_voice/`` и (при наличии) ``vendor/``;
* режим разработки — ``<репозиторий>/src/astra_voice/bootstrap.py``.

Бандл AppImage повторяет установленную раскладку внутри AppDir
(``<AppDir>/usr/lib/astra-voice/bootstrap.py``); команда ``selfinstall``
копирует бандл в домашнюю папку (``arch/appimage.md`` §1).

Пути вычисляются от ``__file__`` (relocatable), ``PYTHONPATH`` не используется.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

COMMANDS = ("app", "worker", "helper", "selfinstall")

USAGE = "usage: bootstrap.py {app|worker|helper|selfinstall} [аргументы]\n"

# Рендер Qt Quick. По умолчанию программный: GL-контекст стоит 65 МБ RSS
# (167 740 → 102 580 кБ, замеры `spikes/m1_live/rss.md`), картинка совпадает.
# `ASTRA_VOICE_RENDER=gl` возвращает аппаратный рендер.
RENDER_ENV = "ASTRA_VOICE_RENDER"
RENDER_GL = "gl"
SOFTWARE_RENDER_ENV = {
    "QT_QUICK_BACKEND": "software",
    # Без этого Mesa всё равно подтянет XCB-плагин под GLX, и экономия падает
    # с 38,8 % до 3,9 % — переменные работают только вместе.
    "QT_XCB_GL_INTEGRATION": "none",
}

# Запрет appimage=deny (arch/appimage.md §5). Снять себя из меню и автозапуска
# можно всегда: эти флаги запрет не блокирует.
UNREGISTER_FLAGS = frozenset({"--unregister", "--uninstall"})
# Служебные флаги трека AppImage (ставит AppRun): системная версия их не знает.
APPIMAGE_INTERNAL_FLAGS = frozenset({"--register"})
EXIT_POLICY_DENIED = 3
# Защита от петли: системная версия, которая сама оказалась AppImage под запретом,
# не передаёт запуск дальше. Внешним программам не достаётся (clean_env снимает ASTRA_VOICE_*).
HANDOFF_ENV = "ASTRA_VOICE_POLICY_HANDOFF"
HANDOFF_MESSAGE = "AppImage запрещён политикой, запускаю системную версию"


def _setup_sys_path(here: Path) -> None:
    """Вставляет vendor и корень пакета в начало ``sys.path``."""
    vendor = here / "vendor"
    package_root = here if (here / "astra_voice").is_dir() else here.parent
    for entry in (package_root, vendor):
        if entry is vendor and not vendor.is_dir():
            continue
        text = str(entry)
        if text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)


def _harden(command: str) -> None:
    """Запрещает core-dump процессу (модель угроз У9)."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:  # noqa: BLE001 — отсутствие ограничения не повод падать
        pass
    if command == "worker":
        # После dumpable=0 файл принадлежит root: пишем заранее (У9/У8).
        try:
            Path("/proc/self/oom_score_adj").write_text("500", encoding="ascii")
        except Exception:  # noqa: BLE001
            pass
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
    except Exception:  # noqa: BLE001
        pass


def _setup_render_env() -> None:
    """Включает программный рендер Qt Quick, если пользователь не решил иначе.

    Значения, уже заданные в окружении, не перебиваются: ``setdefault``
    оставляет и осознанный выбор пользователя, и настройки администратора.
    """
    if os.environ.get(RENDER_ENV, "").strip().lower() == RENDER_GL:
        return
    for name, value in SOFTWARE_RENDER_ENV.items():
        os.environ.setdefault(name, value)


def _refuse_root_in_bundle() -> int | None:
    """Отказ работы от root в бандле AppImage — точка расширения; до 01.10 пропускает."""
    # T1-01.10: MJ-3 — в бандле (paths.install_kind().is_appimage) при os.geteuid() == 0
    # любая команда (app, selfinstall, --uninstall, даже --version) → сообщение «Версия
    # AppImage не запускается от имени root…» и код 3 до импорта Qt; трек .deb не затронут (T-179).
    return None


def _system_version() -> str | None:
    """Лаунчер пакета .deb, если он стоит и исполняемый; иначе ``None``."""
    from astra_voice.core import paths

    path = paths.SYSTEM_EXECUTABLE
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return None


def _journal(message: str) -> None:
    """Одна строка в журнал программы; сбой журнала не мешает запуску."""
    try:
        import logging

        from astra_voice.core.logging import setup_logging

        setup_logging()
        logging.getLogger("astra_voice.bootstrap").info("%s", message)
    except Exception:  # noqa: BLE001 — без журнала запуск всё равно важнее
        pass


def _appimage_policy_gate(command: str, rest: list[str]) -> int | None:
    """Совещательный запрет трека AppImage (arch/appimage.md §5); ``None`` — продолжать.

    Решает только ``core/policy.py``. При запрете: снятие регистрации проходит;
    если стоит пакет .deb — запуск переходит к нему (``selfinstall`` ничего не
    копирует и отдаёт запуск команде ``app``, та делает ``exec``); иначе — код 3.
    """
    from astra_voice.core import paths, policy

    if not paths.install_kind().is_appimage or not policy.appimage_denied(policy.load()):
        return None
    user_args = rest[1:] if command == "selfinstall" else rest
    if UNREGISTER_FLAGS.intersection(user_args):
        return None
    system = None if os.environ.get(HANDOFF_ENV) == "1" else _system_version()
    if system is None:
        sys.stderr.write(policy.APPIMAGE_DENIED_MESSAGE + "\n")
        return EXIT_POLICY_DENIED
    if command == "selfinstall":
        from astra_voice.platform.userinstall import EXIT_SKIPPED

        return EXIT_SKIPPED
    from astra_voice.core.childenv import clean_env

    env = clean_env()
    env[HANDOFF_ENV] = "1"
    _journal(HANDOFF_MESSAGE)
    argv = [arg for arg in user_args if arg not in APPIMAGE_INTERNAL_FLAGS]
    os.execve(system, [system, *argv], env)
    return EXIT_POLICY_DENIED  # сюда execve не возвращается; для тестов с подменой


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in COMMANDS:
        sys.stderr.write(USAGE)
        return 2
    command, rest = args[0], args[1:]

    _setup_sys_path(Path(__file__).resolve().parent)

    refused = _refuse_root_in_bundle()
    if refused is not None:
        return refused

    if command in ("app", "selfinstall"):
        # Совещательный запрет трека до любых действий (arch/appimage.md §5).
        denied = _appimage_policy_gate(command, rest)
        if denied is not None:
            return denied

    if command == "selfinstall":
        # Самоустановка AppImage (arch/appimage.md §1): без Qt, без аудио.
        from astra_voice.platform.userinstall import selfinstall_main

        return selfinstall_main(rest)

    if command in ("app", "worker"):
        from astra_voice.core.audio_env import deny_pulse_autospawn
        from astra_voice.core.childenv import BOOTSTRAP_MANAGED, save_originals

        # До любых перезаписей: внешним программам вернём исходные значения
        # (childenv.clean_env), а не стиль и рендер, выбранные для нас.
        save_originals(BOOTSTRAP_MANAGED)

        # Журнал ещё не настроен: важнее успеть до импорта Qt и libpulse.
        # Воркер повторит вызов после настройки журнала и запишет возможный сбой.
        deny_pulse_autospawn()
        _harden(command)
    if command == "app":
        # Fly навязывает свой стиль через /etc/X11/Xsession.d/06-fly-misc-env,
        # поэтому ставим принудительно и до импорта Qt.
        os.environ["QT_QUICK_CONTROLS_STYLE"] = "Default"
        # Рендер выбирается только для GUI и тоже до импорта Qt.
        _setup_render_env()

    if command == "app":
        from astra_voice.app import main as entry
    elif command == "worker":
        from astra_voice.worker.main import main as entry
    else:
        from astra_voice.helper.main import main as entry
    return entry(rest)


if __name__ == "__main__":
    raise SystemExit(main())
