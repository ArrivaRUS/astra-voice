#!/usr/bin/env python3
"""Документы лицензий AppImage в `usr/share/doc/astra-voice/` (R3.6, T-188) — до BUILD_ID.

Раскладывает:

* `LICENSE` и `NOTICE` корня репозитория;
* `packaging/appimage/licenses/` — полные тексты общих лицензий, LICENSE PyQt5, загрузчик;
* `licenses/debian/<пакет>/copyright` — из тех же проверенных `.deb` (каталог
  `usr/share/doc` распаковки, ссылки между doc-каталогами разрешаются внутри него);
* `licenses/wheels/<dist-info>/` — файлы лицензий колёс (dist-info, а где их нет — из
  пакета: onnxruntime);
* `licenses/INDEX.txt` — какой файл к какому компоненту.

И проверяет полноту: у каждого колеса и пакета Debian есть лицензия, а каждый
`/usr/share/common-licenses/<X>`, на который ссылается copyright Debian, лежит в
`licenses/common/`. Нарушение — код 1, образ не собирается.

    collect_licenses.py --appdir AppDir --root <репо> [--debian-doc <stage>/usr/share/doc
        --deb python3.11-minimal …]
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import sys
from pathlib import Path
from typing import Any

DOC = Path("usr/share/doc/astra-voice")
SITE = Path("opt/python3.11/lib/python3.11/site-packages")
#: Файлы лицензий в dist-info (и в подкаталоге licenses/ по PEP 639).
_LICENSE_RE = re.compile(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*", re.IGNORECASE)
#: Колёса без файла лицензии в dist-info: где лицензия на самом деле.
WHEEL_EXCEPTIONS: dict[str, tuple[str, ...]] = {
    "PyQt5": ("repo:PyQt5/LICENSE",),
    "onnxruntime": ("package:onnxruntime/LICENSE", "package:onnxruntime/ThirdPartyNotices.txt"),
}
#: Имя в /usr/share/common-licenses → файл в licenses/common/.
COMMON = {
    "GPL-2": "GPL-2.0.txt",
    "GPL-3": "GPL-3.0.txt",
    "LGPL-2.1": "LGPL-2.1.txt",
    "LGPL-3": "LGPL-3.0.txt",
    "Apache-2.0": "Apache-2.0.txt",
    "MPL-2.0": "MPL-2.0.txt",
}
#: Имя лицензии: буквы/цифры/+/- и числовые суффиксы через точку (LGPL-2.1, Apache-2.0);
#: завершающая точка предложения и конец файла не мешают (ревью Б).
_COMMON_REF_RE = re.compile(r"/usr/share/common-licenses/([A-Za-z0-9+-]+(?:\.[0-9]+)*)")


class LicenseError(RuntimeError):
    """Лицензия компонента не найдена или ссылается на отсутствующий текст."""


def _resolve_in(base: Path, rel: Path) -> Path:
    """Путь внутри base с разрешением относительных ссылок; выход наружу — ошибка."""
    base = base.resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base):
        raise LicenseError(f"{rel}: ссылка выходит из {base}")
    return target


def pending_sources(root: Path) -> list[Any]:
    """Записи `# pending-source:` lock (тот же разбор, что у сборщика)."""
    lock_path = root / "packaging" / "appimage.lock"
    if not lock_path.is_file():
        return []
    name = "collect_licenses_lockfile"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, root / "packaging" / "appimage" / "lockfile.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return list(sys.modules[name].load(lock_path).pending_sources)


def dist_name(dist_info: Path) -> str:
    return dist_info.name.removesuffix(".dist-info").rsplit("-", 1)[0]


def collect(
    appdir: Path, root: Path, debian_doc: Path | None, debs: list[str]
) -> list[tuple[str, str]]:
    """Разложить документы; вернуть (компонент, путь в DOC) для INDEX.txt."""
    doc = appdir / DOC
    if doc.exists():
        shutil.rmtree(doc)
    doc.mkdir(parents=True)
    index: list[tuple[str, str]] = []
    for name in ("LICENSE", "NOTICE"):
        shutil.copyfile(root / name, doc / name)
        index.append(("astra-voice", name))
    shutil.copytree(root / "packaging" / "appimage" / "licenses", doc / "licenses")
    for path in sorted((doc / "licenses").rglob("*")):
        if path.is_file():
            index.append(("общие тексты и загрузчик", str(path.relative_to(doc))))

    for package in debs:
        if debian_doc is None:
            raise LicenseError("пакеты Debian без --debian-doc")
        source = _resolve_in(debian_doc, Path(package) / "copyright")
        if not source.is_file():
            raise LicenseError(f"{package}: нет copyright в пакете")
        target = doc / "licenses" / "debian" / package / "copyright"
        target.parent.mkdir(parents=True)
        shutil.copyfile(source, target)
        index.append((f"Debian {package}", str(target.relative_to(doc))))
        for ref in sorted(set(_COMMON_REF_RE.findall(source.read_text("utf-8", "replace")))):
            if COMMON.get(ref) is None or not (doc / "licenses" / "common" / COMMON[ref]).is_file():
                raise LicenseError(f"{package}: copyright ссылается на common-licenses/{ref}")

    site = appdir / SITE
    for dist_info in sorted(site.glob("*.dist-info")):
        name = dist_name(dist_info)
        found = [
            p for p in sorted(dist_info.rglob("*")) if p.is_file() and _LICENSE_RE.fullmatch(p.name)
        ]
        target_dir = doc / "licenses" / "wheels" / dist_info.name.removesuffix(".dist-info")
        copied: list[str] = []
        for path in found:
            target = target_dir / path.relative_to(dist_info)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            copied.append(str(target.relative_to(doc)))
        for spec in WHEEL_EXCEPTIONS.get(name, ()) if not found else ():
            kind, rel = spec.split(":", 1)
            if kind == "repo":
                path = doc / "licenses" / rel
                if not path.is_file():
                    raise LicenseError(f"{name}: нет {path.relative_to(doc)}")
                copied.append(str(path.relative_to(doc)))
            else:
                source = site / rel
                if not source.is_file():
                    raise LicenseError(f"{name}: нет {rel} в колесе")
                target = target_dir / Path(rel).name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                copied.append(str(target.relative_to(doc)))
        if not copied:
            raise LicenseError(f"колесо {name}: нет файла лицензии (добавьте в WHEEL_EXCEPTIONS)")
        index += [(f"колесо {dist_info.name.removesuffix('.dist-info')}", c) for c in copied]

    lines = ["# Компонент → файл лицензии (относительно usr/share/doc/astra-voice/)"]
    lines += [f"{component}\t{path}" for component, path in index]
    pending = pending_sources(root)
    if pending:
        lines += [
            "",
            "# ИСХОДНИКИ НЕ ПРИЛОЖЕНЫ — открытый пункт (packaging/appimage.lock, pending-source)",
        ]
        lines += [f"{p.component}\t{p.version}\t{p.license}\t{p.note}" for p in pending]
    (doc / "licenses" / "INDEX.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="документы лицензий AppImage")
    parser.add_argument("--appdir", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--debian-doc", type=Path)
    parser.add_argument("--deb", action="append", default=[], metavar="ПАКЕТ")
    args = parser.parse_args(argv)
    try:
        index = collect(args.appdir, args.root, args.debian_doc, args.deb)
    except (OSError, LicenseError) as exc:
        print(f"ОШИБКА: лицензии: {exc}", file=sys.stderr)
        return 1
    print(f"лицензии: {len(index)} файлов в {DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
