#!/usr/bin/env python3
"""Собрать `DEBIAN/control` из `packaging/debian/control` для режима `--host`.

Нужен только запасному пути сборки (без debhelper): берёт двоичную секцию
исходного `control`, подставляет версию, размер и убирает подстановки `${…}`,
которые в нормальной сборке раскрывает dh. Единственный источник правды по
зависимостям остаётся `packaging/debian/control`.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

_SUBST = re.compile(r"\$\{[^}]*\}")

# Порядок полей в бинарном control (Debian Policy 5.3).
_ORDER = [
    "Package",
    "Version",
    "Architecture",
    "Maintainer",
    "Installed-Size",
    "Depends",
    "Recommends",
    "Suggests",
    "Section",
    "Priority",
    "Homepage",
    "Description",
]


def _stanzas(text: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    # Секции control разделяются пустой строкой; продолжения — с отступа.
    for chunk in re.split(r"\n[ \t]*\n", text.strip()):
        fields: dict[str, str] = {}
        key = ""
        for line in chunk.splitlines():
            if line.lstrip().startswith("#"):  # комментарии deb822
                continue
            if line[:1] in (" ", "\t") and key:
                fields[key] += "\n" + line
            elif ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                fields[key] = value.strip()
        if fields:
            out.append(fields)
    return out


def _clean_deps(value: str) -> str:
    """Свернуть многострочный список зависимостей и выкинуть `${misc:Depends}`."""
    parts = [p.strip() for p in value.replace("\n", " ").split(",")]
    parts = [_SUBST.sub("", p).strip() for p in parts]
    return ", ".join(p for p in parts if p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", required=True, type=Path)
    ap.add_argument("--version", required=True)
    ap.add_argument("--tree", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    stanzas = _stanzas(args.control.read_text(encoding="utf-8"))
    source = next(s for s in stanzas if "Source" in s)
    binary = next(s for s in stanzas if "Package" in s)

    du = subprocess.run(
        ["du", "-sk", "--exclude=DEBIAN", str(args.tree)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]

    fields: dict[str, str] = {
        "Package": binary["Package"],
        "Version": args.version,
        "Architecture": binary.get("Architecture", "amd64"),
        "Maintainer": source["Maintainer"],
        "Installed-Size": du,
        "Section": binary.get("Section", source.get("Section", "utils")),
        "Priority": binary.get("Priority", source.get("Priority", "optional")),
        "Homepage": source.get("Homepage", ""),
        "Description": binary["Description"],
    }
    for key in ("Depends", "Recommends", "Suggests", "Conflicts", "Breaks"):
        if key in binary:
            cleaned = _clean_deps(binary[key])
            if cleaned:
                fields[key] = cleaned

    lines = [
        f"{k}: {fields[k]}" for k in _ORDER if k in fields and fields[k] and k != "Description"
    ]
    lines.append(f"Description: {fields['Description']}")
    args.out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
