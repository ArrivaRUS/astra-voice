#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4 — детерминированная мишень для вставки (аналог xvfb-теста из плана M4).

Окно Qt с `QPlainTextEdit`; каждые 200 мс сбрасывает своё содержимое в файл,
чтобы факт вставки читался с диска, а не «по событию». Заодно пишет момент
первого непустого содержимого — это и есть «отпустил → текст на экране».

    python3 recv_qt.py --out out/recv.txt --seconds 8
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from PyQt5 import QtCore, QtWidgets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/recv.txt")
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--title", default="av-s4-target")
    args = ap.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    w = QtWidgets.QPlainTextEdit()
    w.setWindowTitle(args.title)
    w.resize(700, 220)
    w.show()
    w.setFocus()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    open(args.out, "w", encoding="utf-8").close()
    state = {"first": None, "t0": time.monotonic()}

    def dump() -> None:
        txt = w.toPlainText()
        if txt and state["first"] is None:
            state["first"] = round((time.monotonic() - state["t0"]) * 1000, 1)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(txt)

    t = QtCore.QTimer()
    t.timeout.connect(dump)
    t.start(200)
    QtCore.QTimer.singleShot(int(args.seconds * 1000), app.quit)
    app.exec_()
    dump()
    print("recv_qt: chars=%d first_nonempty_ms=%s file=%s"
          % (len(w.toPlainText()), state["first"], args.out), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
