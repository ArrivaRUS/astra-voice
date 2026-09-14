#!/usr/bin/env python3
"""В CI пропуск теста из-за отсутствующей зависимости — это не успех, а дефект.

`pytest.importorskip("Xlib")` — правильное поведение на машине разработчика:
нет модуля — нет смысла в тесте. В CI та же строка превращает забытый пакет в
зелёную задачу без единой проверки (так задача `xvfb` месяц «проходила»
тесты вехи M4, не имея `python3-xlib`).

Скрипт вычитывает все `pytest.importorskip("…")` из указанных каталогов с
тестами и требует, чтобы каждый такой модуль действительно импортировался.
Список не ведётся руками: добавили в тест новую зависимость — гейт сам
потребует её в CI, пока она не появится в apt-списке задачи.

    scripts/ci_require_imports.py tests/xvfb            # все зависимости каталога
    scripts/ci_require_imports.py tests/unit --also numpy
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

#: Подсказка «что поставить», чтобы отчёт был действием, а не загадкой.
APT_HINT = {
    "PyQt5": "python3-pyqt5",
    "PyQt5.QtQuick": "python3-pyqt5.qtquick",
    "PyQt5.QtWidgets": "python3-pyqt5",
    "Xlib": "python3-xlib",
    "jsonschema": "python3-jsonschema",
    "numpy": "python3-numpy",
    "requests": "python3-requests",
    "yaml": "python3-yaml",
}

_IMPORTORSKIP_RE = re.compile(r"""importorskip\(\s*["']([A-Za-z0-9_.]+)["']""")


def required_modules(paths: list[Path]) -> list[str]:
    """Модули из всех `pytest.importorskip(...)` в указанных файлах/каталогах."""
    found: set[str] = set()
    for path in paths:
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for file in files:
            found.update(_IMPORTORSKIP_RE.findall(file.read_text(encoding="utf-8")))
    return sorted(found)


def missing(modules: list[str]) -> list[str]:
    """Те из модулей, которые сейчас не импортируются."""
    out: list[str] = []
    for name in modules:
        try:
            if importlib.util.find_spec(name) is None:
                out.append(name)
        except (ImportError, ValueError):
            # find_spec подмодуля бросает ImportError, если нет родителя.
            out.append(name)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="обязательные зависимости тестов в CI")
    ap.add_argument("paths", nargs="+", type=Path, help="каталоги или файлы тестов")
    ap.add_argument("--also", nargs="*", default=[], help="модули сверх найденных")
    args = ap.parse_args(argv)

    modules = sorted(set(required_modules(args.paths)) | set(args.also))
    if not modules:
        print("зависимостей через importorskip не найдено — проверять нечего")
        return 0

    gone = missing(modules)
    for name in modules:
        mark = "НЕТ" if name in gone else "ok"
        print(f"  {name}: {mark}")

    if gone:
        print(
            "\nэти модули нужны тестам, но не установлены — в CI тесты будут\n"
            "молча пропущены, а задача позеленеет. Поставьте пакеты:",
            file=sys.stderr,
        )
        for name in gone:
            print(
                f"  - {name} → {APT_HINT.get(name, '<пакет неизвестен, дополните APT_HINT>')}",
                file=sys.stderr,
            )
        return 1
    print(f"все {len(modules)} зависимости на месте — тесты не будут пропущены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
