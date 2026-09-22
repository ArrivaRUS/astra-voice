"""Автомат и очередь хоткея: синтетическое время, без Qt и X-сервера."""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import weakref
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, MagicMock, call

import pytest

from astra_voice.platform import x11
from astra_voice.platform.hotkey import (
    BUSY_MESSAGE,
    DEFAULT_CANDIDATES,
    LOOKAHEAD_S,
    EvdevHotkeyBackend,
    GrabResult,
    HotkeyEvent,
    HotkeyFsm,
    HotkeyManager,
    HotkeyMode,
    HotkeyState,
    X11HotkeyBackend,
)
from astra_voice.platform.x11 import BadCombo, GrabReport, ParsedCombo, X11Display

pytestmark = pytest.mark.unit


class FakeBackend:
    """Очередь и захваты в памяти; никаких обращений к дисплею."""

    def __init__(self) -> None:
        self.grabbed: set[str] = set()
        self.events: list[HotkeyEvent] = []
        self.lookahead: list[HotkeyEvent] = []
        self.calls: list[tuple[str, str]] = []
        self.escape = False
        self.busy: set[str] = set()
        self.available = True

    def grab_combo(self, combo: str) -> GrabResult:
        self.calls.append(("grab", combo))
        if not self.available:
            return GrabResult("not-grabbed")
        if not combo:
            return GrabResult("bad-combo")
        if combo in self.grabbed:
            return GrabResult("duplicate")
        if combo in self.busy:
            return GrabResult("busy", BUSY_MESSAGE)
        self.grabbed.add(combo)
        return GrabResult("ok", keycode=65)

    def ungrab_combo(self, combo: str) -> GrabResult:
        self.calls.append(("ungrab", combo))
        if combo not in self.grabbed:
            return GrabResult("not-grabbed")
        self.grabbed.remove(combo)
        return GrabResult("ok")

    def grab_escape(self) -> GrabResult:
        self.calls.append(("grab", "Escape"))
        self.escape = True
        return GrabResult("ok", keycode=9)

    def ungrab_escape(self) -> None:
        self.calls.append(("ungrab", "Escape"))
        self.escape = False

    def poll_events(self, timeout: float = 0.0) -> list[HotkeyEvent]:
        if timeout:
            assert timeout == LOOKAHEAD_S
            events, self.lookahead = self.lookahead, []
        else:
            events, self.events = self.events, []
        return events

    def fileno(self) -> int:
        return 17


def observed_fsm(
    mode: HotkeyMode = HotkeyMode.PTT,
) -> tuple[HotkeyFsm, list[tuple[HotkeyState, str]]]:
    states: list[tuple[HotkeyState, str]] = []
    return HotkeyFsm(mode, lambda state, reason: states.append((state, reason))), states


def test_ptt_release_and_done() -> None:
    fsm, states = observed_fsm()
    fsm.press(0.0)
    assert fsm.limit_deadline == 120.0
    fsm.release(1.0)
    assert fsm.state == HotkeyState.PROCESSING
    assert "release" in states[-1][1]
    assert fsm.limit_deadline is None
    fsm.press(2.0)
    assert fsm.state == HotkeyState.PROCESSING
    fsm.done(3.0)
    assert states[-1] == (HotkeyState.IDLE, "done")


def test_record_limit_matches_worker_default() -> None:
    from astra_voice.core.constants import RECORD_LIMIT_S as CORE_RECORD_LIMIT_S
    from astra_voice.platform.hotkey import RECORD_LIMIT_S
    from astra_voice.worker.state import LIMIT_S_DEFAULT

    assert RECORD_LIMIT_S == LIMIT_S_DEFAULT == CORE_RECORD_LIMIT_S == 120.0


def test_tap_switches_to_toggle_for_one_recording() -> None:
    fsm, states = observed_fsm()
    fsm.press(0.0)
    fsm.release(0.1)
    assert fsm.state == HotkeyState.RECORDING
    assert fsm.mode == HotkeyMode.TOGGLE
    assert "tap→toggle" in states[-1][1]
    assert fsm.limit_deadline == 120.0
    fsm.press(2.0)
    assert states[-1] == (HotkeyState.PROCESSING, "toggle-off")
    fsm.done(3.0)
    fsm.press(4.0)
    assert fsm.mode.value == "ptt"


@pytest.mark.parametrize("release_at", [0.1, 1.0])
def test_configured_toggle_ignores_release(release_at: float) -> None:
    fsm, states = observed_fsm(HotkeyMode.TOGGLE)
    fsm.press(0.0)
    fsm.release(release_at)
    assert states == [(HotkeyState.RECORDING, "press")]
    fsm.press(2.0)
    assert states[-1] == (HotkeyState.PROCESSING, "toggle-off")


@pytest.mark.parametrize("processing", [False, True])
def test_escape_cancels_recording_and_processing(processing: bool) -> None:
    fsm, states = observed_fsm()
    fsm.press(0.0)
    if processing:
        fsm.release(1.0)
    fsm.escape(2.0)
    assert states[-1] == (HotkeyState.IDLE, "escape-cancel")
    assert fsm.limit_deadline is None
    before = states.copy()
    fsm.tick(121.0)
    fsm.escape(122.0)
    fsm.done(123.0)
    fsm.release(124.0)
    assert states == before


@pytest.mark.parametrize("limit_at", [120.0, 121.0])
def test_limit(limit_at: float) -> None:
    fsm, states = observed_fsm()
    fsm.press(0.0)
    fsm.tick(119.0)
    assert fsm.state == HotkeyState.RECORDING
    fsm.tick(limit_at)
    assert states[-1] == (HotkeyState.PROCESSING, "limit")
    assert fsm.limit_deadline is None
    fsm.tick(130.0)
    assert len(states) == 2


@pytest.mark.parametrize(
    ("held", "expected"),
    [
        (0.299, HotkeyState.RECORDING),
        (0.300, HotkeyState.PROCESSING),
        (0.301, HotkeyState.PROCESSING),
    ],
)
def test_threshold_equality_is_ptt(held: float, expected: HotkeyState) -> None:
    """Только строго меньше 0,3 с — тап; ровно 0,3 с уже PTT."""
    fsm = HotkeyFsm()
    fsm.press(0.0)
    fsm.release(held)
    assert fsm.state == expected


def test_repeat_pair_matches_sequence_without_pair() -> None:
    def run(repeat: bool) -> tuple[list[tuple[HotkeyState, str]], list[str]]:
        backend = FakeBackend()
        manager = HotkeyManager(backend)
        states: list[tuple[HotkeyState, str]] = []
        callbacks: list[str] = []
        manager.on_state = lambda state, reason: states.append((state, reason))
        manager.on_press = lambda: callbacks.append("press")
        manager.on_release = lambda: callbacks.append("release")
        assert manager.grab("Ctrl+Shift+Space", HotkeyMode.PTT).ok
        backend.events = [HotkeyEvent("KeyPress", 65, 100)]
        manager.process_pending(0.0)
        backend.events = (
            [HotkeyEvent("KeyRelease", 65, 150), HotkeyEvent("KeyPress", 65, 150)] if repeat else []
        ) + [HotkeyEvent("KeyRelease", 65, 1100)]
        manager.process_pending(1.0)
        return states, callbacks

    expected = run(False)
    assert expected[1] == ["press", "release"]
    assert expected[0][-1][0] == HotkeyState.PROCESSING
    assert run(True) == expected


def test_repeat_pair_across_reads_and_repeated_press() -> None:
    backend = FakeBackend()
    manager = HotkeyManager(backend)
    manager.grab("Ctrl+Shift+Space", HotkeyMode.TOGGLE)
    backend.events = [HotkeyEvent("KeyPress", 65, 100)]
    manager.process_pending(0.0)
    backend.events = [HotkeyEvent("KeyRelease", 65, 200)]
    backend.lookahead = [HotkeyEvent("KeyPress", 65, 200)]
    manager.process_pending(0.1)
    backend.events = [HotkeyEvent("KeyPress", 65, 300)]
    manager.process_pending(0.2)
    assert manager.fsm.state == HotkeyState.RECORDING
    backend.events = [HotkeyEvent("KeyRelease", 65, 400), HotkeyEvent("KeyPress", 65, 500)]
    manager.process_pending(1.0)
    assert manager.fsm.state.value == "processing"


@pytest.mark.parametrize("same_keycode", [False, True])
def test_different_time_or_keycode_is_not_repeat(same_keycode: bool) -> None:
    backend = FakeBackend()
    manager = HotkeyManager(backend)
    manager.grab("Ctrl+Shift+Space", HotkeyMode.PTT)
    manager.handle_event(HotkeyEvent("KeyPress", 65, 1), 0.0)
    backend.events = [
        HotkeyEvent("KeyRelease", 65, 1000),
        HotkeyEvent("KeyPress", 65 if same_keycode else 66, 1001 if same_keycode else 1000),
    ]
    manager.process_pending(1.0)
    assert manager.fsm.state == HotkeyState.PROCESSING


def test_duplicate_and_not_grabbed() -> None:
    backend = FakeBackend()
    manager = HotkeyManager(backend, clock=lambda: 2.0)
    assert manager.process_pending(0.0).code == "not-grabbed"
    assert manager.handle_event(HotkeyEvent("KeyPress", 65, 1), 0.0).code == "not-grabbed"
    assert manager.fsm.state == HotkeyState.IDLE
    manager.ungrab()
    assert manager.last_result.code == "not-grabbed"
    assert manager.grab("Ctrl+Shift+Space", HotkeyMode.PTT).ok
    assert manager.grab("Ctrl+Shift+Space", HotkeyMode.TOGGLE).code == "duplicate"
    assert manager.fsm.mode == HotkeyMode.PTT
    assert backend.calls == [("grab", "Ctrl+Shift+Space")]
    manager.ungrab()
    assert manager.last_result.ok
    manager.ungrab()
    assert manager.last_result.code == "not-grabbed"
    assert not backend.grabbed


@pytest.mark.parametrize("finish", ["escape", "done", "ungrab"])
def test_escape_grab_lifetime(finish: str) -> None:
    backend = FakeBackend()
    manager = HotkeyManager(backend, clock=lambda: 3.0)
    callbacks: list[str] = []
    manager.on_escape = lambda: callbacks.append("escape")
    manager.grab("Ctrl+Shift+Space", HotkeyMode.PTT)
    assert not backend.escape
    manager.handle_event(HotkeyEvent("KeyPress", 65, 1), 0.0)
    assert backend.escape
    manager.handle_event(HotkeyEvent("KeyRelease", 65, 1001), 1.0)
    assert backend.escape
    if finish == "escape":
        manager.handle_event(HotkeyEvent("KeyPress", 9, 2001), 2.0)
        assert callbacks == ["escape"]
    elif finish == "done":
        manager.fsm.done(2.0)
    else:
        manager.ungrab()
    assert manager.fsm.state == HotkeyState.IDLE
    assert not backend.escape
    assert backend.calls.count(("grab", "Escape")) == 1
    assert backend.calls.count(("ungrab", "Escape")) == 1


def test_escape_in_same_queue_as_start() -> None:
    backend = FakeBackend()
    manager = HotkeyManager(backend)
    manager.grab("Ctrl+Shift+Space", HotkeyMode.PTT)
    backend.events = [HotkeyEvent("KeyPress", 65, 1), HotkeyEvent("KeyPress", 9, 2)]
    manager.process_pending(0.0)
    assert manager.fsm.state == HotkeyState.IDLE
    assert not backend.escape


def test_probe_and_candidates_preserve_active_grab() -> None:
    backend = FakeBackend()
    manager = HotkeyManager(backend)
    manager.grab("Ctrl+F8", HotkeyMode.PTT)
    backend.busy.add("Ctrl+Space")
    assert manager.probe("Ctrl+Space").owner_hint == BUSY_MESSAGE
    assert manager.probe("").code == "bad-combo"
    assert manager.probe("Ctrl+F8").code == "duplicate"
    prefer = [DEFAULT_CANDIDATES[1], "Ctrl+Space", "", DEFAULT_CANDIDATES[0]]
    assert manager.free_candidates(prefer) == [DEFAULT_CANDIDATES[1], DEFAULT_CANDIDATES[0]]
    assert backend.grabbed == {"Ctrl+F8"}
    assert backend.calls[-2:] == [
        ("grab", DEFAULT_CANDIDATES[0]),
        ("ungrab", DEFAULT_CANDIDATES[0]),
    ]
    backend.available = False
    assert manager.free_candidates(list(DEFAULT_CANDIDATES)) == []
    assert manager.fileno() == 17


def test_failed_replacement_preserves_recording() -> None:
    backend = FakeBackend()
    backend.busy.add("Ctrl+Space")
    manager = HotkeyManager(backend)
    manager.grab("Ctrl+Shift+Space", HotkeyMode.PTT)
    manager.handle_event(HotkeyEvent("KeyPress", 65, 1), 0.0)
    assert manager.grab("Ctrl+Space", HotkeyMode.TOGGLE).code == "busy"
    assert manager.fsm.state == HotkeyState.RECORDING
    assert backend.escape
    assert backend.grabbed == {"Ctrl+Shift+Space"}


class FakeDisplay(X11Display):
    """Подмена только примитивов X11; соединение никогда не открывается."""

    def __init__(self) -> None:
        super().__init__()
        self.report = GrabReport(True, [(4, None)], False)
        self.calls: list[tuple[str, int, int]] = []
        self.available = True

    def open(self, display_name: str | None = None) -> bool:
        return self.available

    def parse_combo(self, combo: str) -> ParsedCombo:
        if combo == "bad":
            raise BadCombo
        return ParsedCombo(0, 9, "Escape") if combo == "Escape" else ParsedCombo(4, 65, "space")

    def grab_key(self, keycode: int, base_mask: int) -> GrabReport:
        self.calls.append(("grab", keycode, base_mask))
        return self.report

    def ungrab_key(self, keycode: int, base_mask: int) -> None:
        self.calls.append(("ungrab", keycode, base_mask))


@pytest.mark.parametrize(
    ("report", "code"),
    [
        (GrabReport(True, [(4, None)], False), "ok"),
        (GrabReport(False, [(4, "BadAccess")], True), "busy"),
        (GrabReport(False, [(4, "BadWindow")], False), "not-grabbed"),
    ],
)
def test_x11_uses_primitives_and_only_bad_access_means_busy(
    report: GrabReport,
    code: str,
) -> None:
    display = FakeDisplay()
    display.report = report
    backend = X11HotkeyBackend(display)
    result = backend.grab_combo("Ctrl+Space")
    assert result.code == code
    assert result.owner_hint == (BUSY_MESSAGE if code == "busy" else None)
    assert display.calls == [("grab", 65, 4)]
    backend.close()


def test_x11_canonical_duplicate_and_cleanup() -> None:
    display = FakeDisplay()
    backend = X11HotkeyBackend(display)
    assert backend.grab_combo("bad").code == "bad-combo"
    assert backend.grab_combo("Ctrl+Space").ok
    assert backend.grab_combo("control+space").code == "duplicate"
    assert backend.grab_escape().ok
    assert backend.grab_escape().code == "duplicate"
    backend.close()
    assert display.calls == [("grab", 65, 4), ("grab", 9, 0), ("ungrab", 9, 0), ("ungrab", 65, 4)]
    assert backend.ungrab_combo("Ctrl+Space").code == "not-grabbed"


def test_no_display_and_evdev_stub() -> None:
    display = FakeDisplay()
    display.available = False
    backend = X11HotkeyBackend(display)
    manager = HotkeyManager(backend)
    assert manager.free_candidates(list(DEFAULT_CANDIDATES)) == []
    assert manager.grab("Ctrl+Space", HotkeyMode.PTT).code == "not-grabbed"
    assert manager.fileno() == -1
    assert backend.poll_events() == []
    assert display.calls == []
    with pytest.raises(NotImplementedError):
        EvdevHotkeyBackend()


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Подмена конструктора Xlib: даже при DISPLAY=:0 соединение не открывается."""
    pytest.importorskip("Xlib")
    from Xlib import XK
    from Xlib import display as xdisplay

    conn = MagicMock()
    mapping = {
        9: "Escape",
        37: "Control_L",
        50: "Shift_L",
        55: "v",
        65: "space",
        66: "Caps_Lock",
        67: "F1",
        77: "Num_Lock",
        78: "Scroll_Lock",
    }
    symbols = {code: XK.string_to_keysym(name) for code, name in mapping.items()}
    conn.keysym_to_keycode.side_effect = lambda symbol: next(
        (code for code, value in symbols.items() if value == symbol), 0
    )
    conn.get_keyboard_mapping.side_effect = lambda code, count: [(symbols.get(code, 0),)]
    conn.get_modifier_mapping.return_value = [[50], [66], [37], [], [77], [], [], [78]]
    conn.screen.return_value.root.grab_keyboard.return_value = 0
    conn.create_resource_object.return_value = conn.screen.return_value.root
    conn.screen.return_value.root.query_pointer.return_value.mask = 0
    conn.pending_events.return_value = 0
    conn.fileno.return_value = 17
    monkeypatch.setattr(xdisplay, "Display", lambda name=None: conn)
    return conn


@pytest.fixture
def mocked_x11(connection: MagicMock) -> Iterator[X11Display]:
    """Открытие и закрытие проверяются на подменённом соединении."""
    display = X11Display()
    assert display.open(":isolated-mock")
    try:
        yield display
    finally:
        display.close()


class FakeClock:
    """Монотонное время и ожидание без задержек тестового процесса."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []
        monkeypatch.setattr("astra_voice.platform.x11.time.monotonic", lambda: self.now)
        monkeypatch.setattr("astra_voice.platform.x11.time.sleep", self.sleep)

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.now += duration


@pytest.mark.parametrize("raises", [False, True])
def test_keyboard_context_always_releases(
    mocked_x11: X11Display, connection: MagicMock, raises: bool
) -> None:
    try:
        with mocked_x11.keyboard_grab(window_id=42, timeout_s=1) as grabbed:
            assert grabbed
            assert mocked_x11.keyboard_grab_deadline is not None
            connection.create_resource_object.assert_called_once_with("window", 42)
            if raises:
                raise RuntimeError("закрытие поля")
    except RuntimeError:
        assert raises
    connection.ungrab_keyboard.assert_called_once_with(0)
    assert mocked_x11.keyboard_grab_deadline is None


def test_keyboard_context_reports_refusal(mocked_x11: X11Display) -> None:
    mocked_x11.root.grab_keyboard.return_value = 1
    with mocked_x11.keyboard_grab() as grabbed:
        assert not grabbed
    assert mocked_x11.keyboard_grab_deadline is None


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("open", ()),
        ("parse_combo", ("Ctrl+space",)),
        ("grab_key", (65, 4)),
        ("ungrab_key", (65, 4)),
        ("fake_key", (0, True)),
        ("send_combo", ([], "Return")),
        ("active_window", ()),
        ("client_list_stacking", ()),
        ("wm_class", (0,)),
        ("window_geometry", (0,)),
        ("pending_events", ()),
        ("next_event", ()),
        ("fileno", ()),
    ],
)
def test_keyboard_deadline_enforced_by_public_operations(
    mocked_x11: X11Display,
    connection: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    args: tuple[Any, ...],
) -> None:
    clock = FakeClock(monkeypatch)
    assert mocked_x11.grab_keyboard(timeout_s=0.1)
    clock.now += 0.1
    connection.reset_mock()
    getattr(mocked_x11, method)(*args)
    assert connection.mock_calls[0] == call.ungrab_keyboard(0)
    assert mocked_x11.keyboard_grab_deadline is None


def test_keyboard_deadline_enforced_by_backend_poll(
    mocked_x11: X11Display, connection: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(monkeypatch)
    assert mocked_x11.grab_keyboard(timeout_s=0.1)
    clock.now += 0.1
    assert X11HotkeyBackend(mocked_x11).poll_events() == []
    connection.ungrab_keyboard.assert_called_once_with(0)


def test_keyboard_deadline_retries_failed_release(
    mocked_x11: X11Display, connection: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(monkeypatch)
    assert mocked_x11.grab_keyboard(timeout_s=0.1)
    clock.now += 0.1
    connection.ungrab_keyboard.side_effect = [RuntimeError("X11"), None]
    mocked_x11.pending_events()
    assert mocked_x11.keyboard_grab_expired()
    mocked_x11.pending_events()
    assert mocked_x11.keyboard_grab_deadline is None


def test_keyboard_close_releases_once(mocked_x11: X11Display, connection: MagicMock) -> None:
    assert mocked_x11.grab_keyboard()
    mocked_x11.close()
    mocked_x11.close()
    connection.ungrab_keyboard.assert_called_once_with(0)
    connection.close.assert_called_once_with()
    assert mocked_x11.keyboard_grab_deadline is None


def test_keyboard_finalizer_does_not_retain_owner(connection: MagicMock) -> None:
    display = X11Display()
    assert display.open(":isolated-mock")
    assert display.grab_keyboard()
    owner = weakref.ref(display)
    del display
    gc.collect()
    assert owner() is None
    connection.ungrab_keyboard.assert_called_once_with(0)
    connection.close.assert_called_once_with()


def test_keyboard_finalizer_at_process_exit(connection: MagicMock, tmp_path: Path) -> None:
    """Дочерний процесс оставляет объект живым до atexit; Xlib полностью подменён."""
    trace = tmp_path / "finalizer.txt"
    script = r"""
import sys
from pathlib import Path
from unittest.mock import MagicMock
from Xlib import display
from astra_voice.platform.x11 import X11Display

trace = Path(sys.argv[1])
def record(message):
    with trace.open("a") as stream:
        stream.write(message + "\n")
conn = MagicMock()
conn.get_modifier_mapping.return_value = [[] for _ in range(8)]
conn.screen.return_value.root.grab_keyboard.return_value = 0
conn.ungrab_keyboard.side_effect = lambda timestamp: record("ungrab")
conn.close.side_effect = lambda: record("close")
display.Display = lambda name: conn
x = X11Display()
assert x.open(":isolated-mock")
assert x.grab_keyboard()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(trace)],
        env={**os.environ, "DISPLAY": "", "PYTHONPATH": str(Path(x11.__file__).parents[2])},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert trace.read_text() == "ungrab\nclose\n"


@pytest.mark.parametrize("held", [1, 4, 8, 16, 32, 64, 128, 5])
def test_combo_waits_for_every_semantic_modifier_without_synthesis(
    mocked_x11: X11Display, monkeypatch: pytest.MonkeyPatch, held: int
) -> None:
    clock = FakeClock(monkeypatch)
    mocked_x11.lock_masks = {"Lock": 2, "Num_Lock": 0, "Scroll_Lock": 0}
    mocked_x11.root.query_pointer.return_value.mask = held
    synthesize = MagicMock(return_value=True)
    monkeypatch.setattr(mocked_x11, "fake_key", synthesize)
    assert not mocked_x11.send_combo(["Control_L"], "v")
    synthesize.assert_not_called()
    assert sum(clock.sleeps) == pytest.approx(x11.MODIFIER_RELEASE_TIMEOUT_S)


@pytest.mark.parametrize("timeout", [0.0, 0.013, 2.0])
def test_combo_timeout_is_hard_and_can_be_shortened(
    mocked_x11: X11Display, monkeypatch: pytest.MonkeyPatch, timeout: float
) -> None:
    clock = FakeClock(monkeypatch)
    mocked_x11.root.query_pointer.return_value.mask = 4
    synthesize = MagicMock(return_value=True)
    monkeypatch.setattr(mocked_x11, "fake_key", synthesize)
    assert not mocked_x11.send_combo(["Control_L"], "v", timeout_s=timeout)
    synthesize.assert_not_called()
    assert sum(clock.sleeps) == pytest.approx(min(timeout, x11.MODIFIER_RELEASE_TIMEOUT_S))


@pytest.mark.parametrize("held", [4, 5])
def test_combo_synthesizes_only_after_release_and_ignores_locks(
    mocked_x11: X11Display,
    connection: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    held: int,
) -> None:
    clock = FakeClock(monkeypatch)
    # 32 байта: keycode задаёт индекс байта и номер бита внутри него.
    keymap = [0] * 32
    for mask, code in ((4, 37), (1, 50)):
        if held & mask:
            keymap[code // 8] |= 1 << (code % 8)
    connection.query_keymap.side_effect = [keymap, [0] * 32]
    locks_and_button = 2 | 16 | 128 | 256
    mocked_x11.root.query_pointer.side_effect = [
        SimpleNamespace(mask=held | locks_and_button),
        SimpleNamespace(mask=locks_and_button),
    ]
    synthesize = MagicMock(return_value=True)
    monkeypatch.setattr(mocked_x11, "fake_key", synthesize)
    assert mocked_x11.send_combo(["Control_L"], "v")
    assert clock.sleeps == [0.005]
    assert synthesize.call_args_list == [
        call(37, True),
        call(55, True),
        call(55, False),
        call(37, False),
    ]


@pytest.mark.parametrize("keycode", [37, 50])
def test_combo_rejects_physical_modifier_with_empty_pointer_mask(
    mocked_x11: X11Display,
    connection: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    keycode: int,
) -> None:
    """Сброс маски XKB при смене раскладки не означает отпускание клавиши."""
    clock = FakeClock(monkeypatch)
    keymap = [0] * 32
    keymap[keycode // 8] = 1 << (keycode % 8)
    connection.query_keymap.return_value = keymap
    mocked_x11.root.query_pointer.return_value.mask = 0
    synthesize = MagicMock(return_value=True)
    monkeypatch.setattr(mocked_x11, "fake_key", synthesize)
    assert not mocked_x11.send_combo(["Control_L"], "v")
    synthesize.assert_not_called()
    assert sum(clock.sleeps) == pytest.approx(x11.MODIFIER_RELEASE_TIMEOUT_S)


@pytest.mark.parametrize("elapsed", [x11.MODIFIER_RELEASE_TIMEOUT_S, 0.201])
def test_combo_rejects_release_observed_at_or_after_deadline(
    mocked_x11: X11Display, monkeypatch: pytest.MonkeyPatch, elapsed: float
) -> None:
    clock = FakeClock(monkeypatch)

    def late_query() -> SimpleNamespace:
        clock.now += elapsed
        return SimpleNamespace(mask=0)

    mocked_x11.root.query_pointer.side_effect = late_query
    synthesize = MagicMock(return_value=True)
    monkeypatch.setattr(mocked_x11, "fake_key", synthesize)
    assert not mocked_x11.send_combo(["Control_L"], "v")
    synthesize.assert_not_called()


@pytest.mark.parametrize("timeout", [-1.0, float("nan"), float("inf")])
def test_combo_rejects_invalid_timeout(
    mocked_x11: X11Display, monkeypatch: pytest.MonkeyPatch, timeout: float
) -> None:
    synthesize = MagicMock(return_value=True)
    monkeypatch.setattr(mocked_x11, "fake_key", synthesize)
    assert not mocked_x11.send_combo(["Control_L"], "v", timeout_s=timeout)
    synthesize.assert_not_called()


def test_combo_query_failure_never_synthesizes(
    mocked_x11: X11Display, monkeypatch: pytest.MonkeyPatch
) -> None:
    mocked_x11.root.query_pointer.side_effect = RuntimeError("X11")
    synthesize = MagicMock(return_value=True)
    monkeypatch.setattr(mocked_x11, "fake_key", synthesize)
    assert not mocked_x11.send_combo(["Control_L"], "v")
    synthesize.assert_not_called()


@pytest.mark.parametrize("combo, mods", [("Escape", 0), ("Ctrl+Escape", 4)])
def test_escape_hotkey_stops_instead_of_cancelling(
    mocked_x11: X11Display, combo: str, mods: int
) -> None:
    manager = HotkeyManager(X11HotkeyBackend(mocked_x11))
    states: list[tuple[HotkeyState, str]] = []
    cancelled: list[bool] = []
    manager.on_state = lambda state, reason: states.append((state, reason))
    manager.on_escape = lambda: cancelled.append(True)
    assert manager.grab(combo, HotkeyMode.TOGGLE).ok
    manager.handle_event(HotkeyEvent("KeyPress", 9, 1, mods=mods), 0)
    manager.handle_event(HotkeyEvent("KeyRelease", 9, 2, escape=True, mods=mods), 1)
    manager.handle_event(HotkeyEvent("KeyPress", 9, 3, escape=True, mods=mods), 2)
    assert states[-1] == (HotkeyState.PROCESSING, "toggle-off")
    assert cancelled == []
    manager.ungrab()


def test_plain_escape_cancels_ctrl_escape_hotkey(mocked_x11: X11Display) -> None:
    manager = HotkeyManager(X11HotkeyBackend(mocked_x11))
    assert manager.grab("Ctrl+Escape", HotkeyMode.TOGGLE).ok
    manager.handle_event(HotkeyEvent("KeyPress", 9, 1, mods=4), 0)
    manager.handle_event(HotkeyEvent("KeyRelease", 9, 2, mods=4), 1)
    manager.handle_event(HotkeyEvent("KeyPress", 9, 3, escape=True), 2)
    assert manager.fsm.state == HotkeyState.IDLE
    manager.ungrab()


def test_ptt_release_after_modifier_release_still_stops(mocked_x11: X11Display) -> None:
    manager = HotkeyManager(X11HotkeyBackend(mocked_x11))
    assert manager.grab("Ctrl+space", HotkeyMode.PTT).ok
    manager.handle_event(HotkeyEvent("KeyPress", 65, 1, mods=4), 0)
    manager.handle_event(HotkeyEvent("KeyRelease", 65, 2, mods=0), 1)
    assert manager.fsm.state == HotkeyState.PROCESSING


@pytest.mark.parametrize("mapping_request", [0, 1])
def test_mapping_notify_regrabs_combo_escape_and_notifies(
    mocked_x11: X11Display, connection: MagicMock, mapping_request: int
) -> None:
    from Xlib import XK, X
    from Xlib.protocol.event import MappingNotify

    manager = HotkeyManager(X11HotkeyBackend(mocked_x11))
    states: list[tuple[HotkeyState, str]] = []
    manager.on_state = lambda state, reason: states.append((state, reason))
    assert manager.grab("Ctrl+space", HotkeyMode.TOGGLE).ok
    manager.handle_event(HotkeyEvent("KeyPress", 65, 1, mods=4), 0)
    manager.handle_event(HotkeyEvent("KeyRelease", 65, 2, mods=4), 1)
    notification = MappingNotify(request=mapping_request, first_keycode=8, count=248)

    def refresh(event: Any) -> None:
        assert event is notification
        # Старые захваты снимаются по прежним keycode и маскам блокировок.
        expected = [
            call(code, mask)
            for code, mods in ((9, 0), (65, 4))
            for mask in mocked_x11.mask_variants(mods)
        ]
        assert mocked_x11.root.ungrab_key.call_args_list == expected
        connection.keysym_to_keycode.side_effect = lambda symbol: (
            10 if symbol == XK.string_to_keysym("Escape") else 66
        )
        connection.get_modifier_mapping.return_value = [[] for _ in range(8)]

    connection.refresh_keyboard_mapping.side_effect = refresh
    queue = [notification, SimpleNamespace(type=X.KeyPress, detail=66, time=3, state=4 | 2 | 256)]
    connection.pending_events.side_effect = lambda: len(queue)
    connection.next_event.side_effect = lambda: queue.pop(0)
    manager.process_pending(2)
    connection.refresh_keyboard_mapping.assert_called_once_with(notification)
    assert (HotkeyState.RECORDING, "mapping-regrab:ok;escape:ok") in states
    assert manager.fsm.state == HotkeyState.PROCESSING
    assert manager.escape_result.keycode == 10
    assert mocked_x11.root.grab_key.call_args_list[-2:] == [
        call(66, 4, True, X.GrabModeAsync, X.GrabModeAsync, onerror=ANY),
        call(10, 0, True, X.GrabModeAsync, X.GrabModeAsync, onerror=ANY),
    ]
    queue.append(SimpleNamespace(type=X.KeyPress, detail=10, time=4, state=0))
    manager.process_pending(3)
    assert manager.fsm.state.value == HotkeyState.IDLE.value
    manager.ungrab()


@pytest.mark.parametrize("failure", ["busy", "refresh", "escape"])
def test_mapping_failure_is_reported(
    mocked_x11: X11Display, connection: MagicMock, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from Xlib import X
    from Xlib.protocol.event import MappingNotify

    manager = HotkeyManager(X11HotkeyBackend(mocked_x11))
    states: list[tuple[HotkeyState, str]] = []
    manager.on_state = lambda state, reason: states.append((state, reason))
    assert manager.grab("Ctrl+space", HotkeyMode.TOGGLE).ok
    manager.handle_event(HotkeyEvent("KeyPress", 65, 1, mods=4), 0)
    if failure == "refresh":
        connection.refresh_keyboard_mapping.side_effect = RuntimeError("X11")
    else:
        good = GrabReport(True, [(4, None)], False)
        busy = GrabReport(False, [(4, "BadAccess")], True)
        monkeypatch.setattr(
            mocked_x11,
            "grab_key",
            MagicMock(side_effect=[busy, good] if failure == "busy" else [good, busy]),
        )
    queue = [MappingNotify(request=X.MappingKeyboard, first_keycode=8, count=248)]
    connection.pending_events.side_effect = lambda: len(queue)
    connection.next_event.side_effect = lambda: queue.pop(0)
    manager.process_pending(1)
    expected = {
        "busy": "mapping-regrab:busy;escape:ok",
        "refresh": "mapping-regrab:not-grabbed;escape:not-grabbed",
        "escape": "mapping-regrab:ok;escape:busy",
    }
    assert states[-1] == (HotkeyState.RECORDING, expected[failure])
    assert manager.last_result.ok == (failure == "escape")


@pytest.mark.parametrize("cancel", [False, True])
def test_mapping_retry_recovers_unless_explicitly_ungrabbed(
    mocked_x11: X11Display, connection: MagicMock, cancel: bool
) -> None:
    from Xlib import X
    from Xlib.protocol.event import MappingNotify

    manager = HotkeyManager(X11HotkeyBackend(mocked_x11))
    states: list[tuple[HotkeyState, str]] = []
    manager.on_state = lambda state, reason: states.append((state, reason))
    assert manager.grab("Ctrl+space", HotkeyMode.TOGGLE).ok
    manager.handle_event(HotkeyEvent("KeyPress", 65, 1, mods=4), 0)
    notification = MappingNotify(request=X.MappingKeyboard, first_keycode=8, count=248)
    connection.refresh_keyboard_mapping.side_effect = [RuntimeError("неполная карта"), None]
    queue = [notification]
    connection.pending_events.side_effect = lambda: len(queue)
    connection.next_event.side_effect = lambda: queue.pop(0)
    manager.process_pending(1)
    assert states[-1][1] == "mapping-regrab:not-grabbed;escape:not-grabbed"
    if cancel:
        manager.ungrab()
    mocked_x11.root.grab_key.reset_mock()
    queue.append(notification)
    manager.process_pending(2)
    if cancel:
        mocked_x11.root.grab_key.assert_not_called()
    else:
        assert states[-1][1] == "mapping-regrab:ok;escape:ok"
        assert manager.last_result.keycode == 65
        assert manager.escape_result.keycode == 9
    manager.ungrab()


def test_mapping_with_unchanged_keycode_preserves_ptt_release(
    mocked_x11: X11Display, connection: MagicMock
) -> None:
    from Xlib import X
    from Xlib.protocol.event import MappingNotify

    manager = HotkeyManager(X11HotkeyBackend(mocked_x11))
    assert manager.grab("Ctrl+space", HotkeyMode.PTT).ok
    manager.handle_event(HotkeyEvent("KeyPress", 65, 1, mods=4), 0)
    queue = [MappingNotify(request=X.MappingModifier, first_keycode=0, count=0)]
    connection.pending_events.side_effect = lambda: len(queue)
    connection.next_event.side_effect = lambda: queue.pop(0)
    manager.process_pending(1)
    manager.handle_event(HotkeyEvent("KeyRelease", 65, 2), 1)
    assert manager.fsm.state == HotkeyState.PROCESSING
    manager.ungrab()


def test_wm_class_limits_depth_and_allows_boundary(mocked_x11: X11Display) -> None:
    windows = [MagicMock(id=index + 1) for index in range(10)]
    for index, window in enumerate(windows):
        window.get_wm_class.return_value = None
        window.query_tree.return_value.children = windows[index + 1 : index + 2]
    mocked_x11.d.create_resource_object.return_value = windows[0]
    windows[9].get_wm_class.return_value = ("too", "deep")
    assert mocked_x11.wm_class(1) is None
    windows[9].get_wm_class.assert_not_called()
    windows[8].query_tree.assert_not_called()
    windows[8].get_wm_class.return_value = ("Astra", "Voice")
    assert mocked_x11.wm_class(1) == ("astra", "voice")


def test_wm_class_limits_nodes_and_cycles(mocked_x11: X11Display) -> None:
    root = MagicMock(id=1)
    root.get_wm_class.return_value = None
    mocked_x11.d.create_resource_object.return_value = root
    root.query_tree.return_value.children = [root]
    assert mocked_x11.wm_class(1) is None
    root.get_wm_class.assert_called_once_with()
    children = [MagicMock(id=index + 2) for index in range(256)]
    root.query_tree.return_value.children = children
    assert mocked_x11.wm_class(1) is None
    assert all(not child.get_wm_class.called for child in children)


@pytest.mark.parametrize("property_type", [33, 6], ids=["WINDOW", "CARDINAL"])
def test_active_window_accepts_cardinal_typed_property(
    mocked_x11: X11Display, connection: MagicMock, property_type: int
) -> None:
    """fly-wm (наследник qvwm) пишет списки окон типом CARDINAL: важен только формат 32."""
    from Xlib import X

    root = connection.screen.return_value.root
    connection.intern_atom.return_value = 77
    root.get_full_property.return_value = SimpleNamespace(
        property_type=property_type, format=32, value=[91, 0]
    )
    assert mocked_x11.active_window() == 91
    root.get_full_property.assert_called_with(77, X.AnyPropertyType)
    root.get_full_property.return_value = SimpleNamespace(
        property_type=property_type, format=16, value=[91]
    )
    assert mocked_x11.active_window() is None
