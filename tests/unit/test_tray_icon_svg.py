"""Структура поставляемых SVG: перекраска KDE не затрагивает цвета состояний."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data/icons/hicolor"
DESIGN = REPO / "design/brand/icons/hicolor"
SVG = "{http://www.w3.org/2000/svg}"
COLORS = {
    "listening": "#12B3A0",
    "processing": "#E8A33A",
    "done": "#2FA36B",
    "error": "#D64545",
}
STATES = {*COLORS, "idle", "mono", "nokey"}
ICONS = sorted(
    path for size in ("16x16", "22x22") for path in (DATA / size / "status").glob("*.svg")
)


def test_tray_icon_inventory_and_names() -> None:
    expected = {
        Path(size) / "status" / f"astravoice-tray-{state}.svg"
        for size in ("16x16", "22x22")
        for state in STATES
    }
    assert {path.relative_to(DATA) for path in ICONS} == expected
    assert all(path.name.startswith("astravoice-") for path in ICONS)


@pytest.mark.parametrize("path", ICONS, ids=lambda path: str(path.relative_to(DATA)))
def test_tray_svg_style_and_third_dot(path: Path) -> None:
    root = ET.parse(path).getroot()
    assert root.tag == f"{SVG}svg"
    schemes = [node for node in root.iter() if node.get("id") == "current-color-scheme"]
    assert len(schemes) == 1
    scheme = schemes[0]
    assert scheme.tag == f"{SVG}style"
    scheme_css = "".join("".join(scheme.itertext()).split())
    assert ".ColorScheme-Text" in scheme_css
    assert ".st" not in scheme_css
    assert scheme_css == (
        ".ColorScheme-Text{color:#232629}"
        "@media(prefers-color-scheme:dark){.ColorScheme-Text{color:#eff0f1}}"
    )

    state = path.stem.removeprefix("astravoice-tray-")
    styles = list(root.iter(f"{SVG}style"))
    state_styles = [style for style in styles if style is not scheme]
    circles = list(root.iter(f"{SVG}circle"))
    assert len(circles) == 3
    third = circles[2]

    if state in COLORS:
        assert len(state_styles) == 1
        state_style = state_styles[0]
        assert "id" not in state_style.attrib
        assert state_style.get("type") == "text/css"
        # Ровно одно безусловное правило: ни @media, ни переопределений цвета.
        css = "".join("".join(state_style.itertext()).split())
        assert "@media" not in css
        assert css == f".st{{fill:{COLORS[state]}}}"
        assert third.get("class") == "st"
        assert third.get("fill") == COLORS[state]
    else:
        assert not state_styles  # Включая запрет пустого <style> для idle/mono/nokey.
        assert third.get("class") == "ColorScheme-Text"
        if state == "nokey":
            assert third.get("fill") == "none"
            assert third.get("stroke") == "currentColor"
            assert float(third.attrib["stroke-width"]) > 0
        else:
            assert third.get("fill") == "currentColor"

    if state != "nokey":
        assert third.get("stroke", "none") == "none"
    bars = list(root.iter(f"{SVG}rect"))
    assert len(bars) == 1
    for shape in [*circles[:2], *bars]:
        assert shape.get("class") == "ColorScheme-Text"
        assert shape.get("fill") == "currentColor"


def test_design_and_bundled_status_svgs_match() -> None:
    design = {path.relative_to(DESIGN): path for path in DESIGN.glob("**/status/*.svg")}
    bundled = {path.relative_to(DATA): path for path in DATA.glob("**/status/*.svg")}
    assert design
    assert design.keys() == bundled.keys()
    for relative, path in design.items():
        assert path.read_bytes() == bundled[relative].read_bytes(), str(relative)
