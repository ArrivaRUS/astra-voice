"""Регрессии обновления и выбора моделей в настоящем offscreen-меню."""

from __future__ import annotations

import logging
from dataclasses import replace
from unittest.mock import Mock, call

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QAction, QApplication
from test_tray_model_sync_independent import ManagedPort, Rig
from test_tray_model_sync_independent import qapp as qapp
from test_tray_model_sync_independent import rig as rig

from astra_voice.core import settings as settings_mod
from astra_voice.models.store import StoreError
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.model_downloads import ModelDownloads
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
    checks = Mock(wraps=rig.downloads.can_make_current_or_unknown)
    monkeypatch.setattr(rig.downloads, "can_make_current_or_unknown", checks)
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
    checks = Mock(wraps=rig.downloads.can_make_current_or_unknown)
    monkeypatch.setattr(rig.downloads, "can_make_current_or_unknown", checks)
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
    checks = Mock(wraps=rig.downloads.can_make_current_or_unknown)
    monkeypatch.setattr(rig.downloads, "can_make_current_or_unknown", checks)
    rig.downloads._entries = (rig.port.catalog[0], replace(rig.port.catalog[1], name="Новое имя"))
    rig.downloads.modelsChanged.emit()
    checks.assert_not_called()
    assert rig.choices()[1].text() == "Новое имя"


@pytest.mark.parametrize("error_type", [OSError, StoreError])
def test_memory_read_error_keeps_cards_fresh_and_tray_updates(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    monkeypatch.setattr(rig.port, "mem_total_mb", Mock(side_effect=error_type("unavailable")))
    rig.downloads._entries = (rig.port.catalog[0], replace(rig.port.catalog[1], name="Новое имя"))

    rows = rig.bridge.models
    assert [row["id"] for row in rows] == [FIRST.id, SECOND.id]
    assert rows[1]["name"] == "Новое имя"
    assert [(row["state"], row["badge"]) for row in rows] == [
        ("installed", "active"),
        ("installed", "installed"),
    ]
    assert all(
        (row["hint"], row["hintKind"], row["memoryShortage"]) == ("", "", False) for row in rows
    )
    assert rig.downloads.models_read_failed is False

    rig.downloads.modelsChanged.emit()
    assert [action.data() for action in rig.choices()] == [FIRST.id, SECOND.id]
    assert rig.choices()[1].text() == "Новое имя"
    assert rig.checked_ids() == [FIRST.id]
    assert rig.downloads.models_read_failed is False


@pytest.mark.parametrize("error", [OSError("unavailable"), StoreError("unavailable")])
@pytest.mark.parametrize("failed_read", ["record_state", "installed_ids"])
def test_public_availability_check_refuses_storage_errors(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, error: Exception, failed_read: str
) -> None:
    rig.port.install_both()
    monkeypatch.setattr(rig.port, failed_read, Mock(side_effect=error))
    assert not rig.downloads.can_make_current(SECOND.id)
    assert not rig.bridge.can_select_model(SECOND.id)
    assert rig.downloads.can_make_current_or_unknown(SECOND.id) is None
    assert rig.bridge.can_select_model_or_unknown(SECOND.id) is None


@pytest.mark.parametrize("error_type", [OSError, StoreError])
@pytest.mark.parametrize("failed_read", ["record_state", "installed_ids"])
def test_first_models_read_keeps_cards_when_one_store_read_fails(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    failed_read: str,
) -> None:
    port = ManagedPort()
    port.install_both()
    downloads = ModelDownloads(port)
    record_state = port.record_state

    def unavailable(model_id: str, revision: str) -> str:
        if model_id == SECOND.id:
            raise error_type("private/path model-id")
        return record_state(model_id, revision)

    caplog.set_level(logging.WARNING)
    try:
        # A fresh queue has never exposed models; no snapshot is primed or cleared.
        with monkeypatch.context() as failing:
            failing.setattr(
                port,
                failed_read,
                unavailable
                if failed_read == "record_state"
                else Mock(side_effect=error_type("private/path model-id")),
            )
            for _ in range(3):
                rows = downloads.models
                assert [row["id"] for row in rows] == [FIRST.id, SECOND.id]
                assert downloads.models_read_failed
                if failed_read == "record_state":
                    assert (rows[0]["state"], rows[0]["badge"]) == ("installed", "active")
                    assert (rows[1]["state"], rows[1]["badge"]) == ("installed", "")
                else:
                    assert [(row["state"], row["badge"]) for row in rows] == [
                        ("available", ""),
                        ("available", ""),
                    ]
            assert len(caplog.records) == 1
            assert caplog.records[0].exc_info is None
            assert "private" not in caplog.text and "model-id" not in caplog.text

        rows = downloads.models
        assert not downloads.models_read_failed
        assert [row["id"] for row in rows] == [FIRST.id, SECOND.id]
        assert [(row["state"], row["badge"]) for row in rows] == [
            ("installed", "active"),
            ("installed", "installed"),
        ]
    finally:
        downloads.shutdown()
        downloads.deleteLater()


@pytest.mark.parametrize("error", [OSError("unavailable"), StoreError("unavailable")])
@pytest.mark.parametrize("previously_enabled", [False, True])
def test_storage_error_keeps_previous_availability_and_retries_unchanged_rows(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, error: Exception, previously_enabled: bool
) -> None:
    rig.port.install_both()
    rig.port.records[(SECOND.id, SECOND.revision)] = "ok" if previously_enabled else "broken"
    rig.downloads.modelsChanged.emit()
    actions = rig.choices()
    assert actions[1].isEnabled() == previously_enabled

    # Строки карточек не меняются между неудачной и успешной проверками.
    # Ошибка возникает в проверке доступности, после чтения списка карточек.
    rig.downloads._card_messages[SECOND.id] = "Модель ожидает проверки"
    rows = rig.bridge.models
    monkeypatch.setattr(type(rig.bridge), "models", property(lambda _: rows))
    rig.port.records[(FIRST.id, FIRST.revision)] = "broken"
    rig.port.records[(SECOND.id, SECOND.revision)] = "broken" if previously_enabled else "ok"
    record_state = rig.port.record_state

    def unavailable(model_id: str, revision: str) -> str:
        if model_id == SECOND.id:
            raise error
        return record_state(model_id, revision)

    monkeypatch.setattr(rig.port, "record_state", unavailable)
    rig.bridge.modelsChanged.emit()
    assert rig.choices() == actions
    assert not actions[0].isEnabled()
    assert actions[1].isEnabled() == previously_enabled

    monkeypatch.setattr(rig.port, "record_state", record_state)
    checks = Mock(wraps=rig.downloads.can_make_current_or_unknown)
    monkeypatch.setattr(rig.downloads, "can_make_current_or_unknown", checks)
    rig.bridge.modelsChanged.emit()
    assert rig.choices() == actions
    assert not actions[0].isEnabled()
    assert actions[1].isEnabled() != previously_enabled
    assert checks.call_args_list == [call(FIRST.id), call(SECOND.id)]

    for _ in range(50):
        rig.bridge.modelsChanged.emit()
    assert checks.call_args_list == [call(FIRST.id), call(SECOND.id)]


@pytest.mark.parametrize("error", [OSError("unavailable"), StoreError("unavailable")])
@pytest.mark.parametrize("previously_enabled", [False, True])
def test_installed_ids_error_preserves_availability_and_retries_unchanged_rows(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, error: Exception, previously_enabled: bool
) -> None:
    rig.port.install_both()
    rig.port.records[(SECOND.id, SECOND.revision)] = "ok" if previously_enabled else "broken"
    rig.downloads.modelsChanged.emit()
    actions = rig.choices()
    assert actions[0].isEnabled()
    assert actions[1].isEnabled() == previously_enabled

    # Список карточек прочитан успешно; сбоит следующий запрос installed_ids,
    # который выполняет проверка доступности для меню.
    rig.downloads._card_messages[SECOND.id] = "Модель ожидает проверки"
    rows = rig.bridge.models
    monkeypatch.setattr(type(rig.bridge), "models", property(lambda _: rows))
    rig.port.records[(FIRST.id, FIRST.revision)] = "broken"
    rig.port.records[(SECOND.id, SECOND.revision)] = "broken" if previously_enabled else "ok"
    installed_ids = rig.port.installed_ids
    monkeypatch.setattr(rig.port, "installed_ids", Mock(side_effect=error))
    rig.bridge.modelsChanged.emit()
    assert rig.choices() == actions
    assert actions[0].isEnabled()
    assert actions[1].isEnabled() == previously_enabled

    monkeypatch.setattr(rig.port, "installed_ids", installed_ids)
    checks = Mock(wraps=rig.downloads.can_make_current_or_unknown)
    monkeypatch.setattr(rig.downloads, "can_make_current_or_unknown", checks)
    rig.bridge.modelsChanged.emit()
    assert rig.choices() == actions
    assert not actions[0].isEnabled()
    assert actions[1].isEnabled() != previously_enabled
    assert checks.call_args_list == [call(FIRST.id), call(SECOND.id)]

    for _ in range(50):
        rig.bridge.modelsChanged.emit()
    assert checks.call_args_list == [call(FIRST.id), call(SECOND.id)]


@pytest.mark.parametrize("error_type", [OSError, StoreError])
@pytest.mark.parametrize(
    "failure",
    [
        "bridge-tick",
        "bridge-select",
        "badge-record_state",
        "badge-current_ids",
        "make-record_state",
        "make-current_ids",
        "make-installed_ids",
        "make-set_current",
    ],
)
def test_ib40_store_error_does_not_crash_tray(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    failure: str,
) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    actions = rig.choices()
    updates = Mock(wraps=rig.tray.set_models)
    monkeypatch.setattr(rig.tray, "set_models", updates)
    error = error_type("private/path model-id private-data")

    with monkeypatch.context() as failing:
        kind, method = failure.split("-", 1)
        if kind == "bridge":

            def unavailable(_bridge: object) -> list[dict[str, object]]:
                raise error

            failing.setattr(type(rig.bridge), "models", property(unavailable))
        elif kind == "badge":
            if method == "record_state":
                record_state = rig.port.record_state

                def unavailable_record(model_id: str, revision: str) -> str:
                    if model_id == SECOND.id:
                        raise error
                    return record_state(model_id, revision)

                failing.setattr(rig.port, method, unavailable_record)
            else:
                failing.setattr(rig.port, method, Mock(side_effect=error))
            rig.downloads._entries = (
                replace(rig.port.catalog[0], name="Свежая первая"),
                rig.port.catalog[1],
            )
            rig.port.records[(FIRST.id, FIRST.revision)] = "broken"
            # UI rows stay fresh while the tray retains its confirmed menu.
            rows = rig.bridge.models
            assert [row["id"] for row in rows] == [FIRST.id, SECOND.id]
            assert rows[0]["name"] == "Свежая первая"
            assert (rows[0]["state"], rows[0]["badge"]) == ("broken", "installed")
            assert (rows[1]["state"], rows[1]["badge"]) == ("installed", "installed")
            assert rig.downloads.models_read_failed
            assert rig.downloads.installedCount == 2
            assert rig.downloads.installedSummary.startswith("Установлено 2")
        else:
            make_current = rig.bridge.make_model_current

            def failing_make_current(model_id: str) -> bool:
                with monkeypatch.context() as store_failure:
                    store_failure.setattr(rig.port, method, Mock(side_effect=error))
                    return make_current(model_id)

            failing.setattr(rig.bridge, "make_model_current", failing_make_current)

        if failure in {"bridge-tick", "badge-record_state", "badge-current_ids"}:
            rig.bridge.modelsChanged.emit()
        else:
            rig.choose(SECOND.id)
            rig.show_models.assert_called_once_with()
        updates.assert_not_called()
        assert rig.choices() == actions
        assert rig.checked_ids() == [FIRST.id]

    # Новый сигнал повторяет чтение после ошибки; обновляется и имя, и доступность.
    rig.port.records[(FIRST.id, FIRST.revision)] = "ok"
    rig.downloads._entries = (rig.port.catalog[0], replace(rig.port.catalog[1], name="Новое имя"))
    rig.port.records[(SECOND.id, SECOND.revision)] = "broken"
    rig.bridge.modelsChanged.emit()
    assert rig.choices()[1].text() == "Новое имя"
    assert not rig.choices()[1].isEnabled()
    assert not rig.downloads.models_read_failed
    assert rig.show_models.call_count == (0 if "tick" in failure or "badge" in failure else 1)


@pytest.mark.parametrize("error_type", [OSError, StoreError])
@pytest.mark.parametrize("operation", ["install", "remove"])
def test_install_remove_race_recovers_after_failed_store_read(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception], operation: str
) -> None:
    rig.port.install_both()
    if operation == "install":
        rig.port.records.pop((SECOND.id, SECOND.revision))
    rig.downloads.modelsChanged.emit()
    previous = rig.tray._model_entries
    updates = Mock(wraps=rig.tray.set_models)
    monkeypatch.setattr(rig.tray, "set_models", updates)
    if operation == "install":
        rig.port.records[(SECOND.id, SECOND.revision)] = "ok"
    else:
        rig.port.records.pop((SECOND.id, SECOND.revision))

    with monkeypatch.context() as failing:
        failing.setattr(rig.port, "installed_ids", Mock(side_effect=error_type("private")))
        rig.downloads._entries = (
            rig.port.catalog[0],
            replace(rig.port.catalog[1], name="Свежая вторая"),
        )
        rows = rig.bridge.models
        assert [row["id"] for row in rows] == [FIRST.id, SECOND.id]
        assert rows[1]["name"] == "Свежая вторая"
        assert [(row["state"], row["badge"]) for row in rows] == [
            ("available", ""),
            ("available", ""),
        ]
        assert rig.downloads.models_read_failed
        rig.bridge.modelsChanged.emit()
        updates.assert_not_called()
        assert rig.tray._model_entries == previous

    rig.bridge.modelsChanged.emit()
    expected = [FIRST.id, SECOND.id] if operation == "install" else [FIRST.id]
    assert [model_id for model_id, _ in rig.tray._model_entries or ()] == expected
    updates.assert_called_once()
    assert not rig.downloads.models_read_failed
    checks = Mock(wraps=rig.downloads.can_make_current_or_unknown)
    monkeypatch.setattr(rig.downloads, "can_make_current_or_unknown", checks)
    updates.reset_mock()
    for _ in range(50):
        rig.bridge.modelsChanged.emit()
    checks.assert_not_called()
    updates.assert_not_called()


@pytest.mark.parametrize("failed_read", ["record_state", "installed_ids", "current_ids"])
def test_store_warning_once_per_failure_series_and_recovery_resets(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failed_read: str
) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    caplog.set_level(logging.WARNING)

    for series in range(2):
        with monkeypatch.context() as failing:
            failing.setattr(
                rig.port, failed_read, Mock(side_effect=StoreError("private/path model-id"))
            )
            for _ in range(5):
                rig.bridge.modelsChanged.emit()
                rig.downloads.can_make_current_or_unknown(SECOND.id)
                rig.choose(SECOND.id)
            assert len(caplog.records) == series + 1
            assert all(record.exc_info is None for record in caplog.records)
            assert all("private" not in record.getMessage() for record in caplog.records)
            assert all("model-id" not in record.getMessage() for record in caplog.records)
        rig.bridge.modelsChanged.emit()


@pytest.mark.parametrize("reader", ["downloads", "bridge"])
@pytest.mark.parametrize("failed_read", ["record_state", "installed_ids", "current_ids"])
def test_without_tray_successful_models_read_starts_new_warning_series(
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reader: str,
    failed_read: str,
) -> None:
    port = ManagedPort()
    port.install_both()
    downloads = ModelDownloads(port)
    bridge = SettingsBridge(settings_mod.from_dict({}), downloads=downloads, save=Mock())
    source = bridge if reader == "bridge" else downloads
    caplog.set_level(logging.WARNING)
    try:
        for series in range(2):
            with monkeypatch.context() as failing:
                failing.setattr(
                    port, failed_read, Mock(side_effect=StoreError("private/path model-id"))
                )
                for _ in range(5):
                    assert len(source.models) == 2
                    assert downloads.models_read_failed
                assert len(caplog.records) == series + 1
                assert all(record.exc_info is None for record in caplog.records)
                assert "private" not in caplog.text and "model-id" not in caplog.text
            assert [row["id"] for row in source.models] == [FIRST.id, SECOND.id]
            assert not downloads.models_read_failed
    finally:
        downloads.shutdown()
        bridge.deleteLater()
        downloads.deleteLater()


def test_tray_warning_series_requires_successful_rows_and_availability(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    caplog.set_level(logging.WARNING)
    check = rig.bridge.can_select_model_or_unknown

    def unavailable(model_id: str) -> bool | None:
        with monkeypatch.context() as failing:
            failing.setattr(
                rig.port, "record_state", Mock(side_effect=StoreError("private/path model-id"))
            )
            return check(model_id)

    for series in range(2):
        with monkeypatch.context() as failing:
            failing.setattr(
                rig.port, "installed_ids", Mock(side_effect=StoreError("private/path model-id"))
            )
            rig.downloads.modelsChanged.emit()
        with monkeypatch.context() as failing:
            failing.setattr(rig.bridge, "can_select_model_or_unknown", unavailable)
            for _ in range(5):
                assert len(rig.bridge.models) == 2
                assert not rig.downloads.models_read_failed
                rig.downloads.modelsChanged.emit()
            assert len(caplog.records) == series + 1
        rig.downloads.modelsChanged.emit()
        assert not rig.downloads.models_read_failed
    assert all(record.exc_info is None for record in caplog.records)
    assert "private" not in caplog.text and "model-id" not in caplog.text
    rig.show_models.assert_not_called()


@pytest.mark.parametrize("availability_error", ["unknown", "raised"])
@pytest.mark.parametrize(
    ("with_switcher", "switch_result"), [(False, "ok"), (True, "ok"), (True, "failed")]
)
def test_accepted_selection_survives_reentrant_other_model_availability_error(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    with_switcher: bool,
    switch_result: str,
    availability_error: str,
) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    if not with_switcher:
        rig.downloads._switcher = None
    check = rig.bridge.can_select_model_or_unknown
    failed_checks: list[str] = []

    def unavailable(model_id: str) -> bool | None:
        # Выбор и его чтения хранилища успешны. Только вложенное обновление
        # меню сбоит на соседней модели после set_current.
        if model_id == FIRST.id and rig.port.current == (SECOND.id, SECOND.revision):
            failed_checks.append(model_id)
            if availability_error == "raised":
                raise StoreError("private/path model-id")
            with monkeypatch.context() as store_failure:
                store_failure.setattr(
                    rig.port,
                    "record_state",
                    Mock(side_effect=StoreError("private/path model-id")),
                )
                return check(model_id)
        return check(model_id)

    with monkeypatch.context() as failing:
        failing.setattr(rig.bridge, "can_select_model_or_unknown", unavailable)
        rig.choose(SECOND.id)
        assert failed_checks
        assert rig.port.current == (SECOND.id, SECOND.revision)
        if with_switcher:
            assert rig.switcher.calls == [(SECOND.min_ram_mb, False)]
            assert rig.checked_ids() == [FIRST.id]
        rig.show_models.assert_not_called()

    if with_switcher:
        rig.switcher.finish(switch_result)
    for _ in range(3):
        rig.downloads.modelsChanged.emit()
    assert rig.checked_ids() == [SECOND.id if switch_result == "ok" else FIRST.id]
    if switch_result == "ok":
        rig.show_models.assert_not_called()
    else:
        rig.show_models.assert_called_once_with()


@pytest.mark.parametrize("with_switcher", [False, True])
@pytest.mark.parametrize("failed_read", ["record_state", "installed_ids", "set_current"])
def test_selection_failure_warning_waits_for_successful_tick(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    with_switcher: bool,
    failed_read: str,
) -> None:
    rig.port.install_both()
    rig.downloads.modelsChanged.emit()
    if not with_switcher:
        rig.downloads._switcher = None
    make_current = rig.bridge.make_model_current
    updates = Mock(wraps=rig.tray.set_models)
    monkeypatch.setattr(rig.tray, "set_models", updates)

    def failing_make_current(model_id: str) -> bool:
        with monkeypatch.context() as failing:
            failing.setattr(
                rig.port, failed_read, Mock(side_effect=StoreError("private/path model-id"))
            )
            return make_current(model_id)

    monkeypatch.setattr(rig.bridge, "make_model_current", failing_make_current)
    caplog.set_level(logging.WARNING)
    for series in range(2):
        for _ in range(5):
            rig.choose(SECOND.id)
        assert len(caplog.records) == series + 1
        assert all(record.exc_info is None for record in caplog.records)
        assert all("private" not in record.getMessage() for record in caplog.records)
        assert rig.show_models.call_count == (series + 1) * 5
        assert rig.checked_ids() == [FIRST.id]
        updates.assert_not_called()
        rig.bridge.modelsChanged.emit()


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
