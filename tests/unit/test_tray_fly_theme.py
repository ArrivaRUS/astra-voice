"""US-4.7, ревизия 20 — цвет значка лотка во Fly по теме (T-298, T-299). До кода, 21.10.

Живая проверка 18.10 (Fly 1.8.4): при тёмной теме монохромный XEmbed-значок «чёрный на
чёрном» — `DictationRuntime` создаёт `TrayIconProvider(session)` без источника темы, а
`FlyThemeSource` — заглушка. Контракт (решение Юрки 21.10, тесты его фиксируют):

* `TrayIconProvider(session, theme=None, *, panel_dark=None)` — как сейчас; во Fly цвет
  знака: `TaskbarColor` панели, если известен, иначе `theme.dark`; цветные точки
  состояний (запись/распознавание/готово/ошибка) не меняются;
* провайдер подписан на `theme` (`ThemeSource.subscribe`): смена темы сама сбрасывает
  кэш значков, ручной `refresh()` не нужен;
* `TrayIconProvider.set_theme(theme: ThemeSource | None) -> None` — подключить источник
  позже (после создания runtime), с тем же сбросом кэша и уведомлением;
* `TrayIconProvider.subscribe(callback: Callable[[], None]) -> None` — уведомление ПОСЛЕ
  сброса кэша; `Tray` подписывается в конструкторе и заново ставит значок;
* `DictationRuntime.set_theme_source(source)` → `self.provider.set_theme(source)`;
  `app.main` вызывает его с `theme_bridge.source` (если мост темы есть).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from test_app_shutdown import Rig
from test_app_shutdown import rig as rig
from test_theme_fly import SCHEMES, FlyHome, atomic_write
from test_theme_fly import fly as fly
from test_tray import Harness
from test_tray import forbid_real_x as forbid_real_x
from test_tray import harness as harness

from astra_voice import app as app_mod
from astra_voice.core import paths
from astra_voice.core.theme import FlyThemeSource, ThemeSource
from astra_voice.platform.session import SessionKind
from astra_voice.runtime import DictationRuntime
from astra_voice.ui import tray_icons
from astra_voice.ui.tray_icons import TrayIconProvider, TrayState

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers.qt_app import get_qapplication  # noqa: E402

if TYPE_CHECKING:
    from PyQt5.QtGui import QIcon
    from PyQt5.QtWidgets import QApplication

pytestmark = pytest.mark.unit
DATA = Path(__file__).resolve().parents[2] / "data"
LIGHT_MARK = "#eff0f1"  # светлый знак — для тёмной темы/панели
DARK_MARK = "#232629"  # тёмный знак — для светлой
DOT = {
    TrayState.LISTENING: "#12b3a0",
    TrayState.PROCESSING: "#e8a33a",
    TrayState.DONE: "#2fa36b",
    TrayState.ERROR: "#d64545",
}


class StaticTheme(ThemeSource):
    def __init__(self, dark: bool) -> None:
        super().__init__()
        self.is_dark = dark

    @property
    def dark(self) -> bool:
        return self.is_dark

    @property
    def accent(self) -> str | None:
        return None


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return get_qapplication()


@pytest.fixture(autouse=True)
def no_fly_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    """~/.fly/theme машины не читаем: цвет панели «неизвестен», решает тема."""
    monkeypatch.setattr(tray_icons, "fly_theme_files", lambda: ())


@pytest.fixture
def bundled_icons(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from PyQt5.QtGui import QIcon

    monkeypatch.setattr(paths, "resource_root", lambda: tmp_path / "usr/share/astra-voice")
    monkeypatch.setattr(paths, "data_dir_static", lambda: DATA)
    monkeypatch.setattr(QIcon, "fromTheme", lambda name: QIcon())


def mark(icon: QIcon) -> str:
    """Внутренняя точка нижней полосы знака (вне сглаживания), как в test_tray_icons."""
    name: str = icon.pixmap(16, 16).toImage().pixelColor(5, 10).name()
    return name


def dot(icon: QIcon) -> str:
    name: str = icon.pixmap(16, 16).toImage().pixelColor(11, 5).name()
    return name


def fly_source(fly: FlyHome, file: str) -> FlyThemeSource:
    fly.write_paletterc(color_scheme=fly.system / file, background="#808080")
    return fly.source()


# ── T-298 · значок лотка по теме Fly ───────────────────────────────────────


@pytest.mark.parametrize("state", list(TrayState))
def test_t298_dark_fly_theme_gives_light_mono_mark_and_keeps_state_colors(
    qapp: QApplication, bundled_icons: None, fly: FlyHome, state: TrayState
) -> None:
    provider = TrayIconProvider(SessionKind.FLY, fly_source(fly, "AstraDark.colors"))
    icon = provider.icon(state)
    assert not icon.isNull()
    assert mark(icon) == LIGHT_MARK, "тёмная тема Fly: знак должен быть светлым"
    if state in DOT:
        assert dot(icon) == DOT[state], "цвет состояния не должен зависеть от темы"


@pytest.mark.parametrize("state", list(TrayState))
def test_t298_light_fly_theme_keeps_dark_mono_mark(
    qapp: QApplication, bundled_icons: None, fly: FlyHome, state: TrayState
) -> None:
    """Контроль: как до 1.0."""
    provider = TrayIconProvider(SessionKind.FLY, fly_source(fly, "AstraLight.colors"))
    icon = provider.icon(state)
    assert mark(icon) == DARK_MARK
    if state in DOT:
        assert dot(icon) == DOT[state]


@pytest.mark.parametrize(
    ("panel", "file", "expected"),
    [
        (False, "AstraDark.colors", DARK_MARK),  # светлая панель важнее тёмной темы
        (True, "AstraLight.colors", LIGHT_MARK),  # тёмная панель важнее светлой темы
    ],
)
def test_t298_known_panel_color_wins_over_theme(
    qapp: QApplication,
    bundled_icons: None,
    fly: FlyHome,
    panel: bool,
    file: str,
    expected: str,
) -> None:
    """Контроль приоритета: TaskbarColor панели, если известен, иначе тема."""
    provider = TrayIconProvider(SessionKind.FLY, fly_source(fly, file), panel_dark=lambda: panel)
    assert mark(provider.icon(TrayState.IDLE)) == expected


# ── T-299 · живая смена темы → значок без перезапуска ──────────────────────


def icon_marks(provider: TrayIconProvider) -> tuple[str, str]:
    return mark(provider.icon(TrayState.IDLE)), mark(provider.command_icon(TrayState.IDLE))


def observe_file_write(path: Path, action: Callable[[], None]) -> None:
    """Доставить настоящее fileChanged и собрать уведомления за бюджет истории ≤ 2 с."""
    from PyQt5.QtCore import QEventLoop, QFileSystemWatcher, QTimer

    delivered: list[str] = []
    watcher = QFileSystemWatcher([str(path)])
    watcher.fileChanged.connect(delivered.append)
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(2000)
    action()
    loop.exec_()
    assert delivered, "контрольный watcher не получил файловое событие за 2 с"


def test_t299_panel_only_change_repaints_both_icons_and_notifies_once(
    qapp: QApplication, bundled_icons: None, fly: FlyHome
) -> None:
    source = fly_source(fly, "AstraLight.colors")
    panel = fly.home / ".fly" / "theme" / "current.themerc"
    atomic_write(panel, "[Variables]\nTaskbarColor=#FFFFFF\n")
    provider = TrayIconProvider(SessionKind.FLY, source, panel_paths=(panel,))
    assert icon_marks(provider) == (DARK_MARK, DARK_MARK)
    seen: list[tuple[str, str]] = []
    provider.subscribe(lambda: seen.append(icon_marks(provider)))
    source.start()
    provider.start()
    try:
        observe_file_write(
            panel, lambda: atomic_write(panel, "[Variables]\nTaskbarColor=#101010\n")
        )
        assert source.dark is False, "в этом сценарии тема окон не менялась"
        assert seen == [(LIGHT_MARK, LIGHT_MARK)]
        assert icon_marks(provider) == (LIGHT_MARK, LIGHT_MARK)
    finally:
        provider.stop()
        source.stop()


@pytest.mark.parametrize("order", ["window-first", "panel-first", "unknown-panel-window-first"])
def test_t299_window_and_panel_changes_in_either_order_keep_current_icons(
    qapp: QApplication, bundled_icons: None, fly: FlyHome, order: str
) -> None:
    source = fly_source(fly, "AstraLight.colors")
    panel = fly.home / ".fly" / "theme" / "current.themerc"
    initially_unknown = order == "unknown-panel-window-first"
    atomic_write(
        panel,
        "[Variables]\nTaskbarColor=unknown\n"
        if initially_unknown
        else "[Variables]\nTaskbarColor=#FFFFFF\n",
    )
    provider = TrayIconProvider(SessionKind.FLY, source, panel_paths=(panel,))
    assert icon_marks(provider) == (DARK_MARK, DARK_MARK)
    seen: list[tuple[str, str]] = []
    provider.subscribe(lambda: seen.append(icon_marks(provider)))
    changes: dict[str, tuple[Path, Callable[[], None]]] = {
        "window": (
            fly.paletterc,
            lambda: fly.write_paletterc(color_scheme=fly.system / "AstraDark.colors"),
        ),
        "panel": (
            panel,
            lambda: atomic_write(
                panel,
                "[Variables]\nTaskbarColor=#FFFFFF\n"
                if initially_unknown
                else "[Variables]\nTaskbarColor=#101010\n",
            ),
        ),
    }
    source.start()
    provider.start()
    try:
        parts = ["panel", "window"] if order == "panel-first" else ["window", "panel"]
        for part in parts:
            path, action = changes[part]
            observe_file_write(path, action)
            if initially_unknown:
                expected = LIGHT_MARK if part == "window" else DARK_MARK
                assert icon_marks(provider) == (expected, expected)
                expected_seen = [(LIGHT_MARK, LIGHT_MARK)]
                if part == "panel":
                    expected_seen.append((DARK_MARK, DARK_MARK))
                assert seen == expected_seen
                continue
            panel_changed = part == "panel" or order == "panel-first"
            expected = LIGHT_MARK if panel_changed else DARK_MARK
            assert icon_marks(provider) == (expected, expected)
            assert seen == ([(LIGHT_MARK, LIGHT_MARK)] if panel_changed else [])
        assert source.dark is True
    finally:
        provider.stop()
        source.stop()


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [("#FFFFFF", "#F0F0F0", DARK_MARK), ("#101010", "#202020", LIGHT_MARK)],
    ids=["still-light", "still-dark"],
)
def test_t299_same_effective_panel_color_does_not_notify(
    qapp: QApplication,
    bundled_icons: None,
    fly: FlyHome,
    before: str,
    after: str,
    expected: str,
) -> None:
    source = fly_source(fly, "AstraLight.colors")
    panel = fly.home / ".fly" / "theme" / "current.themerc"
    atomic_write(panel, f"[Variables]\nTaskbarColor={before}\n")
    provider = TrayIconProvider(SessionKind.FLY, source, panel_paths=(panel,))
    assert icon_marks(provider) == (expected, expected)
    seen: list[tuple[str, str]] = []
    provider.subscribe(lambda: seen.append(icon_marks(provider)))
    source.start()
    provider.start()
    try:
        observe_file_write(
            panel, lambda: atomic_write(panel, f"[Variables]\nTaskbarColor={after}\n")
        )
        assert seen == []
        assert icon_marks(provider) == (expected, expected)
    finally:
        provider.stop()
        source.stop()


def test_t299_provider_follows_theme_change_without_manual_refresh(
    qapp: QApplication, bundled_icons: None, fly: FlyHome
) -> None:
    source = fly_source(fly, "AstraLight.colors")
    provider = TrayIconProvider(SessionKind.FLY, source)
    assert mark(provider.icon(TrayState.IDLE)) == DARK_MARK
    fly.write_paletterc(color_scheme=fly.system / "AstraDark.colors")
    assert source.reload() is True
    # Без provider.refresh(): провайдер сам подписан на источник темы.
    assert mark(provider.icon(TrayState.IDLE)) == LIGHT_MARK
    assert mark(provider.command_icon(TrayState.IDLE)) == LIGHT_MARK


def test_t299_provider_notifies_after_cache_drop(
    qapp: QApplication, bundled_icons: None, fly: FlyHome
) -> None:
    source = fly_source(fly, "AstraLight.colors")
    provider = TrayIconProvider(SessionKind.FLY, source)
    provider.icon(TrayState.IDLE)
    seen: list[str] = []
    provider.subscribe(lambda: seen.append(mark(provider.icon(TrayState.IDLE))))
    atomic_write(fly.system / "AstraLight.colors", SCHEMES["AstraDark.colors"])
    assert source.reload() is True
    assert seen == [LIGHT_MARK], "подписчик провайдера должен видеть уже новый значок"


def test_t299_set_theme_attaches_source_later(qapp: QApplication, bundled_icons: None) -> None:
    provider = TrayIconProvider(SessionKind.FLY)
    assert mark(provider.icon(TrayState.IDLE)) == DARK_MARK  # без темы — как до 1.0
    seen: list[int] = []
    provider.subscribe(lambda: seen.append(1))
    theme = StaticTheme(dark=True)
    provider.set_theme(theme)
    assert seen == [1]
    assert mark(provider.icon(TrayState.IDLE)) == LIGHT_MARK
    theme.is_dark = False
    theme._notify()
    assert seen == [1, 1]
    assert mark(provider.icon(TrayState.IDLE)) == DARK_MARK


def test_t299_tray_reapplies_icon_when_provider_changes(harness: Harness) -> None:
    harness.tray.set_state(TrayState.IDLE)
    harness.provider.subscribe.assert_called_once()
    callback = harness.provider.subscribe.call_args.args[0]
    harness.icon.setIcon.reset_mock()
    harness.provider.icon.side_effect = lambda state: f"icon-new:{state.value}"
    callback()
    harness.icon.setIcon.assert_called_with("icon-new:idle")


def test_t299_runtime_set_theme_source_reaches_provider() -> None:
    runtime: Any = Mock()
    theme = StaticTheme(dark=True)
    DictationRuntime.set_theme_source(runtime, theme)
    runtime.provider.set_theme.assert_called_once_with(theme)


def test_t299_main_wires_theme_source_into_runtime(rig: Rig) -> None:
    source = StaticTheme(dark=True)
    rig.theme.source = source
    assert app_mod.main([]) == 7
    rig.runtime.set_theme_source.assert_called_once_with(source)


def test_t299_main_without_theme_bridge_does_not_wire(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контроль: моста темы нет (сбой источника) — runtime работает как до 1.0."""
    monkeypatch.setattr(app_mod, "_make_theme_bridge", Mock(return_value=None))
    assert app_mod.main([]) == 7
    rig.runtime.set_theme_source.assert_not_called()
