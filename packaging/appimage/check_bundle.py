#!/usr/bin/env python3
"""Гейт состава бандла AppImage (arch/appimage.md §9.1, §11 п.3; тест T-169).

Запускается бандловым интерпретатором в изоляции (`build.sh`):

    <AppDir>/opt/python3.11/bin/python3.11 -I check_bundle.py \\
        --appdir <AppDir> --control packaging/debian/control

Проверяет:

1. `sys.path` — только каталоги внутри AppDir (хостовые пакеты не подмешиваются);
2. каждый модуль Python из `Depends` и `Recommends` пакета `.deb` (таблица «пакет →
   модуль» ниже) импортируется и лежит внутри AppDir; неизвестный `python3-*` —
   ошибка (таблицу обновляют вместе с `control`), не-Python зависимости — только
   из списка хостовых исключений;
3. все модули `astra_voice` импортируются (как `packaging/smoke-installed.sh`);
4. `schema.require_available()` и `load_builtin(..., require_schema=True)` проходят —
   каталог грузится с проверкой по схеме, подпись — хостовым `/usr/bin/gpgv`;
5. за всё время проверки — ни одной записи журнала уровня WARNING и выше;
6. печатает версию OpenSSL (для SBOM, принятый риск П16).

`--allow-missing <пакет>` (только `build.sh` при локальной сборке с
`ASTRA_VOICE_APPIMAGE_ALLOW_TODO_HASH=1`): нарушения про модуль незакреплённого колеса
(строка `# TODO-HASH` в lock) печатаются как «пропущено», любые другие валят гейт.

Код возврата 0 — всё прошло, 1 — нарушение, 2 — неверный вызов.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import pkgutil
import re
import ssl
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

#: `python3-*` из `Depends`/`Recommends` → модуль, который обязан быть в бандле.
PYTHON_MODULES: dict[str, tuple[str, ...]] = {
    "python3": (),
    "python3-pyqt5": ("PyQt5.QtCore", "PyQt5.QtGui", "PyQt5.QtWidgets", "PyQt5.QtDBus"),
    "python3-pyqt5.qtquick": ("PyQt5.QtQml", "PyQt5.QtQuick"),
    "python3-numpy": ("numpy",),
    "python3-xlib": ("Xlib",),
    "python3-requests": ("requests",),
    "python3-jsonschema": ("jsonschema",),
}
#: Python-зависимости `.deb`, которых в бандле нет намеренно: их тянет только
#: `onnxruntime` для утилит (квантизация, символьный вывод форм); рантайму
#: распознавания они не нужны (спайк 28.09: `import onnxruntime, onnx_asr` без них).
NOT_BUNDLED_PYTHON: dict[str, str] = {
    "python3-protobuf": "утилиты onnxruntime",
    "python3-flatbuffers": "утилиты onnxruntime",
    "python3-sympy": "утилиты onnxruntime",
    "python3-packaging": "утилиты onnxruntime",
}
#: Остальные зависимости `.deb`: в бандле их нет намеренно — берутся с хоста или уже внутри.
HOST_PACKAGES: dict[str, str] = {
    "misc:Depends": "подстановка debhelper",
    "shlibs:Depends": "подстановка debhelper",
    "qml-module-qtquick2": "QML — в бандле из PyQt5-Qt5",
    "qml-module-qtquick-controls2": "QML — в бандле из PyQt5-Qt5",
    "qml-module-qtquick-layouts": "QML — в бандле из PyQt5-Qt5",
    "qml-module-qtquick-window2": "QML — в бандле из PyQt5-Qt5",
    "qml-module-qtquick-shapes": "QML — в бандле из PyQt5-Qt5",
    "qml-module-qtgraphicaleffects": "QML — в бандле из PyQt5-Qt5",
    "libqt5svg5": "Qt Svg — в бандле из PyQt5-Qt5",
    "pulseaudio-utils": "pactl хоста",
    "gpgv": "проверка подписи — хостовый /usr/bin/gpgv",
    "fonts-pt-root-ui": "шрифт хоста",
    "fonts-pt-mono": "шрифт хоста",
    "polkit-kde-agent-1": "агент polkit хоста",
    "pipewire-pulse": "звуковой сервер хоста",
    "pulseaudio": "звуковой сервер хоста",
    "wireplumber": "звуковой сервер хоста",
}
#: Модули бандла сверх `control`, без которых AppImage не работает.
EXTRA_MODULES = ("onnxruntime", "onnx_asr", "ssl", "certifi")

_DEP_NAME_RE = re.compile(r"\$\{([^}]+)\}|([a-z0-9][a-z0-9+.\-]*)")


class Collector(logging.Handler):
    """Копит записи уровня WARNING и выше за время проверки."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def control_packages(control: str, fields: Iterable[str] = ("Depends", "Recommends")) -> list[str]:
    """Имена пакетов из полей `control` (с продолжениями строк, альтернативы — все)."""
    wanted = set(fields)
    values: dict[str, str] = {}
    current: str | None = None
    for line in control.splitlines():
        if line[:1] in (" ", "\t") and current is not None:
            values[current] += " " + line.strip()
            continue
        current = None
        name, sep, value = line.partition(":")
        if sep and name in wanted:
            current = name
            values[name] = values.get(name, "") + " " + value.strip()
    result: list[str] = []
    for field in fields:
        for item in values.get(field, "").split(","):
            for alternative in item.split("|"):
                match = _DEP_NAME_RE.search(alternative.strip())
                if match is None:
                    continue
                name = match.group(1) or match.group(2)
                if name not in result:
                    result.append(name)
    return result


def required_modules(packages: Iterable[str]) -> tuple[list[str], list[str]]:
    """Модули для импорта и список неизвестных зависимостей (ошибки таблицы)."""
    modules: list[str] = []
    unknown: list[str] = []
    for package in packages:
        if package in PYTHON_MODULES:
            modules.extend(m for m in PYTHON_MODULES[package] if m not in modules)
        elif package in HOST_PACKAGES or package in NOT_BUNDLED_PYTHON:
            continue
        else:
            unknown.append(package)
    return modules, unknown


def outside(path: str | None, appdir: Path) -> bool:
    """Путь не лежит внутри AppDir (встроенные модули без файла — внутри)."""
    if path is None:
        return False
    return not Path(path).resolve().is_relative_to(appdir)


def check_sys_path(appdir: Path, entries: Iterable[str]) -> list[str]:
    return [
        f"sys.path вне бандла: {entry}" for entry in entries if entry and outside(entry, appdir)
    ]


def check_imports(modules: Iterable[str], appdir: Path) -> list[str]:
    problems: list[str] = []
    for name in modules:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — любой сбой импорта = провал гейта
            problems.append(f"{name}: не импортируется: {type(exc).__name__}: {exc}")
            continue
        origin = getattr(module, "__file__", None)
        if outside(origin, appdir):
            problems.append(f"{name}: загружен не из бандла: {origin}")
    return problems


def check_package_tree(lib: Path) -> tuple[int, list[str]]:
    """Импорт всех модулей `astra_voice` (как smoke-installed.sh)."""
    problems: list[str] = []

    def walk_error(name: str) -> None:
        problems.append(f"{name}: {sys.exception()!r}")

    checked = 0
    try:
        import astra_voice

        checked += 1
        for info in pkgutil.walk_packages(astra_voice.__path__, "astra_voice.", onerror=walk_error):
            checked += 1
            try:
                importlib.import_module(info.name)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{info.name}: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"astra_voice: {type(exc).__name__}: {exc}")
    else:
        if outside(astra_voice.__file__, lib):
            problems.append(f"astra_voice загружен не из {lib}: {astra_voice.__file__}")
    return checked, problems


def check_catalog() -> list[str]:
    """Каталог моделей грузится с обязательной проверкой по схеме (arch §11 п.3б)."""
    from astra_voice.core import paths
    from astra_voice.models import schema
    from astra_voice.models.catalog import load_builtin
    from astra_voice.security.verify import Verifier

    problems: list[str] = []
    try:
        schema.require_available()
    except schema.SchemaUnavailable as exc:
        problems.append(f"jsonschema: {exc}")
        return problems
    if not paths.install_kind().is_appimage:
        problems.append(f"install_kind() = {paths.install_kind().name}, ожидался трек AppImage")
    keyring = paths.data_dir_static() / "keys" / "release.gpg"
    with tempfile.TemporaryDirectory(prefix="check-bundle-") as tmp:
        try:
            catalog = load_builtin(
                Verifier("catalog", keyring=keyring),
                state_path=Path(tmp) / "catalog-state.json",
                require_schema=True,
            )
        except Exception as exc:  # noqa: BLE001
            problems.append(f"каталог: {type(exc).__name__}: {exc}")
        else:
            print(f"Каталог: {len(catalog.entries)} моделей, схема проверена")
    return problems


#: Пакет PyPI → модуль, если имена различаются.
PACKAGE_MODULES = {"attrs": "attr"}


def split_allowed(problems: list[str], packages: Iterable[str]) -> tuple[list[str], list[str]]:
    """Отделить нарушения про модули незакреплённых колёс (`модуль: …`) от остальных."""
    modules = {
        PACKAGE_MODULES.get(name.lower(), name.lower().replace("-", "_")) for name in packages
    }
    allowed = [p for p in problems if p.split(":", 1)[0] in modules]
    return [p for p in problems if p not in allowed], allowed


def run(appdir: Path, control: Path, allow_missing: Iterable[str] = ()) -> int:
    appdir = appdir.resolve()
    lib = appdir / "usr" / "lib" / "astra-voice"
    collector = Collector()
    root_logger = logging.getLogger()
    root_logger.addHandler(collector)
    if root_logger.level > logging.WARNING or root_logger.level == logging.NOTSET:
        root_logger.setLevel(logging.WARNING)

    sys.path.insert(0, str(lib))
    problems = check_sys_path(appdir, sys.path)
    packages = control_packages(control.read_text(encoding="utf-8"))
    modules, unknown = required_modules(packages)
    problems += [f"зависимость {name} не описана в check_bundle.py" for name in unknown]
    problems += check_imports([*modules, *EXTRA_MODULES], appdir)
    checked, tree = check_package_tree(lib)
    problems += tree
    problems += check_catalog()

    print(f"OpenSSL: {ssl.OPENSSL_VERSION}")
    print(f"Модули зависимостей: {len(modules) + len(EXTRA_MODULES)}, astra_voice: {checked}")
    root_logger.removeHandler(collector)
    for record in collector.records:
        problems.append(f"журнал {record.levelname}: {record.name}: {record.getMessage()}")
    problems, skipped = split_allowed(problems, allow_missing)
    for problem in skipped:
        print(f"ПРОПУЩЕНО (TODO-HASH, образ не для выпуска): {problem}", file=sys.stderr)
    for problem in problems:
        print(f"ОШИБКА: {problem}", file=sys.stderr)
    print(f"check_bundle: {'OK' if not problems else f'{len(problems)} нарушений'}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="гейт состава бандла AppImage")
    parser.add_argument("--appdir", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument(
        "--allow-missing",
        action="append",
        default=[],
        metavar="ПАКЕТ",
        help="только локально: колесо из строки TODO-HASH lock",
    )
    args = parser.parse_args(argv)
    if not (args.appdir / ".astra-voice-build").is_file():
        print(f"нет маркера сборки в {args.appdir}", file=sys.stderr)
        return 2
    return run(args.appdir, args.control, args.allow_missing)


if __name__ == "__main__":
    raise SystemExit(main())
