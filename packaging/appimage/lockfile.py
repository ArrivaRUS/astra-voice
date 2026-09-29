#!/usr/bin/env python3
"""Разбор `packaging/appimage.lock` — единственного источника закреплённых входов AppImage.

Формат (arch/appimage.md §9.1–§9.2, «Ревизия 3» R3.3; T1 MN-9):

* pip-требования с `--hash=sha256:` — колёса ставятся `pip install --require-hashes`;
* `# tool: <файл> <sha256> <размер> <url>` — инструменты сборки: всегда runtime, его
  подпись и appimagetool; Python AppImage — только при `base: python-appimage` (откат (г));
* директивы `# <ключ>: <значение>`:
  - `base` — база интерпретатора: `debian12` (R3, вариант (а′)) или `python-appimage`;
  - `openssl-origin` — `host` (debian12: libssl/libcrypto с хоста) или `bundled`;
  - `openssl-major` — ожидаемая старшая версия OpenSSL (гейт `check_bundle.py`);
  - `expect-elf`, `max-glibc` — гейт `tools/elf-audit` (MN-9);
  - `runtime-key` — отпечаток ключа подписи `runtime-x86_64.sig`;
  - `build-python`, `build-pip` (обязательны при debian12) — версии Python и pip машины
    сборки, которыми ставятся колёса (`pip --target`); иная версия — отказ сборки;
* однострочные JSON-записи Debian-входов (R3.3), только при `base: debian12`:
  - `# archive: {"id","keyring","signer","release","sha256","size","codename","suite"}` —
    подписанный `InRelease`; `keyring` — путь в репозитории, `signer` — отпечаток основного
    ключа; `codename`/`suite` сверяются с подписанным текстом;
  - `# index: {"id","archive","kind","file","size","sha256","url"}` — `Packages`/`Sources`
    из `InRelease` (`file` — путь как в разделе SHA256 `InRelease`);
  - `# deb: {"package","version","arch","file","size","sha256","url","index","source",
    "source_version","dsc"}` — бинарный пакет из индекса `Packages`;
  - `# source: {"id","file","size","sha256","url"[,"index"|"dsc"][,"git_commit","git_tree"
    [,"git_delta"]]}` — исходник: `.dsc` из индекса `Sources` (`index`), архив из `.dsc`
    (`dsc`) или самостоятельный файл; архив GitHub по коммиту — ещё и дерево git (байты
    архивов GitHub не стабильны, дерево — стабильно; `git_delta` — export-ignore/subst);
* `# host-lib: <SONAME> <пакет Debian/ALSE>` — манифест внешних зависимостей (R3.2): библиотека
  берётся с хоста и в образ не кладётся. Читают check_bundle.py (каждый DT_NEEDED вне AppDir —
  только из манифеста, каждая запись нужна и есть на хосте) и sbom.py (origin=host, not-bundled);
* `# TODO-HASH: <имя>==<версия> …` — колесо решено добавить, но хэш ещё не закреплён.
  Сборка с такой строкой — только локальная проверка;
* `# TODO-PIN: <что> …` — вход Debian/исходника ещё не закреплён. Формат проходит, но
  сборка с такой строкой невозможна (`build.sh` падает), обходного флага нет.

    lockfile.py packaging/appimage.lock check          # формат; код 1 при ошибке
    lockfile.py packaging/appimage.lock get base       # значение директивы
    lockfile.py packaging/appimage.lock tools          # строки «файл sha256 размер url»
    lockfile.py packaging/appimage.lock fetch          # «путь-в-кэше sha256 размер url» всех входов
    lockfile.py packaging/appimage.lock debs           # «пакет путь-в-кэше» бинарных пакетов
    lockfile.py packaging/appimage.lock host-libs      # «SONAME пакет» манифеста хоста
    lockfile.py packaging/appimage.lock todo           # незакреплённые колёса, по строке
    lockfile.py packaging/appimage.lock todo-pin       # незакреплённые входы, по строке
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, TypeVar
from urllib.parse import unquote

#: Инструменты, без которых сборка невозможна (имена файлов из `# tool:`).
REQUIRED_TOOLS = ("runtime-x86_64", "runtime-x86_64.sig", "appimagetool-x86_64.AppImage")
PYTHON_TOOL_RE = re.compile(r"python3\.11\.\d+-cp311-cp311-manylinux_2_\d+_x86_64\.AppImage")
BASES = ("debian12", "python-appimage")
#: Три пакета интерпретатора Debian 12 (R3.2): одна точная версия и один исходник.
PYTHON_DEBS = ("python3.11-minimal", "libpython3.11-minimal", "libpython3.11-stdlib")
PYTHON_SOURCE = "python3.11"
DEB_ARCH = "amd64"
DIRECTIVES = (
    "base",
    "openssl-origin",
    "openssl-major",
    "expect-elf",
    "max-glibc",
    "runtime-key",
)
#: Сборочные Python и pip (base debian12: колёса ставит pip машины сборки — ревью P3-6).
BUILD_DIRECTIVES = ("build-python", "build-pip")
#: Каталоги кэша (R3.3): инструменты, подписанные индексы, пакеты, исходники.
CACHE_DIRS = {"tool": "downloads", "metadata": "metadata", "deb": "debs", "source": "sources"}

_TOOL_RE = re.compile(
    r"#\s*tool:\s+(?P<name>[A-Za-z0-9._+-]+)\s+(?P<sha>[0-9a-f]{64})\s+(?P<size>[0-9]+)"
    r"\s+(?P<url>https://\S+)\s*"
)
_DIRECTIVE_RE = re.compile(r"#\s*(?P<key>[a-z-]+):\s*(?P<value>\S+)\s*")
_RECORD_RE = re.compile(r"#\s*(?P<kind>archive|index|deb|source):\s*(?P<json>\S.*)")
_TODO_RE = re.compile(r"#\s*TODO-HASH:\s*(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s]+)(?:\s.*)?")
_HOST_LIB_RE = re.compile(
    r"#\s*host-lib:\s+(?P<soname>[A-Za-z0-9._+-]+\.so[A-Za-z0-9._+-]*)"
    r"\s+(?P<package>[a-z0-9][a-z0-9.+-]+)\s*"
)
_TODO_PIN_RE = re.compile(r"#\s*TODO-PIN:\s*(?P<what>\S.*)")
_REQ_RE = re.compile(r"(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s\\]+)")
_HASH_RE = re.compile(r"--hash=sha256:(?P<sha>[0-9a-f]{64})")
_VALUE_RE = {
    "base": re.compile("|".join(BASES)),
    "openssl-origin": re.compile(r"host|bundled"),
    "openssl-major": re.compile(r"[0-9]+"),
    "expect-elf": re.compile(r"[0-9]+"),
    "max-glibc": re.compile(r"[0-9]+\.[0-9]+"),
    "runtime-key": re.compile(r"[0-9A-F]{40}"),
    "build-python": re.compile(r"[0-9]+\.[0-9]+"),
    "build-pip": re.compile(r"[0-9]+\.[0-9]+(\.[0-9]+)?"),
}
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_FPR_RE = re.compile(r"[0-9A-F]{40}")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9.+-]*")
_PKG_RE = re.compile(r"[a-z0-9][a-z0-9.+-]+")
_VERSION_RE = re.compile(r"(?:[0-9]+:)?[0-9][A-Za-z0-9.+~-]*")
_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]*")

#: Поля JSON-записей: обязательные и необязательные; лишнее поле — ошибка.
_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "archive": (("id", "keyring", "signer", "release", "sha256", "size", "codename", "suite"), ()),
    "index": (("id", "archive", "kind", "file", "size", "sha256", "url"), ()),
    "deb": (
        (
            "package",
            "version",
            "arch",
            "file",
            "size",
            "sha256",
            "url",
            "index",
            "source",
            "source_version",
            "dsc",
        ),
        (),
    ),
    "source": (
        ("id", "file", "size", "sha256", "url"),
        ("index", "dsc", "git_commit", "git_tree", "git_delta"),
    ),
}


class LockError(ValueError):
    """Файл блокировки не соответствует формату."""


@dataclass(frozen=True)
class Tool:
    name: str
    sha256: str
    size: int
    url: str


@dataclass(frozen=True)
class Requirement:
    name: str
    version: str
    hashes: tuple[str, ...]


@dataclass(frozen=True)
class Archive:
    id: str
    keyring: str
    signer: str
    release: str
    sha256: str
    size: int
    codename: str
    suite: str

    @property
    def cache_path(self) -> str:
        return f"{CACHE_DIRS['metadata']}/{self.id}/InRelease"


@dataclass(frozen=True)
class Index:
    id: str
    archive: str
    kind: str
    file: str
    size: int
    sha256: str
    url: str

    @property
    def cache_path(self) -> str:
        return f"{CACHE_DIRS['metadata']}/{self.archive}/{self.file}"


@dataclass(frozen=True)
class Deb:
    package: str
    version: str
    arch: str
    file: str
    size: int
    sha256: str
    url: str
    index: str
    source: str
    source_version: str
    dsc: str

    @property
    def cache_path(self) -> str:
        return f"{CACHE_DIRS['deb']}/{self.file}"


@dataclass(frozen=True)
class Source:
    id: str
    file: str
    size: int
    sha256: str
    url: str
    index: str | None = None
    dsc: str | None = None
    git_commit: str | None = None
    git_tree: str | None = None
    git_delta: str | None = None

    @property
    def cache_path(self) -> str:
        return f"{CACHE_DIRS['source']}/{self.file}"


@dataclass
class Lock:
    tools: list[Tool] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    directives: dict[str, str] = field(default_factory=dict)
    todo: list[tuple[str, str]] = field(default_factory=list)
    todo_pin: list[str] = field(default_factory=list)
    archives: list[Archive] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    debs: list[Deb] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    host_libs: dict[str, str] = field(default_factory=dict)

    @property
    def base(self) -> str:
        return self.directives["base"]

    @property
    def openssl_major(self) -> int:
        return int(self.directives["openssl-major"])

    @property
    def expect_elf(self) -> int:
        return int(self.directives["expect-elf"])

    @property
    def max_glibc(self) -> tuple[int, int]:
        major, minor = self.directives["max-glibc"].split(".")
        return int(major), int(minor)

    @property
    def runtime_key(self) -> str:
        return self.directives["runtime-key"]

    def tool(self, name: str) -> Tool:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise LockError(f"нет инструмента {name}")

    @property
    def python_tool(self) -> Tool:
        for tool in self.tools:
            if PYTHON_TOOL_RE.fullmatch(tool.name):
                return tool
        raise LockError("нет инструмента python3.11 AppImage")

    def archive(self, ident: str) -> Archive:
        return _by_id(self.archives, ident, "archive")

    def index(self, ident: str) -> Index:
        return _by_id(self.indexes, ident, "index")

    def source(self, ident: str) -> Source:
        return _by_id(self.sources, ident, "source")

    def fetch_list(self) -> list[tuple[str, str, int, str]]:
        """Все входы для `build.sh --fetch`: путь в кэше, sha256, размер, URL."""
        items = [(f"{CACHE_DIRS['tool']}/{t.name}", t.sha256, t.size, t.url) for t in self.tools]
        items += [(a.cache_path, a.sha256, a.size, a.release) for a in self.archives]
        items += [(i.cache_path, i.sha256, i.size, i.url) for i in self.indexes]
        items += [(d.cache_path, d.sha256, d.size, d.url) for d in self.debs]
        items += [(s.cache_path, s.sha256, s.size, s.url) for s in self.sources]
        return items


class _HasId(Protocol):
    @property
    def id(self) -> str: ...


_T = TypeVar("_T", bound=_HasId)


def _by_id(items: list[_T], ident: str, kind: str) -> _T:
    for item in items:
        if item.id == ident:
            return item
    raise LockError(f"нет записи {kind} с id {ident}")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _upstream(version: str) -> str:
    """Версия без эпохи — так она входит в имя файла Debian."""
    return version.split(":", 1)[1] if ":" in version else version


def _check_url(kind: str, url: str, tail: str) -> None:
    if not url.startswith("https://") or any(ch.isspace() for ch in url):
        raise LockError(f"{kind}: URL не https: {url}")
    if not unquote(url).endswith("/" + tail):
        raise LockError(f"{kind}: URL {url} не оканчивается на {tail}")


def _check_relpath(kind: str, value: str) -> None:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value or value != str(path):
        raise LockError(f"{kind}: недопустимый путь {value!r}")


def _record(kind: str, raw: str) -> dict[str, Any]:
    """JSON-объект записи: ровно известные поля, строки и положительные целые."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LockError(f"{kind}: неверный JSON: {exc.msg}") from None
    if not isinstance(data, dict):
        raise LockError(f"{kind}: запись — не объект JSON")
    required, optional = _FIELDS[kind]
    missing = [name for name in required if name not in data]
    if missing:
        raise LockError(f"{kind}: нет полей {', '.join(missing)}")
    extra = sorted(set(data) - set(required) - set(optional))
    if extra:
        raise LockError(f"{kind}: неизвестные поля {', '.join(extra)}")
    for name, value in data.items():
        if name == "size":
            if type(value) is not int or value <= 0:
                raise LockError(f"{kind}: size — положительное целое, а не {value!r}")
        elif not isinstance(value, str) or not value:
            raise LockError(f"{kind}: {name} — непустая строка")
        elif name == "sha256" and not _SHA_RE.fullmatch(value):
            raise LockError(f"{kind}: неверный sha256 {value}")
    return data


def _add_record(lock: Lock, kind: str, raw: str) -> None:
    data = _record(kind, raw)
    if kind == "archive":
        archive = Archive(**data)
        if not _ID_RE.fullmatch(archive.id):
            raise LockError(f"archive: неверный id {archive.id}")
        _check_relpath("archive", archive.keyring)
        if not archive.keyring.startswith("packaging/appimage/keys/"):
            raise LockError(f"archive {archive.id}: keyring вне packaging/appimage/keys/")
        if not _FPR_RE.fullmatch(archive.signer):
            raise LockError(f"archive {archive.id}: signer — 40 hex в верхнем регистре")
        _check_url(f"archive {archive.id}", archive.release, "InRelease")
        lock.archives.append(archive)
    elif kind == "index":
        index = Index(**data)
        if not _ID_RE.fullmatch(index.id):
            raise LockError(f"index: неверный id {index.id}")
        if index.kind not in ("Packages", "Sources"):
            raise LockError(f"index {index.id}: kind — Packages или Sources")
        _check_relpath(f"index {index.id}", index.file)
        if PurePosixPath(index.file).name not in (index.kind, index.kind + ".xz"):
            raise LockError(f"index {index.id}: файл {index.file} не {index.kind}[.xz]")
        _check_url(f"index {index.id}", index.url, index.file)
        lock.indexes.append(index)
    elif kind == "deb":
        deb = Deb(**data)
        for name in ("package", "source"):
            if not _PKG_RE.fullmatch(getattr(deb, name)):
                raise LockError(f"deb: неверное имя {name} {getattr(deb, name)}")
        for name in ("version", "source_version"):
            if not _VERSION_RE.fullmatch(getattr(deb, name)):
                raise LockError(f"deb {deb.package}: неверная {name} {getattr(deb, name)}")
        expected = f"{deb.package}_{_upstream(deb.version)}_{deb.arch}.deb"
        if deb.file != expected:
            raise LockError(f"deb {deb.package}: файл {deb.file}, ожидался {expected}")
        _check_url(f"deb {deb.package}", deb.url, deb.file)
        lock.debs.append(deb)
    else:
        source = Source(**data)
        if not _ID_RE.fullmatch(source.id):
            raise LockError(f"source: неверный id {source.id}")
        if not _FILE_RE.fullmatch(source.file):
            raise LockError(f"source {source.id}: неверное имя файла {source.file}")
        if source.index is not None and source.dsc is not None:
            raise LockError(f"source {source.id}: index и dsc одновременно")
        if (source.git_commit is None) != (source.git_tree is None):
            raise LockError(f"source {source.id}: git_commit и git_tree — только вместе")
        for name in ("git_commit", "git_tree"):
            value = getattr(source, name)
            if value is not None and not re.fullmatch(r"[0-9a-f]{40}", value):
                raise LockError(f"source {source.id}: {name} — 40 hex")
        if source.git_delta is not None:
            if source.git_tree is None:
                raise LockError(f"source {source.id}: git_delta без git_tree")
            _check_relpath(f"source {source.id}", source.git_delta)
            if not source.git_delta.startswith("packaging/appimage/git-trees/"):
                raise LockError(f"source {source.id}: git_delta вне packaging/appimage/git-trees/")
        _check_url(f"source {source.id}", source.url, source.file)
        lock.sources.append(source)


def _check_debian(lock: Lock) -> None:
    """Ссылочная целостность и согласованность Debian-входов (R3.3)."""
    for kind, items in (
        ("archive", lock.archives),
        ("index", lock.indexes),
        ("source", lock.sources),
    ):
        ids = [item.id for item in items]
        if len(ids) != len(set(ids)):
            raise LockError(f"повторяется id записи {kind}")
    files = [source.file for source in lock.sources]
    if len(files) != len(set(files)):
        raise LockError("повторяется файл записи source")
    for index in lock.indexes:
        lock.archive(index.archive)
    for source in lock.sources:
        if source.index is not None:
            if lock.index(source.index).kind != "Sources":
                raise LockError(f"source {source.id}: index {source.index} — не Sources")
            if not source.file.endswith(".dsc"):
                raise LockError(f"source {source.id}: из индекса Sources берётся только .dsc")
        if source.dsc is not None and not lock.source(source.dsc).file.endswith(".dsc"):
            raise LockError(f"source {source.id}: dsc {source.dsc} — не .dsc")
    packages = [deb.package for deb in lock.debs]
    if len(packages) != len(set(packages)):
        raise LockError("пакет Debian указан дважды")
    for deb in lock.debs:
        if lock.index(deb.index).kind != "Packages":
            raise LockError(f"deb {deb.package}: index {deb.index} — не Packages")
        dsc = lock.source(deb.dsc)
        want = f"{deb.source}_{_upstream(deb.source_version)}.dsc"
        if dsc.file != want:
            raise LockError(f"deb {deb.package}: dsc {dsc.file}, ожидался {want}")
    if lock.base != "debian12":
        if lock.archives or lock.indexes or lock.debs:
            raise LockError(f"записи Debian допустимы только при base: debian12, а не {lock.base}")
        return
    pinned = set(packages)
    pending = {pkg for pkg in PYTHON_DEBS if any(pkg in item.split() for item in lock.todo_pin)}
    missing = [pkg for pkg in PYTHON_DEBS if pkg not in pinned and pkg not in pending]
    if missing:
        raise LockError(f"base debian12: нет пакетов {', '.join(missing)}")
    unknown = sorted(pinned - set(PYTHON_DEBS))
    if unknown:
        raise LockError(f"сборщик не умеет раскладывать пакеты {', '.join(unknown)}")
    if lock.debs:
        versions = {(d.version, d.source, d.source_version) for d in lock.debs}
        if len(versions) != 1:
            raise LockError("пакеты Python Debian разных версий или исходников")
        for deb in lock.debs:
            if deb.arch != DEB_ARCH:
                raise LockError(f"deb {deb.package}: архитектура {deb.arch}, нужна {DEB_ARCH}")
            if deb.source != PYTHON_SOURCE:
                raise LockError(f"deb {deb.package}: исходник {deb.source}, нужен {PYTHON_SOURCE}")


def parse(text: str) -> Lock:
    """Разобрать текст lock-файла и проверить его полноту."""
    lock = Lock()
    # Продолжения строк «\» склеиваем, как pip.
    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("#"):
            if buffer:
                raise LockError(f"комментарий внутри требования: {raw.strip()}")
            logical.append(line.strip())
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical.append((buffer + line).strip())
        buffer = ""
    if buffer:
        raise LockError("требование оборвано продолжением «\\» в конце файла")

    for line in logical:
        if not line:
            continue
        if line.startswith("#"):
            _parse_comment(lock, line)
            continue
        parts = line.split()
        req = _REQ_RE.fullmatch(parts[0])
        if req is None:
            raise LockError(f"требование без точной версии: {parts[0]}")
        hashes: list[str] = []
        for option in parts[1:]:
            hash_match = _HASH_RE.fullmatch(option)
            if hash_match is None:
                raise LockError(f"{req['name']}: неожиданный параметр {option}")
            hashes.append(hash_match["sha"])
        if not hashes:
            raise LockError(f"{req['name']}: нет --hash=sha256")
        lock.requirements.append(Requirement(req["name"], req["version"], tuple(hashes)))

    missing = [key for key in DIRECTIVES if key not in lock.directives]
    if missing:
        raise LockError(f"нет директив: {', '.join(missing)}")
    _check_tools(lock)
    if lock.base == "debian12":
        absent = [key for key in BUILD_DIRECTIVES if key not in lock.directives]
        if absent:
            raise LockError(f"base debian12: нет директив {', '.join(absent)}")
    origin = {"debian12": "host", "python-appimage": "bundled"}[lock.base]
    if lock.directives["openssl-origin"] != origin:
        raise LockError(f"base {lock.base}: openssl-origin должен быть {origin}")
    _check_debian(lock)
    if lock.directives["openssl-origin"] == "host":
        wanted = [f"libssl.so.{lock.openssl_major}", f"libcrypto.so.{lock.openssl_major}"]
        missing = [name for name in wanted if name not in lock.host_libs]
        if missing:
            raise LockError(f"OpenSSL с хоста, но нет host-lib: {', '.join(missing)}")
    seen: set[str] = set()
    for item in [(r.name, r.version) for r in lock.requirements] + lock.todo:
        key = _normalize(item[0])
        if key in seen:
            raise LockError(f"пакет {item[0]} указан дважды")
        seen.add(key)
    return lock


def _parse_comment(lock: Lock, line: str) -> None:
    if match := _TOOL_RE.fullmatch(line):
        lock.tools.append(Tool(match["name"], match["sha"], int(match["size"]), match["url"]))
    elif line.startswith(("# tool:", "#tool:")):
        raise LockError(f"неверная строка инструмента: {line}")
    elif match := _RECORD_RE.fullmatch(line):
        _add_record(lock, match["kind"], match["json"])
    elif re.match(r"#\s*(archive|index|deb|source):", line):
        raise LockError(f"неверная запись: {line}")
    elif match := _HOST_LIB_RE.fullmatch(line):
        if match["soname"] in lock.host_libs:
            raise LockError(f"host-lib {match['soname']} повторяется")
        lock.host_libs[match["soname"]] = match["package"]
    elif re.match(r"#\s*host-lib:", line):
        raise LockError(f"неверная строка host-lib: {line}")
    elif match := _TODO_PIN_RE.fullmatch(line):
        lock.todo_pin.append(match["what"].strip())
    elif "TODO-PIN" in line:
        raise LockError(f"неверная строка TODO-PIN: {line}")
    elif (match := _DIRECTIVE_RE.fullmatch(line)) and match["key"] in DIRECTIVES + BUILD_DIRECTIVES:
        key, value = match["key"], match["value"]
        if key in lock.directives:
            raise LockError(f"директива {key} повторяется")
        if not _VALUE_RE[key].fullmatch(value):
            raise LockError(f"неверное значение {key}: {value}")
        lock.directives[key] = value
    elif any(line.startswith(f"# {key}") for key in DIRECTIVES + BUILD_DIRECTIVES):
        raise LockError(f"неверная директива: {line}")
    elif match := _TODO_RE.fullmatch(line):
        lock.todo.append((match["name"], match["version"]))
    elif "TODO-HASH" in line:
        raise LockError(f"неверная строка TODO-HASH: {line}")


def _check_tools(lock: Lock) -> None:
    names = [tool.name for tool in lock.tools]
    if len(names) != len(set(names)):
        raise LockError("инструменты повторяются")
    for name in REQUIRED_TOOLS:
        lock.tool(name)
    python = [name for name in names if PYTHON_TOOL_RE.fullmatch(name)]
    unknown = [name for name in names if name not in REQUIRED_TOOLS and name not in python]
    if unknown:
        raise LockError(f"неизвестные инструменты: {', '.join(unknown)}")
    if lock.base == "python-appimage" and len(python) != 1:
        raise LockError("base python-appimage: нужен ровно один инструмент python3.11 AppImage")
    if lock.base == "debian12" and python:
        raise LockError("base debian12: Python AppImage в lock не нужен")


def load(path: Path) -> Lock:
    return parse(path.read_text(encoding="utf-8"))


COMMANDS = (
    "check",
    "get",
    "tools",
    "fetch",
    "debs",
    "host-libs",
    "todo",
    "todo-pin",
    "requirements",
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[1] not in COMMANDS:
        sys.stderr.write(__doc__ or "")
        return 2
    try:
        lock = load(Path(args[0]))
    except (OSError, LockError) as exc:
        sys.stderr.write(f"ОШИБКА: {args[0]}: {exc}\n")
        return 1
    command = args[1]
    if command == "get":
        known = DIRECTIVES + BUILD_DIRECTIVES
        if len(args) != 3 or args[2] not in known:
            sys.stderr.write(f"get: одна из {', '.join(known)}\n")
            return 2
        if args[2] not in lock.directives:
            sys.stderr.write(f"get: в lock нет {args[2]}\n")
            return 1
        print(lock.directives[args[2]])
    elif command == "tools":
        for tool in lock.tools:
            print(tool.name, tool.sha256, tool.size, tool.url)
    elif command == "fetch":
        for path, sha, size, url in lock.fetch_list():
            print(path, sha, size, url)
    elif command == "debs":
        for deb in lock.debs:
            print(deb.package, deb.cache_path)
    elif command == "host-libs":
        for soname, package in lock.host_libs.items():
            print(soname, package)
    elif command == "todo":
        for name, version in lock.todo:
            print(f"{name}=={version}")
    elif command == "todo-pin":
        for item in lock.todo_pin:
            print(item)
    elif command == "requirements":
        for req in lock.requirements:
            print(f"{req.name}=={req.version}")
    else:
        print(
            f"lock: база {lock.base}, {len(lock.tools)} инструмента, "
            f"{len(lock.debs)} пакетов Debian, {len(lock.sources)} исходников, "
            f"{len(lock.host_libs)} библиотек хоста, "
            f"{len(lock.requirements)} колёс, ELF {lock.expect_elf}, "
            f"glibc ≤ {lock.directives['max-glibc']}, OpenSSL {lock.openssl_major} "
            f"({lock.directives['openssl-origin']}), незакреплено: {len(lock.todo)} колёс, "
            f"{len(lock.todo_pin)} входов"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
