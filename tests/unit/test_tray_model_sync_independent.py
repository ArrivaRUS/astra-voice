"""Независимые сценарии меню: настоящий Tray → SettingsBridge → ModelDownloads.

Служба каталога и ответ рантайма управляются тестом; Qt-меню, сигналы мостов,
отказ по памяти и откат переключения работают без подмены продуктовой логики.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QAction, QApplication, QMenu

from astra_voice.app import _wire_tray_models
from astra_voice.core import settings as settings_mod
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.model_downloads import ModelDownloads
from astra_voice.ui.tray import Tray
from astra_voice.ui.tray_icons import TrayIconProvider
from helpers.qt_app import get_qapplication
from helpers.scripted_model_port import FIRST, SECOND, ScriptedModelPort

pytestmark = pytest.mark.unit


class ManagedPort(ScriptedModelPort):
    """Хранилище только в памяти; имена совпадают, идентификаторы различны."""

    def __init__(self) -> None:
        super().__init__(
            tuple(replace(entry, name="Одинаковая модель") for entry in (FIRST, SECOND))
        )

    def remove(self, model_id: str, revision: str) -> None:
        self.records.pop((model_id, revision))

    def install_both(self) -> None:
        self.records = {(entry.id, entry.revision): "ok" for entry in self.catalog}
        self.current = (FIRST.id, FIRST.revision)


class SwitchReply:
    def __init__(self) -> None:
        self.enough_memory = True
        self.calls: list[tuple[int, bool]] = []
        self.on_switch_finished: Callable[[str], None] | None = None

    def can_switch_without_pause(self, min_ram_mb: int) -> bool:
        return self.enough_memory

    def mem_available_mb(self) -> float | None:
        return None

    def switch_model(self, *, min_ram_mb: int, pause: bool = False) -> None:
        self.calls.append((min_ram_mb, pause))

    def finish(self, result: str) -> None:
        assert self.on_switch_finished is not None
        self.on_switch_finished(result)


@dataclass
class Rig:
    port: ManagedPort
    switcher: SwitchReply
    downloads: ModelDownloads
    bridge: SettingsBridge
    tray: Tray
    menu: QMenu
    show_models: Mock

    def model_action(self) -> QAction:
        actions = [action for action in self.menu.actions() if action.text().startswith("Модель")]
        assert len(actions) == 1
        return actions[0]

    def choices(self) -> list[QAction]:
        submenu = self.model_action().menu()
        assert submenu is not None
        return list(submenu.actions())

    def checked_ids(self) -> list[str]:
        return [str(action.data()) for action in self.choices() if action.isChecked()]

    def choose(self, model_id: str) -> None:
        action = next(action for action in self.choices() if action.data() == model_id)
        action.trigger()


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return get_qapplication()


@pytest.fixture
def rig(qapp: QApplication) -> Iterator[Rig]:
    port = ManagedPort()
    switcher = SwitchReply()
    downloads = ModelDownloads(port, switcher=switcher)
    bridge = SettingsBridge(settings_mod.from_dict({}), downloads=downloads, save=Mock())
    provider = Mock(spec=TrayIconProvider)
    provider.has_icon.return_value = True
    provider.tooltip.return_value = "AstraVoice — готов"
    icon = Mock()
    # Tray.start не вызываем: настоящая панель, X11 и транспорт D-Bus не нужны.
    tray = Tray(provider, tray_factory=lambda: icon, xembed_probe=Mock())
    menu: QMenu = icon.setContextMenu.call_args.args[0]
    show_models = Mock()
    _wire_tray_models(tray, bridge, show_models)
    yield Rig(port, switcher, downloads, bridge, tray, menu, show_models)
    downloads.shutdown()
    tray.stop()
    menu.close()
    menu.deleteLater()
    bridge.deleteLater()
    tray.deleteLater()


def test_models_arriving_after_wiring_update_closed_settings_menu(
    rig: Rig, qapp: QApplication
) -> None:
    assert rig.model_action().text() == "Модель не установлена"
    assert not rig.model_action().isEnabled()

    def service_ready() -> None:
        rig.port.install_both()
        rig.downloads.modelsChanged.emit()

    QTimer.singleShot(0, service_ready)
    assert rig.model_action().menu() is None
    qapp.processEvents()

    assert [action.data() for action in rig.choices()] == [FIRST.id, SECOND.id]
    assert rig.checked_ids() == [FIRST.id]
    rig.show_models.assert_not_called()


def test_duplicate_display_names_select_distinct_ids_and_follow_settings(rig: Rig) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    assert [action.text() for action in rig.choices()] == ["Одинаковая модель"] * 2

    rig.choose(SECOND.id)
    assert rig.switcher.calls == [(SECOND.min_ram_mb, False)]
    assert rig.port.current == (SECOND.id, SECOND.revision)
    rig.switcher.finish("ok")
    assert rig.checked_ids() == [SECOND.id]

    # Обратное переключение из настроек тоже обновляет уже созданное меню.
    rig.bridge.makeModelCurrent(FIRST.id)
    rig.switcher.finish("ok")
    assert rig.checked_ids() == [FIRST.id]
    rig.show_models.assert_not_called()


def test_failed_switch_rolls_menu_back_and_allows_retry(rig: Rig) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    rig.choose(SECOND.id)
    rig.switcher.finish("failed")

    assert rig.port.current == (FIRST.id, FIRST.revision)
    assert rig.checked_ids() == [FIRST.id]
    rig.choose(SECOND.id)
    rig.switcher.finish("ok")
    assert rig.port.current == (SECOND.id, SECOND.revision)
    assert rig.checked_ids() == [SECOND.id]
    assert len(rig.switcher.calls) == 2


def test_pause_refusal_keeps_active_check_and_offers_existing_settings(rig: Rig) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    rig.switcher.enough_memory = False

    rig.choose(SECOND.id)

    assert rig.switcher.calls == []
    assert rig.port.current == (FIRST.id, FIRST.revision)
    assert rig.checked_ids() == [FIRST.id]
    rig.show_models.assert_called_once_with()


def test_repeated_refresh_remove_install_and_empty_do_not_keep_stale_actions(rig: Rig) -> None:
    rig.port.install_both()
    for _ in range(3):
        rig.downloads.modelsChanged.emit()
        rig.bridge.activeModelChanged.emit()
        assert len(rig.choices()) == 2
        assert rig.checked_ids() == [FIRST.id]

    rig.bridge.removeModel(SECOND.id)
    assert rig.model_action().text() == "Модель: Одинаковая модель"
    assert rig.model_action().menu() is None
    assert not rig.model_action().isEnabled()

    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    assert len(rig.choices()) == 2
    rig.choose(SECOND.id)
    assert len(rig.switcher.calls) == 1
    rig.switcher.finish("ok")
    rig.bridge.removeModel(FIRST.id)
    assert rig.model_action().text() == "Модель: Одинаковая модель"

    # Служба сообщает, что на диске больше нет ни одной ревизии.
    rig.port.records.clear()
    rig.port.current = None
    rig.downloads.modelsChanged.emit()
    assert rig.model_action().text() == "Модель не установлена"
    assert rig.model_action().menu() is None
    assert not rig.model_action().isEnabled()
    rig.show_models.assert_not_called()
