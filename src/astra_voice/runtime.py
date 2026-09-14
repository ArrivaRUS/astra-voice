"""Проводка диктовки в цикл Qt; переходами состояний владеет оркестратор."""

from __future__ import annotations

import atexit
import logging
from collections.abc import Callable
from functools import partial
from time import monotonic
from typing import Any, cast

from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEventLoop, QObject, QSocketNotifier, Qt, QTimer

from astra_voice.core.capture_watchdog import CaptureFieldWatchdog
from astra_voice.core.dictation import DictationOrchestrator, DictationPhase
from astra_voice.core.model_request import ModelNotConfigured, build_model_load
from astra_voice.core.paths import data_dir
from astra_voice.core.settings import Settings
from astra_voice.core.stats import SAVE_INTERVAL_S, Stats
from astra_voice.platform.hotkey import (
    DEFAULT_CANDIDATES,
    RECORD_LIMIT_S,
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
from astra_voice.platform.x11 import X11Display
from astra_voice.ui import notify
from astra_voice.ui.indicators import IndicatorGuard
from astra_voice.ui.pill import ERROR_MODEL_LOAD_FAILED, Pill, PillState
from astra_voice.ui.tray import Tray
from astra_voice.ui.tray_icons import TrayIconProvider, TrayState
from astra_voice.worker import ipc
from astra_voice.worker.supervisor import WorkerSupervisor

log = logging.getLogger(__name__)


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
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.session_kind = session_kind
        self.on_quit_requested: Callable[[], None] | None = None
        self._started = False
        self._closed = False
        self._loading_model = False
        self._model_load_generation: int | None = None
        self._model_load_failures = 0
        self._supervisor_factory = supervisor_factory
        self._capture_watchdog_factory = capture_watchdog_factory
        self._capture_watchdog: CaptureFieldWatchdog | None = None
        self.notifier: QSocketNotifier | None = None
        self.tick_timer: QTimer | None = None
        self.stats_timer: QTimer | None = None
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
            )
            rollback.append(("оркестратор", self.orchestrator.shutdown))
            self.supervisor = supervisor_factory(on_event=self._on_worker_event, use_qt=True)
            rollback.append(("воркер", self.supervisor.stop))
            self.hotkey.on_state = self._on_hotkey_state
            self.pill.on_cancel_clicked = lambda: self.orchestrator.cancel("pill")
            self.tray.on_cancel = lambda: self.orchestrator.cancel("tray")
            self.tray.on_copy_last = self._copy_last
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

    def _send(self, message: dict[str, Any], *, timeout: float | None = None) -> None:
        """Адаптирует возвращаемое значение супервизора к порту команд."""
        self.supervisor.send(message, timeout=timeout)

    def restart_worker(self) -> None:
        """Заменяет супервизор, сохраняя уникальность поколений между заменами."""
        if self._closed:
            return
        generation = self.supervisor.generation + 1
        self.supervisor.stop()
        self.supervisor = self._supervisor_factory(on_event=self._on_worker_event, use_qt=True)
        self.supervisor.generation = generation
        self._loading_model = False
        self._model_load_generation = None
        self._model_load_failures = 0
        self.supervisor.start()

    def _load_model(self) -> None:
        """Загружает настроенную модель при каждом запуске нового воркера."""
        if self._model_load_generation == self.supervisor.generation:
            return
        self._loading_model = False
        if self._model_load_failures >= 2:
            log.warning("Загрузка модели остановлена после двух неудачных попыток подряд")
            self.pill.show_state(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
            self.tray.set_state(TrayState.ERROR)
            return
        settings = self.settings.to_dict()
        model_dir = None
        if not settings.get("model_id") or not settings.get("model_revision"):
            model_dir = settings.get("model_dir")
        try:
            request = build_model_load(
                settings,
                model_dir=model_dir,
                store_dir=data_dir() / "store",
            )
        except ModelNotConfigured:
            log.info("модель в настройках не указана")
            return
        try:
            ipc.encode(request)
            if request["threads"] < 1 or request["min_ram_mb"] < 1:
                raise ValueError
        except (ipc.FrameError, TypeError, ValueError):
            self._model_load_failures += 1
            log.warning("параметры модели заданы неверно")
            self.pill.show_state(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
            self.tray.set_state(TrayState.ERROR)
            return
        self._loading_model = True
        self._model_load_generation = self.supervisor.generation
        if self.orchestrator.phase == DictationPhase.IDLE:
            self.pill.show_state(PillState.LOADING_MODEL)
        try:
            self.supervisor.send(request, timeout=10.0)
        except Exception:
            self._loading_model = False
            self._model_load_failures += 1
            log.warning("Не удалось отправить запрос загрузки модели")
            self.pill.show_state(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
            self.tray.set_state(TrayState.ERROR)

    def _on_worker_event(self, event: dict[str, Any]) -> None:
        """Обрабатывает загрузку модели и передаёт исходное событие оркестратору."""
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
                self._loading_model = False
                self._model_load_failures = 0
                if self.orchestrator.phase == DictationPhase.IDLE:
                    self.pill.hide()
                    self.tray.set_state(TrayState.IDLE)
                load_ms = event.get("load_ms")
                if isinstance(load_ms, (int, float)) and not isinstance(load_ms, bool):
                    log.info("модель загружена за %.0f мс", load_ms)
                else:
                    log.info("модель загружена")
            elif event.get("type") == "error" and (
                event.get("request_type") == "model.load"
                or event.get("response_type") == "model.loaded"
            ):
                self._loading_model = False
                self._model_load_failures += 1
                log.warning("Не удалось загрузить модель")
                if self.orchestrator.phase == DictationPhase.IDLE:
                    self.pill.show_state(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
                    self.tray.set_state(TrayState.ERROR)
        self.orchestrator.on_worker_event(event)

    def _on_hotkey_state(self, state: HotkeyState, reason: str) -> None:
        """Откладывает диктовку, пока воркер загружает модель."""
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
        if not self._closed and self._loading_model and state == HotkeyState.RECORDING:
            if self.orchestrator.phase == DictationPhase.IDLE:
                self.pill.show_state(PillState.LOADING_MODEL)
            return
        self.orchestrator.on_hotkey_state(state, reason)

    def record_params(self) -> dict[str, Any]:
        """Параметры записи; расширения настроек читаются перед каждой фразой."""
        return {
            "device": self.settings.extra.get("device"),
            "limit_s": RECORD_LIMIT_S,
            "silence_db": self.settings.extra.get("silence_db", -45.0),
            "insert": True,
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
        atexit.register(self.restore_paste)
        self.x11.open()
        self.tray.start()
        self.supervisor.start()
        result = self.hotkey.grab(self.settings.hotkey, HotkeyMode(self.settings.hotkey_mode))
        if not result.ok:
            self.tray.set_state(TrayState.NOKEY)
            notify.notify_hotkey_not_grabbed()
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
        # Страховка для таймеров, если завершение оркестратора прервалось ошибкой.
        for timer in tuple(self.timers):
            self._cleanup("отложенный таймер", partial(self.cancel_timer, timer))
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
