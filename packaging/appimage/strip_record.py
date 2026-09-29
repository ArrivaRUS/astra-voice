#!/usr/bin/env python3
"""Вычистка `dist-info/RECORD` бандла от строк обёрток консольных скриптов.

pip --target кладёт обёртки в `bin/` и пишет их в RECORD как `../../bin/<имя>` с хэшем
файла, в shebang которого — абсолютный путь интерпретатора (путь рабочего каталога).
Обёрток в образе нет, поэтому строки удаляются: RECORD описывает ровно то, что лежит в
образе, и BUILD_ID не зависит от каталога сборки. Любая другая строка вне site-packages
(`../…`) — неожиданность: сборка останавливается, а не чистит молча.

    strip_record.py <site-packages>
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_PREFIX = "../../bin/"


class RecordError(ValueError):
    """В RECORD есть файл вне site-packages, кроме обёрток bin/."""


def strip_record(text: str, where: str = "RECORD") -> str:
    """Убрать строки `../../bin/…`; на иных строках `../` — `RecordError`."""
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith(SCRIPTS_PREFIX):
            continue
        if line.startswith("../"):
            path = line.split(",", 1)[0]
            raise RecordError(f"{where}: файл вне site-packages, не обёртка bin/: {path}")
        kept.append(line)
    return "".join(kept)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write("usage: strip_record.py <site-packages>\n")
        return 2
    records = sorted(Path(args[0]).glob("*.dist-info/RECORD"))
    try:
        for record in records:
            text = record.read_text(encoding="utf-8")
            cleaned = strip_record(text, str(record.relative_to(args[0])))
            if cleaned != text:
                record.write_text(cleaned, encoding="utf-8")
    except RecordError as exc:
        sys.stderr.write(f"ОШИБКА: {exc}\n")
        return 1
    print(f"RECORD: {len(records)} файлов, строки обёрток bin/ убраны")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
