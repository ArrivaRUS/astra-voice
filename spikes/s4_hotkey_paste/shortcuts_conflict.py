#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S4 — парсер конфликтов глобальных комбинаций KDE (→ platform/shortcuts_conflict.py).

**Только чтение** файла заказчика `~/.config/kglobalshortcutsrc`. Ничего не пишет.

Формат (проверено на машине 2026-09-09):

    [kwin]
    Window Close=Alt+F4\\tMeta+Q,Alt+F4,Закрыть окно
    _k_friendly_name=KWin

то есть `действие = <активные, через TAB>,<по умолчанию>,<человекочитаемое имя>`;
`none` — комбинация снята. Имя раздела — либо `<app>.desktop`, либо служебное имя
компонента; человекочитаемое имя раздела лежит в `_k_friendly_name`.

Использование:
    python3 shortcuts_conflict.py --combo 'Ctrl+Space' --combo 'Alt+F2' --json out/conflicts.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

KDE_PATH = "~/.config/kglobalshortcutsrc"
FLY_PATH = "~/.fly/keyshortcutrc"          # ветка Fly — читаем, если файл есть


def norm_combo(s: str) -> str:
    """`ctrl+space` → `Ctrl+Space`; порядок модификаторов канонизируется."""
    order = ["Meta", "Ctrl", "Alt", "Shift"]
    aliases = {"control": "Ctrl", "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift",
               "meta": "Meta", "super": "Meta", "win": "Meta"}
    parts = [p.strip() for p in s.split("+") if p.strip()]
    mods, key = [], []
    for p in parts:
        a = aliases.get(p.lower())
        if a:
            if a not in mods:
                mods.append(a)
        else:
            key.append(p if len(p) > 1 else p.upper())
    mods.sort(key=lambda m: order.index(m))
    return "+".join(mods + key)


def parse_kglobalshortcutsrc(path: str) -> list:
    """Возвращает список {section, section_name, action, combos, default, title}."""
    rows, section, friendly = [], "", {}
    pending = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                section = m.group(1)
                continue
            if "=" not in line:
                continue
            action, value = line.split("=", 1)
            if action == "_k_friendly_name":
                friendly[section] = value
                continue
            fields = value.split(",", 2)
            active = fields[0] if fields else ""
            default = fields[1] if len(fields) > 1 else ""
            title = fields[2] if len(fields) > 2 else ""
            # ГРАБЛИ (факт 2026-09-09): KConfig экранирует табуляцию, в файле лежит
            # ДВУХСИМВОЛЬНАЯ последовательность «\t», а не настоящий TAB:
            #   _launch=Alt+Space\tAlt+F2\tSearch,…
            # Разбираем оба варианта, иначе Alt+F2 «не находится».
            combos = [norm_combo(c)
                      for c in re.split(r"\\t|\t", active)
                      if c and c.lower() != "none"]
            pending.append({"section": section, "action": action, "combos": combos,
                            "default": default, "title": title})
    for r in pending:
        r["section_name"] = friendly.get(r["section"], r["section"])
        rows.append(r)
    return rows


def find(rows: list, combo: str) -> list:
    want = norm_combo(combo)
    return [r for r in rows if want in r["combos"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--combo", action="append", default=[])
    ap.add_argument("--path", default=KDE_PATH)
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    combos = args.combo or ["Ctrl+Space", "Alt+F2", "Alt+Space"]

    path = os.path.expanduser(args.path)
    report = {"path": path, "exists": os.path.exists(path), "queries": {}}
    if not report["exists"]:
        print("файла нет: %s" % path)
        return 1
    rows = parse_kglobalshortcutsrc(path)
    report["actions_total"] = len(rows)
    report["actions_with_combo"] = sum(1 for r in rows if r["combos"])
    print("%s: разделов=%d, действий=%d, из них с комбинацией=%d"
          % (path, len({r["section"] for r in rows}), len(rows),
             report["actions_with_combo"]))
    for c in combos:
        hits = find(rows, c)
        report["queries"][norm_combo(c)] = [
            {"section": h["section"], "section_name": h["section_name"],
             "action": h["action"], "title": h["title"], "combos": h["combos"]}
            for h in hits]
        if hits:
            for h in hits:
                print("  %-12s ЗАНЯТА → [%s] %s · действие %r · «%s»"
                      % (norm_combo(c), h["section"], h["section_name"],
                         h["action"], h["title"]))
        else:
            print("  %-12s свободна (в kglobalshortcutsrc совпадений нет)" % norm_combo(c))

    fly = os.path.expanduser(FLY_PATH)
    report["fly_keyshortcutrc"] = {"path": fly, "exists": os.path.exists(fly)}
    print("  Fly `%s`: %s" % (fly, "есть" if os.path.exists(fly) else "нет (сессия Fly не запускалась)"))

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
