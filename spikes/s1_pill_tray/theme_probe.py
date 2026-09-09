#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S1 · детект темы и сессии (прототип для core/theme.py и platform/session.py).

Источники истины (arch/plan-claude.md §13.1, design/spec.md §12.1):
  KDE  — ~/.config/kdeglobals: [General] ColorScheme, [Colors:Window] BackgroundNormal
  Fly  — ~/.fly/paletterc:     [General]/[ColorScheme] ColorScheme, BackgroundColor
Палитру Qt НЕ читаем (урок Astra Cowork: в процессе оверлея Qt-палитра врёт).

Запуск:  python3 theme_probe.py [--x]
  --x  дополнительно опросить атомы root-окна текущего $DISPLAY
       (только read-only: _NET_SUPPORTING_WM_CHECK/_NET_WM_NAME/_FLY_WM_PID/_NET_WORKAREA)
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

KDEGLOBALS = Path.home() / ".config" / "kdeglobals"
PALETTERC = Path.home() / ".fly" / "paletterc"
THEMERC = Path.home() / ".fly" / "theme" / "current.themerc"


def read_ini(path: Path) -> dict[str, dict[str, str]]:
    """Мини-парсер ini в стиле KConfig (без интерполяции, дубли — последний выигрывает)."""
    out: dict[str, dict[str, str]] = {}
    if not path.exists():
        return out
    section = ""
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out.setdefault(section, {})[k.strip()] = v.strip()
    return out


def parse_color(value: str) -> tuple[int, int, int] | None:
    """'14,23,41' или '#0E1729' → (r, g, b)."""
    if not value:
        return None
    v = value.strip()
    if v.startswith("#") and len(v) >= 7:
        try:
            return (int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16))
        except ValueError:
            return None
    parts = [p for p in re.split(r"[,\s]+", v) if p]
    if len(parts) >= 3:
        try:
            return tuple(int(float(p)) for p in parts[:3])  # type: ignore[return-value]
        except ValueError:
            return None
    return None


def luma(rgb: tuple[int, int, int]) -> float:
    """Относительная яркость 0..255 (BT.601 — как в KColorUtils «grayscale»)."""
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def scheme_is_dark(name: str) -> bool | None:
    low = (name or "").lower()
    if "dark" in low:
        return True
    if "light" in low:
        return False
    return None


def probe_kde() -> dict:
    ini = read_ini(KDEGLOBALS)
    scheme = ini.get("General", {}).get("ColorScheme", "")
    bg_raw = ini.get("Colors:Window", {}).get("BackgroundNormal", "")
    bg = parse_color(bg_raw)
    dark = scheme_is_dark(scheme)
    reason = "имя схемы"
    if dark is None and bg is not None:
        dark = luma(bg) < 128
        reason = "яркость BackgroundNormal"
    if dark is None:
        reason = "нет данных"
    return {
        "file": str(KDEGLOBALS),
        "exists": KDEGLOBALS.exists(),
        "scheme": scheme,
        "bg_raw": bg_raw,
        "bg": bg,
        "luma": round(luma(bg), 1) if bg else None,
        "dark": dark,
        "reason": reason,
    }


def probe_fly() -> dict:
    ini = read_ini(PALETTERC)
    scheme = ""
    bg_raw = ""
    # ФАКТ (машина заказчика, 2026-09-09): секция называется [Variables], не [General].
    for sec in ("Variables", "General", "ColorScheme", "Palette", ""):
        d = ini.get(sec, {})
        scheme = scheme or d.get("ColorScheme", "")
        bg_raw = bg_raw or d.get("BackgroundColor", "")
    scheme_name = Path(scheme).stem if scheme else ""
    bg = parse_color(bg_raw)
    dark = scheme_is_dark(scheme_name)
    reason = "имя схемы"
    if dark is None and bg is not None:
        dark = luma(bg) < 128
        reason = "яркость BackgroundColor"
    if dark is None:
        reason = "нет данных"
    themerc = read_ini(THEMERC)
    return {
        "file": str(PALETTERC),
        "exists": PALETTERC.exists(),
        "scheme": scheme,
        "scheme_name": scheme_name,
        "bg_raw": bg_raw,
        "bg": bg,
        "luma": round(luma(bg), 1) if bg else None,
        "dark": dark,
        "reason": reason,
        "themerc_exists": THEMERC.exists(),
        "themerc_sections": sorted(themerc.keys())[:8],
    }


def probe_x_atoms() -> dict:
    """Read-only опрос root-окна (нужен $DISPLAY). Для A-04: KDE ∥ FLY ∥ OTHER."""
    import subprocess

    def xprop(args: list[str]) -> str:
        try:
            return subprocess.run(
                ["xprop"] + args, capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except Exception as exc:  # noqa: BLE001
            return f"<ошибка: {exc}>"

    root = xprop(["-root", "_NET_SUPPORTING_WM_CHECK", "_FLY_WM_PID", "_NET_WORKAREA"])
    wm_name = ""
    m = re.search(r"window id # (0x[0-9a-fA-F]+)", root)
    if m:
        wm_name = xprop(["-id", m.group(1), "_NET_WM_NAME"])
    kind = "OTHER"
    if "_FLY_WM_PID" in root and "not found" not in root:
        kind = "FLY"
    elif "KWin" in wm_name:
        kind = "KDE"
    return {"root": root, "wm_name": wm_name, "kind": kind}


def main() -> int:
    kde = probe_kde()
    fly = probe_fly()

    def theme_word(d: dict) -> str:
        if not d["exists"]:
            return "файла нет"
        if d["dark"] is True:
            return "тёмная"
        if d["dark"] is False:
            return "светлая"
        return "неизвестно"

    print(
        "KDE: {} (ColorScheme={!r}, BackgroundNormal={!r}, luma={}, по {}), "
        "Fly: {} (ColorScheme={!r}, BackgroundColor={!r}, luma={}, по {})".format(
            theme_word(kde), kde["scheme"], kde["bg_raw"], kde["luma"], kde["reason"],
            theme_word(fly), fly["scheme_name"], fly["bg_raw"], fly["luma"], fly["reason"],
        )
    )
    print(
        "XDG_CURRENT_DESKTOP={!r}  DESKTOP_SESSION={!r}  "
        "QT_QPA_PLATFORMTHEME={!r}  QT_QUICK_CONTROLS_STYLE={!r}".format(
            os.environ.get("XDG_CURRENT_DESKTOP", ""),
            os.environ.get("DESKTOP_SESSION", ""),
            os.environ.get("QT_QPA_PLATFORMTHEME", ""),
            os.environ.get("QT_QUICK_CONTROLS_STYLE", ""),
        )
    )
    print("файлы: kdeglobals={} paletterc={} current.themerc={}".format(
        kde["exists"], fly["exists"], fly["themerc_exists"]))
    if kde["exists"] and fly["exists"] and kde["dark"] != fly["dark"]:
        print("ВНИМАНИЕ: kdeglobals и paletterc расходятся — источник выбирается по SessionKind "
              "(plan-claude §13.1)")

    if "--x" in sys.argv:
        x = probe_x_atoms()
        print("--- root-атомы ($DISPLAY={}) ---".format(os.environ.get("DISPLAY", "")))
        print(x["root"] or "<пусто>")
        print("WM_NAME:", x["wm_name"] or "<пусто>")
        print("SessionKind по атомам:", x["kind"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
