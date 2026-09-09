#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S1 · трей (прототип для ui/tray.py + ui/tray_icons.py).

Иконка — ПО ИМЕНИ из hicolor (контракт с Plasma: `<style id="current-color-scheme">`
+ `class="ColorScheme-Text"` + `fill="currentColor"`, spec §9.1), а не пиксмап:
только так Plasma перекрашивает знак под панель. Файлы берутся из
design/brand/icons/hicolor/{16x16,22x22}/status/ и раскладываются в
~/.local/share/icons/hicolor/… (пользовательский каталог, root не нужен).

Меню — состав и правила неактивности из spec §9.2 / flows.md:209-214.
Состояния (spec §9.1): idle · listening · processing · done(800 мс) · error · hotkey-not-grabbed.

Запуск (только под Xvfb, см. README):
  python3 tray.py --install-icons --cycle-ms 900 --seconds 8
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC_ICONS = REPO / "design" / "brand" / "icons" / "hicolor"
DST_ICONS = Path.home() / ".local" / "share" / "icons" / "hicolor"
SIZES = ("16x16", "22x22")

# spec §9.1 — состояние → (имя иконки, подсказка)
TRAY_STATES = [
    ("idle", "astravoice-tray-idle", "Astra Voice — готов · Ctrl+Space"),
    ("listening", "astravoice-tray-listening", "Слушаю…"),
    ("processing", "astravoice-tray-processing", "Распознаю…"),
    ("done", "astravoice-tray-done", "Готово"),
    ("error", "astravoice-tray-error", "Микрофон недоступен — открыть"),
    # шестое состояние spec §9.1: свой знак (кольцо), файл в наборе есть — 16/22 px
    ("hotkey-not-grabbed", "astravoice-tray-nokey",
     "Горячая клавиша не захвачена — выбрать другую"),
]


def install_icons(verbose: bool = True) -> list[str]:
    """Копия SVG трея в пользовательский каталог тем. Ничего в системе не трогаем."""
    copied = []
    for size in SIZES:
        src = SRC_ICONS / size / "status"
        if not src.is_dir():
            continue
        dst = DST_ICONS / size / "status"
        dst.mkdir(parents=True, exist_ok=True)
        for svg in sorted(src.glob("astravoice-tray-*.svg")):
            shutil.copy2(svg, dst / svg.name)
            copied.append(str(dst / svg.name))
    if verbose:
        for path in copied:
            print(f"[tray] установлено: {path}")
    return copied


def build_menu(qt, recording: bool, has_last_text: bool, models: list[str],
               active_model: str, updates_enabled: bool):
    """Меню трея по spec §9.2 — состав и условия неактивности."""
    from PyQt5.QtWidgets import QAction, QMenu

    menu = QMenu()
    menu.setObjectName("astravoice-tray-menu")

    # заголовок-статус: всегда неактивен
    status = QAction("Слушаю…" if recording else "Готов", menu)
    status.setEnabled(False)
    menu.addAction(status)
    menu.addSeparator()

    cancel = QAction("Отмена", menu)
    cancel.setEnabled(recording)          # только во время записи/распознавания
    menu.addAction(cancel)

    if len(models) <= 1:
        one = QAction(f"Модель: {active_model}" if models else "Модель не установлена", menu)
        one.setEnabled(False)             # при одной модели пункт без подменю
        menu.addAction(one)
    else:
        sub = menu.addMenu("Модель")
        for name in models:
            act = QAction(name, sub)
            act.setCheckable(True)
            act.setChecked(name == active_model)   # галочка на активной
            sub.addAction(act)

    copy_last = QAction("Скопировать последний текст", menu)
    copy_last.setEnabled(has_last_text)
    menu.addAction(copy_last)

    menu.addSeparator()
    settings = QAction("Настройки…", menu)
    settings.setShortcut("Ctrl+,")
    menu.addAction(settings)

    upd = QAction("Проверить обновления", menu)
    upd.setEnabled(updates_enabled)       # неактивен при выключенных проверках/офлайне
    menu.addAction(upd)

    menu.addAction(QAction("О программе", menu))
    menu.addSeparator()
    quit_act = QAction("Выход", menu)
    quit_act.setShortcut("Ctrl+Q")
    menu.addAction(quit_act)
    return menu, quit_act


def main() -> int:
    ap = argparse.ArgumentParser(description="S1 · трей Astra Voice")
    ap.add_argument("--install-icons", action="store_true")
    ap.add_argument("--cycle-ms", type=int, default=900)
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--recording", action="store_true", help="меню в режиме записи")
    ap.add_argument("--icons-png", metavar="PATH",
                    help="сохранить полосу иконок 22 px (6 состояний) в PNG и выйти")
    ap.add_argument("--force-hicolor", action="store_true",
                    help="QIcon.setThemeName('hicolor') — обход подмены имени (см. README)")
    args = ap.parse_args()

    if os.environ.get("DISPLAY", "") in ("", ":0") and not os.environ.get("AV_ALLOW_DISPLAY"):
        print("[tray] ОТКАЗ: DISPLAY пуст или :0 (сессия заказчика). "
              "Запускайте под Xvfb, см. README. Обход — AV_ALLOW_DISPLAY=1.", file=sys.stderr)
        return 3

    # ГРАБЛИ (найдено в прогоне 2026-09-09): QSystemTrayIcon ходит по СЕССИОННОЙ ШИНЕ,
    # а она общая с реальной сессией заказчика — независимо от $DISPLAY. Запуск на
    # системной шине показывает иконку на НАСТОЯЩЕЙ панели Plasma. Изоляция обязательна:
    #   dbus-run-session -- python3 tray.py …
    # ВНИМАНИЕ: на Астре XDG_RUNTIME_DIR несёт мандатную метку
    # (/run/user/1000z0_0_0x0_0x0), поэтому сравнение с /run/user/<uid>/bus не работает —
    # берём путь из XDG_RUNTIME_DIR. Изолированная шина (dbus-run-session) даёт
    # unix:abstract=/tmp/dbus-… и проверку проходит.
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    user_bus = f"unix:path={runtime}/bus"
    if (not bus or bus.startswith(user_bus)) and not os.environ.get("AV_ALLOW_SESSION_BUS"):
        print("[tray] ОТКАЗ: сессионная шина — общая с сессией заказчика, иконка вылезет "
              "на его панель. Запускайте: dbus-run-session -- python3 tray.py …  "
              "(осознанный обход — AV_ALLOW_SESSION_BUS=1)", file=sys.stderr)
        return 4

    if args.install_icons:
        install_icons()

    from PyQt5 import QtCore
    from PyQt5.QtCore import QTimer
    from PyQt5.QtGui import QIcon
    from PyQt5.QtWidgets import QApplication, QSystemTrayIcon

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Astra Voice")
    app.setDesktopFileName("ru.astralinux.astra-voice")

    # поиск иконок ПО ИМЕНИ: наш пользовательский каталог + hicolor как запасная тема
    QIcon.setThemeSearchPaths([str(DST_ICONS.parent)] + QIcon.themeSearchPaths())
    QIcon.setFallbackThemeName("hicolor")
    print("[tray] системная тема иконок:", QIcon.themeName())
    if args.force_hicolor:
        # ОБХОД найденной в S1 ловушки (см. README «Находки»): при теме astra-proxima
        # QIcon.fromTheme срезает имя по дефисам до «astra» и отдаёт звезду Astra Linux.
        QIcon.setThemeName("hicolor")
        print("[tray] themeName принудительно = hicolor (обход S1-ловушки)")
    print("[tray] themeSearchPaths =", QIcon.themeSearchPaths())

    available = QSystemTrayIcon.isSystemTrayAvailable()
    print(f"[tray] isSystemTrayAvailable() = {available}")
    if not available:
        # ветка «трея нет»: в проде — баннер «трей недоступен» + ретрай ≤ 30 с (plan-claude §3)
        print("[tray] SNI-хоста нет (ожидаемо под Xvfb без панели) — "
              "проверяем только разрешение имён иконок и сборку меню")

    for state, name, tip in TRAY_STATES:
        icon = QIcon.fromTheme(name)
        resolved = icon.name()
        ok = "OK" if resolved == name else f"ПОДМЕНА → {resolved!r}"
        print(f"[tray] {state:<20} icon={name:<30} найдена={not icon.isNull()} "
              f"разрешилось в {ok}")

    if args.icons_png:
        from PyQt5.QtCore import QSize, Qt
        from PyQt5.QtGui import QColor, QPainter, QPixmap

        cell, pad = 22, 10
        strip = QPixmap((cell + pad) * len(TRAY_STATES) + pad, (cell + pad) * 2 + pad)
        strip.fill(QColor("#EFF0F1"))
        p = QPainter(strip)
        p.fillRect(0, cell + pad + pad // 2, strip.width(), cell + pad, QColor("#232629"))
        for i, (_state, name, _tip) in enumerate(TRAY_STATES):
            pm = QIcon.fromTheme(name).pixmap(QSize(cell, cell))
            x = pad + i * (cell + pad)
            p.drawPixmap(x, pad, pm)
            p.drawPixmap(x, cell + 2 * pad + pad // 2, pm)
        p.end()
        strip.save(args.icons_png)
        print(f"[tray] полоса иконок сохранена: {args.icons_png}")
        return 0

    tray = QSystemTrayIcon()
    menu, quit_act = build_menu(
        QtCore, recording=args.recording, has_last_text=False,
        models=["GigaAM v3 RNN-T"], active_model="GigaAM v3 RNN-T",
        updates_enabled=False,
    )
    tray.setContextMenu(menu)
    quit_act.triggered.connect(app.quit)

    print("[tray] меню (spec §9.2):")
    for act in menu.actions():
        if act.isSeparator():
            print("  ---")
        else:
            sc = act.shortcut().toString()
            print(f"  {act.text():<32} enabled={act.isEnabled()}"
                  f"{'  ' + sc if sc else ''}")

    idx = {"i": 0}

    def cycle():
        state, name, tip = TRAY_STATES[idx["i"] % len(TRAY_STATES)]
        tray.setIcon(QIcon.fromTheme(name))
        tray.setToolTip(tip)
        print(f"[tray] state={state} tooltip={tip!r}")
        sys.stdout.flush()
        idx["i"] += 1

    cycle()
    tray.show()
    print(f"[tray] tray.isVisible() = {tray.isVisible()}")

    timer = QTimer()
    timer.timeout.connect(cycle)
    timer.start(args.cycle_ms)
    if args.seconds:
        QTimer.singleShot(int(args.seconds * 1000), app.quit)
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
