"""Штатные уведомления с действиями и без распознанного текста.

Остальной код вызывает готовые обёртки; переменные подписи — сочетание клавиш
и системное описание микрофона. Запрет текста диктовки проверяет AST-тест пакета.
Пока ожидается ID, последнее новое сообщение занимает один слот. Через 1,5 с
слот отправляется с последним подтверждённым ID; поздний ответ всё ещё учитывается.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape
from queue import Empty, Queue
from time import monotonic
from typing import Any, cast

from PyQt5 import sip
from PyQt5.QtCore import (
    QCoreApplication,
    QMetaObject,
    QMetaType,
    QObject,
    Qt,
    QThread,
    QTimer,
    QVariant,
    pyqtSlot,
)
from PyQt5.QtDBus import QDBusConnection, QDBusMessage, QDBusPendingCallWatcher, QDBusPendingReply

# Совместимость с подменой старого транспорта в тестах соседней зоны.
# Интерфейс не создаём: его конструктор синхронно запрашивает Introspect.
from PyQt5.QtDBus import QDBusInterface as QDBusInterface

__all__ = [
    "ACTION_CHOOSE_HOTKEY",
    "ACTION_CHOOSE_MICROPHONE",
    "ACTION_OPEN_SOUND_SETTINGS",
    "ACTION_SHOW_DETAILS",
    "drop_pending",
    "flush_pending",
    "install_dispatcher",
    "last_delivery_ok",
    "notify",
    "notify_engine_failed",
    "notify_hotkey_not_grabbed",
    "notify_hotkey_regrabbed",
    "notify_indicators_lost",
    "notify_microphone_changed",
    "notify_microphone_lost",
    "notify_microphone_muted",
    "notify_microphone_selected",
    "notify_microphone_too_quiet",
    "notify_onboarding_ready",
    "notify_model_installed",
    "notify_selfcheck_failed",
    "notify_tray_depends_on_panel",
    "notify_tray_unavailable",
    "pending_count",
    "reset_state",
    "set_action_handler",
    "shutdown_dispatch",
]

ACTION_CHOOSE_HOTKEY = "choose-hotkey"
ACTION_CHOOSE_MICROPHONE = "choose-microphone"
ACTION_OPEN_SOUND_SETTINGS = "open-sound-settings"
ACTION_SHOW_DETAILS = "show-details"

# Тексты причин «вас не слышно»: без децибел, процентов, имён устройств и служб.
_MIC_MUTED_TITLE = "Микрофон выключен"
_MIC_MUTED_BODY = "Звук микрофона выключен в настройках системы, поэтому программа вас не слышит."
_MIC_QUIET_TITLE = "Микрофон почти не слышно"
_MIC_QUIET_BODY = "Громкость микрофона в системе слишком низкая — вас плохо слышно."
_OPEN_SOUND_SETTINGS = "Открыть настройки звука"

_logger = logging.getLogger(__name__)
_URGENCY = {"low": 0, "normal": 1, "critical": 2}
_ACTION_SIGNAL = (
    "org.freedesktop.Notifications",
    "/org/freedesktop/Notifications",
    "org.freedesktop.Notifications",
    "ActionInvoked",
    "us",
)
_last_id = 0
_last_delivery_ok = False
_last_completed_seq = 0
_last_id_seq = 0
_seq = 0
_epoch = 0
_in_flight_seq: int | None = None
_slot: _Notice | None = None
_queued: deque[_Notice] = deque()
_watchers: dict[int, QDBusPendingCallWatcher] = {}
_sent: dict[int, tuple[_Notice, int]] = {}
_timeout_streak = 0
_timeout_warned = False
_CALL_TIMEOUT_MS = 10_000
_WAIT_FOR_ID_MS = 1500
_PENDING_TTL_S = 120
_TIMEOUT_ERRORS = frozenset(
    (
        "org.freedesktop.DBus.Error.NoReply",
        "org.freedesktop.DBus.Error.Timeout",
        "org.freedesktop.DBus.Error.TimedOut",
    )
)
_NOT_DELIVERED_ERRORS = frozenset(
    (
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.Disconnected",
    )
)
_INVALID_REPLY = "invalid-reply"
_TRANSPORT_ERROR = "transport-error"
# При переполнении сохраняем последние восемь уникальных уведомлений.
_MAX_NOTIFICATIONS = 8


@dataclass(frozen=True)
class _Notice:
    summary: str
    body: str
    urgency: int
    actions: tuple[tuple[str, str], ...]
    created_at: float
    retry: bool


_pending: deque[_Notice] = deque(maxlen=_MAX_NOTIFICATIONS)
_action_handlers: dict[str, Callable[[], None]] = {}
_notification_actions: OrderedDict[int, set[str]] = OrderedDict()
_action_id_seq: OrderedDict[int, int] = OrderedDict()
_action_bus: QDBusConnection | None = None
_action_receiver: _ActionReceiver | None = None


# Вызовы notify() не из GUI-потока (уроки 025, 026): рабочий поток не создаёт
# QObject и не кладёт Python-объекты в очередь Qt. Уведомление — Python-значение в
# _inbox; GUI будит бессмертный получатель через invokeMethod(QueuedConnection)
# без аргументов: в очереди Qt лежит только C++-событие MetaCall, для удаления
# которого (в т. ч. в ~QApplication) GIL не нужен. Получатель создаётся в GUI при
# старте (install_dispatcher); до этого уведомления потоков копятся в _inbox.
# Рабочий поток Qt не трогает ни до установки, ни после закрытия затвора.
# Затвор _gate/_closed закрывается до разрушения QApplication (shutdown_dispatch).
_inbox: Queue[tuple[_Notice, int]] = Queue()
_gate = threading.Lock()
_closed = False
# Пробуждение уже в очереди Qt: посты сливаются в одно событие MetaCall.
_wake_pending = False
_receiver: _NotifyReceiver | None = None
# Поток GUI, в котором создан получатель; сравнение идёт по threading.get_ident():
# QThread.currentThread() в чужом потоке создал бы там QAdoptedThread.
_gui_ident: int | None = None


class _NotifyReceiver(QObject):
    """Бессмертный получатель пробуждений, всегда в потоке GUI."""

    @pyqtSlot()
    def _drain(self) -> None:
        global _wake_pending
        # Флаг снимается до разбора: пост во время разбора разбудит заново.
        with _gate:
            _wake_pending = False
        while True:
            try:
                notice, epoch = _inbox.get_nowait()
            except Empty:
                return
            if epoch == _epoch:
                _submit(notice)


def _install_receiver(app: QCoreApplication) -> bool:
    """Создать получатель в GUI и разобрать накопленное; False — поток не GUI."""
    global _receiver, _gui_ident
    # Сначала без Qt: QThread.currentThread() в чужом потоке создал бы QAdoptedThread.
    if threading.current_thread() is not threading.main_thread():
        return False
    if QThread.currentThread() != app.thread():
        return False
    receiver = _receiver
    if receiver is None:
        receiver = _NotifyReceiver()
        # Объект не удаляется никогда: ни Python, ни чужая инфраструктура Qt.
        sip.transferto(receiver, None)
        with _gate:
            _receiver = receiver
            _gui_ident = threading.get_ident()
    # Уведомления потоков, пришедшие до создания получателя.
    receiver._drain()
    return True


def install_dispatcher() -> None:
    """Создать получатель уведомлений из рабочих потоков; вызывать в GUI при старте."""
    if threading.current_thread() is not threading.main_thread():
        # Отказ до любого обращения к Qt.
        raise RuntimeError("Получатель уведомлений должен создаваться в потоке GUI")
    app = QCoreApplication.instance()
    if app is None or not _install_receiver(app):
        raise RuntimeError("Получатель уведомлений должен создаваться в потоке GUI")


def shutdown_dispatch() -> None:
    """Закрыть затвор до разрушения QApplication.

    После возврата рабочие потоки больше не постят в очередь событий Qt: их
    уведомления отбрасываются. Вызовы из GUI-потока работают как прежде.
    """
    global _closed
    with _gate:
        _closed = True


def _post_from_thread(notice: _Notice) -> None:
    """Из не-GUI потока: только значение в очереди и C++-пробуждение под затвором."""
    global _wake_pending
    with _gate:
        if _closed:
            _logger.debug("Уведомление из потока после остановки отброшено")
            return
        _inbox.put((notice, _epoch))
        # До установки получателя Qt не трогаем: очередь разберёт install_dispatcher().
        if _receiver is not None and not _wake_pending:
            _wake_pending = True
            try:
                posted = QMetaObject.invokeMethod(
                    _receiver, "_drain", Qt.ConnectionType.QueuedConnection
                )
            except Exception:
                # Отказ Qt не должен долететь до вызывающего рабочего потока.
                posted = False
            if not posted:
                # Событие не поставлено: без сброса флага следующие посты молча копятся.
                _wake_pending = False
                _logger.debug("Не удалось разбудить получатель уведомлений из потока")


class _ActionReceiver(QObject):
    """Живёт в модуле, чтобы Qt мог доставлять сигналы после возврата Notify."""

    @pyqtSlot("uint", str)
    def on_action_invoked(self, notification_id: int, key: str) -> None:
        keys = _notification_actions.get(notification_id)
        if keys is None:
            _logger.debug("Действие неизвестного уведомления: %s", notification_id)
            return
        handler = _action_handlers.get(key)
        if key not in keys or handler is None:
            _logger.debug("Неизвестное действие уведомления: %s", notification_id)
            return
        # Уведомления без resident закрываются после выбора действия.
        # Убираем запись до колбэка: повторный сигнал не вызовет его дважды.
        del _notification_actions[notification_id]
        try:
            handler()
        except Exception as exc:
            # Текст исключения может содержать диктовку. Сохраняем тип и стек,
            # но исключаем исходное сообщение и цепочку исключений из журнала.
            try:
                raise RuntimeError(
                    f"Ошибка обработчика уведомления ({type(exc).__name__})"
                ).with_traceback(exc.__traceback__) from None
            except RuntimeError:
                _logger.warning("Не удалось выполнить действие уведомления", exc_info=True)


def set_action_handler(key: str, callback: Callable[[], None] | None) -> None:
    """Зарегистрировать обработчик кнопки; None снимает регистрацию."""
    if callback is None:
        _action_handlers.pop(key, None)
    else:
        _action_handlers[key] = callback


def _connect_actions(bus: QDBusConnection) -> None:
    """Подписаться один раз; отсутствие подписки не мешает показу сообщения."""
    global _action_bus, _action_receiver
    if _action_bus is not None and _action_bus.isConnected():
        return
    if _action_receiver is None:
        _action_receiver = _ActionReceiver()
    try:
        if bus.connect(*_ACTION_SIGNAL, _action_receiver.on_action_invoked):
            _action_bus = bus
            return
    except Exception:
        pass
    _logger.debug("Не удалось подписаться на действия уведомлений")


def reset_state() -> None:
    """Очистить доставку, очередь и обработчики для изоляции тестов."""
    global _last_id, _last_delivery_ok, _last_completed_seq, _last_id_seq, _seq, _epoch
    global _in_flight_seq, _slot, _action_bus, _timeout_streak, _timeout_warned, _closed
    global _wake_pending
    # Получатель бессмертен и не пересоздаётся; сбрасываются очередь и затвор.
    with _gate:
        _epoch += 1
        _closed = False
        _wake_pending = False
        while True:
            try:
                _inbox.get_nowait()
            except Empty:
                break
    _last_id = 0
    _last_delivery_ok = False
    _last_completed_seq = 0
    _last_id_seq = 0
    _seq = 0
    _in_flight_seq = None
    _slot = None
    _queued.clear()
    _sent.clear()
    for watcher in _watchers.values():
        watcher.deleteLater()
    _watchers.clear()
    _timeout_streak = 0
    _timeout_warned = False
    _pending.clear()
    _action_handlers.clear()
    _notification_actions.clear()
    _action_id_seq.clear()
    if _action_bus is not None and _action_receiver is not None:
        try:
            _action_bus.disconnect(*_ACTION_SIGNAL, _action_receiver.on_action_invoked)
        except Exception:
            _logger.debug("Не удалось отключить сигналы уведомлений")
    _action_bus = None


def pending_count() -> int:
    """Вернуть число уникальных уведомлений, ожидающих доставки."""
    return len(_pending)


def drop_pending(summary: str) -> int:
    """Удалить все отложенные уведомления с этим заголовком; вернуть их число."""
    global _slot
    messages = [message for message in _pending if message.summary == summary]
    for message in messages:
        _pending.remove(message)
    removed = len(messages)
    if _slot is not None and _slot.summary == summary:
        _slot = None
        removed += 1
    kept = [notice for notice in _queued if notice.summary != summary]
    removed += len(_queued) - len(kept)
    _queued.clear()
    _queued.extend(kept)
    return removed


def last_delivery_ok() -> bool:
    """True после ответа с ID от самой новой завершившейся отправки."""
    return _last_delivery_ok


def flush_pending() -> int:
    """Передать свежие записи в асинхронную очередь; вернуть их число."""
    _expire_pending()
    count = 0
    for notice in tuple(_pending):
        if not _already_active(notice):
            _queued.append(notice)
            count += 1
    _drain()
    return count


def _expire_pending() -> None:
    expired = len(_pending)
    fresh = [notice for notice in _pending if monotonic() - notice.created_at <= _PENDING_TTL_S]
    expired -= len(fresh)
    _pending.clear()
    _pending.extend(fresh)
    if expired:
        _logger.info("Отброшено устаревших отложенных уведомлений: %d", expired)


def _same_notice(left: _Notice, right: _Notice) -> bool:
    return (left.summary, left.body, left.urgency, left.actions) == (
        right.summary,
        right.body,
        right.urgency,
        right.actions,
    )


def _already_active(notice: _Notice) -> bool:
    return (
        any(_same_notice(notice, active) for active, _ in _sent.values())
        or (_slot is not None and _same_notice(notice, _slot))
        or any(_same_notice(notice, queued) for queued in _queued)
    )


def _remember_pending(notice: _Notice) -> None:
    if not notice.retry:
        return
    _expire_pending()
    if monotonic() - notice.created_at > _PENDING_TTL_S or _already_active(notice):
        return
    if not any(_same_notice(notice, pending) for pending in _pending):
        _pending.append(notice)


def _send(
    seq: int,
    notice: _Notice,
    replaces_id: int,
) -> None:
    """Отправить без ожидания; watcher живёт в словаре до callback или reset."""
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        _on_reply(seq, None, "org.freedesktop.DBus.Error.Disconnected")
        return
    _connect_actions(bus)

    message = QDBusMessage.createMethodCall(
        "org.freedesktop.Notifications",
        "/org/freedesktop/Notifications",
        "org.freedesktop.Notifications",
        "Notify",
    )

    # Python int и пустой list сами по себе дают неверные типы D-Bus.
    replacement = QVariant(replaces_id)
    replacement.convert(QVariant.UInt)
    action_list = QVariant([item for action in notice.actions for item in action])
    action_list.convert(QVariant.StringList)
    priority = QVariant(notice.urgency)
    priority.convert(QMetaType.UChar)
    message.setArguments(
        [
            "Astra Voice",
            replacement,
            "astravoice",
            notice.summary,
            notice.body,
            action_list,
            {"urgency": priority},
            -1,
        ]
    )
    call = bus.asyncCall(message, _CALL_TIMEOUT_MS)
    watcher = QDBusPendingCallWatcher(call)
    _watchers[seq] = watcher
    watcher.finished.connect(lambda finished, seq=seq: _finish(seq, finished))


def _finish(seq: int, watcher: QDBusPendingCallWatcher) -> None:
    """Скопировать ответ в Python-данные до уничтожения Qt D-Bus объектов."""
    if _watchers.get(seq) is not watcher:
        return
    try:
        reply = cast(Any, QDBusPendingReply(watcher)).reply()
        if reply.type() == QDBusMessage.ReplyMessage:
            args = reply.arguments()
            if (
                reply.signature() == "u"
                and len(args) == 1
                and type(args[0]) is int
                and 0 < args[0] <= 0xFFFFFFFF
            ):
                notification_id, error_name = args[0], None
            else:
                notification_id, error_name = None, _INVALID_REPLY
        else:
            notification_id, error_name = None, reply.errorName() or _TRANSPORT_ERROR
    except Exception:
        notification_id, error_name = None, _TRANSPORT_ERROR
    _watchers.pop(seq).deleteLater()
    _on_reply(seq, notification_id, error_name)


def _on_reply(seq: int, notification_id: int | None, error_name: str | None) -> None:
    """Обработать завершение по порядковому номеру без D-Bus типов."""
    global _last_id, _last_id_seq, _last_delivery_ok, _last_completed_seq, _in_flight_seq
    global _timeout_streak, _timeout_warned
    sent = _sent.pop(seq, None)
    if sent is None:
        return
    notice, replaces_id = sent
    if seq > _last_completed_seq:
        _last_completed_seq = seq
        _last_delivery_ok = notification_id is not None
    if notification_id is not None:
        _timeout_streak = 0
        _timeout_warned = False
        if seq > _last_id_seq:
            _last_id, _last_id_seq = notification_id, seq
        if (
            replaces_id
            and replaces_id != notification_id
            and seq >= _action_id_seq.get(replaces_id, 0)
        ):
            _notification_actions.pop(replaces_id, None)
        if seq >= _action_id_seq.get(notification_id, 0):
            _notification_actions.pop(notification_id, None)
            _action_id_seq.pop(notification_id, None)
            _action_id_seq[notification_id] = seq
            if notice.actions:
                _notification_actions[notification_id] = {key for key, _ in notice.actions}
            while len(_action_id_seq) > _MAX_NOTIFICATIONS:
                old_id, _ = _action_id_seq.popitem(last=False)
                _notification_actions.pop(old_id, None)
        for pending in tuple(_pending):
            if _same_notice(pending, notice):
                _pending.remove(pending)
    elif error_name in _TIMEOUT_ERRORS:
        _timeout_streak += 1
        _logger.info(
            "Служба уведомлений не ответила вовремя; сообщение могло быть показано: %s",
            notice.summary,
        )
        if _timeout_streak >= 3 and not _timeout_warned:
            _timeout_warned = True
            _logger.warning("Служба уведомлений не отвечает вовремя")
    elif (
        error_name in _NOT_DELIVERED_ERRORS
        or error_name == _TRANSPORT_ERROR
        or (error_name is not None and error_name.startswith("org.freedesktop.DBus.Error.Spawn."))
    ):
        _timeout_streak = 0
        _timeout_warned = False
        _logger.warning("Не удалось показать уведомление: %s", notice.summary)
        _remember_pending(notice)
    elif error_name == _INVALID_REPLY:
        _timeout_streak = 0
        _timeout_warned = False
        _logger.warning("Служба уведомлений вернула неверный ответ")
    else:
        _timeout_streak = 0
        _timeout_warned = False
        _logger.warning("Служба уведомлений вернула ошибку: %s", error_name)
    if _in_flight_seq == seq:
        _in_flight_seq = None
        _drain()


def _on_wait_elapsed(seq: int, epoch: int) -> None:
    global _in_flight_seq
    if epoch == _epoch and _in_flight_seq == seq:
        _in_flight_seq = None
        _drain()


def _drain() -> None:
    global _in_flight_seq, _slot, _seq
    if _in_flight_seq is not None:
        return
    if _queued:
        notice = _queued.popleft()
    elif _slot is not None:
        notice, _slot = _slot, None
    else:
        return
    for pending in tuple(_pending):
        if _same_notice(notice, pending):
            _pending.remove(pending)
    _seq += 1
    seq = _seq
    _sent[seq] = (notice, _last_id)
    _in_flight_seq = seq
    try:
        _send(seq, notice, _last_id)
    except Exception:
        _on_reply(seq, None, _TRANSPORT_ERROR)
    if _in_flight_seq == seq:
        epoch = _epoch
        QTimer.singleShot(_WAIT_FOR_ID_MS, lambda: _on_wait_elapsed(seq, epoch))


def _submit(notice: _Notice) -> None:
    global _slot
    if _slot is not None and _slot.retry and not notice.retry:
        _logger.debug("Ситуативное уведомление отброшено: в очереди важное")
        return
    _slot = notice
    _drain()


def notify(
    summary: str,
    body: str = "",
    *,
    urgency: str = "normal",
    actions: Sequence[tuple[str, str]] = (),
    retry: bool = True,
) -> None:
    """Показать уведомление; внутри проекта доступно только готовым обёрткам.

    Неизвестная срочность — ошибка вызывающего кода. При подтверждённом
    недоставлении retry разрешает повтор. Тело никогда не попадает в журнал.
    """
    if urgency not in _URGENCY:
        raise ValueError("Неизвестная срочность уведомления")
    notice = _Notice(summary, body, _URGENCY[urgency], tuple(actions), monotonic(), retry)
    # Штатные вызовы из runtime, tray, bridges и app идут через Qt GUI thread;
    # вызов из другого потока передаём туда до работы с QtDBus.
    if threading.get_ident() == _gui_ident:
        _submit(notice)
        return
    # Рабочие потоки Qt не трогают вовсе: даже QCoreApplication.instance() — только
    # в главном. Без приложения главный поток доставляет сразу, как раньше.
    if _gui_ident is None and threading.current_thread() is threading.main_thread():
        app = QCoreApplication.instance()
        if app is None or _install_receiver(app):
            _submit(notice)
            return
    _post_from_thread(notice)


def notify_hotkey_not_grabbed(combo: str) -> None:
    """Сообщить, что сочетание клавиш занято другой программой."""
    notify(
        f"Горячая клавиша {combo} занята другой программой",
        "Как только она освободится, диктовка заработает сама.",
        actions=[(ACTION_CHOOSE_HOTKEY, "Выбрать другую")],
    )


_HOTKEY_LOST_DEFAULT_BODY = (
    "Сочетание клавиш перестало работать. Программа продолжит пробовать вернуть его сама. "
    "Если не получится, откройте настройки и назначьте его снова."
)
_HOTKEY_LOST_BODIES = {
    "bad-combo": (
        "В текущей раскладке клавиатуры нет такого сочетания. Верните прежнюю раскладку "
        "или назначьте другое сочетание в настройках. Программа продолжит пробовать "
        "вернуть клавишу сама."
    ),
    "duplicate": (
        "Сочетание совпало с другой клавишей программы. Назначьте другое сочетание "
        "в настройках. Программа продолжит пробовать вернуть клавишу сама."
    ),
    "not-grabbed": _HOTKEY_LOST_DEFAULT_BODY,
}


def notify_hotkey_lost(code: str = "") -> None:
    """Сообщить о потере горячей клавиши с понятной причиной."""
    notify(
        "Горячая клавиша перестала работать",
        _HOTKEY_LOST_BODIES.get(code, _HOTKEY_LOST_DEFAULT_BODY),
        actions=[(ACTION_CHOOSE_HOTKEY, "Выбрать другую")],
    )


def notify_hotkey_regrabbed(combo: str) -> None:
    """Сообщить об автоматическом восстановлении горячей клавиши."""
    notify(f"Горячая клавиша снова работает: {combo}", urgency="normal", retry=False)


def notify_onboarding_ready(combo: str) -> None:
    """Сообщить о завершении первого запуска и напомнить комбинацию."""
    notify("Astra Voice готов", f"Зажмите {combo} и говорите.")


def notify_microphone_changed(name: str) -> None:
    """Сообщить о смене микрофона, используя его человекочитаемое описание."""
    notify(
        "Микрофон сменился",
        f"Сейчас используется: {escape(name, quote=False)}. Выбрать другой можно в настройках.",
        actions=[(ACTION_CHOOSE_MICROPHONE, "Выбрать микрофон")],
        retry=False,
    )


def notify_microphone_lost(*, during_recording: bool = False) -> None:
    """Сообщить о пропаже микрофона; кнопка ведёт к выбору другого в настройках.

    На старте диктовки (явно выбранного микрофона нет) — важное уведомление с повтором
    доставки. Посреди записи — обычное и без повтора: дребезг разъёма не копит очередь.
    """
    if during_recording:
        notify(
            "Микрофон отключился",
            "Проверьте подключение или выберите микрофон в настройках.",
            actions=[(ACTION_CHOOSE_MICROPHONE, "Выбрать микрофон")],
            retry=False,
        )
    else:
        notify(
            "Микрофон отключился",
            "Проверьте подключение или выберите микрофон в настройках.",
            urgency="critical",
            actions=[(ACTION_CHOOSE_MICROPHONE, "Выбрать микрофон")],
        )


def notify_microphone_muted() -> None:
    """Сообщить, что звук выбранного микрофона выключен в настройках системы."""
    notify(
        _MIC_MUTED_TITLE,
        _MIC_MUTED_BODY,
        actions=[(ACTION_OPEN_SOUND_SETTINGS, _OPEN_SOUND_SETTINGS)],
        retry=False,
    )


def notify_microphone_too_quiet() -> None:
    """Сообщить, что системная громкость выбранного микрофона слишком низкая."""
    notify(
        _MIC_QUIET_TITLE,
        _MIC_QUIET_BODY,
        actions=[(ACTION_OPEN_SOUND_SETTINGS, _OPEN_SOUND_SETTINGS)],
        retry=False,
    )


def notify_microphone_selected(name: str) -> None:
    """Объявить системное описание нового микрофона перед диктовкой."""
    notify(f"Микрофон: {name}", retry=False)


def notify_tray_unavailable() -> None:
    """Сообщить об отсутствии значка на панели."""
    notify(
        "Значок не появился на панели",
        "Программа работает, но значка на панели нет. Показ можно проверить в настройках.",
    )


def notify_tray_depends_on_panel() -> None:
    """Предупредить, что без пилюли запись показывает только панель рабочего стола."""
    notify(
        "Виден только значок на панели",
        "Если панель перезапустится, показывать запись будет нечем. "
        "Включите указатель записи в настройках.",
    )


def notify_indicators_lost() -> None:
    """Сообщить об остановке записи после потери всех её указателей."""
    notify(
        "Запись остановлена",
        "Пропали все указатели записи, поэтому запись остановлена.",
        urgency="critical",
    )


def notify_engine_failed() -> None:
    """Сообщить, что движок распознавания не удалось запустить."""
    notify(
        "Не удалось запустить распознавание",
        "Попробуйте переустановить модель.",
        urgency="critical",
        actions=[(ACTION_SHOW_DETAILS, "Подробности")],
    )


def notify_selfcheck_failed() -> None:
    """Сообщить, что модель не прошла пробное распознавание."""
    notify(
        "Распознавание на этом компьютере не работает",
        "Обратитесь к администратору.",
        urgency="critical",
        actions=[(ACTION_SHOW_DETAILS, "Подробности")],
    )


def notify_model_revoked() -> None:
    """Сообщить, что издатель отозвал установленную версию модели (US-6.6)."""
    notify(
        "Модель недоступна",
        "Эта версия модели отозвана. Установите рекомендованную модель.",
        urgency="critical",
    )


def notify_model_installed() -> None:
    """Сообщить о готовности модели после фоновой установки."""
    notify("Модель установлена", "Можно диктовать.", urgency="normal")
