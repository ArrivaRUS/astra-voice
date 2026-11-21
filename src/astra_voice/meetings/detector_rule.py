"""Call timing and offer continuity over the metadata observer's batches.

Автомат даёт подсказку по последней применённой картине. Устаревший кандидат
отсеивается свежей проверкой OFFER (шаг 2б), которая сама снимает картину потоков.
Точность таймеров — 1 с (ревизия 19.11, решение владельца (`decisions/log.md`)).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from astra_voice.worker.meeting_detector import (
    Direction,
    EventKind,
    NormalizedEvent,
    PictureTiming,
    ProgramKey,
)

T_ON_NS = 5_000_000_000
T_OFF_NS = 10_000_000_000


@dataclass(frozen=True)
class Candidate:
    program: ProgramKey
    offered: bool


@dataclass(frozen=True)
class CallEnded:
    program: ProgramKey


@dataclass(frozen=True)
class _Stream:
    program: ProgramKey
    direction: Direction
    active: bool


@dataclass
class _Program:
    on_deadline: int | None = None
    off_deadline: int | None = None
    ready: bool = False
    qualified: bool = False
    offered: bool = False


class MeetingDetectorRule:
    """Consume each complete start/poll batch from a single serialized owner.

    Pass source.picture_timing after each start/poll, including empty polls.
    For already confirmed input, event timestamps and the injected clock suffice.
    A successful RESET batch is a whole picture, possibly empty. RESET and
    UNAVAILABLE restart absence tracking from the next confirmed absence.
    """

    def __init__(self, *, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._available = True
        self._confirmed = True
        # The source's incarnation counter uniquely identifies a stream in its picture.
        self._streams: dict[int, _Stream] = {}
        self._programs: dict[ProgramKey, _Program] = {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(programs={len(self._programs)})"

    def update(
        self, events: tuple[NormalizedEvent, ...] = (), *, timing: PictureTiming | None = None
    ) -> tuple[Candidate | CallEnded, ...]:
        """Return ends and newly qualified candidates in the updated picture.

        A fresh active interval can qualify again with offered=True. The role
        can use that observation to track the call and suppress another offer.
        """
        if timing is not None:
            seconds = timing.at
        else:
            seconds = events[-1].at if events else self._clock()
        # Compare integer deadlines to avoid rounding past an exact boundary.
        now = round(seconds * 1_000_000_000)
        for event in events:
            self._apply(event)
        self._confirmed = self._available and (timing is None or timing.confirmed)
        if self._available:
            return self._evaluate(now)
        return ()

    def candidate(self, program: ProgramKey) -> Candidate | None:
        """Read current qualification and offer status after an update."""
        state = self._programs.get(program)
        if state is None or not state.ready:
            return None
        return Candidate(program, state.offered)

    def mark_offered(self, program: ProgramKey) -> bool:
        """Record an offer for a current candidate after the role's checks."""
        candidate = self.candidate(program)
        if candidate is None or candidate.offered:
            return False
        self._programs[program].offered = True
        return True

    def _apply(self, event: NormalizedEvent) -> None:
        if event.kind in (EventKind.RESET, EventKind.UNAVAILABLE):
            self._available = event.kind is EventKind.RESET
            self._streams.clear()
            for state in self._programs.values():
                state.on_deadline = None
                state.ready = False
                state.off_deadline = None
            return

        if not self._available or event.key is None:
            return
        incarnation = event.key.incarnation
        if event.kind is EventKind.REMOVE or event.program is None:
            self._streams.pop(incarnation, None)
        elif event.kind is EventKind.UPSERT:
            self._streams[incarnation] = _Stream(
                event.program,
                event.key.direction,
                event.corked is False
                and event.running is not False
                and (event.key.direction is Direction.MICROPHONE or event.muted is not True),
            )

    def _evaluate(self, now: int) -> tuple[Candidate | CallEnded, ...]:
        microphones: set[ProgramKey] = set()
        active_microphones: dict[ProgramKey, None] = {}
        active_outputs: set[ProgramKey] = set()
        for stream in self._streams.values():
            if stream.direction is Direction.MICROPHONE:
                microphones.add(stream.program)
                if stream.active:
                    active_microphones[stream.program] = None
            elif stream.active:
                active_outputs.add(stream.program)

        decisions: list[Candidate | CallEnded] = []
        for program, state in tuple(self._programs.items()):
            if program in microphones:
                state.off_deadline = None
            elif self._confirmed and state.qualified:
                if state.off_deadline is None:
                    state.off_deadline = now + T_OFF_NS
                elif now >= state.off_deadline:
                    del self._programs[program]
                    decisions.append(CallEnded(program))

        for program in active_microphones:
            if program in active_outputs and program not in self._programs:
                self._programs[program] = _Program()

        for program, state in tuple(self._programs.items()):
            if program not in active_microphones or program not in active_outputs:
                state.on_deadline = None
                state.ready = False
                if not state.qualified:
                    del self._programs[program]
            elif state.on_deadline is None:
                state.on_deadline = now + T_ON_NS
            elif not state.ready and now >= state.on_deadline:
                state.ready = state.qualified = True
                decisions.append(Candidate(program, state.offered))
        return tuple(decisions)
