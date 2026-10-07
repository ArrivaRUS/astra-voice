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
    scripts/ci_require_imports.py tests/unit --if-exists tests/net tests/updates

`--if-exists` — каталоги, которых в дереве может ещё не быть (появляются с другой веткой):
отсутствующий пропускается с пометкой. Обычный путь без файла — ошибка (опечатка не должна
молча выключать гейт).

Файл, где `importorskip` встречается внутри тестовой фикстуры, а не как настоящая
зависимость (например, тест самого гейта), исключается строкой-меткой
`ci-require-imports: skip-file` в его тексте. Метка ставится осознанно и только
в таком случае: ею легко спрятать реальную зависимость.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
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

#: Метка «не сканировать этот файл»: `importorskip` здесь — тестовая фикстура.
SKIP_FILE_MARKER = "ci-require-imports: skip-file"


def required_modules(paths: list[Path]) -> list[str]:
    """Модули из всех `pytest.importorskip(...)` в указанных файлах/каталогах."""
    found: set[str] = set()
    for path in paths:
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for file in files:
            text = file.read_text(encoding="utf-8")
            if SKIP_FILE_MARKER in text:
                continue
            found.update(_IMPORTORSKIP_RE.findall(text))
    return sorted(found)


IMPORT_TIMEOUT = 30
_IMPORT_OK = "ASTRA_VOICE_IMPORT_OK"
_IMPORT_PROBE = """import importlib, json, sys, traceback
sys.path = json.loads(sys.argv[2])
try:
    importlib.import_module(sys.argv[1])
except BaseException:
    traceback.print_exc()
    sys.exit(1)
print("ASTRA_VOICE_IMPORT_OK")
"""


def import_errors(modules: list[str]) -> dict[str, str]:
    """Настоящий импорт отдельно от гейта: ошибка, exit или зависание дают отказ."""
    errors: dict[str, str] = {}
    for name in modules:
        try:
            result = subprocess.run(
                [sys.executable, "-B", "-c", _IMPORT_PROBE, name, json.dumps(sys.path)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=IMPORT_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            errors[name] = f"импорт не завершился за {IMPORT_TIMEOUT} с"
        except OSError as exc:
            errors[name] = f"не удалось запустить проверку: {exc}"
        else:
            if result.returncode != 0 or result.stdout.splitlines()[-1:] != [_IMPORT_OK]:
                detail = result.stderr.strip()[-2000:]
                errors[name] = f"код {result.returncode}: {detail or 'импорт не завершён'}"
    return errors


def missing(modules: list[str]) -> list[str]:
    """Совместимый список модулей, которые не удалось импортировать."""
    return list(import_errors(modules))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="обязательные зависимости тестов в CI")
    ap.add_argument("paths", nargs="+", type=Path, help="каталоги или файлы тестов")
    ap.add_argument("--also", nargs="*", default=[], help="модули сверх найденных")
    ap.add_argument(
        "--if-exists",
        nargs="*",
        default=[],
        type=Path,
        help="каталоги, которые проверяются, только если уже есть в дереве",
    )
    args = ap.parse_args(argv)

    absent = [path for path in args.paths if not path.exists()]
    if absent:
        print(f"нет таких путей: {', '.join(map(str, absent))}", file=sys.stderr)
        return 2
    for path in args.if_exists:
        if path.exists():
            args.paths.append(path)
        else:
            print(f"  {path}: каталога ещё нет — пропуск")

    modules = sorted(set(required_modules(args.paths)) | set(args.also))
    if not modules:
        print("зависимостей через importorskip не найдено — проверять нечего")
        return 0

    gone = import_errors(modules)
    for name in modules:
        mark = "НЕТ" if name in gone else "ok"
        print(f"  {name}: {mark}")

    if gone:
        print(
            "\nэти модули нужны тестам, но не импортируются — в CI тесты будут\n"
            "пропущены или упадут. Проверьте установку и ошибки импорта:",
            file=sys.stderr,
        )
        for name in gone:
            print(f"  {name}: {gone[name]}", file=sys.stderr)
            print(
                f"  - {name} → {APT_HINT.get(name, '<пакет неизвестен, дополните APT_HINT>')}",
                file=sys.stderr,
            )
        return 1
    print(f"все {len(modules)} зависимости на месте — тесты не будут пропущены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
