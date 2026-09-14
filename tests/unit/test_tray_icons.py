"""Контракт иконок трея и загрузка SVG без дисплея."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock, patch

import pytest

from astra_voice.core import paths
from astra_voice.core.theme import ThemeSource
from astra_voice.platform.session import SessionKind
from astra_voice.ui.tray_icons import TrayIconProvider, TrayState, _fly_svg, find_tray_icon_path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers.qt_app import get_qapplication  # noqa: E402

if TYPE_CHECKING:
    from PyQt5.QtWidgets import QApplication

pytestmark = pytest.mark.unit
DATA = Path(__file__).resolve().parents[2] / "data"
NAMES = [
    (TrayState.IDLE, "astravoice-tray-idle"),
    (TrayState.LISTENING, "astravoice-tray-listening"),
    (TrayState.PROCESSING, "astravoice-tray-processing"),
    (TrayState.DONE, "astravoice-tray-done"),
    (TrayState.ERROR, "astravoice-tray-error"),
    (TrayState.NOKEY, "astravoice-tray-nokey"),
]


class PanelTheme(ThemeSource):
    is_dark = False

    @property
    def dark(self) -> bool:
        return self.is_dark

    @property
    def accent(self) -> str | None:
        return None


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return get_qapplication()


@pytest.fixture
def bundled_icons(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from PyQt5.QtGui import QIcon

    monkeypatch.setattr(paths, "resource_root", lambda: tmp_path / "usr/share/astra-voice")
    monkeypatch.setattr(paths, "data_dir_static", lambda: DATA)
    monkeypatch.setattr(QIcon, "fromTheme", lambda name: QIcon())


@pytest.mark.parametrize(("state", "expected"), NAMES)
def test_icon_name(state: TrayState, expected: str) -> None:
    assert TrayIconProvider(SessionKind.KDE).icon_name(state) == expected


def test_names_do_not_fall_back_to_astra() -> None:
    provider = TrayIconProvider(SessionKind.KDE)
    assert len(TrayState) == 6
    assert all(not provider.icon_name(state).startswith("astra-") for state in TrayState)


@pytest.mark.parametrize(("state", "name"), NAMES)
@pytest.mark.parametrize("size", [16, 22])
def test_bundled_files(state: TrayState, name: str, size: int) -> None:
    assert (DATA / "icons" / "hicolor" / f"{size}x{size}" / "status" / f"{name}.svg").is_file()


@pytest.mark.parametrize("size", [16, 22])
@pytest.mark.parametrize(
    ("in_theme", "in_data"), [(True, False), (False, True), (True, True), (False, False)]
)
def test_tray_icon_search_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: int,
    in_theme: bool,
    in_data: bool,
) -> None:
    """Установленная тема имеет приоритет; ошибка содержит оба пути поиска."""
    root = tmp_path / "usr/share/astra-voice"
    data = tmp_path / "dev/data"
    monkeypatch.setattr(paths, "resource_root", lambda: root)
    monkeypatch.setattr(paths, "data_dir_static", lambda: data)
    name = TrayIconProvider(SessionKind.FLY).icon_name(TrayState.IDLE)
    relative = Path(f"{size}x{size}") / "status" / f"{name}.svg"
    theme_file = tmp_path / "usr/share/icons/hicolor" / relative
    data_file = data / "icons/hicolor" / relative
    for path, exists in ((theme_file, in_theme), (data_file, in_data)):
        if exists:
            path.parent.mkdir(parents=True)
            path.write_bytes(b"SVG")

    if in_theme or in_data:
        assert find_tray_icon_path(name, size) == (theme_file if in_theme else data_file)
    else:
        with pytest.raises(FileNotFoundError) as error:
            find_tray_icon_path(name, size)
        assert str(theme_file) in str(error.value)
        assert str(data_file) in str(error.value)


@pytest.mark.parametrize("session", [SessionKind.FLY, SessionKind.KDE])
@pytest.mark.parametrize("mixed", [False, True])
def test_icons_load_from_installed_theme_or_mixed_directories(
    qapp: QApplication,
    bundled_icons: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session: SessionKind,
    mixed: bool,
) -> None:
    """Обе ветки загружают тему и независимо ищут каждый размер."""
    data = tmp_path / "dev/data"
    monkeypatch.setattr(paths, "data_dir_static", lambda: data)
    provider = TrayIconProvider(session)
    name = provider.icon_name(TrayState.IDLE)
    for size in (16, 22):
        relative = Path(f"{size}x{size}") / "status" / f"{name}.svg"
        root = data / "icons/hicolor" if mixed and size == 22 else paths.icon_theme_dir()
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_bytes((DATA / "icons/hicolor" / relative).read_bytes())

    icon = provider.icon(TrayState.IDLE)
    assert not icon.isNull()
    assert {(size.width(), size.height()) for size in icon.availableSizes()} == {(16, 16), (22, 22)}
    for size in (16, 22):
        assert not icon.pixmap(size, size).isNull()


@pytest.mark.parametrize("state", list(TrayState))
@pytest.mark.parametrize("size", [16, 22])
@pytest.mark.parametrize(("dark", "color"), [(False, "#232629"), (True, "#eff0f1")])
def test_fly_svg_changes_only_current_color(
    state: TrayState, size: int, dark: bool, color: str
) -> None:
    name = TrayIconProvider(SessionKind.FLY).icon_name(state)
    path = DATA / "icons" / "hicolor" / f"{size}x{size}" / "status" / f"{name}.svg"
    source = path.read_text(encoding="utf-8")
    result = _fly_svg(path, dark=dark).decode("utf-8")
    assert "currentColor" in source
    assert "currentColor" not in result
    assert f'fill="{color}"' in result
    assert result == source.replace("currentColor", color)
    if state == TrayState.LISTENING:
        assert 'fill="#12B3A0"' in result


@pytest.mark.parametrize("session", [SessionKind.KDE, SessionKind.OTHER])
@pytest.mark.parametrize(("state", "name"), NAMES)
def test_theme_lookup_and_cache(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    session: SessionKind,
    state: TrayState,
    name: str,
) -> None:
    from PyQt5.QtGui import QIcon, QPixmap

    pixmap = QPixmap(16, 16)
    pixmap.fill()
    themed = QIcon(pixmap)
    lookup = Mock(return_value=themed)
    monkeypatch.setattr(QIcon, "fromTheme", lookup)
    provider = TrayIconProvider(session)
    assert provider.icon(state) is themed
    assert provider.has_icon(state)
    assert provider.icon(state) is themed
    lookup.assert_called_once_with(name)
    provider.refresh()
    assert provider.icon(state) is themed
    assert lookup.call_count == 2


@pytest.mark.parametrize("session", [SessionKind.KDE, SessionKind.OTHER])
@pytest.mark.parametrize("state", list(TrayState))
def test_empty_theme_falls_back_to_both_files(
    qapp: QApplication,
    bundled_icons: None,
    monkeypatch: pytest.MonkeyPatch,
    session: SessionKind,
    state: TrayState,
) -> None:
    from PyQt5.QtGui import QIcon

    fallback = QIcon()
    add_pixmap = Mock(wraps=fallback.addPixmap)
    monkeypatch.setattr(fallback, "addPixmap", add_pixmap)
    lookup = Mock(return_value=fallback)
    monkeypatch.setattr(QIcon, "fromTheme", lookup)
    provider = TrayIconProvider(session)
    icon = provider.icon(state)
    lookup.assert_called_once_with(provider.icon_name(state))
    assert not icon.isNull()
    assert provider.has_icon(state)
    assert add_pixmap.call_count == 2
    assert {(size.width(), size.height()) for size in icon.availableSizes()} == {(16, 16), (22, 22)}
    for size in (16, 22):
        assert not icon.pixmap(size, size).isNull()


@pytest.mark.parametrize("state", list(TrayState))
@pytest.mark.parametrize("dark", [None, False, True])
def test_fly_renders_panel_color_and_refreshes(
    qapp: QApplication,
    bundled_icons: None,
    monkeypatch: pytest.MonkeyPatch,
    state: TrayState,
    dark: bool | None,
) -> None:
    from PyQt5.QtGui import QIcon

    lookup = Mock(side_effect=AssertionError("Fly must not use the icon theme"))
    monkeypatch.setattr(QIcon, "fromTheme", lookup)
    theme = PanelTheme() if dark is not None else None
    if theme is not None:
        theme.is_dark = bool(dark)
    provider = TrayIconProvider(SessionKind.FLY, theme)
    icon = provider.icon(state)
    assert not icon.isNull()
    assert provider.has_icon(state)
    assert {(size.width(), size.height()) for size in icon.availableSizes()} == {(16, 16), (22, 22)}
    # Внутренняя точка строки знака: полностью непрозрачна, вне сглаженных краёв.
    assert icon.pixmap(16, 16).toImage().pixelColor(5, 10).name() == (
        "#eff0f1" if dark else "#232629"
    )
    if state == TrayState.LISTENING:
        assert icon.pixmap(16, 16).toImage().pixelColor(11, 5).name() == "#12b3a0"
    if theme is not None:
        theme.is_dark = not theme.is_dark
    assert provider.icon(state) is icon
    provider.refresh()
    refreshed = provider.icon(state)
    assert refreshed is not icon
    assert refreshed.pixmap(16, 16).toImage().pixelColor(5, 10).name() == (
        "#eff0f1" if theme is not None and theme.dark else "#232629"
    )
    lookup.assert_not_called()


@pytest.mark.parametrize("session", [SessionKind.KDE, SessionKind.FLY])
@pytest.mark.parametrize("failed_size", [16, 22])
@pytest.mark.parametrize(
    "failure", ["missing", "unreadable", "garbage", "invalid-utf8", "broken-svg", "empty"]
)
def test_bad_icon_is_empty_and_warns_without_qt_stderr(
    qapp: QApplication,
    bundled_icons: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    session: SessionKind,
    failed_size: int,
    failure: str,
) -> None:
    from PyQt5.QtGui import QIcon

    name = "astravoice-tray-idle.svg"
    for size in (16, 22):
        relative = Path("icons") / "hicolor" / f"{size}x{size}" / "status" / name
        path = tmp_path / relative
        path.parent.mkdir(parents=True)
        if size != failed_size:
            path.write_bytes((DATA / relative).read_bytes())
        elif failure == "unreadable":
            # Каталог вместо файла стабильно вызывает OSError даже при запуске от root.
            path.mkdir()
        elif failure != "missing":
            path.write_bytes(
                {
                    "garbage": b"this is not SVG",
                    "invalid-utf8": b"\xff\xfe\x00",
                    "broken-svg": b'<svg xmlns="http://www.w3.org/2000/svg"><path',
                    "empty": b"",
                }[failure]
            )
    monkeypatch.setattr(paths, "data_dir_static", lambda: tmp_path)
    caplog.set_level(logging.WARNING, logger="astra_voice.ui.tray_icons")
    provider = TrayIconProvider(session)

    icon = provider.icon(TrayState.IDLE)

    assert isinstance(icon, QIcon)
    assert icon.isNull()
    assert not provider.has_icon(TrayState.IDLE)
    assert provider.icon(TrayState.IDLE) is icon
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.name == "astra_voice.ui.tray_icons"
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert TrayState.IDLE.value in record.getMessage()
    stderr = capfd.readouterr().err
    assert "qt.svg" not in stderr
    assert stderr == ""


@pytest.mark.parametrize("session", [SessionKind.KDE, SessionKind.FLY])
@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("has_icon_first", [False, True])
def test_has_icon_shares_reads_and_failures_with_icon_until_refresh(
    qapp: QApplication,
    bundled_icons: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session: SessionKind,
    available: bool,
    has_icon_first: bool,
) -> None:
    if not available:
        monkeypatch.setattr(paths, "data_dir_static", lambda: tmp_path)
    provider = TrayIconProvider(session)
    with patch.object(Path, "open", autospec=True, side_effect=Path.open) as read:
        if has_icon_first:
            assert provider.has_icon(TrayState.IDLE) is available
        icon = provider.icon(TrayState.IDLE)
        assert icon.isNull() is not available
        # Отсутствие файла теперь определяется поиском до попытки чтения.
        reads_per_load = 2 if available else 0
        assert read.call_count == reads_per_load

        for _ in range(2):
            assert provider.has_icon(TrayState.IDLE) is available
            assert provider.icon(TrayState.IDLE) is icon
        assert read.call_count == reads_per_load

        provider.refresh()
        assert provider.has_icon(TrayState.IDLE) is available
        assert provider.icon(TrayState.IDLE) is not icon
        assert read.call_count == 2 * reads_per_load


@pytest.mark.parametrize("session", [SessionKind.KDE, SessionKind.FLY])
@pytest.mark.parametrize("failure", ["false", "null", "exception"])
def test_pixmap_failure_is_empty_and_warns(
    qapp: QApplication,
    bundled_icons: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    session: SessionKind,
    failure: str,
) -> None:
    from PyQt5.QtGui import QPixmap

    load = QPixmap.loadFromData

    def failed_load(pixmap: QPixmap, data: bytes, format: str) -> bool:
        if failure == "exception":
            raise RuntimeError("SVG renderer failed")
        if failure == "false":
            assert load(pixmap, data, format)
            assert not pixmap.isNull()
            return False
        return True  # Даже успешный loadFromData не гарантирует непустой QPixmap.

    monkeypatch.setattr(QPixmap, "loadFromData", failed_load)
    provider = TrayIconProvider(session)
    assert provider.icon(TrayState.IDLE).isNull()
    assert not provider.has_icon(TrayState.IDLE)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert caplog.records[0].exc_info is None


@pytest.mark.parametrize("failure", ["lookup", "render", "null"])
def test_theme_failure_is_empty_and_warns(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    from PyQt5.QtGui import QIcon, QPixmap

    pixmap = QPixmap(16, 16)
    pixmap.fill()
    themed = QIcon(pixmap)
    lookup = Mock(return_value=themed)
    if failure == "lookup":
        lookup.side_effect = RuntimeError("Icon theme failed")
    elif failure == "render":
        monkeypatch.setattr(themed, "pixmap", Mock(side_effect=RuntimeError("Renderer failed")))
    else:
        monkeypatch.setattr(themed, "pixmap", Mock(return_value=QPixmap()))
    monkeypatch.setattr(QIcon, "fromTheme", lookup)
    provider = TrayIconProvider(SessionKind.KDE)
    assert not provider.has_icon(TrayState.IDLE)
    assert provider.icon(TrayState.IDLE).isNull()
    lookup.assert_called_once()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert caplog.records[0].exc_info is None


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (TrayState.IDLE, "Astra Voice — готов · Ctrl+Space"),
        (TrayState.LISTENING, "Слушаю…"),
        (TrayState.PROCESSING, "Распознаю…"),
        (TrayState.DONE, "Готово"),
        (TrayState.ERROR, "Микрофон недоступен — открыть"),
        (TrayState.NOKEY, "Горячая клавиша не захвачена — выбрать другую"),
    ],
)
def test_tooltip(state: TrayState, expected: str) -> None:
    assert TrayIconProvider(SessionKind.KDE).tooltip(state) == expected


def test_idle_tooltip_hotkey() -> None:
    assert TrayIconProvider(SessionKind.FLY).tooltip(TrayState.IDLE, hotkey="Alt+F9") == (
        "Astra Voice — готов · Alt+F9"
    )
