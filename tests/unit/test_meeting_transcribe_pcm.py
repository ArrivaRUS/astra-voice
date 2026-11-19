"""D4: streaming PCM has identical bytes and no retained block arrays at the wait."""

from __future__ import annotations

import errno
import fcntl
import os
import time
import wave
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_meeting_transcribe_diar import ScriptedDiar, start

from astra_voice.meetings.model import Transcript
from astra_voice.meetings.timeline import SegmentTimeline
from astra_voice.worker import meeting_diar_client as client
from astra_voice.worker import meeting_transcribe as mt
from helpers.meeting_audio import MID, Rig, bursts, make_disk, write_track

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


@pytest.fixture
def pcm_reader(tmp_path: Path) -> Iterator[tuple[mt._Reader, list[mt._Span]]]:
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    reader = mt._Reader(directory)
    spans: list[mt._Span] = []
    # Different epochs/tracks, a recorded gap, fractional durations and drift.
    for index, (track, first, length, recorded, ppm) in enumerate(
        [
            ("far", 0, 48203, 200, 200),
            ("far", 90000, 36107, 4600, -200),
            ("mic", 0, 80013, 50, 0),
            ("far", 0, 16013, 6500, 0),
        ]
    ):
        name = f"{track}-{index}.wav"
        pcm = ((np.arange(length, dtype=np.int32) * 7 + index * 311) % 60000 - 30000).astype("<i2")
        fd = os.open(tmp_path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as pcm_stream, wave.open(pcm_stream, "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(pcm.tobytes())
        wav = reader.probe(name)
        assert wav is not None
        duration = round(length / 16 * (1 + ppm / 1_000_000))
        clock = SegmentTimeline(
            first, first + length, False, ((first, 10000), (first + length, 10000 + duration))
        )
        spans.append(
            mt._Span(
                track,
                mt._Mapping(clock, recorded, 10000),
                [mt._Piece(name, first, first + length, wav.offset)],
            )
        )
    try:
        yield reader, spans
    finally:
        reader.close()
        os.close(directory)


def source(reader: mt._Reader, spans: list[mt._Span], first: int, end: int) -> client.PcmSource:
    return client.PcmSource(
        (end - first) * 16,
        lambda fd: mt.MeetingTranscribeState._write_block_pcm(
            fd, reader, spans, first, end, lambda: None
        ),
    )


@pytest.mark.parametrize("first,end", [(0, 9000), (1000, 6000), (3250, 4550), (7500, 10000)])
@pytest.mark.parametrize("track", ["mic", "far"])
def test_streamed_memfd_matches_old_pcm_byte_for_byte(
    pcm_reader: tuple[mt._Reader, list[mt._Span]], first: int, end: int, track: str
) -> None:
    reader, spans = pcm_reader
    spans = [span for span in spans if span.track == track]
    expected = mt.MeetingTranscribeState._block_audio(reader, spans, first, end).tobytes()
    fd = client.sealed_pcm(source(reader, spans, first, end))
    try:
        assert os.fstat(fd).st_size == len(expected)
        assert os.pread(fd, len(expected) + 1, 0) == expected
        assert fcntl.fcntl(fd, 1034) & 0xF == 0xF
        with pytest.raises(OSError):
            os.write(fd, b"x")
    finally:
        os.close(fd)


def test_partial_writes_keep_exact_pcm(
    pcm_reader: tuple[mt._Reader, list[mt._Span]], monkeypatch: pytest.MonkeyPatch
) -> None:
    reader, spans = pcm_reader
    expected = mt.MeetingTranscribeState._block_audio(reader, spans, 1000, 9000).tobytes()
    write = os.write
    monkeypatch.setattr(os, "write", lambda fd, view: write(fd, view[:4093]))
    fd = client.sealed_pcm(source(reader, spans, 1000, 9000))
    try:
        assert os.pread(fd, len(expected) + 1, 0) == expected
    finally:
        os.close(fd)


def test_no_pcm_arrays_survive_until_child_reply(
    pcm_reader: tuple[mt._Reader, list[mt._Span]],
    monkeypatch: pytest.MonkeyPatch,
    recorded_heap_trim: list[None],
) -> None:
    reader, spans = pcm_reader
    references: list[Any] = []
    read = reader.read

    def bounded_read(span: mt._Span, a: int, b: int) -> Any:
        assert b - a <= mt.DIAR_PCM_CHUNK_FRAMES
        audio = read(span, a, b)
        references.append(weakref.ref(audio))
        return audio

    monkeypatch.setattr(reader, "read", bounded_read)
    role = object.__new__(client.DiarProcess)
    role.identity = {"meeting_id": MID, "meeting_generation": 0}
    descriptors: list[int] = []

    def send(kind: str, **fields: Any) -> dict[str, Any]:
        assert kind == "diar.block" and fields["block_s"] == 1200
        fd = fields["descriptor"]
        assert fields["pcm_fd"] == fd and os.fstat(fd).st_size == 76_800_000
        assert fcntl.fcntl(fd, 1034) & 0xF == 0xF
        descriptors.append(fd)
        return {"req": "b1", "type": kind, "block_s": 1200, "expected_rtf": 1.0}

    frames = [
        {
            "type": "diar.block.part",
            **role.identity,
            "block_index": 0,
            "seq": 0,
            "last": True,
            "segments": [[0, 1000, "S1"]],
        },
        {
            "type": "diar.block.done",
            **role.identity,
            "block_index": 0,
            "req": "b1",
            "request_type": "diar.block",
            "segments": [],
        },
    ]

    def receive(deadline: float) -> dict[str, Any]:
        assert references and all(ref() is None for ref in references)
        assert recorded_heap_trim == [None]
        with pytest.raises(OSError) as closed:
            os.fstat(descriptors[0])
        assert closed.value.errno == errno.EBADF
        return frames.pop(0)

    monkeypatch.setattr(role, "send", send)
    monkeypatch.setattr(role, "receive", receive)
    assert role.block(source(reader, spans, 0, 1_200_000), 0, 0) == [[0, 1000, "S1"]]


@pytest.mark.parametrize("cause", ["write", "zero-write", "cancel", "seal"])
def test_failed_pcm_build_closes_descriptor(
    pcm_reader: tuple[mt._Reader, list[mt._Span]], monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    reader, spans = pcm_reader
    opened: list[int] = []
    create = os.memfd_create

    def create_fd(*args: Any) -> int:
        fd = create(*args)
        opened.append(fd)
        return fd

    monkeypatch.setattr(os, "memfd_create", create_fd)
    count = 0

    def check() -> None:
        nonlocal count
        count += 1
        if cause == "cancel" and count >= 4:
            raise client.DiarCancelled

    if cause in {"write", "zero-write"}:

        def write(fd: int, view: Any) -> int:
            if cause == "write":
                raise OSError("test-write-error")
            return 0

        monkeypatch.setattr(os, "write", write)
    if cause == "seal":

        def seal(*args: Any) -> Any:
            raise OSError("test-seal-error")

        monkeypatch.setattr(fcntl, "fcntl", seal)
    audio = client.PcmSource(
        9 * 16000,
        lambda fd: mt.MeetingTranscribeState._write_block_pcm(fd, reader, spans, 0, 9000, check),
    )
    with pytest.raises((OSError, client.DiarCancelled)):
        client.sealed_pcm(audio)
    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF


@pytest.mark.usefixtures("synthetic_diar_models")
@pytest.mark.parametrize("cancel", [False, True])
def test_pcm_failure_keeps_existing_job_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    disk = make_disk(tmp_path)
    write_track(disk, "far", bursts([0.5]))
    rig = Rig(disk)
    opened: list[int] = []

    class ProducingDiar(ScriptedDiar):
        def block(self, audio: Any, index: int, offset: int) -> list[list[Any]]:
            client.sealed_pcm(audio)  # The injected write below must fail/cancel.
            raise AssertionError("PCM construction must not succeed")

    def fail(fd: int, view: memoryview, check: Any = None) -> None:
        opened.append(fd)
        assert rig.state.job is not None
        if cancel:
            rig.state.job.cancel()
            raise mt._Cancelled
        raise OSError("test-pcm-write")

    monkeypatch.setattr(mt, "_write_pcm", fail)
    rig.state.diar_factory = lambda identity, cancelled, **kw: ProducingDiar(identity, cancelled)
    try:
        rig.load()
        start(rig, tracks=["far"])
        if cancel:
            until = time.monotonic() + 3
            while time.monotonic() < until and (
                rig.state.job is None or not rig.state.job.finished
            ):
                time.sleep(0.001)
            assert rig.state.job is not None and rig.state.job.finished and rig.state.job.cancelled
            rig.pump()
            assert not any(e["type"] == "meeting.job.done" for e in rig.events)
        else:
            assert rig.finish()["diar_status"] == "failed"
        text = disk.read(MID, "transcript.json")
        assert isinstance(text, Transcript) and text.utterances
        assert rig.state.engine is None
        assert opened
        for fd in opened:
            with pytest.raises(OSError) as closed:
                os.fstat(fd)
            assert closed.value.errno == errno.EBADF
    finally:
        rig.close()
