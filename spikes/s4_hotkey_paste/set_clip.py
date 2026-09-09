#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4 — вспомогательный владелец буфера для негативных кейсов T-14/У40.

Кладёт в CLIPBOARD текст и (опционально) `x-kde-passwordManagerHint=secret` —
так делает KeePassXC — и держит владение, пока идёт тест.

    python3 set_clip.py --text 'пароль-ГЕЛИОТРОП-7' --secret --seconds 6
"""
from __future__ import annotations

import argparse
import sys

from PyQt5 import QtCore, QtGui, QtWidgets

KDE_HINT = "x-kde-passwordManagerHint"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--secret", action="store_true")
    ap.add_argument("--seconds", type=float, default=6.0)
    args = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    md = QtCore.QMimeData()
    md.setText(args.text)
    if args.secret:
        md.setData(KDE_HINT, QtCore.QByteArray(b"secret"))
    QtWidgets.QApplication.clipboard().setMimeData(md, QtGui.QClipboard.Clipboard)
    print("set_clip: formats=%s" % md.formats(), flush=True)
    QtCore.QTimer.singleShot(int(args.seconds * 1000), app.quit)
    app.exec_()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
