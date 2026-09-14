"""Вставка и возврат буфера: цепочка S4, У11/У12/У40, T-14/T-15.

Копируем текст и небольшие MIME-форматы. Гарантия восстановления — текст и text/*:
Klipper вправе пересобрать MIME, поэтому побайтового совпадения не обещаем.
Fly: тип из ClipboardManagerTypesBlacklist должен исключить нашу фразу из
~/.fly/clipboard. Эта ветка НЕ проверена живьём: перелогина заказчика ещё
не было. Это P1-допущение, проверяемое в живом прогоне Fly.

Оркестрация (зона C) на время всей вставки сама глушит хоткей и доставку
результатов распознавания. Этот модуль лишь возвращает busy при повторном
входе; вложенные циклы Qt продолжают обслуживать таймеры и SelectionRequest.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Any

from astra_voice.platform import session
from astra_voice.platform.x11 import X11Display

log = logging.getLogger(__name__)

KDE_HINT = "x-kde-passwordManagerHint"
DELAY_BEFORE_MS = 50
DELAY_AFTER_MS = 100
FLY_SYSTEM_THEME = Path("/usr/share/fly-wm/theme/default.themerc")
FLY_FALLBACK_TYPE = "x-openoffice-link"
_MAX_MIME_BYTES = 1024 * 1024
_TEXT_TARGETS = {"text", "string", "utf8_string", "compound_text"}
_HEAVY_MIME_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "application/x-qt-image",
    "application/x-openoffice",
    "application/vnd.oasis.opendocument",
    "application/vnd.sun.xml",
    "star embed",
    "star object descriptor",
)

_CTRL_SHIFT_V_CLASSES = {
    "konsole",
    "fly-term",
    "flyterm",
    "yakuake",
    "alacritty",
    "gnome-terminal-server",
    "xfce4-terminal",
}
_SHIFT_INSERT_CLASSES = {"xterm", "uxterm", "rxvt", "urxvt"}
_running = False
_last_clipboard_snapshot: dict[str, bytes] | None = None
_pending: _PendingRestore | None = None


class PasteMode(StrEnum):
    AUTO = "auto"
    CLIPBOARD_ONLY = "clipboard-only"


class PasteOutcomeKind(StrEnum):
    PASTED = "pasted"
    CLIPBOARD_ONLY = "clipboard-only"
    WINDOW_CHANGED = "window-changed"
    REFUSED_SECRET = "refused-secret"
    BUSY = "busy"
    FAILED = "failed"


class PasteMethod(StrEnum):
    CTRL_V = "ctrl+v"
    CTRL_SHIFT_V = "ctrl+shift+v"
    SHIFT_INSERT = "shift+insert"
    NONE = "none"


class PasteRestore(StrEnum):
    RESTORED = "restored"
    CLEARED_SECRET = "cleared-secret"
    CLEARED_EMPTY = "cleared-empty"
    SKIPPED_NOT_OWNER = "skipped-not-owner"
    KEPT_OURS = "kept-ours"
    FAILED = "failed"


@dataclass(frozen=True)
class PasteOutcome:
    """Результат без содержимого буфера и распознанной фразы.

    restore относится к CLIPBOARD; очистка секрета в любом из двух буферов
    даёт refused-secret. stripped_controls — число удалённых C0/C1, без
    CR/LF: они заменяются пробелами, включая пару CRLF. busy ничего не меняет;
    его нулевые счётчики означают, что фраза не обрабатывалась. failed сообщает
    об ошибке до публикации или при восстановлении, без текста исключения.
    """

    kind: PasteOutcomeKind
    method: PasteMethod
    restore: PasteRestore
    wm_class: tuple[str, str] | None
    chars: int
    stripped_controls: int
    t_ms: float


def normalize(text: str) -> str:
    """CRLF/CR/LF → пробел; все C0/C1 удаляются (векторы S4, T-15)."""
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return "".join(ch for ch in text if not (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F))


def method_for_wm_class(wm_class: str | tuple[str, str] | None) -> PasteMethod:
    """Выбрать комбинацию по instance/class; xterm не понимает Ctrl+Shift+V."""
    if wm_class is None:
        return PasteMethod.CTRL_V
    names = {name.lower() for name in ((wm_class,) if isinstance(wm_class, str) else wm_class)}
    if names & _SHIFT_INSERT_CLASSES:
        return PasteMethod.SHIFT_INSERT
    if names & _CTRL_SHIFT_V_CLASSES:
        return PasteMethod.CTRL_SHIFT_V
    if any(marker in name for name in names for marker in ("term", "tty", "console")):
        return PasteMethod.SHIFT_INSERT
    return PasteMethod.CTRL_V


def _fly_blacklist_type(
    paths: tuple[Path, ...] | None = None, *, occupied: Collection[str] = ()
) -> str:
    """Первый безопасный тип: текущая тема → пользовательская → системная.

    При отсутствии доступного списка запасной тип — x-openoffice-link,
    входящий в штатный чёрный список fly-wm. Файлы истории не читаются.
    Текст, hint и уже заполненные ключи не должны затираться (У49/T-48).
    """
    if paths is None:
        theme = Path.home() / ".fly/theme"
        paths = (theme / "current.themerc", theme / "default.themerc", FLY_SYSTEM_THEME)
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            key, sep, value = line.partition("=")
            if sep and key.strip() == "ClipboardManagerTypesBlacklist":
                for item in value.strip().strip("\"'").split(":"):
                    item = item.strip()
                    if (
                        item
                        and not item.lower().startswith("text/")
                        and item.lower() != KDE_HINT.lower()
                        and item not in occupied
                    ):
                        return item
    return FLY_FALLBACK_TYPE


def snapshot_mime(md: Any) -> dict[str, bytes]:
    """Скопировать MIME до передачи владения QMimeData (S4, PRD F5.3).

    Текст, text/* и hint сохраняются всегда. Известные тяжёлые форматы
    пропускаются до запроса данных (он может блокировать GUI), остальные
    нетекстовые — при размере свыше 1 МиБ, до копирования в bytes.
    """
    snapshot: dict[str, bytes] = {}
    if md is None:
        return snapshot
    for fmt in md.formats():
        name = fmt.lower()
        required = name.startswith("text/") or name in _TEXT_TARGETS or name == KDE_HINT.lower()
        if not required and name.startswith(_HEAVY_MIME_PREFIXES):
            log.debug("пропущен тяжёлый формат буфера: %s", fmt)
            continue
        data = md.data(fmt)
        if not required and len(data) > _MAX_MIME_BYTES:
            log.debug("пропущен формат буфера свыше 1 МиБ: %s", fmt)
            continue
        snapshot[fmt] = bytes(data)
    return snapshot


def restore_mime(snapshot: dict[str, bytes]) -> Any:
    """Собрать новый QMimeData из копии байтов, не из прежнего указателя."""
    from PyQt5.QtCore import QByteArray, QMimeData

    md = QMimeData()
    for fmt, data in snapshot.items():
        md.setData(fmt, QByteArray(data))
    return md


class _Clipboard:
    """Ленивый адаптер Qt; QApplication создаёт только оркестрация."""

    def __init__(self) -> None:
        from PyQt5.QtCore import QThread
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance()
        if not isinstance(app, QApplication) or QThread.currentThread() != app.thread():
            raise RuntimeError("вставка требует GUI-потока существующего QApplication")
        self.cb = QApplication.clipboard()

    def _mode(self, primary: bool) -> Any:
        from PyQt5.QtGui import QClipboard

        return QClipboard.Selection if primary else QClipboard.Clipboard

    def snapshot(self, primary: bool) -> dict[str, bytes]:
        return snapshot_mime(self.cb.mimeData(self._mode(primary)))

    def put(self, snapshot: dict[str, bytes], primary: bool) -> None:
        self.cb.setMimeData(restore_mime(snapshot), self._mode(primary))

    def owns(self, primary: bool) -> bool:
        return bool(self.cb.ownsSelection() if primary else self.cb.ownsClipboard())

    def clear(self, primary: bool) -> None:
        self.cb.clear(self._mode(primary))


def _wait_ms(delay_ms: int) -> None:
    """Вложенный цикл Qt с таймером откладывает ввод в собственные окна.

    SelectionRequest должен обслуживаться, чтобы чужое окно получило буфер
    по Ctrl+V; XTest в чужое окно тоже продолжает работать. Поэтому time.sleep
    здесь недопустим. ExcludeUserInputEvents не глушит хоткей и доставку
    результатов через таймеры/сокеты: это обязанность оркестрации.
    """
    from PyQt5.QtCore import QEventLoop, Qt, QTimer

    loop = QEventLoop()
    timer = QTimer(loop)
    timer.setSingleShot(True)
    timer.setTimerType(Qt.PreciseTimer)
    timer.timeout.connect(loop.quit)
    timer.start(delay_ms)
    loop.exec_(QEventLoop.ExcludeUserInputEvents)


@cache
def _session_kind(mode: PasteMode) -> session.SessionKind:
    """Кэш на процесс; отдельная проба без X для ручного режима."""
    if mode == PasteMode.CLIPBOARD_ONLY:
        return session.detect({key: value for key, value in os.environ.items() if key != "DISPLAY"})
    return session.detect()


def _focus_matches(
    x: X11Display, target_window: int | None, wm_class: tuple[str, str] | None
) -> bool:
    """Сверить EWMH, реальный фокус и WM_CLASS запомненного клиента.

    Фокус может находиться у дочернего виджета: поднимаемся до клиента,
    не принимая другое окно того же класса, корень или override-redirect.
    Ошибка/исчезновение окна запрещает XTest; обход ограничен 32 предками.
    """
    try:
        if not target_window or not wm_class or x.active_window() != target_window:
            return False
        if x.d is None or x.root is None:
            return False
        window = x.d.get_input_focus().focus
        seen: set[int] = set()
        for _ in range(32):
            wid = int(getattr(window, "id", 0))
            if wid <= 1 or wid == int(x.root.id) or wid in seen:
                return False
            seen.add(wid)
            if window.get_attributes().override_redirect:
                return False
            if wid == target_window:
                return x.wm_class(wid) == wm_class
            window = window.query_tree().parent
    except Exception:
        # Содержимое исключения не журналируем: оно может содержать приватные данные.
        return False
    return False


def _publish(cb: _Clipboard, snapshot: dict[str, bytes], primary: bool) -> None:
    """Запомнить байтовую копию последней нашей публикации в CLIPBOARD."""
    global _last_clipboard_snapshot
    cb.put(snapshot, primary)
    if not primary:
        _last_clipboard_snapshot = snapshot.copy()


def _restore(
    cb: _Clipboard, saved: dict[str, bytes], primary: bool, *, saved_is_ours: bool = False
) -> PasteRestore:
    """Вернуть прежний буфер при сохранённом владении; чужой secret не переиздавать."""
    if not cb.owns(primary):
        return PasteRestore.SKIPPED_NOT_OWNER
    if saved.get(KDE_HINT) == b"secret" and not saved_is_ours:
        cb.clear(primary)
        return PasteRestore.CLEARED_SECRET
    if not saved:
        cb.clear(primary)
        return PasteRestore.CLEARED_EMPTY
    _publish(cb, saved, primary)
    return PasteRestore.RESTORED


@dataclass
class _PendingRestore:
    saved: dict[str, bytes]
    saved_is_ours: bool
    primary: dict[str, bytes] | None = None
    primary_touched: bool = False
    consumed: bool = False
    restore: PasteRestore = PasteRestore.SKIPPED_NOT_OWNER
    primary_restore: PasteRestore | None = None


def has_pending() -> bool:
    """Есть ли снимок AUTO, ожидающий восстановления.

    Ожидание активно и до определения исхода: наш текст уже опубликован,
    но XTest ещё не отработал. При исходе не pasted ожидание снимается.
    CLIPBOARD_ONLY не снимает снимок и не создаёт ожидание.
    """
    return _pending is not None


def restore_pending() -> bool:
    """Однократно вернуть снимок AUTO при завершении в GUI-потоке.

    До определения исхода (текст опубликован, XTest ещё не отработал)
    восстановление тоже разрешено. Правила владения и секрета — штатные.
    Без пауз, циклов событий и обращений к X11Display; ошибки, отсутствие
    QApplication или чужой поток дают False без содержимого буфера в логе.
    """
    global _pending
    try:
        pending = _pending
        if pending is None:
            return False
        cb = _Clipboard()
        _pending = None
        pending.consumed = True
        try:
            pending.restore = _restore(
                cb, pending.saved, False, saved_is_ours=pending.saved_is_ours
            )
        finally:
            if pending.primary_touched and pending.primary is not None:
                pending.primary_restore = _restore(cb, pending.primary, True)
        return any(
            result
            in (
                PasteRestore.RESTORED,
                PasteRestore.CLEARED_SECRET,
                PasteRestore.CLEARED_EMPTY,
            )
            for result in (pending.restore, pending.primary_restore)
        )
    except Exception:
        log.debug("не удалось аварийно восстановить буфер обмена")
        return False


class PasteFlow:
    """Цепочка S4 с переопределяемыми задержками для тестов.

    run возвращает окончательный исход через локальные циклы событий Qt.
    Повторный вход возвращает busy до завершения работы с обоими буферами.
    Штатно CLIPBOARD возвращается только после успешного XTest; иначе фраза
    остаётся для ручной вставки. restore_pending позволяет вернуть его раньше
    при завершении. PRIMARY возвращается по правилам владения/секрета.
    Перед паузой пробуем XGrabKeyboard на корне и сразу снимаем захват:
    XSetInputFocus не вызывается, XGetInputFocus по-прежнему указывает на цель;
    возможны FocusOut/FocusIn с NotifyGrab/NotifyUngrab. Принят остаточный риск
    гонки между пробой и XTest: атомарности в X11 нет (У12/У46).
    """

    def __init__(
        self, *, delay_before_ms: int = DELAY_BEFORE_MS, delay_after_ms: int = DELAY_AFTER_MS
    ) -> None:
        if delay_before_ms < 0 or delay_after_ms < 0:
            raise ValueError("задержки вставки не могут быть отрицательными")
        self.delay_before_ms = delay_before_ms
        self.delay_after_ms = delay_after_ms

    def run(self, text: str, target_window: int | None, mode: PasteMode) -> PasteOutcome:
        """Вернуть исход без исключений и закрыть собственное X11-соединение."""
        global _running, _pending
        if _running:
            # BUSY не создаёт снимок и не отменяет ожидание внешней цепочки.
            return PasteOutcome(
                PasteOutcomeKind.BUSY, PasteMethod.NONE, PasteRestore.KEPT_OURS, None, 0, 0, 0.0
            )
        _running = True
        x: X11Display | None = None
        try:
            cb = _Clipboard()
            if mode == PasteMode.AUTO:
                x = X11Display()
                x.open()
            return self._run(text, target_window, mode, cb, x)
        except Exception:
            # Нет QApplication/GUI-потока либо ошибка до публикации. Ничего не обещаем
            # о наличии фразы в буфере и не выдаём сообщение исключения наружу.
            return PasteOutcome(
                PasteOutcomeKind.FAILED,
                PasteMethod.NONE,
                PasteRestore.SKIPPED_NOT_OWNER,
                None,
                0,
                0,
                0.0,
            )
        finally:
            _pending = None
            try:
                if x is not None:
                    x.close()
            except Exception:
                pass
            finally:
                _running = False

    def _run(
        self,
        text: str,
        target_window: int | None,
        mode: PasteMode,
        cb: _Clipboard,
        x: X11Display | None,
    ) -> PasteOutcome:
        global _pending
        wm_class = x.wm_class(target_window) if x is not None and target_window else None
        planned = method_for_wm_class(wm_class) if x is not None else PasteMethod.NONE
        saved = cb.snapshot(False) if mode == PasteMode.AUTO else None
        # Владение проверяется до новой публикации: один hint не доказывает авторство.
        saved_is_ours = saved is not None and saved == _last_clipboard_snapshot and cb.owns(False)
        primary = cb.snapshot(True) if planned == PasteMethod.SHIFT_INSERT else None
        pending = _PendingRestore(saved, saved_is_ours, primary) if saved is not None else None
        normalized = normalize(text)
        out = {"text/plain": normalized.encode("utf-8", errors="replace"), KDE_HINT: b"secret"}
        # Ручной режим не открывает X даже косвенно через определение сеанса.
        kind_session = _session_kind(mode)
        if kind_session == session.SessionKind.FLY:
            out.setdefault(_fly_blacklist_type(occupied=out), b"")
        kind = PasteOutcomeKind.FAILED
        method = PasteMethod.NONE
        restore = PasteRestore.SKIPPED_NOT_OWNER
        primary_restore: PasteRestore | None = None
        primary_touched = False
        started = time.monotonic()
        try:
            _pending = pending
            _publish(cb, out, False)
            kind = PasteOutcomeKind.CLIPBOARD_ONLY
            restore = PasteRestore.KEPT_OURS
            if primary is not None:
                primary_touched = True
                if pending is not None:
                    pending.primary_touched = True
                cb.put(out, True)
            keyboard_available = False
            if x is not None and x.grab_keyboard():
                x.ungrab_keyboard()
                keyboard_available = x.keyboard_grab_deadline is None
            _wait_ms(self.delay_before_ms)
            if pending is not None and pending.consumed:
                kind = PasteOutcomeKind.WINDOW_CHANGED
            elif x is not None:
                if not keyboard_available or not _focus_matches(x, target_window, wm_class):
                    kind = PasteOutcomeKind.WINDOW_CHANGED
                else:
                    mods, key = {
                        PasteMethod.CTRL_V: (["Control_L"], "v"),
                        PasteMethod.CTRL_SHIFT_V: (["Control_L", "Shift_L"], "v"),
                        PasteMethod.SHIFT_INSERT: (["Shift_L"], "Insert"),
                    }[planned]
                    if x.send_combo(mods, key):
                        kind, method = PasteOutcomeKind.PASTED, planned
                    else:
                        kind = PasteOutcomeKind.WINDOW_CHANGED
            if kind != PasteOutcomeKind.PASTED:
                _pending = None
            _wait_ms(self.delay_after_ms)
        except Exception:
            # После публикации оставляем фразу для ручной вставки. Если XTest уже
            # прошёл успешно, сохраняем PASTED и штатно восстанавливаем CLIPBOARD.
            pass
        finally:
            _pending = None
            try:
                if pending is not None and pending.consumed:
                    restore = pending.restore
                    primary_restore = pending.primary_restore
                elif kind == PasteOutcomeKind.PASTED and saved is not None:
                    restore = _restore(cb, saved, False, saved_is_ours=saved_is_ours)
            except Exception:
                kind = PasteOutcomeKind.FAILED
                restore = PasteRestore.FAILED
            finally:
                try:
                    if (
                        primary_touched
                        and primary is not None
                        and pending is not None
                        and not pending.consumed
                    ):
                        primary_restore = _restore(cb, primary, True)
                except Exception:
                    kind = PasteOutcomeKind.FAILED
        if PasteRestore.CLEARED_SECRET in (restore, primary_restore):
            kind = PasteOutcomeKind.REFUSED_SECRET
        return PasteOutcome(
            kind=kind,
            method=method,
            restore=restore,
            wm_class=wm_class,
            chars=len(normalized),
            stripped_controls=sum(
                ch not in "\r\n" and (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F) for ch in text
            ),
            t_ms=round((time.monotonic() - started) * 1000, 1),
        )


def paste_text(text: str, target_window: int | None, mode: PasteMode) -> PasteOutcome:
    """Проба захвата → 50 мс → проверка фокуса/XTest → 100 мс → возврат при pasted."""
    return PasteFlow().run(text, target_window, mode)
