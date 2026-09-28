"""Проверка доступности импортов QML без создания окон и объектов."""

import re
import sys
from pathlib import Path

from PyQt5.QtCore import QUrl
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtQml import QQmlComponent, QQmlEngine


def main() -> int:
    app = QGuiApplication([])
    engine = QQmlEngine()
    root = Path(sys.argv[1])
    files = sorted(root.rglob("*.qml"))
    bad = []
    warnings = 0
    for path in files:
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(path)))
        if component.isLoading():
            while component.isLoading():
                app.processEvents()
        for error in component.errors():
            message = error.toString()
            if re.search(r"module .* is not installed|plugin cannot be loaded", message, re.I):
                bad.append(message)
            else:
                warnings += 1
    for message in bad:
        print(message, file=sys.stderr)
    print(f"QML: {len(files)} файлов, {warnings} предупреждений, {len(bad)} ошибок импортов")
    return bool(bad)


if __name__ == "__main__":
    raise SystemExit(main())
