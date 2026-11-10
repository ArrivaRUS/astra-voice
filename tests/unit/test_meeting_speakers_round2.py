"""Round 2 regressions: Qt reentry, untrusted aliases and bounded GUI work."""

from __future__ import annotations

import json
import statistics
import threading
import time
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5.QtTest import QSignalSpy
from test_meeting_card_bridge import SaveRig
from test_meeting_jobs_ui import MID
from test_meeting_speakers import data
from test_meeting_speakers import rig as rig
from test_tray import qapp as qapp

from astra_voice.meetings import templates
from astra_voice.meetings.model import SpeakerMap, Utterance
from astra_voice.meetings.store import Writer

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_reentry_from_real_model_signal_is_serialized(rig: SaveRig) -> None:
    bridge = rig.bridge
    entered = False
    observations: list[tuple[bool, str]] = []

    def nested(*args: object) -> None:
        nonlocal entered
        if not entered:
            entered = True
            result = bridge.renameSpeaker("S1", "second")
            observations.append((result, bridge._speakers.labels["S1"].name or ""))

    bridge.cardRows.modelAboutToBeReset.connect(nested)
    bridge.cardRows.dataChanged.connect(nested)
    assert bridge.renameSpeaker("S1", "first")
    assert entered and observations == [(True, "first")]
    assert bridge.cardRows.get(1)["who"] == "second"
    bridge.closeMeeting()
    assert rig.store.read(MID, "speakers.json").labels["S1"].name == "second"


@pytest.mark.parametrize(
    "labels",
    [
        {"me": {"merged_into": "S1"}, "S1": {"name": "bad"}},
        {"S9": {"name": "ghost"}, "S1": {"name": "bad"}},
        {"S1": {"merged_into": "others"}},
        {"S1": {"merged_into": "S3"}, "S3": {"merged_into": "S1"}},
    ],
)
def test_invalid_disk_map_is_empty_everywhere_without_read_writes(
    rig: SaveRig, labels: dict[str, Any]
) -> None:
    path = rig.store.root / MID / "speakers.json"
    raw = json.dumps({"schema": 1, "renumbered": False, "labels": labels})
    path.write_text(raw)
    stamp = path.stat().st_mtime_ns
    rig.open_card()
    bridge = rig.bridge
    assert path.read_text() == raw and path.stat().st_mtime_ns == stamp
    assert bridge.cardRows.get(0)["who"] == "Я"
    assert bridge.cardRows.get(1)["who"] == (
        "bad" if labels.get("S1", {}).get("name") else "Спикер 1"
    )
    assert "S9" not in [row["id"] for row in bridge.card["participants"]]
    assert not bridge.renameSpeaker("S9", "ghost")
    assert not bridge.mergeSpeakers("S1", "S9")
    for template in templates.TEMPLATES:
        for fmt in templates.FORMATS:
            text = templates.render(
                template, rig.meeting(), bridge._card_transcript, fmt, speakers=bridge._speakers
            )
            assert "ghost" not in text
            if template == "stats":
                assert "Я" in text
    assert bridge.renameSpeaker("S1", "valid")
    bridge.closeMeeting()
    assert set(rig.store.read(MID, "speakers.json").labels) == {"S1", "S2", "S3"}


def large(rig: SaveRig) -> None:
    meeting, transcript, _ = data()
    transcript.transcript_revision = 1
    transcript.utterances = [
        Utterance(
            f"u{i}",
            seq=i,
            speaker="S1",
            track="far",
            text="речь",
            start_ms=i * 1000,
            end_ms=i * 1000 + 1000,
            final=True,
        )
        for i in range(9000)
    ]
    rig.store.write(MID, "transcript.json", transcript, writer=Writer.TRANSCRIBE)
    rig.store.write(MID, "speakers.json", SpeakerMap(), writer=Writer.GUI)
    rig.open_card()


def test_9000_replies_no_reset_and_benchmark(rig: SaveRig) -> None:
    large(rig)
    model = rig.bridge.cardRows
    resets = QSignalSpy(model.modelReset)
    changes = QSignalSpy(model.dataChanged)
    labels = []
    rig.bridge.cardChanged.connect(
        lambda: labels.append(rig.bridge.card["participants"][1]["label"])
    )
    times = []
    for name in ("one", "two", "three"):
        started = time.perf_counter()
        assert rig.bridge.renameSpeaker("S1", name)
        times.append(1000 * (time.perf_counter() - started))
        assert model.get(8999)["who"] == name
        assert rig.bridge.flushSpeakerWrites()
    print(f"renameSpeaker 9000 ms: {times}; median={statistics.median(times):.3f}")
    assert not resets
    assert len(changes) == 3 and labels == ["one", "two", "three"]
    assert statistics.median(times) < 15


def test_background_fifo_and_last_name_on_close(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = rig.bridge
    entered, release = threading.Event(), threading.Event()
    original = rig.store.write
    writes: list[tuple[str | None, int]] = []
    gui_thread = threading.get_ident()

    def slow(mid: str, filename: str, document: Any, **kwargs: Any) -> None:
        writes.append((document.labels["S1"].name, threading.get_ident()))
        entered.set()
        assert release.wait(1)
        original(mid, filename, document, **kwargs)

    monkeypatch.setattr(rig.store, "write", slow)
    spy = QSignalSpy(bridge.speakerOperationFinished)
    assert bridge.renameSpeaker("S1", "first")
    bridge._speaker_poll()
    assert entered.wait(1)
    assert bridge.renameSpeaker("S1", "second")
    assert bridge.renameSpeaker("S1", "last")
    assert bridge.cardRows.get(1)["who"] == "last"
    assert not spy  # accepted, but no durability receipt yet
    timer = threading.Timer(0.05, release.set)
    timer.start()
    try:
        bridge.closeMeeting()
    finally:
        release.set()
        timer.join()
    assert not bridge.cardOpen
    assert [name for name, _ in writes] == ["first", "second", "last"]
    assert all(thread != gui_thread for _, thread in writes)
    assert rig.store.read(MID, "speakers.json").labels["S1"].name == "last"
    assert [row[0] for row in spy] == ["renamed"] * 3


def test_failed_write_rolls_back_and_discards_dependent_snapshots(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = rig.bridge
    assert bridge.renameSpeaker("S1", "durable")
    assert bridge.flushSpeakerWrites()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fail(*args: Any, **kwargs: Any) -> None:
        calls.append(args)
        entered.set()
        assert release.wait(1)
        raise OSError("PRIVATE_NAME")

    monkeypatch.setattr(rig.store, "write", fail)
    assert bridge.renameSpeaker("S1", "failure")
    bridge._speaker_poll()
    assert entered.wait(1)
    assert bridge.renameSpeaker("S1", "dependent")
    assert bridge.cardRows.get(1)["who"] == "dependent"
    release.set()
    assert bridge.flushSpeakerWrites()
    assert len(calls) == 1
    assert bridge.speakerOperation["code"] == "save-error"
    assert bridge.cardRows.get(1)["who"] == "durable"
    assert bridge.card["participants"][1]["label"] == "durable"
    assert "PRIVATE_NAME" not in str(bridge.speakerOperation)
    assert rig.store.read(MID, "speakers.json").labels["S1"].name == "durable"
    bridge.setProperty("saveTemplate", "stats")
    assert bridge.savePreview()["rows"][0]["who"] == "durable"


def test_merge_reset_reentry_and_completion_signal_reentry(rig: SaveRig) -> None:
    bridge = rig.bridge
    during: list[bool] = []
    guard = False

    def nested() -> None:
        nonlocal guard
        if not guard:
            guard = True
            during.append(bridge.renameSpeaker("S1", "second"))
            assert bridge.cardRows.get(1)["who"] != "second"

    bridge.cardRows.modelAboutToBeReset.connect(nested)
    assert bridge.mergeSpeakers("S2", "S1")
    assert during == [True]
    assert bridge.cardRows.get(3)["who"] == "second"

    def on_result(code: str) -> None:
        if code == "merged":
            assert bridge.renameSpeaker("S1", "third")

    bridge.speakerOperationFinished.connect(on_result)
    assert bridge.flushSpeakerWrites()
    assert bridge.cardRows.get(1)["who"] == "third"
    assert bridge.card["participants"][1]["label"] == "third"
    assert rig.store.read(MID, "speakers.json").labels["S1"].name == "third"


def test_incremental_roles_panel_identity_and_preview_visibility(rig: SaveRig) -> None:
    bridge = rig.bridge
    old = bridge.card["participants"]
    data = QSignalSpy(bridge.cardRows.dataChanged)
    resets = QSignalSpy(bridge.cardRows.modelReset)
    previews = QSignalSpy(bridge.savePreviewChanged)
    assert bridge.renameSpeaker("S2", "Имя")
    assert not resets and len(data) == 1
    assert data[0][0].row() == data[0][1].row() == 3
    assert set(data[0][2]) == {259, 261, 264}  # who, num, accessibleName
    assert bridge.card["participants"][0] is old[0]
    assert not previews
    bridge.setProperty("savePreviewOpen", True)
    previews = QSignalSpy(bridge.savePreviewChanged)
    assert bridge.renameSpeaker("S2", "Имя 2")
    assert len(previews) == 1
    bridge.setProperty("savePreviewOpen", False)
    previews = QSignalSpy(bridge.savePreviewChanged)
    assert bridge.renameSpeaker("S2", "Имя 3")
    assert not previews
    assert "Имя 3" in bridge.cardRows.get(3)["accessibleName"]
    assert "Имя 3" in bridge.cardRows.data(bridge.cardRows.index(3), 264)


@pytest.mark.parametrize("close", ["window", "program"])
def test_window_and_program_drain_names(rig: SaveRig, close: str) -> None:
    from PyQt5.QtCore import QCoreApplication
    from PyQt5.QtGui import QCloseEvent
    from PyQt5.QtWidgets import QWidget

    bridge = rig.bridge
    assert bridge.renameSpeaker("S1", "last")
    if close == "window":
        window = QWidget()
        bridge.bind_window(window)
        event = QCloseEvent()
        QCoreApplication.sendEvent(window, event)
        assert event.isAccepted()
    else:
        bridge.shutdown()
    assert rig.store.read(MID, "speakers.json").labels["S1"].name == "last"


def test_close_timeout_retains_card_and_retry_flushes(
    rig: SaveRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = rig.bridge
    assert bridge.renameSpeaker("S1", "last")
    writer = bridge._speaker_writer
    original = writer.close
    monkeypatch.setattr(writer, "close", Mock(return_value=False))
    bridge.closeMeeting()
    assert bridge.cardOpen and bridge.speakerOperation["code"] == "save-error"
    monkeypatch.setattr(writer, "close", original)
    bridge.closeMeeting()
    assert not bridge.cardOpen
    assert rig.store.read(MID, "speakers.json").labels["S1"].name == "last"


@pytest.mark.parametrize("template", templates.TEMPLATES)
@pytest.mark.parametrize("fmt", templates.FORMATS)
def test_template_examples_and_untrusted_map(template: str, fmt: str) -> None:
    from astra_voice.meetings.model import Speaker

    meeting, transcript, names = data()
    transcript.utterances[2].start_ms = transcript.utterances[1].end_ms
    text = templates.render(template, meeting, transcript, fmt, speakers=names)
    assert "Спикер 1 (" not in text
    if template == "me_and_others":
        assert "Ирина Соколова" not in text and "Спикер" not in text
        assert "Я" in text and "Собеседники" in text
        assert "слово1 слово2 слово3" in text
    else:
        assert "Ирина Соколова" in text and "Спикер 3" not in text
    if template == "stenogram":
        assert (
            "[0:01:00] `Ирина Соколова`: `слово1 слово2`"
            if fmt == "md"
            else "[0:01:00] Ирина Соколова: слово1 слово2"
        ) in text
    if template == "text_only":
        assert "Участники:" in text and "слово1 слово2" in text
        assert "[0:" not in text
    names.labels["me"] = Speaker(merged_into="S1")
    names.labels["S9"] = Speaker(name="GHOST")
    direct = templates.render(template, meeting, transcript, fmt, speakers=names)
    assert direct == text
    stats = templates.statistics(meeting, transcript.utterances, speakers=names)
    assert next(row for row in stats.speakers if row.label == "Я").minutes == 1
    assert all(row.label != "GHOST" for row in stats.speakers)


def test_queued_disk_write_cannot_cross_generation(rig: SaveRig) -> None:
    bridge = rig.bridge
    path = rig.store.root / MID / "speakers.json"
    before = path.read_bytes()
    assert bridge.renameSpeaker("S1", "old generation")
    meeting = rig.meeting()
    meeting.meeting_generation += 1
    rig.store.write(MID, "meeting.json", meeting, writer=Writer.GUI)
    assert bridge.flushSpeakerWrites()
    assert path.read_bytes() == before
    assert bridge.speakerOperation["code"] == "save-error"


def test_room_binary_template_always_ignores_names() -> None:
    meeting, transcript, names = data()
    meeting.variant = "mic_only"
    for row in transcript.utterances:
        if row.speaker == "me":
            row.speaker = "S8"
    for fmt in templates.FORMATS:
        named = templates.render("me_and_others", meeting, transcript, fmt, speakers=names)
        assert named == templates.render("me_and_others", meeting, transcript, fmt)
        assert "Ирина" not in named and "Спикер" not in named
        assert "Участники" in named


def test_shutdown_receipt_callback_cannot_add_unflushed_work(rig: SaveRig) -> None:
    bridge = rig.bridge
    assert bridge.renameSpeaker("S1", "last")

    def callback(code: str) -> None:
        if code == "renamed":
            bridge.renameSpeaker("S1", "too late")

    bridge.speakerOperationFinished.connect(callback)
    bridge.shutdown()
    assert not bridge._speaker_pending and not bridge._speaker_commands
    assert rig.store.read(MID, "speakers.json").labels["S1"].name == "last"
    assert bridge.speakerOperation["code"] == "unavailable"
