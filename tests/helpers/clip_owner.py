"""Владелец CLIPBOARD из S4 set_clip.py, включая метку секрета KeePassXC."""

from __future__ import annotations

import argparse
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", required=True)
    parser.add_argument("--html")
    parser.add_argument("--secret", action="store_true")
    parser.add_argument("--ready", type=Path, required=True)
    args = parser.parse_args()
    app = QtWidgets.QApplication(["astra-clip-owner"])
    mime = QtCore.QMimeData()
    mime.setText(args.text)
    if args.html is not None:
        mime.setHtml(args.html)
    if args.secret:
        mime.setData("x-kde-passwordManagerHint", QtCore.QByteArray(b"secret"))
    QtWidgets.QApplication.clipboard().setMimeData(mime, QtGui.QClipboard.Clipboard)

    def ready() -> None:
        args.ready.write_text("ready", encoding="utf-8")

    QtCore.QTimer.singleShot(0, ready)
    app.exec_()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
