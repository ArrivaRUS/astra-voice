#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборка финального лого-пака Astra Voice (знак 06 «Слоги», доводка после ⛔ G2a).

Запуск:  python3 _build-logo-pack.py
Требует: rsvg-convert, xmllint, chromium (проверка), inkscape (только для
         перегенерации кривых текста — путь уже вшит в WORDMARK_D).

Всё, что генерируется, лежит в этой же папке (design/brand/).
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ICONS = os.path.join(HERE, "icons", "hicolor")
SRC = os.path.join(HERE, "icons", "_src")

# ---------------------------------------------------------------- палитра ---
INK          = "#0E1729"   # brand.ink        — знак на светлом
INK_LIGHT    = "#F2F5FA"   # fg.1             — знак на тёмном
ACCENT       = "#12B3A0"   # brand.accent     — «слушаю», светлая тема
ACCENT_DARK  = "#2FD9C4"   # accent (dark)    — «слушаю», тёмная тема
TILE         = "#1B3A73"   # brand.primary    — плашка иконки приложения

STATES = [
    # имя         светлая     тёмная        подпись
    ("idle",       None,        None,        "покой"),
    ("listening",  "#12B3A0",   "#2FD9C4",   "слушаю"),
    ("processing", "#E8A33A",   "#F2B559",   "распознаю"),
    ("done",       "#2FA36B",   "#4FBF88",   "готово"),
    ("error",      "#D64545",   "#F0645F",   "ошибка"),
]

# ------------------------------------------------------- геометрия знака ---
# Мастер-сетка знака: bbox ровно (0,0)-(100,66).
#   строка   — прямоугольник-пилюля x=0..100, y=46..66 (толщина 20)
#   точки    — cy=12.2, растут слева направо, промежутки сужаются
# Пропорции, выведенные из мастера и переиспользуемые на всех размерах:
K_ROW   = float(os.environ.get("K_ROW", 1.23))   # длина строки / ширина ряда точек
K_R2    = float(os.environ.get("K_R2", 1.38))    # r2 / r1
K_R3    = float(os.environ.get("K_R3", 1.90))    # r3 / r1
K_G1    = float(os.environ.get("K_G1", 2.55))    # промежуток 1 в радиусах r1
K_G2    = float(os.environ.get("K_G2", 0.90))    # промежуток 2 в радиусах r1
K_VGAP  = float(os.environ.get("K_VGAP", 0.62))  # зазор точки→строка, в толщинах строки
K_OFF   = 0.20     # сдвиг первой точки от левого торца строки, в r1
# ширина ряда = 2r1 + g1 + 2*K_R2*r1 + g2 + 2*K_R3*r1 = r1 * K_ROWSPAN
K_ROWSPAN = 2 + K_G1 + 2 * K_R2 + K_G2 + 2 * K_R3
K_R1_OF_BAR = 1.0 / (K_ROW * K_ROWSPAN)                    # r1 = bar_w * это


def mark(bx, by, bw, bh, rboost=1.0):
    """Знак по левому торцу строки. Возвращает dict с готовой геометрией.
    bx,by,bw,bh — прямоугольник строки (by — её ВЕРХ)."""
    r1 = bw * K_R1_OF_BAR * rboost
    r2, r3 = r1 * K_R2, r1 * K_R3
    g1, g2 = r1 * K_G1, r1 * K_G2
    x = bx + K_OFF * r1
    cx1 = x + r1
    cx2 = cx1 + r1 + g1 + r2
    cx3 = cx2 + r2 + g2 + r3
    cy = by - K_VGAP * bh - r3
    return dict(
        bar=(bx, by, bw, bh, bh / 2.0),
        dots=[(cx1, cy, r1), (cx2, cy, r2), (cx3, cy, r3)],
        top=cy - r3, bottom=by + bh,
        row_l=x, row_r=cx3 + r3,
    )


def hand(bar, dots):
    """Геометрия, снятая вручную по пиксельной сетке (16 px)."""
    bx, by, bw, bh = bar
    return dict(bar=(bx, by, bw, bh, bh / 2.0), dots=dots,
                top=min(cy - r for _, cy, r in dots), bottom=by + bh,
                row_l=dots[0][0] - dots[0][2], row_r=dots[2][0] + dots[2][2])


# 16 px: точки 2/3/4 px по целым пикселям, промежутки 2 и 1 px (ритм 2 : 1),
# строка 14×4 — вдвое тяжелее самой крупной точки. Даунскейл сюда не годится:
# производные радиусы дают 1,9 px и точки мылятся в серую грязь.
HAND16_TRAY = hand((1.0, 9.0, 14.0, 4.0),
                   [(2.0, 5.0, 1.0), (6.5, 5.0, 1.5), (11.0, 5.0, 2.0)])
HAND16_ICON = hand((2.0, 9.0, 12.0, 3.0),
                   [(3.0, 5.0, 1.0), (6.9, 5.0, 1.3), (10.7, 5.0, 1.7)])


def master():
    """Мастер-знак: строка длиной 100, толщиной 20, bbox = (0,0)-(100, H)."""
    r3 = 100.0 * K_R1_OF_BAR * K_R3
    by = 2 * r3 + K_VGAP * 20.0
    return mark(0.0, by, 100.0, 20.0), 100.0, by + 20.0


def f(v):
    """Короткая запись числа."""
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def draw(m, ink, accent, cls_ink="", cls_acc=""):
    """SVG-тело знака: строка + точки 1,2 цветом ink, точка 3 — accent."""
    bx, by, bw, bh, rx = m["bar"]
    ci = (' class="%s"' % cls_ink) if cls_ink else ""
    ca = (' class="%s"' % cls_acc) if cls_acc else ""
    out = []
    for i, (cx, cy, r) in enumerate(m["dots"]):
        is_acc = (i == 2)
        out.append('<circle%s cx="%s" cy="%s" r="%s" fill="%s"/>'
                   % (ca if is_acc else ci, f(cx), f(cy), f(r),
                      accent if is_acc else ink))
    out.append('<rect%s x="%s" y="%s" width="%s" height="%s" rx="%s" fill="%s"/>'
               % (ci, f(bx), f(by), f(bw), f(bh), f(rx), ink))
    return "".join(out)


# ------------------------------------------------- логотип-замок (кривые) ---
# «Astra Voice», PT Root UI Medium 100, letter-spacing 3, переведено в контуры
# через inkscape --export-text-to-path. Локальная система координат текста:
# bbox (0,0)-(532.1, 70.8); высота прописных = 70; базовая линия y=70.
WORDMARK_D = open(os.path.join(HERE, "icons", "_src", "wordmark.d")).read().strip() \
    if os.path.exists(os.path.join(HERE, "icons", "_src", "wordmark.d")) else None
WM_W, WM_CAP, WM_OVERSHOOT = 532.1, 70.0, 0.8

MARK_H_IN_LOCKUP = float(os.environ.get("MH", 104))   # высота знака в замке
LOCKUP_GAP = float(os.environ.get("GAP", 32))         # просвет знак↔имя
MARK_CY = float(os.environ.get("MCY", 34))            # центр знака в коорд. текста
WM_RAW_X, WM_RAW_Y = 1.5, 130.0    # bbox контуров в исходной системе Inkscape

# Раскладка замка. Система координат текста: капитель y=0, базовая линия y=70,
# оптический низ круглых букв y=70.8. Знак центрируется по оптической середине
# имени (MARK_CY) и слегка выступает над капителью и под базовой линией.
_MM, _MW, _MH = master()
MARK_S = MARK_H_IN_LOCKUP / _MH
MARK_W = 100.0 * MARK_S
TEXT_X = MARK_W + LOCKUP_GAP
_m_top = MARK_CY - MARK_H_IN_LOCKUP / 2.0
_m_bot = MARK_CY + MARK_H_IN_LOCKUP / 2.0
_top = min(0.0, _m_top)
_bot = max(WM_CAP + WM_OVERSHOOT, _m_bot)
MARK_TOP = _m_top - _top           # верх знака в системе замка
TEXT_TOP = 0.0 - _top              # капитель имени в системе замка
LOCK_W, LOCK_H = TEXT_X + WM_W, _bot - _top


def svg_lockup(ink, accent, mono=False, media=False, pad=0.0, title="Astra Voice"):
    m = _MM
    if media:
        css = ('<style>svg{color:%s}.acc{fill:%s}'
               '@media (prefers-color-scheme:dark){svg{color:%s}.acc{fill:%s}}</style>'
               % (INK, ACCENT, INK_LIGHT, ACCENT_DARK))
        body = draw(m, "currentColor", "currentColor", cls_acc="acc")
        txt_fill = "currentColor"
    elif mono:
        css = ""
        body = draw(m, "currentColor", "currentColor")
        txt_fill = "currentColor"
    else:
        css = ""
        body = draw(m, ink, accent)
        txt_fill = ink
    W, H = LOCK_W + 2 * pad, LOCK_H + 2 * pad
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %s %s" width="%s" height="%s"'
        ' role="img" aria-label="%s">%s'
        '<g transform="translate(%s,%s)">'
        '<g transform="translate(0,%s) scale(%s)">%s</g>'
        '<path transform="translate(%s,%s)" d="%s" fill="%s"/>'
        '</g></svg>\n'
        % (f(W), f(H), f(W), f(H), title, css, f(pad), f(pad),
           f(MARK_TOP), f(MARK_S), body,
           f(TEXT_X - WM_RAW_X), f(TEXT_TOP - WM_RAW_Y), WORDMARK_D, txt_fill))


# ------------------------------------------------------------- иконки app ---
# (сторона, поле, толщина строки, верх строки, rx плашки) — 16/22/24/32 вычищены
# вручную под пиксельную сетку, 64 — мастер для 64…512.
_IT  = float(os.environ.get("IT", 0.125))   # толщина строки, доля стороны
_IL  = float(os.environ.get("IL", 0.02))    # подъём знака над центром, доля стороны
_IRX = float(os.environ.get("IRX", 0.115))  # левый край ряда точек, доля стороны
_IRW = float(os.environ.get("IRW", 0.62))   # ширина ряда точек, доля стороны
ICON_SIZES = (16, 22, 24, 32, 64)


def icon_mark(S):
    """Знак в плашке: строка ВЫХОДИТ за края плашки (обрезается ею) — так она
    читается как продолжающаяся строка текста, а не как «рот» на «лице».
    Ряд точек заметно уже и сдвинут влево — композиция намеренно асимметрична."""
    t = _IT * S
    bleed = 0.05 * S
    bx, bw = -bleed, S + 2 * bleed
    row_x, row_w = _IRX * S, _IRW * S            # ряд точек: старт и ширина
    r1 = row_w / K_ROWSPAN
    r2, r3 = r1 * K_R2, r1 * K_R3
    g1, g2 = r1 * K_G1, r1 * K_G2
    cx1 = row_x + r1
    cx2 = cx1 + r1 + g1 + r2
    cx3 = cx2 + r2 + g2 + r3
    mark_h = 2 * r3 + K_VGAP * t + t
    top = (S - mark_h) / 2.0 - _IL * S
    by = top + 2 * r3 + K_VGAP * t
    cy = by - K_VGAP * t - r3
    return dict(bar=(bx, by, bw, t, t / 2.0),
                dots=[(cx1, cy, r1), (cx2, cy, r2), (cx3, cy, r3)],
                top=top, bottom=by + t, row_l=row_x, row_r=cx3 + r3)


def svg_icon(S):
    rx = round(0.22 * S, 2)
    m = HAND16_ICON if S == 16 else icon_mark(S)
    clip = ('<clipPath id="t"><rect x="0" y="0" width="%d" height="%d" rx="%s"/></clipPath>'
            % (S, S, f(rx))) if S != 16 else ""
    g = ('<g clip-path="url(#t)">%s</g>' % draw(m, INK_LIGHT, ACCENT_DARK)) if S != 16 \
        else draw(m, INK_LIGHT, ACCENT_DARK)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d"'
        ' role="img" aria-label="Astra Voice">%s'
        '<rect x="0" y="0" width="%d" height="%d" rx="%s" fill="%s"/>%s</svg>\n'
        % (S, S, S, S, clip, S, S, f(rx), TILE, g))


# -------------------------------------------------------------- трей KDE ---
# Символьные, форма одна на все состояния, цветом отличается только точка 3.
# Соглашение KDE: <style id="current-color-scheme"> + class="ColorScheme-Text",
# Plasma подменяет содержимое этого стиля цветом темы панели.
TRAY_GEOM = {22: dict(bx=2.0, by=13.0, bw=18.0, bh=4.0)}   # 16 px — HAND16_TRAY


def svg_tray(S, light=None, dark=None, mono=False):
    if S == 16:
        m = HAND16_TRAY
    else:
        g = TRAY_GEOM[S]
        m = mark(g["bx"], g["by"], g["bw"], g["bh"])
    if mono or light is None:
        acc_css = ""
        acc_fill = "currentColor"
        acc_cls = "ColorScheme-Text"
    else:
        acc_css = ('.st{fill:%s}@media (prefers-color-scheme:dark){.st{fill:%s}}'
                   % (light, dark))
        acc_fill = light
        acc_cls = "st"
    body = []
    for i, (cx, cy, r) in enumerate(m["dots"]):
        is_acc = (i == 2)
        body.append('<circle class="%s" cx="%s" cy="%s" r="%s" fill="%s"/>'
                    % (acc_cls if is_acc else "ColorScheme-Text",
                       f(cx), f(cy), f(r),
                       acc_fill if is_acc else "currentColor"))
    bx, by, bw, bh, rx = m["bar"]
    body.append('<rect class="ColorScheme-Text" x="%s" y="%s" width="%s" height="%s"'
                ' rx="%s" fill="currentColor"/>'
                % (f(bx), f(by), f(bw), f(bh), f(rx)))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d">'
        '<style id="current-color-scheme" type="text/css">.ColorScheme-Text{color:#232629}'
        '@media (prefers-color-scheme:dark){.ColorScheme-Text{color:#eff0f1}}%s</style>'
        '%s</svg>\n' % (S, S, S, S, acc_css, "".join(body)))


# --------------------------------------------------------------- запись ----
def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def main():
    written = []
    if WORDMARK_D is None:
        sys.exit("нет icons/_src/wordmark.d — сначала переведи имя в кривые")

    # --- логотипы ---------------------------------------------------------
    written.append(w(os.path.join(HERE, "logo.svg"), svg_lockup(INK, ACCENT)))
    written.append(w(os.path.join(HERE, "logo-dark.svg"), svg_lockup(INK_LIGHT, ACCENT_DARK)))
    written.append(w(os.path.join(HERE, "logo-mono.svg"), svg_lockup(INK, ACCENT, mono=True)))
    written.append(w(os.path.join(HERE, "readme-mark.svg"),
                     svg_lockup(INK, ACCENT, media=True, pad=22.0)))

    # знак отдельно (мастер-сетка 100×66, поля нулевые)
    m = _MM
    written.append(w(os.path.join(HERE, "logo-mark.svg"),
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %s %s" width="%s" height="%s"'
        ' role="img" aria-label="Astra Voice — знак">'
        '<style>svg{color:%s}.acc{fill:%s}'
        '@media (prefers-color-scheme:dark){svg{color:%s}.acc{fill:%s}}</style>'
        '%s</svg>\n' % (f(_MW), f(_MH), f(_MW), f(_MH), INK, ACCENT, INK_LIGHT, ACCENT_DARK,
                        draw(m, "currentColor", "currentColor", cls_acc="acc"))))

    # --- иконка приложения ------------------------------------------------
    written.append(w(os.path.join(ICONS, "scalable", "apps", "astra-voice.svg"), svg_icon(64)))
    for S in ICON_SIZES:
        written.append(w(os.path.join(SRC, "icon-%d.svg" % S), svg_icon(S)))

    # --- фавикон ----------------------------------------------------------
    written.append(w(os.path.join(HERE, "favicon.svg"), svg_icon(32)))

    # --- трей -------------------------------------------------------------
    for S in (16, 22):
        for name, lt, dk, _ in STATES:
            written.append(w(os.path.join(ICONS, "%dx%d" % (S, S), "status",
                                          "astra-voice-tray-%s.svg" % name),
                             svg_tray(S, lt, dk)))
        written.append(w(os.path.join(ICONS, "%dx%d" % (S, S), "status",
                                      "astra-voice-tray-mono.svg"),
                         svg_tray(S, mono=True)))

    # --- PNG --------------------------------------------------------------
    pngs = []
    for S in (16, 22, 24, 32, 48, 64, 128, 256, 512):
        src = os.path.join(SRC, "icon-%d.svg" % S) if S in ICON_SIZES \
            else os.path.join(SRC, "icon-64.svg")
        dst = os.path.join(ICONS, "%dx%d" % (S, S), "apps", "astra-voice.png")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        subprocess.run(["rsvg-convert", "-w", str(S), "-h", str(S), "-o", dst, src], check=True)
        pngs.append(dst)
    subprocess.run(["rsvg-convert", "-w", "32", "-h", "32",
                    "-o", os.path.join(HERE, "favicon-32.png"),
                    os.path.join(HERE, "favicon.svg")], check=True)
    pngs.append(os.path.join(HERE, "favicon-32.png"))

    for p in written + pngs:
        print(os.path.relpath(p, HERE))
    print("--- %d svg, %d png" % (len(written), len(pngs)))


if __name__ == "__main__":
    main()
