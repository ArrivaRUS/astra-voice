"""Автомат одной диктовки; ввод, вставка, индикаторы и таймеры внедряются извне.

Вложенный цикл вставки складывает входящие события в очередь. Отмена и смена
поколения запрещают дальнейшую вставку, включая отложенный повтор BUSY.
Содержимое речи не передаётся ни статистике, ни журналу, ни индикаторам.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from collections.abc import Callable
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

from astra_voice.platform.hotkey import HotkeyState
from astra_voice.platform.paste import PasteMode, PasteOutcome, PasteOutcomeKind
from astra_voice.ui.pill import (
    ERROR_BUFFER_CLEARED,
    ERROR_MICROPHONE_UNAVAILABLE,
    ERROR_RECOGNITION_FAILED,
    ERROR_RECOGNITION_RESTARTED,
    STATE_DURATION_MS,
    PillState,
)
from astra_voice.ui.tray_icons import TrayState

RECOGNIZE_TIMEOUT_S = 30.0
PROCESSING_WATCHDOG_MS = 35000
CANCEL_TIMEOUT_MS = 1500
BUSY_RETRY_MS = 200


class DictationPhase(Enum):
    """Фазы GUI; ожидание повтора вставки также относится к PASTING."""

    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"
    PASTING = "pasting"
    FINISHING = "finishing"


class PillPort(Protocol):
    """Индикатор получает только состояние, разрешённую причину и уровень."""

    def show_state(
        self, state: PillState, *, text: str | None = None, level: float | None = None
    ) -> None: ...

    def hide(self) -> None: ...


class TrayPort(Protocol):
    """Минимальный контракт трея без владения окном или приложением."""

    def set_state(self, state: TrayState, tooltip: str | None = None) -> None: ...

    def set_has_last_text(self, value: bool) -> None: ...


class StatsPort(Protocol):
    """Приёмник обезличенной статистики, совместимый с Stats.append."""

    def append(self, event_type: str, **fields: object) -> None: ...


def level_from_dbfs(dbfs: float) -> float:
    """Линейная шкала −60…0 dBFS → 0…1; края обрезаются, NaN означает тишину."""
    if math.isnan(dbfs):
        return 0.0
    return min(1.0, max(0.0, (dbfs + 60.0) / 60.0))


class DictationOrchestrator:
    """Обслуживает одну диктовку в вызывающем потоке, без собственного цикла.

    schedule должен откладывать вызов, а не выполнять его синхронно. Все его
    ручки принадлежат автомату. Пилюля сама скрывает завершающее состояние;
    таймер FINISHING лишь возвращает фазу и трей в IDLE.
    """

    def __init__(
        self,
        *,
        send: Callable[..., None],
        generation: Callable[[], int],
        pill: PillPort,
        tray: TrayPort,
        paste: Callable[[str, int | None, PasteMode], PasteOutcome],
        active_window: Callable[[], int | None],
        schedule: Callable[[int, Callable[[], None]], object],
        cancel_timer: Callable[[object], None],
        hotkey_done: Callable[[], None],
        set_recording: Callable[[bool], None],
        notify: Callable[[str, str], None],
        paste_mode: Callable[[], PasteMode],
        record_params: Callable[[], dict[str, Any]],
        clock: Callable[[], float] = time.monotonic,
        stats: StatsPort | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._send = send
        self._generation = generation
        self._pill = pill
        self._tray = tray
        self._paste = paste
        self._active_window = active_window
        self._schedule = schedule
        self._cancel_timer = cancel_timer
        self._hotkey_done = hotkey_done
        self._set_recording = set_recording
        self._notify = notify
        self._paste_mode = paste_mode
        self._record_params = record_params
        self._clock = clock
        self._stats = stats
        self._log = log if log is not None else logging.getLogger(__name__)
        self._phase = DictationPhase.IDLE
        self._target_window: int | None = None
        self._utterance_id = ""
        self._worker_generation = 0
        self._last_text: str | None = None
        self._started = False
        self._cold = True
        self._t0 = 0.0
        self._t_stop: float | None = None
        self._t_ms = 0.0
        self._paste_ms = 0.0
        self._retries = 0
        self._cancel_requested = False
        self._closed = False
        self._suspended = False
        self._queue: deque[Callable[[], None]] = deque()
        self._timer_serial = 0
        self._timers: dict[int, tuple[object, Callable[[], None]]] = {}

    @property
    def phase(self) -> DictationPhase:
        """Текущая фаза, доступная только для чтения."""
        return self._phase

    @property
    def last_text(self) -> str | None:
        """Последняя непустая фраза только в памяти процесса."""
        return self._last_text

    def on_hotkey_state(self, state: HotkeyState, reason: str) -> None:
        """Принимает состояния HotkeyFsm, включая IDLE с escape-cancel."""
        if self._closed:
            return
        if self._suspended:
            self._queue.append(lambda: self.on_hotkey_state(state, reason))
            return
        if state == HotkeyState.RECORDING:
            if self._phase in (DictationPhase.IDLE, DictationPhase.FINISHING):
                self._start()
        elif state == HotkeyState.PROCESSING:
            self._stop()
        elif state == HotkeyState.IDLE and reason == "escape-cancel":
            self.cancel("escape")

    def _start(self) -> None:
        self._target_window = self._active_window()
        self._utterance_id = uuid4().hex
        self._worker_generation = self._generation()
        self._cancel_timers()
        self._cancel_requested = False
        self._cold, self._started = not self._started, True
        self._t_stop = None
        self._t_ms = self._paste_ms = 0.0
        self._retries = 0
        self._change_phase(DictationPhase.RECORDING)
        params = self._record_params()
        message = {**params, "type": "record.start", "utterance_id": self._utterance_id}
        if not self._command(message, timeout=float(params["limit_s"]) + RECOGNIZE_TIMEOUT_S):
            return
        if self._phase != DictationPhase.RECORDING or self._cancel_requested:
            return
        self._pill.show_state(PillState.LISTENING)
        self._tray.set_state(TrayState.LISTENING)
        self._set_recording(True)
        self._t0 = self._clock()

    def _stop(self) -> None:
        if self._phase != DictationPhase.RECORDING or self._cancel_requested:
            return
        self._t_stop = self._clock()
        self._change_phase(DictationPhase.PROCESSING)
        if not self._command({"type": "record.stop", "utterance_id": self._utterance_id}):
            return
        if self.phase != DictationPhase.PROCESSING or self._cancel_requested:
            return
        if not self._command(
            {"type": "recognize", "utterance_id": self._utterance_id}, timeout=RECOGNIZE_TIMEOUT_S
        ):
            return
        if self.phase != DictationPhase.PROCESSING or self._cancel_requested:
            return
        self._set_recording(False)
        self._pill.show_state(PillState.PROCESSING)
        self._tray.set_state(TrayState.PROCESSING)
        self._later(PROCESSING_WATCHDOG_MS, self._watchdog)

    def on_worker_event(self, event: dict[str, Any]) -> None:
        """Отбрасывает чужие результаты до чтения содержимого распознанной речи."""
        if self._closed:
            return
        if self._suspended:
            saved = event.copy()
            self._queue.append(lambda: self.on_worker_event(saved))
            return
        if self._phase in (DictationPhase.IDLE, DictationPhase.FINISHING):
            return
        if self._generation() != self._worker_generation:
            self._fail_restarted()
            return
        generation = event.get("generation")
        if not isinstance(generation, int):
            return
        if generation < self._worker_generation:
            self._log.debug("диктовка: событие старого поколения отброшено")
            return
        if generation > self._worker_generation:
            self._fail_restarted()
            return
        if event.get("utterance_id", self._utterance_id) != self._utterance_id:
            return
        kind = event.get("type")
        if kind == "error":
            self._error(event.get("code"))
        elif kind == "cancelled":
            self._cancelled()
        elif self._cancel_requested:
            return
        elif kind == "result" and self._phase == DictationPhase.PROCESSING:
            self._result(event["text"])
        elif kind == "audio.ready":
            self._log.debug("диктовка: audio.ready")
        elif self._phase == DictationPhase.RECORDING:
            if kind == "level":
                self._pill.show_state(
                    PillState.LISTENING, level=level_from_dbfs(float(event["peak_dbfs"]))
                )
            elif kind == "silent":
                self._pill.show_state(PillState.LISTENING_SILENT)
            elif kind == "record.limit":
                self._pill.show_state(PillState.LIMIT)
                self._stop()

    def _result(self, text: str) -> None:
        received = self._clock()
        assert self._t_stop is not None
        self._t_ms = max(0.0, (received - self._t_stop) * 1000)
        self._cancel_timers()
        if not text.strip():
            self._dictation_stat("empty")
            self._finish(PillState.EMPTY)
            return
        self._change_phase(DictationPhase.PASTING)
        self._attempt_paste(text)

    def _attempt_paste(self, text: str) -> None:
        if self._closed or self._cancel_requested or self._phase != DictationPhase.PASTING:
            return
        if self._generation() != self._worker_generation:
            self._fail_restarted()
            return
        started = self._clock()
        self._suspended = True
        try:
            kind = self._paste(text, self._target_window, self._paste_mode()).kind
        except Exception:
            # Исключение может содержать фразу: не передаём даже его repr в журнал.
            self._log.warning("диктовка: ошибка вызова вставки")
            kind = PasteOutcomeKind.FAILED
        finally:
            self._paste_ms += max(0.0, (self._clock() - started) * 1000)
            self._suspended = False
        # Сохраняем PASTING при разборе: нажатия внутри paste не создают очередь
        # следующей диктовки, а отмена/крах не затираются успешным исходом вставки.
        while self._queue and not self._closed:
            self._queue.popleft()()
        if self._closed or self._cancel_requested or self._phase != DictationPhase.PASTING:
            return
        if self._generation() != self._worker_generation:
            self._fail_restarted()
            return
        if kind == PasteOutcomeKind.BUSY and self._retries == 0:
            self._retries += 1
            self._later(BUSY_RETRY_MS, lambda: self._attempt_paste(text))
            return
        self._last_text = text
        self._tray.set_has_last_text(True)
        if kind == PasteOutcomeKind.PASTED:
            self._dictation_stat("ok")
            self._finish(PillState.DONE, tray=TrayState.DONE)
        elif kind in (
            PasteOutcomeKind.CLIPBOARD_ONLY,
            PasteOutcomeKind.WINDOW_CHANGED,
            PasteOutcomeKind.BUSY,
        ):
            # зона B: отдельная подпись для window-changed
            self._dictation_stat("ok")
            self._finish(PillState.CLIPBOARD_ONLY)
        elif kind == PasteOutcomeKind.REFUSED_SECRET:
            self._dictation_stat("ok")
            self._fail_secret()
        else:
            self._fail_recognition()

    def cancel(self, source: str) -> None:
        """Единая отмена; результат после запроса больше не допускается к вставке."""
        if self._closed:
            return
        if self._suspended:
            self._queue.append(lambda: self.cancel(source))
            return
        if self._phase in (DictationPhase.IDLE, DictationPhase.FINISHING):
            return
        if self._cancel_requested:
            return
        self._cancel_requested = True
        if self._t_stop is None:
            self._t_stop = self._clock()
        self._cancel_timers()
        self._set_recording(False)
        self._tray.set_state(TrayState.IDLE)
        # Сторож ставится до send: синхронный cancelled тоже должен его отменить.
        self._later(CANCEL_TIMEOUT_MS, self._cancelled)
        try:
            self._send({"type": "record.cancel", "utterance_id": self._utterance_id})
        except Exception:
            self._log.warning("диктовка: отправка отмены не удалась, ожидается сторож")

    def on_indicators_lost(self) -> None:
        """IndicatorGuard сам уведомляет пользователя; здесь только отмена О4."""
        self.cancel("indicators")

    def _cancelled(self) -> None:
        self._cancel_requested = True
        if self._t_stop is None:
            self._t_stop = self._clock()
        self._dictation_stat("cancelled")
        self._finish(PillState.CANCELLED)

    def _error(self, code: object) -> None:
        if code in ("worker-crashed", "restart-limit", "worker-start"):
            self._fail_restarted()
        elif code in ("audio-no-device", "audio-busy", "audio-failed"):
            kind = {"audio-no-device": "none", "audio-busy": "busy"}.get(str(code), "other")
            self._append_stat("mic_error", kind=kind, recovered_by="none")
            self._fail_microphone()
        else:
            self._fail_recognition()

    def _watchdog(self) -> None:
        self._log.warning("диктовка: истёк сторож PROCESSING")
        self._fail_recognition()

    def _fail_restarted(self) -> None:
        self._begin_finish()
        self._pill.show_state(PillState.ERROR, text=ERROR_RECOGNITION_RESTARTED)
        self._end_finish(PillState.ERROR, tray=TrayState.ERROR)

    def _fail_microphone(self) -> None:
        self._begin_finish()
        self._pill.show_state(PillState.ERROR, text=ERROR_MICROPHONE_UNAVAILABLE)
        self._end_finish(PillState.ERROR, tray=TrayState.ERROR)

    def _fail_recognition(self) -> None:
        self._begin_finish()
        self._pill.show_state(PillState.ERROR, text=ERROR_RECOGNITION_FAILED)
        self._end_finish(PillState.ERROR, tray=TrayState.ERROR)

    def _fail_secret(self) -> None:
        self._begin_finish()
        self._pill.show_state(PillState.ERROR, text=ERROR_BUFFER_CLEARED)
        self._end_finish(PillState.ERROR, tray=TrayState.ERROR)

    def _finish(self, state: PillState, *, tray: TrayState = TrayState.IDLE) -> None:
        self._begin_finish()
        self._pill.show_state(state)
        self._end_finish(state, tray=tray)

    def _begin_finish(self) -> None:
        self._cancel_timers()
        self._change_phase(DictationPhase.FINISHING)
        self._set_recording(False)

    def _end_finish(self, state: PillState, *, tray: TrayState) -> None:
        self._tray.set_state(tray)
        self._later(STATE_DURATION_MS[state], self._tail_done)
        self._hotkey_done()

    def _tail_done(self) -> None:
        self._change_phase(DictationPhase.IDLE)
        self._tray.set_state(TrayState.IDLE)

    def _command(self, message: dict[str, Any], **kwargs: float) -> bool:
        try:
            self._send(message, **kwargs)
        except Exception:
            self._log.warning("диктовка: отправка команды не удалась")
            self._fail_recognition()
            return False
        return True

    def _dictation_stat(self, result: str) -> None:
        stop = self._t_stop if self._t_stop is not None else self._clock()
        self._append_stat(
            "dictation",
            result=result,
            audio_ms=max(0.0, (stop - self._t0) * 1000),
            t_ms=self._t_ms,
            paste_ms=self._paste_ms,
            cold=self._cold,
        )

    def _append_stat(self, event_type: str, **fields: object) -> None:
        if self._stats is not None:
            try:
                self._stats.append(event_type, **fields)
            except Exception:
                self._log.warning("диктовка: статистика недоступна")

    def _change_phase(self, phase: DictationPhase) -> None:
        self._phase = phase
        self._log.debug("диктовка: фаза %s", phase.value)

    def _later(self, milliseconds: int, callback: Callable[[], None]) -> None:
        self._timer_serial += 1
        token = self._timer_serial

        def fire() -> None:
            # Даже уже доставленный отменённый таймер не выполняет работу. Его
            # оболочка не удерживает фразу из замыкания BUSY после отмены.
            entry = self._timers.pop(token, None)
            if entry is not None and not self._closed:
                entry[1]()

        handle = self._schedule(milliseconds, fire)
        self._timers[token] = (handle, callback)

    def _cancel_timers(self) -> None:
        pending, self._timers = self._timers, {}
        for handle, _ in pending.values():
            try:
                self._cancel_timer(handle)
            except Exception:
                self._log.warning("диктовка: отмена ручки таймера не удалась")

    def shutdown(self) -> None:
        """Закрывает автомат, освобождая таймеры и индикаторы даже при ошибках."""
        if self._closed:
            return
        active = self._phase not in (DictationPhase.IDLE, DictationPhase.FINISHING)
        self._closed = True
        self._cancel_requested = True
        self._queue.clear()
        self._cancel_timers()
        self._phase = DictationPhase.IDLE
        actions: list[Callable[[], None]] = []
        if active:
            actions.append(
                lambda: self._send({"type": "record.cancel", "utterance_id": self._utterance_id})
            )
        actions.extend((lambda: self._set_recording(False), self._hotkey_done, self._pill.hide))
        for action in actions:
            try:
                action()
            except Exception:
                self._log.warning("диктовка: ошибка при завершении")
