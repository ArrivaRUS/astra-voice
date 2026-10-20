"""Рекомендация выбирается только мастером и один раз за жизнь общей очереди."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtTest import QSignalSpy

from astra_voice.core.policy import Policy, PolicyStatus
from astra_voice.core.settings import Settings
from astra_voice.models.catalog import Catalog, RevokedEntry
from astra_voice.models.store import ModelStore
from astra_voice.net.gate import NetworkGate
from astra_voice.ui.bridges import OnboardingController, SettingsBridge
from astra_voice.ui.model_downloads import ModelDownloads, ModelService
from helpers.model_rig import FakeModelPort, ModelRig, model_rig_session
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def qapp() -> Iterator[None]:
    get_qapplication()
    yield
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)


@pytest.fixture
def model_rig() -> Iterator[ModelRig]:
    yield from model_rig_session()


def test_preselect_requires_opt_in_and_emits_selection_once() -> None:
    port = FakeModelPort()
    downloads = ModelDownloads(port)
    changed = QSignalSpy(downloads.selectionChanged)
    cards = QSignalSpy(downloads.modelsChanged)
    assert not downloads.models[0]["selected"]

    downloads.preselect_recommended()
    assert downloads.models[0]["selected"]
    assert downloads.selectionSummary == "Будет скачано 226 МБ"
    assert downloads.canContinueFromModel
    assert len(changed) == len(cards) == 1
    downloads.preselect_recommended()
    assert len(changed) == len(cards) == 1
    assert port.download_calls == 0
    assert downloads.downloadState == "idle"


def test_wizard_preselects_own_downloads(model_rig: ModelRig) -> None:
    _, create = model_rig
    controller = create()
    assert controller._owns_downloads
    assert not controller.models[0]["selected"]
    controller.next()
    assert controller.step == 2
    assert controller.models[0]["selected"]
    assert controller.canContinueFromModel


def test_auto_selection_uses_existing_space_and_download_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = FakeModelPort()
    downloads = ModelDownloads(port)
    downloads.preselect_recommended()
    port.space = False
    assert not downloads.selectionFits
    assert not downloads.canContinueFromModel
    port.space = True
    assert downloads.selectionFits
    assert downloads.canContinueFromModel
    begin = Mock()
    monkeypatch.setattr(downloads, "_begin_queue", begin)
    downloads.startSelectedDownloads()
    begin.assert_called_once_with((port.entry,))
    assert port.download_calls == 0


def test_restored_wizard_model_step_preselects_immediately(model_rig: ModelRig) -> None:
    _, create = model_rig
    controller = create(onboarding_step=2)
    assert controller.step == 2
    assert controller.models[0]["selected"]


def test_settings_keep_empty_selection_without_wizard() -> None:
    downloads = ModelDownloads(FakeModelPort())
    bridge = SettingsBridge(Settings(), downloads=downloads, save=Mock())
    assert not bridge.models[0]["selected"]
    assert not downloads.canContinueFromModel
    assert bridge.selectionSummary == ""


def test_shared_wizard_reentry_and_card_refresh_preserve_manual_deselection() -> None:
    port = FakeModelPort()
    downloads = ModelDownloads(port)
    settings = Settings(extra={"onboarding_language_set": True})
    bridge = SettingsBridge(settings, downloads=downloads, save=Mock())
    first = OnboardingController(
        bridge, settings=settings, downloads=downloads, device_provider=lambda: []
    )
    try:
        assert not first._owns_downloads
        assert not first.models[0]["selected"]
        first.next()
        assert first.models[0]["selected"]
        first.toggleModel(port.entry.id)
        # Каталог/карточки перечитаны, а рекомендация осталась той же.
        port.entry = replace(port.entry, description="Обновлённое описание")
        downloads._entries = port.entries()
        downloads.network_changed()
        downloads.preselect_recommended()
        assert not first.models[0]["selected"]
        first.shutdown()
        second = OnboardingController(
            bridge, settings=settings, downloads=downloads, device_provider=lambda: []
        )
        try:
            assert not second.models[0]["selected"]
            assert not bridge.models[0]["selected"]
        finally:
            second.shutdown()
    finally:
        first.shutdown()
        downloads.shutdown()


def test_recheck_before_first_model_step_does_not_consume_preselection() -> None:
    downloads = ModelDownloads(FakeModelPort())
    downloads._rechecking = True
    settings = Settings(extra={"onboarding_language_set": True})
    bridge = SettingsBridge(settings, downloads=downloads, save=Mock())
    controller = OnboardingController(
        bridge, settings=settings, downloads=downloads, device_provider=lambda: []
    )
    try:
        assert controller.step == 1
        assert not controller.models[0]["selected"]
        downloads._rechecking = False
        controller.next()
        assert controller.step == 2
        assert controller.models[0]["selected"]
    finally:
        controller.shutdown()
        downloads.shutdown()


def test_manual_choice_before_first_opt_in_is_respected() -> None:
    port = FakeModelPort()
    downloads = ModelDownloads(port)
    downloads.toggleModel(port.entry.id)
    downloads.toggleModel(port.entry.id)
    downloads.preselect_recommended()
    assert not downloads.models[0]["selected"]


@pytest.mark.parametrize("record", ["ok", "broken", "older-without-badge"])
def test_installed_recommendation_is_not_preselected(
    monkeypatch: pytest.MonkeyPatch, record: str
) -> None:
    port = FakeModelPort()
    port.ready = record == "ok"
    port.damaged = record == "broken"
    if record == "older-without-badge":
        monkeypatch.setattr(port, "installed_ids", lambda: ((port.entry.id, "older"),))
    downloads = ModelDownloads(port)
    downloads.preselect_recommended()
    assert not downloads.models[0]["selected"]
    assert downloads._recommended_preselect_done
    # Даже удаление прежней установки не возвращает первоначальное предложение.
    port.ready = port.damaged = False
    monkeypatch.setattr(port, "installed_ids", lambda: ())
    downloads._set_card(port.entry, "available")
    downloads.preselect_recommended()
    assert not downloads.models[0]["selected"]


@pytest.mark.parametrize("state", ["available", "failed"])
@pytest.mark.parametrize("denial", ["user", "admin", "invalid", "environment"])
def test_network_gate_denial_blocks_auto_selection_even_for_failed_card(
    monkeypatch: pytest.MonkeyPatch, state: str, denial: str
) -> None:
    port = FakeModelPort()
    settings = Settings(offline=denial == "user")
    policy = Policy(
        values={"offline": True} if denial == "admin" else {},
        status=PolicyStatus.INVALID if denial == "invalid" else PolicyStatus.OK,
    )
    if denial == "environment":
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    gate = NetworkGate(settings, policy)
    monkeypatch.setattr(port, "allowed", lambda: gate.allowed("download"))
    monkeypatch.setattr(port, "refusal", lambda: gate.refusal("download"))
    downloads = ModelDownloads(port)
    downloads._set_card(port.entry, state)
    downloads.preselect_recommended()
    assert not downloads.models[0]["selected"]
    assert not downloads._recommended_preselect_done
    # Снятие временного сетевого запрета разрешает первую попытку выбора.
    monkeypatch.setattr(port, "allowed", lambda: (True, ""))
    monkeypatch.setattr(port, "refusal", lambda: "")
    downloads.network_changed()
    downloads.preselect_recommended()
    assert downloads.models[0]["selected"]
    assert downloads._recommended_preselect_done


@pytest.mark.parametrize("state", ["available", "failed"])
def test_selectable_uninstalled_states_are_preselected(state: str) -> None:
    port = FakeModelPort()
    downloads = ModelDownloads(port)
    downloads._set_card(port.entry, state)
    downloads.preselect_recommended()
    assert downloads.models[0]["selected"]


@pytest.mark.parametrize(
    "state", ["queued", "downloading", "verifying", "switching", "policy", "offline-user"]
)
def test_unselectable_card_states_do_not_consume_preselection(state: str) -> None:
    port = FakeModelPort()
    downloads = ModelDownloads(port)
    downloads._set_card(port.entry, state)
    downloads.preselect_recommended()
    assert not downloads.models[0]["selected"]
    assert not downloads._recommended_preselect_done
    downloads._set_card(port.entry, "available")
    downloads.preselect_recommended()
    assert downloads.models[0]["selected"]
    assert downloads._recommended_preselect_done


@pytest.mark.parametrize("guard", ["_rechecking", "_shutting_down", "_switching_entry"])
def test_toggle_guards_also_block_preselection(guard: str) -> None:
    downloads = ModelDownloads(FakeModelPort())
    setattr(downloads, guard, True)
    downloads.preselect_recommended()
    assert not downloads.models[0]["selected"]
    assert not downloads._recommended_preselect_done
    setattr(downloads, guard, None if guard == "_switching_entry" else False)
    downloads.preselect_recommended()
    assert downloads.models[0]["selected"]
    assert downloads._recommended_preselect_done


def test_missing_recommendation_or_catalog_entry_is_not_preselected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = FakeModelPort()
    downloads = ModelDownloads(port)
    with monkeypatch.context() as patch:
        patch.setattr(port, "recommended", lambda: None)
        downloads.preselect_recommended()
        assert not downloads.models[0]["selected"]
        assert downloads._recommended_preselect_done
    downloads.preselect_recommended()
    assert not downloads.models[0]["selected"]
    downloads = ModelDownloads(port)
    downloads._entries = ()
    downloads.preselect_recommended()
    assert downloads.models == []
    assert downloads._recommended_preselect_done
    downloads._entries = port.entries()
    downloads.preselect_recommended()
    assert not downloads.models[0]["selected"]
    ModelDownloads(None).preselect_recommended()


def test_revoked_recommendation_is_not_preselected() -> None:
    port = FakeModelPort()
    port.revoked.add((port.entry.id, port.entry.revision))
    downloads = ModelDownloads(port)
    downloads.preselect_recommended()
    assert not downloads.models[0]["selected"]
    assert downloads._recommended_preselect_done
    port.revoked.clear()
    downloads.preselect_recommended()
    assert not downloads.models[0]["selected"]


def test_removed_catalog_entry_permanently_finishes_preselection() -> None:
    port = FakeModelPort()
    port.entry = replace(port.entry, removed_from_catalog=True)
    downloads = ModelDownloads(port)
    downloads.preselect_recommended()
    assert downloads.models == []
    assert downloads._recommended_preselect_done
    port.entry = replace(port.entry, removed_from_catalog=False)
    downloads._entries = port.entries()
    downloads.preselect_recommended()
    assert not downloads.models[0]["selected"]


def test_wizard_retries_card_refresh_only_on_model_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloads = ModelDownloads(FakeModelPort())
    downloads._rechecking = True
    preselect = Mock(wraps=downloads.preselect_recommended)
    monkeypatch.setattr(downloads, "preselect_recommended", preselect)
    settings = Settings(extra={"onboarding_language_set": True})
    bridge = SettingsBridge(settings, downloads=downloads, save=Mock())
    controller = OnboardingController(
        bridge, settings=settings, downloads=downloads, device_provider=lambda: []
    )
    try:
        downloads.modelsChanged.emit()
        preselect.assert_not_called()
        controller.next()
        assert controller.step == 2
        preselect.assert_called_once_with()
        assert not controller.models[0]["selected"]
        assert not downloads._recommended_preselect_done

        changed = QSignalSpy(downloads.selectionChanged)
        downloads._rechecking = False
        downloads.modelsChanged.emit()
        assert controller.models[0]["selected"]
        assert preselect.call_count >= 2
        assert len(changed) == 1
        downloads.modelsChanged.emit()
        assert controller.models[0]["selected"]
        assert len(changed) == 1

        # После ручного снятия галочки повторный сигнал её не восстанавливает.
        controller.toggleModel(controller.models[0]["id"])
        downloads.modelsChanged.emit()
        assert not controller.models[0]["selected"]
        assert len(changed) == 2
        controller.next()
        assert controller.step == 3
        preselect.reset_mock()
        downloads.modelsChanged.emit()
        preselect.assert_not_called()
    finally:
        controller.shutdown()
        downloads.shutdown()


def test_service_recommendation_excludes_revoked_entry(tmp_path: Path) -> None:
    entry = FakeModelPort().entry
    service = ModelService.__new__(ModelService)
    service._catalog = Catalog(1, 1, (RevokedEntry(entry.id, entry.revision, "revoked"),), (entry,))
    service._store = ModelStore(tmp_path / "models")
    assert service.recommended() is None
    downloads = ModelDownloads(service)
    downloads.preselect_recommended()
    assert downloads.models == []
