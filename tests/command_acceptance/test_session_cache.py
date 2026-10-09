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
