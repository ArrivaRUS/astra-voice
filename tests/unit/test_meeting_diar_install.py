"""S7b1: pair job lifecycle, shared presentation and trusted hash transport."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QCoreApplication
from PyQt5.QtTest import QSignalSpy
from test_meeting_card_bridge import SaveRig
from test_meeting_jobs import MID
from test_meeting_jobs import Rig as JobsRig
from test_tray import qapp as qapp

from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.meetings.diar_models import enabled_diar_models, installed_diar_models
from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.policy import parse_policy
from astra_voice.models import diarization
from astra_voice.models.catalog import Catalog
from astra_voice.models.installer import InstallResult, ReasonCode
from astra_voice.ui.meeting_bridge import MeetingBridge
from astra_voice.ui.meeting_texts import diar_install_presentation

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
PAIR = {
    "segmentation_dir": "/trusted/seg",
    "embedder_dir": "/trusted/embed",
    "segmentation_sha256": "a" * 64,
    "embedder_sha256": "b" * 64,
}


class SyntheticService:
    def __init__(self) -> None:
        self.pair: dict[str, str] | None = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel: threading.Event | None = None
        self.calls = 0
        self.thread: int | None = None
        self.result = "installed"
        self.reason_code: ReasonCode = ""
        self.reason = ""

    def trusted_models(self) -> dict[str, str] | None:
        return self.pair

    def install(self, *, cancel: threading.Event, sink: Any) -> InstallResult:
        self.calls += 1
        self.thread = threading.get_ident()
        self.cancel = cancel
        self.started.set()
        sink("stage", ("downloading",))
        sink("source", ("corp",))
        while not self.release.wait(0.005) and not cancel.is_set():
            pass
        code = "cancelled" if cancel.is_set() else self.result
        if code == "installed":
            self.pair = dict(PAIR)
        sink("finished", (code, self.reason))
        return InstallResult(
            "ok" if code == "installed" else "error", self.reason, reason_code=self.reason_code
        )


def spin(until: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 3
    while not until() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.002)
    assert until()


@pytest.fixture
def bound(qapp: object) -> Iterator[tuple[MeetingBridge, SyntheticService]]:
    service = SyntheticService()
    bridge = MeetingBridge(
        Settings(meeting_diarization=True),
        store=Mock(),
        save=Mock(),
        diarization_service=service,
    )
    yield bridge, service
    service.release.set()
    bridge.shutdown()


def test_one_install_for_section_and_card_and_cancel(
    bound: tuple[MeetingBridge, SyntheticService],
) -> None:
    bridge, service = bound
    requested = QSignalSpy(bridge.diarizationInstallRequested)
    bridge.requestDiarizationInstall()  # also works without an open card
    assert service.started.wait(1)
    for _ in range(5):
        bridge.requestDiarizationInstall()
        bridge.diarizationInstallRequested.emit()  # handler guards duplicate signals too
    assert service.calls == 1
    assert service.thread != threading.get_ident()
    bridge.cancelDiarizationInstall()
    assert service.cancel is not None and service.cancel.is_set()
    spin(lambda: bridge.diarizationInstall["code"] == "cancelled")
    assert bridge.diarizationInstall["text"] == "Установка отменена"
    assert bridge.diarizationInstall["action"] == "Установить"
    assert len(requested) == 6
    bridge.leaveMeetingsSection()
    assert bridge.diarizationInstall["code"] == "missing"


def test_shutdown_cancels_thread_and_closes_channel(
    bound: tuple[MeetingBridge, SyntheticService],
) -> None:
    bridge, service = bound
    bridge.requestDiarizationInstall()
    assert service.started.wait(1)
    channel = bridge._diar_downloads._channel
    bridge.shutdown()
    assert service.cancel is not None and service.cancel.is_set()
    assert channel.closed
    assert bridge._diar_downloads._thread is None
    before = bridge.diarizationInstall
    channel.post("finished", ("installed", "", ""))
    QCoreApplication.processEvents()
    assert bridge.diarizationInstall == before


@pytest.mark.parametrize(
    "offline,policy,code",
    [
        (True, Policy(), "offline-user"),
        (False, Policy(values={"offline": "true"}), "policy"),
        (False, Policy(values={"profile": "corporate"}), "policy"),
    ],
)
def test_network_closed_before_click_has_no_button(
    qapp: object,
    offline: bool,
    policy: Policy,
    code: str,
) -> None:
    service = SyntheticService()
    bridge = MeetingBridge(
        Settings(meeting_diarization=True, offline=offline),
        policy=policy,
        store=Mock(),
        diarization_service=service,
    )
    try:
        assert bridge.diarizationInstall["code"] == code
        assert bridge.diarizationInstall["action"] == ""
        bridge.requestDiarizationInstall()
        assert service.calls == 0
    finally:
        bridge.shutdown()


@pytest.mark.parametrize("policy_value", [None, "off"])
def test_switch_off_hides_row_and_blocks_start(qapp: object, policy_value: str | None) -> None:
    service = SyntheticService()
    bridge = MeetingBridge(
        Settings(meeting_diarization=policy_value is not None),
        policy=Policy(raw={} if policy_value is None else {"meeting_diarization": policy_value}),
        store=Mock(),
        diarization_service=service,
    )
    try:
        assert not bridge.diarizationInstall["visible"]
        bridge.requestDiarizationInstall()
        assert not service.calls
    finally:
        bridge.shutdown()


CASES = [
    (
        "missing",
        "",
        False,
        "Не установлены — без них все собеседники подписываются «Собеседники» · 34 МБ",
        "Установить",
    ),
    ("downloading", "", False, "Скачиваю с huggingface.co · 42 %", "Отмена"),
    ("verifying", "", False, "Проверяю модели…", "Отмена"),
    ("installing", "", False, "Устанавливаю модели…", "Отмена"),
    ("installed", "", False, "Установлены · 34 МБ", ""),
    ("cancelled", "", False, "Установка отменена", "Установить"),
    (
        "failed:offline",
        "",
        False,
        "Не удалось загрузить модели — сеть отключена в настройках",
        "Повторить",
    ),
    (
        "failed:settings",
        "",
        False,
        "Не удалось загрузить модели — сеть отключена в настройках",
        "Повторить",
    ),
    (
        "failed:admin",
        "",
        False,
        "Не удалось загрузить модели — скачивание запрещено администратором",
        "Повторить",
    ),
    ("no-space", "", False, "Не удалось загрузить модели — не хватает места на диске", "Повторить"),
    (
        "broken",
        "",
        False,
        "Не удалось установить модели разделения. Попробуйте ещё раз.",
        "Повторить",
    ),
    (
        "broken",
        "",
        True,
        "Уже выполняется установка или удаление моделей разделения. Попробуйте позже.",
        "Повторить",
    ),
    ("broken", "revoked", False, "Эта версия отозвана авторами каталога", ""),
    ("damaged", "", False, "Файлы моделей не читаются", "Переустановить"),
    ("offline-user", "", False, "Не установлены · Включён офлайн-режим — «Сеть и обновления»", ""),
    ("policy", "", False, "Не установлены · Задано администратором: работа без сети", ""),
]


@pytest.mark.parametrize("code,reason,busy,text,action", CASES)
@pytest.mark.parametrize("surface", ["section", "card"])
def test_table_rows(
    bound: tuple[MeetingBridge, SyntheticService],
    code: str,
    reason: str,
    busy: bool,
    text: str,
    action: str,
    surface: str,
) -> None:
    bridge, _ = bound
    bridge._diar_code, bridge._diar_reason, bridge._diar_busy = code, reason, busy
    bridge._diar_percent = 42
    changed = QSignalSpy(bridge.diarizationInstallChanged)
    bridge._publish_diar_install()
    state = bridge.diarizationInstall
    assert len(changed) == 1
    assert state["code"] == code
    assert state["action"] == action
    if surface == "card":
        text = (
            "Модели установлены — следующие расшифровки будут разделены по голосам"
            if code == "installed"
            else "Разделение по голосам: модели не установлены"
            if code in {"missing", "offline-user", "policy"}
            else "Разделение по голосам: " + text[0].lower() + text[1:]
        )
    assert state["cardText" if surface == "card" else "text"] == text
    expected_names = {
        "Установить": "Установить модели для разделения по голосам, 34 МБ",
        "Отмена": "Отменить установку моделей разделения",
        "Повторить": "Повторить установку моделей разделения",
        "Переустановить": "Переустановить модели для разделения по голосам",
        "": "",
    }
    assert state["actionName"] == expected_names[action]


def test_corporate_source() -> None:
    state = diar_install_presentation("downloading", 42, "corp")
    assert state["text"] == "Скачиваю с корпоративного сервера · 42 %"


def test_revoked_result_and_catalog_recovery(bound: tuple[MeetingBridge, SyntheticService]) -> None:
    bridge, service = bound
    revoked = [False]
    serial = [1]
    bridge._diar_revoked = lambda: revoked[0]
    bridge._diar_catalog_token = lambda: serial[0]
    service.result, service.reason_code = "broken", "revoked"
    bridge.requestDiarizationInstall()
    assert service.started.wait(1)
    service.release.set()
    spin(lambda: bridge.diarizationInstall["code"] == "broken")
    assert bridge.diarizationInstall["action"] == ""
    spin(lambda: bridge._diar_downloads._thread is None)
    bridge.refreshDiarizationModels()
    bridge.requestDiarizationInstall()
    assert bridge.diarizationInstall["action"] == "" and service.calls == 1
    revoked[0] = True
    bridge.refreshDiarizationModels()
    bridge.requestDiarizationInstall()
    assert service.calls == 1 and bridge.diarizationInstall["action"] == ""
    revoked[0] = False
    serial[0] += 1
    bridge.refreshDiarizationModels()
    assert bridge.diarizationInstall["code"] == "missing"


def test_installed_card_result_until_reopen_and_provider_updates(
    tmp_path: Path, qapp: object
) -> None:
    rig = SaveRig(tmp_path)
    service = SyntheticService()
    rig.bridge.bindDiarizationService(service)
    rig.create(state=MeetingState.DONE)
    try:
        assert rig.open_card()["diarizationModelsMissing"]
        rig.bridge.requestDiarizationInstall()
        assert service.started.wait(1)
        service.release.set()
        spin(lambda: rig.bridge.diarizationInstall["code"] == "installed")
        card = rig.bridge.card
        assert not card["diarizationModelsMissing"]
        assert card["diarizationInstallVisible"]
        assert card["diarizationHint"] == (
            "Модели установлены — следующие расшифровки будут разделены по голосам"
        )
        assert not card["diarizationAction"]
        assert (
            enabled_diar_models(
                rig.bridge._stored, rig.bridge._parsed, rig.bridge._diar_models_provider
            )
            == PAIR
        )
        rig.bridge.closeMeeting()
        card = rig.open_card()
        assert not card["diarizationModelsMissing"] and not card["diarizationInstallVisible"]
        assert not card["diarizationHint"]
    finally:
        service.release.set()
        rig.bridge.shutdown()


def test_progress_rate_and_accessible_rate(bound: tuple[MeetingBridge, SyntheticService]) -> None:
    bridge, _ = bound
    now = [0.0]
    bridge._clock = lambda: now[0]
    bridge._diar_event("stage", ("downloading",))
    published: list[tuple[float, int, int]] = []
    bridge.diarizationInstallChanged.connect(
        lambda: published.append(
            (
                now[0],
                bridge.diarizationInstall["percent"],
                bridge.diarizationInstall["accessiblePercent"],
            )
        )
    )
    for step in range(201):
        now[0] = step / 100
        bridge._diar_event("progress", (step / 200,))
    assert len(published) <= 21
    assert all(b[0] - a[0] >= 0.1 - 1e-9 for a, b in zip(published, published[1:], strict=False))
    accessible = [(0.0, 0, 0)]
    for row in published:
        if row[2] != accessible[-1][2]:
            accessible.append(row)
    assert all(b[0] - a[0] >= 1 - 1e-9 for a, b in zip(accessible, accessible[1:], strict=False))
    now[0] = 2.01
    bridge._diar_event("progress", (0.99,))
    assert bridge.diarizationInstall["percent"] == 100
    now[0] = 2.11
    bridge._diar_progress_flush()
    assert bridge.diarizationInstall["percent"] == 99  # trailing sample is not lost


def test_untrusted_catalog_returns_none_and_job_skips_pair(tmp_path: Path) -> None:
    def provider() -> Catalog:
        return Catalog(serial=0, trust_epoch=0, entries=(), revoked=())

    assert installed_diar_models(catalog_provider=provider) is None
    rig = JobsRig(tmp_path)
    rig.jobs.diar_models = lambda: enabled_diar_models(
        Settings(meeting_diarization=True),
        parse_policy(Policy()),
        lambda: installed_diar_models(catalog_provider=provider),
    )
    rig.create()
    rig.jobs.on_recorded(MID)
    rig.jobs.tick()
    rig.loaded()
    assert rig.accepted()["diarization"] is False
    assert "diar_models" not in rig.role.last("meeting.job.start")
    rig.jobs.shutdown()


def test_provider_hashes_reach_diar_process_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_meeting_transcribe_diar import ScriptedDiar

    from astra_voice.worker.meeting_diar_client import DiarProcess
    from helpers.meeting_audio import Rig, bursts, make_disk, write_track

    provider = Mock(return_value=dict(PAIR))
    monkeypatch.setattr(diarization, "trusted_diarization_models", provider)
    jobs = JobsRig(tmp_path / "gui")
    jobs.jobs.diar_models = lambda: installed_diar_models()
    jobs.create(variant="app_stream")
    jobs.jobs.on_recorded(MID)
    jobs.jobs.tick()
    jobs.loaded()
    start = jobs.accepted()
    assert start["diar_models"] == PAIR
    provider.assert_called_once_with(None)
    disk = make_disk(tmp_path / "role")
    write_track(disk, "far", bursts([0.5]))
    write_track(disk, "mic", bursts([0.6]))
    role = Rig(disk)
    sent: list[dict[str, Any]] = []

    class SpyDiar(ScriptedDiar):
        def load(self, models: dict[str, str]) -> None:
            # Run the real load body; only the subprocess transport is synthetic.
            DiarProcess.load(cast(DiarProcess, self), models)

        def send(self, kind: str, **fields: Any) -> dict[str, Any]:
            sent.append({"type": kind, **fields})
            return sent[-1]

        def wait(self, *args: Any, **kwargs: Any) -> None:
            pass

    role.state.diar_factory = lambda identity, cancelled, **kwargs: SpyDiar(identity, cancelled)
    try:
        role.load()
        identity = role.identity()
        start.update(identity, dir=str(disk.root / identity["meeting_id"]))
        start.pop("req", None)
        start.pop("type", None)
        role.send("meeting.job.start", **start)
        role.credit(64)
        done = role.finish()
        assert done["type"] == "meeting.job.done", done
        assert {key: sent[0][key] for key in PAIR} == PAIR
    finally:
        role.close()
        jobs.jobs.shutdown()


def test_bridge_meta_contract(bound: tuple[MeetingBridge, SyntheticService]) -> None:
    bridge, _ = bound
    meta = bridge.metaObject()
    prop = meta.property(meta.indexOfProperty("diarizationInstall"))
    assert prop.notifySignal().methodSignature() == b"diarizationInstallChanged()"
    for slot in (
        b"requestDiarizationInstall()",
        b"cancelDiarizationInstall()",
        b"leaveMeetingsSection()",
        b"refreshDiarizationModels()",
    ):
        assert meta.indexOfSlot(slot) >= 0


def test_app_binds_service_catalog_without_rereading_and_provider_stays_fresh(
    qapp: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice import app
    from astra_voice.models import catalog_effective
    from astra_voice.models.store import ModelStore
    from astra_voice.ui.model_downloads import ModelService

    model = ModelService.__new__(ModelService)
    model._store = ModelStore(tmp_path / "models")
    model._downloader = Mock(_http=Mock(), _corp_base=None)
    model._catalog_error = None
    model._catalog = Catalog(serial=1, trust_epoch=0, entries=(), revoked=())
    selected: list[dict[str, str] | None] = [None]
    catalogs: list[Catalog] = []

    def trusted(
        store: ModelStore, *, catalog_provider: Callable[[], Catalog]
    ) -> dict[str, str] | None:
        assert store is model._store
        catalogs.append(catalog_provider())
        return selected[0]

    monkeypatch.setattr(diarization, "trusted_diarization_models", trusted)
    reread = Mock(side_effect=AssertionError("catalog reread"))
    monkeypatch.setattr(catalog_effective, "load_effective", reread)
    monkeypatch.setattr(diarization, "load_effective", reread)
    bridge = MeetingBridge(
        Settings(meeting_diarization=True), store=Mock(), diar_models_provider=lambda: None
    )
    try:
        app._bind_meeting_diarization(bridge, model)
        assert bridge._diar_models_provider() is None
        selected[0] = dict(PAIR)
        assert bridge._diar_models_provider() == PAIR
        assert catalogs and all(catalog is model.catalog for catalog in catalogs)
        reread.assert_not_called()
    finally:
        bridge.shutdown()


def test_bridge_reflects_already_revoked_pair_without_install(
    bound: tuple[MeetingBridge, SyntheticService],
) -> None:
    bridge, service = bound
    service.pair = dict(PAIR)
    bridge._diar_revoked = lambda: True
    bridge.refreshDiarizationModels()
    assert bridge.diarizationInstall["code"] == "broken"
    assert bridge.diarizationInstall["text"] == "Эта версия отозвана авторами каталога"
    assert not bridge.diarizationInstall["action"]
    bridge.requestDiarizationInstall()
    assert not service.calls


def test_leaving_real_window_section_resets_cancelled_only(
    bound: tuple[MeetingBridge, SyntheticService],
) -> None:
    from PyQt5.QtCore import QObject, pyqtSignal

    class Shell(QObject):
        currentSectionChanged = pyqtSignal()

    bridge, _ = bound
    window = Shell()
    window.setProperty("currentSection", {"key": "meetings"})
    bridge.bind_window(window)
    bridge._diar_event("finished", ("cancelled", "", "cancelled"))
    window.currentSectionChanged.emit()
    assert bridge.diarizationInstall["code"] == "cancelled"
    window.setProperty("currentSection", {"key": "general"})
    window.currentSectionChanged.emit()
    assert bridge.diarizationInstall["code"] == "missing"
    bridge._diar_event("stage", ("downloading",))
    window.currentSectionChanged.emit()
    assert bridge.diarizationInstall["code"] == "downloading"


def test_ui_changes_do_not_rehash_pair(bound: tuple[MeetingBridge, SyntheticService]) -> None:
    bridge, _ = bound
    provider = Mock(return_value=None)
    bridge._diar_models_provider = provider
    for _ in range(10):
        bridge.changed.emit()
    provider.assert_not_called()
    bridge.refreshDiarizationModels()
    provider.assert_called_once_with()


def test_accessible_progress_catches_last_sample_without_new_bytes(
    bound: tuple[MeetingBridge, SyntheticService],
) -> None:
    bridge, _ = bound
    now = [0.0]
    bridge._clock = lambda: now[0]
    bridge._diar_last_accessible = 0.0
    bridge._diar_event("progress", (0.42,))
    assert bridge.diarizationInstall["percent"] == 42
    assert bridge.diarizationInstall["accessiblePercent"] == 0
    now[0] = 1.0
    bridge._diar_progress_flush()
    assert bridge.diarizationInstall["accessiblePercent"] == 42
