"""Автомат PTT/toggle и глобальный хоткей X11 без Qt и потоков.

Оркестрация читает ``fileno()`` через QSocketNotifier, вызывает
``process_pending()`` и ставит свой таймер на ``fsm.limit_deadline``.
Время автомата — монотонные секунды; время X-события нужно только для
фильтра автоповтора. Логика перенесена из S4 ``PttMachine``.
"""

from __future__ import annotations

import logging
import select
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol

from astra_voice.core.constants import RECORD_LIMIT_S as RECORD_LIMIT_S
from astra_voice.platform.x11 import BadCombo, ParsedCombo, X11Display, X11Unavailable

log = logging.getLogger(__name__)

PTT_THRESHOLD_S = 0.3
LOOKAHEAD_S = 0.002
DEFAULT_CANDIDATES = ("Ctrl+Shift+Space", "Ctrl+Alt+D")
BUSY_MESSAGE = "Комбинация занята другой программой (возможно Handy или переключатель ввода)"
ResultCode = Literal["ok", "busy", "bad-combo", "duplicate", "not-grabbed"]


class HotkeyMode(Enum):
    PTT = "ptt"
    TOGGLE = "toggle"


class HotkeyState(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"


@dataclass(frozen=True)
class GrabResult:
    """Код операции; подсказка владельца только при BadAccess."""

    code: ResultCode
    owner_hint: str | None = None
    keycode: int | None = None
    mods: int = 0

    @property
    def ok(self) -> bool:
        return self.code == "ok"


@dataclass(frozen=True)
class ProbeResult(GrabResult):
    """Результат кратковременного захвата; успешный захват уже снят."""


@dataclass(frozen=True)
class HotkeyEvent:
    """Клавишное событие: time — исходная метка сервера, не секунды."""

    kind: Literal["KeyPress", "KeyRelease"]
    keycode: int
    time: int
    escape: bool = False
    mods: int = 0


@dataclass(frozen=True)
class MappingEvent:
    """Результаты перезахвата в порядке очереди, перед клавишами новой карты."""

    combos: dict[str, GrabResult]
    escape: GrabResult | None


class HotkeyFsm:
    """Чистый автомат; ровно 0,3 с считается удержанием, как в S4."""

    def __init__(
        self,
        mode: HotkeyMode = HotkeyMode.PTT,
        on_state: Callable[[HotkeyState, str], None] | None = None,
    ) -> None:
        self.state = HotkeyState.IDLE
        self.mode = mode
        self.on_state = on_state
        self._configured_mode = mode
        self._press_ts = 0.0
        self._limit_deadline: float | None = None

    @property
    def limit_deadline(self) -> float | None:
        return self._limit_deadline

    def _emit(self, reason: str, now: float) -> None:
        if self.on_state is not None:
            self.on_state(self.state, reason)

    def press(self, now: float) -> None:
        if self.state == HotkeyState.IDLE:
            self._press_ts = now
            self.mode = self._configured_mode
            self.state = HotkeyState.RECORDING
            self._limit_deadline = now + RECORD_LIMIT_S
            self._emit("press", now)
        elif self.state == HotkeyState.RECORDING and self.mode == HotkeyMode.TOGGLE:
            self.stop(now, "toggle-off")

    def release(self, now: float) -> None:
        if self.state != HotkeyState.RECORDING or self.mode == HotkeyMode.TOGGLE:
            return
        held = now - self._press_ts
        if held < PTT_THRESHOLD_S:
            self.mode = HotkeyMode.TOGGLE
            self._emit(f"tap→toggle ({held:.3f} с)", now)
        else:
            self.stop(now, f"release ({held:.3f} с)")

    def stop(self, now: float, reason: str) -> None:
        if self.state == HotkeyState.RECORDING:
            self.state = HotkeyState.PROCESSING
            self._limit_deadline = None
            self._emit(reason, now)

    def escape(self, now: float) -> None:
        if self.state != HotkeyState.IDLE:
            self.state = HotkeyState.IDLE
            self.mode = self._configured_mode
            self._limit_deadline = None
            self._emit("escape-cancel", now)

    def done(self, now: float) -> None:
        if self.state == HotkeyState.PROCESSING:
            self.state = HotkeyState.IDLE
            self.mode = self._configured_mode
            self._emit("done", now)

    def tick(self, now: float) -> None:
        if self._limit_deadline is not None and now >= self._limit_deadline:
            self.stop(now, "limit")


class HotkeyBackend(Protocol):
    """Захваты и очередь клавиш; ожидание пары автоповтора ограничено S4."""

    def grab_combo(self, combo: str) -> GrabResult: ...
    def ungrab_combo(self, combo: str) -> GrabResult: ...
    def grab_escape(self) -> GrabResult: ...
    def ungrab_escape(self) -> None: ...
    def poll_events(self, timeout: float = 0.0) -> Sequence[HotkeyEvent | MappingEvent]: ...
    def fileno(self) -> int: ...


class X11HotkeyBackend:
    """Ленивое соединение X11; разбор, маски и откат захватов — в x11.py."""

    def __init__(self, display: X11Display | None = None) -> None:
        self._x = display if display is not None else X11Display()
        self._combos: dict[str, ParsedCombo] = {}
        self._requested_combos: dict[str, None] = {}
        self._escape: ParsedCombo | None = None
        self._escape_requested = False

    def grab_combo(self, combo: str) -> GrabResult:
        if not self._x.open():
            return GrabResult("not-grabbed")
        try:
            parsed = self._x.parse_combo(combo)
        except BadCombo:
            return GrabResult("bad-combo")
        except X11Unavailable:
            return GrabResult("not-grabbed")
        grabbed = list(self._combos.values())
        if self._escape is not None:
            grabbed.append(self._escape)
        if any((p.mods, p.keycode) == (parsed.mods, parsed.keycode) for p in grabbed):
            return GrabResult("duplicate")
        result = self._grab(parsed)
        if result.ok:
            self._combos[combo] = parsed
            self._requested_combos[combo] = None
        return result

    def _grab(self, parsed: ParsedCombo) -> GrabResult:
        report = self._x.grab_key(parsed.keycode, parsed.mods)
        if report.ok:
            return GrabResult("ok", keycode=parsed.keycode, mods=parsed.mods)
        if report.bad_access:
            return GrabResult("busy", owner_hint=BUSY_MESSAGE)
        log.debug("захват X11 не выполнен: %s", report.per_mask)
        return GrabResult("not-grabbed")

    def ungrab_combo(self, combo: str) -> GrabResult:
        self._requested_combos.pop(combo, None)
        parsed = self._combos.pop(combo, None)
        if parsed is None:
            return GrabResult("not-grabbed")
        self._x.ungrab_key(parsed.keycode, parsed.mods)
        return GrabResult("ok")

    def grab_escape(self) -> GrabResult:
        if self._escape is not None:
            return GrabResult("duplicate")
        if not self._x.open():
            return GrabResult("not-grabbed")
        try:
            parsed = self._x.parse_combo("Escape")
        except BadCombo:
            return GrabResult("bad-combo")
        except X11Unavailable:
            return GrabResult("not-grabbed")
        # Escape может быть самим хоткеем: его основной захват не снимаем.
        if any((p.mods, p.keycode) == (parsed.mods, parsed.keycode) for p in self._combos.values()):
            self._escape = parsed
            self._escape_requested = True
            return GrabResult("ok", keycode=parsed.keycode)
        result = self._grab(parsed)
        if result.ok:
            self._escape = parsed
            self._escape_requested = True
        return result

    def ungrab_escape(self) -> None:
        self._escape_requested = False
        parsed, self._escape = self._escape, None
        if parsed is not None and not any(
            (p.mods, p.keycode) == (parsed.mods, parsed.keycode) for p in self._combos.values()
        ):
            self._x.ungrab_key(parsed.keycode, parsed.mods)

    def _refresh_mapping(self, event: Any) -> MappingEvent:
        """Снимает старые маски до обновления карты, затем захватывает новые."""
        combos = list(self._requested_combos)
        escape = self._escape_requested
        self.ungrab_escape()
        for combo in combos:
            self.ungrab_combo(combo)
        if not self._x.refresh_keyboard_mapping(event):
            failed = GrabResult("not-grabbed")
            results = dict.fromkeys(combos, failed)
            escape_result = failed if escape else None
        else:
            results = {combo: self.grab_combo(combo) for combo in combos}
            escape_result = self.grab_escape() if escape else None
        # Промежуточная карта может быть неполной; следующая нотификация повторит попытку.
        self._requested_combos = dict.fromkeys(combos)
        self._escape_requested = escape
        return MappingEvent(results, escape_result)

    def poll_events(self, timeout: float = 0.0) -> list[HotkeyEvent | MappingEvent]:
        pending = self._x.pending_events()
        if self._x.d is None:
            return []
        from Xlib import X

        events: list[HotkeyEvent | MappingEvent] = []
        if timeout > 0 and not pending:
            select.select([self.fileno()], [], [], timeout)
        while self._x.pending_events():
            event = self._x.next_event()
            # MappingNotify приходит всем клиентам без маски подписки XSelectInput.
            if event.type == X.MappingNotify:
                if event.request in (X.MappingKeyboard, X.MappingModifier):
                    events.append(self._refresh_mapping(event))
            elif event.type in (X.KeyPress, X.KeyRelease):
                events.append(
                    HotkeyEvent(
                        "KeyPress" if event.type == X.KeyPress else "KeyRelease",
                        int(event.detail),
                        int(event.time),
                        self._escape is not None and event.detail == self._escape.keycode,
                        self._x.semantic_modifiers(int(event.state)),
                    )
                )
        return events

    def fileno(self) -> int:
        """Дескриптор для внешнего наблюдателя; без соединения — минус один."""
        return self._x.fileno()

    def close(self) -> None:
        self.ungrab_escape()
        for combo in list(self._requested_combos):
            self.ungrab_combo(combo)
        self._x.close()


class EvdevHotkeyBackend:
    """P2"""

    def __init__(self) -> None:
        raise NotImplementedError


class HotkeyManager:
    """Связка автомата и бэкенда; колбэки клавиш вызываются без аргументов.

    Escape захватывается при RECORDING и снимается в IDLE, как в S4:
    это сохраняет отмену во время PROCESSING. ``ungrab()`` возвращает None;
    код последней операции, включая ``not-grabbed``, доступен в ``last_result``.
    После смены карты ``on_state`` получает причину ``mapping-regrab:<код>``
    и результат Escape, если он был захвачен; состояние автомата сохраняется.
    """

    def __init__(
        self,
        backend: HotkeyBackend | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend if backend is not None else X11HotkeyBackend()
        self._clock = clock
        self.on_press: Callable[[], None] | None = None
        self.on_release: Callable[[], None] | None = None
        self.on_escape: Callable[[], None] | None = None
        self.on_state: Callable[[HotkeyState, str], None] | None = None
        self.fsm = HotkeyFsm(on_state=self._on_state)
        self.last_result = GrabResult("not-grabbed")
        self.escape_result = GrabResult("not-grabbed")
        self._combo: str | None = None
        self._keycode: int | None = None
        self._mods = 0
        self._escape_grabbed = False
        self._escape_keycode: int | None = None
        self._key_down = False

    def grab(self, combo: str, mode: HotkeyMode) -> GrabResult:
        if combo == self._combo:
            self.last_result = GrabResult("duplicate")
            return self.last_result
        result = self.backend.grab_combo(combo)
        if result.ok:
            if self._combo is not None:
                self.ungrab()
            self._combo = combo
            self._keycode = result.keycode
            self._mods = result.mods
            self.fsm = HotkeyFsm(mode, self._on_state)
        self.last_result = result
        return result

    def ungrab(self) -> None:
        if self._combo is None:
            self.last_result = GrabResult("not-grabbed")
            return
        self.fsm.escape(self._clock())
        self.last_result = self.backend.ungrab_combo(self._combo)
        self._combo = None
        self._keycode = None
        self._key_down = False
        # События старого захвата не должны запустить следующую запись.
        self.backend.poll_events()

    def probe(self, combo: str) -> ProbeResult:
        if combo == self._combo:
            return ProbeResult("duplicate")
        result = self.backend.grab_combo(combo)
        if result.ok:
            self.backend.ungrab_combo(combo)
        return ProbeResult(result.code, result.owner_hint, result.keycode, result.mods)

    def free_candidates(self, prefer: list[str]) -> list[str]:
        return [combo for combo in prefer if self.probe(combo).ok]

    def fileno(self) -> int:
        return self.backend.fileno()

    def _on_state(self, state: HotkeyState, reason: str) -> None:
        if state == HotkeyState.RECORDING and not self._escape_grabbed:
            self.escape_result = self.backend.grab_escape()
            self._escape_grabbed = self.escape_result.ok
            self._escape_keycode = self.escape_result.keycode
        elif state == HotkeyState.IDLE:
            self.backend.ungrab_escape()
            self._escape_grabbed = False
            self._escape_keycode = None
        if self.on_state is not None:
            self.on_state(state, reason)

    def handle_event(self, event: HotkeyEvent, now: float) -> GrabResult:
        """Передаёт одну клавишу автомату; пары автоповтора убирает очередь."""
        if self._combo is None:
            self.last_result = GrabResult("not-grabbed")
            return self.last_result
        callback: Callable[[], None] | None = None
        matches_combo = event.keycode == self._keycode and event.mods == self._mods
        if (
            not matches_combo
            and (event.escape or event.keycode == self._escape_keycode)
            and self._escape_grabbed
            and event.kind == "KeyPress"
        ):
            self.fsm.escape(now)
            callback = self.on_escape
        elif event.keycode == self._keycode:
            if event.kind == "KeyPress" and matches_combo and not self._key_down:
                self._key_down = True
                self.fsm.press(now)
                callback = self.on_press
            elif event.kind == "KeyRelease" and self._key_down:
                self._key_down = False
                self.fsm.release(now)
                callback = self.on_release
        if callback is not None:
            callback()
        self.last_result = GrabResult("ok")
        return self.last_result

    def _handle_mapping(self, event: MappingEvent) -> None:
        """Обновляет коды и сообщает наружу как успех, так и отказ перезахвата."""
        if self._combo is None or self._combo not in event.combos:
            return
        self.last_result = event.combos[self._combo]
        if self._keycode != self.last_result.keycode:
            self._key_down = False
        self._keycode = self.last_result.keycode
        self._mods = self.last_result.mods
        reason = f"mapping-regrab:{self.last_result.code}"
        if event.escape is not None:
            self.escape_result = event.escape
            self._escape_grabbed = event.escape.ok
            self._escape_keycode = event.escape.keycode
            reason += f";escape:{event.escape.code}"
        if self.on_state is not None:
            self.on_state(self.fsm.state, reason)

    def process_pending(self, now: float | None = None) -> GrabResult:
        """Разбирает очередь и убирает пары повтора, включая соседний read."""
        events = list(self.backend.poll_events())
        self.last_result = GrabResult("ok" if self._combo is not None else "not-grabbed")
        index = 0
        while index < len(events):
            event = events[index]
            if isinstance(event, MappingEvent):
                self._handle_mapping(event)
                index += 1
                continue
            if event.kind == "KeyRelease" and index + 1 == len(events):
                if event.keycode == self._keycode or event.escape:
                    events.extend(self.backend.poll_events(LOOKAHEAD_S))
            following = events[index + 1] if index + 1 < len(events) else None
            if (
                event.kind == "KeyRelease"
                and isinstance(following, HotkeyEvent)
                and following.kind == "KeyPress"
                and event.keycode == following.keycode
                and event.time == following.time
                and event.mods == following.mods
            ):
                index += 2
                continue
            self.handle_event(event, self._clock() if now is None else now)
            index += 1
        return self.last_result
