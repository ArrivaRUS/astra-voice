#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S1 · пилюля-оверлей (прототип для ui/pill.py + qml/Pill.qml).

Все числа — из design/tokens.json (`component.pill.*`, `color.fixed.*`) и
design/spec.md §8. Ни одного значения «на глаз»: см. TOKENS ниже, каждый ключ
подписан источником.

Два бэкенда:
  --backend widget  — QWidget + QPainter (работает на стоковой ALSE: python3-pyqt5)
  --backend qml     — QQmlApplicationEngine + qml/Pill.qml (требует python3-pyqt5.qtquick,
                      на стоковой машине НЕ установлен — plan-claude §12; deb поставит в M1)
  --backend auto    — qml, если импортируется PyQt5.QtQuick, иначе widget (по умолчанию)

Окно: Qt.Tool | FramelessWindowHint | WindowStaysOnTopHint | WindowDoesNotAcceptFocus
      + EWMH руками (x11ewmh): тип NOTIFICATION, состояния ABOVE/SKIP_TASKBAR/SKIP_PAGER,
      _NET_WM_USER_TIME=0, для Fly — _FLY_WM_WINDOW_MAP_ANIMATION=0/_FLY_WM_FADE_SHOW=0.
      requestActivate/activateWindow НЕ вызываются нигде.

Запуск (только под Xvfb, см. README):
  python3 pill.py --state listening --seconds 5
  python3 pill.py --cycle --seconds 8
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOKENS_PATH = REPO / "design" / "tokens.json"


# --------------------------------------------------------------------------
# Токены: читаем из design/tokens.json, ничего не хардкодим
# --------------------------------------------------------------------------
def load_tokens() -> dict:
    data = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    pill = data["component"]["pill"]
    fixed = data["color"]["fixed"]

    def px(node) -> int:
        v = node["value"]
        return int(round(float(str(v).replace("px", ""))))

    return {
        # component.pill.*
        "h": px(pill["h"]),                              # 36
        "radius": px(pill["radius"]),                    # 18
        "min_w": px(pill["min-w"]),                      # 172
        "max_w": px(pill["max-w"]),                      # 320
        "pad_x": 10,                                     # padding "0 10px"
        "gap": px(pill["gap"]),                          # 9
        "bars": int(pill["bars"]["value"]),              # 9
        "bar_w": px(pill["bar-w"]),                      # 6
        "bar_gap": px(pill["bar-gap"]),                  # 3
        "bar_radius": px(pill["bar-radius"]),            # 3
        "bar_area_h": px(pill["bar-area-h"]),            # 20
        "bars_block_w": px(pill["bars-block-w"]),        # 78
        "bar_h_min": px(pill["bar-h-min"]),              # 3
        "bar_h_max": px(pill["bar-h-max"]),              # 20
        "bar_h_flat": px(pill["bar-h-flat"]),            # 4
        "dots_count": int(pill["processing-dots"]["value"]["count"]),      # 3
        "dots_w": px({"value": pill["processing-dots"]["value"]["w"]}),    # 5
        "dots_h": list(pill["processing-dots"]["value"]["heights"]),       # [5,9,5]
        "close_size": px({"value": pill["close-btn"]["value"]["size"]}),   # 22
        "offset": 48,                                    # position.offset, spec §8.4 У10
        # color.fixed.*
        "c_bg": fixed["pill-bg"]["value"],
        "c_fg": fixed["pill-fg"]["value"],
        "c_close_bg": fixed["pill-close-bg"]["value"],
        "c_close_fg": fixed["pill-close-fg"]["value"],
        "c_bar_live": fixed["pill-bar-live"]["value"],
        "c_bar_flat": fixed["pill-bar-flat"]["value"],
        "c_processing": fixed["pill-processing"]["value"],
        "c_done": fixed["pill-done"]["value"],
        "c_error": fixed["pill-error"]["value"],
        # spec §8.2: текст 12.5, ключевое слово вес 700; семейство — font.family.ui
        "font_px": 12.5,
        "font_family": data["font"]["family"]["ui"]["value"].split(",")[0].strip("'\""),
        "icon_stroke": 1.5,
        "icon_16": 16,
    }


T = load_tokens()

# spec §8.4 — тексты состояний (в спайке 4 из 12)
STATE_TEXT = {
    "listening": "Слушаю",
    "processing": "Распознаю…",
    "done": "Готово",
    "error": "Микрофон недоступен",
}
STATES = list(STATE_TEXT)

# spec §8.3, эталонный кадр из макета (_base.py:344)
REF_LEVELS = [7, 12, 18, 20, 14, 9, 15, 11, 6]


def qcolor(css: str):
    """'rgba(14,23,41,.94)' | '#F2F5FA' → QColor."""
    from PyQt5.QtGui import QColor

    s = css.strip()
    if s.startswith("rgba("):
        parts = [p.strip() for p in s[5:-1].split(",")]
        r, g, b = (int(p) for p in parts[:3])
        a = float(parts[3])
        return QColor(r, g, b, int(round(a * 255)))
    if s.startswith("rgb("):
        r, g, b = (int(p.strip()) for p in s[4:-1].split(","))
        return QColor(r, g, b)
    return QColor(s)


# --------------------------------------------------------------------------
# Общая геометрия/позиционирование (одинаково для обоих бэкендов)
# --------------------------------------------------------------------------
class Placement:
    """Позиция пилюли: низ по центру РАБОЧЕЙ области монитора активного окна, 48 px.

    spec §8.4 У10 + синтез Р7: основной источник — QScreen.availableGeometry;
    расхождение с _NET_WORKAREA (struts) → берём struts.
    """

    def __init__(self, x11=None, verbose: bool = True):
        self.x11 = x11
        self.verbose = verbose
        self.report: dict = {}

    def screen_for_active_window(self):
        from PyQt5.QtGui import QGuiApplication
        from PyQt5.QtCore import QPoint

        active = 0
        geom = None
        if self.x11 is not None:
            active = self.x11.active_window()
            geom = self.x11.window_geometry(active) if active else None
        self.report["active_window"] = hex(active) if active else "нет"
        if geom:
            cx = geom[0] + geom[2] // 2
            cy = geom[1] + geom[3] // 2
            scr = QGuiApplication.screenAt(QPoint(cx, cy))
            if scr is not None:
                self.report["screen_source"] = "экран активного окна"
                return scr
        self.report["screen_source"] = "основной монитор (активного окна нет)"
        return QGuiApplication.primaryScreen()

    def work_area(self, screen):
        """Возвращает (x, y, w, h) рабочей области и пишет расхождение в отчёт."""
        ag = screen.availableGeometry()
        avail = (ag.x(), ag.y(), ag.width(), ag.height())
        self.report["availableGeometry"] = avail
        struts = None
        if self.x11 is not None:
            wa = self.x11.net_workarea()
            if len(wa) >= 4:
                struts = (wa[0], wa[1], wa[2], wa[3])
        self.report["_NET_WORKAREA"] = struts
        if struts and struts != avail:
            self.report["Р7"] = "РАСХОЖДЕНИЕ → берём _NET_WORKAREA (struts)"
            return struts
        self.report["Р7"] = ("совпадает" if struts
                             else "_NET_WORKAREA не опубликован WM — сверку struts переносим "
                                  "на живой прогон")
        return avail

    def place(self, win, win_w: int, win_h: int, pill_h: int, margin: int) -> tuple[int, int]:
        screen = self.screen_for_active_window()
        wx, wy, ww, wh = self.work_area(screen)
        x = wx + (ww - win_w) // 2
        # видимый низ пилюли = wy + wh - offset  (spec §8.4: 48 от нижнего края рабочей области)
        y = wy + wh - T["offset"] - pill_h - margin
        self.report["позиция окна"] = (x, y, win_w, win_h)
        self.report["видимый низ пилюли"] = y + margin + pill_h
        self.report["низ рабочей области"] = wy + wh
        return x, y


# --------------------------------------------------------------------------
# Рисование (QPainter) — общее для widget-бэкенда
# --------------------------------------------------------------------------
def content_width(painter_fm, state: str) -> tuple[int, int, int]:
    """(натуральная ширина пилюли, ширина левого блока, ширина текста)."""
    if state == "listening":
        lead = T["bars_block_w"]
        trail = T["gap"] + T["close_size"]
    elif state == "processing":
        lead = T["dots_count"] * T["dots_w"] + (T["dots_count"] - 1) * T["bar_gap"]
        trail = 0
    elif state == "done":
        lead = T["icon_16"]
        trail = 0
    else:  # error — «›» справа (spec §8.4 п.11)
        lead = T["icon_16"]
        trail = T["gap"] + 8
    text_w = painter_fm.horizontalAdvance(STATE_TEXT[state])
    natural = T["pad_x"] + lead + T["gap"] + text_w + trail + T["pad_x"]
    return natural, lead, text_w


def clamp_width(natural: int) -> int:
    return max(T["min_w"], min(T["max_w"], natural))


def draw_pill(p, rect, state: str, levels, phase: float, font):
    """rect — QRect самой пилюли (без поля под тень)."""
    from PyQt5.QtCore import QPointF, QRectF, Qt
    from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen

    p.setRenderHint(QPainter.Antialiasing, True)

    # тень 0 6px 18px rgba(0,0,0,.4) — spec §8.2
    for i in range(18, 0, -3):
        sh = QColor(0, 0, 0, int(0.4 * 255 * (1 - i / 18.0) * 0.20))
        p.setPen(Qt.NoPen)
        p.setBrush(sh)
        p.drawRoundedRect(
            QRectF(rect.x() - i / 2.0, rect.y() + 6 - i / 2.0,
                   rect.width() + i, rect.height() + i),
            T["radius"] + i / 2.0, T["radius"] + i / 2.0)

    # подложка
    p.setPen(Qt.NoPen)
    p.setBrush(qcolor(T["c_bg"]))
    p.drawRoundedRect(QRectF(rect), T["radius"], T["radius"])

    p.setFont(font)
    fm = p.fontMetrics()
    natural, lead, text_w = content_width(fm, state)
    # при клампе до min ширины содержимое центрируется
    x = rect.x() + T["pad_x"] + max(0, (rect.width() - natural) // 2)
    cy = rect.y() + rect.height() / 2.0

    if state == "listening":
        base_y = cy + T["bar_area_h"] / 2.0
        for i in range(T["bars"]):
            lvl = levels[i]
            h = max(T["bar_h_min"], round(lvl * T["bar_h_max"]))
            bx = x + i * (T["bar_w"] + T["bar_gap"])
            p.setBrush(qcolor(T["c_bar_live"]))
            p.drawRoundedRect(QRectF(bx, base_y - h, T["bar_w"], h),
                              T["bar_radius"], T["bar_radius"])
        x += T["bars_block_w"]
    elif state == "processing":
        # 3 точки 5 px, высоты 5/9/5, пульсируют (spec §8.4 п.6)
        for i in range(T["dots_count"]):
            k = 0.65 + 0.35 * (0.5 + 0.5 * math.sin(phase * 2 * math.pi + i * 0.7))
            h = T["dots_h"][i] * k
            dx = x + i * (T["dots_w"] + T["bar_gap"])
            p.setBrush(qcolor(T["c_processing"]))
            p.drawRoundedRect(QRectF(dx, cy - h / 2.0, T["dots_w"], h),
                              T["dots_w"] / 2.0, T["dots_w"] / 2.0)
        x += lead
    elif state == "done":
        draw_glyph(p, "check", x, cy, T["icon_16"], qcolor(T["c_done"]))
        x += T["icon_16"]
    else:
        draw_glyph(p, "alert", x, cy, T["icon_16"], qcolor(T["c_error"]))
        x += T["icon_16"]

    x += T["gap"]
    p.setPen(qcolor(T["c_error"] if state == "error" else T["c_fg"]))
    p.drawText(int(x), int(cy - fm.height() / 2.0), text_w + 2, fm.height(),
               Qt.AlignLeft | Qt.AlignVCenter, STATE_TEXT[state])
    x += text_w

    if state == "listening":
        x += T["gap"]
        d = T["close_size"]
        p.setPen(Qt.NoPen)
        p.setBrush(qcolor(T["c_close_bg"]))
        p.drawEllipse(QRectF(x, cy - d / 2.0, d, d))
        pen = QPen(qcolor(T["c_close_fg"]))
        pen.setWidthF(T["icon_stroke"])
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        r = 4.0
        ccx, ccy = x + d / 2.0, cy
        p.drawLine(QPointF(ccx - r, ccy - r), QPointF(ccx + r, ccy + r))
        p.drawLine(QPointF(ccx + r, ccy - r), QPointF(ccx - r, ccy + r))
    elif state == "error":
        x += T["gap"]
        pen = QPen(qcolor(T["c_error"]))
        pen.setWidthF(T["icon_stroke"])
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        path.moveTo(x, cy - 4)
        path.lineTo(x + 4, cy)
        path.lineTo(x, cy + 4)
        p.drawPath(path)


def draw_glyph(p, name: str, x: float, cy: float, size: int, color):
    """Глифы набора: viewBox 0 0 16 16, обводка 1.5, fill none (spec §8.4)."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPainterPath, QPen

    k = size / 16.0
    ox, oy = x, cy - size / 2.0
    pen = QPen(color)
    pen.setWidthF(T["icon_stroke"] * k)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    path = QPainterPath()
    if name == "check":
        path.moveTo(ox + 3.5 * k, oy + 8.5 * k)
        path.lineTo(ox + 6.5 * k, oy + 11.5 * k)
        path.lineTo(ox + 12.5 * k, oy + 4.5 * k)
    else:  # alert
        path.moveTo(ox + 8 * k, oy + 2 * k)
        path.lineTo(ox + 15 * k, oy + 13.5 * k)
        path.lineTo(ox + 1 * k, oy + 13.5 * k)
        path.closeSubpath()
        path.moveTo(ox + 8 * k, oy + 6.5 * k)
        path.lineTo(ox + 8 * k, oy + 9.5 * k)
        path.moveTo(ox + 8 * k, oy + 11.5 * k)
        path.lineTo(ox + 8 * k, oy + 11.6 * k)
    p.drawPath(path)


# --------------------------------------------------------------------------
# Widget-бэкенд
# --------------------------------------------------------------------------
def run_widget(args) -> int:
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtGui import QFont, QPainter
    from PyQt5.QtWidgets import QApplication, QWidget

    app = QApplication(sys.argv)
    # без этого WM_CLASS = "pill.py" (Qt берёт basename argv[0]) — правила KWin/fly-wm
    # и список терминалов опираются на WM_CLASS, поэтому задаём явно
    app.setApplicationName("astra-voice")
    app.setDesktopFileName("ru.astralinux.astra-voice")

    x11 = None
    try:
        from x11ewmh import X11

        x11 = X11()
    except Exception as exc:  # noqa: BLE001
        print(f"[pill] X11-помощник недоступен: {exc}", file=sys.stderr)

    margin = 0 if args.no_shadow else 24

    class Pill(QWidget):
        def __init__(self):
            super().__init__(None)
            self.setWindowFlags(
                Qt.Tool
                | Qt.FramelessWindowHint
                | Qt.WindowStaysOnTopHint
                | Qt.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setWindowTitle("astra-voice-pill")
            self.setObjectName("astra-voice-pill")
            self.state = args.state
            self.levels = [v / 20.0 for v in REF_LEVELS]
            self.phase = 0.0
            self.first_paint_ms = None
            self._font = QFont(T["font_family"])
            self._font.setPixelSize(int(round(T["font_px"])))
            self.resize(*self.window_size())

        def window_size(self) -> tuple[int, int]:
            from PyQt5.QtGui import QFontMetrics

            fm = QFontMetrics(self._font)
            natural, _, _ = content_width(fm, self.state)
            return clamp_width(natural) + 2 * margin, T["h"] + 2 * margin

        def relayout(self):
            w, h = self.window_size()
            self.resize(w, h)
            return w, h

        def paintEvent(self, _ev):
            from PyQt5.QtCore import QRect

            if self.first_paint_ms is None:
                self.first_paint_ms = (time.monotonic() - t_show) * 1000.0
            p = QPainter(self)
            rect = QRect(margin, margin, self.width() - 2 * margin, T["h"])
            draw_pill(p, rect, self.state, self.levels, self.phase, self._font)
            p.end()

    pill = Pill()
    win_w, win_h = pill.window_size()

    placement = Placement(x11)
    x, y = placement.place(pill, win_w, win_h, T["h"], margin)
    pill.move(x, y)

    # EWMH — ДО map: тип окна, состояния, user-time. requestActivate не зовём.
    wid = int(pill.winId())
    if x11 is not None:
        x11.set_window_type_notification(wid)
        x11.set_state_above_skip(wid)
        x11.set_user_time_zero(wid)
        if args.fly:
            x11.set_fly_no_animation(wid)
            x11.set_fly_corner_radius(wid, T["radius"])
        x11.sync()

    t_show = time.monotonic()
    pill.show()
    if x11 is not None:
        x11.reassert_above(wid)   # после map свойство уже не читается (урок Cowork)

    # уровень 30 кадр/с (spec §8.3: linear, по кадрам, без сглаживания)
    frame = {"n": 0}

    def tick():
        frame["n"] += 1
        pill.phase = (frame["n"] % 30) / 30.0
        if pill.state == "listening":
            t0 = frame["n"] / 30.0
            pill.levels = [
                min(1.0, max(0.05, (REF_LEVELS[i] / 20.0)
                             * (0.55 + 0.45 * math.sin(2 * math.pi * (t0 + i * 0.11)))))
                for i in range(T["bars"])
            ]
        pill.update()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(33)  # ≈30 кадр/с

    if args.cycle:
        def next_state():
            i = (STATES.index(pill.state) + 1) % len(STATES)
            pill.state = STATES[i]
            w, h = pill.relayout()
            nx, ny = placement.place(pill, w, h, T["h"], margin)
            pill.move(nx, ny)
            pill.update()

        cyc = QTimer()
        cyc.timeout.connect(next_state)
        cyc.start(int(args.cycle_ms))

    def report():
        print(f"[pill] backend=widget  winId={hex(wid)}  state={pill.state}")
        print(f"[pill] окно: {pill.geometry().x()},{pill.geometry().y()} "
              f"{pill.geometry().width()}x{pill.geometry().height()}  поле под тень={margin}")
        print(f"[pill] первый кадр: {pill.first_paint_ms:.1f} мс "
              f"(spec §8.4 — показ ≤ 100 мс)" if pill.first_paint_ms is not None
              else "[pill] первый кадр: не отрисован")
        for k, v in placement.report.items():
            print(f"[pill] {k}: {v}")
        if x11 is not None:
            print("[pill] _NET_WM_WINDOW_TYPE:", x11.atom_names(wid, "_NET_WM_WINDOW_TYPE"))
            print("[pill] _NET_WM_STATE:", x11.atom_names(wid, "_NET_WM_STATE"))
            print("[pill] _NET_WM_USER_TIME:", x11.get32(wid, "_NET_WM_USER_TIME"))
        sys.stdout.flush()

    QTimer.singleShot(400, report)
    if args.seconds:
        QTimer.singleShot(int(args.seconds * 1000), app.quit)
    return app.exec_()


# --------------------------------------------------------------------------
# QML-бэкенд (готов к прогону после M1, когда deb поставит python3-pyqt5.qtquick)
# --------------------------------------------------------------------------
def run_qml(args) -> int:
    from PyQt5.QtCore import QTimer, QUrl, QObject, pyqtProperty, pyqtSignal
    from PyQt5.QtGui import QGuiApplication
    from PyQt5.QtQml import QQmlApplicationEngine

    app = QGuiApplication(sys.argv)

    x11 = None
    try:
        from x11ewmh import X11

        x11 = X11()
    except Exception as exc:  # noqa: BLE001
        print(f"[pill] X11-помощник недоступен: {exc}", file=sys.stderr)

    class Model(QObject):
        changed = pyqtSignal()

        def __init__(self):
            super().__init__()
            self._state = args.state
            self._levels = [v / 20.0 for v in REF_LEVELS]

        @pyqtProperty(str, notify=changed)
        def state(self):
            return self._state

        @pyqtProperty(str, notify=changed)
        def text(self):
            return STATE_TEXT[self._state]

        @pyqtProperty("QVariantList", notify=changed)
        def levels(self):
            return self._levels

    model = Model()
    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty("pillModel", model)
    ctx.setContextProperty("tokens", {k: v for k, v in T.items()})
    engine.load(QUrl.fromLocalFile(str(Path(__file__).with_name("qml") / "Pill.qml")))
    roots = engine.rootObjects()
    if not roots:
        print("[pill] Pill.qml не загрузился", file=sys.stderr)
        return 2
    win = roots[0]
    wid = int(win.winId())
    if x11 is not None:
        x11.set_window_type_notification(wid)
        x11.set_state_above_skip(wid)
        x11.set_user_time_zero(wid)
        if args.fly:
            x11.set_fly_no_animation(wid)
        x11.sync()
    win.setProperty("visible", True)
    if x11 is not None:
        x11.reassert_above(wid)

    frame = {"n": 0}

    def tick():
        frame["n"] += 1
        t0 = frame["n"] / 30.0
        model._levels = [
            min(1.0, max(0.05, (REF_LEVELS[i] / 20.0)
                         * (0.55 + 0.45 * math.sin(2 * math.pi * (t0 + i * 0.11)))))
            for i in range(T["bars"])
        ]
        model.changed.emit()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(33)
    if args.seconds:
        QTimer.singleShot(int(args.seconds * 1000), app.quit)
    return app.exec_()


def main() -> int:
    ap = argparse.ArgumentParser(description="S1 · пилюля Astra Voice")
    ap.add_argument("--state", choices=STATES, default="listening")
    ap.add_argument("--backend", choices=["auto", "widget", "qml"], default="auto")
    ap.add_argument("--seconds", type=float, default=0, help="0 = не выходить")
    ap.add_argument("--cycle", action="store_true", help="перебирать состояния")
    ap.add_argument("--cycle-ms", type=int, default=1500)
    ap.add_argument("--no-shadow", action="store_true",
                    help="без поля под тень — окно ровно по пилюле (для замеров геометрии)")
    ap.add_argument("--fly", action="store_true", help="Fly-ветка: атомы _FLY_WM_*")
    args = ap.parse_args()

    if os.environ.get("DISPLAY", "") in ("", ":0") and not os.environ.get("AV_ALLOW_DISPLAY"):
        print("[pill] ОТКАЗ: DISPLAY пуст или :0 (сессия заказчика). "
              "Запускайте под Xvfb, см. README. Обход — AV_ALLOW_DISPLAY=1.",
              file=sys.stderr)
        return 3

    backend = args.backend
    if backend == "auto":
        try:
            import PyQt5.QtQuick  # noqa: F401

            backend = "qml"
        except ImportError:
            backend = "widget"
            print("[pill] python3-pyqt5.qtquick не установлен → бэкенд widget "
                  "(plan-claude §12; deb поставит зависимость в M1)", file=sys.stderr)
    if backend == "qml":
        return run_qml(args)
    return run_widget(args)


if __name__ == "__main__":
    raise SystemExit(main())
