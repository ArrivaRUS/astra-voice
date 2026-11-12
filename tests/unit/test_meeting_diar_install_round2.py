"""Review regressions: nonblocking trust reads, effective opt-in and late cancel."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from test_meeting_diar_install import PAIR, SyntheticService, spin
from test_tray import qapp as qapp

from astra_voice.core.meeting_session import MetadataWriter
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.models import diarization
from astra_voice.models.installer import InstallResult
from astra_voice.ui.meeting_bridge import MeetingBridge

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


class HeldCancellation(SyntheticService):
    def install(self, *, cancel: threading.Event, sink: Any) -> InstallResult:
        self.cancel = cancel
        self.calls += 1
        self.started.set()
        assert self.release.wait(3)
        sink("finished", ("cancelled", ""))
        return InstallResult("error", reason_code="cancelled")


@pytest.fixture
def delayed(qapp: object) -> Iterator[tuple[MeetingBridge, HeldCancellation]]:
    service = HeldCancellation()
    bridge = MeetingBridge(
        Settings(meeting_diarization=True), store=Mock(), diarization_service=service
    )
    yield bridge, service
    service.release.set()
    bridge.shutdown()


@pytest.mark.parametrize("via", ["setting", "policy"])
def test_effective_off_to_on_discovers_existing_pair(qapp: object, via: str) -> None:
    service = SyntheticService()
    provider = Mock(return_value=dict(PAIR))
    service.trusted_models = provider  # type: ignore[method-assign]
    bridge = MeetingBridge(
        Settings(meeting_diarization=False), store=Mock(), save=Mock(), diarization_service=service
    )
    try:
        provider.assert_not_called()
        if via == "setting":
            bridge.setSetting("meeting_diarization", True)
        else:
            bridge.refreshPolicy(Policy(raw={"meeting_diarization": "on"}))
        assert bridge.diarizationInstall["code"] == "installed"
        assert bridge.diarizationInstall["text"] == "Установлены · 34 МБ"
        assert bridge.diarizationInstall["action"] == ""
        provider.assert_called_once_with()
    finally:
        bridge.shutdown()


@pytest.mark.parametrize("leave", [False, True])
def test_late_cancel_result_remembers_section_departure(
    delayed: tuple[MeetingBridge, HeldCancellation],
    leave: bool,
) -> None:
    bridge, service = delayed
    bridge.requestDiarizationInstall()
    assert service.started.wait(1)
    bridge.cancelDiarizationInstall()
    if leave:
        bridge.leaveMeetingsSection()
    assert bridge.diarizationInstall["code"] == "downloading"
    service.release.set()
    expected = "missing" if leave else "cancelled"
    spin(lambda: bridge._diar_downloads._thread is None)
    assert bridge.diarizationInstall["code"] == expected
    assert bridge.diarizationInstall["text"] == (
        "Не установлены — без них все собеседники подписываются «Собеседники» · 34 МБ"
        if leave
        else "Установка отменена"
    )


@pytest.mark.parametrize("entrypoint", ["installed", "bound-service"])
def test_pair_mutation_does_not_block_metadata_controls(
    qapp: object,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    from astra_voice.meetings.diar_models import installed_diar_models

    entered, release = threading.Event(), threading.Event()

    def mutation() -> None:
        with diarization._PAIR_LOCK:
            entered.set()
            assert release.wait(3)

    def trusted(*args: Any, **kwargs: Any) -> dict[str, str]:
        with diarization._PAIR_LOCK:
            return dict(PAIR)

    monkeypatch.setattr(diarization, "trusted_diarization_models", trusted)
    service = SyntheticService()
    service.trusted_models = trusted  # type: ignore[method-assign]
    bridge = MeetingBridge(
        Settings(meeting_diarization=False), store=Mock(), diarization_service=service
    )
    provider = installed_diar_models if entrypoint == "installed" else bridge._diar_models_provider
    owner = threading.Thread(target=mutation)
    writer = MetadataWriter()
    observed: list[dict[str, str] | None] = []
    controls: list[str] = []
    ready = threading.Event()
    try:
        owner.start()
        assert entered.wait(1)
        writer.submit(lambda: observed.append(provider()))
        writer.submit(lambda: controls.append("cancelled=true"))

        def deleting() -> None:
            controls.append("DELETING")
            ready.set()

        writer.submit(deleting)
        assert ready.wait(0.3), "trust probe blocked cancellation/deletion behind installation"
        assert observed == [None]
        assert controls == ["cancelled=true", "DELETING"]
        release.set()
        owner.join(1)
        assert provider() == PAIR  # absence during mutation is not cached
    finally:
        release.set()
        owner.join(1)
        assert writer.close(timeout=1)
        bridge.shutdown()


@pytest.mark.xfail(strict=True, reason="R2.1: D4 trust boundary requires forbidden worker changes")
@pytest.mark.parametrize("replacement", [None, {**PAIR, "embedder_sha256": "c" * 64}])
def test_reconfirm_trust_after_asr_unload_before_diar_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: dict[str, str] | None,
) -> None:
    from test_meeting_jobs import MID
    from test_meeting_jobs import Rig as JobsRig
    from test_meeting_transcribe_diar import ScriptedDiar

    from astra_voice.meetings.diar_models import installed_diar_models
    from helpers.meeting_audio import Rig, bursts, make_disk, write_track

    selected: list[dict[str, str] | None] = [dict(PAIR)]
    monkeypatch.setattr(diarization, "trusted_diarization_models", lambda *a, **k: selected[0])
    jobs = JobsRig(tmp_path / "gui")
    jobs.jobs.diar_models = installed_diar_models
    jobs.create(variant="app_stream")
    jobs.jobs.on_recorded(MID)
    jobs.jobs.tick()
    jobs.loaded()
    request = jobs.accepted()
    disk = make_disk(tmp_path / "role")
    write_track(disk, "mic", bursts([0.6]))
    write_track(disk, "far", bursts([0.5]))
    role = Rig(disk)
    loaded: list[dict[str, str]] = []

    class CaptureDiar(ScriptedDiar):
        def load(self, models: dict[str, str]) -> None:
            loaded.append(dict(models))

    original = role.state._unload_ack

    def unload() -> dict[str, Any]:
        ack = original()
        selected[0] = replacement
        return ack

    monkeypatch.setattr(role.state, "_unload_ack", unload)
    role.state.diar_factory = lambda identity, cancelled, **kw: CaptureDiar(identity, cancelled)
    try:
        role.load()
        identity = role.identity()
        request.update(identity, dir=str(disk.root / identity["meeting_id"]))
        request.pop("type", None)
        request.pop("req", None)
        role.send("meeting.job.start", **request)
        role.credit(64)
        result = role.finish()
        assert loaded == ([] if replacement is None else [replacement])
        assert result["type"] == "meeting.job.done"
    finally:
        role.close()
        jobs.jobs.shutdown()
