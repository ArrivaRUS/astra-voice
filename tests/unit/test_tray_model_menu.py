"""Регрессии обновления и выбора моделей в настоящем offscreen-меню."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock, call

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QAction, QApplication
from test_tray_model_sync_independent import Rig
from test_tray_model_sync_independent import qapp as qapp
from test_tray_model_sync_independent import rig as rig

from astra_voice.models.store import StoreError
from helpers.scripted_model_port import FIRST, SECOND, pump_until

pytestmark = pytest.mark.unit


def test_unchanged_ticks_preserve_open_menu_and_actions(
    rig: Rig, qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    submenu = rig.model_action().menu()
    actions = rig.choices()
    submenu.popup(QPoint(10, 10))
    submenu.setActiveAction(actions[1])
    calls = Mock(wraps=rig.tray.set_models)
    monkeypatch.setattr(rig.tray, "set_models", calls)

    for _ in range(50):
        rig.downloads.modelsChanged.emit()
        qapp.processEvents()

    calls.assert_not_called()
    assert submenu.isVisible()
    assert submenu.activeAction() is actions[1]
    assert submenu.actions() == actions
    submenu.close()


@pytest.mark.parametrize("progress_ticks", [False, True])
def test_unchanged_availability_skips_checks_after_priming(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, progress_ticks: bool
) -> None:
    rig.port.install_both()
    rig.port.records[(SECOND.id, SECOND.revision)] = "broken"
    rig.downloads.modelsChanged.emit()
    checks = Mock(wraps=rig.downloads.can_make_current)
    monkeypatch.setattr(rig.downloads, "can_make_current", checks)
    for tick in range(50):
        if progress_ticks:
            rig.downloads._card_progress[SECOND.id] = tick / 50
        rig.downloads.modelsChanged.emit()
    checks.assert_not_called()
    assert not rig.choices()[1].isEnabled()


@pytest.mark.parametrize("field", ["badge", "state", "message", "uninstalled-state"])
def test_availability_change_checks_each_installed_model_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    rig.port.install_both()
    if field == "uninstalled-state":
        rig.port.records.pop((SECOND.id, SECOND.revision))
    rig.downloads.modelsChanged.emit()
    checks = Mock(wraps=rig.downloads.can_make_current)
    monkeypatch.setattr(rig.downloads, "can_make_current", checks)
    if field == "badge":
        rig.port.current = (SECOND.id, SECOND.revision)
    elif field in {"state", "uninstalled-state"}:
        rig.downloads._card_states[SECOND.id] = "queued"
    else:
        rig.downloads._card_messages[SECOND.id] = "Модель ожидает проверки"
    # Один сигнал очереди порождает modelsChanged и activeModelChanged моста.
    rig.downloads.modelsChanged.emit()
    expected_ids = [FIRST.id] if field == "uninstalled-state" else [FIRST.id, SECOND.id]
    assert checks.call_args_list == [call(model_id) for model_id in expected_ids]
    rig.downloads.modelsChanged.emit()
    assert checks.call_args_list == [call(model_id) for model_id in expected_ids]


def test_name_change_updates_menu_without_rechecking_availability(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    checks = Mock(wraps=rig.downloads.can_make_current)
    monkeypatch.setattr(rig.downloads, "can_make_current", checks)
    rig.downloads._entries = (rig.port.catalog[0], replace(rig.port.catalog[1], name="Новое имя"))
    rig.downloads.modelsChanged.emit()
    checks.assert_not_called()
    assert rig.choices()[1].text() == "Новое имя"


@pytest.mark.parametrize("error", [OSError("unavailable"), StoreError("unavailable")])
def test_public_availability_check_refuses_storage_errors(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    rig.port.install_both()
    monkeypatch.setattr(rig.port, "record_state", Mock(side_effect=error))
    assert not rig.downloads.can_make_current(SECOND.id)
    assert not rig.bridge.can_select_model(SECOND.id)


def test_active_only_update_preserves_action_identity(rig: Rig) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    actions = rig.choices()
    rig.tray.set_models(
        [(FIRST.id, "Одинаковая модель"), (SECOND.id, "Одинаковая модель")], SECOND.id
    )
    assert rig.choices() == actions
    assert rig.checked_ids() == [SECOND.id]


@pytest.mark.parametrize("busy", ["_rechecking", "_queue_running", "_shutting_down"])
def test_busy_refusal_opens_models_once(rig: Rig, busy: str) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    setattr(rig.downloads, busy, True)
    try:
        rig.choose(SECOND.id)
        for _ in range(3):
            rig.downloads.modelsChanged.emit()
        rig.show_models.assert_called_once_with()
        assert rig.switcher.calls == []
        assert rig.checked_ids() == [FIRST.id]
    finally:
        setattr(rig.downloads, busy, False)


@pytest.mark.parametrize("result", ["ok", "failed", "too-slow", "low-memory"])
def test_pending_switch_keeps_confirmed_check_and_resolves_once(
    rig: Rig, result: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    rig.choose(SECOND.id)
    for _ in range(3):
        rig.downloads.modelsChanged.emit()
    assert rig.checked_ids() == [FIRST.id]
    rig.show_models.assert_not_called()

    if result == "low-memory":
        monkeypatch.setattr(rig.switcher, "mem_available_mb", lambda: 1.0)
    rig.switcher.finish(result)
    for _ in range(3):
        rig.downloads.modelsChanged.emit()
    assert rig.checked_ids() == [SECOND.id if result == "ok" else FIRST.id]
    assert rig.show_models.call_count == (0 if result == "ok" else 1)


def test_switch_from_settings_keeps_confirmed_check_without_opening_window(rig: Rig) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    rig.bridge.makeModelCurrent(SECOND.id)
    assert rig.checked_ids() == [FIRST.id]
    rig.switcher.finish("failed")
    assert rig.checked_ids() == [FIRST.id]
    rig.show_models.assert_not_called()


@pytest.mark.parametrize("invalid", ["broken", "revoked"])
def test_invalid_installed_model_is_disabled_without_rebuild(rig: Rig, invalid: str) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    actions = rig.choices()
    if invalid == "broken":
        rig.port.records[(SECOND.id, SECOND.revision)] = "broken"
    else:
        rig.port.revoked.add(SECOND.id)
    rig.downloads.modelsChanged.emit()
    assert rig.choices() == actions
    assert not actions[1].isEnabled()
    rig.port.records[(SECOND.id, SECOND.revision)] = "ok"
    rig.port.revoked.clear()
    rig.downloads.modelsChanged.emit()
    assert rig.choices() == actions
    assert actions[1].isEnabled()


def test_two_hundred_real_rebuilds_release_actions(rig: Rig) -> None:
    rig.port.install_both()
    for index in range(200):
        rig.port.catalog = (FIRST, replace(SECOND, name=f"Модель {index}"))
        rig.downloads._entries = rig.port.catalog
        rig.downloads.modelsChanged.emit()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert len(rig.tray._model_group.actions()) == 2
        # QMenu владеет также собственной menuAction.
        assert len(rig.tray._model_menu.findChildren(QAction)) == 3


def test_rebuild_defers_deletion_of_removed_actions(rig: Rig) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    actions = rig.choices()
    rig.tray.set_models([("third", "Третья"), ("fourth", "Четвёртая")], "third")
    assert not any(sip.isdeleted(action) for action in actions)
    assert all(action not in rig.tray._model_group.actions() for action in actions)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert all(sip.isdeleted(action) for action in actions)


@pytest.mark.parametrize("pause", [False, True])
def test_real_click_with_parent_triggered_subscriber(
    rig: Rig, qapp: QApplication, pause: bool
) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    rig.switcher.enough_memory = not pause
    seen: list[str] = []
    rig.menu.triggered.connect(lambda action: seen.append(str(action.data())))
    rig.menu.popup(QPoint(10, 10))
    qapp.processEvents()
    model_action = rig.model_action()
    rig.menu.setActiveAction(model_action)
    QTest.keyClick(rig.menu, Qt.Key_Right)
    qapp.processEvents()
    submenu = model_action.menu()
    target = rig.choices()[1]
    QTest.mouseClick(submenu, Qt.LeftButton, Qt.NoModifier, submenu.actionGeometry(target).center())
    qapp.processEvents()
    assert seen == [SECOND.id]
    assert rig.checked_ids() == [FIRST.id]
    if pause:
        rig.show_models.assert_called_once_with()
    else:
        rig.switcher.finish("ok")
        assert rig.checked_ids() == [SECOND.id]
        rig.show_models.assert_not_called()
    rig.menu.close()


@pytest.mark.parametrize("result", ["ok", "failed"])
def test_synchronous_switch_reply_resolves_once(
    rig: Rig, result: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    monkeypatch.setattr(rig.switcher, "switch_model", lambda **kwargs: rig.switcher.finish(result))
    rig.choose(SECOND.id)
    rig.downloads.modelsChanged.emit()
    assert rig.checked_ids() == [SECOND.id if result == "ok" else FIRST.id]
    assert rig.show_models.call_count == (0 if result == "ok" else 1)


def test_sync_without_switcher_accepts_new_current(rig: Rig) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    rig.downloads._switcher = None
    rig.choose(SECOND.id)
    assert rig.checked_ids() == [SECOND.id]
    rig.show_models.assert_not_called()


def test_another_selection_during_switch_preserves_original_pending(rig: Rig) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    rig.choose(SECOND.id)
    rig.choose(FIRST.id)
    assert rig.checked_ids() == [FIRST.id]
    rig.show_models.assert_called_once_with()
    assert len(rig.switcher.calls) == 1
    rig.switcher.finish("ok")
    assert rig.checked_ids() == [SECOND.id]
    rig.show_models.assert_called_once_with()


def test_real_click_can_rebuild_menu_inside_triggered(rig: Rig, qapp: QApplication) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    seen: list[str] = []
    rig.menu.triggered.connect(lambda action: seen.append(str(action.data())))
    rig.tray.on_model_selected = lambda model_id: rig.tray.set_models(
        [(FIRST.id, "Новая первая"), (SECOND.id, "Новая вторая")], FIRST.id
    )
    rig.menu.popup(QPoint(10, 10))
    qapp.processEvents()
    model_action = rig.model_action()
    rig.menu.setActiveAction(model_action)
    QTest.keyClick(rig.menu, Qt.Key_Right)
    qapp.processEvents()
    submenu = model_action.menu()
    target = rig.choices()[1]
    QTest.mouseClick(submenu, Qt.LeftButton, Qt.NoModifier, submenu.actionGeometry(target).center())
    assert seen == [SECOND.id]
    assert rig.choices()[0].text() == "Новая первая"
    assert not sip.isdeleted(target)
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert sip.isdeleted(target)
    assert rig.checked_ids() == [FIRST.id]
    submenu.close()


@pytest.mark.parametrize("invalid", ["broken", "revoked", "pending"])
@pytest.mark.parametrize("changed_field", ["badge", "state", "message", None])
def test_model_installed_this_session_then_invalidated_refreshes_on_row_change(
    rig: Rig, invalid: str, changed_field: str | None
) -> None:
    rig.port.open_gates()
    rig.bridge.toggleModel(FIRST.id)
    rig.bridge.toggleModel(SECOND.id)
    rig.bridge.startSelectedDownloads()
    pump_until(
        lambda: rig.bridge.downloadState == "done" and rig.downloads._model_thread is None,
        "установка обеих моделей в памяти",
    )
    # Сохранённое сообщение карточки имеет приоритет, в том числе перед отзывом.
    if changed_field == "badge":
        rig.port.current = (SECOND.id, SECOND.revision)
    message = "Модель установлена"
    rig.downloads._set_card(SECOND, "installed", message)
    before = next(row for row in rig.bridge.models if row["id"] == SECOND.id)
    actions = rig.choices()
    target = next(action for action in actions if action.data() == SECOND.id)
    assert target.isEnabled()
    if invalid == "revoked":
        rig.port.revoked.add(SECOND.id)
    else:
        rig.port.records[(SECOND.id, SECOND.revision)] = invalid
    if changed_field == "state":
        rig.downloads._set_card(SECOND, "failed", message)
    elif changed_field == "message":
        rig.downloads._set_card(SECOND, "installed", "Требуется проверка модели")
    else:
        if changed_field == "badge":
            rig.port.current = (FIRST.id, FIRST.revision)
        rig.downloads.modelsChanged.emit()
    row = next(row for row in rig.bridge.models if row["id"] == SECOND.id)
    fields = ("id", "badge", "state", "message")
    assert (tuple(row[key] for key in fields) != tuple(before[key] for key in fields)) == (
        changed_field is not None
    )
    assert rig.choices() == actions
    assert target.isEnabled() == (changed_field is None)
    if changed_field is None:
        # Известное ограничение: прежний fingerprint оставляет QAction доступным.
        rig.choose(SECOND.id)
        for _ in range(3):
            rig.downloads.modelsChanged.emit()
        rig.show_models.assert_called_once_with()
        assert rig.switcher.calls == []
        assert rig.checked_ids() == [FIRST.id]
        return

    rig.port.revoked.clear()
    rig.port.records[(SECOND.id, SECOND.revision)] = "ok"
    if changed_field == "badge":
        rig.port.current = (SECOND.id, SECOND.revision)
    rig.downloads._set_card(SECOND, "installed", message)
    assert rig.choices() == actions
    assert target.isEnabled()


@pytest.mark.parametrize("revoked_revision", ["r1", "r2"])
def test_availability_checks_installed_revision_instead_of_latest_catalog_revision(
    rig: Rig, revoked_revision: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.port.install_both()
    rig.port.catalog = (rig.port.catalog[0], replace(rig.port.catalog[1], revision="r2"))
    rig.downloads._entries = rig.port.catalog
    monkeypatch.setattr(
        rig.port,
        "is_revoked",
        lambda entry: entry.id == SECOND.id and entry.revision == revoked_revision,
    )
    monkeypatch.setattr(
        rig.port,
        "revoked_revision",
        lambda model_id, revision: model_id == SECOND.id and revision == revoked_revision,
    )
    # Публичная проверка использует установленную r1 независимо от строки меню.
    assert rig.downloads.can_make_current(SECOND.id) == (revoked_revision == "r2")
    assert rig.port.records[(SECOND.id, "r1")] == "ok"
