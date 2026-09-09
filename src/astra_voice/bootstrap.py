"""Единая точка входа всех процессов Astra Voice (синтез-план §7, И7).

Запускается как скрипт, а не импортируется:

    /usr/bin/python3 -I /usr/lib/astra-voice/bootstrap.py app [аргументы]

Два расположения файла:

* установленное — ``/usr/lib/astra-voice/bootstrap.py`` рядом с каталогом
  ``astra_voice/`` и (при наличии) ``vendor/``;
* режим разработки — ``<репозиторий>/src/astra_voice/bootstrap.py``.

Пути вычисляются от ``__file__`` (relocatable), ``PYTHONPATH`` не используется.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

COMMANDS = ("app", "worker", "helper")

USAGE = "usage: bootstrap.py {app|worker|helper} [аргументы]\n"

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


def _harden() -> None:
    """Запрещает core-dump процессу (модель угроз У9)."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:  # noqa: BLE001 — отсутствие ограничения не повод падать
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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in COMMANDS:
        sys.stderr.write(USAGE)
        return 2
    command, rest = args[0], args[1:]

    _setup_sys_path(Path(__file__).resolve().parent)

    if command in ("app", "worker"):
        _harden()
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
