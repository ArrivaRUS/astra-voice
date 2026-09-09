"""Источник темы: разбор kdeglobals, слежение за файлом, мост в QML.

Фикстуры синтетические, но в формате KConfig (`ключ=значение`, секции `[Colors:*]`),
как на машине заказчика: Breeze (светлая), BreezeDark, Astra Dark.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from astra_voice.core.theme import (
    FlyThemeSource,
    KdeThemeSource,
    ThemeSource,
    fly_palette_path,
    kde_config_path,
    luma,
    parse_color,
    read_ini,
)

pytestmark = pytest.mark.unit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

BREEZE_LIGHT = """\
[ColorEffects:Disabled]
Color=56,56,56

[Colors:Selection]
BackgroundNormal=61,174,233

[Colors:Window]
BackgroundNormal=239,240,241
ForegroundNormal=35,38,41

[General]
ColorScheme=Breeze
ColorSchemeHash=0c941dc42003ab6d965e2d412837789d183c66f2
"""

BREEZE_DARK = """\
[Colors:Selection]
BackgroundNormal=61,174,233

[Colors:Window]
BackgroundNormal=42,46,50
ForegroundNormal=252,252,252

[General]
AccentColor=61,174,233
ColorScheme=BreezeDark
"""

# Формат файла заказчика: значения в hex, имя схемы без слова dark в отдельном слове.
ASTRA_DARK = """\
[Colors:Selection]
BackgroundNormal=#2FB3FA

[Colors:Window]
BackgroundAlternate=#404040
BackgroundNormal=#262626
ForegroundNormal=#F0F0F0

[General]
ColorScheme=AstraDark
"""

# Схема без слова light/dark в имени — тёмность решается яркостью фона.
NAMELESS_DARK = """\
[Colors:Window]
BackgroundNormal=#101820

[General]
ColorScheme=Corporate
"""


def write(tmp_path: Path, text: str, name: str = "kdeglobals") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ── парсер ──────────────────────────────────────────────────────────────────


def test_read_ini_sections_and_comments(tmp_path: Path) -> None:
    path = write(tmp_path, "# заметка\n[General]\nColorScheme=Breeze\nбез-равно\n")
    ini = read_ini(path)
    assert ini["General"]["ColorScheme"] == "Breeze"
    assert "без-равно" not in ini["General"]


def test_read_ini_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_ini(tmp_path / "нет-такого") == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("14,23,41", (14, 23, 41)),
        ("#0E1729", (14, 23, 41)),
        ("239, 240, 241", (239, 240, 241)),
        ("", None),
        ("мусор", None),
        ("#ZZZZZZ", None),
    ],
)
def test_parse_color(raw: str, expected: tuple[int, int, int] | None) -> None:
    assert parse_color(raw) == expected


def test_luma_orders_light_over_dark() -> None:
    assert luma((239, 240, 241)) > 128 > luma((42, 46, 50))


# ── KdeThemeSource ──────────────────────────────────────────────────────────


def test_kde_breeze_light(tmp_path: Path) -> None:
    source = KdeThemeSource(write(tmp_path, BREEZE_LIGHT))
    assert source.dark is False
    assert source.accent == "#3DAEE9"  # запасной путь: фон выделения
    assert source.window_bg == "#EFF0F1"
    assert source.text == "#232629"


def test_kde_breeze_dark(tmp_path: Path) -> None:
    source = KdeThemeSource(write(tmp_path, BREEZE_DARK))
    assert source.dark is True
    assert source.accent == "#3DAEE9"  # [General] AccentColor


def test_kde_astra_dark(tmp_path: Path) -> None:
    source = KdeThemeSource(write(tmp_path, ASTRA_DARK))
    assert source.dark is True
    assert source.accent == "#2FB3FA"
    assert source.window_bg == "#262626"


def test_kde_dark_by_background_luma(tmp_path: Path) -> None:
    """Имя схемы ничего не говорит — решает яркость фона окна."""
    source = KdeThemeSource(write(tmp_path, NAMELESS_DARK))
    assert source.dark is True


def test_kde_config_path_follows_xdg_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Живой прогон M1: путь к kdeglobals обязан слушать XDG_CONFIG_HOME, а не только HOME."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert kde_config_path() == tmp_path / "kdeglobals"


def test_kde_config_path_defaults_to_home_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Переменной нет или она относительная — работает ~/.config (правило XDG)."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert kde_config_path() == Path.home() / ".config" / "kdeglobals"
    monkeypatch.setenv("XDG_CONFIG_HOME", "относительный/путь")
    assert kde_config_path() == Path.home() / ".config" / "kdeglobals"


def test_kde_config_path_does_not_create_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kdeglobals — чужой файл: каталог под него не создаём и права не трогаем."""
    base = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base))
    kde_config_path()
    assert not base.exists()


def test_kde_source_without_path_uses_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Источник без явного пути читает файл из XDG_CONFIG_HOME."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    write(tmp_path, BREEZE_DARK)
    source = KdeThemeSource()
    assert source.path == tmp_path / "kdeglobals"
    assert source.dark is True


def test_fly_palette_path_ignores_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fly держит палитру в ~/.fly вне XDG — переменная на путь влиять не должна."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/не-важно")
    assert fly_palette_path() == Path.home() / ".fly" / "paletterc"
    assert FlyThemeSource().path == fly_palette_path()


def test_kde_missing_file_is_light(tmp_path: Path) -> None:
    source = KdeThemeSource(tmp_path / "kdeglobals")
    assert source.dark is False
    assert source.accent is None
    assert isinstance(source, ThemeSource)


def test_kde_reload_reports_change_only_on_real_change(tmp_path: Path) -> None:
    path = write(tmp_path, BREEZE_LIGHT)
    source = KdeThemeSource(path)
    seen: list[bool] = []
    source.subscribe(lambda: seen.append(source.dark))

    assert source.reload() is False
    assert seen == []

    path.write_text(BREEZE_DARK, encoding="utf-8")
    assert source.reload() is True
    assert seen == [True]
    assert source.dark is True


# ── QFileSystemWatcher: атомарная перезапись файла ──────────────────────────


def test_kde_watcher_survives_atomic_rewrite(tmp_path: Path) -> None:
    """kwriteconfig пишет tmp + rename — путь выпадает из наблюдателя, его надо вернуть."""
    from PyQt5.QtCore import QCoreApplication, QEventLoop, QTimer

    app = QCoreApplication.instance() or QCoreApplication([])
    path = write(tmp_path, BREEZE_LIGHT)
    source = KdeThemeSource(path)
    source.start()
    try:
        seen: list[bool] = []
        source.subscribe(lambda: seen.append(source.dark))

        tmp = tmp_path / "kdeglobals.tmp"
        tmp.write_text(BREEZE_DARK, encoding="utf-8")
        os.replace(tmp, path)

        loop = QEventLoop()
        QTimer.singleShot(1500, loop.quit)
        source.subscribe(loop.quit)
        loop.exec_()

        assert seen == [True], "перезапись файла не долетела до подписчика"
        watcher = source._watcher
        assert watcher is not None
        assert str(path) in watcher.files(), "путь не возвращён под наблюдение"
    finally:
        source.stop()
    assert app is not None


# ── Fly-заглушка ────────────────────────────────────────────────────────────


def test_fly_stub_is_honestly_light(tmp_path: Path) -> None:
    source = FlyThemeSource(tmp_path / "paletterc")
    assert source.dark is False
    assert source.accent is None


# ── мост в QML ──────────────────────────────────────────────────────────────


def test_bridge_exposes_properties_and_emits_changed(tmp_path: Path) -> None:
    from PyQt5.QtCore import QCoreApplication

    from astra_voice.ui.theme_bridge import ThemeBridge

    QCoreApplication.instance() or QCoreApplication([])
    path = write(tmp_path, BREEZE_LIGHT)
    source = KdeThemeSource(path)
    bridge = ThemeBridge(source)

    assert bridge.dark is False
    assert bridge.accent.name().upper() == "#3DAEE9"
    assert bridge.windowBg.name().upper() == "#EFF0F1"

    fired: list[int] = []
    bridge.changed.connect(lambda: fired.append(1))
    path.write_text(BREEZE_DARK, encoding="utf-8")
    source.reload()

    assert fired == [1]
    assert bridge.dark is True


def test_bridge_falls_back_when_system_silent(tmp_path: Path) -> None:
    from PyQt5.QtCore import QCoreApplication

    from astra_voice.ui.theme_bridge import ThemeBridge

    QCoreApplication.instance() or QCoreApplication([])
    bridge = ThemeBridge(KdeThemeSource(tmp_path / "kdeglobals"))
    assert bridge.dark is False
    assert bridge.accent.name().upper() == "#1B3A73"
