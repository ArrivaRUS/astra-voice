"""On-demand meeting checks and private call instances, owned by the capture loop."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from astra_voice.meetings.events import DetectorCode, DetectorPurpose
from astra_voice.meetings.identity import Reason
from astra_voice.worker.meeting_audio_local import CONNECT_LOCK

if TYPE_CHECKING:
    from astra_voice.worker.meeting_capture import MeetingCaptureState, Message
    from astra_voice.worker.meeting_detector import ProgramKey
    from astra_voice.worker.meeting_identity_check import CallAnchor, CheckRequest, CheckResult
    from astra_voice.worker.meeting_metadata import PulseMetadata
    from astra_voice.worker.meeting_observer import MeetingObserver

MAX_CALLS = 8
OFFER_ATTEMPTS = 3
OFFER_INTERVAL = 1.0

_UNSETTLED = {
    "picture_changed": DetectorCode.PICTURE_CHANGED,
    "pulse_failed": DetectorCode.PULSE_FAILED,
    "budget": DetectorCode.BUDGET,
    "busy": DetectorCode.BUSY,
    "rate": DetectorCode.RATE,
    "remote": DetectorCode.REMOTE,
}
_VERIFICATION_FAILED = {
    "verification=failed",
    Reason.SS_TIMEOUT,
    Reason.SS_LIMIT,
    Reason.SS_INSUFFICIENT,
}
_VERIFICATION_UNAVAILABLE = {"verification=unavailable", Reason.SS_UNAVAILABLE}
_MISMATCHES = {
    Reason.PID,
    Reason.DEAD,
    Reason.UID,
    Reason.EXE,
    Reason.BINARY,
    Reason.REUSED,
    Reason.CONFLICT,
    Reason.NO_CONNECTION,
    Reason.UNAVAILABLE,
}
_RETRY = {"picture_changed", "pulse_failed", "busy", "budget"}


def result_code(result: CheckResult) -> DetectorCode | None:
    if result.unsettled is not None and result.unsettled in _UNSETTLED:
        return _UNSETTLED[result.unsettled]
    journal = set(result.journal)
    if journal & _VERIFICATION_FAILED:
        return DetectorCode.VERIFICATION_FAILED
    if journal & _VERIFICATION_UNAVAILABLE:
        return DetectorCode.VERIFICATION_UNAVAILABLE
    if journal & _MISMATCHES:
        return DetectorCode.IDENTITY_MISMATCH
    if Reason.COUNTER in journal:
        return DetectorCode.IDENTITY_MISMATCH_COUNTER
    return None if result.offer else DetectorCode.NO_CALL


class Checker(Protocol):
    def run(self, request: CheckRequest) -> CheckResult: ...
    def shutdown(self, deadline: float) -> bool: ...


CheckFactory = Callable[["MeetingObserver", Callable[[], float]], Checker]


def role_checker(observer: MeetingObserver, clock: Callable[[], float]) -> Checker:
    from astra_voice.meetings.apps import AppDataError
    from astra_voice.worker import meeting_identity_check as check

    assert observer.address is not None
    # Use the checker's loader binding so the role and the measurement share one catalog.
    apps = check.load_apps()  # type: ignore[attr-defined]
    if not apps:
        raise AppDataError("empty meeting catalog")
    return check.IdentityCheck(
        server=observer.address.address,
        apps=apps,
        own_contexts=observer.registry.snapshot,
        clock=clock,
        metadata=_check_metadata,
    )


def _check_metadata(**kwargs: Any) -> PulseMetadata:
    from astra_voice.worker.meeting_metadata import PulseMetadata
    from astra_voice.worker.pulse_stream import _PulseAsync

    class CheckPulse(_PulseAsync):
        def context_new(self, api: int, name: bytes) -> int | None:
            with CONNECT_LOCK:
                return super().context_new(api, name)

        def context_connect(self, ctx: int, server: bytes | None, flags: int, spawn: None) -> int:
            with CONNECT_LOCK:
                return super().context_connect(ctx, server, flags, spawn)

    return PulseMetadata(pulse=CheckPulse(), **kwargs)


@dataclass(frozen=True, repr=False)
class _CallInstance:
    anchor: CallAnchor
    program: ProgramKey
    cookie: int
    created_at: float


@dataclass(repr=False)
class _Offer:
    program: ProgramKey
    epoch: int
    attempts: int = 0
    retry_at: float | None = None


@dataclass(frozen=True, repr=False)
class _Attempt:
    purpose: DetectorPurpose
    epoch: int
    started_at: float
    server_generation: int
    request: Message | None = None
    offer: _Offer | None = None
    blocked_epoch: int = 0
    end_revisions: dict[ProgramKey, int] = field(default_factory=dict, compare=False)
    prepare_revision: int = 0


@dataclass(frozen=True)
class _MeasuredServer:
    cookie: int = field(repr=False)


class MeetingCallService:
    def __init__(self, role: MeetingCaptureState, factory: CheckFactory) -> None:
        self._role = role
        self._observer = role.observer
        self._clock = role.clock
        self._factory = factory
        self._checker: Checker | None = None
        self._shutdown_complete = False
        self._instances: dict[str, _CallInstance] = {}
        self._instances_epoch = self._observer.epoch
        self._offers: dict[ProgramKey, _Offer] = {}
        self._cards: dict[str, Message] = {}
        self._server_cookie: int | None = None
        self._server_generation = 0
        self._automation_disabled = False
        self._last_code: dict[tuple[DetectorPurpose, DetectorCode], float] = {}

    def anchor_for(self, call_id: str) -> CallAnchor | None:
        if (
            self._instances_epoch != self._observer.epoch
            or self._observer.automation_off.is_set()
            or self._role.terminating
            or self._role.stopped
        ):
            return None
        instance = self._instances.get(call_id)
        return instance.anchor if instance is not None else None

    def query(self, request: Message, arrived_at: float) -> list[Message]:
        role = self._role
        remote = self._observer.automation_off.is_set() or self._observer.address is None
        if remote or role.terminating:
            if remote:
                role.messages.put({"type": "card-remote"})
            inactive = role._reply("meeting.call.state", request, active=False)
            return role._remember(request, [inactive])
        reply = role._reply("meeting.ack", request)
        req = request["req"]
        role.pending[req] = [reply]
        self._cards[req] = request
        with self._observer.rule_lock:
            end_revisions = self._observer.end_revisions()
        attempt = _Attempt(
            DetectorPurpose.CARD,
            self._observer.epoch,
            arrived_at,
            self._server_generation,
            request=request,
            end_revisions=end_revisions,
        )
        self._launch(attempt)
        return role._remember(request, [reply])

    def _launch(self, attempt: _Attempt) -> None:
        try:
            from astra_voice.worker.meeting_identity_check import (
                CHECK_SECONDS,
                CheckRequest,
                Purpose,
            )

            checker = self._get_checker()
            request = CheckRequest(
                Purpose(attempt.purpose),
                attempt.offer.program if attempt.offer is not None else None,
                None,
                attempt.started_at + CHECK_SECONDS,
            )
            threading.Thread(
                target=self._run_check,
                args=(checker, request, attempt),
                name="meeting-check",
                daemon=True,
            ).start()
        except Exception:
            # Factory/thread creation failures get a final outcome, with no automatic retry.
            self._role.messages.put({"type": "check-result", "attempt": attempt, "result": None})

    def _get_checker(self) -> Checker:
        if self._checker is None:
            self._checker = self._factory(self._observer, self._clock)
        return self._checker

    def start_checker(self) -> Checker | None:
        """Get the shared checker on the control loop before launching a measurement."""
        try:
            return self._get_checker()
        except Exception:
            return None

    def run_start(self, checker: Checker | None, request: CheckRequest) -> CheckResult:
        """Run a check on its worker thread, retaining the private START target."""
        return self._check(checker, request)

    @staticmethod
    def _check(checker: Checker | None, request: CheckRequest) -> CheckResult:
        from astra_voice.meetings.apps import AppClass
        from astra_voice.worker.meeting_identity_check import CheckResult, Unsettled

        if checker is not None:
            try:
                return checker.run(request)
            except Exception:
                pass
        return CheckResult(
            settled=False,
            unsettled=Unsettled.PULSE_FAILED,
            app_class=AppClass.OTHER,
            offer=False,
            variant1=False,
            journal=(),
            measured_mono_ms=0,
            attempts=0,
            anchor=None,
            target=None,
        )

    def _run_check(self, checker: Checker, request: CheckRequest, attempt: _Attempt) -> None:
        from astra_voice.worker.meeting_identity_check import Unsettled

        result = self._check(checker, request)
        if self._role.terminating or self._role.stopped:
            return
        event: Message = {"type": "check-result", "attempt": attempt}
        if attempt.request is not None and result.offer and (anchor := result.anchor) is not None:
            event["measured_server"] = _MeasuredServer(anchor.server_cookie)
        if attempt.epoch != self._observer.epoch:
            if attempt.offer is not None and result.unsettled != Unsettled.REMOTE:
                return
            result = replace(result, anchor=None, target=None)
        else:
            result = replace(result, target=None)
        event["result"] = result
        self._role.messages.put(event)

    def _can_offer(self, offer: _Offer) -> bool:
        role = self._role
        return (
            offer.epoch == self._observer.epoch
            and not role.terminating
            and not role.stopped
            and not self._observer.automation_off.is_set()
            and not (
                role.preparing
                or role.identity
                or role.opening
                or role.sources
                or role.finalizing
                or role.close_deadline is not None
            )
        )

    def _start_offer(self, offer: _Offer, now: float) -> None:
        if self._can_offer(offer):
            with self._observer.rule_lock:
                candidate = self._observer.rule.candidate(offer.program)
                end_revisions = self._observer.end_revisions()
            if candidate is not None and not candidate.offered:
                offer.attempts += 1
                offer.retry_at = None
                self._offers[offer.program] = offer
                self._launch(
                    _Attempt(
                        DetectorPurpose.OFFER,
                        offer.epoch,
                        now,
                        self._server_generation,
                        offer=offer,
                        blocked_epoch=self._role.session.snapshot().blocked_epoch,
                        end_revisions=end_revisions,
                        prepare_revision=self._role.prepare_starts,
                    )
                )
                return
        self._offers.pop(offer.program, None)

    def observer_event(self, event: Message, output: list[Message]) -> None:
        if event["epoch"] != self._observer.epoch:
            return
        kind = event["type"]
        if kind == "detector-call-ended":
            program = event["program"]
            self._offers.pop(program, None)
            self._end_calls("mic_released", output, program)
        elif not event["offered"]:
            program = event["program"]
            previous = self._offers.get(program)
            if previous is None or previous.retry_at is not None:
                self._start_offer(_Offer(program, event["epoch"]), self._clock())

    def retry_offers(self) -> None:
        for offer in tuple(self._offers.values()):
            if offer.retry_at is None:
                continue
            now = self._clock()
            if not self._can_offer(offer):
                del self._offers[offer.program]
            elif now >= offer.retry_at:
                self._start_offer(offer, now)
            else:
                with self._observer.rule_lock:
                    candidate = self._observer.rule.candidate(offer.program)
                if candidate is None or candidate.offered:
                    del self._offers[offer.program]

    def cancel_offer_retries(self) -> None:
        for program, offer in tuple(self._offers.items()):
            if offer.retry_at is not None:
                del self._offers[program]

    def _end_calls(
        self, reason: str, output: list[Message], program: ProgramKey | None = None
    ) -> None:
        for call_id, instance in tuple(self._instances.items()):
            if program is None or instance.program == program:
                self._end_call(call_id, reason, output)

    def _end_call(self, call_id: str, reason: str, output: list[Message]) -> None:
        del self._instances[call_id]
        output.append({"type": "meeting.call.ended", "call_id": call_id, "reason": reason})

    def resolve_server(
        self, observer_events: list[Message], checks: list[Message], output: list[Message]
    ) -> bool:
        if self._role.terminating or self._role.stopped:
            return False
        epoch = self._observer.epoch
        cookies = set()
        if self._server_cookie is not None:
            cookies.add(self._server_cookie)
        observed_cookie = None
        for event in observer_events:
            if event["epoch"] == epoch:
                observed_cookie = event["server"].cookie
                cookies.add(observed_cookie)

        if not self._observer.automation_off.is_set():
            for event in checks:
                attempt: _Attempt = event["attempt"]
                result: CheckResult | None = event["result"]
                # Earlier generations cannot supply evidence for another reset.
                if (
                    attempt.epoch == epoch
                    and attempt.server_generation == self._server_generation
                    and result is not None
                    and result.anchor is not None
                ):
                    cookies.add(result.anchor.server_cookie)

        if len(cookies) > 1:
            # Invalidate this whole batch before finish() publishes any results.
            self._end_calls("server_changed", output)
            self._server_generation += 1
            self._server_cookie = observed_cookie
            return True
        elif cookies:
            self._server_cookie = cookies.pop()
        return False

    def _identify(self, anchor: CallAnchor, output: list[Message]) -> str:
        for call_id, instance in self._instances.items():
            if instance.anchor == anchor:
                return call_id
        if len(self._instances) >= MAX_CALLS:
            self._end_call(next(iter(self._instances)), "evicted", output)
        call_id = uuid.uuid4().hex
        self._instances_epoch = self._observer.epoch
        self._instances[call_id] = _CallInstance(
            anchor, anchor.program, anchor.server_cookie, self._clock()
        )
        return call_id

    @staticmethod
    def _public_call(result: CheckResult, call_id: str) -> Message:
        return {
            "call_id": call_id,
            "app_class": result.app_class,
            "variant1": result.variant1,
            "measured_mono_ms": result.measured_mono_ms,
        }

    def _emit_code(
        self,
        purpose: DetectorPurpose,
        code: DetectorCode | None,
        duration_ms: int,
        now: float,
        output: list[Message],
    ) -> None:
        if code is None:
            return
        key = (purpose, code)
        if now - self._last_code.get(key, float("-inf")) < 1.0:
            return
        self._last_code[key] = now
        output.append(
            {
                "type": "meeting.detector.code",
                "purpose": purpose,
                "code": code,
                "duration_ms": duration_ms,
            }
        )

    def remote_card(self, output: list[Message]) -> None:
        self._emit_code(DetectorPurpose.CARD, DetectorCode.REMOTE, 0, self._clock(), output)

    def finish_start(self, code: DetectorCode, duration_ms: int, output: list[Message]) -> None:
        if code == DetectorCode.REMOTE:
            self._disable_automation()
        self._emit_code(DetectorPurpose.START, code, duration_ms, self._clock(), output)
        self.sync_automation(output)

    def _disable_automation(self) -> None:
        observer = self._observer
        if not observer.automation_off.is_set():
            observer.automation_off.set()
            observer.stop()

    def sync_automation(self, output: list[Message]) -> None:
        if self._observer.automation_off.is_set() and not self._automation_disabled:
            self._automation_disabled = True
            self._offers.clear()
            self._end_calls("detector_off", output)

    def note_result(self, event: Message) -> None:
        role, observer = self._role, self._observer
        result = event["result"]
        if (
            not (role.terminating or role.stopped)
            and result is not None
            and result.unsettled == "remote"
            and not observer.automation_off.is_set()
        ):
            # Preserve the diagnostic of the OFFER that stops the observer.
            attempt = event["attempt"]
            event["report_remote_offer"] = (
                attempt.offer is not None and attempt.epoch == observer.epoch
            )
            self._disable_automation()

    def finish(
        self,
        attempt: _Attempt,
        result: CheckResult | None,
        output: list[Message],
        *,
        report_remote_offer: bool = False,
        measured_server: _MeasuredServer | None = None,
    ) -> None:
        role, observer = self._role, self._observer
        if role.terminating or role.stopped:
            return
        current_epoch = attempt.epoch == observer.epoch
        offer, request = attempt.offer, attempt.request
        if offer is not None and not current_epoch and not report_remote_offer:
            return
        if request is not None and self._cards.pop(request["req"], None) is None:
            return

        now = self._clock()
        code = result_code(result) if result is not None else DetectorCode.PULSE_FAILED
        self.sync_automation(output)
        duration = 0 if result is None else max(0, int((now - attempt.started_at) * 1000))
        self._emit_code(attempt.purpose, code, duration, now, output)

        if request is not None:
            public: Message = {"active": False}
            if result is not None and result.offer and not observer.automation_off.is_set():
                # M8 completes CARD across detector.stop with an unregistered ID.
                # Step 5 checks session admission and capture preparation for CARD.
                call_id = None
                if not current_epoch:
                    measured_cookie = (
                        measured_server.cookie if measured_server is not None else None
                    )
                    if attempt.server_generation == self._server_generation and (
                        self._server_cookie is None or self._server_cookie == measured_cookie
                    ):
                        call_id = uuid.uuid4().hex
                else:
                    anchor = result.anchor
                    with observer.rule_lock:
                        if self._fresh(attempt, anchor):
                            assert anchor is not None
                            call_id = self._identify(anchor, output)
                if call_id is not None:
                    public = {"active": True, **self._public_call(result, call_id)}
            self.finish_card(request, public, output)
        elif offer is not None and current_epoch and self._offers.get(offer.program) is offer:
            del self._offers[offer.program]
            if result is not None:
                self._finish_offer(attempt, offer, result, now, output)

    def _same_call(self, attempt: _Attempt, program: ProgramKey) -> bool:
        return self._observer.end_revision(program) == attempt.end_revisions.get(program, 0)

    def _fresh(self, attempt: _Attempt, anchor: CallAnchor | None) -> bool:
        return (
            anchor is not None
            and self._same_call(attempt, anchor.program)
            and attempt.server_generation == self._server_generation
            and anchor.server_cookie == self._server_cookie
        )

    def _retry_offer(self, offer: _Offer, now: float) -> None:
        if offer.attempts < OFFER_ATTEMPTS:
            offer.retry_at = now + OFFER_INTERVAL
            self._offers[offer.program] = offer

    def _finish_offer(
        self,
        attempt: _Attempt,
        offer: _Offer,
        result: CheckResult,
        now: float,
        output: list[Message],
    ) -> None:
        observer = self._observer
        with observer.rule_lock:
            if not (
                self._can_offer(offer)
                and self._role.session.allowed(attempt.blocked_epoch)
                and attempt.prepare_revision == self._role.prepare_starts
                and self._same_call(attempt, offer.program)
            ):
                return
            if not result.offer:
                if result.unsettled in _RETRY:
                    self._retry_offer(offer, now)
                return
            anchor = result.anchor
            if not self._fresh(attempt, anchor):
                return
            assert anchor is not None
            if observer.rule.mark_offered(offer.program):
                call_id = self._identify(anchor, output)
                output.append(
                    {"type": "meeting.call.started", **self._public_call(result, call_id)}
                )

    def finish_card(self, request: Message, public: Message, output: list[Message]) -> None:
        output.append(self._role._outcome(request, {"type": "meeting.call.state", **public}))

    def detector_stopped(self, output: list[Message]) -> None:
        self._offers.clear()
        self._end_calls("detector_off", output)

    def terminate(self) -> None:
        self._role.messages.put({"type": "detector-clear"})
        for request in self._cards.values():
            self._role.messages.put({"type": "card-cancelled", "request": request})
        self._cards.clear()

    def shutdown(self, deadline: float) -> bool:
        if self._checker is None or self._shutdown_complete:
            return True
        try:
            self._shutdown_complete = self._checker.shutdown(deadline)
        except Exception:
            return False
        return self._shutdown_complete

    def cleanup(self, deadline: float) -> None:
        while time.monotonic() < deadline and not self.shutdown(deadline):
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
