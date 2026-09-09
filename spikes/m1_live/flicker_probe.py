#!/usr/bin/env python3
"""Запускает QML-файл в чистом QQmlApplicationEngine (без app.py) — для пробы «моргания».

    python3 spikes/m1_live/flicker_probe.py                      # пробник flicker_probe.qml
    python3 spikes/m1_live/flicker_probe.py qml/Main.qml         # окно приложения без Python-обвязки
    QSG_RENDER_LOOP=basic     python3 spikes/m1_live/flicker_probe.py
    QT_QUICK_BACKEND=software python3 spikes/m1_live/flicker_probe.py

`qmlscene`/`qml` на машине заказчика отсутствуют (только обёртка qtchooser), поэтому свой раннер.
Стиль контролов принудительно `Default`, как в лаунчере `scripts/astra-voice`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Default")

from PyQt5.QtCore import QUrl  # noqa: E402
from PyQt5.QtQml import QQmlApplicationEngine  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402


def main() -> int:
    here = Path(__file__).resolve().parent
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else here / "flicker_probe.qml"
    app = QApplication(sys.argv[:1])
    engine = QQmlApplicationEngine()
    engine.load(QUrl.fromLocalFile(str(target)))
    if not engine.rootObjects():
        sys.stderr.write(f"QML не загрузился: {target}\n")
        return 1
    return int(app.exec_())


if __name__ == "__main__":
    raise SystemExit(main())
