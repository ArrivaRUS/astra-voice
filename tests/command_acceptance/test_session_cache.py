"""login1 requirements with a delayed transport; no real system/session bus."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.core.command_mode import SessionSnapshot
from astra_voice.core.meeting_session import MeetingController, StorageStatus
from astra_voice.meetings.store import MeetingStore
from astra_voice.platform import session_state
from astra_voice.platform.session import SessionKind
from astra_voice.platform.session_state import (
    LOGIN,
    MANAGER_PATH,
    SESSION,
    ReadCallback,
    SessionState,
)
from astra_voice.worker import ipc
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit
SESSION_PATH = "/org/freedesktop/login1/session/one"
OTHER_PATH = "/org/freedesktop/login1/session/two"


class FakeLogin:
    def __init__(self) -> None:
        self.pending: list[tuple[str, str, str, list[Any], ReadCallback]] = []
        self.subscriptions: dict[tuple[str, str], Callable[..., None]] = {}
        self.trace: list[tuple[str, str, str]] = []
        self.rows: list[list[Any]] = [["one", 1000, "user", "seat0", SESSION_PATH]]
        self.props: dict[str, dict[str, Any]] = {
            SESSION_PATH: {
                "User": [1000, "/org/freedesktop/login1/user/_1000"],
                "Class": "user",
                "Type": "x11",
                "Remote": False,
                "Seat": ["seat0", "/org/freedesktop/login1/seat/seat0"],
                "Display": ":99",
                "LockedHint": False,
                "Active": True,
            }
        }
        self.sleeping = False
        self.closed = 0

    def read(
        self, path: str, interface: str, method: str, arguments: list[Any], callback: ReadCallback
    ) -> None:
        self.trace.append(("read", path, method))
        self.pending.append((path, interface, method, arguments, callback))

    def subscribe(
        self, service: str, path: str, interface: str, signal: str, callback: Callable[..., None]
    ) -> Callable[[], None]:
        self.trace.append(("subscribe", path, signal))
        key = (path, signal)
        self.subscriptions[key] = callback

        def cancel() -> None:
            self.subscriptions.pop(key, None)

        return cancel

    def signal(self, path: str, name: str, *arguments: Any) -> None:
        self.subscriptions[(path, name)](*arguments)

    def complete_one(self) -> None:
        path, _, method, _, callback = self.pending.pop(0)
        payload: Any = {
            "Get": self.sleeping,
            "GetSession": SESSION_PATH,
            "ListSessions": self.rows,
            "GetAll": self.props.get(path),
        }[method]
        callback(payload, False)

    def complete(self) -> None:
        for _ in range(20):
            if not self.pending:
                return
            self.complete_one()
        raise AssertionError("session read cycle did not finish")

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def session() -> Iterator[tuple[SessionState, FakeLogin]]:
    get_qapplication()
    fake = FakeLogin()
    cache = SessionState(fake, uid=1000)
    yield cache, fake
    cache.stop()


def ready(session: tuple[SessionState, FakeLogin]) -> tuple[SessionState, FakeLogin]:
    cache, fake = session
    cache.start()
    fake.complete()
    assert cache.is_admissible()
    return cache, fake


def add_remote_session(
    fake: FakeLogin, session_id: str = "two", *, session_type: str = "tty", uid: int = 1000
) -> str:
    path = f"/org/freedesktop/login1/session/{session_id}"
    fake.rows.append([session_id, uid, "user", "", path])
    fake.props[path] = {
        "User": [uid, f"/org/freedesktop/login1/user/_{uid}"],
        "Class": "user",
        "Type": session_type,
        "Remote": True,
        "Seat": ["", "/"],
    }
    fake.signal(MANAGER_PATH, "SessionNew", session_id, path)
    return path


def remove_session(fake: FakeLogin, path: str) -> None:
    fake.rows = [row for row in fake.rows if row[4] != path]
    del fake.props[path]
    fake.signal(MANAGER_PATH, "SessionRemoved", path.rsplit("/", 1)[-1], path)


@pytest.fixture
def recording(
    session: tuple[SessionState, FakeLogin],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filesystem_euid: None,
) -> Iterator[tuple[MeetingController, list[dict[str, Any]], list[SessionSnapshot]]]:
    cache, _ = session
    worker = session_state._SessionWorker()
    worker._fly_environment = False
    worker.state = cache
    cache.changed.connect(worker._changed)
    with monkeypatch.context() as patch:
        patch.setattr(session_state, "_fly_process_present", lambda: False)
        ready(session)

    sent: list[dict[str, Any]] = []
    published: list[SessionSnapshot] = []
    supervisor = Mock(ready=True, generation=1)
    supervisor.send.side_effect = sent.append
    controller = MeetingController(
        MeetingStore(tmp_path / "meetings"),
        supervisor,
        session=lambda: worker.snapshot,
        session_kind=SessionKind.KDE,
        enabled=lambda: True,
        policy_allowed=lambda: True,
        storage_probe=lambda _: StorageStatus(1000, True, False),
        metadata_submit=lambda job: job(),
        filesystem_submit=lambda job: job(),
        clock=lambda: 100.0,
    )

    def notify(snapshot: SessionSnapshot) -> None:
        published.append(snapshot)
        controller.on_session_changed()

    worker.updated.connect(notify)
    try:
        assert controller.start()
        assert controller.meeting is not None
        for state, fields in (
            (
                "prepared",
                {
                    "mic_label": "",
                    "far_available": False,
                    "server_local": True,
                    "server_kind": "pipewire",
                },
            ),
            ("started", {"t0_mono_ms": 100_000, "tracks": ["mic"]}),
        ):
            request = sent[-1]
            event = {
                "type": f"meeting.capture.{state}",
                "req": request["req"],
                "request_type": request["type"],
                "meeting_id": controller.meeting.id,
                "meeting_generation": 0,
                "capture_id": "capture1",
                "state_rev": int(state == "started"),
                "blocked_epoch": 1,
                "pause_reasons": [],
                "manual_resume_required": False,
                **fields,
            }
            ipc.encode(event, role="meeting-capture")
            controller.handle_event({**event, "generation": 1})
        assert controller.state == "recording"
        yield controller, sent, published
    finally:
        cache.changed.disconnect(worker._changed)
        controller.close()


@pytest.mark.parametrize("uid", [1000, 1001], ids=["ssh", "other-user"])
def test_unrelated_login_and_logout_leave_recording_running(
    session: tuple[SessionState, FakeLogin],
    recording: tuple[MeetingController, list[dict[str, Any]], list[SessionSnapshot]],
    uid: int,
) -> None:
    cache, fake = session
    controller, sent, published = recording
    path = add_remote_session(fake, uid=uid)
    if uid != cache.uid:
        # An exact foreign UID is enough even for a local graphical session.
        fake.props[path].update(Type="x11", Remote=False, Seat=fake.props[SESSION_PATH]["Seat"])
        fake.rows[-1][3] = "seat0"
    fake.complete()
    reads = len(fake.trace)
    remove_session(fake, path)
    fake.complete()
    unknown = [snapshot.known for snapshot in published if not snapshot.known]
    pauses = [message["reason"] for message in sent if message["type"] == "meeting.capture.pause"]
    assert (unknown, pauses) == ([], [])
    assert controller.state == "recording"
    assert len(fake.trace) == reads


@pytest.mark.parametrize("session_type", ["x11", "wayland"])
def test_remote_graphical_session_is_not_a_candidate(
    session: tuple[SessionState, FakeLogin], session_type: str
) -> None:
    cache, fake = ready(session)
    add_remote_session(fake, session_type=session_type)
    assert cache.known
    fake.complete()
    assert cache.is_admissible()
    fake.signal(SESSION_PATH, "PropertiesChanged", SESSION, {}, ["LockedHint"])
    assert not cache.known
    fake.complete()
    assert cache.is_admissible()


@pytest.mark.parametrize(
    "properties",
    [
        {"Remote": False, "Seat": ["seat1", "/org/freedesktop/login1/seat/seat1"]},
        {"Remote": 1},
        {"Remote": "true"},
        {"Seat": ["seat1", "/org/freedesktop/login1/seat/seat1"]},
        {"Seat": [None, "/"]},
        {"Seat": ["", 0]},
        {"Class": "greeter"},
        {"User": [True, "/org/freedesktop/login1/user/_1000"]},
    ],
    ids=["local", "integer", "string", "seat", "seat-id-type", "seat-path-type", "class", "uid"],
)
def test_unconfirmed_graphical_session_fails_closed(
    session: tuple[SessionState, FakeLogin], properties: dict[str, Any]
) -> None:
    cache, fake = ready(session)
    path = add_remote_session(fake, session_type="x11")
    fake.props[path].update(properties)
    assert cache.known  # Wait for evidence before deciding whether to invalidate.
    fake.complete()
    assert not cache.known and not cache.is_admissible()


def test_classification_read_error_invalidates_and_refreshes(
    session: tuple[SessionState, FakeLogin],
) -> None:
    cache, fake = ready(session)
    path = add_remote_session(fake)
    assert cache.known
    request_path, _, method, _, reply = fake.pending.pop(0)
    assert (request_path, method) == (path, "GetAll")
    reply(None, True)
    assert not cache.known and not cache.is_admissible()
    fake.complete()
    assert cache.is_admissible()


def test_lock_during_classification_keeps_locked_verdict(
    session: tuple[SessionState, FakeLogin],
) -> None:
    cache, fake = ready(session)
    path = add_remote_session(fake)
    fake.signal(SESSION_PATH, "Lock")
    assert cache.known and cache.locked and not cache.is_admissible()
    fake.complete()
    assert cache.known and cache.locked and not cache.is_admissible()
    remove_session(fake, path)
    assert not cache.known  # The pre-Lock reply must not cache the path either.


def test_selected_session_events_invalidate_without_classification(
    session: tuple[SessionState, FakeLogin],
) -> None:
    cache, fake = ready(session)
    fake.signal(MANAGER_PATH, "SessionNew", "one", SESSION_PATH)
    assert not cache.known
    assert fake.trace[-1] == ("read", MANAGER_PATH, "Get")
    fake.complete()
    assert cache.is_admissible()
    remove_session(fake, SESSION_PATH)
    assert not cache.known and not cache.is_admissible()
    assert not fake.pending


def test_removal_before_classification_reply_takes_fail_closed_path(
    session: tuple[SessionState, FakeLogin],
) -> None:
    cache, fake = ready(session)
    path = add_remote_session(fake)
    properties = fake.props[path]
    request_path, _, method, _, reply = fake.pending.pop(0)
    assert (request_path, method) == (path, "GetAll")
    remove_session(fake, path)
    assert not cache.known
    fake.complete()
    assert cache.is_admissible()
    reply(properties, False)
    assert cache.is_admissible()
    assert not fake.pending


@pytest.mark.parametrize("event", ["disconnected", "owner-changed", "stop"])
def test_classification_reset_restores_removal_invalidation(
    session: tuple[SessionState, FakeLogin], event: str
) -> None:
    cache, fake = ready(session)
    path = add_remote_session(fake)
    fake.complete()
    assert cache.is_admissible()
    if event == "disconnected":
        fake.signal("/org/freedesktop/DBus/Local", "Disconnected")
        assert not cache.known
    elif event == "owner-changed":
        fake.signal("/org/freedesktop/DBus", "NameOwnerChanged", LOGIN, ":1.1", ":1.2")
        assert not cache.known
        fake.complete()
        assert cache.is_admissible()
    else:
        cache.stop()
        ready(session)
    seen: list[bool] = []
    cache.changed.connect(lambda: seen.append(cache.known))
    remove_session(fake, path)
    assert seen and not any(seen)
    fake.complete()
    assert cache.is_admissible()


def test_classification_overflow_restores_removal_pause(
    session: tuple[SessionState, FakeLogin],
    recording: tuple[MeetingController, list[dict[str, Any]], list[SessionSnapshot]],
) -> None:
    cache, fake = session
    controller, sent, published = recording
    for index in range(session_state._MAX_IRRELEVANT_SESSIONS):
        add_remote_session(fake, f"ssh{index}", uid=1001)
        fake.complete()
    path = add_remote_session(fake, "overflow", uid=1001)
    fake.complete()
    assert cache.is_admissible()
    assert all(snapshot.known for snapshot in published)

    remove_session(fake, "/org/freedesktop/login1/session/ssh0")
    assert not cache.known
    fake.complete()
    assert cache.is_admissible()
    remove_session(fake, path)
    assert not cache.known
    fake.complete()
    assert cache.is_admissible()
    assert [m["reason"] for m in sent if m["type"] == "meeting.capture.pause"] == ["unknown"]
    assert controller.state == "paused" and controller.manual_resume_required


def test_construction_unknown_then_subscriptions_before_first_locked_hint_read(
    session: tuple[SessionState, FakeLogin],
) -> None:
    cache, fake = session
    assert not cache.is_admissible()
    assert fake.trace == []
    cache.start()
    assert not cache.is_admissible()
    fake.complete()
    assert cache.is_admissible()
    read_index = fake.trace.index(("read", SESSION_PATH, "GetAll"))
    for signal in ("Lock", "Unlock", "PropertiesChanged"):
        assert fake.trace.index(("subscribe", SESSION_PATH, signal)) < read_index
    before = list(fake.trace)
    cache.start()
    assert fake.trace == before  # idempotent start; no polling at steady state


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("User", [1001, "/org/freedesktop/login1/user/_1001"]),
        ("User", [True, "/org/freedesktop/login1/user/_1000"]),
        ("Class", "greeter"),
        ("Type", "tty"),
        ("Remote", True),
        ("Seat", ["", "/org/freedesktop/login1/seat/seat0"]),
        ("Display", ""),
        ("LockedHint", "false"),
        ("LockedHint", 0),
    ],
)
def test_graphical_session_must_be_verified_with_exact_types(
    session: tuple[SessionState, FakeLogin], field: str, value: object
) -> None:
    cache, fake = session
    fake.props[SESSION_PATH][field] = value
    cache.start()
    fake.complete()
    assert not cache.known
    assert not cache.is_admissible()


def test_multiple_graphical_sessions_are_unknown(session: tuple[SessionState, FakeLogin]) -> None:
    cache, fake = session
    fake.rows.append(["two", 1000, "user", "seat1", OTHER_PATH])
    fake.props[OTHER_PATH] = dict(fake.props[SESSION_PATH])
    cache.start()
    fake.complete()
    assert not cache.known
    assert not cache.is_admissible()


def test_lock_latch_cannot_be_reopened_by_unrelated_stale_hint_false(
    session: tuple[SessionState, FakeLogin],
) -> None:
    cache, fake = ready(session)
    fake.signal(SESSION_PATH, "Lock")
    assert cache.locked
    fake.signal(MANAGER_PATH, "SessionNew", "other", OTHER_PATH)
    fake.complete()
    assert cache.known and cache.locked
    assert cache.lock_unconfirmed()
    assert not cache.is_admissible()


def test_unlock_needs_full_fresh_read(session: tuple[SessionState, FakeLogin]) -> None:
    cache, fake = ready(session)
    fake.signal(SESSION_PATH, "Lock")
    fake.signal(SESSION_PATH, "Unlock")
    assert not cache.is_admissible()
    fake.complete()
    assert cache.is_admissible()


def test_prelock_reply_cannot_reopen_admission(session: tuple[SessionState, FakeLogin]) -> None:
    cache, fake = ready(session)
    cache.refresh()
    fake.complete_one()  # PreparingForSleep
    fake.complete_one()  # GetSession
    fake.complete_one()  # ListSessions, GetAll is now pending
    fake.signal(SESSION_PATH, "Lock")
    fake.complete()
    assert not cache.is_admissible()
    assert not cache.known


def test_sleep_invalidates_reads_and_resume_requires_confirmation(
    session: tuple[SessionState, FakeLogin],
) -> None:
    cache, fake = ready(session)
    fake.signal(MANAGER_PATH, "PrepareForSleep", True)
    assert cache.suspending
    assert not cache.known and not cache.is_admissible()
    fake.signal(MANAGER_PATH, "PrepareForSleep", False)
    assert not cache.suspending
    assert not cache.is_admissible()
    fake.complete()
    assert cache.is_admissible()


def test_malformed_signals_close_cache_without_escaping_qt_slot(
    session: tuple[SessionState, FakeLogin],
) -> None:
    cache, fake = ready(session)
    fake.signal(SESSION_PATH, "PropertiesChanged", SESSION, "bad-map", [])
    assert not cache.is_admissible()
    cache.refresh()
    fake.complete()
    assert cache.is_admissible()
    fake.signal(MANAGER_PATH, "PrepareForSleep", "false")
    assert not cache.is_admissible()


def test_disconnection_and_owner_loss_are_fail_closed(
    session: tuple[SessionState, FakeLogin],
) -> None:
    cache, fake = ready(session)
    fake.signal("/org/freedesktop/DBus", "NameOwnerChanged", LOGIN, ":1.1", "")
    assert not cache.is_admissible()
    cache.refresh()
    fake.complete()
    fake.signal("/org/freedesktop/DBus/Local", "Disconnected")
    assert not cache.is_admissible()


def test_properties_invalidated_triggers_reread_before_permission(
    session: tuple[SessionState, FakeLogin],
) -> None:
    cache, fake = ready(session)
    fake.signal(SESSION_PATH, "PropertiesChanged", SESSION, {}, ["LockedHint"])
    assert not cache.is_admissible()
    fake.props[SESSION_PATH]["LockedHint"] = True
    fake.complete()
    assert cache.known and cache.locked


def test_stop_unsubscribes_and_ignores_pending_response(
    session: tuple[SessionState, FakeLogin],
) -> None:
    cache, fake = ready(session)
    cache.refresh()
    cache.stop()
    assert fake.subscriptions == {}
    fake.complete()
    assert not cache.is_admissible()
    assert not cache.known
    assert fake.closed == 1


@pytest.mark.parametrize("active", [False, True])
def test_login1_active_is_published_by_worker_and_command_guard(
    session: tuple[SessionState, FakeLogin], monkeypatch: pytest.MonkeyPatch, active: bool
) -> None:
    from astra_voice.platform import session_state as module

    cache, fake = session
    monkeypatch.setattr(module, "_fly_process_present", lambda: False)
    worker = module._SessionWorker()
    worker._fly_environment = False
    worker.state = cache
    cache.changed.connect(worker._changed)
    fake.props[SESSION_PATH]["Active"] = active
    cache.start()
    fake.complete()
    assert cache.known and cache.active is active
    assert worker.snapshot.active is active
    assert worker.snapshot.allowed is active  # Existing command guard, real producer.


@pytest.mark.parametrize("invalidated", [False, True])
def test_active_changed_invalidates_then_rereads_before_admission(
    session: tuple[SessionState, FakeLogin], monkeypatch: pytest.MonkeyPatch, invalidated: bool
) -> None:
    from astra_voice.platform import session_state as module

    cache, fake = session
    monkeypatch.setattr(module, "_fly_process_present", lambda: False)
    worker = module._SessionWorker()
    worker._fly_environment = False
    worker.state = cache
    cache.changed.connect(worker._changed)
    cache.start()
    fake.complete()
    before = worker.snapshot.cue_epoch
    assert worker.snapshot.allowed
    fake.props[SESSION_PATH]["Active"] = False
    fake.signal(
        SESSION_PATH,
        "PropertiesChanged",
        SESSION,
        {} if invalidated else {"Active": False},
        ["Active"] if invalidated else [],
    )
    assert not cache.known and not worker.snapshot.active and not worker.snapshot.allowed
    assert worker.snapshot.cue_epoch > before
    fake.complete()
    assert cache.known and not worker.snapshot.active and not worker.snapshot.allowed
    fake.props[SESSION_PATH]["Active"] = True
    fake.signal(SESSION_PATH, "PropertiesChanged", SESSION, {"Active": True}, [])
    assert not cache.known
    fake.complete()
    assert worker.snapshot.active and worker.snapshot.allowed


@pytest.mark.parametrize("value", [None, "true", 1, 0, [], {}])
def test_active_missing_or_nonbool_keeps_session_unknown(
    session: tuple[SessionState, FakeLogin], value: object
) -> None:
    cache, fake = session
    if value is None:
        fake.props[SESSION_PATH].pop("Active")
    else:
        fake.props[SESSION_PATH]["Active"] = value
    cache.start()
    fake.complete()
    assert not cache.known and not cache.active and not cache.is_admissible()


@pytest.mark.parametrize("fly", [False, True])
def test_real_producer_epoch_rejects_old_cue_before_queued_gui_delivery(
    session: tuple[SessionState, FakeLogin], monkeypatch: pytest.MonkeyPatch, fly: bool
) -> None:
    from types import SimpleNamespace
    from typing import cast
    from unittest.mock import Mock

    from PyQt5.QtCore import Qt

    from astra_voice.core.recording_cues import RecordingCues
    from astra_voice.platform import session_state as module
    from astra_voice.runtime import DictationRuntime

    cache, fake = session
    monkeypatch.setattr(module, "_fly_process_present", lambda: fly)
    worker = module._SessionWorker()
    worker._fly_environment = fly
    worker.state = cache
    cache.changed.connect(worker._changed)
    # No QCoreApplication.processEvents: simulate delayed GUI dispatch while
    # the real producer keeps publishing immutable snapshots in its own thread.
    gui_callbacks: list[object] = []
    worker.updated.connect(gui_callbacks.append, Qt.QueuedConnection)
    cache.start()
    fake.complete()
    assert worker.snapshot.known and worker.snapshot.active
    assert worker.snapshot.supported is not fly
    host = cast(
        DictationRuntime,
        SimpleNamespace(
            _closed=False, _command_session=SimpleNamespace(snapshot=lambda: worker.snapshot)
        ),
    )

    def admission() -> tuple[bool, int, int]:
        return DictationRuntime._cue_admission(host)

    spawn = Mock(side_effect=AssertionError("Must not spawn old cue"))
    player = RecordingCues(enabled=True, available=lambda: True, popen=spawn, admission=admission)
    # Keep the queue pending; all readiness/epoch checks use production methods.
    monkeypatch.setattr(player, "_run", lambda: None)
    try:
        player.play("start", (1, "u"))
        cue = player._queue[0]
        assert player._valid(cue)
        old_cue_epoch, old_command_epoch = worker.snapshot.cue_epoch, worker.snapshot.blocked_epoch
        cache.refresh()  # known→unknown, including on Fly where supported=False
        assert not worker.snapshot.known
        fake.complete()  # known again, still no GUI dispatch
        assert admission()[0] is True
        assert worker.snapshot.cue_epoch > old_cue_epoch
        if fly:
            assert worker.snapshot.blocked_epoch == old_command_epoch
        else:
            assert worker.snapshot.blocked_epoch > old_command_epoch
        assert gui_callbacks == []
        assert not player._valid(cue)
        player._play(cue, {}, 0)
        spawn.assert_not_called()
        player._queue.clear()
        player.play("stop", (1, "u"))
        assert not player._queue  # Old session's stop is rejected as well.
    finally:
        player.shutdown()
