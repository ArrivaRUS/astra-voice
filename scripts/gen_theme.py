#!/usr/bin/env python3
"""Генератор QML-темы из design/tokens.json.

Пишет `qml/Theme.qml` (singleton со всеми токенами приложения, светлая/тёмная пара
разводится одним флагом `dark`), `qml/PillTheme.qml` (фиксированные цвета и геометрия
пилюли — они с темой НЕ меняются) и `qml/qmldir`.

    python3 scripts/gen_theme.py            # перегенерировать
    python3 scripts/gen_theme.py --check    # сравнить с файлами в репозитории (CI)

Вывод детерминированный: группы в фиксированном порядке, ключи внутри группы
отсортированы. Токены, значение которых — проза, ссылка вида `state.<тема>.…`
или составная CSS-строка, пропускаются: в QML им нет применения.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
TOKENS = REPO / "design" / "tokens.json"
QML_DIR = REPO / "qml"

HEADER = (
    "// СГЕНЕРИРОВАНО scripts/gen_theme.py из design/tokens.json {version} — НЕ ПРАВИТЬ РУКАМИ.\n"
    "// Перегенерация: python3 scripts/gen_theme.py · "
    "проверка: python3 scripts/gen_theme.py --check\n"
)

PX_RE = re.compile(r"^(-?\d+(?:\.\d+)?)px$")
MS_RE = re.compile(r"^(\d+(?:\.\d+)?)ms$")
EM_RE = re.compile(r"^(-?\d+(?:\.\d+)?)em$")
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
RGBA_RE = re.compile(r"^rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\.\d+|\d+(?:\.\d+)?)\s*\)$")
BEZIER_RE = re.compile(r"^cubic-bezier\(([-\d.,\s]+)\)$")

# суффиксы для многозначных паддингов: 2 значения → Y/X, 3 → Top/X/Bottom, 4 → CSS-порядок
MULTI_SUFFIX = {
    2: ("Y", "X"),
    3: ("Top", "X", "Bottom"),
    4: ("Top", "Right", "Bottom", "Left"),
}

# группы Theme.qml: (путь в tokens.json, префикс имени свойства)
THEME_GROUPS: list[tuple[str, str]] = [
    ("state.focus", "focus"),
    ("font.family", "font"),
    ("font.weight", "weight"),
    ("font.role", "font"),
    ("space", "space"),
    ("size", "size"),
    ("radius", "radius"),
    ("border", "border"),
    ("motion.duration", "duration"),
    ("motion.easing", "easing"),
    ("component", ""),
]
# группы Theme.qml, парные по теме: (путь light, путь dark, префикс)
THEME_PAIRS: list[tuple[str, str, str]] = [
    ("color.light", "color.dark", ""),
    ("color.state.light", "color.state.dark", "indicator"),
    ("state.light", "state.dark", "state"),
]
# компоненты, чьи токены уезжают в PillTheme, а не в Theme
PILL_COMPONENTS = ("pill",)


def camel(name: str) -> str:
    parts = [p for p in re.split(r"[-.]", name) if p]
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def prop_name(prefix: str, path: str) -> str:
    base = camel(path)
    if not prefix:
        return base
    return prefix + base[:1].upper() + base[1:]


def dig(data: dict[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        node = node[part]
    return node


def leaves(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Плоский список (путь, значение) из дерева токенов. Лист — словарь с ключом value."""
    out: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        nested = any(isinstance(v, dict) for k, v in node.items() if k != "value")
        if "value" in node and not nested:
            out.append((prefix, node["value"]))
            return out
        for key in sorted(node):
            if key.startswith("$"):
                continue
            out.extend(leaves(node[key], f"{prefix}.{key}" if prefix else key))
    return out


def num(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def qml_color(value: str) -> str | None:
    if HEX_RE.match(value):
        return f'"{value.upper()}"'
    m = RGBA_RE.match(value)
    if m:
        r, g, b = (int(m.group(i)) / 255 for i in (1, 2, 3))
        a = float(m.group(4))
        return f"Qt.rgba({num(r)}, {num(g)}, {num(b)}, {num(a)})"
    return None


def scalar(value: Any) -> tuple[str, str] | None:
    """Значение токена → (тип QML, литерал). None — токен в QML не переносится."""
    if isinstance(value, bool):
        return ("bool", "true" if value else "false")
    if isinstance(value, int):
        return ("int", str(value))
    if isinstance(value, float):
        return ("real", num(value))
    if isinstance(value, list) and value and all(isinstance(v, (int, float)) for v in value):
        return ("var", "[" + ", ".join(num(v) for v in value) + "]")
    if not isinstance(value, str):
        return None
    color = qml_color(value)
    if color:
        return ("color", color)
    m = PX_RE.match(value)
    if m:
        return ("real", num(float(m.group(1))))
    m = MS_RE.match(value)
    if m:
        return ("int", str(int(float(m.group(1)))))
    m = EM_RE.match(value)
    if m:
        return ("real", num(float(m.group(1))))
    m = BEZIER_RE.match(value)
    if m:
        nums = [float(p) for p in m.group(1).split(",")]
        if len(nums) == 4:
            return ("var", "[" + ", ".join(num(v) for v in nums) + "]")
    if value.startswith("'") and value.endswith("'"):
        return ("string", json.dumps(value.strip("'"), ensure_ascii=False))
    return None


def emit(name: str, value: Any, aliases: dict[str, str]) -> list[str]:
    """Одно значение токена → строки свойств QML (может быть несколько для паддингов)."""
    if isinstance(value, str) and value in aliases:
        return [f"readonly property color {name}: {aliases[value]}"]
    if isinstance(value, str) and " " in value:
        parts = value.split()
        if all(PX_RE.match(p) for p in parts) and len(parts) in MULTI_SUFFIX:
            return [
                f"readonly property real {name}{suffix}: {num(float(PX_RE.match(part).group(1)))}"  # type: ignore[union-attr]
                for suffix, part in zip(MULTI_SUFFIX[len(parts)], parts, strict=True)
            ]
        return []
    got = scalar(value)
    if got is None:
        return []
    kind, literal = got
    if kind == "string":
        return [f"readonly property string {name}: {literal}"]
    return [f"readonly property {kind} {name}: {literal}"]


def emit_pair(name: str, light: Any, dark: Any) -> list[str]:
    """Пара светлая/тёмная → одно свойство с ветвлением по `dark`."""
    lit_light = scalar(light)
    if lit_light is None or lit_light[0] != "color":
        return []
    lit_dark = scalar(dark) if dark is not None else None
    if lit_dark is None or lit_dark[0] != "color":
        lit_dark = lit_light
    if lit_dark[1] == lit_light[1]:
        return [f"readonly property color {name}: {lit_light[1]}"]
    return [f"readonly property color {name}: dark ? {lit_dark[1]} : {lit_light[1]}"]


def build_theme(data: dict[str, Any]) -> str:
    body: list[str] = []
    seen: set[str] = set()
    aliases: dict[str, str] = {}

    def add(lines: list[str], name: str) -> None:
        if not lines:
            return
        if name in seen:
            raise SystemExit(f"gen_theme: имя свойства повторяется — {name}")
        seen.add(name)
        body.extend("    " + line for line in lines)

    body.append("    // ── тема: dark берётся из themeSource (core/theme.py), без него — светлая")
    body.append(
        '    readonly property bool dark: (typeof themeSource !== "undefined" '
        "&& themeSource !== null) ? themeSource.dark === true : false"
    )
    for light_path, dark_path, prefix in THEME_PAIRS:
        light = dict(leaves(dig(data, light_path)))
        dark = dict(leaves(dig(data, dark_path)))
        body.append("")
        body.append(f"    // ── {light_path} / {dark_path}")
        for key in sorted(light):
            name = prop_name(prefix, key)
            add(emit_pair(name, light[key], dark.get(key)), name)
            if prefix == "" and light_path == "color.light":
                aliases[key] = name

    for path, prefix in THEME_GROUPS:
        body.append("")
        body.append(f"    // ── {path}")
        for key, value in leaves(dig(data, path)):
            if path == "component" and key.split(".")[0] in PILL_COMPONENTS:
                continue
            name = prop_name(prefix, key)
            if path == "font.family":
                # Qt 5.15 знает только одно семейство: основное — в `fontUi`,
                # весь стек запасных — в `fontUiStack` (перебор при отсутствии шрифта).
                families = [f.strip().strip("'\"") for f in str(value).split(",")]
                first = json.dumps(families[0], ensure_ascii=False)
                add([f"readonly property string {name}: {first}"], name)
                stack = ", ".join(json.dumps(f, ensure_ascii=False) for f in families)
                add([f"readonly property var {name}Stack: [{stack}]"], f"{name}Stack")
                continue
            if isinstance(value, dict) and set(value) == {"light", "dark"}:
                # токен сам задан парой тем (например counter-on-active) — разводим по `dark`
                add(emit_pair(name, value["light"], value["dark"]), name)
                continue
            add(emit(name, value, aliases), name)
    return render(data, "Theme", body)


def build_pill(data: dict[str, Any]) -> str:
    body: list[str] = []
    seen: set[str] = set()
    body.append("    // ── color.fixed: рисуется поверх чужого рабочего стола, с темой НЕ меняется")
    for key, value in leaves(dig(data, "color.fixed")):
        name = camel(key)
        lines = emit(name, value, {})
        if lines and name not in seen:
            seen.add(name)
            body.extend("    " + line for line in lines)
    body.append("")
    body.append("    // ── component.pill")
    for key, value in leaves(dig(data, "component")):
        if key.split(".")[0] not in PILL_COMPONENTS:
            continue
        name = camel(key)
        lines = emit(name, value, {})
        if lines and name not in seen:
            seen.add(name)
            body.extend("    " + line for line in lines)
    return render(data, "PillTheme", body)


def render(data: dict[str, Any], type_name: str, body: list[str]) -> str:
    version = data["$meta"]["version"]
    lines = [
        HEADER.format(version=version),
        "pragma Singleton",
        "import QtQuick 2.15",
        "",
        "QtObject {",
    ]
    lines.extend(body)
    lines.append("}")
    return "\n".join(lines) + "\n"


def build_qmldir() -> str:
    types = sorted(p.stem for p in QML_DIR.glob("*.qml") if p.stem not in {"Theme", "PillTheme"})
    lines = [
        "# СГЕНЕРИРОВАНО scripts/gen_theme.py — НЕ ПРАВИТЬ РУКАМИ.",
        "singleton Theme 1.0 Theme.qml",
        "singleton PillTheme 1.0 PillTheme.qml",
    ]
    lines.extend(f"{name} 1.0 {name}.qml" for name in types)
    return "\n".join(lines) + "\n"


def outputs() -> dict[Path, str]:
    data = json.loads(TOKENS.read_text(encoding="utf-8"))
    return {
        QML_DIR / "Theme.qml": build_theme(data),
        QML_DIR / "PillTheme.qml": build_pill(data),
        QML_DIR / "qmldir": build_qmldir(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="сравнить с репозиторием, ничего не писать"
    )
    args = parser.parse_args(argv)

    QML_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for path, text in outputs().items():
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != text:
                shown = path.relative_to(REPO) if path.is_relative_to(REPO) else path
                stale.append(str(shown))
            continue
        path.write_text(text, encoding="utf-8")
    if args.check:
        if stale:
            print("gen_theme --check: устарели " + ", ".join(stale), file=sys.stderr)
            return 1
        print("gen_theme --check: генерат совпадает с репозиторием")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
