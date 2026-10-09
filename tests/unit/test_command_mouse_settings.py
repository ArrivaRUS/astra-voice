"""Mouse input is opt-in and malformed saved values never grab a default button."""

from __future__ import annotations

from pathlib import Path

import pytest

from astra_voice.core.settings import Settings, from_dict, load, save
from astra_voice.platform.mouse_button import qt_button_to_logical

pytestmark = pytest.mark.unit


def test_mouse_defaults_do_not_change_keyboard_command() -> None:
    for settings in (Settings(), from_dict({}), from_dict({"schema_version": 1})):
        assert not settings.command_mouse_enabled
        assert settings.command_mouse_button == 2
        assert settings.command_enabled
        assert settings.command_hotkey == "Super_L"


@pytest.mark.parametrize("button", [2, *range(8, 32)])
def test_valid_buttons_round_trip(button: int, tmp_path: Path) -> None:
    settings = from_dict({"command_mouse_button": button, "command_mouse_enabled": True})
    path = tmp_path / "settings.json"
    save(settings, path)
    restored = load(path)
    assert restored.command_mouse_enabled
    assert restored.command_mouse_button == button
    assert not {"command_mouse_enabled", "command_mouse_button"} & restored.extra.keys()


@pytest.mark.parametrize(
    "button", [True, False, None, "2", 2.0, [], {}, -1, 0, 1, 3, 4, 5, 6, 7, 32]
)
def test_invalid_button_disables_mouse_without_changing_keyboard(button: object) -> None:
    settings = from_dict(
        {"command_mouse_button": button, "command_mouse_enabled": True, "command_hotkey": "Ctrl+F9"}
    )
    assert not settings.command_mouse_enabled
    assert settings.command_mouse_button == 2
    assert settings.command_enabled
    assert settings.command_hotkey == "Ctrl+F9"


@pytest.mark.parametrize("enabled", [None, 0, 1, "true", "false", [], {}])
def test_invalid_enabled_value_is_fail_closed(enabled: object) -> None:
    settings = from_dict({"command_mouse_button": 8, "command_mouse_enabled": enabled})
    assert not settings.command_mouse_enabled
    assert settings.command_mouse_button == 8


def test_explicit_enable_with_missing_button_uses_middle() -> None:
    settings = from_dict({"command_mouse_enabled": True})
    assert settings.command_mouse_enabled
    assert settings.command_mouse_button == 2


@pytest.mark.parametrize("bit", range(3, 27))
def test_qt_extra_buttons_are_single_bits_not_x11_numbers(bit: int) -> None:
    assert qt_button_to_logical(1 << bit) == bit + 5


@pytest.mark.parametrize("button", [0, 1, 2, 3, 5, 6, 7, 12, 1 << 27, -4, True])
def test_qt_primary_secondary_combinations_and_invalid_values_are_rejected(button: int) -> None:
    assert qt_button_to_logical(button) is None


def test_qt_middle_button_maps_to_x11_middle_not_scroll() -> None:
    assert qt_button_to_logical(4) == 2
