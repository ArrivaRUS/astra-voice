"""Примитивы X11 через ``python3-xlib``, без зависимости от Qt.

Нужны для активации окна: KWin применяет защиту от кражи фокуса и в ответ на
``requestActivate()`` без свежего ``_NET_WM_USER_TIME`` лишь подсвечивает окно
(``_NET_WM_STATE_DEMANDS_ATTENTION``). Второй экземпляр берёт метку времени у
X-сервера и передаёт её первому, первый ставит её окну перед активацией.

``PyQt5.QtX11Extras`` в Astra не установлен, поэтому всё делается на Xlib.
Без Xlib, ``DISPLAY`` или при ошибке возвращается нейтральный результат.
Исключения разбора комбинаций описаны в ``X11Display.parse_combo``.
Соединения открываются только при явном вызове, отдельно от Qt.
"""

from __future__ import annotations

import logging
import math
import os
import time
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any

log = logging.getLogger(__name__)

TIMESTAMP_ATOM = "_ASTRA_VOICE_TIMESTAMP"
USER_TIME_ATOM = "_NET_WM_USER_TIME"
_WAIT_S = 1.0
_POLL_S = 0.005
# Ц2: из 500 мс цепочка вставки уже тратит 150 мс; ожидание ограничено 200 мс.
MODIFIER_RELEASE_TIMEOUT_S = 0.2
_WM_CLASS_MAX_DEPTH = 8
_WM_CLASS_MAX_NODES = 256


class BadCombo(Exception):
    """Комбинация неверна или её клавиши нет в раскладке."""


class X11Unavailable(Exception):
    """Операция требует доступного соединения X11."""


@dataclass
class ParsedCombo:
    """Модификаторы и клавиша в текущей раскладке X11."""

    mods: int
    keycode: int
    keysym_name: str


@dataclass
class GrabReport:
    """Результат захвата каждой маски; при ошибке захваты отменены."""

    ok: bool
    per_mask: list[tuple[int, str | None]]
    bad_access: bool


class X11Display:
    """Собственное соединение X11; конструктор не обращается к серверу.

    ``open`` возвращает успех. Контекстный менеджер открывает соединение
    по умолчанию, если оно ещё не открыто, и всегда закрывает на выходе.
    При неудаче открытия объект остаётся пригодным для безопасных вызовов.
    Очередь следует читать через ``pending_events``/``next_event``: они снимают
    просроченный захват. При бездействии соединения дедлайн не обслуживается.
    Потоковый таймер запрещён: Xlib не потокобезопасен, захват принадлежит соединению.
    """

    def __init__(self) -> None:
        self.d: Any = None
        self.root: Any = None
        self.lock_masks: dict[str, int] = {"Lock": 0, "Num_Lock": 0, "Scroll_Lock": 0}
        self.keyboard_grab_deadline: float | None = None
        self._error_count = 0
        self._last_error: str | None = None
        self._finalizer: weakref.finalize[[Any], X11Display] | None = None

    def _on_error(self, exc: Any, request: Any) -> int:
        """Подавляет stderr Xlib и учитывает асинхронную ошибку."""
        self._error_count += 1
        self._last_error = type(exc).__name__
        log.debug("ошибка X11: %s", exc)
        return 1

    def open(self, display_name: str | None = None) -> bool:
        """Открывает соединение; без Xlib/дисплея возвращает False."""
        self._enforce_keyboard_deadline()
        if self.d is not None:
            return True
        if not (display_name or os.environ.get("DISPLAY", "").strip()):
            log.debug("X11 недоступен: DISPLAY не задан")
            return False
        try:
            from Xlib import display as xdisplay

            self.d = xdisplay.Display(display_name)
            self._finalizer = weakref.finalize(self, self._close_connection, self.d)
            owner = weakref.ref(self)

            def on_error(exc: Any, request: Any) -> int:
                instance = owner()
                return instance._on_error(exc, request) if instance is not None else 1

            # Обработчик не удерживает владельца через соединение финализатора.
            self.d.set_error_handler(on_error)
            self.root = self.d.screen().root
            before = self._error_count
            self.lock_masks = self._read_lock_masks()
            self.d.sync()
            if self._error_count != before:
                self.close()
                return False
            return True
        except Exception as exc:
            log.debug("не удалось открыть X11: %s", exc)
            self.close()
            return False

    @staticmethod
    def _close_connection(conn: Any) -> None:
        """Освобождение при сборке объекта и штатном выходе процесса через atexit."""
        try:
            conn.ungrab_keyboard(0)  # X.CurrentTime; финализатору не нужны импорты.
            conn.sync()
        except Exception as exc:
            log.debug("не удалось снять захват при закрытии X11: %s", exc)
        finally:
            try:
                conn.close()
            except Exception as exc:
                log.debug("не удалось закрыть X11: %s", exc)

    def close(self) -> None:
        """Снимает захват клавиатуры и закрывает соединение; идемпотентно."""
        if self._finalizer is not None:
            self._finalizer()
            self._finalizer = None
        elif self.d is not None:
            self._close_connection(self.d)
        self.d = self.root = None
        self.keyboard_grab_deadline = None
        self.lock_masks = dict.fromkeys(self.lock_masks, 0)

    def __enter__(self) -> X11Display:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _require_display(self) -> Any:
        if self.d is None:
            raise X11Unavailable("соединение X11 не открыто")
        return self.d

    def _read_lock_masks(self) -> dict[str, int]:
        from Xlib import XK

        conn = self._require_display()
        symbols = {
            "Lock": {XK.string_to_keysym("Caps_Lock"), XK.string_to_keysym("Shift_Lock")},
            "Num_Lock": {XK.string_to_keysym("Num_Lock")},
            "Scroll_Lock": {XK.string_to_keysym("Scroll_Lock")},
        }
        masks = dict.fromkeys(symbols, 0)
        for index, codes in enumerate(conn.get_modifier_mapping()):
            for code in codes:
                if not code:
                    continue
                keysyms = set(conn.get_keyboard_mapping(int(code), 1)[0]) - {0}
                for name, wanted in symbols.items():
                    if keysyms & wanted:
                        masks[name] |= 1 << index
        return masks

    def mask_variants(self, base: int) -> list[int]:
        """Все сочетания Lock/Num/Scroll поверх base, без повторений."""
        # S4 README, «Находка: масок 4, а не 8»: Scroll Lock не замаплен.
        variants = [base]
        for mask in self.lock_masks.values():
            variants = list(dict.fromkeys(variants + [value | mask for value in variants]))
        return variants

    def parse_combo(self, combo: str) -> ParsedCombo:
        """Разбирает одну клавишу с модификаторами, как в S4.

        Неверный синтаксис/keysym/раскладка → BadCombo; нет соединения
        или ошибка Xlib → X11Unavailable. Enter можно назначить хоткеем,
        но синтезировать его нельзя.
        """
        self._enforce_keyboard_deadline()
        try:
            from Xlib import XK, X

            aliases = {
                "ctrl": X.ControlMask,
                "control": X.ControlMask,
                "shift": X.ShiftMask,
                "alt": X.Mod1Mask,
                "meta": X.Mod1Mask,
                "super": X.Mod4Mask,
                "win": X.Mod4Mask,
            }
            key_aliases = {"space": "space", "esc": "Escape", "escape": "Escape"}
            mods = 0
            key: str | None = None
            for part in combo.split("+"):
                part = part.strip()
                if not part:
                    raise BadCombo("пустая часть комбинации")
                if part.lower() in aliases:
                    mods |= aliases[part.lower()]
                elif key is None:
                    key = key_aliases.get(part.lower(), part)
                else:
                    raise BadCombo("в комбинации должна быть одна клавиша")
            if key is None:
                raise BadCombo("нет клавиши в комбинации")
            keysym = XK.string_to_keysym(key)
            if not keysym:
                raise BadCombo("неизвестный keysym")
            code = int(self._require_display().keysym_to_keycode(keysym))
            if not code:
                raise BadCombo("keysym отсутствует в текущей раскладке")
            return ParsedCombo(mods, code, key)
        except (BadCombo, X11Unavailable):
            raise
        except Exception as exc:
            log.debug("не удалось разобрать комбинацию X11: %s", exc)
            raise X11Unavailable("недоступна раскладка X11") from exc

    def grab_key(self, keycode: int, base_mask: int) -> GrabReport:
        """Захватывает все маски; любая ошибка вызывает полный откат."""
        self._enforce_keyboard_deadline()
        results: list[tuple[int, str | None]] = []
        for mask in self.mask_variants(base_mask):
            name: str | None = None
            try:
                from Xlib import X, error

                conn = self._require_display()
                # Нулевой keycode означал бы AnyKey: такой захват запрещён.
                if not 8 <= keycode <= 255 or not 0 <= mask <= 255:
                    raise BadCombo("недопустимый keycode или маска")
                catcher = error.CatchError(error.BadAccess)
                before = self._error_count
                self.root.grab_key(
                    keycode, mask, True, X.GrabModeAsync, X.GrabModeAsync, onerror=catcher
                )
                conn.sync()
                caught = catcher.get_error()
                if caught is not None:
                    name = type(caught).__name__
                elif self._error_count != before:
                    name = self._last_error
            except Exception as exc:
                name = type(exc).__name__
            if name is not None:
                log.debug("не удалось захватить клавишу X11: %s", name)
            results.append((mask, name))
        ok = all(name is None for _, name in results)
        if not ok:
            self.ungrab_key(keycode, base_mask)
        return GrabReport(ok, results, any(name == "BadAccess" for _, name in results))

    def ungrab_key(self, keycode: int, base_mask: int) -> None:
        """Снимает все варианты захвата, продолжая после ошибок X11."""
        self._enforce_keyboard_deadline()
        if not 8 <= keycode <= 255:
            return
        for mask in self.mask_variants(base_mask):
            if not 0 <= mask <= 255:
                continue
            try:
                conn = self._require_display()
                self.root.ungrab_key(keycode, mask)
                conn.sync()
            except Exception as exc:
                log.debug("не удалось снять захват клавиши X11: %s", exc)

    @contextmanager
    def keyboard_grab(
        self, window_id: int | None = None, timeout_s: float = 30.0
    ) -> Iterator[bool]:
        """Основной контракт поля захвата комбинации: ``with x.keyboard_grab() as ok``.

        Проверяйте ok перед чтением клавиш. Захват снимается в finally, включая
        исключение, потерю фокуса и закрытие поля при выходе из контекста.
        """
        grabbed = self.grab_keyboard(window_id, timeout_s)
        try:
            yield grabbed
        finally:
            if grabbed:
                self.ungrab_keyboard()

    def grab_keyboard(self, window_id: int | None = None, timeout_s: float = 30.0) -> bool:
        """Захват ТОЛЬКО для открытого поля комбинации (У21/T-24).

        Низкоуровневый примитив; поле использует ``keyboard_grab``.
        Срок до 30 секунд проверяется при следующих операциях соединения.
        Дополнительная защита — close и финализатор, в том числе при выходе.
        """
        self.ungrab_keyboard()
        try:
            from Xlib import X

            conn = self._require_display()
            if not math.isfinite(timeout_s) or timeout_s <= 0:
                return False
            if self.keyboard_grab_deadline is not None:
                return False
            window = (
                self.root if window_id is None else conn.create_resource_object("window", window_id)
            )
            deadline = time.monotonic() + min(timeout_s, 30.0)
            status = window.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
            if status != X.GrabSuccess:
                log.debug("сервер отклонил захват клавиатуры: %s", status)
                return False
            self.keyboard_grab_deadline = deadline
            return True
        except Exception as exc:
            log.debug("не удалось захватить клавиатуру X11: %s", exc)
            return False

    def keyboard_grab_expired(self) -> bool:
        """Истёк ли срок активного захвата; чистая проверка для диагностики."""
        return (
            self.keyboard_grab_deadline is not None
            and time.monotonic() >= self.keyboard_grab_deadline
        )

    def _enforce_keyboard_deadline(self) -> None:
        """Снимает просроченный захват в потоке, обслуживающем соединение."""
        if self.keyboard_grab_expired():
            self.ungrab_keyboard()

    def pending_events(self) -> int:
        """Число событий; одновременно обслуживает срок захвата клавиатуры."""
        self._enforce_keyboard_deadline()
        return int(self.d.pending_events()) if self.d is not None else 0

    def next_event(self) -> Any:
        """Следующее событие; вызывается после проверки pending_events."""
        self._enforce_keyboard_deadline()
        return self._require_display().next_event()

    def fileno(self) -> int:
        """Дескриптор для внешнего цикла событий, либо минус один."""
        self._enforce_keyboard_deadline()
        return int(self.d.fileno()) if self.d is not None else -1

    def refresh_keyboard_mapping(self, event: Any) -> bool:
        """Обновляет keycode и маски блокировок после MappingNotify."""
        self._enforce_keyboard_deadline()
        try:
            conn = self._require_display()
            before = self._error_count
            conn.refresh_keyboard_mapping(event)
            self.lock_masks = self._read_lock_masks()
            conn.sync()
            return self._error_count == before
        except Exception as exc:
            log.debug("не удалось обновить карту клавиатуры X11: %s", exc)
            return False

    def semantic_modifiers(self, mask: int) -> int:
        """Control/Shift/Mod1..Mod5 без Lock/Num/Scroll и кнопок мыши."""
        from Xlib import X

        meaningful = (
            X.ControlMask
            | X.ShiftMask
            | X.Mod1Mask
            | X.Mod2Mask
            | X.Mod3Mask
            | X.Mod4Mask
            | X.Mod5Mask
        )
        for lock in self.lock_masks.values():
            meaningful &= ~lock
        return int(mask & meaningful)

    def modifiers_held(self) -> bool:
        """Проверяет физические модификаторы и дополнительный сигнал маски X11.

        XKB может сбросить маску при переключении группы через Ctrl+Shift,
        хотя клавиши ещё зажаты. Биты keycode сохраняются до отпускания.
        Строки Lock/Num/Scroll исключаются по текущим маскам блокировок.
        """
        conn = self._require_display()
        mapping = conn.get_modifier_mapping()
        keymap = conn.query_keymap()
        physical = any(
            keymap[code // 8] & (1 << (code % 8))
            for index, codes in enumerate(mapping)
            if self.semantic_modifiers(1 << index)
            for code in codes
            if code
        )
        mask = int(self.root.query_pointer().mask)
        return physical or bool(self.semantic_modifiers(mask))

    def keys_held(self) -> bool:
        """Есть ли зажатые клавиши вне строк Lock/Num_Lock/Scroll_Lock (У50)."""
        from Xlib import X

        conn = self._require_display()
        lock_mask = X.LockMask
        for mask in self.lock_masks.values():
            lock_mask |= mask
        ignored = {
            int(code)
            for index, codes in enumerate(conn.get_modifier_mapping())
            if (1 << index) & lock_mask
            for code in codes
            if code
        }
        keymap = conn.query_keymap()
        return any(
            keymap[code // 8] & (1 << (code % 8)) for code in range(8, 256) if code not in ignored
        )

    def ungrab_keyboard(self) -> None:
        """Снимает захват идемпотентно; при ошибке сохраняет срок для повтора."""
        if self.keyboard_grab_deadline is None:
            return
        try:
            from Xlib import X

            conn = self._require_display()
            before = self._error_count
            conn.ungrab_keyboard(X.CurrentTime)
            conn.sync()
            if self._error_count == before:
                self.keyboard_grab_deadline = None
        except Exception as exc:
            log.debug("не удалось снять захват клавиатуры X11: %s", exc)

    def _safe_keycode(self, keycode: int) -> bool:
        """Отсекает Enter во всех группах раскладки, включая цифровой блок."""
        from Xlib import XK

        if not 8 <= keycode <= 255:
            return False
        forbidden = {XK.string_to_keysym(name) for name in ("Return", "KP_Enter", "ISO_Enter")}
        # ISO_Enter отсутствует в базовой таблице XK некоторых версий Xlib.
        forbidden.add(0xFE34)
        keysyms = set(self._require_display().get_keyboard_mapping(keycode, 1)[0]) - {0}
        return bool(keysyms) and not keysyms & forbidden

    def fake_key(self, keycode: int, press: bool) -> bool:
        """XTest нажатие/отпускание; Enter, отсутствие XTest или ошибка → False."""
        self._enforce_keyboard_deadline()
        try:
            from Xlib import X
            from Xlib.ext import xtest

            conn = self._require_display()
            if not conn.has_extension("XTEST") or not self._safe_keycode(keycode):
                log.debug("XTest недоступен или клавиша запрещена")
                return False
            before = self._error_count
            xtest.fake_input(conn, X.KeyPress if press else X.KeyRelease, keycode)
            conn.sync()
            return self._error_count == before
        except Exception as exc:
            log.debug("не удалось синтезировать клавишу X11: %s", exc)
            return False

    def send_combo(
        self,
        mod_keysym_names: list[str],
        key_keysym_name: str,
        timeout_s: float = MODIFIER_RELEASE_TIMEOUT_S,
    ) -> bool:
        """Модификаторы + одна клавиша; Enter/Return запрещён (У12).

        Проверяет всю комбинацию до ввода, отпускает клавиши в обратном
        порядке даже после ошибки. Ждёт отпускания всех клавиш, кроме строк
        Lock/Num_Lock/Scroll_Lock, не более timeout_s (максимум 200 мс);
        иначе не вводит ничего, в том числе при удержании клавиши хоткея (У50).
        Ошибка/нет XTest → False.
        Принят остаточный риск: крах между Ctrl↓ и Ctrl↑ оставляет логический
        Ctrl нажатым на XTest-устройстве; снимается первым живым нажатием Ctrl.
        """
        self._enforce_keyboard_deadline()
        if not math.isfinite(timeout_s) or timeout_s < 0:
            return False
        pressed: list[int] = []
        ok = True
        try:
            from Xlib import XK

            conn = self._require_display()
            modifiers = {
                "Shift_L",
                "Shift_R",
                "Control_L",
                "Control_R",
                "Alt_L",
                "Alt_R",
                "Meta_L",
                "Meta_R",
                "Super_L",
                "Super_R",
                "Hyper_L",
                "Hyper_R",
                "ISO_Level3_Shift",
                "Mode_switch",
            }
            if any(name not in modifiers for name in mod_keysym_names):
                log.debug("XTest: неверное имя модификатора")
                return False
            if key_keysym_name.lower() in {"enter", "return", "kp_enter", "iso_enter"}:
                log.debug("XTest: Enter запрещён")
                return False
            codes = []
            for name in [*mod_keysym_names, key_keysym_name]:
                symbol = XK.string_to_keysym(name)
                code = int(conn.keysym_to_keycode(symbol)) if symbol else 0
                if not self._safe_keycode(code):
                    log.debug("XTest: клавиша отсутствует или запрещена")
                    return False
                codes.append(code)
            codes = list(dict.fromkeys(codes))
            deadline = time.monotonic() + min(timeout_s, MODIFIER_RELEASE_TIMEOUT_S)
            while True:
                held = self.keys_held()
                remaining = deadline - time.monotonic()
                if remaining <= 0 and timeout_s > 0:
                    return False
                if not held:
                    break
                if remaining <= 0:
                    return False
                time.sleep(min(_POLL_S, remaining))
            for code in codes:
                # Даже при ошибке sync запрос нажатия мог дойти до сервера.
                pressed.append(code)
                if not self.fake_key(code, True):
                    ok = False
                    break
        except Exception as exc:
            log.debug("не удалось синтезировать комбинацию X11: %s", exc)
            ok = False
        finally:
            for code in reversed(pressed):
                if not self.fake_key(code, False):
                    ok = False
        return ok

    def _root_windows(self, name: str) -> list[int]:
        try:
            from Xlib import Xatom

            conn = self._require_display()
            atom = conn.intern_atom(name, only_if_exists=True)
            prop = self.root.get_full_property(atom, Xatom.WINDOW) if atom else None
            if prop is not None and prop.format == 32:
                return [int(wid) for wid in prop.value if wid]
        except Exception as exc:
            log.debug("не удалось прочитать %s: %s", name, exc)
        return []

    def active_window(self) -> int | None:
        """Активное окно EWMH или None."""
        self._enforce_keyboard_deadline()
        windows = self._root_windows("_NET_ACTIVE_WINDOW")
        return windows[0] if windows else None

    def client_list_stacking(self) -> list[int]:
        """Клиенты EWMH снизу вверх; при ошибке пустой список."""
        self._enforce_keyboard_deadline()
        return self._root_windows("_NET_CLIENT_LIST_STACKING")

    def net_workarea(self) -> tuple[int, int, int, int] | None:
        """Область текущего стола (x, y, ширина, высота) через открытое соединение.

        Без номера стола используется нулевой; при коротком массиве — первый.
        Нет свойства, неверный формат или неположительный размер → None.
        """
        self._enforce_keyboard_deadline()
        try:
            from Xlib import Xatom

            conn = self._require_display()
            atom = conn.intern_atom("_NET_WORKAREA", only_if_exists=True)
            prop = self.root.get_full_property(atom, Xatom.CARDINAL) if atom else None
            if prop is None or prop.format != 32 or len(prop.value) < 4:
                return None
            desktop_atom = conn.intern_atom("_NET_CURRENT_DESKTOP", only_if_exists=True)
            desktop = (
                self.root.get_full_property(desktop_atom, Xatom.CARDINAL) if desktop_atom else None
            )
            index = 0
            if desktop is not None and desktop.format == 32 and len(desktop.value):
                index = int(desktop.value[0]) * 4
            if index < 0 or index + 4 > len(prop.value):
                index = 0
            x, y, width, height = (int(v) for v in prop.value[index : index + 4])
            return (x, y, width, height) if width > 0 and height > 0 else None
        except Exception as exc:
            log.debug("не удалось прочитать _NET_WORKAREA: %s", exc)
            return None

    def set_cardinal(self, window_id: int, atom_name: str, value: int = 0) -> bool:
        """Ставит одно 32-битное CARDINAL через открытое соединение; ошибка → False."""
        self._enforce_keyboard_deadline()
        if not window_id or not 0 <= value <= 0xFFFFFFFF:
            return False
        try:
            from Xlib import Xatom

            conn = self._require_display()
            before = self._error_count
            window = conn.create_resource_object("window", window_id)
            window.change_property(conn.intern_atom(atom_name), Xatom.CARDINAL, 32, [value])
            conn.sync()
            return self._error_count == before
        except Exception as exc:
            log.debug("не удалось выставить %s: %s", atom_name, exc)
            return False

    def set_fly_animations_off(self, window_id: int) -> bool:
        """Отключает анимации показа Fly; True означает запись обоих свойств.

        P1-допущение: ветка Fly живьём ещё не проверялась — перелогина заказчика не было.
        """
        map_ok = self.set_cardinal(window_id, "_FLY_WM_WINDOW_MAP_ANIMATION")
        fade_ok = self.set_cardinal(window_id, "_FLY_WM_FADE_SHOW")
        return map_ok and fade_ok

    def wm_class(self, wid: int) -> tuple[str, str] | None:
        """WM_CLASS внутри рамки KWin; поиск до глубины 8 и не более 256 узлов."""
        self._enforce_keyboard_deadline()
        if not wid:
            return None
        try:
            window = self._require_display().create_resource_object("window", wid)
            pending = [(window, 0)]
            seen: set[int] = set()
            # S4 paste.py / живой KDE: WM_CLASS может быть у ребёнка рамки KWin.
            while pending:
                window, depth = pending.pop()
                window_id = int(window.id)
                if window_id in seen:
                    continue
                if len(seen) >= _WM_CLASS_MAX_NODES:
                    return None
                seen.add(window_id)
                try:
                    cls = window.get_wm_class()
                    if cls and len(cls) == 2:
                        return str(cls[0]).lower(), str(cls[1]).lower()
                    if depth < _WM_CLASS_MAX_DEPTH:
                        children = window.query_tree().children
                        if len(children) + len(pending) + len(seen) > _WM_CLASS_MAX_NODES:
                            return None
                        pending.extend((child, depth + 1) for child in reversed(children))
                except Exception as exc:
                    log.debug("не удалось прочитать дочернее окно X11: %s", exc)
        except Exception as exc:
            log.debug("не удалось прочитать WM_CLASS: %s", exc)
        return None

    def window_geometry(self, wid: int) -> tuple[int, int, int, int] | None:
        """Клиентская геометрия (x, y, w, h) в координатах корня или None."""
        self._enforce_keyboard_deadline()
        if not wid:
            return None
        try:
            window = self._require_display().create_resource_object("window", wid)
            geometry = window.get_geometry()
            translated = self.root.translate_coords(window, 0, 0)
            if not translated.same_screen:
                return None
            return (int(translated.x), int(translated.y), int(geometry.width), int(geometry.height))
        except Exception as exc:
            log.debug("не удалось прочитать геометрию X11: %s", exc)
            return None


def _set_states(wid: int, names: list[str]) -> bool:
    """Дополняет состояния до показа; после показа отправляет запрос WM."""
    if not wid:
        return False
    with X11Display() as x11:
        try:
            from Xlib import X, Xatom
            from Xlib.protocol import event

            conn = x11._require_display()
            before = x11._error_count
            window = conn.create_resource_object("window", wid)
            atom = conn.intern_atom("_NET_WM_STATE")
            states = [int(conn.intern_atom(name)) for name in names]
            if window.get_attributes().map_state == X.IsUnmapped:
                prop = window.get_full_property(atom, Xatom.ATOM)
                existing = [] if prop is None else [int(value) for value in prop.value]
                window.change_property(atom, Xatom.ATOM, 32, list(dict.fromkeys(existing + states)))
            else:
                message = event.ClientMessage(
                    window=wid,
                    client_type=atom,
                    data=(32, [1, states[0], states[1] if len(states) > 1 else 0, 1, 0]),
                )
                x11.root.send_event(
                    message, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask
                )
            conn.sync()
            return x11._error_count == before
        except Exception as exc:
            log.debug("не удалось выставить состояния окна X11: %s", exc)
            return False


def set_window_state_above(wid: int) -> bool:
    """Запрашивает ABOVE; подтверждает отправку, а не решение WM."""
    return _set_states(wid, ["_NET_WM_STATE_ABOVE"])


def set_skip_taskbar_pager(wid: int) -> bool:
    """Запрашивает скрытие из панели задач и переключателя рабочих столов."""
    return _set_states(wid, ["_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER"])


def set_window_type(wid: int, kind: str) -> bool:
    """Ставит тип dock/utility/notification; неверный тип или ошибка → False."""
    if not wid or kind not in {"dock", "utility", "notification"}:
        return False
    with X11Display() as x11:
        try:
            from Xlib import Xatom

            conn = x11._require_display()
            before = x11._error_count
            window = conn.create_resource_object("window", wid)
            window.change_property(
                conn.intern_atom("_NET_WM_WINDOW_TYPE"),
                Xatom.ATOM,
                32,
                [conn.intern_atom(f"_NET_WM_WINDOW_TYPE_{kind.upper()}")],
            )
            conn.sync()
            return x11._error_count == before
        except Exception as exc:
            log.debug("не удалось выставить тип окна X11: %s", exc)
            return False


def set_struts(wid: int, left: int, right: int, top: int, bottom: int) -> bool:
    """Резервирует полосы вдоль полных сторон корня; нули снимают резерв."""
    edges = [left, right, top, bottom]
    if not wid or any(value < 0 or value > 0xFFFFFFFF for value in edges):
        return False
    with X11Display() as x11:
        try:
            from Xlib import Xatom

            conn = x11._require_display()
            before = x11._error_count
            window = conn.create_resource_object("window", wid)
            geometry = x11.root.get_geometry()
            max_x, max_y = max(0, int(geometry.width) - 1), max(0, int(geometry.height) - 1)
            partial = edges + [
                0,
                max_y if left else 0,
                0,
                max_y if right else 0,
                0,
                max_x if top else 0,
                0,
                max_x if bottom else 0,
            ]
            for name, values in (("_NET_WM_STRUT_PARTIAL", partial), ("_NET_WM_STRUT", edges)):
                window.change_property(conn.intern_atom(name), Xatom.CARDINAL, 32, values)
            conn.sync()
            return x11._error_count == before
        except Exception as exc:
            log.debug("не удалось выставить резерв окна X11: %s", exc)
            return False


def server_timestamp() -> int:
    """Текущее время X-сервера в миллисекундах или 0.

    Штатный приём: нулевая правка свойства собственного окна рождает
    ``PropertyNotify``, в котором сервер проставляет своё время.
    """
    try:
        from Xlib import X, Xatom
        from Xlib import display as xdisplay
    except Exception:  # noqa: BLE001 — python3-xlib может отсутствовать
        return 0
    conn = None
    try:
        conn = xdisplay.Display()
        conn.set_error_handler(lambda *_: None)
        window = conn.screen().root.create_window(
            0, 0, 1, 1, 0, X.CopyFromParent, event_mask=X.PropertyChangeMask
        )
        atom = conn.intern_atom(TIMESTAMP_ATOM)
        window.change_property(atom, Xatom.STRING, 8, b"")
        conn.flush()
        deadline = time.monotonic() + _WAIT_S
        while time.monotonic() < deadline:
            for _ in range(conn.pending_events()):
                event = conn.next_event()
                if event.type == X.PropertyNotify and event.window.id == window.id:
                    window.destroy()
                    return int(event.time)
            time.sleep(_POLL_S)
        window.destroy()
        return 0
    except Exception as exc:  # noqa: BLE001 — X может быть недоступен
        log.debug("не удалось получить метку времени X: %s", exc)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def set_user_time(window_id: int, timestamp: int) -> bool:
    """Ставит ``_NET_WM_USER_TIME``; ноль до первого map запрещает забирать фокус."""
    if not window_id or timestamp < 0:
        return False
    try:
        from Xlib import Xatom
        from Xlib import display as xdisplay
    except Exception:  # noqa: BLE001
        return False
    conn = None
    try:
        conn = xdisplay.Display()
        # Асинхронные ошибки X (окно могло исчезнуть) не должны сорить в stderr:
        # gate CI требует пустой stderr приложения.
        conn.set_error_handler(lambda *_: None)
        window = conn.create_resource_object("window", int(window_id))
        atom = conn.intern_atom(USER_TIME_ATOM)
        window.change_property(atom, Xatom.CARDINAL, 32, [int(timestamp)])
        conn.sync()
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("не удалось выставить %s: %s", USER_TIME_ATOM, exc)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
