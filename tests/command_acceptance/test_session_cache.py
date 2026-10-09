"""login1 requirements with a delayed transport; no real system/session bus."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from astra_voice.platform.session_state import (
    LOGIN,
    MANAGER_PATH,
    SESSION,
    ReadCallback,
    SessionState,
)
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
