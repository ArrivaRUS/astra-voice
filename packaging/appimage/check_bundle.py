#!/usr/bin/env python3
"""Гейт состава бандла AppImage (arch/appimage.md §9.1, §11 п.3, R3.4; T-169, T-184, T-186).

Запускается бандловым интерпретатором в изоляции (`build.sh`):

    <AppDir>/opt/python3.11/bin/python3.11 -I check_bundle.py \\
        --appdir <AppDir> --control packaging/debian/control \\
        --openssl-major 3 --openssl-origin host

Проверяет:

1. `sys.path` и префиксы интерпретатора — только внутри AppDir; `sitecustomize` не с хоста;
2. каждый модуль Python из `Depends` и `Recommends` пакета `.deb` (таблица «пакет →
   модуль» ниже) импортируется и лежит внутри AppDir; неизвестный `python3-*` —
   ошибка (таблицу обновляют вместе с `control`), не-Python зависимости — только
   из списка хостовых исключений;
3. все модули `astra_voice` импортируются (как `packaging/smoke-installed.sh`);
4. `schema.require_available()` и `load_builtin(..., require_schema=True)` проходят —
   каталог грузится с проверкой по схеме, подпись — хостовым `/usr/bin/gpgv`;
5. за всё время проверки — ни одной записи журнала уровня WARNING и выше;
6. OpenSSL (значения из lock): старшая версия равна `--openssl-major`; `_ssl`/`_hashlib` —
   из бандла; libssl/libcrypto фактически загружены (`/proc/self/maps`) с хоста из
   системных каталогов (`--openssl-origin host`) или из бандла (`bundled`, откат (г));
7. в AppDir нет readline/gdbm/`_dbm`/libdb, а при OpenSSL с хоста — своих libssl/libcrypto;
   нет абсолютных и выходящих из AppDir ссылок;
8. каждый DT_NEEDED каждого ELF разрешается внутри AppDir по RUNPATH/RPATH или относится к
   манифесту хостовых библиотек `HOST_SONAMES` и есть на машине проверки.

`--allow-missing <пакет>` (только `build.sh` при локальной сборке с
`ASTRA_VOICE_APPIMAGE_ALLOW_TODO_HASH=1`): нарушения про модуль незакреплённого колеса
(строка `# TODO-HASH` в lock) печатаются как «пропущено», любые другие валят гейт.

Код возврата 0 — всё прошло, 1 — нарушение, 2 — неверный вызов.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib
import logging
import os
import pkgutil
import re
import ssl
import struct
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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

#: Каталоги библиотек хоста (Debian/ALSE multiarch и классические).
HOST_LIB_DIRS = (
    "/lib/x86_64-linux-gnu",
    "/usr/lib/x86_64-linux-gnu",
    "/lib64",
    "/usr/lib64",
    "/lib",
    "/usr/lib",
)


class ElfError(ValueError):
    """Файл похож на ELF, но разобрать его нельзя."""


@dataclass(frozen=True)
class Dynamic:
    """Динамическая секция ELF: DT_NEEDED, DT_RPATH и DT_RUNPATH (как записаны, с $ORIGIN)."""

    needed: tuple[str, ...]
    rpath: tuple[str, ...] = ()
    runpath: tuple[str, ...] = ()


def read_dynamic(path: Path) -> Dynamic | None:
    """DT_NEEDED и RUNPATH объекта ELF64 LE; None — не ELF. Без readelf: гейт идёт в бандле."""
    with path.open("rb") as fh:
        header = fh.read(64)
        if header[:4] != b"\x7fELF":
            return None
        if len(header) < 64 or header[4] != 2 or header[5] != 1:
            raise ElfError(f"{path}: не ELF64 little-endian")
        (e_type,) = struct.unpack_from("<H", header, 16)
        if e_type not in (2, 3):  # не ET_EXEC/ET_DYN: объектный файл, загрузчику не нужен
            return Dynamic(())
        (phoff,) = struct.unpack_from("<Q", header, 32)
        phentsize, phnum = struct.unpack_from("<HH", header, 54)
        fh.seek(phoff)
        table = fh.read(phentsize * phnum)
        if len(table) != phentsize * phnum or phentsize < 56:
            raise ElfError(f"{path}: обрезана таблица программных заголовков")
        loads: list[tuple[int, int, int]] = []
        dynamic: tuple[int, int] | None = None
        for i in range(phnum):
            p_type, _flags, offset, vaddr, _paddr, filesz = struct.unpack_from(
                "<IIQQQQ", table, i * phentsize
            )
            if p_type == 1:  # PT_LOAD
                loads.append((vaddr, offset, filesz))
            elif p_type == 2:  # PT_DYNAMIC
                dynamic = (offset, filesz)
        if dynamic is None:
            return Dynamic(())
        fh.seek(dynamic[0])
        raw = fh.read(dynamic[1])
        needed: list[int] = []
        tags: dict[int, int] = {}
        for tag, value in struct.iter_unpack("<qQ", raw[: len(raw) // 16 * 16]):
            if tag == 0:  # DT_NULL
                break
            if tag == 1:  # DT_NEEDED
                needed.append(value)
            elif tag in (5, 10, 15, 29):  # DT_STRTAB, DT_STRSZ, DT_RPATH, DT_RUNPATH
                tags[tag] = value
        if 5 not in tags or 10 not in tags:
            raise ElfError(f"{path}: нет DT_STRTAB/DT_STRSZ")
        for vaddr, offset, filesz in loads:
            if vaddr <= tags[5] < vaddr + filesz:
                fh.seek(offset + tags[5] - vaddr)
                break
        else:
            raise ElfError(f"{path}: DT_STRTAB вне сегментов PT_LOAD")
        strtab = fh.read(tags[10])

    def string(offset: int) -> str:
        end = strtab.find(b"\0", offset)
        if offset >= len(strtab) or end < 0:
            raise ElfError(f"{path}: строка вне DT_STRTAB")
        return strtab[offset:end].decode("utf-8", "surrogateescape")

    def paths(tag: int) -> tuple[str, ...]:
        return tuple(p for p in string(tags[tag]).split(":") if p) if tag in tags else ()

    return Dynamic(tuple(string(offset) for offset in needed), paths(15), paths(29))


#: Библиотеки, которые AppImage берёт с хоста (R3.2, «манифест внешних зависимостей»): SONAME →
#: пакет ALSE 1.8/Debian 12. Всё прочее из DT_NEEDED обязано найтись внутри AppDir. В списке
#: намеренно нет readline/gdbm/libdb/ncurses/nsl/tirpc: модули, которым они нужны, удалены.
HOST_SONAMES: dict[str, str] = {
    # glibc и загрузчик
    "ld-linux-x86-64.so.2": "libc6",
    "libc.so.6": "libc6",
    "libm.so.6": "libc6",
    "libpthread.so.0": "libc6",
    "libdl.so.2": "libc6",
    "librt.so.1": "libc6",
    "libutil.so.1": "libc6",
    # рантайм C++
    "libstdc++.so.6": "libstdc++6",
    "libgcc_s.so.1": "libgcc-s1",
    # интерпретатор Debian и его модули (R3.2): базовые библиотеки любой ALSE 1.8
    "libz.so.1": "zlib1g",
    "libexpat.so.1": "libexpat1",
    "libbz2.so.1.0": "libbz2-1.0",
    "liblzma.so.5": "liblzma5",
    "libffi.so.8": "libffi8",
    "libsqlite3.so.0": "libsqlite3-0",
    "libcrypt.so.1": "libcrypt1",
    "libuuid.so.1": "libuuid1",
    # OpenSSL — только с хоста (R3: в образ не кладётся, TLS обновляет ОС)
    "libssl.so.3": "libssl3",
    "libcrypto.so.3": "libssl3",
    # графика, шрифты, шина — прежний контракт Qt из PyQt5-Qt5
    "libGL.so.1": "libgl1",
    "libX11.so.6": "libx11-6",
    "libX11-xcb.so.1": "libx11-xcb1",
    "libXext.so.6": "libxext6",
    "libxcb.so.1": "libxcb1",
    "libxcb-glx.so.0": "libxcb-glx0",
    "libxcb-icccm.so.4": "libxcb-icccm4",
    "libxcb-image.so.0": "libxcb-image0",
    "libxcb-keysyms.so.1": "libxcb-keysyms1",
    "libxcb-randr.so.0": "libxcb-randr0",
    "libxcb-render.so.0": "libxcb-render0",
    "libxcb-render-util.so.0": "libxcb-render-util0",
    "libxcb-shape.so.0": "libxcb-shape0",
    "libxcb-shm.so.0": "libxcb-shm0",
    "libxcb-sync.so.1": "libxcb-sync1",
    "libxcb-xfixes.so.0": "libxcb-xfixes0",
    "libxcb-xinerama.so.0": "libxcb-xinerama0",
    "libxcb-xkb.so.1": "libxcb-xkb1",
    "libxkbcommon.so.0": "libxkbcommon0",
    "libxkbcommon-x11.so.0": "libxkbcommon-x11-0",
    "libfontconfig.so.1": "libfontconfig1",
    "libfreetype.so.6": "libfreetype6",
    "libglib-2.0.so.0": "libglib2.0-0",
    "libgthread-2.0.so.0": "libglib2.0-0",
    "libdbus-1.so.3": "libdbus-1-3",
    "libgssapi_krb5.so.2": "libgssapi-krb5-2",
}


def _expand(entries: Iterable[str], origin: Path) -> tuple[Path, ...] | None:
    """Пути поиска с подстановкой $ORIGIN; None — неподдерживаемая подстановка ($LIB…)."""
    result: list[Path] = []
    for entry in entries:
        value = entry.replace("${ORIGIN}", str(origin)).replace("$ORIGIN", str(origin))
        if "$" in value:
            return None
        result.append(Path(value))
    return tuple(result)


def check_needed(
    appdir: Path,
    host_sonames: Mapping[str, str] = HOST_SONAMES,
    host_dirs: Sequence[str] = HOST_LIB_DIRS,
) -> tuple[int, list[str]]:
    """Каждый DT_NEEDED каждого ELF в AppDir разрешается (T-186).

    Модель загрузчика glibc: у объекта с DT_RUNPATH — только он; без него — DT_RPATH самого
    объекта и цепочки загрузивших его объектов (так numpy.libs находит libgfortran). Обход —
    от корней: ELF, чьё имя не значится ничьим DT_NEEDED.
    Найденное по путям поиска обязано лежать внутри AppDir; не найденное — SONAME из
    `host_sonames`, присутствующий на хосте проверки. Иначе — висячая зависимость.
    """
    appdir = appdir.resolve()
    infos: dict[Path, Dynamic] = {}
    problems: list[str] = []
    for path in sorted(appdir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            info = read_dynamic(path)
        except (OSError, ElfError) as exc:
            problems.append(f"ELF не читается: {exc}")
            continue
        if info is not None:
            infos[path] = info
    seen: set[tuple[Path, tuple[Path, ...]]] = set()
    host_cache: dict[str, bool] = {}

    def on_host(soname: str) -> bool:
        if soname not in host_cache:
            host_cache[soname] = any(os.path.exists(os.path.join(d, soname)) for d in host_dirs)
        return host_cache[soname]

    def visit(path: Path, inherited: tuple[Path, ...]) -> None:
        if (path, inherited) in seen:
            return
        seen.add((path, inherited))
        info = infos[path]
        rel = path.relative_to(appdir)
        runpath = _expand(info.runpath, path.parent)
        rpath = _expand(() if info.runpath else info.rpath, path.parent)
        if runpath is None or rpath is None:
            problems.append(f"{rel}: неподдерживаемая подстановка в RPATH/RUNPATH")
            return
        chain = rpath + inherited
        search = (() if info.runpath else chain) + runpath
        for soname in info.needed:
            found = next((d / soname for d in search if (d / soname).exists()), None)
            if found is not None:
                target = found.resolve()
                if not target.is_relative_to(appdir):
                    problems.append(f"{rel}: {soname} найден вне AppDir: {found}")
                elif target in infos:
                    visit(target, chain)
            elif soname in host_sonames:
                if not on_host(soname):
                    problems.append(f"{rel}: хостовая {soname} не найдена на машине проверки")
            else:
                problems.append(f"{rel}: висячий DT_NEEDED {soname}")

    # Корни — то, что грузят по пути (интерпретатор, модули Python, плагины Qt): ELF, чьё имя
    # никто в AppDir не требует. Зависимости проверяются в контексте загрузившей их цепочки.
    wanted = {soname for info in infos.values() for soname in info.needed}
    for path in infos:
        if path.name not in wanted:
            visit(path, ())
    return len(infos), sorted(set(problems))


#: Файлы, которых не должно быть в AppDir при любой базе (R3.2): readline, gdbm, dbm, libdb.
FORBIDDEN_ALWAYS = (
    "libreadline.so*",
    "readline.cpython-*.so",
    "libgdbm.so*",
    "libgdbm_compat.so*",
    "_gdbm.cpython-*.so",
    "_dbm.cpython-*.so",
    "libdb-*.so",
    "libdb.so*",
)
#: При OpenSSL с хоста в AppDir нет своих libssl/libcrypto.
FORBIDDEN_HOST_OPENSSL = ("libssl.so*", "libcrypto.so*")


def check_forbidden(appdir: Path, openssl_origin: str) -> list[str]:
    patterns = FORBIDDEN_ALWAYS + (FORBIDDEN_HOST_OPENSSL if openssl_origin == "host" else ())
    problems: list[str] = []
    for path in sorted(appdir.rglob("*")):
        if any(fnmatch.fnmatchcase(path.name, pattern) for pattern in patterns):
            problems.append(f"запрещённый файл в AppDir: {path.relative_to(appdir)}")
    return problems


def check_symlinks(appdir: Path) -> list[str]:
    """Нет абсолютных ссылок и ссылок, выходящих из AppDir (хостовый sitecustomize, R3.2)."""
    appdir = appdir.resolve()
    problems: list[str] = []
    for path in sorted(appdir.rglob("*")):
        if not path.is_symlink():
            continue
        target = os.readlink(path)
        rel = path.relative_to(appdir)
        if os.path.isabs(target):
            problems.append(f"абсолютная ссылка {rel} -> {target}")
        elif not Path(os.path.normpath(path.parent / target)).is_relative_to(appdir):
            problems.append(f"ссылка {rel} -> {target} выходит из AppDir")
    return problems


_MAPS_LIB_RE = re.compile(r"lib(ssl|crypto)\.so(\.[0-9.]+)?")


def loaded_openssl(maps: str) -> dict[str, set[str]]:
    """Пути libssl/libcrypto из текста /proc/self/maps: имя → множество путей."""
    found: dict[str, set[str]] = {}
    for line in maps.splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) < 6:
            continue
        path = parts[5].removesuffix(" (deleted)")
        name = os.path.basename(path)
        if _MAPS_LIB_RE.fullmatch(name):
            found.setdefault(name, set()).add(path)
    return found


def check_openssl(
    appdir: Path,
    major: int,
    origin: str,
    version_info: Sequence[int],
    maps: str,
    module_files: Mapping[str, str | None],
    host_dirs: Sequence[str] = HOST_LIB_DIRS,
) -> list[str]:
    """OpenSSL нужной старшей версии, `_ssl`/`_hashlib` из бандла, libssl/libcrypto — откуда
    велено lock: `host` — из системных каталогов вне AppDir, `bundled` — из AppDir (T-184)."""
    appdir = appdir.resolve()
    problems: list[str] = []
    if not version_info or version_info[0] != major:
        problems.append(f"OpenSSL {tuple(version_info)}: ожидалась старшая версия {major}")
    for name, file in module_files.items():
        if file is None or outside(file, appdir):
            problems.append(f"{name}: модуль не из бандла: {file}")
    loaded = loaded_openssl(maps)
    wanted = [f"libssl.so.{major}", f"libcrypto.so.{major}"] if origin == "host" else []
    for name in wanted:
        if name not in loaded:
            problems.append(f"{name} не загружена в процесс (нет в /proc/self/maps)")
    allowed = {os.path.realpath(d) for d in host_dirs}
    for name, paths in sorted(loaded.items()):
        for path in sorted(paths):
            inside = not outside(path, appdir)
            if origin == "host" and inside:
                problems.append(f"{name} загружена из бандла: {path}")
            elif origin == "host" and os.path.realpath(os.path.dirname(path)) not in allowed:
                problems.append(f"{name} загружена не из системного каталога: {path}")
            elif origin == "bundled" and not inside:
                problems.append(f"{name} загружена не из бандла: {path}")
    return problems


def check_prefixes(appdir: Path) -> list[str]:
    """Префиксы интерпретатора внутри AppDir; хостовый sitecustomize не подключён (R3.2)."""
    appdir = appdir.resolve()
    problems = [
        f"sys.{name} вне бандла: {getattr(sys, name)}"
        for name in ("prefix", "exec_prefix", "base_prefix", "base_exec_prefix")
        if outside(getattr(sys, name), appdir)
    ]
    for name in ("sitecustomize", "usercustomize"):
        module = sys.modules.get(name)
        if module is not None and outside(getattr(module, "__file__", None), appdir):
            problems.append(f"{name} подключён не из бандла: {getattr(module, '__file__', None)}")
    return problems


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
        problems.append(f"jsonschema: SchemaUnavailable: {exc}")
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


#: Какие нарушения про модуль незакреплённого колеса прощаются: его отсутствие и
#: вытекающая из него недоступность проверки схемы. «Загружен не из бандла» и прочее — нет.
ALLOWED_KINDS = (
    "не импортируется: ModuleNotFoundError",
    "SchemaUnavailable: Модуль jsonschema недоступен.",
)


def split_allowed(problems: list[str], packages: Iterable[str]) -> tuple[list[str], list[str]]:
    """Отделить отсутствие модулей незакреплённых колёс от остальных нарушений."""
    modules = {
        PACKAGE_MODULES.get(name.lower(), name.lower().replace("-", "_")) for name in packages
    }
    prefixes = tuple(f"{module}: {kind}" for module in modules for kind in ALLOWED_KINDS)
    allowed = [p for p in problems if p.startswith(prefixes)]
    return [p for p in problems if p not in allowed], allowed


def run(
    appdir: Path,
    control: Path,
    allow_missing: Iterable[str] = (),
    openssl_major: int = 3,
    openssl_origin: str = "host",
) -> int:
    appdir = appdir.resolve()
    lib = appdir / "usr" / "lib" / "astra-voice"
    collector = Collector()
    root_logger = logging.getLogger()
    root_logger.addHandler(collector)
    if root_logger.level > logging.WARNING or root_logger.level == logging.NOTSET:
        root_logger.setLevel(logging.WARNING)

    sys.path.insert(0, str(lib))
    problems = check_sys_path(appdir, sys.path) + check_prefixes(appdir)
    packages = control_packages(control.read_text(encoding="utf-8"))
    modules, unknown = required_modules(packages)
    problems += [f"зависимость {name} не описана в check_bundle.py" for name in unknown]
    problems += check_imports([*modules, *EXTRA_MODULES], appdir)
    checked, tree = check_package_tree(lib)
    problems += tree
    problems += check_catalog()

    # Настоящая работа с OpenSSL до чтения карты памяти: хэш через _hashlib и TLS-контекст.
    hashlib.new("sha256", b"astra-voice").hexdigest()
    ssl.create_default_context()
    module_files = {
        name: getattr(importlib.import_module(name), "__file__", None)
        for name in ("_ssl", "_hashlib")
    }
    maps = Path("/proc/self/maps").read_text(encoding="utf-8", errors="replace")
    problems += check_openssl(
        appdir, openssl_major, openssl_origin, ssl.OPENSSL_VERSION_INFO, maps, module_files
    )
    problems += check_forbidden(appdir, openssl_origin)
    problems += check_symlinks(appdir)
    elf_count, needed = check_needed(appdir)
    problems += needed

    print(f"OpenSSL: {ssl.OPENSSL_VERSION} ({openssl_origin})")
    for name, paths in sorted(loaded_openssl(maps).items()):
        print(f"  {name}: {', '.join(sorted(paths))}")
    print(f"Модули зависимостей: {len(modules) + len(EXTRA_MODULES)}, astra_voice: {checked}")
    print(f"ELF: {elf_count}, DT_NEEDED проверены")
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
    parser.add_argument("--openssl-major", type=int, required=True, help="из lock")
    parser.add_argument(
        "--openssl-origin", choices=("host", "bundled"), required=True, help="из lock"
    )
    args = parser.parse_args(argv)
    if not (args.appdir / ".astra-voice-build").is_file():
        print(f"нет маркера сборки в {args.appdir}", file=sys.stderr)
        return 2
    return run(
        args.appdir, args.control, args.allow_missing, args.openssl_major, args.openssl_origin
    )


if __name__ == "__main__":
    raise SystemExit(main())
