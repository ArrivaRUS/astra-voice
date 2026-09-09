#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4 — спайк «глобальный хоткей» (M0.S4 → platform/hotkey.py, platform/x11.py).

Проверяет допущения синтез-плана §4 / плана #1 §3 (`platform/x11.py`, `platform/hotkey.py`):

  * `XGrabKey` на root по всем 8 комбинациям масок Lock/Num/Scroll (маски Num/Scroll
    определяются из `get_modifier_mapping()`, а не хардкодом Mod2/Mod5);
  * автомат PTT `Idle → Recording → Processing` с фильтром автоповтора
    (KeyRelease+KeyPress с одинаковым `time` и keycode), порогом 0,3 с и лимитом 120 с;
  * временный grab `Escape` — только на время записи (T1 У21/T-24);
  * `BadAccess` на занятой комбинации распознаётся и не роняет процесс;
  * события читаются через `QSocketNotifier` на fd `Display` — **без потоков**.

Запуск (только в изолированном X, ср. spikes/s1_pill_tray/README.md):

    PYTHONPATH=~/.cache/astra-voice-spikes/s4-site \
      python3 hotkey.py --grab ctrl+space --probe alt+F2 --seconds 20 --json out/hotkey.json

Защита: как и в S1, отказываемся стартовать на дисплее заказчика (`:0` / пустой),
обход — `AV_ALLOW_DISPLAY=1` (только для живого этапа после «ок» заказчика).
"""

from __future__ import annotations

import argparse
import json
import os
import select
import sys
import time

from Xlib import X, XK, display, error
from Xlib.ext import xtest  # noqa: F401  (проверяем наличие расширения на старте)

from PyQt5 import QtCore

# --- пороги из docs/plans.md M0.S4 / M4 -------------------------------------
PTT_THRESHOLD_S = 0.3      # короче — «тап» (toggle), длиннее — удержание (PTT)
RECORD_LIMIT_S = 120.0     # лимит записи (настройка 30–300 в проде)
LOOKAHEAD_S = 0.002        # окно ожидания пары KeyRelease+KeyPress автоповтора

MOD_ALIASES = {
    "ctrl": X.ControlMask, "control": X.ControlMask,
    "shift": X.ShiftMask,
    "alt": X.Mod1Mask, "meta": X.Mod1Mask,
    "super": X.Mod4Mask, "win": X.Mod4Mask,
}
KEY_ALIASES = {"space": "space", "esc": "Escape", "escape": "Escape"}


def guard_display() -> None:
    dsp = os.environ.get("DISPLAY", "")
    if os.environ.get("AV_ALLOW_DISPLAY") == "1":
        return
    if dsp in ("", ":0", ":0.0"):
        sys.stderr.write(
            "ОТКАЗ: DISPLAY=%r — это дисплей заказчика. Спайк рассчитан на изолированный X.\n"
            "Обход для живого этапа: AV_ALLOW_DISPLAY=1\n" % dsp)
        raise SystemExit(3)


class X11Keys:
    """Тонкая обёртка над Display: маски, grab/ungrab, XTest. Состояния нет, кроме Display."""

    def __init__(self) -> None:
        self.d = display.Display()
        self.root = self.d.screen().root
        self.lock_masks = self._lock_masks()

    # --- маски Lock/Num/Scroll -------------------------------------------
    def _mask_of_keysym(self, name: str) -> int:
        ks = XK.string_to_keysym(name)
        if ks == 0:
            return 0
        codes = {kc for kc in (self.d.keysym_to_keycode(ks),) if kc}
        # keysym_to_keycode отдаёт один keycode; для надёжности пройдём всю карту
        mapping = self.d.get_modifier_mapping()
        for idx, keycodes in enumerate(mapping):
            for kc in keycodes:
                if kc and (kc in codes or self.d.keycode_to_keysym(kc, 0) == ks):
                    return 1 << idx
        return 0

    def _lock_masks(self) -> dict:
        num = self._mask_of_keysym("Num_Lock")
        scroll = self._mask_of_keysym("Scroll_Lock")
        return {"Lock": X.LockMask, "Num_Lock": num, "Scroll_Lock": scroll}

    def mask_variants(self, base: int) -> list:
        """Все комбинации «залипающих» масок поверх базовой (обычно 8 штук)."""
        extra = [m for m in (X.LockMask, self.lock_masks["Num_Lock"],
                             self.lock_masks["Scroll_Lock"]) if m]
        out = [base]
        for m in extra:
            out += [v | m for v in out]
        # уникальные, порядок сохраняем
        seen, uniq = set(), []
        for v in out:
            if v not in seen:
                seen.add(v)
                uniq.append(v)
        return uniq

    # --- разбор комбинации ------------------------------------------------
    def parse_combo(self, combo: str):
        mods, key = 0, None
        for part in combo.split("+"):
            p = part.strip()
            low = p.lower()
            if low in MOD_ALIASES:
                mods |= MOD_ALIASES[low]
            else:
                key = KEY_ALIASES.get(low, p)
        if key is None:
            raise ValueError("нет клавиши в комбинации %r" % combo)
        ks = XK.string_to_keysym(key)
        if ks == 0:
            raise ValueError("неизвестный keysym %r" % key)
        kc = self.d.keysym_to_keycode(ks)
        if kc == 0:
            raise ValueError("keysym %r нет в текущей раскладке" % key)
        return mods, kc, key

    # --- grab / ungrab ----------------------------------------------------
    def grab(self, keycode: int, base_mask: int):
        """Возвращает (ok, [(mask, err_name)]). BadAccess не роняет процесс."""
        results = []
        for mask in self.mask_variants(base_mask):
            err = error.CatchError(error.BadAccess)
            self.root.grab_key(keycode, mask, 1, X.GrabModeAsync, X.GrabModeAsync,
                               onerror=err)
            self.d.sync()
            e = err.get_error()
            results.append((mask, None if e is None else type(e).__name__))
        ok = all(name is None for _, name in results)
        if not ok:  # частичный захват не оставляем
            self.ungrab(keycode, base_mask)
        return ok, results

    def ungrab(self, keycode: int, base_mask: int) -> None:
        for mask in self.mask_variants(base_mask):
            self.root.ungrab_key(keycode, mask)
        self.d.sync()

    # --- XTest ------------------------------------------------------------
    def fake_key(self, keycode: int, press: bool) -> None:
        xtest.fake_input(self.d, X.KeyPress if press else X.KeyRelease, keycode)
        self.d.sync()


class PttMachine(QtCore.QObject):
    """Автомат PTT/toggle. Состояния: idle · recording · processing."""

    def __init__(self, log: list) -> None:
        super().__init__()
        self.state = "idle"
        self.mode = None          # 'ptt' | 'toggle'
        self.press_ts = 0.0
        self.log = log
        self.limit_timer = QtCore.QTimer()
        self.limit_timer.setSingleShot(True)
        self.limit_timer.timeout.connect(lambda: self.stop("limit"))
        self.on_state = lambda *_: None

    def _emit(self, reason: str) -> None:
        self.log.append({"t": round(time.monotonic(), 4), "state": self.state,
                         "mode": self.mode, "reason": reason})
        self.on_state(self.state, reason)

    def key_press(self, ts: float) -> None:
        if self.state == "idle":
            self.press_ts = ts
            self.state = "recording"
            self.mode = "ptt"          # уточним на отпускании
            self.limit_timer.start(int(RECORD_LIMIT_S * 1000))
            self._emit("press")
        elif self.state == "recording" and self.mode == "toggle":
            self.stop("toggle-off")

    def key_release(self, ts: float) -> None:
        if self.state != "recording":
            return
        held = ts - self.press_ts
        if held < PTT_THRESHOLD_S and self.mode == "ptt":
            self.mode = "toggle"       # короткий тап → режим переключателя
            self._emit("tap→toggle (%.3f с)" % held)
            return
        self.stop("release (%.3f с)" % held)

    def cancel(self) -> None:
        if self.state in ("recording", "processing"):
            self.limit_timer.stop()
            self.state = "idle"
            self.mode = None
            self._emit("escape-cancel")

    def stop(self, reason: str) -> None:
        if self.state != "recording":
            return
        self.limit_timer.stop()
        self.state = "processing"
        self._emit(reason)
        # в спайке «обработка» мгновенная
        self.state = "idle"
        self.mode = None
        self._emit("done")


class HotkeySpike(QtCore.QObject):
    def __init__(self, args) -> None:
        super().__init__()
        self.args = args
        self.x = X11Keys()
        self.report = {
            "display": os.environ.get("DISPLAY"),
            "lock_masks": {k: hex(v) for k, v in self.x.lock_masks.items()},
            "events": [],
            "fsm": [],
        }
        self.fsm = PttMachine(self.report["fsm"])
        self.fsm.on_state = self._on_state
        self.esc_grabbed = False

        self.mods, self.keycode, keyname = self.x.parse_combo(args.grab)
        self.esc_mods, self.esc_keycode, _ = self.x.parse_combo("Escape")
        variants = self.x.mask_variants(self.mods)
        ok, res = self.x.grab(self.keycode, self.mods)
        self.report["grab"] = {
            "combo": args.grab, "keysym": keyname, "keycode": self.keycode,
            "base_mask": hex(self.mods), "variants": [hex(m) for m in variants],
            "variant_count": len(variants),
            "ok": ok,
            "errors": [[hex(m), e] for m, e in res if e],
        }
        print("grab %s → keycode=%d масок=%d ok=%s" %
              (args.grab, self.keycode, len(variants), ok), flush=True)
        if not ok:
            print("  BadAccess: комбинация занята другим клиентом", flush=True)

        if args.probe:
            self.report["probe"] = self._probe(args.probe)

        self.notifier = QtCore.QSocketNotifier(self.x.d.fileno(),
                                               QtCore.QSocketNotifier.Read, self)
        self.notifier.activated.connect(self._on_x_fd)
        self.report["threads_used"] = False

    # --- BadAccess на занятой комбинации ---------------------------------
    def _probe(self, combo: str) -> dict:
        """Второй клиент занимает комбинацию → наш grab обязан вернуть BadAccess."""
        other = display.Display()
        mods, kc, name = self.x.parse_combo(combo)
        out = {"combo": combo, "keycode": kc}
        for mask in self.x.mask_variants(mods):
            other.screen().root.grab_key(kc, mask, 1, X.GrabModeAsync, X.GrabModeAsync)
        other.sync()
        ok, res = self.x.grab(kc, mods)
        out["free_before"] = False
        out["our_grab_ok"] = ok
        out["errors"] = [[hex(m), e] for m, e in res if e]
        for mask in self.x.mask_variants(mods):
            other.screen().root.ungrab_key(kc, mask)
        other.sync()
        ok2, res2 = self.x.grab(kc, mods)
        out["after_release_ok"] = ok2
        if ok2:
            self.x.ungrab(kc, mods)
        other.close()
        print("probe %s: занят другим клиентом → наш grab ok=%s (%s); после снятия ok=%s"
              % (combo, ok, out["errors"], ok2), flush=True)
        return out

    # --- временный grab Escape -------------------------------------------
    def _on_state(self, state: str, reason: str) -> None:
        print("  [%s] %s" % (state, reason), flush=True)
        if state == "recording" and not self.esc_grabbed:
            ok, _ = self.x.grab(self.esc_keycode, 0)
            self.esc_grabbed = ok
            self.report.setdefault("escape", []).append({"action": "grab", "ok": ok})
            print("  Escape: grab ok=%s" % ok, flush=True)
        elif state == "idle" and self.esc_grabbed:
            self.x.ungrab(self.esc_keycode, 0)
            self.esc_grabbed = False
            self.report.setdefault("escape", []).append({"action": "ungrab"})
            print("  Escape: ungrab", flush=True)

    # --- чтение событий без потоков --------------------------------------
    def _drain(self) -> list:
        evs = []
        while self.x.d.pending_events():
            evs.append(self.x.d.next_event())
        return evs

    def _lookahead(self) -> list:
        """Ждём ≤2 мс пару автоповтора, если она не попала в тот же read."""
        select.select([self.x.d.fileno()], [], [], LOOKAHEAD_S)
        return self._drain()

    def _on_x_fd(self) -> None:
        evs = self._drain()
        i = 0
        while i < len(evs):
            ev = evs[i]
            nxt = evs[i + 1] if i + 1 < len(evs) else None
            if (ev.type == X.KeyRelease and ev.detail == self.keycode and nxt is None):
                more = self._lookahead()
                if more:
                    evs.extend(more)
                    nxt = evs[i + 1]
            if (ev.type == X.KeyRelease and nxt is not None
                    and nxt.type == X.KeyPress
                    and nxt.detail == ev.detail and nxt.time == ev.time):
                self.report["events"].append(
                    {"type": "autorepeat-dropped", "keycode": ev.detail, "time": ev.time})
                i += 2
                continue
            self._handle(ev)
            i += 1

    def _handle(self, ev) -> None:
        if ev.type not in (X.KeyPress, X.KeyRelease):
            return
        kind = "KeyPress" if ev.type == X.KeyPress else "KeyRelease"
        self.report["events"].append({"type": kind, "keycode": ev.detail,
                                      "state": ev.state, "time": ev.time})
        now = time.monotonic()
        if ev.detail == self.keycode:
            if ev.type == X.KeyPress:
                self.fsm.key_press(now)
            else:
                self.fsm.key_release(now)
        elif ev.detail == self.esc_keycode and self.esc_grabbed and ev.type == X.KeyPress:
            self.fsm.cancel()

    def finish(self, path: str) -> None:
        self.x.ungrab(self.keycode, self.mods)
        if self.esc_grabbed:
            self.x.ungrab(self.esc_keycode, 0)
        pressed = sum(1 for e in self.report["events"] if e["type"] == "KeyPress")
        dropped = sum(1 for e in self.report["events"] if e["type"] == "autorepeat-dropped")
        self.report["summary"] = {"key_presses": pressed, "autorepeat_dropped": dropped}
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.report, fh, ensure_ascii=False, indent=2)
        print(json.dumps(self.report["summary"], ensure_ascii=False), flush=True)


def selftest_fsm() -> int:
    """Автомат на синтетических событиях — без X (для CI/unit)."""
    _app = QtCore.QCoreApplication(sys.argv)   # ссылку держим: нужен для QTimer лимита
    assert _app is not None
    log = []
    m = PttMachine(log)
    m.key_press(0.0)
    m.key_release(1.0)          # удержание 1 с → PTT
    assert m.state == "idle" and any("release" in r["reason"] for r in log), log
    log2 = []
    m2 = PttMachine(log2)
    m2.key_press(0.0)
    m2.key_release(0.1)         # тап < 0,3 с → toggle, запись продолжается
    assert m2.state == "recording" and m2.mode == "toggle", (m2.state, m2.mode)
    m2.key_press(2.0)           # второй тап → стоп
    assert m2.state == "idle", m2.state
    log3 = []
    m3 = PttMachine(log3)
    m3.key_press(0.0)
    m3.cancel()
    assert m3.state == "idle" and log3[-1]["reason"] == "escape-cancel", log3
    print("selftest-fsm: PASS (ptt, tap→toggle, escape-cancel)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grab", default="ctrl+space")
    ap.add_argument("--probe", default=None, help="комбинация для проверки BadAccess")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--json", default="")
    ap.add_argument("--selftest-fsm", action="store_true")
    args = ap.parse_args()

    if args.selftest_fsm:
        return selftest_fsm()

    guard_display()
    app = QtCore.QCoreApplication(sys.argv)
    spike = HotkeySpike(args)
    QtCore.QTimer.singleShot(int(args.seconds * 1000), app.quit)
    app.exec_()
    spike.finish(args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
