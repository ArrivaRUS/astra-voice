"""Состояния, сроки показа и мост к заранее загруженной QML-пилюле."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from enum import Enum
from functools import partial
from math import ceil
from time import perf_counter
from typing import Any, Final

from PyQt5.QtCore import QCoreApplication, QEventLoop, QObject, QPoint, QRect, Qt, QTimer, QUrl
from PyQt5.QtGui import QGuiApplication, QRegion, QScreen
from PyQt5.QtQuick import QQuickView

from astra_voice.core.paths import qml_dir
from astra_voice.platform.session import SessionKind
from astra_voice.platform.x11 import X11Display

log = logging.getLogger(__name__)

# Единственные допустимые причины ERROR: PRD §6/§9 A3, arch/plan-synth.md §7 О1,
# docs/plans.md M4 (У11/У40). Оркестрация импортирует эти константы.
ERROR_MICROPHONE_UNAVAILABLE: Final = "Микрофон недоступен"
ERROR_RECOGNITION_RESTARTED: Final = "Распознавание перезапущено"
ERROR_BUFFER_CLEARED: Final = "Буфер очищен"
ERROR_RECOGNITION_FAILED: Final = "Не удалось распознать"
ERROR_REASONS: Final = frozenset(
    (
        ERROR_MICROPHONE_UNAVAILABLE,
        ERROR_RECOGNITION_RESTARTED,
        ERROR_BUFFER_CLEARED,
        ERROR_RECOGNITION_FAILED,
    )
)


class PillState(Enum):
    HIDDEN = "hidden"
    LOADING_MODEL = "loading-model"
    LISTENING = "listening"
    LISTENING_SILENT = "listening-silent"
    LIMIT = "limit"
    PROCESSING = "processing"
    DONE = "done"
    CLIPBOARD_ONLY = "clipboard-only"
    EMPTY = "empty"
    CANCELLED = "cancelled"
    ERROR = "error"
    DISABLED = "disabled"


STATE_DURATION_MS: dict[PillState, int] = {
    PillState.DONE: 500,
    PillState.CLIPBOARD_ONLY: 1200,
    PillState.EMPTY: 1000,
    PillState.CANCELLED: 800,
    PillState.ERROR: 3000,
    PillState.LIMIT: 2000,
    PillState.LOADING_MODEL: 5000,
}

_LISTENING_STATES = (PillState.LISTENING, PillState.LISTENING_SILENT)
_INVISIBLE_STATES = (PillState.HIDDEN, PillState.DISABLED)
_HISTORY_SIZE = 9
# §8.2: тень 0 6px 18px; QML рисует её за границами корневого Item.
_SHADOW_MARGIN = 18
_SHADOW_OFFSET_Y = 6
_EDGE_OFFSET = 48
# §8.2, PillTheme.pillRadius (корневой QML Item не экспортирует этот токен).
_PILL_RADIUS = 18


class Pill(QObject):
    """Владеет одним окном; не принимает и не хранит распознанную речь.

    Выключение подменяет запрошенное состояние на DISABLED, сохраняя его срок.
    set_forced(True) снимает эту подмену: О4 требует немедленно показать текущую
    запись при потере значка трея даже поверх пользовательского выключения.
    Снятие форса снова применяет настройку. HIDDEN и явный DISABLED не показываются
    даже при форсе; истёкшие состояния не восстанавливаются при включении.
    """

    def __init__(
        self,
        *,
        session: SessionKind = SessionKind.KDE,
        qml_url: QUrl | None = None,
        view_factory: Callable[[], Any] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.on_cancel_clicked: Callable[[], None] | None = None
        self.on_details_clicked: Callable[[], None] | None = None
        self._session = session
        self._enabled = True
        self._forced = False
        self._requested_state = PillState.HIDDEN
        self._state = PillState.HIDDEN
        self._error_label = ""
        self._levels: deque[float] = deque([0.0] * _HISTORY_SIZE, maxlen=_HISTORY_SIZE)
        self._timer_generation = 0
        self._exposure_pending = False
        self._x11 = X11Display()
        self.destroyed.connect(self._x11.close)
        self._x11_started = False
        self._x11_retry_used = False
        self._has_compositor: bool | None = None
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.setInterval(0)
        self._show_timer.timeout.connect(self._after_show)
        self._above_timer = QTimer(self)
        self._above_timer.setInterval(1000)
        self._above_timer.timeout.connect(self._reassert_above)

        factory = view_factory if view_factory is not None else lambda: QQuickView()
        self._view = factory()
        # QWindow.setParent принимает QWindow; здесь нужно именно владение QObject.
        self.destroyed.connect(self._view.deleteLater)
        self._view.setFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
        )
        self._view.setColor(Qt.transparent)
        self._view.setResizeMode(QQuickView.SizeViewToRootObject)
        self._view.setSource(
            qml_url if qml_url is not None else QUrl.fromLocalFile(str(qml_dir() / "Pill.qml"))
        )
        self._root = self._view.rootObject()
        if self._root is None:
            raise RuntimeError("Не удалось загрузить QML пилюли")
        self._root.cancelClicked.connect(self._cancel_clicked)
        self._root.detailsClicked.connect(self._details_clicked)
        self._root.setProperty("x", _SHADOW_MARGIN)
        self._root.setProperty("y", _SHADOW_MARGIN)
        # Row пересчитывает ширину на этапе polish. Добавляем поля после встроенного
        # изменения размера QQuickView, иначе оно снова обрежет окно до размеров Item.
        self._root.widthChanged.connect(self._resize, Qt.QueuedConnection)
        self._root.heightChanged.connect(self._resize, Qt.QueuedConnection)
        self._view.widthChanged.connect(self._resize, Qt.QueuedConnection)
        self._view.heightChanged.connect(self._resize, Qt.QueuedConnection)
        self._view.visibleChanged.connect(self._visibility_changed)
        # QML и native window готовы заранее; X-соединение нужно только при показе.
        self._wid = int(self._view.winId())
        self._render()
        self._resize()

    def show_state(
        self, state: PillState, *, text: str | None = None, level: float | None = None
    ) -> None:
        started = perf_counter()
        self._timer_generation += 1
        self._requested_state = state
        self._error_label = ""
        if state is PillState.ERROR:
            self._error_label = ERROR_RECOGNITION_FAILED
            if text is not None and text in ERROR_REASONS:
                self._error_label = text
            elif text is not None:
                # Чужая строка не должна попасть ни в QML, ни в журнал.
                log.warning("Пилюля: причина вне реестра")
        if state not in _LISTENING_STATES:
            self._clear_levels()
        elif level is not None:
            self._levels.append(level)
        self._render(show_started=started)
        duration = STATE_DURATION_MS.get(state)
        if duration is not None:
            # singleShot нельзя остановить: поколение отменяет действие старого вызова.
            QTimer.singleShot(duration, partial(self._expire, self._timer_generation))

    def hide(self) -> None:
        self.show_state(PillState.HIDDEN)

    @property
    def visible(self) -> bool:
        if self._state in _INVISIBLE_STATES or not self._view.isVisible():
            return False
        if self._exposure_pending:
            # О4 читает visible синхронно после show(). Даём Qt один проход для
            # первого Expose, не подменяя подтверждение показа льготным True.
            # Снимаем флаг до processEvents: обработчики могут войти сюда повторно.
            self._exposure_pending = False
            QCoreApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        try:
            return (
                self._state not in _INVISIBLE_STATES
                and bool(self._view.isVisible())
                and bool(self._view.isExposed())
            )
        except (AttributeError, NotImplementedError):
            # Backend без подтверждения экспозиции (в т.ч. некоторые offscreen)
            # не может служить доказательством наличия индикатора для О4.
            return False

    @property
    def state(self) -> PillState:
        return self._state

    def set_enabled(self, value: bool) -> None:
        self._enabled = value
        self._render()

    def set_forced(self, value: bool) -> None:
        self._forced = value
        self._render()

    def _clear_levels(self) -> None:
        self._levels.clear()
        self._levels.extend([0.0] * _HISTORY_SIZE)

    def _render(self, *, show_started: float | None = None) -> None:
        state = self._requested_state if self._enabled or self._forced else PillState.DISABLED
        self._state = state
        if state not in _LISTENING_STATES:
            self._clear_levels()
        log.debug("Пилюля: %s", state.name)
        # Обе подписи очищаются и при скрытии, и при пользовательском выключении.
        self._root.setProperty("label", self._error_label if state is PillState.ERROR else "")
        self._root.setProperty("levels", list(self._levels))
        self._root.setProperty("avState", state.value)
        if state in _INVISIBLE_STATES:
            self._view.hide()
        else:
            self._prepare_x11()
            self._refresh_compositor()
            self._resize()
            self._view.show()
            if show_started is not None:
                elapsed_ms = (perf_counter() - show_started) * 1000
                log.debug("Пилюля: show_state → show() %.2f мс", elapsed_ms)

    def _expire(self, generation: int) -> None:
        if generation == self._timer_generation:
            # LIMIT через 2000 мс скрывается; PROCESSING включает зона оркестрации.
            self.hide()

    def _resize(self) -> None:
        self._view.resize(
            ceil(float(self._root.property("pillWidth"))) + _SHADOW_MARGIN * 2,
            ceil(float(self._root.property("pillHeight"))) + _SHADOW_MARGIN * 2 + _SHADOW_OFFSET_Y,
        )
        self._update_mask()
        self._place()

    def _active_screen(self) -> QScreen | None:
        try:
            active = self._x11.active_window()
            geometry = self._x11.window_geometry(active) if active else None
            if geometry is not None:
                x, y, width, height = geometry
                screen = QGuiApplication.screenAt(QPoint(x + width // 2, y + height // 2))
                if screen is not None:
                    return screen
        except Exception as exc:
            log.debug("Пилюля: экран активного окна недоступен: %s", exc)
        return QGuiApplication.primaryScreen()

    def _net_workarea(self) -> QRect | None:
        # TODO: перейти на platform/x11, когда соседняя зона добавит чтение _NET_WORKAREA
        if self._x11.d is None:
            return None
        try:
            from Xlib import Xatom

            conn = self._x11.d
            atom = conn.intern_atom("_NET_WORKAREA", only_if_exists=True)
            prop = self._x11.root.get_full_property(atom, Xatom.CARDINAL) if atom else None
            if prop is None or prop.format != 32 or len(prop.value) < 4:
                return None
            desktop_atom = conn.intern_atom("_NET_CURRENT_DESKTOP", only_if_exists=True)
            desktop = (
                self._x11.root.get_full_property(desktop_atom, Xatom.CARDINAL)
                if desktop_atom
                else None
            )
            index = 0
            if desktop is not None and desktop.format == 32 and len(desktop.value):
                index = int(desktop.value[0]) * 4
            if index < 0 or index + 4 > len(prop.value):
                return None
            x, y, width, height = (int(v) for v in prop.value[index : index + 4])
            return QRect(x, y, width, height) if width > 0 and height > 0 else None
        except Exception as exc:
            log.debug("Пилюля: _NET_WORKAREA недоступна: %s", exc)
            return None

    def _place(self) -> None:
        # Настройки верхнего края в контракте нет: все вызовы размещают снизу.
        screen = self._active_screen()
        if screen is None:
            return
        area = screen.availableGeometry()
        workarea = self._net_workarea()
        if workarea is not None:
            # EWMH публикует общую область рабочего стола, а не отдельного монитора.
            workarea = workarea.intersected(screen.geometry())
            if not workarea.isEmpty() and workarea != area:
                log.debug(
                    "Пилюля: availableGeometry расходится со struts, используем _NET_WORKAREA"
                )
                area = workarea
        y = (
            area.y()
            + area.height()
            - _EDGE_OFFSET
            - self._view.height()
            + _SHADOW_MARGIN
            + _SHADOW_OFFSET_Y
        )
        self._view.setPosition(
            area.x() + (area.width() - self._view.width()) // 2,
            y,
        )

    def _prepare_x11(self) -> None:
        if self._x11_started or QGuiApplication.platformName() != "xcb":
            return
        self._x11_started = True
        try:
            # offscreen/Wayland winId не является XID, даже если DISPLAY задан.
            if not self._x11.open():
                log.debug("Пилюля: X11 недоступен")
                return
            self._apply_ewmh()
            if self._session is SessionKind.FLY:
                # TODO: перейти на platform/x11
                self._set_cardinal_zero("_FLY_WM_WINDOW_MAP_ANIMATION")
                self._set_cardinal_zero("_FLY_WM_FADE_SHOW")
        except Exception as exc:
            self._lose_x11(exc)

    def _refresh_compositor(self) -> None:
        if self._x11.d is None:
            return
        try:
            atom = self._x11.d.intern_atom("_NET_WM_CM_S0")
            self._has_compositor = bool(self._x11.d.get_selection_owner(atom))
        except Exception as exc:
            self._lose_x11(exc)

    def _set_cardinal_zero(self, name: str) -> None:
        try:
            from Xlib import Xatom

            conn = self._x11.d
            if conn is not None:
                window = conn.create_resource_object("window", self._wid)
                window.change_property(conn.intern_atom(name), Xatom.CARDINAL, 32, [0])
                conn.sync()
        except Exception as exc:
            log.debug("Пилюля: не удалось выставить %s: %s", name, exc)

    def _apply_ewmh(self) -> None:
        if self._x11.d is None:
            return
        try:
            from Xlib import Xatom

            conn = self._x11.d
            window = conn.create_resource_object("window", self._wid)
            window.change_property(
                conn.intern_atom("_NET_WM_WINDOW_TYPE"),
                Xatom.ATOM,
                32,
                [conn.intern_atom("_NET_WM_WINDOW_TYPE_NOTIFICATION")],
            )
            self._set_states("_NET_WM_STATE_ABOVE")
            self._set_states("_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER")
            self._set_cardinal_zero("_NET_WM_USER_TIME")
        except Exception as exc:
            self._lose_x11(exc)

    def _set_states(self, *names: str) -> None:
        # TODO: перейти на platform/x11, когда EWMH-функции примут соединение.
        # Сейчас готовые функции открывают новое соединение на каждый вызов.
        from Xlib import X, Xatom, error
        from Xlib.protocol import event

        conn = self._x11.d
        window = conn.create_resource_object("window", self._wid)
        atom = conn.intern_atom("_NET_WM_STATE")
        states = [conn.intern_atom(name) for name in names]
        catcher = error.CatchError()
        if window.get_attributes().map_state == X.IsUnmapped:
            prop = window.get_full_property(atom, Xatom.ATOM)
            existing = [] if prop is None else list(prop.value)
            window.change_property(
                atom, Xatom.ATOM, 32, list(dict.fromkeys(existing + states)), onerror=catcher
            )
        else:
            message = event.ClientMessage(
                window=self._wid,
                client_type=atom,
                data=(32, [1, states[0], states[1] if len(states) > 1 else 0, 1, 0]),
            )
            self._x11.root.send_event(
                message,
                event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask,
                onerror=catcher,
            )
        conn.sync()
        if catcher.get_error() is not None:
            raise RuntimeError("X11 отклонил запрос состояния пилюли")

    def _lose_x11(self, exc: Exception) -> None:
        log.debug("Пилюля: X11 недоступен, re-assert приостановлен: %s", exc)
        self._x11.close()

    def _update_mask(self) -> None:
        if self._has_compositor is not False:
            if self._has_compositor is True:
                # Композитор мог включиться после предыдущего показа.
                self._view.setMask(QRegion())
            return
        width = ceil(float(self._root.property("pillWidth")))
        height = ceil(float(self._root.property("pillHeight")))
        radius = min(_PILL_RADIUS, width // 2, height // 2)
        diameter = radius * 2
        x = y = _SHADOW_MARGIN
        region = QRegion(x + radius, y, width - diameter, height)
        region |= QRegion(x, y + radius, width, height - diameter)
        for dx in (0, width - diameter):
            for dy in (0, height - diameter):
                region |= QRegion(x + dx, y + dy, diameter, diameter, QRegion.Ellipse)
        self._view.setMask(region)

    def _visibility_changed(self) -> None:
        self._exposure_pending = bool(self._view.isVisible())
        if self._view.isVisible():
            self._show_timer.start()
            self._above_timer.start()
        else:
            self._show_timer.stop()
            self._above_timer.stop()

    def _after_show(self) -> None:
        if self._state not in _INVISIBLE_STATES and self._view.isVisible():
            self._apply_ewmh()
            self._reassert_above()

    def _reassert_above(self) -> None:
        # Перекрытое окно тоже надо поднимать; isExposed здесь не является условием.
        if self._state in _INVISIBLE_STATES or not self._view.isVisible():
            return
        try:
            if self._x11.d is None:
                if not self._x11_started or self._x11_retry_used:
                    return
                # Одна попытка восстановления на следующем тике, без цикла открытия.
                self._x11_retry_used = True
                if not self._x11.open():
                    log.debug("Пилюля: повторное открытие X11 не удалось")
                    return
            from Xlib import X, error

            stack = self._x11.client_list_stacking()
            # client_list_stacking подавляет ошибки; sync обнаружит разрыв соединения.
            self._x11.d.sync()
            for wid in reversed(stack):
                if wid == self._wid:
                    return
                try:
                    window = self._x11.d.create_resource_object("window", wid)
                    if window.get_attributes().map_state != X.IsViewable:
                        continue
                except error.BadWindow as exc:
                    log.debug("Пилюля: окно стека недоступно: %s", exc)
                    continue
                try:
                    self._set_states("_NET_WM_STATE_ABOVE")
                finally:
                    self._view.raise_()
                return
        except Exception as exc:
            self._lose_x11(exc)

    def _cancel_clicked(self) -> None:
        if self.on_cancel_clicked is not None:
            self.on_cancel_clicked()

    def _details_clicked(self) -> None:
        if self.on_details_clicked is not None:
            self.on_details_clicked()
