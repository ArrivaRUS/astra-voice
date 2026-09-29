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
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol
from uuid import uuid4

from astra_voice.platform.hotkey import HotkeyState
from astra_voice.platform.paste import PasteMode, PasteOutcome, PasteOutcomeKind, normalize
from astra_voice.ui.pill import (
    CLIPBOARD_NOT_FETCHED,
    CLIPBOARD_WINDOW_CHANGED,
    ERROR_BUFFER_CLEARED,
    ERROR_MICROPHONE_CHANGED,
    ERROR_MICROPHONE_LOST,
    ERROR_MICROPHONE_SILENT,
    ERROR_MICROPHONE_UNAVAILABLE,
    ERROR_MODEL_NOT_LOADED,
    ERROR_RECOGNITION_FAILED,
    ERROR_RECOGNITION_RESTARTED,
    STATE_DURATION_MS,
    PillState,
)
from astra_voice.ui.tray_icons import TrayState

RECOGNIZE_TIMEOUT_S = 30.0
PROCESSING_WATCHDOG_MS = 35000
# Решение заказчика 2026-09-27: 50 мс хвоста; если последнее слово обрезается, поднять до 100 мс.
# Запись в это время продолжается, поэтому фаза, пилюля и трей остаются «слушаю».
RELEASE_TAIL_MS = 50
CANCEL_TIMEOUT_MS = 1500
CANCEL_RESTART_MS = 2000
BUSY_RETRY_MS = 200
TEST_RECORD_LIMIT_MS = 10000
LEVEL_RECORD_LIMIT_S = 60.0
LEVEL_TOTAL_LIMIT_S = 180.0
LEVEL_LIMIT_MESSAGE = "Проверка микрофона остановлена. Нажмите, чтобы продолжить"
LEVEL_FAILED = "Не удалось проверить микрофон. Попробуйте ещё раз."
# S5-A5: столько звука до смены микрофона уже стоит распознать; короче — отмена.
DEVICE_CHANGE_MIN_AUDIO_MS = 300
DEVICE_SWITCHED = "switched"
DEVICE_LOST = "device-lost"
# Пилюлю о смене микрофона эти состояния не перебивают, пока она на экране (S5-A5 D3).
_DEVICE_PILL_KEEPS_OVER = frozenset(
    (PillState.PROCESSING, PillState.DONE, PillState.EMPTY, PillState.CANCELLED)
)


@dataclass(frozen=True)
class MicrophoneLevelUpdate:
    """Отдельный канал измерения, никогда не содержащий распознанной речи."""

    state: Literal["idle", "listening", "error"]
    peak_dbfs: float | None = None
    message: str = ""


LevelCallback = Callable[[MicrophoneLevelUpdate], None]


@dataclass(frozen=True)
class MicrophoneTestUpdate:
    """Частный результат для экрана микрофона; text нельзя передавать другим портам."""

    state: Literal["idle", "preparing", "recording", "processing", "done", "error"]
    text: str = ""
    duration_s: float | None = None
    peak_dbfs: float | None = None
    message: str = ""


TestCallback = Callable[[MicrophoneTestUpdate], None]
TEST_BUSY = "Сначала завершите текущую диктовку, затем попробуйте ещё раз."
TEST_FAILED = "Не удалось распознать речь. Попробуйте ещё раз."
TEST_MODEL_UNAVAILABLE = "Модель ещё не готова. Завершите её установку и попробуйте ещё раз."
TEST_PREPARING = "Готовлю модель…"


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
        restart_worker: Callable[[], None],
        pill: PillPort,
        tray: TrayPort,
        paste: Callable[[str, int | None, PasteMode], PasteOutcome],
        active_window: Callable[[], int | None],
        schedule: Callable[[int, Callable[[], None]], object],
        cancel_timer: Callable[[object], None],
        hotkey_done: Callable[[], None],
        hotkey_cancel: Callable[[], None],
        hotkey_idle: Callable[[], bool],
        set_recording: Callable[[bool], None],
        paste_mode: Callable[[], PasteMode],
        record_params: Callable[[], dict[str, Any]],
        clock: Callable[[], float] = time.monotonic,
        stats: StatsPort | None = None,
        log: logging.Logger | None = None,
        on_device_changed: Callable[[str], None] | None = None,
        on_device_lost: Callable[[], None] | None = None,
        on_device_selected: Callable[[str], None] | None = None,
        on_device_resolved: Callable[[str], None] | None = None,
        on_silent: Callable[[], None] | None = None,
        on_success: Callable[[float | None, float | None, bool], None] | None = None,
        on_idle: Callable[[], None] | None = None,
    ) -> None:
        self._send = send
        self._generation = generation
        self._restart_worker = restart_worker
        self._pill = pill
        self._tray = tray
        self._paste = paste
        self._active_window = active_window
        self._schedule = schedule
        self._cancel_timer = cancel_timer
        self._hotkey_done = hotkey_done
        self._hotkey_cancel = hotkey_cancel
        self._hotkey_idle = hotkey_idle
        self._set_recording = set_recording
        self._paste_mode = paste_mode
        self._record_params = record_params
        self._clock = clock
        self._stats = stats
        self._on_device_changed = on_device_changed
        self._on_device_lost = on_device_lost
        self._on_device_selected = on_device_selected
        self._on_device_resolved = on_device_resolved
        self._on_silent = on_silent
        self._on_success = on_success
        self._on_idle = on_idle
        self._resolved_device: str = ""
        self._announced_selected_device: str | None = None
        self._audio_opened = False
        self._announcement_generation: int | None = None
        self._device_selected = False
        self._log = log if log is not None else logging.getLogger(__name__)
        self._phase = DictationPhase.IDLE
        self._target_window: int | None = None
        self._utterance_id = ""
        self._worker_generation = 0
        self._last_text: str | None = None
        self._started = False
        self._cold = True
        self._t0 = 0.0
        self._t_ready: float | None = None
        self._t_stop: float | None = None
        self._t_release: float | None = None
        self._t_ms = 0.0
        self._paste_ms = 0.0
        self._t_total_ms: float | None = None
        self._result_audio_ms: float | None = None
        self._result_infer_ms: float | None = None
        self._retries = 0
        self._cancel_requested = False
        self._cancel_pending = False
        self._tail_pending = False
        self._delivered = False
        self._closed = False
        self._suspended = False
        self._queue: deque[Callable[[], None]] = deque()
        self._timer_serial = 0
        self._timers: dict[int, tuple[object, Callable[[], None]]] = {}
        self._test_callback: TestCallback | None = None
        self._level_callback: LevelCallback | None = None
        self._level_device = ""
        self._level_failed = False
        self._level_idle_message = ""
        self._level_awaiting_audio_closed = False
        # Смена микрофона посреди записи (audio.device.changed): не больше одной на диктовку.
        self._device_change_kind: str | None = None
        self._device_pill_until: float | None = None

    @property
    def level_active(self) -> bool:
        """Идёт измерение; ожидание подтверждения отмены сюда не входит."""
        return self._level_callback is not None and not self._cancel_requested

    def start_level_monitor(self, device: str, callback: LevelCallback) -> bool:
        """Открывает микрофон без обращения к модели или распознаванию."""
        if (
            self._closed
            or self._level_callback is not None
            or self.test_active
            or self._cancel_pending
            or self._phase not in (DictationPhase.IDLE, DictationPhase.FINISHING)
        ):
            callback(MicrophoneLevelUpdate("error", message=TEST_BUSY))
            return False
        self._level_callback = callback
        self._level_device = device
        self._level_failed = False
        self._level_idle_message = ""
        self._level_awaiting_audio_closed = False
        self._cancel_requested = False
        try:
            self._utterance_id = uuid4().hex
            self._cancel_timers()
            self._target_window = None
            self._delivered = False
            self._worker_generation = self._generation()
            self._sync_device_generation()
            self._phase = DictationPhase.RECORDING
            self._later(int(LEVEL_TOTAL_LIMIT_S * 1000), self._level_total_timeout)
            self._start_level_recording()
        except Exception:
            self._end_level(LEVEL_FAILED)
            return False
        return not self._level_failed

    def _start_level_recording(self) -> None:
        # Индикация включается до открытия: ошибка индикатора запрещает запись.
        self._pill.show_state(PillState.LISTENING)
        if not self.level_active:
            return
        self._tray.set_state(TrayState.LISTENING)
        if not self.level_active:
            return
        self._set_recording(True)
        if not self.level_active:
            return
        message: dict[str, Any] = {
            "type": "record.start",
            "utterance_id": self._utterance_id,
            "limit_s": LEVEL_RECORD_LIMIT_S,
        }
        if self._level_device:
            message["device"] = self._level_device
        self._send(message, timeout=LEVEL_RECORD_LIMIT_S + RECOGNIZE_TIMEOUT_S)
        if self.level_active and self._level_callback is not None:
            self._level_callback(MicrophoneLevelUpdate("listening"))

    def stop_level_monitor(self) -> None:
        """Отменяет запись; idle приходит только после освобождения микрофона."""
        if self._level_callback is not None and not self._cancel_requested:
            self._end_level()

    def _level_total_timeout(self) -> None:
        if self.level_active:
            self._end_level(idle_message=LEVEL_LIMIT_MESSAGE)

    def _level_notify(self, update: MicrophoneLevelUpdate) -> None:
        if self._level_callback is not None:
            try:
                self._level_callback(update)
            except Exception:
                # Терминальное уведомление не должно мешать освобождению микрофона.
                self._level_failed = True

    def _end_level(self, message: str = "", *, idle_message: str = "") -> None:
        self._cancel_requested = True
        self._cancel_pending = True
        self._level_idle_message = idle_message
        self._cancel_timers()
        actions: tuple[Callable[[], None], ...] = (
            lambda: self._set_recording(False),
            lambda: self._tray.set_state(TrayState.IDLE),
            self._pill.hide,
        )
        for action in actions:
            try:
                action()
            except Exception:
                message = message or LEVEL_FAILED
        if message:
            self._level_failed = True
            self._level_notify(MicrophoneLevelUpdate("error", message=message))
        try:
            self._later(CANCEL_TIMEOUT_MS, self._level_cancel_timeout)
            self._send({"type": "record.cancel", "utterance_id": self._utterance_id})
        except Exception:
            self._level_failed = True
            self._level_notify(MicrophoneLevelUpdate("error", message=LEVEL_FAILED))
            self._level_cancel_timeout()

    def _finish_level(self) -> None:
        callback, self._level_callback = self._level_callback, None
        idle_message, self._level_idle_message = self._level_idle_message, ""
        self._level_awaiting_audio_closed = False
        self._cancel_pending = False
        self._cancel_timers()
        self._phase = DictationPhase.IDLE
        if callback is not None and not self._level_failed:
            try:
                callback(MicrophoneLevelUpdate("idle", message=idle_message))
            except Exception:
                try:
                    callback(MicrophoneLevelUpdate("error", message=LEVEL_FAILED))
                except Exception:
                    pass

    def _level_cancel_timeout(self) -> None:
        if self._level_callback is None:
            return
        self._cancel_timers()
        try:
            self._later(CANCEL_RESTART_MS, self._level_cancel_restart)
        except Exception:
            self._level_failed = True
            self._level_notify(MicrophoneLevelUpdate("error", message=LEVEL_FAILED))
        try:
            # Ставим до send: ответ может прийти синхронно.
            self._level_awaiting_audio_closed = True
            self._send({"type": "audio.close"})
        except Exception:
            self._level_failed = True
            self._level_notify(MicrophoneLevelUpdate("error", message=LEVEL_FAILED))
            self._level_cancel_restart()

    def _level_cancel_restart(self) -> None:
        try:
            if self._generation() == self._worker_generation:
                self._restart_worker()
        except Exception:
            self._level_failed = True
            self._level_notify(MicrophoneLevelUpdate("error", message=LEVEL_FAILED))
        finally:
            self._finish_level()

    def _level_event(self, event: dict[str, Any]) -> None:
        if self._generation() != self._worker_generation or (
            isinstance(event.get("generation"), int)
            and event["generation"] > self._worker_generation
        ):
            self._end_level(LEVEL_FAILED)
            self._finish_level()
            self._sync_device_generation()
            return
        if event.get("generation") != self._worker_generation:
            return
        if event.get("utterance_id", self._utterance_id) != self._utterance_id:
            return
        kind = event.get("type")
        if kind == "audio.closed":
            # В ответе нет utterance_id: чужой поздний ответ не снимает сторожей.
            if self._cancel_pending and self._level_awaiting_audio_closed:
                self._finish_level()
        elif kind == "cancelled":
            if not self._cancel_requested:
                self._end_level(LEVEL_FAILED)
            self._finish_level()
        elif self._cancel_pending:
            return
        elif kind == "error":
            self._error(event.get("code"))
        elif kind == "audio.ready":
            self._audio_ready(event)
        elif kind == "level":
            peak = event.get("peak_dbfs")
            if isinstance(peak, (int, float)) and not isinstance(peak, bool):
                self._pill.show_state(PillState.LISTENING, level=level_from_dbfs(float(peak)))
                if self.level_active and self._level_callback is not None:
                    self._level_callback(MicrophoneLevelUpdate("listening", peak_dbfs=float(peak)))
        elif kind == "record.limit" and self.level_active:
            # Удаляем завершённый буфер. Его поздний cancelled отсеется по старому id.
            previous = self._utterance_id
            self._utterance_id = uuid4().hex
            self._send({"type": "record.cancel", "utterance_id": previous})
            if self.level_active:
                self._start_level_recording()

    @property
    def test_active(self) -> bool:
        """Проверка владеет тем же автоматом и микрофоном, что обычная диктовка."""
        return self._test_callback is not None

    def start_test(self, device: str, callback: TestCallback) -> bool:
        """Записывает выбранный микрофон без вставки, статистики и последнего текста."""
        if (
            self._closed
            or self._level_callback is not None
            or self.test_active
            or self._cancel_pending
            or self._phase not in (DictationPhase.IDLE, DictationPhase.FINISHING)
        ):
            callback(MicrophoneTestUpdate("error", message=TEST_BUSY))
            return False
        self._test_callback = callback
        self._start(test_device=device)
        return True

    def stop_test(self) -> None:
        """Стоп всегда проходит: запись → распознавание, распознавание → отмена."""
        if not self.test_active:
            return
        if self._phase == DictationPhase.RECORDING:
            self._stop()
        else:
            self.cancel("microphone-test")

    def cancel_test(self) -> None:
        """Уход с экрана отменяет и запись, и распознавание без результата."""
        if self.test_active:
            self.cancel("microphone-test")

    def _safe_ui(self, call: Callable[[], None], message: str) -> None:
        try:
            call()
        except Exception as error:
            # Сообщение и цепочка исключений могут содержать речь из callback.
            # Сохраняем стек, заменяя исключение служебным сообщением.
            self._log.warning(
                message, exc_info=(RuntimeError, RuntimeError(message), error.__traceback__)
            )

    def _finish_test(self, update: MicrophoneTestUpdate) -> None:
        callback, self._test_callback = self._test_callback, None
        self._cancel_timers()
        self._change_phase(DictationPhase.IDLE)
        self._safe_ui(
            lambda: self._set_recording(False), "диктовка: не удалось обновить индикатор записи"
        )
        self._safe_ui(
            lambda: self._tray.set_state(TrayState.IDLE), "диктовка: не удалось обновить трей"
        )
        if callback is not None:
            self._safe_ui(
                lambda: callback(update), "диктовка: не удалось обновить экран проверки микрофона"
            )

    @property
    def phase(self) -> DictationPhase:
        """Текущая фаза, доступная только для чтения."""
        return self._phase

    @property
    def last_text(self) -> str | None:
        """Последняя непустая фраза только в памяти процесса."""
        return self._last_text

    @property
    def resolved_device(self) -> str:
        """Имя действительно открытого микрофона; пусто, пока оно неизвестно."""
        return self._resolved_device

    def reset_device_announcement(self) -> None:
        """Сбрасывает объявление при выборе в настройках или новом поколении воркера."""
        self._audio_opened = False
        self._announced_selected_device = None
        self._announcement_generation = self._generation()
        if self._resolved_device:
            self._resolved_device = ""
            if self._on_device_resolved is not None:
                self._on_device_resolved("")

    def _sync_device_generation(self) -> None:
        if self._announcement_generation != self._generation():
            self.reset_device_announcement()

    def on_hotkey_state(self, state: HotkeyState, reason: str) -> None:
        """Принимает состояния HotkeyFsm, включая IDLE с escape-cancel."""
        if self._closed:
            return
        if self.test_active or self._level_callback is not None:
            # Сброс FSM вызывает вложенный IDLE/escape-cancel: его тоже игнорируем.
            # Отмена самой проверки идёт через stop_test/cancel_test, а не хоткей.
            if state in (HotkeyState.RECORDING, HotkeyState.PROCESSING):
                self._hotkey_cancel()
            return
        if self._suspended:
            self._queue.append(lambda: self.on_hotkey_state(state, reason))
            return
        if state == HotkeyState.RECORDING:
            if self._phase in (DictationPhase.IDLE, DictationPhase.FINISHING):
                self._start()
            elif reason == "press":
                # Только новое нажатие: tap→toggle и mapping-regrab сообщают
                # о продолжающейся записи и не должны её отменять.
                self._hotkey_cancel()
        elif state == HotkeyState.PROCESSING:
            # Предел длительности фразы — не отпускание клавиши: хвост не нужен.
            self._stop(tail=reason != "limit")
        elif state == HotkeyState.IDLE and reason == "escape-cancel":
            self.cancel("escape")

    def _start(self, *, test_device: str | None = None) -> None:
        if self._cancel_pending and self._generation() == self._worker_generation:
            self._hotkey_cancel()
            return
        self._cancel_pending = False
        self._target_window = None if self.test_active else self._active_window()
        self._utterance_id = uuid4().hex
        self._sync_device_generation()
        self._worker_generation = self._generation()
        self._cancel_timers()
        self._cancel_requested = False
        self._tail_pending = False
        self._delivered = False
        if not self.test_active:
            self._cold, self._started = not self._started, True
        self._t_ready = None
        self._t_stop = None
        self._t_release = None
        self._t_ms = self._paste_ms = 0.0
        self._t_total_ms = None
        self._result_audio_ms = None
        self._result_infer_ms = None
        self._retries = 0
        self._device_change_kind = None
        self._device_pill_until = None
        try:
            self._device_selected = False
            params: dict[str, Any] = (
                {"device": test_device, "limit_s": TEST_RECORD_LIMIT_MS / 1000}
                if self.test_active
                else self._record_params()
            )
            message = {
                "type": "record.start",
                "utterance_id": self._utterance_id,
                "limit_s": params["limit_s"],
            }
            device = params.get("device")
            if isinstance(device, str) and device:
                message["device"] = device
                self._device_selected = True
            timeout = float(params["limit_s"]) + RECOGNIZE_TIMEOUT_S
        except Exception:
            self._log.warning("диктовка: параметры записи недоступны")
            self._fail_recognition()
            self._hotkey_cancel()
            return
        self._t0 = self._clock()
        self._change_phase(DictationPhase.RECORDING)
        if not self._command(message, timeout=timeout):
            self._hotkey_cancel()
            return
        if self._phase != DictationPhase.RECORDING or self._cancel_requested:
            return
        if self._test_callback is not None:
            self._later(TEST_RECORD_LIMIT_MS, self.stop_test)
        if not self.test_active:
            self._pill.show_state(PillState.LISTENING)
        self._tray.set_state(TrayState.LISTENING)
        self._set_recording(True)
        if self._test_callback is not None:
            self._test_callback(MicrophoneTestUpdate("recording"))

    def _stop(self, *, recording_stopped: bool = False, tail: bool = True) -> None:
        """Отпускание клавиши даёт хвост записи; фаза меняется только после него."""
        if self._level_callback is not None:
            self.stop_level_monitor()
            return
        if self._phase != DictationPhase.RECORDING or self._cancel_requested:
            return
        if tail and not recording_stopped and self._t_release is None:
            self._t_release = self._clock()
        # По record.limit запись уже остановлена, а проверка микрофона хвоста не ждёт.
        if RELEASE_TAIL_MS > 0 and tail and not recording_stopped and not self.test_active:
            if self._tail_pending:
                return
            self._tail_pending = True
            self._later(RELEASE_TAIL_MS, self._release_tail_elapsed)
            return
        self._finish_recording(recording_stopped=recording_stopped)

    def _release_tail_elapsed(self) -> None:
        """Хвост дописан: дальше обычная остановка записи и распознавание."""
        if not self._tail_pending:
            return
        self._tail_pending = False
        if self._phase != DictationPhase.RECORDING or self._cancel_requested:
            return
        self._finish_recording()

    def _finish_recording(self, *, recording_stopped: bool = False) -> None:
        self._tail_pending = False
        if self.test_active:
            self._cancel_timers()
        self._t_stop = self._clock()
        self._change_phase(DictationPhase.PROCESSING)
        # По record.limit воркер уже остановил запись; повторный stop даёт bad-state.
        if not recording_stopped and not self._command(
            {"type": "record.stop", "utterance_id": self._utterance_id}
        ):
            return
        if self.phase != DictationPhase.PROCESSING or self._cancel_requested:
            return
        if self._test_callback is not None:
            callback = self._test_callback
            self._safe_ui(
                lambda: self._set_recording(False), "диктовка: не удалось обновить индикатор записи"
            )
            self._safe_ui(
                lambda: callback(MicrophoneTestUpdate("processing")),
                "диктовка: не удалось обновить экран проверки микрофона",
            )
            # Обработчик состояния вправе сразу отменить распознавание.
            if self.phase != DictationPhase.PROCESSING or self._cancel_requested:
                return
        if not self._command(
            {"type": "recognize", "utterance_id": self._utterance_id}, timeout=RECOGNIZE_TIMEOUT_S
        ):
            return
        if self.phase != DictationPhase.PROCESSING or self._cancel_requested:
            return
        self._safe_ui(
            lambda: self._set_recording(False), "диктовка: не удалось обновить индикатор записи"
        )
        if not self.test_active and not self._device_pill_held(PillState.PROCESSING):
            self._safe_ui(
                lambda: self._pill.show_state(PillState.PROCESSING),
                "диктовка: не удалось обновить пилюлю",
            )
        self._safe_ui(
            lambda: self._tray.set_state(TrayState.PROCESSING), "диктовка: не удалось обновить трей"
        )
        self._later(PROCESSING_WATCHDOG_MS, self._watchdog)

    def on_worker_event(self, event: dict[str, Any]) -> None:
        """Отбрасывает чужие результаты до чтения содержимого распознанной речи."""
        if self._closed:
            return
        if self._level_callback is not None:
            try:
                self._level_event(event)
            except Exception:
                if self._level_callback is not None:
                    self._end_level(LEVEL_FAILED)
            return
        if self._suspended:
            saved = event.copy()
            self._queue.append(lambda: self.on_worker_event(saved))
            return
        # hello до первой диктовки тоже начинает новое поколение объявлений.
        self._sync_device_generation()
        if (
            self._phase in (DictationPhase.IDLE, DictationPhase.FINISHING)
            and not self._cancel_pending
        ):
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
        if kind == "cancelled":
            self._cancelled()
        elif self._cancel_pending:
            return
        elif (
            kind == "error"
            and event.get("code") == "bad-state"
            and event.get("request_type") == "record.stop"
        ):
            # Воркер сам остановил запись раньше нашего stop (record.limit или смена
            # микрофона). Буфер цел: исход скажет уже отправленный recognize.
            self._log.debug("диктовка: запись уже остановлена воркером")
        elif kind == "error":
            self._error(event.get("code"))
        elif self._cancel_requested:
            return
        elif kind == "audio.device.changed":
            self._device_change(event)
        elif kind == "result" and self._phase == DictationPhase.PROCESSING:
            text = event.get("text")
            if not isinstance(text, str):
                self._log.debug("диктовка: некорректное поле text")
                text = ""
            duration = event.get("t_ms")
            self._result_audio_ms = self._measurement_number(event.get("audio_ms"))
            self._result_infer_ms = self._measurement_number(event.get("infer_ms"))
            self._result(
                text,
                duration_s=(
                    max(0.0, float(duration) / 1000)
                    if isinstance(duration, (int, float))
                    and not isinstance(duration, bool)
                    and math.isfinite(duration)
                    else None
                ),
            )
        elif kind == "audio.ready":
            self._audio_ready(event)
        elif self._phase == DictationPhase.RECORDING:
            if kind == "level":
                peak = event.get("peak_dbfs")
                if not isinstance(peak, (int, float)) or isinstance(peak, bool):
                    self._log.debug("диктовка: некорректное поле peak_dbfs")
                    return
                try:
                    level = level_from_dbfs(float(peak))
                except (ValueError, OverflowError):
                    self._log.debug("диктовка: некорректное поле peak_dbfs")
                    return
                if self._test_callback is not None:
                    self._test_callback(MicrophoneTestUpdate("recording", peak_dbfs=float(peak)))
                else:
                    self._pill.show_state(PillState.LISTENING, level=level)
            elif kind == "silent":
                if self.test_active:
                    self._stop()
                else:
                    self._pill.show_state(PillState.LISTENING_SILENT)
                    self._announce_silent()
            elif kind == "record.limit":
                if not self.test_active:
                    self._pill.show_state(PillState.LIMIT)
                self._stop(recording_stopped=True)

    def _device_change(self, event: dict[str, Any]) -> None:
        """Воркер сам остановил запись из-за смены микрофона (S5-A5, IPC v3)."""
        kind = event.get("kind")
        label = event.get("label")
        label = label.strip() if isinstance(label, str) else ""
        if kind == DEVICE_SWITCHED and not label:
            # Без подписи сказать «сейчас используется» нечего — как у воркера по бюджету.
            kind = DEVICE_LOST
        if kind not in (DEVICE_SWITCHED, DEVICE_LOST):
            self._log.debug("диктовка: некорректное поле kind")
            return
        if self._device_change_kind is not None:
            self._log.debug("диктовка: повторная смена микрофона отброшена")
            return
        if self.test_active:
            return
        self._device_change_kind = kind
        # Только вид события: подпись устройства и текст в журнал не идут.
        self._log.info("диктовка: смена микрофона посреди записи (%s)", kind)
        self._announce_device_change(kind, label)
        if self._phase != DictationPhase.RECORDING:
            # Клавишу уже отпустили: распознавание записанного идёт своим ходом.
            return
        audio_ms = event.get("audio_ms")
        if (
            isinstance(audio_ms, int)
            and not isinstance(audio_ms, bool)
            and audio_ms >= DEVICE_CHANGE_MIN_AUDIO_MS
        ):
            # Как после record.limit: воркер уже остановил запись, stop не нужен.
            self._stop(recording_stopped=True)
            return
        self._begin_finish()
        self._send_cancel()
        self._end_finish(
            PillState.ERROR, tray=TrayState.ERROR if kind == DEVICE_LOST else TrayState.IDLE
        )

    def _announce_device_change(self, kind: str, label: str) -> None:
        switched = kind == DEVICE_SWITCHED
        self._device_pill_until = self._clock() + STATE_DURATION_MS[PillState.ERROR] / 1000
        self._append_stat(
            "mic_error", kind="device-changed" if switched else DEVICE_LOST, recovered_by="none"
        )
        self._safe_ui(
            lambda: self._pill.show_state(
                PillState.ERROR,
                text=ERROR_MICROPHONE_CHANGED if switched else ERROR_MICROPHONE_LOST,
            ),
            "диктовка: не удалось обновить пилюлю",
        )
        if switched:
            # Следующая запись с этого микрофона не объявит «Микрофон: X» ещё раз.
            self._announced_selected_device = label
            if label != self._resolved_device:
                self._resolved_device = label
                on_resolved = self._on_device_resolved
                if on_resolved is not None:
                    self._safe_ui(
                        lambda: on_resolved(label), "диктовка: не удалось обновить микрофон"
                    )
            on_changed = self._on_device_changed
            if on_changed is not None:
                self._safe_ui(
                    lambda: on_changed(label), "диктовка: не удалось сообщить о смене микрофона"
                )
        elif self._on_device_lost is not None:
            self._safe_ui(self._on_device_lost, "диктовка: не удалось сообщить о смене микрофона")

    def _announce_silent(self) -> None:
        """Сообщает наружу о тишине; причину выясняет рантайм, не автомат."""
        if self._on_silent is None:
            return
        self._safe_ui(self._on_silent, "диктовка: не удалось назвать причину тишины")

    def _audio_ready(self, event: dict[str, Any]) -> None:
        # Воркер передаёт системное описание, а не идентификатор из record_params.
        device = event.get("device")
        name = device.strip() if isinstance(device, str) else ""
        if not name:
            changed = event.get("changed")
            if isinstance(changed, str) and ": " in changed:
                name = changed.rpartition(": ")[2].strip()
        if name and name != self._resolved_device:
            self._resolved_device = name
            if self._on_device_resolved is not None:
                self._on_device_resolved(name)
        if self.test_active or self._level_callback is not None:
            return
        self._log.debug("диктовка: audio.ready")
        if self._t_ready is None:
            self._t_ready = self._clock()
        first_open = not self._audio_opened
        self._audio_opened = True
        if self._phase != DictationPhase.RECORDING:
            return
        if not name:
            return
        # audio.ready приходит только при открытии, в том числе после долгих повторов.
        previous_name = self._announced_selected_device
        if name == previous_name:
            return
        self._announced_selected_device = name
        if first_open or previous_name is None:
            if self._on_device_selected is not None:
                self._on_device_selected(name)
        elif self._on_device_changed is not None:
            self._on_device_changed(name)

    def _result(self, text: str, *, duration_s: float | None = None) -> None:
        received = self._clock()
        assert self._t_stop is not None
        self._t_ms = max(0.0, (received - self._t_stop) * 1000)
        if self.test_active:
            self._finish_test(
                MicrophoneTestUpdate(
                    "done",
                    text=text,
                    duration_s=duration_s if duration_s is not None else self._t_ms / 1000,
                )
            )
            return
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
        not_fetched = False
        outcome: PasteOutcome | None = None
        try:
            outcome = self._paste(text, self._target_window, self._paste_mode())
            kind = outcome.kind
            not_fetched = outcome.reason == "not-fetched"
        except Exception:
            # Исключение может содержать фразу: не передаём даже его repr в журнал.
            self._log.warning("диктовка: ошибка вызова вставки")
            kind = PasteOutcomeKind.FAILED
        finally:
            finished = self._clock()
            self._paste_ms += max(0.0, (finished - started) * 1000)
            self._suspended = False
        # Исход известен до разбора очереди: доставленную фразу отменить уже нельзя.
        self._delivered = kind in (
            PasteOutcomeKind.PASTED,
            PasteOutcomeKind.CLIPBOARD_ONLY,
            PasteOutcomeKind.WINDOW_CHANGED,
        )
        if (
            kind == PasteOutcomeKind.PASTED
            and outcome is not None
            and outcome.delivered_ms is not None
            and self._t_total_ms is None
        ):
            release = self._t_release if self._t_release is not None else self._t_stop
            assert release is not None
            self._t_total_ms = max(0.0, (started + outcome.delivered_ms / 1000 - release) * 1000)
        # Сохраняем PASTING: нажатия внутри paste не запускают следующую диктовку.
        while self._queue and not self._closed:
            if self._phase in (DictationPhase.FINISHING, DictationPhase.IDLE):
                self._log.debug("диктовка: отложенные события после завершения отброшены")
                self._queue.clear()
                break
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
        self._last_text = normalize(text)
        self._tray.set_has_last_text(True)
        if kind == PasteOutcomeKind.PASTED:
            self._dictation_stat("ok")
            self._finish(PillState.DONE, tray=TrayState.DONE)
        elif kind == PasteOutcomeKind.WINDOW_CHANGED:
            self._dictation_stat("ok")
            self._begin_finish()
            if not_fetched:
                self._pill.show_state(PillState.CLIPBOARD_ONLY, text=CLIPBOARD_NOT_FETCHED)
            else:
                self._pill.show_state(PillState.CLIPBOARD_ONLY, text=CLIPBOARD_WINDOW_CHANGED)
            self._end_finish(PillState.CLIPBOARD_ONLY, tray=TrayState.IDLE)
        elif kind in (PasteOutcomeKind.CLIPBOARD_ONLY, PasteOutcomeKind.BUSY):
            self._dictation_stat("ok")
            self._finish(PillState.CLIPBOARD_ONLY)
        elif kind == PasteOutcomeKind.REFUSED_SECRET:
            self._dictation_stat("ok")
            self._fail_secret()
        else:
            self._fail_recognition()

    def cancel(self, source: str) -> None:
        """Единая отмена до доставки; успешную вставку отменить уже нельзя."""
        if self._closed:
            return
        if self._level_callback is not None:
            self.stop_level_monitor()
            return
        if self._suspended:
            self._queue.append(lambda: self.cancel(source))
            return
        if self._delivered:
            self._log.debug("диктовка: поздняя отмена после доставки")
            return
        if self._phase in (DictationPhase.IDLE, DictationPhase.FINISHING):
            return
        if self._cancel_requested:
            return
        self._cancel_requested = True
        self._cancel_pending = True
        self._tail_pending = False
        if self._t_stop is None:
            self._t_stop = self._clock()
        # _cancel_timers снимает все ручки _later, поэтому сначала очищаем старые.
        self._cancel_timers()
        # Сторож ставится до send: синхронный cancelled тоже должен его отменить.
        self._later(CANCEL_TIMEOUT_MS, self._cancel_timeout)
        self._send_cancel()
        self._safe_ui(self._hotkey_cancel, "диктовка: не удалось сбросить горячую клавишу")
        self._safe_ui(
            lambda: self._set_recording(False), "диктовка: не удалось обновить индикатор записи"
        )
        self._safe_ui(
            lambda: self._tray.set_state(TrayState.IDLE), "диктовка: не удалось обновить трей"
        )

    def _send_cancel(self) -> None:
        try:
            self._send({"type": "record.cancel", "utterance_id": self._utterance_id})
        except Exception:
            self._log.warning("диктовка: отправка отмены не удалась")

    def on_indicators_lost(self) -> None:
        """IndicatorGuard сам уведомляет пользователя; здесь только отмена О4."""
        self.cancel("indicators")

    def _cancelled(self) -> None:
        self._cancel_pending = False
        if self.test_active:
            self._finish_test(MicrophoneTestUpdate("idle"))
            return
        if self._phase in (DictationPhase.IDLE, DictationPhase.FINISHING):
            self._cancel_timers()
            self._tail_done()
            return
        self._cancel_requested = True
        if self._t_stop is None:
            self._t_stop = self._clock()
        self._dictation_stat("cancelled")
        self._finish(PillState.CANCELLED)

    def _cancel_timeout(self) -> None:
        if self._generation() != self._worker_generation:
            self._fail_restarted()
            return
        if self.test_active:
            # Не снимаем блокировку старта внутри callback: воркер ещё не ответил.
            self._finish_test(MicrophoneTestUpdate("idle"))
        else:
            self._cancelled()
        self._cancel_pending = True
        # FINISHING не означает освобождение микрофона. Ставим второй сторож
        # до send, чтобы даже синхронное подтверждение сняло эскалацию.
        self._later(CANCEL_RESTART_MS, self._restart_after_cancel)
        try:
            self._send({"type": "audio.close"})
        except Exception:
            self._log.warning("диктовка: освобождение микрофона не удалось")

    def _restart_after_cancel(self) -> None:
        try:
            if self._generation() == self._worker_generation:
                self._restart_worker()
        except Exception:
            self._log.warning("диктовка: перезапуск воркера не удался")
        finally:
            self._cancel_pending = False

    def _error(self, code: object) -> None:
        if self.test_active or self._level_callback is not None:
            message = {
                "audio-no-device": "Микрофон недоступен. Подключите его или выберите другой.",
                "audio-busy": "Микрофон занят. Закройте другую программу и попробуйте ещё раз.",
                "audio-failed": "Не удалось записать звук. Выберите другой микрофон.",
                "audio-silent": (
                    "Микрофон молчит. Проверьте, что он включён, или выберите другой."
                ),
                "no-model": TEST_MODEL_UNAVAILABLE,
            }.get(str(code), TEST_FAILED)
            if self._level_callback is not None:
                self._end_level(message)
                return
            self._finish_test(MicrophoneTestUpdate("error", message=message))
            return
        if code in ("worker-crashed", "restart-limit", "worker-start"):
            self._fail_restarted()
        elif code == "audio-no-device" and self._device_selected:
            self._append_stat("mic_error", kind="device-lost", recovered_by="none")
            self._begin_finish()
            self._pill.show_state(PillState.ERROR, text=ERROR_MICROPHONE_LOST)
            self._end_finish(PillState.ERROR, tray=TrayState.ERROR)
            if self._on_device_lost is not None:
                self._on_device_lost()
        elif code == "audio-silent":
            # Источник открыт, но звука не даёт. Статистику и причину пишет
            # обработчик тишины: у него есть состояние источника (kind=muted/…).
            self._begin_finish()
            self._pill.show_state(PillState.ERROR, text=ERROR_MICROPHONE_SILENT)
            self._end_finish(PillState.ERROR, tray=TrayState.ERROR)
            self._announce_silent()
        elif code in ("audio-no-device", "audio-busy", "audio-failed"):
            kind = {"audio-no-device": "none", "audio-busy": "busy"}.get(str(code), "other")
            self._append_stat("mic_error", kind=kind, recovered_by="none")
            self._fail_microphone()
        elif code == "no-model":
            self._fail_model_not_loaded()
        else:
            self._fail_recognition()

    def _watchdog(self) -> None:
        self._log.warning("диктовка: истёк сторож PROCESSING")
        self._fail_recognition(cancel_worker=True)

    def _fail_restarted(self) -> None:
        if self.test_active:
            self._cancel_pending = False
            self._finish_test(MicrophoneTestUpdate("error", message=TEST_FAILED))
            return
        self._begin_finish()
        self._pill.show_state(PillState.ERROR, text=ERROR_RECOGNITION_RESTARTED)
        self._end_finish(PillState.ERROR, tray=TrayState.ERROR)

    def _fail_microphone(self) -> None:
        self._begin_finish()
        self._pill.show_state(PillState.ERROR, text=ERROR_MICROPHONE_UNAVAILABLE)
        self._end_finish(PillState.ERROR, tray=TrayState.ERROR)

    def _fail_recognition(self, *, cancel_worker: bool = False) -> None:
        if self.test_active:
            # Даже ошибка отправки stop/recognize не должна оставить запись открытой.
            self._cancel_requested = True
            self._cancel_pending = True
            self._finish_test(MicrophoneTestUpdate("error", message=TEST_FAILED))
            self._later(CANCEL_TIMEOUT_MS, self._cancel_timeout)
            self._send_cancel()
            return
        self._begin_finish()
        if cancel_worker:
            self._send_cancel()
        self._pill.show_state(PillState.ERROR, text=ERROR_RECOGNITION_FAILED)
        self._end_finish(PillState.ERROR, tray=TrayState.ERROR)

    def _fail_model_not_loaded(self) -> None:
        self._begin_finish()
        self._pill.show_state(PillState.ERROR, text=ERROR_MODEL_NOT_LOADED)
        self._end_finish(PillState.ERROR, tray=TrayState.ERROR)

    def _fail_secret(self) -> None:
        self._begin_finish()
        self._pill.show_state(PillState.ERROR, text=ERROR_BUFFER_CLEARED)
        self._end_finish(PillState.ERROR, tray=TrayState.ERROR)

    def _finish(self, state: PillState, *, tray: TrayState = TrayState.IDLE) -> None:
        self._begin_finish()
        if not self._device_pill_held(state):
            self._pill.show_state(state)
        self._end_finish(state, tray=tray)

    def _device_pill_held(self, state: PillState) -> bool:
        """Пилюля «Микрофон сменился/отключился» ещё видна и важнее этого состояния."""
        if self._device_pill_until is None or state not in _DEVICE_PILL_KEEPS_OVER:
            return False
        return self._clock() < self._device_pill_until

    def _begin_finish(self) -> None:
        self._cancel_pending = False
        self._tail_pending = False
        self._cancel_timers()
        self._change_phase(DictationPhase.FINISHING)
        self._set_recording(False)

    def _end_finish(self, state: PillState, *, tray: TrayState) -> None:
        duration = STATE_DURATION_MS[state]
        if self._device_change_kind == DEVICE_LOST and self._device_pill_held(state):
            # Трей горит ошибкой, пока видна пилюля «Микрофон отключился».
            assert self._device_pill_until is not None
            tray = TrayState.ERROR
            remaining = math.ceil((self._device_pill_until - self._clock()) * 1000)
            duration = max(duration, remaining)
        self._tray.set_state(tray)
        self._later(duration, self._tail_done)
        self._hotkey_done()
        # done завершает только PROCESSING; терминальный исход возможен и из RECORDING.
        if not self._hotkey_idle():
            self._hotkey_cancel()

    def _tail_done(self) -> None:
        self._change_phase(DictationPhase.IDLE)
        self._tray.set_state(TrayState.IDLE)

    def _command(self, message: dict[str, Any], **kwargs: float) -> bool:
        try:
            self._send(message, **kwargs)
        except Exception:
            self._log.warning(
                "диктовка: отправка команды %s не удалась",
                message.get("type"),
                exc_info=None if self.test_active else True,
            )
            self._fail_recognition()
            return False
        return True

    def _dictation_stat(self, result: str) -> None:
        stop = self._t_stop if self._t_stop is not None else self._clock()
        audio_ms = max(0.0, (stop - self._t0) * 1000)
        fields: dict[str, object] = dict(
            result=result,
            # Включает открытие устройства: _t0 ставится до отправки record.start.
            audio_ms=audio_ms,
            # До получения первого audio.ready в GUI, включая доставку события.
            open_ms=(
                max(0.0, (self._t_ready - self._t0) * 1000) if self._t_ready is not None else None
            ),
            t_ms=self._t_ms,
            paste_ms=self._paste_ms,
            cold=self._cold,
        )
        if self._delivered and self._t_total_ms is not None:
            fields["t_total_ms"] = self._t_total_ms
        self._append_stat("dictation", **fields)
        if result == "ok" and self._on_success is not None:
            self._on_success(self._result_audio_ms, self._result_infer_ms, self._cold)

    @staticmethod
    def _measurement_number(value: object) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if math.isfinite(number) and number > 0:
                return number
        return None

    def model_changed(self) -> None:
        """Первая диктовка новой модели снова считается холодной."""
        self._started = False

    def _append_stat(self, event_type: str, **fields: object) -> None:
        # Неизвестные значения (open_ms без audio.ready — машины с Fly, 22.09) не
        # пишутся вовсе: статистика принимает только числа, строки и флаги.
        fields = {key: value for key, value in fields.items() if value is not None}
        if self._stats is not None:
            try:
                self._stats.append(event_type, **fields)
            except Exception:
                self._log.warning("диктовка: статистика недоступна")

    def _change_phase(self, phase: DictationPhase) -> None:
        previous = self._phase
        self._phase = phase
        self._log.debug("диктовка: фаза %s", phase.value)
        if phase == DictationPhase.IDLE and previous != phase and self._on_idle is not None:
            self._safe_ui(self._on_idle, "диктовка: не удалось обработать простой")

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
        self._closed = True
        if self._level_callback is not None:
            self.stop_level_monitor()
            self._finish_level()
        active = self._phase not in (DictationPhase.IDLE, DictationPhase.FINISHING)
        self._cancel_pending = False
        self._tail_pending = False
        self._cancel_requested = True
        self._queue.clear()
        self._cancel_timers()
        self._phase = DictationPhase.IDLE
        callback, self._test_callback = self._test_callback, None
        actions: list[Callable[[], None]] = []
        if callback is not None:
            actions.append(lambda: callback(MicrophoneTestUpdate("idle")))
        if active:
            actions.extend((self._hotkey_cancel, self._send_cancel))
        actions.extend((lambda: self._set_recording(False), self._hotkey_done, self._pill.hide))
        for action in actions:
            try:
                action()
            except Exception:
                self._log.warning("диктовка: ошибка при завершении")
