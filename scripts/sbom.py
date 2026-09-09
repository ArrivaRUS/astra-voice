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


def _wheel_components() -> list[dict[str, Any]]:
    if not LOCK.is_file():
        return []
    text = LOCK.read_text(encoding="utf-8")
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


def _elf_components(deb: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="sbom-") as tmp:
        root = Path(tmp)
        subprocess.run(["dpkg-deb", "-x", str(deb), str(root)], check=True)
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deb", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

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
