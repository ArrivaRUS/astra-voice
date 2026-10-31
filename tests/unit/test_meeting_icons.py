"""E13 §8.2: fixed glyph geometry, explicit fill and independent tray assets."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PyQt5.QtCore import QSize

from astra_voice.ui import icons

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

EXPECTED_PATHS = {
    "meeting": (
        "M2.5 3.5h7a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1H6l-2.5 2v-2h-1a1 1 0 0 1-1-1v-4a1 1 0"
        " 0 1 1-1zM12.5 6.5h1a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1h-1v2l-2.5-2H8"
    ),
    "rec": (
        "M13.6 8a5.6 5.6 0 1 0-11.2 0 5.6 5.6 0 1 0 11.2 0zM10.4 8a2.4 2.4 0 1 0-4.8 0 "
        "2.4 2.4 0 1 0 4.8 0z"
    ),
    "pause": "M6 3.5v9M10 3.5v9",
    "stop": (
        "M5.2 4h5.6a1.2 1.2 0 0 1 1.2 1.2v5.6a1.2 1.2 0 0 1-1.2 1.2H5.2A1.2 1.2 0 0 1 4"
        " 10.8V5.2A1.2 1.2 0 0 1 5.2 4z"
    ),
    "play": "M5 3.3v9.4L12.8 8z",
    "chat": (
        "M2.5 3h11a1 1 0 0 1 1 1v6.5a1 1 0 0 1-1 1H7l-3 2.5v-2.5H2.5a1 1 0 0 1-1-1V4a1 "
        "1 0 0 1 1-1zM4.5 6h7M4.5 8.5h4.5"
    ),
    "collapse": "M2 4.5 5.5 8 2 11.5M14 4.5 10.5 8 14 11.5",
    "expand": "M5.5 4.5 2 8l3.5 3.5M10.5 4.5 14 8l-3.5 3.5",
    "chevl": "M10 4L6 8l4 4",
    "more": (
        "M4.4 8a.9.9 0 1 0-1.8 0 .9.9 0 1 0 1.8 0zM8.9 8a.9.9 0 1 0-1.8 0 .9.9 0 1 0 1."
        "8 0zM13.4 8a.9.9 0 1 0-1.8 0 .9.9 0 1 0 1.8 0z"
    ),
    "home": "M2.5 7.5 8 3l5.5 4.5M4 6.5v6.5h8V6.5M6.8 13v-3.2h2.4V13",
    "mic": (
        "M8 2a2 2 0 0 1 2 2v3.5a2 2 0 0 1-4 0V4a2 2 0 0 1 2-2zM3.8 7.8a4.2 4.2 0 0 0 8.4 0M8 12v2.2"
    ),
    "people": (
        "M8.2 5.5a2.2 2.2 0 1 0-4.4 0 2.2 2.2 0 1 0 4.4 0zM2 13c.3-2.3 1.9-3.6 4-3.6s3."
        "7 1.3 4 3.6M13.1 6a1.8 1.8 0 1 0-3.6 0 1.8 1.8 0 1 0 3.6 0zM11 9.4c1.7.1 2.8 1"
        ".2 3 3.1"
    ),
    "edit": "M10.8 2.7l2.5 2.5-7.8 7.8-3 .5.5-3z",
    "save": "M8 2.2v8M4.8 7.2 8 10.4l3.2-3.2M2.8 12.2v1.5h10.4v-1.5",
    "spark": (
        "M8 1.8l1.4 4 4 1.4-4 1.4L8 12.6 6.6 8.6l-4-1.4 4-1.4zM12.6 11.2l.6 1.5 1.5.6-1"
        ".5.6-.6 1.5-.6-1.5-1.5-.6 1.5-.6z"
    ),
    "merge": "M4 2.5v3.2c0 1.8 1.5 3 4 3s4-1.2 4-3V2.5M8 8.7v4.8M5.8 11.4 8 13.6l2.2-2.2",
    "copy": (
        "M6.7 5.5h5.6a1.2 1.2 0 0 1 1.2 1.2v6.1a1.2 1.2 0 0 1-1.2 1.2H6.7a1.2 1.2 0 0 1"
        "-1.2-1.2V6.7a1.2 1.2 0 0 1 1.2-1.2zM10.5 5.5V3a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v"
        "7.5a1 1 0 0 0 1 1h2"
    ),
    "head": (
        "M2.8 10V8a5.2 5.2 0 0 1 10.4 0v2M3 9.2h1a1 1 0 0 1 1 1v2.3a1 1 0 0 1-1 1H3a1 1"
        " 0 0 1-1-1v-2.3a1 1 0 0 1 1-1zM12 9.2h1a1 1 0 0 1 1 1v2.3a1 1 0 0 1-1 1h-1a1 1"
        " 0 0 1-1-1v-2.3a1 1 0 0 1 1-1z"
    ),
    "speaker": (
        "M2.5 6.2h2.7L8.8 3v10L5.2 9.8H2.5zM11 5.5a3.5 3.5 0 0 1 0 5M12.8 3.8a6 6 0 0 1 0 8.4"
    ),
    "doc": "M4 1.8h8V14H4zM6 5h4M6 7.5h4M6 10h2.5",
    "chart": "M2.5 13.5h11M4.5 11V8M8 11V4.5M11.5 11V6.5",
}


@pytest.mark.parametrize("name", sorted(EXPECTED_PATHS))
def test_meeting_glyph_path_snapshot(name: str) -> None:
    assert icons.PATHS.get(name) == EXPECTED_PATHS[name]


def test_meeting_glyph_inventory_matches_tokens() -> None:
    assert len(EXPECTED_PATHS) == 22
    assert icons.MEETING_NAMES == frozenset(EXPECTED_PATHS)


@pytest.mark.parametrize("name", ["rec", "stop", "play", "more"])
def test_filled_glyph_renders_solid_center_and_contour_mode_is_distinct(name: str) -> None:
    provider = icons.IconProvider()
    filled, _ = provider.requestImage(f"{name}?c=123456ff&w=1.5&f=1", QSize(32, 32))
    outline, _ = provider.requestImage(f"{name}?c=123456ff&w=1.5", QSize(32, 32))
    assert filled.pixelColor(16, 16).alpha() > 200
    assert filled != outline
    assert not filled.isNull()


@pytest.mark.parametrize("size", [16, 22])
@pytest.mark.parametrize("state", ["rec", "paused"])
def test_meeting_tray_svg_copies_design_with_theme_contract(size: int, state: str) -> None:
    name = f"astravoice-tray-meeting-{state}.svg"
    shipped = ROOT / f"data/icons/hicolor/{size}x{size}/status" / name
    assert (
        shipped.read_bytes() == (ROOT / f"design/meetings/icons/{size}x{size}" / name).read_bytes()
    )
    root = ET.fromstring(shipped.read_bytes())
    assert root.attrib["viewBox"] == f"0 0 {size} {size}"
    assert b"ColorScheme-Text" in shipped.read_bytes()
    assert b"currentColor" in shipped.read_bytes()
    assert b"animate" not in shipped.read_bytes()
