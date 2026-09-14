"""Мишень из S4 recv_qt.py: QPlainTextEdit с дампом каждые 200 мс."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PyQt5 import QtCore, QtWidgets


def write_atomic(path: Path, text: str) -> None:
    """Читатель никогда не увидит частично записанный дамп."""
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(text, encoding="utf-8")
    pending.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    args = parser.parse_args()
    app = QtWidgets.QApplication(["astra-paste-target", "-name", "astra-paste-target"])
    window = QtWidgets.QPlainTextEdit()
    window.setWindowTitle("Astra Paste Target")
    window.resize(700, 220)
    window.show()
    window.setFocus()
    dumps = 0

    def dump() -> None:
        nonlocal dumps
        write_atomic(args.out, window.toPlainText())
        dumps += 1
        write_atomic(args.ready, json.dumps({"window": int(window.winId()), "dumps": dumps}))

    timer = QtCore.QTimer()
    timer.timeout.connect(dump)
    timer.start(200)
    app.exec_()
    dump()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
