#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4 — спайк «вставка текста» (M0.S4 → platform/paste.py).

Цепочка плана #1 §3 / T1 У11/У12/У40 (T-14, T-15):

  1. запомнить активное окно (`_NET_ACTIVE_WINDOW`) и его `WM_CLASS` **до** вставки;
  2. сохранить **все** форматы `QMimeData` (копию байтов, не указатель);
  3. положить в CLIPBOARD только `text/plain` + `x-kde-passwordManagerHint=secret`;
  4. пауза 50 мс;
  5. если активное окно сменилось → **только буфер** (`clipboard-only`), XTest не шлём;
  6. комбинация по `WM_CLASS`: терминалы → `Ctrl+Shift+V`, иначе `Ctrl+V`;
  7. пауза 100 мс;
  8. восстановить исходный буфер — только если владелец всё ещё мы (`ownsClipboard`);
     исходник с `x-kde-passwordManagerHint=secret` не восстанавливается («Буфер очищен»).

Нормализация текста: `\\r\\n` → пробел, все C0/C1 (`\\x00–\\x1f`, `\\x7f–\\x9f`) удаляются.
**Enter/Return не синтезируется никогда.**

Запуск (изолированный X):
    PYTHONPATH=~/.cache/astra-voice-spikes/s4-site \\
      python3 paste.py --text 'Проверка связи, ёж.' --json out/paste_kate.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from Xlib import X, XK, display
from Xlib.ext import xtest

from PyQt5 import QtCore, QtGui, QtWidgets

TERMINAL_CLASSES = {"konsole", "fly-term", "flyterm", "xterm", "uxterm", "yakuake",
                    "alacritty", "gnome-terminal-server", "xfce4-terminal"}
KDE_HINT = "x-kde-passwordManagerHint"
DELAY_BEFORE_MS = 50     # план #1 §3 / plans.md M4
DELAY_AFTER_MS = 100


def guard_display() -> None:
    dsp = os.environ.get("DISPLAY", "")
    if os.environ.get("AV_ALLOW_DISPLAY") == "1":
        return
    if dsp in ("", ":0", ":0.0"):
        sys.stderr.write("ОТКАЗ: DISPLAY=%r — дисплей заказчика. AV_ALLOW_DISPLAY=1 для обхода.\n"
                         % dsp)
        raise SystemExit(3)


def normalize(text: str) -> str:
    """`\\r\\n` → пробел; все C0/C1 удаляются (T-15). Enter не синтезируется вовсе."""
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return "".join(ch for ch in text
                   if not (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F))


class X11(object):
    def __init__(self) -> None:
        self.d = display.Display()
        self.root = self.d.screen().root
        self.a_active = self.d.intern_atom("_NET_ACTIVE_WINDOW")

    def active_window(self):
        try:
            p = self.root.get_full_property(self.a_active, X.AnyPropertyType)
            if not p or not p.value:
                return None
            return int(p.value[0])
        except Exception:
            return None

    def wm_class(self, wid):
        if not wid:
            return None
        try:
            w = self.d.create_resource_object("window", wid)
            cls = w.get_wm_class()
            if cls:
                return cls[0].lower(), cls[1].lower()
            # окно-«рамка» KWin: спускаемся по дереву детей
            for child in w.query_tree().children:
                c = child.get_wm_class()
                if c:
                    return c[0].lower(), c[1].lower()
        except Exception:
            pass
        return None

    def keycode(self, name: str) -> int:
        return self.d.keysym_to_keycode(XK.string_to_keysym(name))

    def send_combo(self, mods, key) -> None:
        """XTest: только модификаторы + буква. Return/Enter не шлём никогда."""
        codes = [self.keycode(m) for m in mods]
        kc = self.keycode(key)
        for c in codes:
            xtest.fake_input(self.d, X.KeyPress, c)
        xtest.fake_input(self.d, X.KeyPress, kc)
        xtest.fake_input(self.d, X.KeyRelease, kc)
        for c in reversed(codes):
            xtest.fake_input(self.d, X.KeyRelease, c)
        self.d.sync()


def snapshot_mime(md: QtCore.QMimeData) -> dict:
    return {fmt: bytes(md.data(fmt)) for fmt in md.formats()}


def restore_mime(snapshot: dict) -> QtCore.QMimeData:
    md = QtCore.QMimeData()
    for fmt, data in snapshot.items():
        md.setData(fmt, QtCore.QByteArray(data))
    return md


class PasteFlow(QtCore.QObject):
    def __init__(self, args) -> None:
        super().__init__()
        self.args = args
        self.x = X11()
        self.cb = QtWidgets.QApplication.clipboard()
        self.report = {"display": os.environ.get("DISPLAY"), "steps": []}
        self.t = {}

    def log(self, name, **kw):
        kw["step"] = name
        kw["t_ms"] = round((time.monotonic() - self.t0) * 1000, 1)
        self.report["steps"].append(kw)
        print("  %-18s %s" % (name, {k: v for k, v in kw.items()
                                     if k not in ("step",)}), flush=True)

    def start(self) -> None:
        self.t0 = time.monotonic()
        self.wid = self.x.active_window()
        cls = self.x.wm_class(self.wid)
        self.report["target"] = {"window": self.wid, "wm_class": cls}
        self.is_terminal = bool(cls and (cls[0] in TERMINAL_CLASSES
                                         or cls[1] in TERMINAL_CLASSES))
        self.log("active-window", wid=self.wid, wm_class=cls, terminal=self.is_terminal)

        md = self.cb.mimeData()
        self.saved = snapshot_mime(md) if md is not None else {}
        self.saved_secret = self.saved.get(KDE_HINT, b"") == b"secret"
        self.report["clipboard_before"] = {
            "formats": sorted(self.saved.keys()),
            "text_len": len(self.saved.get("text/plain", b"")),
            "password_hint": self.saved_secret,
        }
        self.log("save-clipboard", formats=len(self.saved), secret=self.saved_secret)

        text = normalize(self.args.text)
        self.report["text_raw"] = self.args.text
        self.report["text_normalized"] = text
        self.report["stripped_controls"] = len(self.args.text) - len(text)
        out = QtCore.QMimeData()
        out.setText(text)
        out.setData(KDE_HINT, QtCore.QByteArray(b"secret"))
        self.t["set"] = time.monotonic()
        self.cb.setMimeData(out, QtGui.QClipboard.Clipboard)
        self.log("put-text", chars=len(text), hint="secret")

        QtCore.QTimer.singleShot(int(self.args.delay_before_ms), self.do_paste)

    def do_paste(self) -> None:
        now_wid = self.x.active_window()
        if now_wid != self.wid:
            self.report["mode"] = "clipboard-only"
            self.log("window-changed", was=self.wid, now=now_wid, action="clipboard-only")
            QtCore.QTimer.singleShot(DELAY_AFTER_MS, self.do_restore)
            return
        mods = ["Control_L", "Shift_L"] if self.is_terminal else ["Control_L"]
        self.report["mode"] = "ctrl+shift+v" if self.is_terminal else "ctrl+v"
        t = time.monotonic()
        self.x.send_combo(mods, "v")
        self.report["xtest_ms"] = round((time.monotonic() - t) * 1000, 2)
        self.log("xtest", combo=self.report["mode"], ms=self.report["xtest_ms"])
        QtCore.QTimer.singleShot(DELAY_AFTER_MS, self.do_restore)

    def do_restore(self) -> None:
        owns = self.cb.ownsClipboard()
        self.report["owns_clipboard"] = owns
        if not owns:
            self.log("restore-skip", reason="буфер уже не наш")
        elif self.saved_secret:
            self.cb.clear(QtGui.QClipboard.Clipboard)
            self.report["restore"] = "cleared"
            self.log("restore-clear", reason="исходник с passwordManagerHint=secret (У40)")
        elif not self.saved:
            self.cb.clear(QtGui.QClipboard.Clipboard)
            self.report["restore"] = "cleared-empty"
            self.log("restore-clear", reason="буфер был пуст")
        else:
            self.cb.setMimeData(restore_mime(self.saved), QtGui.QClipboard.Clipboard)
            self.report["restore"] = "restored"
            self.log("restore", formats=len(self.saved))
        self.report["total_ms"] = round((time.monotonic() - self.t["set"]) * 1000, 1)
        # --hold: держим процесс живым, чтобы внешний клиент (xclip) успел прочитать
        # восстановленный буфер. В X11 владелец CLIPBOARD обязан быть жив — без
        # менеджера буфера (Klipper) содержимое умирает вместе с процессом.
        QtCore.QTimer.singleShot(150 + int(self.args.hold_ms), self.finish)

    def finish(self) -> None:
        md = QtWidgets.QApplication.clipboard().mimeData()
        self.report["clipboard_after"] = {
            "formats": sorted(md.formats()) if md else [],
            "text": md.text() if md else "",
        }
        if self.args.json:
            os.makedirs(os.path.dirname(self.args.json) or ".", exist_ok=True)
            with open(self.args.json, "w", encoding="utf-8") as fh:
                json.dump(self.report, fh, ensure_ascii=False, indent=2)
        print(json.dumps({k: self.report[k] for k in
                          ("mode", "restore", "total_ms", "owns_clipboard")
                          if k in self.report}, ensure_ascii=False), flush=True)
        QtWidgets.QApplication.quit()


def selftest_normalize() -> int:
    cases = [
        ("ls\n rm -rf ~", "ls  rm -rf ~"),
        ("a\r\nb", "a b"),
        ("\x1b[31mкрасный\x1b[0m", "[31mкрасный[0m"),
        ("оп\x0fерация", "операция"),
        ("хвост\x9b", "хвост"),
        ("Проверка связи, ёж.", "Проверка связи, ёж."),
    ]
    for src, want in cases:
        got = normalize(src)
        assert got == want, (src, got, want)
        assert "\n" not in got and "\r" not in got
    print("selftest-normalize: PASS (%d кейсов, T-15)" % len(cases))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="Проверка связи, ёж.")
    ap.add_argument("--json", default="")
    ap.add_argument("--delay-before-ms", type=int, default=DELAY_BEFORE_MS,
                    help="пауза перед XTest (штатно 50; увеличиваем только в тестах)")
    ap.add_argument("--hold-ms", type=int, default=0,
                    help="держать процесс после восстановления, чтобы xclip успел прочитать")
    ap.add_argument("--selftest-normalize", action="store_true")
    args = ap.parse_args()
    if args.selftest_normalize:
        return selftest_normalize()
    guard_display()
    app = QtWidgets.QApplication(sys.argv)
    flow = PasteFlow(args)
    QtCore.QTimer.singleShot(0, flow.start)
    app.exec_()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
