"""Состояния, сроки показа и мост к заранее загруженной QML-пилюле."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from enum import Enum
from functools import partial
from math import ceil
from time import monotonic, perf_counter
from typing import Any, Final

from PyQt5.QtCore import QEvent, QMetaObject, QObject, QPoint, QRect, Qt, QTimer, QUrl
from PyQt5.QtGui import QGuiApplication, QRegion, QScreen
from PyQt5.QtQuick import QQuickView

from astra_voice.core.paths import qml_dir
from astra_voice.platform.session import SessionKind
from astra_voice.platform.x11 import X11Display

log = logging.getLogger(__name__)

# Единственные допустимые причины ERROR: PRD §6/§9 A3, arch/plan-synth.md §7 О1,
# docs/plans.md M4 (У11/У40). Оркестрация импортирует эти константы.
ERROR_MICROPHONE_UNAVAILABLE: Final = "Микрофон недоступен"
ERROR_MICROPHONE_CHANGED: Final = "Микрофон сменился"
ERROR_MICROPHONE_LOST: Final = "Микрофон отключился"
ERROR_RECOGNITION_RESTARTED: Final = "Распознавание перезапущено"
ERROR_BUFFER_CLEARED: Final = "Буфер очищен"
ERROR_RECOGNITION_FAILED: Final = "Не удалось распознать"
ERROR_MODEL_NOT_LOADED: Final = "Модель не загружена"
ERROR_MODEL_LOAD_FAILED: Final = "Не удалось загрузить модель"
ERROR_SELFCHECK_FAILED: Final = "Распознавание не работает"
ERROR_MODEL_REVOKED: Final = "Модель недоступна"
ERROR_REASONS: Final = frozenset(
    (
        ERROR_MICROPHONE_UNAVAILABLE,
        ERROR_MICROPHONE_CHANGED,
        ERROR_MICROPHONE_LOST,
        ERROR_RECOGNITION_RESTARTED,
        ERROR_BUFFER_CLEARED,
        ERROR_RECOGNITION_FAILED,
        ERROR_MODEL_NOT_LOADED,
        ERROR_MODEL_LOAD_FAILED,
        ERROR_SELFCHECK_FAILED,
        ERROR_MODEL_REVOKED,
    )
)

# Единственная допустимая уточняющая подпись CLIPBOARD_ONLY для оркестрации.
CLIPBOARD_WINDOW_CHANGED: Final = "Окно сменилось — текст в буфере"
CLIPBOARD_REASONS: Final = frozenset((CLIPBOARD_WINDOW_CHANGED,))


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
    PillState.LOADING_MODEL: 10000,
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
# Асинхронный map и смена override-redirect могут временно убрать экспозицию.
# Через секунду без Expose О4 снова требует честный False; насос событий запрещён (У54).
_EXPOSURE_WAIT_MS = 1000
_WINDOW_FLAGS = (
    Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
)


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
        self._label = ""
        self._levels: deque[float] = deque([0.0] * _HISTORY_SIZE, maxlen=_HISTORY_SIZE)
        self._timer_generation = 0
        self._exposed = False
        self._exposure_deadline: float | None = None
        self._remapping = False
        self._occluded = False
        self._compatibility_mode = False
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
        self._view.installEventFilter(self)
        # QWindow.setParent принимает QWindow; здесь нужно именно владение QObject.
        self.destroyed.connect(self._view.deleteLater)
        self._view.setFlags(_WINDOW_FLAGS)
        self._view.setColor(Qt.transparent)
        # SizeViewToRootObject запускает нулевой таймер и позже срезает поля до
        # размеров контента. Размером окна владеет Python; QML рисует по pillWidth/Height.
        self._view.setResizeMode(QQuickView.SizeRootObjectToView)
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
        # Запасной путь для изменений контента/экрана вне show_state. Каждый show()
        # сам синхронно завершает компоновку и задаёт размер, не ожидая этих сигналов.
        self._root.pillWidthChanged.connect(self._resize, Qt.QueuedConnection)
        self._root.pillHeightChanged.connect(self._resize, Qt.QueuedConnection)
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
        self._label = ""
        if state in (PillState.ERROR, PillState.CLIPBOARD_ONLY):
            reasons = ERROR_REASONS if state is PillState.ERROR else CLIPBOARD_REASONS
            if state is PillState.ERROR:
                self._label = ERROR_RECOGNITION_FAILED
            if text is not None and text in reasons:
                self._label = text
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
        """Экспозиция и кэш перекрытия: без X-запросов и обработки событий."""
        if self._state in _INVISIBLE_STATES or self._occluded:
            return False
        if self._exposure_deadline is not None and monotonic() < self._exposure_deadline:
            return True
        return bool(self._view.isVisible()) and self._exposed

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Запомнить экспозицию окна, пропуская события к самому окну."""
        if watched is self._view:
            if event.type() == QEvent.Expose:
                if not self._remapping:
                    self._exposure_deadline = None
                try:
                    self._exposed = bool(self._view.isVisible() and self._view.isExposed())
                except (AttributeError, NotImplementedError):
                    # Без подтверждения экспозиции окно не доказывает индикацию О4.
                    self._exposed = False
            elif event.type() == QEvent.Hide:
                self._exposed = False
                if not self._remapping:
                    self._exposure_deadline = None
        return False

    @property
    def state(self) -> PillState:
        return self._state

    @property
    def window_size(self) -> tuple[int, int]:
        """Фактический размер окна, без компоновки и обработки отложенных сигналов."""
        return int(self._view.width()), int(self._view.height())

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
        self._root.setProperty(
            "label", self._label if state in (PillState.ERROR, PillState.CLIPBOARD_ONLY) else ""
        )
        self._root.setProperty("levels", list(self._levels))
        self._root.setProperty("avState", state.value)
        if state in _INVISIBLE_STATES:
            self._exposed = False
            self._exposure_deadline = None
            self._view.hide()
        else:
            self._prepare_x11()
            self._update_compatibility_mode()
            self._refresh_compositor()
            self._show_window()
            if show_started is not None:
                elapsed_ms = (perf_counter() - show_started) * 1000
                log.debug("Пилюля: show_state → show() %.2f мс", elapsed_ms)

    def _show_window(self) -> None:
        if self._view.isVisible() and self._exposure_deadline is None:
            try:
                exposed = bool(self._view.isExposed())
            except (AttributeError, NotImplementedError):
                exposed = False
            if not exposed:
                # Внешний XUnmapWindow не сбрасывает Qt visible. show() без hide()
                # в таком состоянии ничего не делает (У56).
                self._hide_for_remap()
        if not self._view.isVisible():
            self._wait_for_exposure()
            # WM удаляет свойства при withdraw; они нужны перед каждым новым map.
            self._apply_ewmh()
        self._resize()
        self._view.show()

    def _wait_for_exposure(self) -> None:
        # Повторные show_state/set_forced не продлевают даже истёкшее ожидание.
        if self._exposure_deadline is None:
            self._exposure_deadline = monotonic() + _EXPOSURE_WAIT_MS / 1000

    def _hide_for_remap(self, *, compatibility: bool | None = None) -> None:
        self._wait_for_exposure()
        self._remapping = True
        try:
            self._exposed = False
            self._view.hide()
            if compatibility is not None:
                self._compatibility_mode = compatibility
                flags = _WINDOW_FLAGS
                if compatibility:
                    flags |= Qt.BypassWindowManagerHint
                self._view.setFlags(flags)
                # Backend может заменить native window при смене флагов.
                self._wid = int(self._view.winId())
        finally:
            self._remapping = False

    def _update_compatibility_mode(self) -> bool:
        """Перепоказ при смене fullscreen, сохраняя QQuickView и загруженный QML."""
        if self._x11.d is None:
            return False
        try:
            from Xlib import Xatom, error

            conn = self._x11.d
            active = self._x11.active_window()
            fullscreen = False
            if active and active != self._wid:
                try:
                    window = conn.create_resource_object("window", active)
                    prop = window.get_full_property(conn.intern_atom("_NET_WM_STATE"), Xatom.ATOM)
                except error.BadWindow:
                    # Активное окно исчезло между запросами; дождёмся следующего тика.
                    return False
                fullscreen = (
                    prop is not None
                    and prop.format == 32
                    and conn.intern_atom("_NET_WM_STATE_FULLSCREEN") in prop.value
                )
            if fullscreen == self._compatibility_mode:
                return False
            self._hide_for_remap(compatibility=fullscreen)
            return True
        except Exception as exc:
            self._lose_x11(exc)
            return False

    def _expire(self, generation: int) -> None:
        if generation == self._timer_generation:
            # LIMIT через 2000 мс скрывается; PROCESSING включает зона оркестрации.
            self.hide()

    def _resize(self) -> None:
        # В Qt 5 Item.polish() лишь планирует работу на кадр. QML forceLayout()
        # сначала завершает Text, затем Row: так pillWidth уже учитывает подпись,
        # иконку и кнопки текущего состояния. DirectConnection не качает события,
        # не ждёт первого кадра и не пересоздаёт заранее загруженную сцену.
        QMetaObject.invokeMethod(self._root, "forceLayout", Qt.DirectConnection)
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
            window.change_property(
                conn.intern_atom("_NET_WM_DESKTOP"), Xatom.CARDINAL, 32, [0xFFFFFFFF]
            )
            self._set_states("_NET_WM_STATE_ABOVE")
            self._set_states("_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER")
            self._set_states("_NET_WM_STATE_DEMANDS_ATTENTION", enabled=False)
            self._set_cardinal_zero("_NET_WM_USER_TIME")
            if self._session is SessionKind.FLY:
                self._set_cardinal_zero("_FLY_WM_WINDOW_MAP_ANIMATION")
                self._set_cardinal_zero("_FLY_WM_FADE_SHOW")
        except Exception as exc:
            self._lose_x11(exc)

    def _set_states(self, *names: str, enabled: bool = True) -> None:
        # TODO: перейти на platform/x11, когда EWMH-функции примут соединение.
        # Сейчас готовые функции открывают новое соединение на каждый вызов.
        from Xlib import X, Xatom, error
        from Xlib.protocol import event

        conn = self._x11.d
        window = conn.create_resource_object("window", self._wid)
        atom = conn.intern_atom("_NET_WM_STATE")
        states = [conn.intern_atom(name) for name in names]
        catcher = error.CatchError()
        if self._compatibility_mode or window.get_attributes().map_state == X.IsUnmapped:
            prop = window.get_full_property(atom, Xatom.ATOM)
            existing = [] if prop is None else list(prop.value)
            updated = (
                list(dict.fromkeys(existing + states))
                if enabled
                else [state for state in existing if state not in states]
            )
            window.change_property(atom, Xatom.ATOM, 32, updated, onerror=catcher)
        else:
            message = event.ClientMessage(
                window=self._wid,
                client_type=atom,
                data=(32, [int(enabled), states[0], states[1] if len(states) > 1 else 0, 1, 0]),
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
        if self._x11.d is not None:
            self._occluded = True  # После потери проверки нельзя подтверждать О4 старым кэшем.
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
        if self._view.isVisible():
            self._show_timer.start()
            self._above_timer.start()
        else:
            self._exposed = False
            if not self._remapping:
                self._exposure_deadline = None
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
                self._apply_ewmh()
            if self._update_compatibility_mode():
                self._show_window()
            if self._x11.d is None:
                return
            self._occluded = self._is_occluded()
            if self._occluded:
                self._set_states("_NET_WM_STATE_ABOVE")
                if self._compatibility_mode:
                    from Xlib import X

                    # WM не обрабатывает EWMH для override-redirect. Прямой restack
                    # не активирует окно и не создаёт DEMANDS_ATTENTION, в отличие от Qt.raise_().
                    window = self._x11.d.create_resource_object("window", self._wid)
                    window.configure(stack_mode=X.Above)
                    self._x11.d.sync()
                # Запрос ABOVE не доказывает видимость: проверяем фактический результат.
                self._occluded = self._is_occluded()
            self._set_states("_NET_WM_STATE_DEMANDS_ATTENTION", enabled=False)
        except Exception as exc:
            self._lose_x11(exc)

    def _is_occluded(self) -> bool:
        """Проверка только из re-assert; все координаты — физические пиксели X root."""
        from Xlib import X, error

        stack = self._x11.client_list_stacking()
        # Override-redirect не входит в список клиентов WM. Его порядок относительно
        # рамок managed-окон и других override-redirect берём из дерева X-сервера.
        if self._compatibility_mode or self._wid not in stack:
            stack = [int(window.id) for window in self._x11.root.query_tree().children]
        # client_list_stacking подавляет ошибки; sync обнаружит разрыв соединения.
        self._x11.d.sync()
        if self._wid not in stack:
            return True
        geometry = self._x11.window_geometry(self._wid)
        if geometry is None:
            return True
        pill_rect = QRect(*geometry)
        for wid in reversed(stack[stack.index(self._wid) + 1 :]):
            try:
                window = self._x11.d.create_resource_object("window", wid)
                attributes = window.get_attributes()
                if attributes.map_state != X.IsViewable or attributes.win_class == X.InputOnly:
                    continue
                geometry = self._x11.window_geometry(wid)
                if geometry is None or pill_rect.intersects(QRect(*geometry)):
                    return True
            except error.BadWindow as exc:
                log.debug("Пилюля: окно стека недоступно: %s", exc)
        return False

    def _cancel_clicked(self) -> None:
        if self.on_cancel_clicked is not None:
            self.on_cancel_clicked()

    def _details_clicked(self) -> None:
        if self.on_details_clicked is not None:
            self.on_details_clicked()
