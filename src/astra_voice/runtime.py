"""Проводка диктовки в цикл Qt; переходами состояний владеет оркестратор."""

from __future__ import annotations

import atexit
import logging
from collections.abc import Callable
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any, cast

from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEventLoop, QObject, QSocketNotifier, Qt, QTimer

from astra_voice.core import paths
from astra_voice.core.capture_watchdog import CaptureFieldWatchdog
from astra_voice.core.dictation import (
    LEVEL_FAILED,
    TEST_BUSY,
    TEST_MODEL_UNAVAILABLE,
    TEST_PREPARING,
    DictationOrchestrator,
    DictationPhase,
    LevelCallback,
    MicrophoneLevelUpdate,
    MicrophoneTestUpdate,
    TestCallback,
)
from astra_voice.core.model_source import (
    ModelRevoked,
    ModelStore,
    RevokedCheck,
    resolve_model_request,
    smoke_matches,
    smoke_wav_path,
)
from astra_voice.core.settings import Settings, is_valid_combo
from astra_voice.core.stats import SAVE_INTERVAL_S, Stats
from astra_voice.platform.hotkey import (
    DEFAULT_CANDIDATES,
    RECORD_LIMIT_S,
    GrabResult,
    HotkeyManager,
    HotkeyMode,
    HotkeyState,
)
from astra_voice.platform.paste import (
    PasteMode,
    PasteOutcome,
    paste_text,
    publish_clipboard,
    restore_pending,
)
from astra_voice.platform.session import SessionKind
from astra_voice.platform.sound import MicrophoneProblem, MicrophoneState, SoundControl
from astra_voice.platform.x11 import X11Display
from astra_voice.ui import notify
from astra_voice.ui.indicators import IndicatorGuard
from astra_voice.ui.notify import (
    ACTION_CHOOSE_HOTKEY,
    ACTION_CHOOSE_MICROPHONE,
    ACTION_OPEN_SOUND_SETTINGS,
    ACTION_SHOW_DETAILS,
)
from astra_voice.ui.pill import (
    ERROR_MODEL_LOAD_FAILED,
    ERROR_MODEL_NOT_LOADED,
    ERROR_MODEL_REVOKED,
    ERROR_SELFCHECK_FAILED,
    Pill,
    PillState,
)
from astra_voice.ui.tray import Tray
from astra_voice.ui.tray_icons import TrayIconProvider, TrayState
from astra_voice.worker import ipc
from astra_voice.worker.supervisor import WorkerSupervisor

log = logging.getLogger(__name__)

# После model.loaded: в замерах загрузка занимает 0,7 с, распознавание 6 с —
# 157 мс. Для эталона 1,6 с ожидаем <1 с (F4.9); 3 с — аварийный запас для CPU.
SELFCHECK_TIMEOUT_S = 3.0
SELFCHECK_WATCHDOG_MS = 3000
PREPARING_WATCHDOG_MS = 30_000
REGRAB_INTERVAL_MS = 30000
_NOTIFICATION_ACTIONS = (ACTION_CHOOSE_HOTKEY, ACTION_SHOW_DETAILS, ACTION_CHOOSE_MICROPHONE)


def _cpu_model() -> str:
    """Читает название первого процессора; отсутствие сведений допустимо."""
    try:
        with Path("/proc/cpuinfo").open(encoding="utf-8") as handle:
            for line in handle:
                key, separator, value = line.partition(":")
                if separator and key.strip() == "model name":
                    return value.strip()
    except Exception:
        return ""
    return ""


class DictationRuntime(QObject):
    """Владеет ресурсами диктовки; создаётся и обслуживается в потоке GUI.

    Фабрики подменяют внешние ресурсы, а методы _create_timer и
    _create_notifier позволяют проверять проводку без цикла событий и дисплея.
    После shutdown повторный запуск невозможен: нужен новый экземпляр.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        session_kind: SessionKind,
        model_store: ModelStore | None = None,
        parent: QObject | None = None,
        supervisor_factory: Callable[..., WorkerSupervisor] = WorkerSupervisor,
        pill_factory: Callable[..., Pill] = Pill,
        tray_factory: Callable[..., Tray] = Tray,
        hotkey_factory: Callable[[], HotkeyManager] = HotkeyManager,
        stats_factory: Callable[[], Stats] = Stats,
        paste_func: Callable[[str, int | None, PasteMode], PasteOutcome] = paste_text,
        restore_paste: Callable[[], bool] = restore_pending,
        x11_factory: Callable[[], X11Display] = X11Display,
        guard_factory: Callable[..., IndicatorGuard] = IndicatorGuard,
        provider_factory: Callable[[SessionKind], TrayIconProvider] = TrayIconProvider,
        capture_watchdog_factory: Callable[[], CaptureFieldWatchdog] = CaptureFieldWatchdog,
        sound_factory: Callable[[SessionKind], SoundControl] = lambda kind: SoundControl(
            session=kind
        ),
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.model_store = model_store
        self.session_kind = session_kind
        # Каталог создаётся позже окна: проверку отзыва подставляют сеттером.
        self._revoked_check: RevokedCheck | None = None
        self._revoked_notified = False
        self.on_quit_requested: Callable[[], None] | None = None
        self.on_show_requested: Callable[[], None] | None = None
        self._resolved_device = ""
        self._on_device_resolved: Callable[[str], None] | None = None
        self.sound = sound_factory(session_kind)
        # Причина «вас не слышно» объявляется один раз на причину за сеанс (F6.6 (д)).
        self._announced_mic_problems: set[MicrophoneProblem] = set()
        self._started = False
        self._closed = False
        self._loading_model = False
        self._model_load_generation: int | None = None
        self._model_load_request: dict[str, Any] | None = None
        self._model_load_failures = 0
        self._selfcheck: str = "idle"
        self._selfcheck_attempts = 0
        self._selfcheck_timer: QTimer | None = None
        self._loaded_model: dict[str, str] = {}
        self._pending_test: tuple[str, TestCallback] | None = None
        self._preparing_timer: QTimer | None = None
        self._model_load_ms: int | float | None = None
        self._supervisor_factory = supervisor_factory
        self._capture_watchdog_factory = capture_watchdog_factory
        self._capture_watchdog: CaptureFieldWatchdog | None = None
        self.notifier: QSocketNotifier | None = None
        self.tick_timer: QTimer | None = None
        self.stats_timer: QTimer | None = None
        self._regrab_timer: QTimer | None = None
        self._regrab_attempts = 0
        self._regrab_code: str | None = None
        self._regrab_target: tuple[str, str] | None = None
        self.timers: set[QTimer] = set()
        rollback: list[tuple[str, Callable[[], object]]] = [
            ("объекты Qt", self._delete_build_children),
        ]
        try:
            self.x11 = x11_factory()
            rollback.append(("основное соединение X11", self.x11.close))
            self.hotkey = hotkey_factory()
            rollback.extend(
                (
                    ("соединение хоткея", self._close_hotkey_backend),
                    ("захват Escape", self._ungrab_escape),
                    ("захват хоткея", self.hotkey.ungrab),
                )
            )
            self.pill = pill_factory(session=session_kind, parent=self)
            rollback.append(("пилюля", self.pill.hide))
            self.provider = provider_factory(session_kind)
            self.tray = tray_factory(self.provider, hotkey=settings.hotkey, parent=self)
            rollback.append(("трей", self.tray.stop))
            self.guard = guard_factory(self.pill, self.tray, parent=self)
            rollback.append(("страж индикаторов", self.guard.stop))
            self.stats = stats_factory()
            rollback.append(("статистика", self.stats.flush))
            self.paste_func = paste_func
            self.restore_paste = restore_paste
            self.orchestrator = DictationOrchestrator(
                send=self._send,
                generation=lambda: self.supervisor.generation,
                restart_worker=self.restart_worker,
                pill=self.pill,
                tray=self.tray,
                paste=self.paste_func,
                active_window=self.x11.active_window,
                schedule=self.schedule,
                cancel_timer=self.cancel_timer,
                hotkey_done=lambda: self.hotkey.fsm.done(monotonic()),
                hotkey_cancel=lambda: self.hotkey.fsm.escape(monotonic()),
                hotkey_idle=lambda: self.hotkey.fsm.state is HotkeyState.IDLE,
                set_recording=self.guard.set_recording,
                paste_mode=self.paste_mode,
                record_params=self.record_params,
                stats=self.stats,
                on_device_changed=notify.notify_microphone_changed,
                on_device_lost=notify.notify_microphone_lost,
                on_device_selected=notify.notify_microphone_selected,
                on_device_resolved=self._device_resolved,
                on_silent=self._microphone_silent,
            )
            rollback.append(("оркестратор", self.orchestrator.shutdown))
            self.supervisor = supervisor_factory(on_event=self._on_worker_event, use_qt=True)
            rollback.append(("воркер", self.supervisor.stop))
            self.hotkey.on_state = self._on_hotkey_state
            self.pill.on_cancel_clicked = lambda: self.orchestrator.cancel("pill")
            self.tray.on_cancel = lambda: self.orchestrator.cancel("tray")
            self.tray.on_copy_last = self._copy_last
            self.tray.on_model_recheck = self._recheck_model
            self.tray.on_quit = self._quit_requested
            self.guard.on_stop_recording = self.orchestrator.on_indicators_lost
            if not settings.pill_enabled:
                self.pill.set_enabled(False)
        except Exception:
            self._closed = True
            for step, action in reversed(rollback):
                self._cleanup(step, action)
            raise

    def _delete_build_children(self) -> None:
        """Удаляет и те QObject, чьи конструкторы не успели вернуть результат."""
        # Держим Python-обёртки живыми до удаления C++-объектов: GC цикла
        # runtime/Pill/View иначе может вызвать eventFilter уже мёртвой Pill.
        # Удаление Pill также закрывает её X11 и ставит удаление QQuickView.
        for child in reversed(self.children()):
            self._cleanup("дочерний объект Qt", partial(sip.delete, child))

    @property
    def phase(self) -> DictationPhase:
        """Текущая фаза оркестратора."""
        return self.orchestrator.phase

    @property
    def last_text(self) -> str | None:
        """Последняя фраза в памяти; не передаётся журналу или уведомлениям."""
        return self.orchestrator.last_text

    def set_revoked_check(self, revoked: RevokedCheck | None) -> None:
        """Подключает проверку отзыва ревизии по каталогу (US-6.6)."""
        self._revoked_check = revoked

    def _resolve_model_request(self) -> dict[str, Any] | None:
        """Запрос загрузки с учётом отзыва; ModelRevoked пробрасывается выше."""
        return resolve_model_request(
            self.settings,
            self.model_store,
            store_dir=paths.model_store_dir(),
            revoked=self._revoked_check,
        )

    def _send(self, message: dict[str, Any], *, timeout: float | None = None) -> None:
        """Адаптирует возвращаемое значение супервизора к порту команд."""
        self.supervisor.send(message, timeout=timeout)

    def start_level_monitor(self, device: str, callback: LevelCallback) -> bool:
        """Измерение уровня не зависит от модели и её самопроверки."""
        if self._closed or not self._started or self.supervisor.state != "running":
            callback(MicrophoneLevelUpdate("error", message=LEVEL_FAILED))
            return False
        if self._pending_test is not None:
            callback(MicrophoneLevelUpdate("error", message=TEST_BUSY))
            return False
        return self.orchestrator.start_level_monitor(device, callback)

    def stop_level_monitor(self) -> None:
        """Освобождает микрофон без распознавания."""
        self.orchestrator.stop_level_monitor()

    def start_test(self, device: str, callback: TestCallback) -> bool:
        """Запускает частную проверку; новую модель мастера сначала загружает."""
        if (
            self._pending_test is not None
            or self.orchestrator.test_active
            or self.orchestrator.level_active
            or self.phase not in (DictationPhase.IDLE, DictationPhase.FINISHING)
        ):
            callback(MicrophoneTestUpdate("error", message=TEST_BUSY))
            return False
        if self._closed or not self._started:
            callback(MicrophoneTestUpdate("error", message=TEST_MODEL_UNAVAILABLE))
            return False
        try:
            request = self._resolve_model_request()
        except Exception:
            request = None
        same_model = request == self._model_load_request
        if request is None or (self._selfcheck == "failed" and same_model):
            callback(MicrophoneTestUpdate("error", message=TEST_MODEL_UNAVAILABLE))
            return False
        if self._selfcheck == "ok" and not self._loading_model and same_model:
            return self.orchestrator.start_test(device, callback)
        # После установки на шаге 2 модель ещё может отсутствовать в живом воркере.
        # Сохраняем только выбранное устройство и адрес получателя, без речи.
        self._pending_test = (device, callback)
        self._cancel_preparing_timer()
        self._preparing_timer = self.schedule(PREPARING_WATCHDOG_MS, self._preparing_watchdog)
        callback(MicrophoneTestUpdate("preparing", message=TEST_PREPARING))
        if self._pending_test is not None and (not self._loading_model or not same_model):
            try:
                self.restart_worker(wait_for_model=True)
            except Exception:
                self._fail_pending_test()
        return True

    def stop_test(self) -> None:
        """Остановка не зависит от загрузки модели и блокировок начала диктовки."""
        self._cancel_preparing_timer()
        if self._pending_test is not None:
            _, callback = self._pending_test
            self._pending_test = None
            callback(MicrophoneTestUpdate("idle"))
        else:
            self.orchestrator.stop_test()

    def cancel_test(self) -> None:
        """Отменяет проверку при уходе с её экрана."""
        self._cancel_preparing_timer()
        if self._pending_test is not None:
            self.stop_test()
        else:
            self.orchestrator.cancel_test()

    def reload_model(self) -> None:
        """Перезапускает воркер, если текущая модель ещё не загружена и проверена."""
        try:
            request = self._resolve_model_request()
        except Exception:
            request = None
        if (
            request is not None
            and request == self._model_load_request
            and self._selfcheck == "ok"
            and not self._loading_model
        ):
            log.info("Модель уже загружена и проверена; перезапуск воркера не требуется")
            return
        self.restart_worker(wait_for_model=True)

    def _fail_pending_test(self) -> None:
        self._cancel_preparing_timer()
        pending, self._pending_test = self._pending_test, None
        if pending is not None:
            pending[1](MicrophoneTestUpdate("error", message=TEST_MODEL_UNAVAILABLE))

    def _cancel_preparing_timer(self) -> None:
        timer, self._preparing_timer = self._preparing_timer, None
        if timer is not None:
            self.cancel_timer(timer)

    def _preparing_watchdog(self) -> None:
        # schedule уже удалил сработавший таймер из набора и сам освободит его.
        self._preparing_timer = None
        self._fail_pending_test()

    def _recheck_model(self) -> None:
        """Жест пользователя начинает новую серию проверок после запрета."""
        if self._closed or self._selfcheck != "failed":
            return
        self.hotkey.fsm.escape(monotonic())
        self.pill.show_state(PillState.LOADING_MODEL)
        self.tray.set_state(TrayState.NOKEY if self._regrab_timer else TrayState.IDLE)
        try:
            self.restart_worker()
        except Exception:
            self._selfcheck = "running"
            self._selfcheck_attempts = 1
            self._finish_selfcheck("worker-error")

    def restart_worker(
        self, *, retry_selfcheck: bool = False, wait_for_model: bool = False
    ) -> None:
        """Заменяет супервизор, сохраняя уникальность поколений между заменами.

        wait_for_model закрывает вход в диктовку до hello и загрузки новой модели.
        Пользовательский перезапуск сбрасывает ошибки загрузки и самопроверки;
        retry_selfcheck сохраняет бюджет автоматической повторной попытки.
        """
        if self._closed:
            return
        # После пользовательского сброса хоткей закрыт уже до первого hello.
        self._loading_model = wait_for_model or retry_selfcheck or self._selfcheck == "failed"
        self._reset_selfcheck(retry=retry_selfcheck)
        generation = self.supervisor.generation + 1
        self.supervisor.stop()
        self.supervisor = self._supervisor_factory(on_event=self._on_worker_event, use_qt=True)
        self.supervisor.generation = generation
        self._model_load_generation = None
        if not retry_selfcheck:
            self._model_load_failures = 0
        self.supervisor.start()

    def _model_revoked(self) -> None:
        """Отозванную ревизию не грузим: показываем состояние и зовём поставить другую."""
        self._loading_model = False
        if self._selfcheck == "retrying":
            self._finish_selfcheck("load-failed")
            return
        if self.orchestrator.phase == DictationPhase.IDLE:
            self.pill.show_state(PillState.ERROR, text=ERROR_MODEL_REVOKED)
        self.tray.set_state(TrayState.ERROR)
        if not self._revoked_notified:
            self._revoked_notified = True
            notify.notify_model_revoked()

    def _load_model(self) -> None:
        """Загружает настроенную модель при каждом запуске нового воркера."""
        if self._model_load_generation == self.supervisor.generation:
            return
        if self._selfcheck == "failed":
            # После провала новую серию разрешает только жест пользователя.
            return
        self._reset_selfcheck(retry=self._selfcheck == "retrying")
        self._loading_model = False
        if self._model_load_failures >= 2:
            self._fail_pending_test()
            log.warning("Загрузка модели остановлена после двух неудачных попыток подряд")
            self.pill.show_state(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
            self.tray.set_state(TrayState.ERROR)
            return
        try:
            request = self._resolve_model_request()
        except ModelRevoked:
            self._fail_pending_test()
            self._model_revoked()
            return
        if request is None:
            self._fail_pending_test()
            log.info("модель в настройках не указана")
            if self._selfcheck == "retrying":
                self._finish_selfcheck("load-failed")
            return
        try:
            ipc.encode(request)
            if request["threads"] < 1 or request["min_ram_mb"] < 1:
                raise ValueError
        except (ipc.FrameError, TypeError, ValueError):
            self._fail_pending_test()
            self._model_load_failures += 1
            if self._selfcheck == "retrying":
                self._finish_selfcheck("load-failed")
                return
            log.warning("параметры модели заданы неверно")
            self.pill.show_state(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
            self.tray.set_state(TrayState.ERROR)
            return
        self._loading_model = True
        self._model_load_generation = self.supervisor.generation
        self._model_load_request = request
        if self.orchestrator.phase == DictationPhase.IDLE:
            self.pill.show_state(PillState.LOADING_MODEL)
        try:
            self.supervisor.send(request, timeout=10.0)
        except Exception:
            self._loading_model = False
            self._fail_pending_test()
            self._model_load_failures += 1
            if self._selfcheck == "retrying":
                self._finish_selfcheck("load-failed")
                return
            log.warning("Не удалось отправить запрос загрузки модели")
            self.pill.show_state(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
            self.tray.set_state(TrayState.ERROR)

    def _reset_selfcheck(self, *, retry: bool = False) -> None:
        """Снимает сторожа и сведения о предыдущей загрузке."""
        self._cancel_selfcheck_timer()
        self._selfcheck = "retrying" if retry else "idle"
        self.tray.set_model_recheck_enabled(False)
        if retry:
            return
        self._selfcheck_attempts = 0
        self._loaded_model = {}
        self._model_load_ms = None

    def _cancel_selfcheck_timer(self) -> None:
        timer, self._selfcheck_timer = self._selfcheck_timer, None
        if timer is not None:
            self.cancel_timer(timer)

    def _model_ready(self) -> None:
        """Объявляет готовность только после успешной проверки."""
        self.tray.set_model_recheck_enabled(False)
        self._loading_model = False
        self._model_load_failures = 0
        if self.orchestrator.phase == DictationPhase.IDLE:
            # Нажатие во время проверки могло оставить автомат в PROCESSING.
            self.hotkey.fsm.escape(monotonic())
            self.pill.hide()
            self.tray.set_state(TrayState.NOKEY if self._regrab_timer else TrayState.IDLE)
        if self._model_load_ms is not None:
            log.info("модель загружена за %.0f мс", self._model_load_ms)
        else:
            log.info("модель загружена")
        self._cancel_preparing_timer()
        pending, self._pending_test = self._pending_test, None
        if pending is not None:
            self.orchestrator.start_test(*pending)

    def _start_selfcheck(self, event: dict[str, Any]) -> None:
        """Запускает эталон, сохраняя только сведения о модели и времени загрузки."""
        self._selfcheck = "running"
        if self._selfcheck_attempts == 0:
            self._selfcheck_attempts = 1
        self._loaded_model = {
            target: value if isinstance(value := event.get(source), str) else ""
            for target, source in (
                ("model_id", "id"),
                ("revision", "revision"),
                ("engine_version", "engine_version"),
            )
        }
        load_ms = event.get("load_ms")
        self._model_load_ms = (
            load_ms if isinstance(load_ms, (int, float)) and not isinstance(load_ms, bool) else None
        )
        wav = smoke_wav_path()
        if not wav.is_file():
            log.warning("эталон самопроверки не найден")
            self._finish_selfcheck("no-wav")
            return
        self._selfcheck_timer = self.schedule(SELFCHECK_WATCHDOG_MS, self._selfcheck_watchdog)
        try:
            self.supervisor.send(
                {"type": "transcribe.file", "path": str(wav)}, timeout=SELFCHECK_TIMEOUT_S
            )
        except Exception:
            self._finish_selfcheck("worker-error")

    def _selfcheck_watchdog(self) -> None:
        # schedule уже удалил сработавший таймер из набора и сам освободит его.
        self._selfcheck_timer = None
        self._finish_selfcheck("timeout")

    def _finish_selfcheck(self, reason: str) -> None:
        """Завершает попытку; таймаут и отмена допускают один повтор без текста."""
        if self._closed or self._selfcheck not in ("running", "retrying"):
            return
        self._cancel_selfcheck_timer()
        retry = reason in ("timeout", "cancelled") and self._selfcheck_attempts == 1
        self._selfcheck = "retrying" if retry else "ok" if reason == "ok" else "failed"
        log_outcome = log.info if reason == "ok" else log.warning
        log_outcome(
            "самопроверка модели: причина=%s, попытка=%d, повтор=%s",
            reason,
            self._selfcheck_attempts,
            retry,
        )
        try:
            self.stats.append(
                "model_selfcheck",
                **self._loaded_model,
                result="ok" if reason == "ok" else "fail",
                cpu_model=_cpu_model(),
            )
        except Exception:
            log.warning("Не удалось сохранить статистику самопроверки")
        if retry:
            self._selfcheck_attempts = 2
            self._loading_model = True
            # IPC резервирует id "file" до конца поколения. Новый воркер также
            # исключает поздний ответ первой попытки; счётчик сохраняется до hello.
            if self.supervisor.generation == self._model_load_generation:
                try:
                    self.restart_worker(retry_selfcheck=True)
                except Exception:
                    self._finish_selfcheck("worker-error")
            # При load-timeout супервизор уже заменил процесс до доставки ошибки.
            return
        if reason == "ok":
            self._model_ready()
        else:
            self._loading_model = False
            self._fail_pending_test()
            self.tray.set_model_recheck_enabled(True)
            if reason == "cancelled":
                return
            self.tray.set_state(TrayState.ERROR)
            self.pill.show_state(PillState.ERROR, text=ERROR_SELFCHECK_FAILED)
            if reason in {"load-failed", "worker-error", "no-wav", "timeout"}:
                notify.notify_engine_failed()
            else:
                notify.notify_selfcheck_failed()

    def _on_worker_event(self, event: dict[str, Any]) -> None:
        """Обрабатывает загрузку модели и передаёт исходное событие оркестратору."""
        if (
            not self._closed
            and event.get("generation") == self.supervisor.generation
            and event.get("type") == "error"
            and event.get("code") in ("worker-start", "worker-crashed", "restart-limit")
        ):
            self._fail_pending_test()
        if event.get("utterance_id") == "file" or event.get("request_type") == "transcribe.file":
            if (
                not self._closed
                and self._selfcheck == "running"
                and event.get("generation") == self._model_load_generation
            ):
                if event.get("type") == "result":
                    if event.get("generation") == self.supervisor.generation:
                        text = event.get("text")
                        self._finish_selfcheck(
                            "ok" if isinstance(text, str) and smoke_matches(text) else "no-match"
                        )
                elif event.get("type") == "cancelled":
                    if event.get("generation") == self.supervisor.generation:
                        self._finish_selfcheck("cancelled")
                elif event.get("type") == "error":
                    self._finish_selfcheck(
                        "timeout"
                        if event.get("code") in ("timeout", "load-timeout")
                        else "worker-error"
                    )
            return
        if (
            not self._closed
            and self._selfcheck == "retrying"
            and event.get("type") == "error"
            and event.get("generation") == self.supervisor.generation
            and event.get("code") in ("worker-start", "worker-crashed", "restart-limit")
        ):
            self._finish_selfcheck("worker-error")
            return
        if (
            not self._closed
            and event.get("type") == "hello"
            and event.get("generation") == self.supervisor.generation
        ):
            self._load_model()
        if (
            not self._closed
            and self._loading_model
            and event.get("generation") == self._model_load_generation
        ):
            if event.get("type") == "model.loaded":
                if (
                    self._selfcheck in ("idle", "retrying")
                    and event.get("generation") == self.supervisor.generation
                ):
                    self._start_selfcheck(event)
            elif event.get("type") == "error" and (
                event.get("request_type") == "model.load"
                or event.get("response_type") == "model.loaded"
            ):
                self._loading_model = False
                self._fail_pending_test()
                self._model_load_failures += 1
                if self._selfcheck == "retrying":
                    self._finish_selfcheck("load-failed")
                    return
                log.warning("Не удалось загрузить модель")
                if self.orchestrator.phase == DictationPhase.IDLE:
                    self.pill.show_state(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
                    self.tray.set_state(TrayState.ERROR)
        self.orchestrator.on_worker_event(event)

    def _on_hotkey_state(self, state: HotkeyState, reason: str) -> None:
        """Откладывает диктовку, пока воркер загружает модель."""
        if self._pending_test is not None:
            if state in (HotkeyState.RECORDING, HotkeyState.PROCESSING):
                self.hotkey.fsm.escape(monotonic())
            return
        if not self._closed and self._selfcheck == "failed" and state == HotkeyState.RECORDING:
            if self.orchestrator.phase == DictationPhase.IDLE:
                self.pill.show_state(PillState.ERROR, text=ERROR_MODEL_NOT_LOADED)
            return
        if (
            not self._closed
            and state == HotkeyState.RECORDING
            and reason == "press"
            and self._model_load_failures >= 2
            and not self._loading_model
            and self.orchestrator.phase == DictationPhase.IDLE
        ):
            self._model_load_failures = 0
            self._model_load_generation = None
            self._load_model()
            return
        if (
            not self._closed
            and (self._loading_model or self._selfcheck == "retrying")
            and state == HotkeyState.RECORDING
        ):
            if self.orchestrator.phase == DictationPhase.IDLE:
                self.pill.show_state(PillState.LOADING_MODEL)
            return
        self.orchestrator.on_hotkey_state(state, reason)

    @property
    def has_volume_control(self) -> bool:
        """Есть ли в системе чем менять громкость микрофона."""
        return self.sound.has_volume_control

    @property
    def has_sound_settings(self) -> bool:
        """Есть ли в системе чем открыть панель настроек звука."""
        return self.sound.has_sound_settings

    @property
    def has_sound_service(self) -> bool:
        """Есть ли в системе чем перезапустить звуковую службу сеанса."""
        return self.sound.has_sound_service

    def microphone_state(self) -> MicrophoneState:
        """Состояние выбранного микрофона по звуковой службе, без его открытия."""
        return self.sound.microphone_state(self.settings.extra.get("device"))

    def raise_microphone_volume(self) -> bool:
        """Действие человека: включить звук микрофона и поднять громкость до полной."""
        raised = self.sound.raise_microphone(self.settings.extra.get("device"))
        if raised:
            self._mic_error_stat(recovered_by="raise_volume")
            self._announced_mic_problems.clear()
        return raised

    def open_sound_settings(self) -> bool:
        """Действие человека: открыть системную панель звука; ничего не меняет."""
        return self.sound.open_sound_settings()

    def restart_sound_service(self) -> bool:
        """Действие человека из «Отладки»: перезапустить звуковую службу сеанса."""
        return self.sound.restart_sound_service()

    def _microphone_silent(self) -> None:
        """Уточняет причину тишины по звуковой службе и называет её один раз."""
        if self._closed:
            return
        problem = self.microphone_state().problem
        self._mic_error_stat(kind="silent" if problem is None else problem.value)
        if problem is None:
            # Причина устранена (или неизвестна): следующая даст новое уведомление.
            self._announced_mic_problems.clear()
            return
        if problem in self._announced_mic_problems:
            return
        self._announced_mic_problems.add(problem)
        if problem is MicrophoneProblem.MUTED:
            notify.notify_microphone_muted()
        else:
            notify.notify_microphone_too_quiet()

    def _mic_error_stat(self, *, kind: str = "silent", recovered_by: str = "none") -> None:
        try:
            self.stats.append("mic_error", kind=kind, recovered_by=recovered_by)
        except Exception:
            log.warning("Не удалось сохранить статистику микрофона")

    def _device_resolved(self, name: str) -> None:
        self._resolved_device = name
        if self._on_device_resolved is not None:
            self._on_device_resolved(name)

    def subscribe_device_resolved(self, callback: Callable[[str], None] | None) -> str:
        """Заменяет подписчика и возвращает последнее известное имя микрофона."""
        self._on_device_resolved = callback
        return self._resolved_device

    def apply_pill_enabled(self, value: bool) -> None:
        """Меняет видимость индикатора и соответствующее состояние трея."""
        self.pill.set_enabled(value)
        self.tray.set_pill_enabled(value)

    def apply_hotkey(self, combo: str, mode: str) -> str:
        """Освобождает прежний хоткей и захватывает сохранённый без перезапуска."""
        try:
            hotkey_mode = HotkeyMode(mode)
        except ValueError:
            hotkey_mode = HotkeyMode.PTT
        self.settings.hotkey = combo
        self.settings.hotkey_mode = hotkey_mode.value
        self.hotkey.ungrab()
        result = self._grab_hotkey()
        if not result.ok:
            self.tray.set_state(TrayState.NOKEY)
            self._start_regrab(result.code)
        else:
            self._stop_regrab()
            if self.orchestrator.phase == DictationPhase.IDLE and self._selfcheck != "failed":
                self.tray.set_state(TrayState.IDLE)
        return result.code

    def _grab_hotkey(self) -> GrabResult:
        """Проверяет сочетание перед каждым захватом, включая старт и повторы."""
        if not is_valid_combo(self.settings.hotkey):
            log.warning("hotkey=%r недопустим, беру значение по умолчанию", self.settings.hotkey)
            self.settings.hotkey = Settings.hotkey
        return self.hotkey.grab(self.settings.hotkey, HotkeyMode(self.settings.hotkey_mode))

    def _start_regrab(self, code: str) -> None:
        """Повторяет захват; новая настройка начинает собственный отсчёт попыток."""
        if self._closed:
            return
        target = (self.settings.hotkey, self.settings.hotkey_mode)
        if self._regrab_timer is not None and target == self._regrab_target:
            return
        self._stop_regrab()
        self._regrab_target = target
        self._regrab_attempts = 0
        self._regrab_code = code
        timer = self._create_timer()
        self._regrab_timer = timer
        timer.setSingleShot(False)
        timer.timeout.connect(self._retry_hotkey)
        timer.start(REGRAB_INTERVAL_MS)

    def _stop_regrab(self) -> None:
        """Отменяет повтор и освобождает таймер."""
        timer, self._regrab_timer = self._regrab_timer, None
        self._regrab_target = None
        if timer is not None:
            self._cleanup("остановка таймера перезахвата", timer.stop)
            self._cleanup("удаление таймера перезахвата", timer.deleteLater)

    def _retry_hotkey(self) -> None:
        """Неудачи молчат; журнал отмечает только смену кода результата."""
        if self._closed or self._regrab_timer is None:
            return
        self._regrab_attempts += 1
        result = self._grab_hotkey()
        if result.ok:
            self._stop_regrab()
            if self.orchestrator.phase == DictationPhase.IDLE and self._selfcheck != "failed":
                self.tray.set_state(TrayState.IDLE)
            notify.notify_hotkey_regrabbed(self.settings.hotkey)
            log.info("Горячая клавиша снова захвачена: %s", self.settings.hotkey)
            self._record_hotkey_grab("regrabbed", self._regrab_attempts)
        elif result.code != self._regrab_code:
            log.info("Повторный захват горячей клавиши: %s → %s", self._regrab_code, result.code)
        self._regrab_code = result.code

    def _record_hotkey_grab(self, result: str, attempts: int) -> None:
        """Сбой статистики не мешает запуску и восстановлению клавиши."""
        try:
            self.stats.append("hotkey_grab", key_role="text", result=result, attempts=attempts)
        except Exception:
            log.warning("Не удалось сохранить статистику захвата горячей клавиши")

    def apply_device(self, value: str | None) -> None:
        """value не нужен: record_params возьмёт устройство из настроек перед следующей записью.
        Сброс объявления даст «Микрофон: имя» вместо «Микрофон сменился» при следующем открытии.
        """
        self.orchestrator.reset_device_announcement()
        log.info("Устройство записи изменено; применяется со следующей записи")

    def record_params(self) -> dict[str, Any]:
        """Параметры записи; расширения настроек читаются перед каждой фразой."""
        # TODO(M5/M6): реализовать silence_db/insert; воркер их пока не поддерживает.
        return {
            "device": self.settings.extra.get("device"),
            "limit_s": RECORD_LIMIT_S,
        }

    def paste_mode(self) -> PasteMode:
        """Выбирает вставку или только публикацию в буфере обмена."""
        if self.settings.extra.get("paste_clipboard_only", False) is True:
            return PasteMode.CLIPBOARD_ONLY
        return PasteMode.AUTO

    def _create_timer(self) -> QTimer:
        """Создаёт таймер с владельцем runtime; точка подмены для тестов."""
        return QTimer(self)

    def _create_notifier(self, fd: int) -> QSocketNotifier:
        """Создаёт наблюдатель чтения X11; точка подмены для тестов."""
        return QSocketNotifier(fd, QSocketNotifier.Read, self)

    def schedule(self, ms: int, callback: Callable[[], None]) -> QTimer:
        """Ставит отменяемый одноразовый таймер в текущем потоке GUI."""
        if self._closed:
            raise RuntimeError("Диктовка уже завершена")
        timer = self._create_timer()
        timer.setSingleShot(True)
        timer.setTimerType(Qt.PreciseTimer)
        self.timers.add(timer)

        def fire() -> None:
            if timer not in self.timers:
                return
            self.timers.remove(timer)
            try:
                if not self._closed:
                    callback()
            finally:
                timer.deleteLater()

        timer.timeout.connect(fire)
        timer.start(ms)
        return timer

    def cancel_timer(self, handle: object) -> None:
        """Останавливает доставку и освобождает ручку, включая отложенный повтор."""
        timer = cast(QTimer, handle)
        self.timers.discard(timer)
        try:
            timer.stop()
        finally:
            timer.deleteLater()

    def start(self) -> None:
        """Поднимает ресурсы один раз; недоступный хоткей не мешает работе UI."""
        if self._started or self._closed:
            return
        self._started = True
        for key in _NOTIFICATION_ACTIONS:
            notify.set_action_handler(key, self._show_requested)
        notify.set_action_handler(ACTION_OPEN_SOUND_SETTINGS, self._open_sound_settings_requested)
        atexit.register(self.restore_paste)
        self.x11.open()
        self.tray.start()
        self.supervisor.start()
        result = self._grab_hotkey()
        self._record_hotkey_grab("ok" if result.ok else "busy", 1)
        if not result.ok:
            self.tray.set_state(TrayState.NOKEY)
            self._start_regrab(result.code)
            notify.notify_hotkey_not_grabbed(self.settings.hotkey)
            try:
                candidates = self.hotkey.free_candidates(list(DEFAULT_CANDIDATES))
                log.info("Хоткей не захвачен (%s); свободные варианты: %s", result.code, candidates)
            except Exception:
                log.warning("Не удалось проверить свободные сочетания клавиш")
        fd = self.hotkey.fileno()
        if fd >= 0:
            self.notifier = self._create_notifier(fd)
            self.notifier.activated.connect(self._process_hotkey)
        self.tick_timer = self._create_timer()
        self.tick_timer.timeout.connect(self._tick_hotkey)
        self.tick_timer.start(200)
        self.stats_timer = self._create_timer()
        self.stats_timer.timeout.connect(self._flush_stats)
        self.stats_timer.start(int(SAVE_INTERVAL_S * 1000))

    def _flush_stats(self) -> None:
        """Сохраняет события и без новых диктовок; сбой не останавливает таймер."""
        if not self._closed:
            try:
                self.stats.flush()
            except Exception:
                log.warning("Не удалось сохранить статистику")

    def begin_hotkey_capture(self) -> bool:
        """Единственная точка захвата клавиатуры полем комбинации (экран в M5).

        Сторож сам создаёт отдельное X-соединение в своём потоке. GUI к нему
        не обращается; будущий экран читает клавиши своим обычным путём.
        Повторный вызов при активном захвате не продлевает его срок.
        """
        if self._closed:
            return False
        if self._capture_watchdog is not None:
            if self._capture_watchdog.active:
                return True
            self.end_hotkey_capture()
        watchdog = self._capture_watchdog_factory()
        self._capture_watchdog = watchdog
        if watchdog.open():
            return True
        self.end_hotkey_capture()
        return False

    def end_hotkey_capture(self) -> None:
        """Завершает выбор комбинации и останавливает его сторож; идемпотентно."""
        watchdog, self._capture_watchdog = self._capture_watchdog, None
        if watchdog is not None:
            watchdog.close()

    def _process_hotkey(self, *args: object) -> None:
        """Передаёт готовность дескриптора менеджеру клавиш."""
        if not self._closed:
            self.hotkey.process_pending()

    def _tick_hotkey(self) -> None:
        """Даёт готовому автомату проверить предел длительности фразы."""
        if not self._closed:
            self.hotkey.fsm.tick(monotonic())

    def _copy_last(self) -> None:
        """Копирует нормализованную фразу в буфер Qt с пометкой secret (У59)."""
        text = self.last_text
        if text is not None and not publish_clipboard(text, session_kind=self.session_kind):
            log.warning("Не удалось скопировать последний текст в буфер обмена")

    def _quit_requested(self) -> None:
        """Передаёт запрос выхода владельцу приложения."""
        if self.on_quit_requested is not None:
            self.on_quit_requested()

    def _show_requested(self) -> None:
        """Передаёт действие уведомления владельцу главного окна."""
        if not self._closed and self.on_show_requested is not None:
            self.on_show_requested()

    def _open_sound_settings_requested(self) -> None:
        """Кнопка уведомления: открыть системную панель звука."""
        if not self._closed:
            self.sound.open_sound_settings()

    def _drain_worker_events(self) -> None:
        """Даёт IPC до 50 мс на освобождение микрофона и обрабатывает события Qt."""
        self.supervisor.pump(timeout=0.05)
        QCoreApplication.processEvents(QEventLoop.ExcludeUserInputEvents, 50)

    @staticmethod
    def _cleanup(step: str, action: Callable[[], object]) -> None:
        """Изолирует ошибку одного шага, не раскрывая содержимое исключения."""
        try:
            action()
        except Exception:
            log.warning("Ошибка завершения диктовки: %s", step)

    def shutdown(self) -> None:
        """Освобождает ресурсы в установленном порядке; повторный вызов пуст."""
        if self._closed:
            return
        self._closed = True
        self._cancel_preparing_timer()
        if self._pending_test is not None:
            self.stop_test()
        if self._started:
            for key in _NOTIFICATION_ACTIONS:
                notify.set_action_handler(key, None)
            notify.set_action_handler(ACTION_OPEN_SOUND_SETTINGS, None)
        self.on_show_requested = None
        self._cleanup("оркестратор", self.orchestrator.shutdown)
        self._cleanup("страж индикаторов", self.guard.stop)
        notifier = self.notifier
        if notifier is not None:
            self._cleanup("отключение наблюдателя", lambda: notifier.setEnabled(False))
            self._cleanup("удаление наблюдателя", notifier.deleteLater)
        if self.tick_timer is not None:
            self._cleanup("остановка таймера хоткея", self.tick_timer.stop)
            self._cleanup("удаление таймера хоткея", self.tick_timer.deleteLater)
        if self.stats_timer is not None:
            self._cleanup("остановка таймера статистики", self.stats_timer.stop)
            self._cleanup("удаление таймера статистики", self.stats_timer.deleteLater)
        self._stop_regrab()
        # Страховка для таймеров, если завершение оркестратора прервалось ошибкой.
        for timer in tuple(self.timers):
            self._cleanup("отложенный таймер", partial(self.cancel_timer, timer))
        self._selfcheck_timer = None
        self._cleanup("восстановление буфера обмена", self.restore_paste)
        if self._started:
            self._cleanup("обработчик atexit", partial(atexit.unregister, self.restore_paste))
        self._cleanup("освобождение микрофона", self._close_audio)
        self._cleanup("доставка команд воркеру", self._drain_if_running)
        self._cleanup("воркер", self.supervisor.stop)
        self._cleanup("статистика", self.stats.flush)
        self._cleanup("захват хоткея", self.hotkey.ungrab)
        self._cleanup("захват Escape", self._ungrab_escape)
        self._cleanup("сторож поля комбинации", self.end_hotkey_capture)
        self._cleanup("соединение хоткея", self._close_hotkey_backend)
        self._cleanup("трей", self.tray.stop)
        self._cleanup("основное соединение X11", self.x11.close)

    def _ungrab_escape(self) -> None:
        """Снимает временный захват даже после сбоя основного ungrab."""
        action = getattr(self.hotkey.backend, "ungrab_escape", None)
        if callable(action):
            action()

    def _close_hotkey_backend(self) -> None:
        """Закрывает отдельное соединение стандартного бэкенда, если оно есть."""
        action = getattr(self.hotkey.backend, "close", None)
        if callable(action):
            action()

    def _close_audio(self) -> None:
        """Отправляет освобождение микрофона только работающему воркеру."""
        if self.supervisor.state == "running":
            self.supervisor.send({"type": "audio.close"})

    def _drain_if_running(self) -> None:
        """Прокачивает события отдельно от send, чтобы его сбой не мешал stop."""
        if self.supervisor.state == "running":
            self._drain_worker_events()
