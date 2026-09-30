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

Режим AppImage (arch/appimage.md §9.2, T1 MN-5): состав — колёса и инструменты из
`packaging/appimage.lock`, интерпретатор python-appimage, OpenSSL из `libssl` бандла
(принятый риск П16 назван явно), ELF AppDir со sha256:

    scripts/sbom.py --appdir AppDir --lock packaging/appimage.lock --out dist/sbom-appimage.cdx.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
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


def appimage_bom(appdir: Path, lock: Path, epoch: int) -> dict[str, Any]:
    """SBOM AppDir до упаковки (тот же состав, что внутри .AppImage)."""
    info = _build_info(appdir)
    version, build_id = info["VERSION"], info["BUILD_ID"]
    openssl = _openssl_components(appdir)
    if not openssl:
        raise ValueError("в AppDir не найден libssl с версией OpenSSL")
    components = (
        _wheel_components(lock) + _tool_components(lock) + openssl + _tree_elf_components(appdir)
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
            bom = appimage_bom(args.appdir, args.lock, epoch)
        except (OSError, KeyError, ValueError) as exc:
            print(f"SBOM AppImage: {exc}", file=sys.stderr)
            return 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(bom, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"SBOM: {args.out} ({len(bom['components'])} компонентов)")
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
