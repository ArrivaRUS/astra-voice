"""E13.M7 step 5a: stable local names, GUI writes and BL-3 publication barrier."""

from __future__ import annotations

import dataclasses
import logging
import stat
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from PyQt5.QtTest import QSignalSpy
from test_meeting_card_bridge import SaveRig
from test_meeting_jobs_ui import MID
from test_tray import qapp as qapp

from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.meetings import speakers, templates
from astra_voice.meetings.diar_models import enabled_diar_models, installed_diar_models
from astra_voice.meetings.events import MeetingState, PhaseStatus
from astra_voice.meetings.model import (
    Meeting,
    ReconcileProgress,
    Speaker,
    SpeakerMap,
    Transcript,
    Utterance,
)
from astra_voice.meetings.policy import parse_policy
from astra_voice.meetings.store import MeetingStoreError, Writer
from astra_voice.ui import meeting_card

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def data() -> tuple[Meeting, Transcript, SpeakerMap]:
    meeting = Meeting(
        MID, started_at="2026-11-10T12:00:00Z", state=MeetingState.DONE, variant="app_stream"
    )
    meeting.transcription.diarization = True
    meeting.transcription.diar_status = "done"
    rows = [
        Utterance(
            f"u{i}",
            seq=i,
            speaker=who,
            track="mic" if who == "me" else "far",
            text=f"слово{i}",
            start_ms=i * 60_000,
            end_ms=(i + 1) * 60_000,
            final=True,
            multiple_speakers=i == 2,
        )
        for i, who in enumerate(("me", "S1", "S3", "S2"))
    ]
    transcript = Transcript(
        MID,
        utterances=rows,
        labels_final=True,
        reconcile=ReconcileProgress(status=PhaseStatus.DONE),
    )
    names = SpeakerMap(
        labels={
            "S1": Speaker(name="Ирина Соколова"),
            "S2": Speaker(),
            "S3": Speaker(merged_into="S1"),
        }
    )
    return meeting, transcript, names


def test_chain_and_no_renumbering() -> None:
    meeting, _, names = data()
    names.labels["S2"].merged_into = "S3"
    names.labels["S8"] = Speaker()
    assert speakers.canonical_speaker("S2", names) == "S1"
    assert speakers.speaker_label(meeting, "S2", names) == "Ирина Соколова"
    assert speakers.speaker_label(meeting, "S2", names, file=True) == "Ирина Соколова"
    assert speakers.speaker_label(meeting, "S8", names) == "Спикер 8"
    assert speakers.speaker_number("S2", names) == 1
    assert names.to_dict()["renumbered"] is False
    names.labels["S1"].merged_into = "S2"
    with pytest.raises(ValueError):
        speakers.canonical_speaker("S2", names)


@pytest.mark.parametrize(
    "name,expected", [(" Ирина ", "Ирина"), ("  ", None), ("я" * 60, "я" * 60)]
)
def test_name_trim_empty_and_sixty(name: str, expected: str | None) -> None:
    _, _, names = data()
    assert speakers.rename(names, "S2", name).labels["S2"].name == expected


@pytest.mark.parametrize("name", ["я" * 61, "Ира\n", "\tИра", "Ир\x00а", "Ир\u202eа", "Ир\u2028а"])
def test_invalid_name_is_rejected_without_echoing_input(name: str) -> None:
    with pytest.raises(ValueError) as error:
        speakers.validate_name(name)
    assert name not in str(error.value)


@pytest.mark.parametrize(
    "source,target",
    [("S1", "S1"), ("S3", "S2"), ("S2", "S3"), ("me", "S1"), ("S1", "me"), ("S8", "S1")],
)
def test_invalid_merges(source: str, target: str) -> None:
    _, _, names = data()
    before = names.to_dict()
    with pytest.raises(ValueError):
        speakers.merge(names, source, target)
    assert names.to_dict() == before


@pytest.mark.parametrize("template", templates.TEMPLATES)
@pytest.mark.parametrize("fmt", templates.FORMATS)
def test_all_templates_names_headers_canonical_merge_and_escaping(template: str, fmt: str) -> None:
    meeting, transcript, names = data()
    name = "<b>Ира</b> *[ссылка](https://x.test)* `код` | &"
    names.labels["S1"].name = name
    before = transcript.to_dict()
    text = templates.render(template, meeting, transcript, fmt, speakers=names)
    header = next(line for line in text.splitlines() if "Участники:" in line)
    assert "Спикер 3" not in text
    if template == "me_and_others":
        assert name not in text and "Собеседники" in header
    else:
        assert name in text and "Участники:" in header
    if fmt == "md" and template != "me_and_others":
        assert (
            templates._md_escape(name) in text
            or templates._md_escape("Спикер 1 (" + name + ")") in text
        )
    if template != "stats":
        assert "слово1 слово2" not in text  # §Д.5: multi never joins, even after merging names
        assert "слово1" in text and "слово2" in text
        assert ("(несколько говорящих)" in text) == (template == "stenogram")
    else:
        stats = templates.statistics(meeting, transcript.utterances, speakers=names)
        assert sum(row.share or 0 for row in stats.speakers) == 100
        assert [(row.minutes, row.replies) for row in stats.speakers if row.label == name] == [
            (2, 2)
        ]
    assert transcript.to_dict() == before


def test_panel_shared_minutes_room_live_and_candidate_numbers() -> None:
    meeting, transcript, names = data()
    minutes = templates.speaker_minutes(transcript.utterances, names)
    rows = meeting_card.participants(
        meeting,
        minutes,
        live=False,
        speakers=names,
        utterances=transcript.utterances,
        editable=True,
    )
    assert [row["id"] for row in rows] == ["me", "S1", "S2"]
    assert [row["minutes"] for row in rows] == ["1 мин", "2 мин", "1 мин"]
    assert rows[1]["label"] == "Ирина Соколова" and rows[1]["num"] == "1"
    assert rows[1]["firstText"] == "слово1" and rows[1]["firstStartMs"] == 60_000
    assert rows[2]["mergeCandidates"] == [{"id": "S1", "label": "Ирина Соколова", "num": "1"}]
    assert (
        templates.statistics(meeting, transcript.utterances, speakers=names).speakers[0].minutes
        == 2
    )
    meeting.variant = "mic_only"
    room_rows = [
        dataclasses.replace(row, speaker="S8") if row.speaker == "me" else row
        for row in transcript.utterances
    ]
    rows = meeting_card.participants(
        meeting, templates.speaker_minutes(room_rows), live=True, utterances=room_rows
    )
    assert all(row["kind"] == "speaker" and not row["canEdit"] for row in rows)
    assert [row["num"] for row in rows] == ["1", "2", "3", "8"]


@pytest.mark.parametrize("choice", [False, True])
@pytest.mark.parametrize("policy_value", [None, "off", "on"])
@pytest.mark.parametrize("installed", [False, True])
def test_diarization_setting_policy_provider_matrix(
    choice: bool, policy_value: str | None, installed: bool
) -> None:
    settings = Settings(meeting_diarization=choice)
    policy = parse_policy(
        Policy(raw={} if policy_value is None else {"meeting_diarization": policy_value})
    )
    pair = (
        {
            "segmentation_dir": "/trusted/seg",
            "embedder_dir": "/trusted/embed",
            "segmentation_sha256": "a" * 64,
            "embedder_sha256": "b" * 64,
        }
        if installed
        else None
    )
    provider = Mock(return_value=pair)
    effective = choice if policy_value is None else policy_value == "on"
    assert enabled_diar_models(settings, policy, provider) == (pair if effective else None)
    assert provider.call_count == int(effective)
    assert installed_diar_models() is None


@pytest.fixture
def rig(tmp_path: Path, qapp: object) -> Iterator[SaveRig]:
    instance = SaveRig(tmp_path)
    meeting, transcript, names = data()
    instance.store.create(meeting)
    instance.store.write(MID, "transcript.json", transcript, writer=Writer.TRANSCRIBE)
    instance.store.write(MID, "speakers.json", names, writer=Writer.GUI)
    instance.open_card()
    yield instance
    instance.bridge.shutdown()


def test_bridge_rename_merge_last_value_close_and_privacy(
    rig: SaveRig, caplog: pytest.LogCaptureFixture
) -> None:
    bridge = rig.bridge
    original = (rig.store.root / MID / "transcript.json").read_bytes()
    spy = QSignalSpy(bridge.speakerOperationFinished)
    assert bridge.card["namesEditable"]
    with caplog.at_level(logging.DEBUG):
        assert bridge.renameSpeaker("S2", "PRIVATE_NAME")
        assert bridge.cardRows.get(3)["who"] == "PRIVATE_NAME"
        assert bridge.renameSpeaker("S2", "LAST_PRIVATE_NAME")
        assert bridge.mergeSpeakers("S2", "S1")
        assert bridge.flushSpeakerWrites()
        assert bridge.speakerOperation == {
            "ok": True,
            "code": "merged",
            "text": "Спикер 2 объединён с «Ирина Соколова» · статистика пересчитана",
        }
        assert len(bridge.card["participants"]) == 2
        assert bridge.card["participants"][1]["minutes"] == "3 мин"
        assert bridge.cardRows.get(3)["who"] == "Ирина Соколова"
        assert bridge.renameSpeaker("S1", "LAST_PRIVATE_NAME")
        bridge.closeMeeting()
        bridge.shutdown()
    assert "PRIVATE_NAME" not in caplog.text
    assert rig.store.read(MID, "speakers.json").labels["S1"].name == "LAST_PRIVATE_NAME"
    assert (rig.store.root / MID / "transcript.json").read_bytes() == original
    assert stat.S_IMODE((rig.store.root / MID / "speakers.json").stat().st_mode) == 0o600
    assert [entry[0] for entry in spy] == ["renamed", "renamed", "merged", "renamed"]


@pytest.mark.parametrize(
    "reason",
    [
        "live",
        "not-final",
        "diar-pending",
        "d5",
        "inflight",
        "pending",
        "busy",
        "policy",
        "delete",
        "tombstone",
        "closed",
        "cancelled",
    ],
)
def test_python_barrier_refuses_without_disk_write(
    rig: SaveRig, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = rig.bridge
    meeting = bridge._card_meeting()
    assert meeting is not None and bridge._card_transcript is not None
    if reason == "live":
        meeting.state = MeetingState.TRANSCRIBING
    elif reason == "not-final":
        bridge._card_transcript.labels_final = False
    elif reason == "diar-pending":
        meeting.transcription.diar_status = "running"
    elif reason == "d5":
        bridge._card_transcript.reconcile.status = PhaseStatus.RUNNING
    elif reason == "inflight":
        bridge._card_transcript.reconcile.inflight_id = "u2"
    elif reason == "pending":
        bridge._card_tail = True
    elif reason == "busy":
        monkeypatch.setattr(rig.jobs, "names_busy", lambda mid: True)
    elif reason == "policy":
        bridge.refreshPolicy(Policy(raw={"meeting_record": "off"}))
    elif reason == "delete":
        bridge._delete_pending[MID] = meeting
    elif reason == "tombstone":
        rig.jobs.tombstones.add(MID)
    elif reason == "closed":
        bridge._closed = True
    elif reason == "cancelled":
        meeting.cancelled = True
    path = rig.store.root / MID / "speakers.json"
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    assert not bridge.renameSpeaker("S1", "PRIVATE_NAME")
    assert not bridge.mergeSpeakers("S2", "S1")
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    assert bridge.speakerOperation["code"] == "unavailable"


@pytest.mark.parametrize("writer", [Writer.DIAR, Writer.TRANSCRIBE])
def test_store_lease_proves_exit_before_edit(rig: SaveRig, writer: Writer) -> None:
    path = rig.store.root / MID / "speakers.json"
    before = path.read_bytes()
    with rig.store.writer(MID, writer):
        assert rig.bridge.renameSpeaker("S1", "PRIVATE_NAME")
        assert rig.bridge.flushSpeakerWrites()
    assert path.read_bytes() == before
    assert rig.bridge.speakerOperation["code"] == "save-error"


def test_invalid_input_bridge_and_disk_failure_hide_names(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    before = rig.store.read(MID, "speakers.json")
    assert not rig.bridge.renameSpeaker("S1", "я" * 61)
    assert not rig.bridge.mergeSpeakers("S3", "S1")
    assert not rig.bridge.mergeSpeakers("S1", "me")
    with caplog.at_level(logging.DEBUG):
        monkeypatch.setattr(rig.store, "write", Mock(side_effect=RuntimeError("PRIVATE_NAME")))
        assert rig.bridge.renameSpeaker("S1", "PRIVATE_NAME")
        assert rig.bridge.flushSpeakerWrites()
    assert rig.store.read(MID, "speakers.json") == before
    assert "PRIVATE_NAME" not in caplog.text + str(rig.bridge.speakerOperation)


def test_no_diarization_models_missing_failed_and_install_signal(rig: SaveRig) -> None:
    bridge = rig.bridge
    meeting = bridge._card_meeting()
    assert meeting is not None
    meeting.transcription.diarization = False
    bridge.changed.emit()
    assert bridge.card["speakerState"] == "no-diarization"
    assert bridge.card["diarizationModelsMissing"]
    assert bridge.card["diarizationHint"] == "Разделение по голосам: модели не установлены"
    spy = QSignalSpy(bridge.diarizationInstallRequested)
    bridge.requestDiarizationInstall()
    assert len(spy) == 1
    bridge.refreshPolicy(Policy(raw={"meeting_diarization": "off"}))
    assert not bridge.card["diarizationModelsMissing"]
    bridge.requestDiarizationInstall()
    assert len(spy) == 1
    meeting.transcription.diarization = True
    meeting.transcription.diar_status = "failed"
    bridge.changed.emit()
    assert bridge.card["diarizationFailed"]
    assert bridge.card["speakerState"] == "no-diarization"


def test_gui_reset_before_new_job_and_no_reset_by_role(rig: SaveRig) -> None:
    meeting = rig.meeting()
    # Force a restart's durable transcribing state, as admission does.
    meeting.state = MeetingState.TRANSCRIBING
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    with rig.store.writer(MID, Writer.TRANSCRIBE) as writer:
        with pytest.raises(MeetingStoreError):
            writer.reset_speakers(meeting.meeting_generation)
    with rig.store.writer(MID, Writer.GUI) as writer:
        writer.reset_speakers(meeting.meeting_generation)
    assert rig.store.read(MID, "speakers.json").labels == {}
    assert stat.S_IMODE((rig.store.root / MID / "speakers.json").stat().st_mode) == 0o600


def test_new_diar_admission_resets_names_before_start(tmp_path: Path) -> None:
    from test_meeting_jobs import MID as JOB_MID
    from test_meeting_jobs import Rig

    rig = Rig(tmp_path)
    try:
        rig.create()
        path = rig.store.root / JOB_MID / "speakers.json"
        path.write_text(
            '{"schema":1,"labels":{"S1":{"name":"PRIVATE_NAME","merged_into":null}},"renumbered":false}'
        )
        path.chmod(0o600)
        rig.jobs.diar_models = lambda: {
            "segmentation_dir": "/trusted/seg",
            "embedder_dir": "/trusted/embed",
            "segmentation_sha256": "a" * 64,
            "embedder_sha256": "b" * 64,
        }
        rig.jobs.transcribe(JOB_MID)
        rig.running()
        assert rig.store.read(JOB_MID, "speakers.json").labels == {}
        assert rig.role.last("meeting.job.start")["diarization"] is True
    finally:
        rig.jobs.shutdown()


def test_empty_name_reverts_immediately_reopen_and_single_merge_target(rig: SaveRig) -> None:
    assert rig.bridge.renameSpeaker("S1", "   ")
    assert rig.bridge.cardRows.get(1)["who"] == "Спикер 1"
    assert rig.bridge.cardRows.get(2)["who"] == "Спикер 1"
    assert rig.bridge.flushSpeakerWrites()
    assert rig.store.read(MID, "speakers.json").labels["S1"].name is None
    assert rig.bridge.mergeSpeakers("S2", "S1")
    assert not rig.bridge.card["participants"][1]["canMerge"]
    rig.bridge.closeMeeting()
    card = rig.open_card()
    assert card["participants"][1]["label"] == "Спикер 1"
    assert len(card["participants"]) == 2


def test_ready_live_and_policy_off_keep_marks_and_plain_text(rig: SaveRig) -> None:
    original = rig.bridge._card_transcript
    assert original is not None
    marks = [(u.text, u.multiple_speakers, u.echo) for u in original.utterances]
    assert rig.bridge.renameSpeaker("S1", "<b>Ира</b> *текст*")
    assert rig.bridge.cardRows.get(1)["who"] == "<b>Ира</b> *текст*"
    assert rig.bridge.mergeSpeakers("S2", "S1")
    rig.bridge.refreshPolicy(Policy(raw={"meeting_record": "off"}))
    assert rig.bridge.card["speakerState"] == "policy-off"
    assert not rig.bridge.card["namesEditable"]
    assert rig.bridge.card["participants"][1]["label"] == "<b>Ира</b> *текст*"
    assert [(u.text, u.multiple_speakers, u.echo) for u in original.utterances] == marks
    meeting = rig.bridge._card_meeting()
    assert meeting is not None
    meeting.state = MeetingState.TRANSCRIBING
    rig.bridge._card_builder.speakers = None  # type: ignore[union-attr]
    rig.bridge._card_builder.load(list(original.utterances), complete=False)  # type: ignore[union-attr]
    rig.bridge.changed.emit()
    assert rig.bridge.card["speakerState"] == "transcribing"
    assert "номера спикеров" in rig.bridge.card["namesHint"]
    assert all(not row["canEdit"] for row in rig.bridge.card["participants"])
    assert rig.bridge.cardRows.get(1)["who"] == "Спикер 1"


def test_no_diarization_and_room_fallbacks() -> None:
    meeting, _, _ = data()
    meeting.transcription.diarization = False
    rows = meeting_card.participants(meeting, {"me": 1, "others": 3}, live=False)
    assert [row["label"] for row in rows] == ["Я", "Собеседники"]
    assert [row["minutes"] for row in rows] == ["1 мин", "3 мин"]
    meeting.variant = "mic_only"
    rows = meeting_card.participants(meeting, {"others": 4}, live=False)
    assert [row["label"] for row in rows] == ["Участники"]
    assert rows[0]["minutes"] == "4 мин"


def test_merge_rounds_after_summing_and_complete_d5_fallback_allows_names(rig: SaveRig) -> None:
    rows = [
        Utterance("a", speaker="S1", text="а", end_ms=20_000),
        Utterance("b", speaker="S3", text="б", end_ms=20_000),
    ]
    _, _, names = data()
    assert templates.speaker_minutes(rows, names) == {"S1": 1}
    transcript = rig.bridge._card_transcript
    assert transcript is not None
    transcript.reconcile.failed = True  # completed fallback, not an active D5 process
    assert rig.bridge.renameSpeaker("S1", "Имя")


@pytest.mark.parametrize("fmt", templates.FORMATS)
@pytest.mark.parametrize("template", templates.TEMPLATES)
def test_bridge_export_and_preview_use_current_names(rig: SaveRig, fmt: str, template: str) -> None:
    from PyQt5.QtTest import QTest

    assert rig.bridge.renameSpeaker("S1", "Ирина Соколова")
    rig.bridge.setProperty("saveTemplate", template)
    preview = rig.bridge.savePreview()
    if template == "stats":
        assert preview["rows"][0]["who"] == "Ирина Соколова"
    elif template == "me_and_others":
        assert preview["rows"][1]["who"] == "Собеседники"
    elif template != "text_only":
        assert preview["rows"][1]["who"] == "Ирина Соколова"
    rig.answer = str(rig.documents / ("имена." + fmt))
    spy = QSignalSpy(rig.bridge.cardSaveFinished)
    assert rig.bridge.saveAs(fmt) == "pending"
    for _ in range(100):
        if spy:
            break
        QTest.qWait(10)
    assert spy and spy[0][0] == "saved"
    text = Path(rig.answer).read_text()
    if template == "me_and_others":
        assert "Ирина Соколова" not in text and "Собеседники" in text
    else:
        assert "Ирина Соколова" in text and "Спикер 3" not in text
    assert "Участники:" in text


def test_invalid_policy_closes_only_diarization() -> None:
    provider = Mock(
        return_value={
            "segmentation_dir": "/trusted/seg",
            "embedder_dir": "/trusted/embed",
            "segmentation_sha256": "a" * 64,
            "embedder_sha256": "b" * 64,
        }
    )
    policy = parse_policy(Policy(raw={"meeting_diarization": "INVALID"}))
    assert enabled_diar_models(Settings(meeting_diarization=True), policy, provider) is None
    provider.assert_not_called()


def test_reset_wrong_generation_and_live_lease_leave_names(rig: SaveRig) -> None:
    meeting = rig.meeting()
    meeting.state = MeetingState.TRANSCRIBING
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    path = rig.store.root / MID / "speakers.json"
    before = path.read_bytes()
    with rig.store.writer(MID, Writer.GUI) as writer:
        with pytest.raises(MeetingStoreError):
            writer.reset_speakers(meeting.meeting_generation + 1)
    with rig.store.writer(MID, Writer.DIAR), rig.store.writer(MID, Writer.GUI) as writer:
        with pytest.raises(MeetingStoreError):
            writer.reset_speakers(meeting.meeting_generation)
    assert path.read_bytes() == before


def test_bridge_meta_contract_and_model_provider_wire(
    tmp_path: Path, rig: SaveRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice import app
    from astra_voice.core import meeting_jobs
    from astra_voice.meetings import diar_models
    from astra_voice.platform.session import SessionKind
    from astra_voice.ui import (
        meeting_notice,
        meeting_notifications,
        meeting_offer_window,
        meeting_overlay,
    )
    from astra_voice.ui import meeting_recording as recording_module

    meta = rig.bridge.metaObject()
    for signature in (
        b"renameSpeaker(QString,QString)",
        b"mergeSpeakers(QString,QString)",
        b"requestDiarizationInstall()",
    ):
        assert meta.indexOfSlot(signature) >= 0
    for signature in (b"speakerOperationFinished(QString)", b"diarizationInstallRequested()"):
        assert meta.indexOfSignal(signature) >= 0
    assert meta.property(meta.indexOfProperty("speakerTexts")).isConstant()
    assert (
        meta.property(meta.indexOfProperty("speakerOperation")).notifySignal().methodSignature()
        == b"speakerOperationChanged()"
    )
    recording = Mock()
    settings = Settings(meeting_diarization=True)
    policy = parse_policy(Policy())
    probe = Mock(
        return_value={
            "segmentation_dir": "/trusted/seg",
            "embedder_dir": "/trusted/embed",
            "segmentation_sha256": "a" * 64,
            "embedder_sha256": "b" * 64,
        }
    )
    jobs_factory = Mock()
    bridge = Mock(_store=rig.store, _stored=settings, _parsed=policy)
    with monkeypatch.context() as patch:
        for module, name in (
            (meeting_overlay, "MeetingOverlay"),
            (meeting_notice, "MeetingNotice"),
            (meeting_offer_window, "MeetingOffer"),
            (meeting_notifications, "MeetingNotifications"),
        ):
            patch.setattr(module, name, Mock())
        patch.setattr(recording_module, "MeetingRecording", Mock(return_value=recording))
        patch.setattr(diar_models, "enabled_diar_models", probe)
        patch.setattr(meeting_jobs, "MeetingJobs", jobs_factory)
        app._bind_meeting_recording(bridge, Mock(device=""), SessionKind.KDE, None)
        resolved = jobs_factory.call_args.kwargs["diar_models"]()
    assert resolved == {
        "segmentation_dir": "/trusted/seg",
        "embedder_dir": "/trusted/embed",
        "segmentation_sha256": "a" * 64,
        "embedder_sha256": "b" * 64,
    }
    probe.assert_called_once_with(settings, policy, bridge._diar_models_provider)
