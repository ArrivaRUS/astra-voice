#!/usr/bin/env python3
"""SBOM в формате CycloneDX 1.5 (JSON).

`syft` в apt Astra нет (arch/plan-claude.md §7.3), поэтому состав собирается из
двух честных источников:

* `packaging/wheels.lock` — вендорируемые колёса с их sha256 (они же попадают
  в пакет байт-в-байт, `dh_strip` их не трогает);
* поле `Depends` собранного `.deb` — системные пакеты; их версии берутся
  `dpkg-query` на машине сборки, если пакет там установлен.

Дополнительно перечисляются ELF из пакета с их sha256 — это тот самый список
объектов, который заказчик подписывает по ГОСТ в контуре с ЗПС (v1.1).

    scripts/sbom.py --deb dist/astra-voice_0.1.0~m1_amd64.deb --out dist/sbom.cdx.json

Режим AppImage (arch/appimage.md §9.2, «Ревизия 3» R3.4; T1 MN-5). Lock разбирает тот же
`packaging/appimage/lockfile.py`, что и сборщик. База `debian12`:

* пакеты Debian — полная версия, sha256 входного `.deb`, исходник и `.dsc`, число
  оставленных файлов;
* колёса lock и вложенные в них библиотеки (`*.libs/`, модули Qt 5.15.19 с ссылками на
  исходники, ICU) — лицензия и sha256 каждого файла;
* внешние зависимости хоста из записей `# host-lib:` и OpenSSL — `origin=host`,
  `not-bundled`; версия OpenSSL машины сборки — только наблюдение среды проверки;
* type2-runtime — поставляемый компонент (он внутри .AppImage) со статически вшитыми
  libfuse, squashfuse и библиотеками Alpine; appimagetool и подпись — сборочные входы;
* каждый ELF AppDir со sha256 и происхождением (пакет Debian или колесо); ELF без
  происхождения — ошибка, SBOM не пишется.

База `python-appimage` (откат (г)) — прежняя модель: интерпретатор-инструмент и бандловый
OpenSSL (принятый риск П16).

    scripts/sbom.py --appdir AppDir --lock packaging/appimage.lock \\
        --cache ~/.cache/astra-voice-dev/appimage --out dist/sbom-appimage.cdx.json
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import importlib.util
import io
import json
import os
import re
import ssl
import subprocess
import sys
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "packaging" / "wheels.lock"

_LOCK_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s\\]+)\s*\\?\s*(?:\n\s*--hash=sha256:(?P<sha>[0-9a-f]{64}))?",
    re.MULTILINE,
)
_DEP_RE = re.compile(r"^\s*([a-z0-9][a-z0-9+.\-]*)")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def _wheel_components(lock: Path = LOCK) -> list[dict[str, Any]]:
    if not lock.is_file():
        return []
    text = lock.read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    out: list[dict[str, Any]] = []
    for m in _LOCK_RE.finditer(text):
        name, version, sha = m.group("name"), m.group("version"), m.group("sha")
        comp: dict[str, Any] = {
            "type": "library",
            "bom-ref": f"pkg:pypi/{name}@{version}",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{name}@{version}",
            "scope": "required",
            "properties": [{"name": "astra-voice:origin", "value": "vendored wheel"}],
        }
        if sha:
            comp["hashes"] = [{"alg": "SHA-256", "content": sha}]
        out.append(comp)
    return out


def _deb_field(deb: Path, field: str) -> str:
    return subprocess.run(
        ["dpkg-deb", "--field", str(deb), field],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _installed_version(pkg: str) -> str | None:
    proc = subprocess.run(
        ["dpkg-query", "-W", "-f", "${Version}", pkg],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _dep_components(deb: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field in ("Depends", "Recommends"):
        try:
            raw = _deb_field(deb, field)
        except subprocess.CalledProcessError:
            continue
        for item in raw.split(","):
            # Альтернативы «a | b»: в SBOM попадает первая — её и ставит apt.
            first = item.split("|")[0]
            m = _DEP_RE.match(first)
            if not m:
                continue
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            version = _installed_version(name)
            comp: dict[str, Any] = {
                "type": "library",
                "bom-ref": f"pkg:deb/debian/{name}" + (f"@{version}" if version else ""),
                "name": name,
                "version": version or "unknown",
                "purl": f"pkg:deb/debian/{name}" + (f"@{version}" if version else ""),
                "scope": "required" if field == "Depends" else "optional",
                "properties": [{"name": "astra-voice:origin", "value": f"apt ({field})"}],
            }
            out.append(comp)
    return out


def _tree_elf_components(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        with open(path, "rb") as fh:
            if fh.read(4) != b"\x7fELF":
                continue
        rel = "/" + str(path.relative_to(root))
        out.append(
            {
                "type": "file",
                "bom-ref": f"file:{rel}",
                "name": rel,
                "hashes": [{"alg": "SHA-256", "content": _sha256(path)}],
                "properties": [
                    {"name": "astra-voice:gost-signing-object", "value": "true"},
                ],
            }
        )
    return out


def _elf_components(deb: Path) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="sbom-") as tmp:
        root = Path(tmp)
        subprocess.run(["dpkg-deb", "-x", str(deb), str(root)], check=True)
        return _tree_elf_components(root)


_TOOL_RE = re.compile(
    r"^#\s*tool:\s+(?P<name>\S+)\s+(?P<sha>[0-9a-f]{64})\s+(?P<size>\d+)\s+(?P<url>\S+)\s*$",
    re.MULTILINE,
)
_OPENSSL_RE = re.compile(rb"OpenSSL (\d+\.\d+\.\d+[a-z]*) ")
_PYTHON_TOOL_RE = re.compile(r"python(\d+\.\d+\.\d+)-")


def _tool_components(lock: Path) -> list[dict[str, Any]]:
    """Инструменты сборки и база интерпретатора из строк `# tool:` lock-файла."""
    out: list[dict[str, Any]] = []
    for m in _TOOL_RE.finditer(lock.read_text(encoding="utf-8")):
        name, url = m.group("name"), m.group("url")
        python = _PYTHON_TOOL_RE.match(name)
        comp: dict[str, Any] = {
            # Интерпретатор python-appimage попадает в образ; остальные — только сборка.
            "type": "application" if python else "file",
            "bom-ref": f"tool:{name}",
            "name": "cpython (python-appimage)" if python else name,
            "hashes": [{"alg": "SHA-256", "content": m.group("sha")}],
            "externalReferences": [{"type": "distribution", "url": url}],
            "scope": "required" if python else "excluded",
            "properties": [
                {
                    "name": "astra-voice:origin",
                    "value": "bundled interpreter" if python else "build tool",
                }
            ],
        }
        if python:
            comp["version"] = python.group(1)
            comp["purl"] = f"pkg:generic/cpython@{python.group(1)}"
        out.append(comp)
    return out


def _openssl_components(appdir: Path) -> list[dict[str, Any]]:
    """OpenSSL из `libssl` бандла — явная строка SBOM для принятого риска П16 (T1 MN-5)."""
    versions: dict[str, list[str]] = {}
    for path in sorted(appdir.rglob("libssl.so*")):
        if not path.is_file() or path.is_symlink():
            continue
        found = _OPENSSL_RE.search(path.read_bytes())
        if found:
            version = found.group(1).decode("ascii")
            versions.setdefault(version, []).append("/" + str(path.relative_to(appdir)))
    return [
        {
            "type": "library",
            "bom-ref": f"pkg:generic/openssl@{version}",
            "name": "openssl",
            "version": version,
            "purl": f"pkg:generic/openssl@{version}",
            "scope": "required",
            "properties": [
                {"name": "astra-voice:origin", "value": "bundled with python-appimage"},
                {"name": "astra-voice:files", "value": " ".join(files)},
                {"name": "astra-voice:accepted-risk", "value": "П16 (decisions/log.md 28.09)"},
            ],
        }
        for version, files in versions.items()
    ]


def _build_info(appdir: Path) -> dict[str, str]:
    info: dict[str, str] = {}
    for line in (appdir / ".astra-voice-build").read_text(encoding="ascii").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            info[key] = value
    return info


def _load_lockfile() -> Any:
    """Тот же разбор lock, что у build.sh (packaging/appimage/lockfile.py)."""
    name = "astra_voice_appimage_lockfile"
    if name not in sys.modules:
        path = ROOT / "packaging" / "appimage" / "lockfile.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _prop(name: str, value: str) -> dict[str, str]:
    return {"name": f"astra-voice:{name}", "value": value}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


SITE = "opt/python3.11/lib/python3.11/site-packages"
#: Файлы Qt внутри PyQt5/Qt5 → модуль Qt (исходник в lock: `# source: <модуль>-<версия>`).
QT_MODULES: dict[str, tuple[str, ...]] = {
    "qtbase": (
        "lib/libQt5Core.",
        "lib/libQt5Gui.",
        "lib/libQt5Widgets.",
        "lib/libQt5DBus.",
        "lib/libQt5Network.",
        "lib/libQt5XcbQpa.",
        "plugins/platforms/",
        "plugins/xcbglintegrations/",
        "plugins/platforminputcontexts/",
    ),
    "qtdeclarative": (
        "lib/libQt5Qml.",
        "lib/libQt5QmlModels.",
        "lib/libQt5QmlWorkerScript.",
        "lib/libQt5Quick.",
        "qml/QtQml/",
        "qml/QtQuick.2/",
        "qml/QtQuick/Layouts/",
        "qml/QtQuick/Window.2/",
    ),
    "qtquickcontrols2": (
        "lib/libQt5QuickControls2.",
        "lib/libQt5QuickTemplates2.",
        "qml/QtQuick/Controls.2/",
        "qml/QtQuick/Templates.2/",
    ),
    "qtsvg": ("lib/libQt5Svg.", "plugins/imageformats/", "plugins/iconengines/"),
    "icu": ("lib/libicu",),
}
#: Версия ICU — из имени файла Qt (libicuuc.so.56), версия Qt — из пина PyQt5-Qt5 в lock.
_ICU_RE = re.compile(r"libicu[a-z0-9]*\.so\.([0-9]+)")
#: Лицензии вложенных в колёса библиотек (по имени файла; auditwheel добавляет хэш к имени).
NESTED_LICENSES = (
    (re.compile(r"libopenblas"), "BSD-3-Clause"),
    (re.compile(r"libgfortran"), "GPL-3.0-or-later WITH GCC-exception-3.1"),
    (re.compile(r"libquadmath"), "LGPL-2.1-or-later"),
)
#: Статически вшито в type2-runtime (scripts/common/install-dependencies.sh и Dockerfile
#: коммита dd6cebed): версия None — пакет Alpine 3.21 без закреплённой версии (риск R3).
RUNTIME_PARTS = (
    ("libfuse", "3.15.0", "LGPL-2.1-only", "libfuse-3.15.0"),
    ("squashfuse", "0.5.2", "BSD-2-Clause", None),
    ("musl", None, "MIT", None),
    ("zstd", None, "BSD-3-Clause", None),
    ("zlib", None, "Zlib", None),
    ("mimalloc", None, "MIT", None),
)


def _is_elf(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    with open(path, "rb") as fh:
        return fh.read(4) == b"\x7fELF"


def _source_refs(lock: Any, *ids: str | None) -> list[dict[str, str]]:
    refs = []
    for ident in ids:
        if ident is not None:
            refs.append({"type": "source-distribution", "url": lock.source(ident).url})
    return refs


def _deb_files(deb: Path) -> list[str]:
    """Пути файлов и ссылок .deb (без ./), через dpkg-deb --fsys-tarfile."""
    raw = subprocess.run(
        ["dpkg-deb", "--fsys-tarfile", str(deb)], capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        return [m.name.removeprefix("./") for m in tar.getmembers() if not m.isdir()]


def _appdir_path(deb_path: str) -> str | None:
    """Куда build.sh кладёт файл пакета интерпретатора; None — файл в образ не идёт."""
    if deb_path == "usr/bin/python3.11":
        return "opt/python3.11/bin/python3.11"
    if deb_path.startswith("usr/lib/python3.11/"):
        return "opt/python3.11/" + deb_path.removeprefix("usr/")
    return None


def _debian_components(
    appdir: Path, lock: Any, cache: Path, origin: dict[str, str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for deb in lock.debs:
        ref = f"pkg:deb/debian/{deb.package}@{deb.version}?arch={deb.arch}"
        path = cache / deb.cache_path
        if _sha256(path) != deb.sha256:
            raise ValueError(f"{deb.package}: .deb в кэше не совпал с lock")
        kept = 0
        for member in _deb_files(path):
            target = _appdir_path(member)
            if target is None or not (appdir / target).exists():
                continue
            kept += 1
            origin.setdefault(target, ref)
        dsc = lock.source(deb.dsc)
        tarballs = [s.id for s in lock.sources if s.dsc == deb.dsc]
        out.append(
            {
                "type": "library",
                "bom-ref": ref,
                "name": deb.package,
                "version": deb.version,
                "purl": ref,
                "scope": "required",
                "licenses": [
                    {"license": {"name": f"PSF-2.0 и др.: /usr/share/doc/{deb.package}/copyright"}}
                ],
                "hashes": [{"alg": "SHA-256", "content": deb.sha256}],
                "externalReferences": [
                    {"type": "distribution", "url": deb.url},
                    {"type": "source-distribution", "url": dsc.url},
                    *_source_refs(lock, *tarballs),
                ],
                "properties": [
                    _prop("origin", "bundled (Debian 12)"),
                    _prop("source-package", f"{deb.source} {deb.source_version}"),
                    _prop("files-retained", str(kept)),
                ],
            }
        )
    return out


def _wheel_records(appdir: Path) -> dict[str, list[str]]:
    """Нормализованное имя колеса → пути из RECORD (относительно site-packages)."""
    site = appdir / SITE
    result: dict[str, list[str]] = {}
    for record in sorted(site.glob("*.dist-info/RECORD")):
        name = record.parent.name.removesuffix(".dist-info").rsplit("-", 1)[0]
        with record.open(encoding="utf-8", newline="") as fh:
            result[_norm(name)] = [row[0] for row in csv.reader(fh) if row]
    return result


def _nested_license(name: str) -> str | None:
    for pattern, license_id in NESTED_LICENSES:
        if pattern.search(name):
            return license_id
    return None


def _qt_module(rel: str) -> str | None:
    """Модуль Qt для пути внутри PyQt5/Qt5/; None — не распознан."""
    for module, prefixes in QT_MODULES.items():
        if rel.startswith(prefixes):
            return module
    return None


# Provenance of the pinned NumPy runtime libraries; generic pending-source still applies
# to other/unresolved inputs. IDs refer to complete CentOS SRPMs in appimage.lock.
GCC_RUNTIME_SOURCES = (
    ("libquadmath-*.so*", "gcc-libquadmath"),
    ("libgfortran-*.so*", "gcc-libgfortran"),
)


def _gcc_source(lock: Any, name: str) -> Any:
    for pattern, ident in GCC_RUNTIME_SOURCES:
        if fnmatch.fnmatchcase(name, pattern):
            return next((source for source in lock.sources if source.id == ident), None)
    return None


def _pending_props(lock: Any, name: str, used: set[str]) -> list[dict[str, str]]:
    """Свойства «исходники не приложены» для файла из записей `# pending-source:` lock."""
    props: list[dict[str, str]] = []
    for pending in lock.pending_sources:
        if fnmatch.fnmatchcase(name, pending.match):
            used.add(pending.id)
            props += [
                _prop("sources", "not-attached"),
                _prop("open-item", pending.note),
                _prop("component-version", pending.version),
            ]
    return props


def _wheel_components_r3(appdir: Path, lock: Any, origin: dict[str, str]) -> list[dict[str, Any]]:
    records = _wheel_records(appdir)
    pending_used: set[str] = set()
    site = appdir / SITE
    out: list[dict[str, Any]] = []
    for req in lock.requirements:
        ref = f"pkg:pypi/{req.name}@{req.version}"
        files = records.get(_norm(req.name))
        if files is None:
            raise ValueError(f"колесо {req.name}: нет RECORD в AppDir")
        nested: list[dict[str, Any]] = []
        qt_files: dict[str, list[str]] = {}
        for rel in files:
            path = site / rel
            if not path.exists():
                continue
            target = f"{SITE}/{rel}"
            if rel.startswith("PyQt5/Qt5/"):
                module = _qt_module(rel.removeprefix("PyQt5/Qt5/"))
                if module is None and _is_elf(path):
                    raise ValueError(f"ELF Qt без модуля в карте sbom.py: {rel}")
                if module is not None:
                    qt_ref = f"pkg:generic/qt5-{module}@{req.version}"
                    if module == "icu":
                        icu = _ICU_RE.fullmatch(path.name)
                        if icu is None:
                            raise ValueError(f"не определить версию ICU: {rel}")
                        qt_ref = f"pkg:generic/icu@{icu.group(1)}"
                    qt_files.setdefault(qt_ref, []).append(rel)
                    if _is_elf(path):
                        origin[target] = qt_ref
                    continue
            if _is_elf(path):
                origin[target] = ref
                if ".libs/" in rel:
                    license_id = _nested_license(path.name)
                    comp: dict[str, Any] = {
                        "type": "library",
                        "bom-ref": f"file:/{target}",
                        "name": path.name,
                        "hashes": [{"alg": "SHA-256", "content": _sha256(path)}],
                        "properties": [
                            _prop("vendored-by", ref),
                            *_pending_props(lock, path.name, pending_used),
                        ],
                    }
                    if license_id:
                        comp["licenses"] = [{"expression": license_id}]
                    source = _gcc_source(lock, path.name)
                    if source is not None:
                        comp["externalReferences"] = _source_refs(lock, source.id)
                        comp["properties"] += [
                            _prop("sources", "pinned"),
                            _prop("source-file", source.file),
                            _prop("source-sha256", source.sha256),
                        ]
                    nested.append(comp)
        icu_refs = sorted(r for r in qt_files if r.startswith("pkg:generic/icu@"))
        if len(icu_refs) > 1:
            raise ValueError(f"несколько версий ICU в колесе: {', '.join(icu_refs)}")
        for qt_ref, qt_rel in sorted(qt_files.items()):
            name, _, component_version = qt_ref.split("/", 1)[1].partition("@")
            module = name.removeprefix("qt5-")
            is_icu = module == "icu"
            comp = {
                "type": "library",
                "bom-ref": qt_ref,
                "name": name,
                "version": component_version,
                "purl": qt_ref,
                "licenses": [{"expression": "ICU" if is_icu else "LGPL-3.0-only"}],
                "properties": [
                    _prop("vendored-by", ref),
                    _prop("files", str(len(qt_rel))),
                ],
            }
            if not is_icu:
                comp["externalReferences"] = _source_refs(lock, f"{module}-{component_version}")
            nested.append(comp)
        comp = {
            "type": "library",
            "bom-ref": ref,
            "name": req.name,
            "version": req.version,
            "purl": ref,
            "scope": "required",
            "hashes": [{"alg": "SHA-256", "content": sha} for sha in req.hashes],
            "properties": [_prop("origin", "vendored wheel")],
        }
        if req.name == "PyQt5":
            comp["externalReferences"] = _source_refs(lock, "pyqt5-sdist")
        if nested:
            comp["components"] = nested
        out.append(comp)
    stale = sorted(p.id for p in lock.pending_sources if p.id not in pending_used)
    if stale:
        raise ValueError(f"pending-source без файла в образе: {', '.join(stale)}")
    return out


def _host_components(lock: Any) -> list[dict[str, Any]]:
    """Внешние зависимости (R3.2): с хоста, в образ не кладутся."""
    packages: dict[str, list[str]] = {}
    for soname, package in lock.host_libs.items():
        packages.setdefault(package, []).append(soname)
    out: list[dict[str, Any]] = []
    for package, sonames in sorted(packages.items()):
        props = [
            _prop("origin", "host"),
            _prop("not-bundled", "true"),
            _prop("sonames", " ".join(sorted(sonames))),
        ]
        if package == "libssl3":
            # Версия OpenSSL машины сборки — не свойство образа: только в лог (ревью В).
            props.append(_prop("requirement", f"OpenSSL major {lock.openssl_major}"))
        out.append(
            {
                "type": "library",
                "bom-ref": f"host:{package}",
                "name": "openssl" if package == "libssl3" else package,
                "scope": "required",
                "properties": props,
            }
        )
    return out


def _runtime_components(lock: Any) -> list[dict[str, Any]]:
    """Runtime поставляется внутри .AppImage; appimagetool и .sig — только сборка."""
    out: list[dict[str, Any]] = []
    for tool in lock.tools:
        if tool.name == "runtime-x86_64":
            release = tool.url.rstrip("/").split("/")[-2]
            nested = []
            for name, version, license_id, source in RUNTIME_PARTS:
                part: dict[str, Any] = {
                    "type": "library",
                    "bom-ref": f"runtime:{name}",
                    "name": name,
                    "version": version or "не закреплена (Alpine 3.21)",
                    "licenses": [{"expression": license_id}],
                    "properties": [_prop("linkage", "static")],
                }
                if source:
                    part["externalReferences"] = _source_refs(lock, source)
                nested.append(part)
            out.append(
                {
                    "type": "application",
                    "bom-ref": f"pkg:generic/appimage-type2-runtime@{release}",
                    "name": "type2-runtime",
                    "version": release,
                    "purl": f"pkg:generic/appimage-type2-runtime@{release}",
                    "scope": "required",
                    "licenses": [{"expression": "MIT"}],
                    "hashes": [{"alg": "SHA-256", "content": tool.sha256}],
                    "externalReferences": [
                        {"type": "distribution", "url": tool.url},
                        *_source_refs(lock, "type2-runtime-src"),
                    ],
                    "properties": [_prop("origin", "shipped: AppImage runtime")],
                    "components": nested,
                }
            )
        else:
            out.append(
                {
                    "type": "file",
                    "bom-ref": f"tool:{tool.name}",
                    "name": tool.name,
                    "hashes": [{"alg": "SHA-256", "content": tool.sha256}],
                    "externalReferences": [{"type": "distribution", "url": tool.url}],
                    "scope": "excluded",
                    "properties": [_prop("origin", "build tool")],
                }
            )
    return out


def _attributed_elf(appdir: Path, origin: dict[str, str]) -> list[dict[str, Any]]:
    """ELF AppDir со sha256 и происхождением; ELF без происхождения — ошибка."""
    elves = _tree_elf_components(appdir)
    unknown = [c["name"] for c in elves if c["name"].lstrip("/") not in origin]
    if unknown:
        raise ValueError(f"ELF без происхождения: {', '.join(unknown[:5])}")
    for comp in elves:
        comp["properties"].append(_prop("from", origin[comp["name"].lstrip("/")]))
    return elves


def appimage_bom(
    appdir: Path, lock_path: Path, epoch: int, cache: Path | None = None
) -> dict[str, Any]:
    """SBOM AppDir до упаковки (тот же состав, что внутри .AppImage)."""
    info = _build_info(appdir)
    version, build_id = info["VERSION"], info["BUILD_ID"]
    lock = _load_lockfile().load(lock_path)
    if lock.base == "debian12":
        if cache is None:
            raise ValueError("для базы debian12 нужен --cache (входные .deb)")
        origin: dict[str, str] = {}
        components = (
            _debian_components(appdir, lock, cache, origin)
            + _wheel_components_r3(appdir, lock, origin)
            + _host_components(lock)
            + _runtime_components(lock)
            + _attributed_elf(appdir, origin)
        )
    else:
        openssl = _openssl_components(appdir)
        if not openssl:
            raise ValueError("в AppDir не найден libssl с версией OpenSSL")
        components = (
            _wheel_components(lock_path)
            + _tool_components(lock_path)
            + openssl
            + _tree_elf_components(appdir)
        )
    ts = datetime.fromtimestamp(epoch, tz=UTC)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:"
        + str(uuid.uuid5(uuid.NAMESPACE_URL, f"astra-voice-appimage/{version}/{build_id}")),
        "version": 1,
        "metadata": {
            "timestamp": ts.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "tools": [{"vendor": "ArrivaRUS", "name": "astra-voice sbom.py", "version": "1"}],
            "component": {
                "type": "application",
                "bom-ref": f"pkg:generic/astra-voice-appimage@{version}",
                "name": "astra-voice",
                "version": version,
                "purl": f"pkg:generic/astra-voice-appimage@{version}?arch=x86_64",
                "licenses": [{"license": {"id": "GPL-3.0-or-later"}}],
                "properties": [{"name": "astra-voice:build-id", "value": build_id}],
            },
        },
        "components": components,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--deb", type=Path)
    source.add_argument("--appdir", type=Path, help="AppDir AppImage (вместе с --lock)")
    ap.add_argument("--lock", type=Path, help="packaging/appimage.lock для --appdir")
    ap.add_argument("--cache", type=Path, help="кэш входов AppImage (база debian12)")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    if args.appdir is not None:
        if args.lock is None or not args.lock.is_file():
            print("для --appdir нужен существующий --lock", file=sys.stderr)
            return 2
        if not (args.appdir / ".astra-voice-build").is_file():
            print(f"нет маркера сборки в {args.appdir}", file=sys.stderr)
            return 2
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH") or 0)
        try:
            bom = appimage_bom(args.appdir, args.lock, epoch, args.cache)
        except (OSError, KeyError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"SBOM AppImage: {exc}", file=sys.stderr)
            return 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(bom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"SBOM: {args.out} ({len(bom['components'])} компонентов)")
        print(f"OpenSSL машины сборки (наблюдение, в SBOM не пишется): {ssl.OPENSSL_VERSION}")
        return 0

    if not args.deb.is_file():
        print(f"нет пакета: {args.deb}", file=sys.stderr)
        return 2

    version = _deb_field(args.deb, "Version")
    # Воспроизводимость: время сборки берём из SOURCE_DATE_EPOCH, если задан.
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    ts = datetime.fromtimestamp(int(epoch) if epoch else args.deb.stat().st_mtime, tz=UTC)

    components = _wheel_components() + _dep_components(args.deb) + _elf_components(args.deb)
    bom: dict[str, Any] = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:"
        + str(uuid.uuid5(uuid.NAMESPACE_URL, f"astra-voice/{version}/{_sha256(args.deb)}")),
        "version": 1,
        "metadata": {
            "timestamp": ts.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "tools": [{"vendor": "ArrivaRUS", "name": "astra-voice sbom.py", "version": "1"}],
            "component": {
                "type": "application",
                "bom-ref": f"pkg:deb/astra-voice@{version}",
                "name": "astra-voice",
                "version": version,
                "purl": f"pkg:deb/astra-voice@{version}?arch=amd64",
                "licenses": [{"license": {"id": "GPL-3.0-or-later"}}],
                "hashes": [{"alg": "SHA-256", "content": _sha256(args.deb)}],
            },
        },
        "components": components,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(bom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"SBOM: {args.out} ({len(components)} компонентов)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
