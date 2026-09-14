"""Проводка диктовки в цикл Qt; переходами состояний владеет оркестратор."""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from time import monotonic
from typing import Any, cast

from PyQt5.QtCore import QCoreApplication, QEventLoop, QObject, QSocketNotifier, Qt, QTimer
from PyQt5.QtWidgets import QApplication

from astra_voice.core.dictation import DictationOrchestrator, DictationPhase
from astra_voice.core.settings import Settings
from astra_voice.core.stats import Stats
from astra_voice.platform.hotkey import (
    DEFAULT_CANDIDATES,
    RECORD_LIMIT_S,
    HotkeyManager,
    HotkeyMode,
)
from astra_voice.platform.paste import PasteMode, PasteOutcome, paste_text
from astra_voice.platform.session import SessionKind
from astra_voice.platform.x11 import X11Display
from astra_voice.ui import notify
from astra_voice.ui.indicators import IndicatorGuard
from astra_voice.ui.pill import Pill
from astra_voice.ui.tray import Tray
from astra_voice.ui.tray_icons import TrayIconProvider, TrayState
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
        x11_factory: Callable[[], X11Display] = X11Display,
        guard_factory: Callable[..., IndicatorGuard] = IndicatorGuard,
        provider_factory: Callable[[SessionKind], TrayIconProvider] = TrayIconProvider,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.session_kind = session_kind
        self.on_quit_requested: Callable[[], None] | None = None
        self._started = False
        self._closed = False
        self.notifier: QSocketNotifier | None = None
        self.tick_timer: QTimer | None = None
        self.timers: set[QTimer] = set()
        self.x11 = x11_factory()
        self.hotkey = hotkey_factory()
        self.pill = pill_factory(session=session_kind, parent=self)
        self.provider = provider_factory(session_kind)
        self.tray = tray_factory(self.provider, hotkey=settings.hotkey, parent=self)
        self.guard = guard_factory(self.pill, self.tray, parent=self)
        self.stats = stats_factory()
        self.paste_func = paste_func
        self.orchestrator = DictationOrchestrator(
            send=self._send,
            generation=lambda: self.supervisor.generation,
            pill=self.pill,
            tray=self.tray,
            paste=self.paste_func,
            active_window=self.x11.active_window,
            schedule=self.schedule,
            cancel_timer=self.cancel_timer,
            hotkey_done=lambda: self.hotkey.fsm.done(monotonic()),
            set_recording=self.guard.set_recording,
            notify=notify.notify,
            paste_mode=self.paste_mode,
            record_params=self.record_params,
            stats=self.stats,
        )
        self.supervisor = supervisor_factory(
            on_event=self.orchestrator.on_worker_event, use_qt=True
        )
        self.hotkey.on_state = self.orchestrator.on_hotkey_state
        self.pill.on_cancel_clicked = lambda: self.orchestrator.cancel("pill")
        self.tray.on_cancel = lambda: self.orchestrator.cancel("tray")
        self.tray.on_copy_last = self._copy_last
        self.tray.on_quit = self._quit_requested
        self.guard.on_stop_recording = self.orchestrator.on_indicators_lost
        if not settings.pill_enabled:
            self.pill.set_enabled(False)

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

    def _process_hotkey(self, *args: object) -> None:
        """Передаёт готовность дескриптора менеджеру клавиш."""
        if not self._closed:
            self.hotkey.process_pending()

    def _tick_hotkey(self) -> None:
        """Даёт готовому автомату проверить предел длительности фразы."""
        if not self._closed:
            self.hotkey.fsm.tick(monotonic())

    def _copy_last(self) -> None:
        """Копирует фразу исключительно в буфер обмена Qt."""
        text = self.last_text
        if text is not None:
            QApplication.clipboard().setText(text)

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
        # Страховка для таймеров, если завершение оркестратора прервалось ошибкой.
        for timer in tuple(self.timers):
            self._cleanup("отложенный таймер", partial(self.cancel_timer, timer))
        self._cleanup("захват хоткея", self.hotkey.ungrab)
        self._cleanup("захват Escape", self._ungrab_escape)
        self._cleanup("соединение хоткея", self._close_hotkey_backend)
        self._cleanup("освобождение микрофона", self._close_audio)
        self._cleanup("доставка команд воркеру", self._drain_if_running)
        self._cleanup("воркер", self.supervisor.stop)
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
