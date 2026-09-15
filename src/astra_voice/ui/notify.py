"""Штатные уведомления без кнопок и без распознанного текста.

Остальной код вызывает готовые обёртки; переменные подписи — сочетание клавиш
и системное описание микрофона. Запрет текста диктовки проверяет AST-тест пакета.
"""

from __future__ import annotations

import logging
from collections import deque
from time import monotonic

from PyQt5.QtCore import QMetaType, QVariant
from PyQt5.QtDBus import QDBus, QDBusConnection, QDBusMessage

# Совместимость с подменой старого транспорта в тестах соседней зоны.
# Интерфейс не создаём: его конструктор синхронно запрашивает Introspect.
from PyQt5.QtDBus import QDBusInterface as QDBusInterface

__all__ = [
    "drop_pending",
    "flush_pending",
    "last_delivery_ok",
    "notify",
    "notify_hotkey_not_grabbed",
    "notify_hotkey_regrabbed",
    "notify_microphone_changed",
    "notify_microphone_lost",
    "notify_microphone_selected",
    "notify_tray_depends_on_panel",
    "notify_tray_unavailable",
    "notify_indicators_lost",
    "notify_selfcheck_failed",
    "pending_count",
    "reset_state",
]

_logger = logging.getLogger(__name__)
_URGENCY = {"low": 0, "normal": 1, "critical": 2}
_last_id = 0
_last_delivery_ok = False
# При переполнении сохраняем последние восемь уникальных уведомлений.
_pending: deque[tuple[str, str, int]] = deque(maxlen=8)


def reset_state() -> None:
    """Очистить очередь, идентификатор и признак доставки для изоляции тестов."""
    global _last_id, _last_delivery_ok
    _last_id = 0
    _last_delivery_ok = False
    _pending.clear()


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


def _send(summary: str, body: str, urgency: int, replaces_id: int) -> int:
    """Отправить сообщение через QtDBus; точка подмены транспорта в тестах."""
    # Из общего бюджета notify() 500 мс оставляем 50 мс на обработку результата.
    deadline = monotonic() + 0.450
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        _logger.debug("Шина уведомлений недоступна")
        return 0

    message = QDBusMessage.createMethodCall(
        "org.freedesktop.Notifications",
        "/org/freedesktop/Notifications",
        "org.freedesktop.Notifications",
        "Notify",
    )

    # Python int и пустой list сами по себе дают неверные типы D-Bus.
    replacement = QVariant(replaces_id)
    replacement.convert(QVariant.UInt)
    actions = QVariant([])
    actions.convert(QVariant.StringList)
    priority = QVariant(urgency)
    priority.convert(QMetaType.UChar)
    message.setArguments(
        [
            "Astra Voice",
            replacement,
            "astravoice",
            summary,
            body,
            actions,
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


def _deliver(summary: str, body: str, urgency: int) -> bool:
    """Проверить доставку, записывая при сбое только заголовок-константу."""
    global _last_id, _last_delivery_ok
    try:
        _last_id = _send(summary, body, urgency, _last_id)
    except Exception:
        # Исключение транспорта тоже может содержать переданное сообщение.
        _last_id = 0
    _last_delivery_ok = _last_id != 0
    if not _last_delivery_ok:
        _logger.warning("Не удалось показать уведомление: %s", summary)
    return _last_delivery_ok


def notify(summary: str, body: str = "", *, urgency: str = "normal") -> None:
    """Показать уведомление; внутри проекта доступно только готовым обёрткам.

    Неизвестная срочность — ошибка вызывающего кода. При сбое транспорта
    сохраняем сообщение для повторной доставки и логируем только заголовок.
    """
    if urgency not in _URGENCY:
        raise ValueError("Неизвестная срочность уведомления")
    message = (summary, body, _URGENCY[urgency])
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


def notify_selfcheck_failed() -> None:
    """Сообщить, что модель не прошла пробное распознавание."""
    notify(
        "Распознавание на этом компьютере не работает",
        "Обратитесь к администратору.",
        urgency="critical",
    )
