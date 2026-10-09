"""Автомат PTT/toggle и глобальный хоткей X11 без Qt и потоков.

Оркестрация читает ``fileno()`` через QSocketNotifier, вызывает
``process_pending()`` и ставит свой таймер на ``fsm.limit_deadline``.
Время автомата — монотонные секунды; время X-события нужно только для
фильтра автоповтора. Логика перенесена из S4 ``PttMachine``.
"""

from __future__ import annotations

import logging
import select
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol

from astra_voice.core.constants import RECORD_LIMIT_S as RECORD_LIMIT_S
from astra_voice.platform.x11 import BadCombo, ParsedCombo, X11Display, X11Unavailable

log = logging.getLogger(__name__)

PTT_THRESHOLD_S = 0.3
LOOKAHEAD_S = 0.002
DEFAULT_CANDIDATES = ("Ctrl+Shift+Space", "Ctrl+Alt+D")
BUSY_MESSAGE = "Комбинация занята другой программой (возможно Handy или переключатель ввода)"
ResultCode = Literal["ok", "busy", "bad-combo", "duplicate", "not-grabbed"]
# Причина on_state после смены карты: ``mapping-regrab:<код>[;escape:<код>]``.
MAPPING_REGRAB_PREFIX = "mapping-regrab:"


class HotkeyMode(Enum):
    PTT = "ptt"
    TOGGLE = "toggle"


class HotkeyState(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"


@dataclass(frozen=True)
class GrabResult:
    """Код операции; подсказка владельца только при BadAccess."""

    code: ResultCode
    owner_hint: str | None = None
    keycode: int | None = None
    mods: int = 0

    @property
    def ok(self) -> bool:
        return self.code == "ok"


@dataclass(frozen=True)
class ProbeResult(GrabResult):
    """Результат кратковременного захвата; успешный захват уже снят."""


@dataclass(frozen=True)
class HotkeyEvent:
    """Клавишное событие: time — исходная метка сервера, не секунды."""

    kind: Literal["KeyPress", "KeyRelease"]
    keycode: int
    time: int
    escape: bool = False
    mods: int = 0
    confirmed_hold: bool = False


@dataclass(frozen=True)
class MappingEvent:
    """Результаты перезахвата в порядке очереди, перед клавишами новой карты.

    Поля после ``escape`` — диагностика для журнала: вид MappingNotify, диапазон
    keycode, изменились ли коды/маски блокировок, трогались ли захваты и время.
    """

    combos: dict[str, GrabResult]
    escape: GrabResult | None
    request: str = ""
    first: int = 0
    count: int = 0
    keycode_changed: bool = False
    masks_changed: bool = False
    regrabbed: bool = False
    elapsed_ms: float = 0.0


class HotkeyFsm:
    """Чистый автомат; ровно 0,3 с считается удержанием, как в S4."""

    def __init__(
        self,
        mode: HotkeyMode = HotkeyMode.PTT,
        on_state: Callable[[HotkeyState, str], None] | None = None,
    ) -> None:
        self.state = HotkeyState.IDLE
        self.mode = mode
        self.on_state = on_state
        self._configured_mode = mode
        self._press_ts = 0.0
        self._limit_deadline: float | None = None

    @property
    def limit_deadline(self) -> float | None:
        return self._limit_deadline

    def _emit(self, reason: str, now: float) -> None:
        if self.on_state is not None:
            self.on_state(self.state, reason)

    def press(self, now: float) -> None:
        if self.state == HotkeyState.IDLE:
            self._press_ts = now
            self.mode = self._configured_mode
            self.state = HotkeyState.RECORDING
            self._limit_deadline = now + RECORD_LIMIT_S
            self._emit("press", now)
        elif self.state == HotkeyState.RECORDING and self.mode == HotkeyMode.TOGGLE:
            self.stop(now, "toggle-off")

    def release(self, now: float) -> None:
        if self.state != HotkeyState.RECORDING or self.mode == HotkeyMode.TOGGLE:
            return
        held = now - self._press_ts
        if held < PTT_THRESHOLD_S:
            self.mode = HotkeyMode.TOGGLE
            self._emit(f"tap→toggle ({held:.3f} с)", now)
        else:
            self.stop(now, f"release ({held:.3f} с)")

    def stop(self, now: float, reason: str) -> None:
        if self.state == HotkeyState.RECORDING:
            self.state = HotkeyState.PROCESSING
            self._limit_deadline = None
            self._emit(reason, now)

    def escape(self, now: float) -> None:
        if self.state != HotkeyState.IDLE:
            self.state = HotkeyState.IDLE
            self.mode = self._configured_mode
            self._limit_deadline = None
            self._emit("escape-cancel", now)

    def done(self, now: float) -> None:
        if self.state == HotkeyState.PROCESSING:
            self.state = HotkeyState.IDLE
            self.mode = self._configured_mode
            self._emit("done", now)

    def tick(self, now: float) -> None:
        if self._limit_deadline is not None and now >= self._limit_deadline:
            self.stop(now, "limit")


class HotkeyBackend(Protocol):
    """Захваты и очередь клавиш; ожидание пары автоповтора ограничено S4.

    Бэкенд с захватом всей клавиатуры дополнительно реализует
    cancel_keyboard_grab(); key_is_down(keycode) защищает новый жест от
    физически удержанной клавиши при восстановлении команды.
    """

    def grab_combo(self, combo: str) -> GrabResult: ...
    def ungrab_combo(self, combo: str) -> GrabResult: ...
    def grab_escape(self) -> GrabResult: ...
    def ungrab_escape(self) -> None: ...
    def poll_events(self, timeout: float = 0.0) -> Sequence[HotkeyEvent | MappingEvent]: ...
    def fileno(self) -> int: ...


class X11HotkeyBackend:
    """Ленивое соединение X11; разбор, маски и откат захватов — в x11.py."""

    def __init__(self, display: X11Display | None = None) -> None:
        self._x = display if display is not None else X11Display()
        self._combos: dict[str, ParsedCombo] = {}
        self._requested_combos: dict[str, None] = {}
        self._escape: ParsedCombo | None = None
        self._escape_requested = False
        # Escape совпал с хоткеем: своего захвата у него нет.
        self._escape_shared = False
        # Фактически захваченные маски по (keycode, mods): снимать нужно именно их,
        # даже если маски блокировок с тех пор сменились.
        self._masks: dict[tuple[int, int], tuple[int, ...]] = {}
        self.synchronous_super = False

    def combo_signature(self, combo: str) -> tuple[int, int] | None:
        """Реальная пара keycode/modifiers для сравнения ролей и алиасов."""
        if not self._x.open():
            return None
        try:
            parsed = self._x.parse_combo(combo)
        except (BadCombo, X11Unavailable):
            return None
        return parsed.keycode, parsed.mods

    def grab_combo(self, combo: str) -> GrabResult:
        if not self._x.open():
            return GrabResult("not-grabbed")
        try:
            parsed = self._x.parse_combo(combo)
        except BadCombo:
            return GrabResult("bad-combo")
        except X11Unavailable:
            return GrabResult("not-grabbed")
        grabbed = list(self._combos.values())
        if self._escape is not None:
            grabbed.append(self._escape)
        if any((p.mods, p.keycode) == (parsed.mods, parsed.keycode) for p in grabbed):
            return GrabResult("duplicate")
        result = self._grab(parsed)
        if result.ok:
            self._combos[combo] = parsed
            self._requested_combos[combo] = None
        return result

    def _grab(self, parsed: ParsedCombo, *, keep: tuple[int, ...] = ()) -> GrabResult:
        masks = tuple(self._x.mask_variants(parsed.mods))
        options = {}
        if self.synchronous_super and parsed.keysym_name in ("Super_L", "Super_R"):
            options["keyboard_sync"] = True
        report = self._x.grab_key(parsed.keycode, parsed.mods, masks=masks, keep=keep, **options)
        if report.ok:
            self._masks[(parsed.keycode, parsed.mods)] = masks
            return GrabResult("ok", keycode=parsed.keycode, mods=parsed.mods)
        if report.bad_access:
            return GrabResult("busy", owner_hint=BUSY_MESSAGE)
        log.debug("захват X11 не выполнен: %s", report.per_mask)
        return GrabResult("not-grabbed")

    def _release(self, parsed: ParsedCombo, masks: tuple[int, ...] | None = None) -> None:
        """Снимает сохранённые маски клавиши (или только ``masks``)."""
        key = (parsed.keycode, parsed.mods)
        if masks is None:
            masks = self._masks.pop(key, None)
        if masks is None:
            self._x.ungrab_key(parsed.keycode, parsed.mods)
        elif masks:
            self._x.ungrab_key(parsed.keycode, parsed.mods, masks=masks)

    def ungrab_combo(self, combo: str) -> GrabResult:
        self._requested_combos.pop(combo, None)
        parsed = self._combos.pop(combo, None)
        if parsed is None:
            return GrabResult("not-grabbed")
        self._release(parsed)
        return GrabResult("ok")

    def cancel_keyboard_grab(self) -> None:
        """Отмена операции, не probe/MappingNotify: отпустить и активный grab."""
        self._x.cancel_keyboard_grab()

    def key_is_down(self, keycode: int) -> bool:
        """После нового grab требуем отпускания уже зажатой клавиши."""
        try:
            keymap = self._x._require_display().query_keymap()
            return bool(keymap[keycode // 8] & (1 << (keycode % 8)))
        except Exception:
            # Неизвестное состояние не разрешает начать команду автоповтором.
            return True

    def grab_escape(self) -> GrabResult:
        if self._escape is not None:
            return GrabResult("duplicate")
        if not self._x.open():
            return GrabResult("not-grabbed")
        try:
            parsed = self._x.parse_combo("Escape")
        except BadCombo:
            return GrabResult("bad-combo")
        except X11Unavailable:
            return GrabResult("not-grabbed")
        # Escape может быть самим хоткеем: его основной захват не снимаем.
        if self._shares_combo(parsed):
            self._escape = parsed
            self._escape_requested = True
            self._escape_shared = True
            return GrabResult("ok", keycode=parsed.keycode)
        result = self._grab(parsed)
        if result.ok:
            self._escape = parsed
            self._escape_requested = True
            self._escape_shared = False
        return result

    def _shares_combo(self, parsed: ParsedCombo) -> bool:
        return any(
            (p.mods, p.keycode) == (parsed.mods, parsed.keycode) for p in self._combos.values()
        )

    def ungrab_escape(self) -> None:
        self._escape_requested = False
        parsed, self._escape = self._escape, None
        shared, self._escape_shared = self._escape_shared, False
        if parsed is not None and not shared and not self._shares_combo(parsed):
            self._release(parsed)

    def _move(self, old: ParsedCombo | None, parsed: ParsedCombo) -> tuple[GrabResult, bool, bool]:
        """Переносит захват на новую карту без окна, когда клавиша не захвачена.

        Возвращает результат, изменился ли keycode и трогались ли захваты.
        Сначала захватываются новые маски, потом снимаются лишние старые;
        при неудаче старый захват тоже снимается: он относится к прежней карте.
        """
        masks = tuple(self._x.mask_variants(parsed.mods))
        new_key = (parsed.keycode, parsed.mods)
        old_key = (old.keycode, old.mods) if old is not None else None
        old_masks = self._masks.get(old_key, ()) if old_key is not None else ()
        keycode_changed = old is not None and old.keycode != parsed.keycode
        if old_key == new_key and old_masks == masks:
            return GrabResult("ok", keycode=parsed.keycode, mods=parsed.mods), False, False
        keep = old_masks if old_key == new_key else ()
        result = self._grab(parsed, keep=keep)
        # При отказе старый захват тоже снимается: он поставлен по прежней карте, и его
        # keycode/маски могут уже означать другую клавишу. Держать его — значит ловить
        # чужие нажатия; хоткей честно считается потерянным, повтор ведёт runtime.
        if old is not None:
            if result.ok and old_key == new_key:
                stale = tuple(mask for mask in old_masks if mask not in masks)
                self._release(old, stale)
            else:
                self._release(old)
        return result, keycode_changed, True

    def _remap_combo(self, combo: str) -> tuple[GrabResult, bool, bool]:
        old = self._combos.pop(combo, None)
        try:
            parsed = self._x.parse_combo(combo)
        except (BadCombo, X11Unavailable) as exc:
            if old is not None:
                self._release(old)
            code: ResultCode = "bad-combo" if isinstance(exc, BadCombo) else "not-grabbed"
            return GrabResult(code), old is not None, old is not None
        result, keycode_changed, touched = self._move(old, parsed)
        if result.ok:
            self._combos[combo] = parsed
        return result, keycode_changed, touched

    def _remap_escape(self) -> tuple[GrabResult, bool, bool]:
        old = None if self._escape_shared else self._escape
        old_keycode = self._escape.keycode if self._escape is not None else None
        self._escape = None
        self._escape_shared = False
        try:
            parsed = self._x.parse_combo("Escape")
        except (BadCombo, X11Unavailable) as exc:
            if old is not None:
                self._release(old)
            code: ResultCode = "bad-combo" if isinstance(exc, BadCombo) else "not-grabbed"
            return GrabResult(code), old is not None, old is not None
        changed = old_keycode is not None and old_keycode != parsed.keycode
        if self._shares_combo(parsed):
            if old is not None:
                self._release(old)
            self._escape = parsed
            self._escape_shared = True
            return GrabResult("ok", keycode=parsed.keycode), changed, old is not None
        result, _, touched = self._move(old, parsed)
        if result.ok:
            self._escape = parsed
        return result, changed, touched

    def _drop_all(self) -> None:
        """Карта неизвестна: снимаем прежние захваты по сохранённым маскам."""
        if self._escape is not None and not self._escape_shared:
            self._release(self._escape)
        self._escape = None
        self._escape_shared = False
        for parsed in self._combos.values():
            self._release(parsed)
        self._combos.clear()

    def _refresh_mapping(self, event: Any) -> MappingEvent:
        """Пересчитывает карту и перезахватывает только изменившееся.

        Xorg шлёт MappingNotify при каждой смене источника клавиш (физическая
        клавиатура ↔ XTest). Если keycode и маски блокировок те же, захват не
        трогается вовсе: снятие и повторный захват оставляли окно, в котором
        нажатие уходило в окно фокуса, а неудача убивала хоткей до следующей
        нотификации.
        """
        from Xlib import X

        started = time.monotonic()
        combos = list(self._requested_combos)
        escape = self._escape_requested
        old_locks = dict(self._x.lock_masks)
        keycode_changed = masks_changed = touched = False
        if not self._x.refresh_keyboard_mapping(event):
            touched = bool(self._combos) or self._escape is not None
            self._drop_all()
            failed = GrabResult("not-grabbed")
            results = dict.fromkeys(combos, failed)
            escape_result = failed if escape else None
        else:
            masks_changed = old_locks != self._x.lock_masks
            results = {}
            for combo in combos:
                results[combo], changed, moved = self._remap_combo(combo)
                keycode_changed |= changed
                touched |= moved
            escape_result = None
            if escape:
                escape_result, changed, moved = self._remap_escape()
                keycode_changed |= changed
                touched |= moved
        # Промежуточная карта может быть неполной; следующая нотификация повторит попытку.
        self._requested_combos = dict.fromkeys(combos)
        self._escape_requested = escape
        request = int(getattr(event, "request", -1))
        return MappingEvent(
            results,
            escape_result,
            request={X.MappingModifier: "modifier", X.MappingKeyboard: "keyboard"}.get(
                request, str(request)
            ),
            first=int(getattr(event, "first_keycode", 0)),
            count=int(getattr(event, "count", 0)),
            keycode_changed=keycode_changed,
            masks_changed=masks_changed,
            regrabbed=touched,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )

    def poll_events(self, timeout: float = 0.0) -> list[HotkeyEvent | MappingEvent]:
        pending = self._x.pending_events()
        if self._x.d is None:
            return []
        from Xlib import X

        events: list[HotkeyEvent | MappingEvent] = []
        if timeout > 0 and not pending:
            select.select([self.fileno()], [], [], timeout)
        while self._x.pending_events():
            event = self._x.next_event()
            # MappingNotify приходит всем клиентам без маски подписки XSelectInput.
            if event.type == X.MappingNotify:
                if event.request in (X.MappingKeyboard, X.MappingModifier):
                    events.append(self._refresh_mapping(event))
            elif event.type in (X.KeyPress, X.KeyRelease):
                events.append(
                    HotkeyEvent(
                        "KeyPress" if event.type == X.KeyPress else "KeyRelease",
                        int(event.detail),
                        int(event.time),
                        self._escape is not None and event.detail == self._escape.keycode,
                        self._x.semantic_modifiers(int(event.state)),
                    )
                )
        return events

    def fileno(self) -> int:
        """Дескриптор для внешнего наблюдателя; без соединения — минус один."""
        return self._x.fileno()

    def close(self) -> None:
        self.ungrab_escape()
        for combo in list(self._requested_combos):
            self.ungrab_combo(combo)
        self._x.close()


class EvdevHotkeyBackend:
    """P2"""

    def __init__(self) -> None:
        raise NotImplementedError


class HotkeyManager:
    """Связка автомата и бэкенда; колбэки клавиш вызываются без аргументов.

    Escape захватывается при RECORDING и снимается в IDLE, как в S4:
    это сохраняет отмену во время PROCESSING. ``ungrab()`` возвращает None;
    код последней операции, включая ``not-grabbed``, доступен в ``last_result``.
    После смены карты ``on_state`` получает причину ``mapping-regrab:<код>``
    и результат Escape, если он был захвачен; состояние автомата сохраняется.
    Если перезахват не удался, повторный ``grab()`` того же сочетания
    восстанавливает захват, не сбрасывая автомат (повтор ведёт runtime).
    """

    def __init__(
        self,
        backend: HotkeyBackend | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend if backend is not None else X11HotkeyBackend()
        self._clock = clock
        self.defer_single_super = False
        self.on_deferred_press: Callable[[], None] | None = None
        self._deferred_press: float | None = None
        self.on_press: Callable[[], None] | None = None
        self.on_release: Callable[[], None] | None = None
        self.on_escape: Callable[[], None] | None = None
        self.on_state: Callable[[HotkeyState, str], None] | None = None
        self.fsm = HotkeyFsm(on_state=self._on_state)
        self.last_result = GrabResult("not-grabbed")
        self.escape_result = GrabResult("not-grabbed")
        self._combo: str | None = None
        self._keycode: int | None = None
        self._mods = 0
        self._escape_grabbed = False
        self._escape_keycode: int | None = None
        self._external_escape_token: object | None = None
        self._external_escape_callback: Callable[[], None] | None = None
        self._external_escape_revoking = False
        self._key_down = False
        self._event_generation = 0
        self.mapping_release_lost = False
        # Перезахват после смены карты не удался; grab() того же сочетания восстановит.
        self._lost = False
        # Диагностика журнала: пары автоповтора за удержание и пропуски нажатий подряд.
        self._repeat_pairs = 0
        self._skipped_presses = 0

    @property
    def has_pending_press(self) -> bool:
        return self._deferred_press is not None

    def acquire_external_escape(self, token: object, callback: Callable[[], None]) -> GrabResult:
        """Lease passive Escape on this existing reader without changing its FSM."""
        if self._external_escape_revoking or self.fsm.state != HotkeyState.IDLE:
            return GrabResult("busy")
        if self._external_escape_token is not None:
            return (
                self.escape_result if token is self._external_escape_token else GrabResult("busy")
            )
        try:
            self.escape_result = self.backend.grab_escape()
        except Exception:
            self.escape_result = GrabResult("not-grabbed")
            log.warning("Клавиша отмены недоступна")
        if self.escape_result.ok:
            self._escape_grabbed = True
            self._escape_keycode = self.escape_result.keycode
            self._external_escape_token = token
            self._external_escape_callback = callback
        return self.escape_result

    def release_external_escape(self, token: object) -> None:
        """Only the matching owner can release a lease; never call its callback."""
        if token is not self._external_escape_token:
            return
        self._external_escape_token = None
        self._external_escape_callback = None
        if self.fsm.state == HotkeyState.IDLE:
            self.backend.ungrab_escape()
            self._escape_grabbed = False
            self._escape_keycode = None

    def _revoke_external_escape(self) -> None:
        token = self._external_escape_token
        if token is None or self._external_escape_revoking:
            return
        self._external_escape_revoking = True
        callback = self._external_escape_callback
        try:
            # Cancel the owner's operation before taking its cancellation key away.
            if callback is not None:
                callback()
        finally:
            self.release_external_escape(token)
            self._external_escape_revoking = False

    def signature(self, combo: str) -> tuple[int, int] | None:
        """Без захвата сравнивает сочетания по текущей карте X11."""
        resolve = getattr(self.backend, "combo_signature", None)
        if resolve is None:
            return None
        result = resolve(combo)
        return result if isinstance(result, tuple) and len(result) == 2 else None

    def grab(self, combo: str, mode: HotkeyMode) -> GrabResult:
        self._revoke_external_escape()
        configure = getattr(self.backend, "configure", None)
        if configure is not None:
            configure(defer_super=self.defer_single_super and mode == HotkeyMode.PTT)
        if combo == self._combo and self._lost:
            # Захват потерян после смены карты: восстанавливаем, автомат не трогаем.
            result = self._grab_released_combo(combo)
            if result.ok:
                self._event_generation += 1
                self._lost = False
                self._keycode = result.keycode
                self._mods = result.mods
                self._key_down = False
                # Escape нужен, пока автомат не в IDLE; если он пропал вместе с хоткеем —
                # возвращаем и его.
                if self.fsm.state != HotkeyState.IDLE and not self._escape_grabbed:
                    self.escape_result = self.backend.grab_escape()
                    self._escape_grabbed = self.escape_result.ok
                    self._escape_keycode = self.escape_result.keycode
            self.last_result = result
            return result
        if combo == self._combo:
            self.last_result = GrabResult("duplicate")
            return self.last_result
        result = self._grab_released_combo(combo)
        if result.ok:
            if self._combo is not None:
                self.ungrab()
            self._event_generation += 1
            self._combo = combo
            self._keycode = result.keycode
            self._mods = result.mods
            self.fsm = HotkeyFsm(mode, self._on_state)
        self.last_result = result
        return result

    def _grab_released_combo(self, combo: str) -> GrabResult:
        result = self.backend.grab_combo(combo)
        held = getattr(self.backend, "key_is_down", None)
        if (
            result.ok
            and self.defer_single_super
            and result.keycode is not None
            and held is not None
            and held(result.keycode)
        ):
            # После force-ungrab отпускание может уйти окну фокуса. Не ждём
            # KeyRelease на нашем соединении: штатный retry проверит keymap снова.
            self.backend.ungrab_combo(combo)
            self._cancel_keyboard_grab()
            self.backend.poll_events()
            return GrabResult("not-grabbed")
        return result

    def _cancel_keyboard_grab(self) -> None:
        cancel = getattr(self.backend, "cancel_keyboard_grab", None)
        if cancel is not None:
            cancel()

    def ungrab(self) -> None:
        self._revoke_external_escape()
        self._event_generation += 1
        self.mapping_release_lost = False
        self._deferred_press = None
        if self._combo is None:
            self.last_result = GrabResult("not-grabbed")
            return
        self.fsm.escape(self._clock())
        self.last_result = self.backend.ungrab_combo(self._combo)
        self._cancel_keyboard_grab()
        self._combo = None
        self._keycode = None
        self._key_down = False
        self._lost = False
        # События старого захвата не должны запустить следующую запись.
        self.backend.poll_events()

    def probe(self, combo: str) -> ProbeResult:
        if combo == self._combo:
            return ProbeResult("duplicate")
        probe = getattr(self.backend, "probe_combo", None)
        if probe is not None:
            result = probe(combo)
            return ProbeResult(result.code, result.owner_hint, result.keycode, result.mods)
        result = self.backend.grab_combo(combo)
        if result.ok:
            self.backend.ungrab_combo(combo)
        return ProbeResult(result.code, result.owner_hint, result.keycode, result.mods)

    def free_candidates(self, prefer: list[str]) -> list[str]:
        return [combo for combo in prefer if self.probe(combo).ok]

    def fileno(self) -> int:
        return self.backend.fileno()

    def _on_state(self, state: HotkeyState, reason: str) -> None:
        if state == HotkeyState.RECORDING and not self._escape_grabbed:
            self.escape_result = self.backend.grab_escape()
            self._escape_grabbed = self.escape_result.ok
            self._escape_keycode = self.escape_result.keycode
        elif state == HotkeyState.IDLE and self._external_escape_token is None:
            self.backend.ungrab_escape()
            self._escape_grabbed = False
            self._escape_keycode = None
        if self.on_state is not None:
            self.on_state(state, reason)

    def handle_event(self, event: HotkeyEvent, now: float) -> GrabResult:
        """Передаёт одну клавишу автомату; пары автоповтора убирает очередь."""
        if (
            self._external_escape_token is not None
            and self._escape_grabbed
            and event.kind == "KeyPress"
            and (event.escape or event.keycode == self._escape_keycode)
        ):
            external_callback = self._external_escape_callback
            if external_callback is not None:
                external_callback()
            return GrabResult("ok")
        if self._combo is None:
            self.last_result = GrabResult("not-grabbed")
            return self.last_result
        callback: Callable[[], None] | None = None
        if event.kind == "KeyPress" and event.keycode != self._keycode:
            self._deferred_press = None
        matches_combo = event.keycode == self._keycode and event.mods == self._mods
        if (
            not matches_combo
            and (event.escape or event.keycode == self._escape_keycode)
            and self._escape_grabbed
            and event.kind == "KeyPress"
        ):
            before = self.fsm.state
            self.fsm.escape(now)
            callback = self.on_escape
            log.info("хоткей: Escape, автомат %s→%s", before.value, self.fsm.state.value)
        elif event.keycode == self._keycode:
            before = self.fsm.state
            if event.kind == "KeyPress" and matches_combo and not self._key_down:
                self._key_down = True
                self._repeat_pairs = 0
                self._skipped_presses = 0
                if (
                    self.defer_single_super
                    and not event.confirmed_hold
                    and self._combo.lower() in ("super_l", "super_r", "super", "win")
                    and self.fsm._configured_mode == HotkeyMode.PTT
                    and self.fsm.state == HotkeyState.IDLE
                ):
                    self._deferred_press = now
                    if self.on_deferred_press is not None:
                        self.on_deferred_press()
                else:
                    self.fsm.press(now - PTT_THRESHOLD_S if event.confirmed_hold else now)
                    callback = self.on_press
                log.info(
                    "хоткей: нажатие keycode=%d mods=%#x key_down=1, автомат %s→%s",
                    event.keycode,
                    event.mods,
                    before.value,
                    self.fsm.state.value,
                )
            elif event.kind == "KeyRelease" and self._key_down:
                # A short single-Win tap never enters recording/toggle. If the
                # timer was delayed, discard the gesture rather than record after release.
                pending = self._deferred_press is not None
                self._deferred_press = None
                self._key_down = False
                self._skipped_presses = 0
                if not pending:
                    self.fsm.release(now)
                    callback = self.on_release
                log.info(
                    "хоткей: отпускание keycode=%d mods=%#x key_down=0, автомат %s→%s, "
                    "отфильтровано пар автоповтора: %d",
                    event.keycode,
                    event.mods,
                    before.value,
                    self.fsm.state.value,
                    self._repeat_pairs,
                )
            elif event.kind == "KeyPress":
                # Первый пропуск подряд — INFO, остальные до следующего нажатия — DEBUG.
                self._skipped_presses += 1
                log.log(
                    logging.INFO if self._skipped_presses == 1 else logging.DEBUG,
                    "хоткей: нажатие пропущено: fsm=%s key_down=%d mods=%#x, ожидались %#x",
                    self.fsm.state.value,
                    self._key_down,
                    event.mods,
                    self._mods,
                )
            else:
                log.debug("хоткей: отпускание без нажатия пропущено, mods=%#x", event.mods)
        if callback is not None:
            callback()
        self.last_result = GrabResult("ok")
        return self.last_result

    def tick(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        pending = self._deferred_press
        if pending is not None and now - pending >= PTT_THRESHOLD_S:
            self._deferred_press = None
            if self._key_down and self._combo is not None and not self._lost:
                self.fsm.press(pending)
                if self.on_press is not None:
                    self.on_press()
        self.fsm.tick(now)

    def _handle_mapping(self, event: MappingEvent) -> None:
        """Обновляет коды и сообщает наружу как успех, так и отказ перезахвата."""
        self._deferred_press = None
        self._log_mapping(event)
        if self._external_escape_token is not None and event.escape is not None:
            self.escape_result = event.escape
            self._escape_grabbed = event.escape.ok
            self._escape_keycode = event.escape.keycode
            if not event.escape.ok:
                self._revoke_external_escape()
        if self._combo is None or self._combo not in event.combos:
            return
        self.last_result = event.combos[self._combo]
        # A successful new grab cannot deliver release of the old physical key.
        # Expose the lost PTT stop edge without changing ordinary text policy.
        self.mapping_release_lost = (
            self._key_down
            and self.fsm.state == HotkeyState.RECORDING
            and self.fsm.mode == HotkeyMode.PTT
            and self._keycode != self.last_result.keycode
        )
        if self._keycode != self.last_result.keycode:
            self._key_down = False
        self._keycode = self.last_result.keycode
        self._mods = self.last_result.mods
        self._lost = not self.last_result.ok
        reason = f"{MAPPING_REGRAB_PREFIX}{self.last_result.code}"
        if event.escape is not None:
            self.escape_result = event.escape
            self._escape_grabbed = event.escape.ok
            self._escape_keycode = event.escape.keycode
            reason += f";escape:{event.escape.code}"
        if self.on_state is not None:
            self.on_state(self.fsm.state, reason)

    def _log_mapping(self, event: MappingEvent) -> None:
        """Одна строка на MappingNotify: что пришло, что изменилось, что с захватом."""
        if event.regrabbed:
            parts = []
            if self._combo is not None and self._combo in event.combos:
                parts.append(f"хоткей={event.combos[self._combo].code}")
            if event.escape is not None:
                parts.append(f"escape={event.escape.code}")
            outcome = ", ".join(parts) or "нечего перезахватывать"
        else:
            outcome = "не нужен"
        log.info(
            "хоткей: MappingNotify request=%s first=%d count=%d, keycode изменился: %s, "
            "маски изменились: %s, перезахват: %s, %.1f мс",
            event.request or "?",
            event.first,
            event.count,
            "да" if event.keycode_changed else "нет",
            "да" if event.masks_changed else "нет",
            outcome,
            event.elapsed_ms,
        )

    def process_pending(self, now: float | None = None) -> GrabResult:
        """Разбирает очередь и убирает пары повтора, включая соседний read."""
        events = list(self.backend.poll_events())
        generation = self._event_generation
        self.last_result = GrabResult("ok" if self._combo is not None else "not-grabbed")
        index = 0
        while index < len(events):
            # A callback can cancel/rearm while this batch still contains old keys.
            if generation != self._event_generation:
                break
            event = events[index]
            if isinstance(event, MappingEvent):
                self._handle_mapping(event)
                index += 1
                continue
            if event.kind == "KeyRelease" and index + 1 == len(events):
                if event.keycode == self._keycode or event.escape:
                    events.extend(self.backend.poll_events(LOOKAHEAD_S))
            following = events[index + 1] if index + 1 < len(events) else None
            if (
                event.kind == "KeyRelease"
                and isinstance(following, HotkeyEvent)
                and following.kind == "KeyPress"
                and event.keycode == following.keycode
                and event.time == following.time
                and event.mods == following.mods
            ):
                if event.keycode == self._keycode:
                    self._repeat_pairs += 1
                index += 2
                continue
            self.handle_event(event, self._clock() if now is None else now)
            index += 1
        return self.last_result
