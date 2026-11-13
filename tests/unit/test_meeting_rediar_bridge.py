"""5в-6: coherent published pairs, delayed gestures and the QML redo vocabulary."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtTest import QSignalSpy
from test_meeting_card_bridge import SaveRig, save_result
from test_meeting_jobs_ui import MID
from test_meeting_speakers import data
from test_tray import qapp as qapp

from astra_voice.meetings import speakers, templates
from astra_voice.meetings.events import MeetingState, ProcessingStatus
from astra_voice.meetings.model import Meeting, SpeakerMap
from astra_voice.meetings.store import Writer
from astra_voice.ui import meeting_card
from astra_voice.ui.meeting_texts import REDO_TEXTS, redo_presentation

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]
PAIR = {"segmentation_dir": "/trusted/seg", "embedder_dir": "/trusted/emb"}


@pytest.fixture
def rig(tmp_path: Path, qapp: object) -> Iterator[SaveRig]:
    rig = SaveRig(tmp_path)
    meeting, text, names = data()
    meeting.processing_status = ProcessingStatus.SUCCEEDED
    meeting.audio.retain_days = 7
    meeting.transcription.finished_at = "2026-11-12T12:00:00Z"
    rig.store.create(meeting)
    rig.store.write(MID, "transcript.json", text, writer=Writer.TRANSCRIBE)
    rig.store.write(MID, "speakers.json", names, writer=Writer.GUI)
    rig.bridge._diar_models_provider = lambda: PAIR
    rig.jobs.diar_models = lambda: PAIR
    rig.open_card()
    yield rig
    rig.bridge.shutdown()


def sync(rig: SaveRig) -> None:
    rig.jobs.meetings[MID] = rig.meeting()
    rig.bridge._overlay_capture()
    rig.bridge._sync_card()
    rig.bridge.changed.emit()


def read_pair(rig: SaveRig) -> None:
    rig.bridge._load_transcript(rig.meeting())
    rig.tasks.pop(0)()
    rig.bridge._card_poll()


def enqueue(rig: SaveRig) -> Meeting:
    current = rig.meeting()
    result = rig.store.rediar_enqueue(MID, current.meeting_generation)
    assert result is not None
    sync(rig)
    return result


def publish(rig: SaveRig, *, finish: bool, bind_old: bool = True) -> None:
    current = rig.meeting()
    old = rig.store.read(MID, "transcript.json")
    if bind_old:
        names = rig.store.read(MID, "speakers.json")
        names.transcript_generation = old.meeting_generation
        raw_put(rig, "speakers.json", names)
    new = deepcopy(old)
    new.meeting_generation = current.meeting_generation
    new.transcript_revision += 1
    for row in new.utterances:
        row.text = "NEW_" + row.text
    # Exact K3 seam: public path changes, old names still present until K4.
    raw_put(rig, "transcript.json", new)
    if finish:
        raw_put(rig, "speakers.json", SpeakerMap(transcript_generation=new.meeting_generation))
        current.rediar = None
        current.transcript_generation = None
        rig.store.write(MID, "meeting.json", current, writer=Writer.GUI)


def raw_put(rig: SaveRig, name: str, value: Any) -> None:
    (rig.store.root / MID / name).write_text(json.dumps(value.to_dict()), encoding="utf-8")


@pytest.mark.parametrize("template", templates.TEMPLATES)
@pytest.mark.parametrize("fmt", ["txt", "md"])
def test_enqueue_and_cancel_preserve_all_exports_and_names(
    rig: SaveRig, template: str, fmt: str
) -> None:
    bridge = rig.bridge
    bridge.setProperty("saveTemplate", template)
    before = bridge.savePreviewText(fmt)
    preview = bridge.savePreview()
    resets = QSignalSpy(bridge._card_rows.modelReset)
    assert bridge.rediarizeMeeting(MID, 0, True)
    rig.jobs.tick()
    sync(rig)
    assert bridge.savePreviewText(fmt) == before
    assert bridge.savePreview() == preview
    assert not bridge.card["namesEditable"] and not resets
    assert bridge.cardRows.get(1)["who"] == "Ирина Соколова"
    assert not bridge.renameSpeaker("S1", "SHOULD_NOT_APPEAR")
    assert bridge.cancelDiarizationRedo(MID, 0)
    rig.jobs.tick()
    sync(rig)
    assert bridge.diarizationRedo["code"] == "cancelled"
    assert bridge.savePreviewText(fmt) == before and not resets
    bridge.closeMeeting()
    rig.open_card()
    assert bridge.diarizationRedo["code"] == ""
    assert bridge.savePreviewText(fmt)  # default template is restored on open


@pytest.mark.parametrize("bound", [False, True])
def test_i3_legacy_and_bound_map_visible_when_first_redo_opened(rig: SaveRig, bound: bool) -> None:
    if bound:
        names = rig.store.read(MID, "speakers.json")
        names.transcript_generation = 0
        rig.store.write(MID, "speakers.json", names, writer=Writer.GUI)
    enqueue(rig)
    rig.open_card()
    assert rig.bridge.cardRows.get(1)["who"] == "Ирина Соколова"
    assert rig.bridge._speakers.transcript_generation == 0
    assert not rig.bridge.card["namesEditable"]
    assert "Ирина Соколова" in rig.bridge.savePreviewText("txt")


@pytest.mark.parametrize("bound", [False, True])
def test_k3_before_k4_never_uses_old_names(rig: SaveRig, bound: bool) -> None:
    enqueue(rig)
    publish(rig, finish=False, bind_old=bound)
    rig.open_card()
    assert rig.bridge.cardRows.get(1)["who"] == "Спикер 1"
    assert "Ирина Соколова" not in rig.bridge.savePreviewText("txt")
    assert "NEW_" in rig.bridge.savePreviewText("txt")
    assert rig.bridge._card_transcript is not None
    assert rig.bridge._card_transcript.meeting_generation == 1


@pytest.mark.parametrize("template", templates.TEMPLATES)
@pytest.mark.parametrize("fmt", ["txt", "md"])
def test_t1_barrier_old_text_then_k3_k6_is_one_complete_pair(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch, template: str, fmt: str
) -> None:
    bridge = rig.bridge
    bridge.setProperty("saveTemplate", template)
    before = bridge.savePreviewText(fmt)
    before_preview = bridge.savePreview()
    enqueue(rig)
    entered, release = threading.Event(), threading.Event()
    original = rig.store.read
    stopped = False

    def read(mid: str, filename: str) -> Any:
        nonlocal stopped
        value = original(mid, filename)
        if filename == "transcript.json" and not stopped:
            stopped = True
            entered.set()
            assert release.wait(2)
        return value

    monkeypatch.setattr(rig.store, "read", read)
    bridge._load_transcript(rig.meeting())
    worker = threading.Thread(target=rig.tasks.pop(0))
    worker.start()
    try:
        assert entered.wait(2)
        publish(rig, finish=True)
        assert bridge.savePreviewText(fmt) == before
        assert bridge.savePreview() == before_preview
        rig.answer = str(rig.documents / f"before.{fmt}")
        assert save_result(rig, lambda: bridge.saveAs(fmt)) == "saved"
        assert Path(rig.answer).read_text(encoding="utf-8") == before
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    sync(rig)
    while rig.tasks:
        rig.tasks.pop(0)()
    bridge._card_poll()
    after = bridge.savePreviewText(fmt)
    assert "Ирина Соколова" not in after
    assert bridge._card_transcript is not None
    assert bridge._card_transcript.meeting_generation == 1
    assert bridge._speakers.transcript_generation == 1
    assert bridge.savePreview()  # all consumers use the newly completed pair
    rig.answer = str(rig.documents / f"after.{fmt}")
    assert save_result(rig, lambda: bridge.saveAs(fmt)) == "saved"
    assert Path(rig.answer).read_text(encoding="utf-8") == after
    resets = QSignalSpy(bridge._card_rows.modelReset)
    sync(rig)
    assert not resets and not rig.tasks  # K6 metadata does not publish twice


def test_mismatch_retries_keep_entire_previous_pair(rig: SaveRig) -> None:
    before = rig.bridge.savePreviewText("txt")
    names = rig.store.read(MID, "speakers.json")
    names.transcript_generation = 99
    raw_put(rig, "speakers.json", names)
    read_pair(rig)
    assert rig.bridge.savePreviewText("txt") == before
    assert rig.bridge.cardRows.get(1)["who"] == "Ирина Соколова"
    assert rig.bridge.cardSaveError == REDO_TEXTS["readError"]


def test_optimistic_write_at_enqueue_rolls_back(rig: SaveRig) -> None:
    assert rig.bridge.renameSpeaker("S1", "OPTIMISTIC_PRIVATE")
    assert rig.bridge.cardRows.get(1)["who"] == "OPTIMISTIC_PRIVATE"
    enqueue(rig)
    assert rig.bridge.flushSpeakerWrites()
    assert rig.bridge.cardRows.get(1)["who"] == "Ирина Соколова"
    assert rig.bridge.speakerOperation["ok"] is False
    assert "OPTIMISTIC_PRIVATE" not in rig.bridge.savePreviewText("txt")
    assert rig.store.read(MID, "speakers.json").labels["S1"].name == "Ирина Соколова"


@pytest.mark.parametrize("change", ["card", "other-card", "generation", "queued", "running"])
def test_delayed_gestures_recheck_target_and_state(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    expected = rig.bridge.diarizationRedo["expectedGeneration"]
    if change == "card":
        rig.bridge.closeMeeting()
    elif change == "other-card":
        other_id = "2026-11-01_130000-ab12"
        rig.create(other_id, state=MeetingState.DONE)
        other_text = deepcopy(rig.store.read(MID, "transcript.json"))
        other_text.meeting_id = other_id
        rig.store.write(other_id, "transcript.json", other_text, writer=Writer.TRANSCRIBE)
        rig.open_card(other_id)
    elif change == "generation":
        enqueue(rig)
        publish(rig, finish=True)
    else:
        enqueue(rig)
        monkeypatch.setattr(
            rig.jobs,
            "redo_snapshot",
            lambda _: {
                "busy": True,
                "status": change,
                "stage": "diar",
                "percent": 35,
            },
        )
    start, keep = Mock(), Mock()
    monkeypatch.setattr(rig.jobs, "rediarize", start)
    monkeypatch.setattr(rig.jobs, "keep_as_is", keep)
    assert not rig.bridge.rediarizeMeeting(MID, expected, True)
    assert not rig.bridge.keepDiarizationAsIs(MID, expected)
    assert not start.called and not keep.called


def test_warning_requires_explicit_confirmation(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert rig.bridge.diarizationRedo["needsWarning"]
    assert "Ирина Соколова" in rig.bridge.diarizationRedo["warningText"]
    start = Mock()
    monkeypatch.setattr(rig.jobs, "rediarize", start)
    assert not rig.bridge.rediarizeMeeting(MID, 0, False)
    assert rig.bridge.rediarizeMeeting(MID, 0, True)
    start.assert_called_once_with(MID)


@pytest.mark.parametrize("outcome", ["failed-again", "redo-failed", "cancelled", "done"])
def test_ephemeral_outcomes_close_with_card(rig: SaveRig, outcome: str) -> None:
    rig.jobs.redo_results[MID] = outcome
    assert rig.bridge.diarizationRedo["code"] == outcome
    before = rig.bridge.savePreviewText("txt")
    rig.bridge.closeMeeting()
    assert MID not in rig.jobs.redo_results
    rig.open_card()
    assert rig.bridge.diarizationRedo["code"] == ""
    assert rig.bridge.savePreviewText("txt") == before


@pytest.mark.parametrize(
    "code,needle",
    [
        (
            "failed",
            "не удалось — собеседники подписаны «Собеседники». "
            "Звук сохранён, можно попробовать ещё раз",
        ),
        ("queued", "начнётся после текущей расшифровки"),
        ("wait-memory", "подождёт — сейчас мало свободной памяти"),
        ("running", "разделяю заново · 35 %"),
        ("reconcile", "уточняю реплики · 35 %"),
        ("cancelled", "отменено — собеседники подписаны «Собеседники»"),
        (
            "failed-again",
            "снова не удалось — собеседники подписаны «Собеседники». "
            "Звук сохранён, можно повторить позже",
        ),
        (
            "no-audio",
            "не удалось — собеседники подписаны «Собеседники». "
            "Записи звука нет, разделить заново нельзя",
        ),
        ("done", "Собеседники разделены по голосам — можно вписать имена участников"),
        ("redo-failed", "разделить заново не удалось — оставлены прежнее разделение и имена"),
        ("offer", "эту встречу можно разделить, пока хранится звук (до 19.11)"),
        ("kept-7", "оставлено как есть. Звук хранится до 19.11 — до этого встречу можно разделить"),
        (
            "kept-30",
            "оставлено как есть. Звук хранится до 19.11 — до этого встречу можно разделить",
        ),
        ("kept-deleted", "оставлено как есть. Звук удалён по настройке хранения"),
        ("recovery_error", "не удалось завершить — программа повторит попытку сама"),
    ],
)
def test_literal_texts(code: str, needle: str) -> None:
    state = redo_presentation(code, 35, date="19.11")
    expected = needle if code == "done" else "Разделение по голосам: " + needle
    assert state["text"] == expected
    assert state["showProgress"] == (code in {"running", "reconcile"})


def test_room_first_and_just_installed_texts() -> None:
    assert "все реплики подписаны «Участники»" in str(
        redo_presentation("failed", 0, room=True)["text"]
    )
    assert (
        redo_presentation("running", 35, first=True)["text"]
        == "Разделение по голосам: разделяю · 35 %"
    )
    assert (
        redo_presentation("offer", 0, installed=True)["text"]
        == "Модели установлены — эту встречу тоже можно разделить по голосам"
    )


@pytest.mark.parametrize(
    "status,stage,code,percent",
    [
        ("queued", "", "queued", 0),
        ("waiting_memory", "", "wait-memory", 0),
        ("running", "diar", "running", 35),
        ("running", "reconcile", "reconcile", 92),
        ("committing", "reconcile", "reconcile", 100),
        ("recovery_error", "", "recovery_error", 0),
    ],
)
def test_backend_codes_badge_resume_and_progress(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch, status: str, stage: str, code: str, percent: int
) -> None:
    enqueue(rig)
    monkeypatch.setattr(
        rig.jobs,
        "redo_snapshot",
        lambda _: {
            "busy": True,
            "status": status,
            "stage": stage,
            "percent": percent,
        },
    )
    state = rig.bridge.diarizationRedo
    assert state["code"] == code and state["percent"] == percent
    assert state["stage"] == stage  # restart midway through D5 keeps actual 92%, not 0
    if code == "recovery_error":
        assert state["action"] == "" and not state["canKeep"]
    rig.bridge.changed.emit()
    assert rig.bridge.card["badge"]["text"] == f"Разделение по голосам · {percent} %"
    assert rig.bridge.meetings[0]["badge"] == f"Разделение по голосам · {percent} %"
    assert not state["namesEnabled"] and state["saveEnabled"]
    assert state["renameEnabled"] and state["deleteEnabled"]
    assert "до конца разделения" in rig.bridge.card["subtitle"]["suffix"]
    monkeypatch.setattr(
        rig.jobs,
        "redo_snapshot",
        lambda _: {
            "busy": True,
            "status": status,
            "stage": stage,
            "percent": 0,
        },
    )
    assert rig.bridge.diarizationRedo["percent"] == percent


@pytest.mark.parametrize(
    "mode,storage",
    [
        ("deleted", "звук удалён"),
        ("retained", "звук хранится до 19.11"),
        ("failed", "звук хранится до разделения"),
        ("active", "звук хранится до конца разделения"),
        ("pending", "звук удаляется по настройке хранения"),
    ],
)
def test_disk_storage_on_both_surfaces(rig: SaveRig, mode: str, storage: str) -> None:
    meeting = rig.meeting()
    if mode == "deleted":
        meeting.audio.present = False
    elif mode == "failed":
        meeting.transcription.diar_status = "failed"
    elif mode == "active":
        enqueue(rig)
        meeting = rig.meeting()
    elif mode == "pending":
        meeting.audio.retain_days = 0
        meeting.cleanup_pending = True
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    sync(rig)
    assert meeting_card.audio_storage(meeting) == storage
    assert rig.bridge.card["subtitle"]["suffix"].endswith(storage) if storage else True
    assert meeting_card.audio_storage(meeting, short=True) in rig.bridge.meetings[0]["subtitle"]
    if mode == "pending":
        assert "звук удалён" not in rig.bridge.card["subtitle"]["suffix"]


@pytest.mark.parametrize("bound", [False, True])
def test_speaker_map_preserves_generation(rig: SaveRig, bound: bool) -> None:
    text = rig.store.read(MID, "transcript.json")
    names = rig.store.read(MID, "speakers.json")
    names.transcript_generation = 42 if bound else None
    assert (
        speakers.speaker_map(text.utterances, names).transcript_generation
        == names.transcript_generation
    )
    assert (
        speakers.validated_map(names.labels, names).transcript_generation
        == names.transcript_generation
    )


@pytest.mark.parametrize(
    "mode,code",
    [
        ("failed", "failed"),
        ("room", "failed"),
        ("no-audio", "no-audio"),
        ("offer", "offer"),
        ("installed", "offer"),
        ("partial-d5", ""),
    ],
)
def test_disk_drives_row_codes(rig: SaveRig, mode: str, code: str) -> None:
    meeting = rig.meeting()
    if mode in {"failed", "room", "no-audio"}:
        meeting.transcription.diar_status = "failed"
        meeting.processing_status = ProcessingStatus.PARTIAL
        if mode == "room":
            meeting.variant = "mic_only"
        if mode == "no-audio":
            meeting.audio.present = False
    elif mode == "partial-d5":
        meeting.processing_status = ProcessingStatus.PARTIAL
    else:
        meeting.transcription.diarization = False
        meeting.transcription.diar_status = "skipped"
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    if mode == "installed":
        rig.bridge._diar_card_installed = MID
    sync(rig)
    state = rig.bridge.diarizationRedo
    assert state["code"] == code
    if mode == "room":
        assert "все реплики подписаны «Участники»" in state["text"]
    if mode == "installed":
        assert state["text"] == "Модели установлены — эту встречу тоже можно разделить по голосам"
    if mode == "no-audio":
        assert not state["canStart"] and not state["canKeep"]


@pytest.mark.parametrize("days,code", [(0, "kept-deleted"), (7, "kept-7"), (30, "kept-30")])
def test_keep_actual_backend_storage_and_no_extension(rig: SaveRig, days: int, code: str) -> None:
    meeting = rig.meeting()
    meeting.transcription.diar_status = "failed"
    meeting.processing_status = ProcessingStatus.PARTIAL
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    rig.jobs.audio_retention_days = lambda: days
    sync(rig)
    assert rig.bridge.keepDiarizationAsIs(MID, 0)
    rig.jobs.tick()
    sync(rig)
    state = rig.bridge.diarizationRedo
    assert state["code"] == code
    assert state["audioPresent"] == (days != 0)
    assert rig.meeting().processing_status == ProcessingStatus.SUCCEEDED
    before = (rig.store.root / MID / "meeting.json").read_bytes()
    assert not rig.bridge.keepDiarizationAsIs(MID, 0)
    assert (rig.store.root / MID / "meeting.json").read_bytes() == before
    rig.bridge.closeMeeting()
    rig.open_card()
    assert rig.bridge.diarizationRedo["code"] == ("offer" if days else "")


def test_offer_failed_keep_only_closes_row(rig: SaveRig) -> None:
    meeting = rig.meeting()
    meeting.transcription.diarization = False
    meeting.transcription.diar_status = "skipped"
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    rig.jobs.redo_results[MID] = "failed-again"
    sync(rig)
    before = (rig.store.root / MID / "meeting.json").read_bytes()
    assert rig.bridge.keepDiarizationAsIs(MID, 0)
    assert rig.bridge.diarizationRedo["code"] == "offer"
    assert (rig.store.root / MID / "meeting.json").read_bytes() == before


@pytest.mark.parametrize("failed", [False, True])
def test_real_redo_failure_keeps_published_result(rig: SaveRig, failed: bool) -> None:
    if failed:
        meeting = rig.meeting()
        meeting.processing_status = ProcessingStatus.PARTIAL
        meeting.transcription.diar_status = "failed"
        rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
        sync(rig)
    before = rig.bridge.savePreviewText("txt")
    assert rig.bridge.rediarizeMeeting(MID, 0, True)
    rig.start_running()
    active = rig.jobs._active
    assert active is not None
    rig.role.emit(
        "meeting.job.failed", meeting_id=MID, meeting_generation=active.generation, code="io"
    )
    rig.recording.tick()
    rig.role.exited = True
    rig.jobs.tick()
    sync(rig)
    assert rig.bridge.diarizationRedo["code"] == ("failed-again" if failed else "redo-failed")
    assert rig.bridge.savePreviewText("txt") == before


def test_recovery_error_is_visible_when_no_text_can_be_read(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue(rig)
    (rig.store.root / MID / "transcript.json").write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(
        rig.jobs,
        "redo_snapshot",
        lambda _: {
            "busy": True,
            "status": "recovery_error",
            "recoveryError": "io",
        },
    )
    rig.open_card()
    assert rig.bridge.card["view"] == "read-error"
    assert rig.bridge.card["diarizationRedo"]["code"] == "recovery_error"
    assert "invalid" not in rig.bridge.card["diarizationRedo"]["text"]
    assert rig.bridge.savePreviewText("txt") == ""


@pytest.mark.parametrize("blocked", ["off", "models", "audio", "cleanup", "policy", "busy"])
def test_offer_and_direct_slot_recheck_admission(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch, blocked: str
) -> None:
    meeting = rig.meeting()
    meeting.transcription.diarization = False
    meeting.transcription.diar_status = "skipped"
    if blocked == "off":
        rig.settings.meeting_diarization = False
    elif blocked == "models":
        rig.bridge._diar_models_provider = lambda: None
        rig.bridge.refreshDiarizationModels()
    elif blocked == "audio":
        meeting.audio.present = False
    elif blocked == "cleanup":
        meeting.cleanup_pending = True
    elif blocked == "policy":
        from astra_voice.core.policy import Policy
        from astra_voice.meetings.policy import parse_policy

        rig.bridge._policy = Policy(raw={"meeting_diarization": "off"})
        rig.bridge._parsed = parse_policy(rig.bridge._policy)
    elif blocked == "busy":
        monkeypatch.setattr(rig.jobs, "redo_snapshot", lambda _: {"busy": True, "status": "queued"})
        monkeypatch.setattr(rig.jobs, "names_busy", lambda _: True)
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    sync(rig)
    start = Mock()
    monkeypatch.setattr(rig.jobs, "rediarize", start)
    assert rig.bridge.diarizationRedo["code"] != "offer"
    assert not rig.bridge.divideMeeting(MID, 0)
    assert not start.called


def test_qml_redo_metaobject_contract(rig: SaveRig) -> None:
    meta = rig.bridge.metaObject()
    for signature in (
        b"rediarizeMeeting(QString,int,bool)",
        b"divideMeeting(QString,int)",
        b"cancelDiarizationRedo(QString,int)",
        b"keepDiarizationAsIs(QString,int)",
    ):
        assert meta.indexOfMethod(signature) >= 0
    prop = meta.property(meta.indexOfProperty("diarizationRedo"))
    assert prop.hasNotifySignal()
    assert bytes(prop.notifySignal().name()) == b"cardChanged"


def test_cancel_and_keep_after_previous_cancel_use_published_generation(rig: SaveRig) -> None:
    meeting = rig.meeting()
    meeting.transcription.diar_status = "failed"
    meeting.processing_status = ProcessingStatus.PARTIAL
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    sync(rig)
    assert rig.bridge.rediarizeMeeting(MID, 0, True)
    rig.jobs.tick()
    sync(rig)
    assert rig.bridge.cancelDiarizationRedo(MID, 0)
    rig.jobs.tick()
    sync(rig)
    assert rig.meeting().meeting_generation != 0
    assert rig.bridge.diarizationRedo["expectedGeneration"] == 0
    assert rig.bridge.keepDiarizationAsIs(MID, 0)
    rig.jobs.tick()
    assert rig.meeting().processing_status == ProcessingStatus.SUCCEEDED


@pytest.mark.parametrize("kind", ["names", "merge", "neither"])
def test_warning_names_or_merges_from_same_pair(rig: SaveRig, kind: str) -> None:
    names = rig.store.read(MID, "speakers.json")
    for record in names.labels.values():
        if kind != "names":
            record.name = None
        if kind != "merge":
            record.merged_into = None
    rig.store.write(MID, "speakers.json", names, writer=Writer.GUI)
    read_pair(rig)
    assert rig.bridge.diarizationRedo["needsWarning"] == (kind != "neither")


def test_cancel_ignores_committing_even_with_old_published_generation(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueue(rig)
    monkeypatch.setattr(
        rig.jobs,
        "redo_snapshot",
        lambda _: {
            "busy": True,
            "status": "committing",
            "stage": "reconcile",
            "percent": 100,
        },
    )
    cancel = Mock()
    monkeypatch.setattr(rig.jobs, "cancel", cancel)
    assert not rig.bridge.cancelDiarizationRedo(MID, 0)
    assert not cancel.called


@pytest.mark.parametrize("order", ["read-before-save", "read-during-write", "deliver-during-write"])
def test_late_loader_cannot_revert_saved_names(rig: SaveRig, order: str) -> None:
    bridge = rig.bridge
    if order == "read-during-write":
        assert bridge.renameSpeaker("S1", "Новое имя")
    bridge.retryCard()
    rig.tasks.pop(0)()  # Complete the read, but delay delivery to the GUI.
    if order != "read-during-write":
        assert bridge.renameSpeaker("S1", "Новое имя")
    if order != "deliver-during-write":
        assert bridge.flushSpeakerWrites()
    bridge._card_poll()
    assert bridge.cardRows.get(1)["who"] == "Новое имя"
    assert "Новое имя" in bridge.savePreviewText("txt")
    assert bridge.flushSpeakerWrites()
    bridge._card_poll()
    while rig.tasks:
        rig.tasks.pop(0)()
    bridge._card_poll()
    assert bridge.renameSpeaker("S2", "Второе имя")
    assert bridge.flushSpeakerWrites()
    assert bridge.cardRows.get(1)["who"] == "Новое имя"
    assert bridge.cardRows.get(3)["who"] == "Второе имя"
    names = rig.store.read(MID, "speakers.json")
    assert names.labels["S1"].name == "Новое имя"
    assert names.labels["S2"].name == "Второе имя"
    for fmt in ("txt", "md"):
        content = bridge.savePreviewText(fmt)
        assert "Новое имя" in content and "Второе имя" in content
        rig.answer = str(rig.documents / f"fresh-names.{fmt}")

        def save(fmt: str = fmt) -> str:
            return str(bridge.saveAs(fmt))

        assert save_result(rig, save) == "saved"
        assert Path(rig.answer).read_text(encoding="utf-8") == content


@pytest.mark.parametrize("kind", ["separated", "failed", "offer", "kept"])
@pytest.mark.parametrize("background", [False, True])
def test_enqueue_failure_is_redo_outcome_independent_of_export(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch, kind: str, background: bool
) -> None:
    import errno

    from astra_voice.core.meeting_session import MetadataWriter
    from astra_voice.meetings import store
    from astra_voice.security.files import write_private

    bridge = rig.bridge
    if kind != "separated":
        meeting = rig.meeting()
        meeting.transcription.diarization = kind == "failed"
        meeting.transcription.diar_status = "failed" if kind == "failed" else "skipped"
        meeting.processing_status = (
            ProcessingStatus.PARTIAL if kind == "failed" else ProcessingStatus.SUCCEEDED
        )
        text = rig.store.read(MID, "transcript.json")
        for row in text.utterances:
            if row.speaker != "me":
                row.speaker = "others"
        text.transcript_revision += 1
        rig.store.write(MID, "transcript.json", text, writer=Writer.TRANSCRIBE)
        rig.store.write(
            MID, "speakers.json", SpeakerMap(transcript_generation=0), writer=Writer.GUI
        )
        rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
        sync(rig)
        read_pair(rig)
        if kind == "kept":
            bridge._redo_local[MID] = "kept"
    expected = "redo-failed" if kind == "separated" else "failed-again"
    content = bridge.savePreviewText("txt")
    entered, release = threading.Event(), threading.Event()
    writer = MetadataWriter()
    submit = rig.jobs._submit
    if background:
        rig.jobs._submit = writer.submit
    calls = 0

    def fail(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal calls
        if Path(path).name == "meeting.json":
            calls += 1
            if background and calls == 1:
                entered.set()
                assert release.wait(5)
            raise OSError(errno.EIO, "injected descriptor failure")
        write_private(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(store, "write_private", fail)
            assert bridge.rediarizeMeeting(MID, 0, True)
            if background:
                assert entered.wait(2)
                # The export uses its own real writer and fails on a missing directory.
                rig.answer = str(rig.documents / "missing" / "export.txt")
                assert save_result(rig, lambda: bridge.saveAs("txt")) == "error"
                assert bridge.cardSaveError == meeting_card.TEXTS["saveError"]
                bridge.clearCardSaveError()  # D4 cancellation must not cancel the redo result.
                assert bridge.cardSaveError == ""
                release.set()
                assert writer.close(timeout=2)
            rig.recording.tick()
            assert calls == 3 and rig.meeting().rediar is None
            assert rig.jobs.failures[MID] == "io"
            state = bridge.diarizationRedo
            assert state["code"] == expected and state["visible"]
            assert state["text"] == redo_presentation(expected, 0)["text"]
            assert state["canKeep"] == (kind != "separated")
            assert bool(state["action"]) == (kind != "separated")
            assert bridge.cardSaveError == ""
            bridge.clearCardSaveError()
            assert bridge.diarizationRedo["code"] == expected
            assert bridge.savePreviewText("txt") == content
        rig.jobs._submit = submit
        assert bridge.rediarizeMeeting(MID, 0, True)
        rig.recording.tick()
        assert rig.meeting().rediar is not None
        assert bridge.diarizationRedo["code"] not in {"redo-failed", "failed-again"}
        assert bridge.cardSaveError == ""
    finally:
        release.set()
        assert writer.close(timeout=2)
        rig.jobs._submit = submit


@pytest.mark.parametrize("outcome", ["failed-again", "cancelled", "kept"])
def test_offer_terminal_outcome_does_not_outlive_audio_on_disk(rig: SaveRig, outcome: str) -> None:
    from datetime import UTC, datetime, timedelta

    wall = [datetime(2026, 11, 13, 12, tzinfo=UTC)]
    rig.jobs.wall_clock = lambda: wall[0]
    meeting = rig.meeting()
    if outcome == "kept":
        meeting.transcription.diar_status = "failed"
        meeting.processing_status = ProcessingStatus.PARTIAL
    else:
        meeting.transcription.diarization = False
        meeting.transcription.diar_status = "skipped"
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    sync(rig)
    rig.jobs.start_retention()
    rig.jobs.tick()
    if outcome == "kept":
        assert rig.bridge.keepDiarizationAsIs(MID, 0)
        rig.recording.tick()
        assert rig.bridge.diarizationRedo["code"] == "kept-7"
    else:
        assert rig.bridge.divideMeeting(MID, 0)
        if outcome == "cancelled":
            rig.jobs.tick()
            assert rig.bridge.cancelDiarizationRedo(MID, 0)
        else:
            rig.start_running()
            active = rig.jobs._active
            assert active is not None
            rig.role.emit(
                "meeting.job.failed",
                meeting_id=MID,
                meeting_generation=active.generation,
                code="io",
            )
            rig.recording.tick()
            rig.role.exited = True
        rig.recording.tick()
        assert rig.jobs.redo_results[MID] == outcome
        assert rig.bridge.diarizationRedo["code"] == (
            "offer" if outcome == "cancelled" else "failed-again"
        )
    assert rig.meeting().audio.present
    wall[0] += timedelta(days=9)
    rig.recording.tick()  # Ordinary retention pass, with the card still open.
    assert not rig.meeting().audio.present
    assert rig.bridge.cardOpen
    assert rig.bridge.diarizationRedo["audioPresent"] is False
    assert rig.bridge.diarizationRedo["code"] == ""
    assert rig.bridge.diarizationRedo["text"] == ""
    assert "звук удалён" in rig.bridge.card["subtitle"]["suffix"]


@pytest.mark.parametrize("outcome", ["failed-again", "cancelled"])
def test_original_diar_failure_without_audio_overrides_ephemeral_result(
    rig: SaveRig, outcome: str
) -> None:
    meeting = rig.meeting()
    meeting.transcription.diar_status = "failed"
    meeting.processing_status = ProcessingStatus.PARTIAL
    meeting.audio.present = False
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    rig.jobs.redo_results[MID] = outcome
    sync(rig)
    assert rig.bridge.diarizationRedo["code"] == "no-audio"
    assert "Звук сохранён" not in rig.bridge.diarizationRedo["text"]
    assert not rig.bridge.diarizationRedo["action"]


def test_failed_offer_publishes_cleanup_pending_after_real_unlink_error(rig: SaveRig) -> None:
    from datetime import UTC, datetime, timedelta

    wall = [datetime(2026, 11, 13, 12, tzinfo=UTC)]
    now = [1000.0]
    rig.jobs.wall_clock = lambda: wall[0]
    rig.jobs.clock = lambda: now[0]
    meeting = rig.meeting()
    meeting.transcription.diarization = False
    meeting.transcription.diar_status = "skipped"
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    sync(rig)
    rig.jobs.start_retention()
    assert rig.bridge.divideMeeting(MID, 0)
    rig.start_running()
    active = rig.jobs._active
    assert active is not None
    rig.role.emit(
        "meeting.job.failed", meeting_id=MID, meeting_generation=active.generation, code="io"
    )
    rig.recording.tick()
    rig.role.exited = True
    rig.recording.tick()
    assert rig.bridge.diarizationRedo["code"] == "failed-again"
    obstacle = rig.store.root / MID / "audio" / "not-an-audio-file"
    obstacle.mkdir(mode=0o700)  # Actual EISDIR from unlink(), no filesystem mocks.
    wall[0] += timedelta(days=10)
    rig.recording.tick()
    assert rig.meeting().cleanup_pending and rig.meeting().audio.present
    assert rig.jobs.meetings[MID].cleanup_pending
    state = rig.bridge.diarizationRedo
    assert state["cleanupPending"] and state["code"] == "" and not state["visible"]
    assert not state["canStart"] and not state["canKeep"]
    assert state["action"] == "" and state["keepAction"] == "" and state["audioUntil"] == ""
    assert "звук удаляется" in rig.bridge.card["subtitle"]["suffix"]
    assert "звук удаляется" in rig.bridge.meetings[0]["subtitle"]
    assert "до 19.11" not in rig.bridge.card["subtitle"]["suffix"]
    rig.open_card()
    assert not rig.bridge.diarizationRedo["visible"]
    assert not rig.bridge.divideMeeting(MID, 0)
    assert not rig.bridge.rediarizeMeeting(MID, 0, True)
    assert not rig.bridge.keepDiarizationAsIs(MID, 0)
    obstacle.rmdir()
    now[0] += 4.9
    rig.recording.tick()
    assert rig.meeting().audio.present and rig.meeting().cleanup_pending
    now[0] += 0.1
    rig.recording.tick()
    assert not rig.meeting().audio.present and not rig.meeting().cleanup_pending
    assert not rig.jobs.meetings[MID].audio.present
    assert "звук удалён" in rig.bridge.card["subtitle"]["suffix"]
    assert not rig.bridge.diarizationRedo["visible"]


@pytest.mark.parametrize("failed", [False, True])
def test_cleanup_pending_disables_actions_and_never_promises_storage(
    rig: SaveRig, failed: bool
) -> None:
    meeting = rig.meeting()
    meeting.transcription.diarization = failed
    meeting.transcription.diar_status = "failed" if failed else "skipped"
    meeting.processing_status = ProcessingStatus.PARTIAL if failed else ProcessingStatus.SUCCEEDED
    meeting.cleanup_pending = True
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    sync(rig)
    state = rig.bridge.diarizationRedo
    assert state["code"] == ("no-audio" if failed else "")
    assert not state["canStart"] and not state["canKeep"]
    assert not state["action"] and not state["keepAction"]
    assert state["audioPresent"] and state["cleanupPending"]
    assert state["audioUntil"] == ""
    if failed:
        assert "звук удаляется" in state["text"]
        assert "Записи звука нет" not in state["text"]
    assert "звук удаляется" in rig.bridge.card["subtitle"]["suffix"]
    assert "звук удалён" not in rig.bridge.card["subtitle"]["suffix"]
    assert not rig.bridge.keepDiarizationAsIs(MID, 0)


def test_enqueue_failure_outcome_lasts_until_card_close(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.meetings.store import MeetingStoreError

    def fail(*args: Any, **kwargs: Any) -> None:
        raise MeetingStoreError("io")

    monkeypatch.setattr(rig.store, "rediar_enqueue", fail)
    assert rig.bridge.rediarizeMeeting(MID, 0, True)
    rig.recording.tick()
    assert rig.bridge.diarizationRedo["code"] == "redo-failed"
    rig.bridge.clearCardSaveError()
    rig.bridge.changed.emit()
    assert rig.bridge.diarizationRedo["code"] == "redo-failed"
    rig.bridge.closeMeeting()
    rig.open_card()
    assert rig.jobs.failures[MID] == "io"
    assert rig.bridge.diarizationRedo["code"] == ""
    assert rig.bridge.cardSaveError == ""


@pytest.mark.parametrize("failed", [False, True])
def test_enqueue_failure_outcome_is_checked_against_confirmed_audio(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    from astra_voice.meetings.store import MeetingStoreError

    meeting = rig.meeting()
    meeting.transcription.diarization = failed
    meeting.transcription.diar_status = "failed" if failed else "skipped"
    meeting.processing_status = ProcessingStatus.PARTIAL if failed else ProcessingStatus.SUCCEEDED
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    sync(rig)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise MeetingStoreError("io")

    monkeypatch.setattr(rig.store, "rediar_enqueue", fail)
    assert rig.bridge.rediarizeMeeting(MID, 0, True)
    rig.recording.tick()
    assert rig.bridge.diarizationRedo["code"] == "failed-again"
    meeting.audio.present = False
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    sync(rig)
    assert rig.bridge.diarizationRedo["code"] == ("no-audio" if failed else "")
    assert "Звук сохранён" not in rig.bridge.diarizationRedo["text"]
