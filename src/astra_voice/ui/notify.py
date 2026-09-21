"""Штатные уведомления с действиями и без распознанного текста.

Остальной код вызывает готовые обёртки; переменные подписи — сочетание клавиш
и системное описание микрофона. Запрет текста диктовки проверяет AST-тест пакета.
"""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from time import monotonic

from PyQt5.QtCore import QMetaType, QObject, QVariant, pyqtSlot
from PyQt5.QtDBus import QDBus, QDBusConnection, QDBusMessage

# Совместимость с подменой старого транспорта в тестах соседней зоны.
# Интерфейс не создаём: его конструктор синхронно запрашивает Introspect.
from PyQt5.QtDBus import QDBusInterface as QDBusInterface

__all__ = [
    "ACTION_CHOOSE_HOTKEY",
    "ACTION_CHOOSE_MICROPHONE",
    "ACTION_SHOW_DETAILS",
    "drop_pending",
    "flush_pending",
    "last_delivery_ok",
    "notify",
    "notify_engine_failed",
    "notify_hotkey_not_grabbed",
    "notify_hotkey_regrabbed",
    "notify_indicators_lost",
    "notify_microphone_changed",
    "notify_microphone_lost",
    "notify_microphone_selected",
    "notify_onboarding_ready",
    "notify_model_installed",
    "notify_selfcheck_failed",
    "notify_tray_depends_on_panel",
    "notify_tray_unavailable",
    "pending_count",
    "reset_state",
    "set_action_handler",
]

ACTION_CHOOSE_HOTKEY = "choose-hotkey"
ACTION_CHOOSE_MICROPHONE = "choose-microphone"
ACTION_SHOW_DETAILS = "show-details"

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
# При переполнении сохраняем последние восемь уникальных уведомлений.
_MAX_NOTIFICATIONS = 8
_pending: deque[tuple[str, str, int, tuple[tuple[str, str], ...]]] = deque(
    maxlen=_MAX_NOTIFICATIONS
)
_action_handlers: dict[str, Callable[[], None]] = {}
_notification_actions: OrderedDict[int, set[str]] = OrderedDict()
_action_bus: QDBusConnection | None = None
_action_receiver: _ActionReceiver | None = None


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
    global _last_id, _last_delivery_ok, _action_bus
    _last_id = 0
    _last_delivery_ok = False
    _pending.clear()
    _action_handlers.clear()
    _notification_actions.clear()
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
    messages = [message for message in _pending if message[0] == summary]
    for message in messages:
        _pending.remove(message)
    return len(messages)


def last_delivery_ok() -> bool:
    """Вернуть результат последней попытки доставки; до первой попытки — False."""
    return _last_delivery_ok


def flush_pending() -> int:
    """Повторить каждое отложенное уведомление один раз; вернуть число доставленных."""
    delivered = 0
    for message in tuple(_pending):
        if _deliver(*message):
            _pending.remove(message)
            delivered += 1
    return delivered


def _send(
    summary: str,
    body: str,
    urgency: int,
    replaces_id: int,
    actions: Sequence[tuple[str, str]] = (),
) -> int:
    """Отправить сообщение через QtDBus; точка подмены транспорта в тестах."""
    # Из общего бюджета notify() 500 мс оставляем 50 мс на обработку результата.
    deadline = monotonic() + 0.450
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        _logger.debug("Шина уведомлений недоступна")
        return 0
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
    action_list = QVariant([item for action in actions for item in action])
    action_list.convert(QVariant.StringList)
    priority = QVariant(urgency)
    priority.convert(QMetaType.UChar)
    message.setArguments(
        [
            "Astra Voice",
            replacement,
            "astravoice",
            summary,
            body,
            action_list,
            {"urgency": priority},
            -1,
        ]
    )
    timeout_ms = int((deadline - monotonic()) * 1000)
    if timeout_ms <= 0:
        return 0
    reply = bus.call(message, QDBus.Block, timeout_ms)
    if reply.type() != QDBusMessage.ReplyMessage:
        _logger.debug("Служба уведомлений вернула ошибку")
        return 0
    arguments = reply.arguments()
    if len(arguments) != 1 or type(arguments[0]) is not int or not 0 < arguments[0] <= 0xFFFFFFFF:
        _logger.warning("Служба уведомлений вернула неверный ответ")
        return 0
    notification_id: int = arguments[0]
    return notification_id


def _deliver(summary: str, body: str, urgency: int, actions: tuple[tuple[str, str], ...]) -> bool:
    """Проверить доставку, записывая при сбое только заголовок-константу."""
    global _last_id, _last_delivery_ok
    replaces_id = _last_id
    try:
        _last_id = _send(summary, body, urgency, replaces_id, actions)
    except Exception:
        # Исключение транспорта тоже может содержать переданное сообщение.
        _last_id = 0
    _last_delivery_ok = _last_id != 0
    _notification_actions.pop(replaces_id, None)
    if not _last_delivery_ok:
        _logger.warning("Не удалось показать уведомление: %s", summary)
    else:
        _notification_actions.pop(_last_id, None)
        if actions:
            _notification_actions[_last_id] = {key for key, _ in actions}
            while len(_notification_actions) > _MAX_NOTIFICATIONS:
                _notification_actions.popitem(last=False)
    return _last_delivery_ok


def notify(
    summary: str,
    body: str = "",
    *,
    urgency: str = "normal",
    actions: Sequence[tuple[str, str]] = (),
) -> None:
    """Показать уведомление; внутри проекта доступно только готовым обёрткам.

    Неизвестная срочность — ошибка вызывающего кода. При сбое транспорта
    сохраняем сообщение для повторной доставки и логируем только заголовок.
    """
    if urgency not in _URGENCY:
        raise ValueError("Неизвестная срочность уведомления")
    message = (summary, body, _URGENCY[urgency], tuple(actions))
    if _deliver(*message):
        if message in _pending:
            _pending.remove(message)
    elif message not in _pending:
        _pending.append(message)


def notify_hotkey_not_grabbed(combo: str) -> None:
    """Сообщить, что сочетание клавиш занято другой программой."""
    notify(
        f"Горячая клавиша {combo} занята другой программой",
        "Как только она освободится, диктовка заработает сама.",
        actions=[(ACTION_CHOOSE_HOTKEY, "Выбрать другую")],
    )


def notify_hotkey_regrabbed(combo: str) -> None:
    """Сообщить об автоматическом восстановлении горячей клавиши."""
    notify(f"Горячая клавиша снова работает: {combo}", urgency="normal")


def notify_onboarding_ready(combo: str) -> None:
    """Сообщить о завершении первого запуска и напомнить комбинацию."""
    notify("Astra Voice готов", f"Зажмите {combo} и говорите.")


def notify_microphone_changed(name: str) -> None:
    """Сообщить о смене микрофона, используя его человекочитаемое описание."""
    notify(
        "Микрофон сменился",
        f"Сейчас используется: {name}. Выбрать другой можно в настройках.",
        actions=[(ACTION_CHOOSE_MICROPHONE, "Выбрать микрофон")],
    )


def notify_microphone_lost() -> None:
    """Сообщить о пропаже явно выбранного микрофона."""
    notify(
        "Микрофон отключился",
        "Проверьте подключение или выберите микрофон в настройках.",
        urgency="critical",
    )


def notify_microphone_selected(name: str) -> None:
    """Объявить системное описание нового микрофона перед диктовкой."""
    notify(f"Микрофон: {name}")


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


def notify_model_installed() -> None:
    """Сообщить о готовности модели после фоновой установки."""
    notify("Модель установлена", "Можно диктовать.", urgency="normal")
