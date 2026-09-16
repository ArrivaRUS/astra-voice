"""Уведомления: контракт D-Bus и запрет передачи диктовки по AST всего пакета."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event
from time import perf_counter
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QMetaType, QObject, QVariant
from PyQt5.QtDBus import QDBus, QDBusMessage

from astra_voice.ui import notify as notifications

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
NOTIFY_PATH = ROOT / "src/astra_voice/ui/notify.py"


@pytest.fixture(autouse=True)
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[Mock]:
    """Подменить шину до любого вызова, сохранив настоящие типы аргументов Qt."""
    notifications.reset_state()
    bus = Mock()
    bus.isConnected.return_value = True
    bus.connect.return_value = True
    reply = Mock()
    reply.type.return_value = QDBusMessage.ReplyMessage
    reply.arguments.return_value = [41]
    bus.call.return_value = reply
    connection = Mock()
    connection.sessionBus.return_value = bus
    forbidden_interface = Mock(side_effect=AssertionError("Синхронный Introspect запрещён"))
    create_method_call = QDBusMessage.createMethodCall

    def create_message(service: str, path: str, interface: str, method: str) -> QDBusMessage:
        message = create_method_call(service, path, interface, method)
        # Сохраняем типы QVariant до преобразования arguments() обратно в Python.
        message.setArguments = Mock(wraps=message.setArguments)
        return message

    monkeypatch.setattr(notifications, "QDBusConnection", connection)
    monkeypatch.setattr(notifications, "QDBusInterface", forbidden_interface)
    monkeypatch.setattr(QDBusMessage, "createMethodCall", create_message)
    yield Mock(bus=bus, reply=reply, factory=forbidden_interface)
    notifications.reset_state()


def _arguments(message: QDBusMessage) -> list[Any]:
    return cast(list[Any], message.setArguments.call_args.args[0])


@pytest.mark.parametrize(("urgency", "expected"), [("low", 0), ("normal", 1), ("critical", 2)])
def test_notify_arguments(transport: Mock, urgency: str, expected: int) -> None:
    notifications.notify("Заголовок", "Сообщение", urgency=urgency)
    transport.factory.assert_not_called()
    transport.bus.call.assert_called_once()
    message, mode, timeout_ms = transport.bus.call.call_args.args
    assert message.service() == "org.freedesktop.Notifications"
    assert message.path() == "/org/freedesktop/Notifications"
    assert message.interface() == "org.freedesktop.Notifications"
    assert message.member() == "Notify"
    assert mode == QDBus.Block
    assert 0 < timeout_ms <= 450
    args = _arguments(message)
    assert len(args) == 8
    assert args[0] == "Astra Voice"
    assert args[1].userType() == QMetaType.UInt
    assert args[1].value() == 0
    assert args[2:5] == ["astravoice", "Заголовок", "Сообщение"]
    assert args[5].type() == QVariant.StringList
    assert args[5].value() == []
    assert set(args[6]) == {"urgency"}
    assert args[6]["urgency"].userType() == QMetaType.UChar
    assert args[6]["urgency"].value() == bytes([expected])
    assert args[7] == -1


def test_notify_defaults(transport: Mock) -> None:
    notifications.notify("Заголовок")
    args = _arguments(transport.bus.call.call_args.args[0])
    assert args[4] == ""
    assert args[6]["urgency"].value() == b"\x01"
    assert notifications.last_delivery_ok() is True
    assert notifications.pending_count() == 0


def _invoke_action(transport: Mock, notification_id: int, key: str) -> None:
    transport.bus.connect.call_args.args[-1](notification_id, key)


def test_notify_actions_and_single_uint_signal_subscription(transport: Mock) -> None:
    actions = [("first", "Первое"), ("second", "Второе")]
    for _ in range(3):
        notifications.notify("Заголовок", actions=actions)
        value = _arguments(transport.bus.call.call_args.args[0])[5]
        assert value.type() == QVariant.StringList
        assert value.value() == ["first", "Первое", "second", "Второе"]
    transport.bus.connect.assert_called_once()
    service, path, interface, signal, signature, slot = transport.bus.connect.call_args.args
    assert service == interface == "org.freedesktop.Notifications"
    assert path == "/org/freedesktop/Notifications"
    assert signal == "ActionInvoked"
    assert signature == "us"
    assert isinstance(slot.__self__, QObject)
    receiver = notifications._action_receiver
    assert receiver is not None
    assert slot.__self__ is receiver
    assert receiver.metaObject().indexOfSlot(b"on_action_invoked(uint,QString)") >= 0


@pytest.mark.parametrize("notification_id", [41, 0xF0000001])
def test_action_invoked_once_and_redelivery_rearms(transport: Mock, notification_id: int) -> None:
    handler = Mock()
    notifications.set_action_handler("details", handler)
    transport.reply.arguments.return_value = [notification_id]
    notifications.notify("Заголовок", actions=[("details", "Подробности")])
    _invoke_action(transport, notification_id, "details")
    _invoke_action(transport, notification_id, "details")
    handler.assert_called_once_with()
    notifications.notify("Заголовок", actions=[("details", "Подробности")])
    _invoke_action(transport, notification_id, "details")
    assert handler.call_count == 2
    transport.bus.connect.assert_called_once()


def test_action_closed_notification_id_reused_by_daemon_is_ignored(transport: Mock) -> None:
    handler = Mock()
    notifications.set_action_handler("details", handler)
    notifications.notify("Заголовок", actions=[("details", "Подробности")])
    # Выбор действия закрывает non-resident уведомление; NotificationClosed не подписан.
    _invoke_action(transport, 41, "details")
    handler.assert_called_once_with()
    assert 41 not in notifications._notification_actions
    handler.reset_mock()

    # Демон выдал тот же ID чужому уведомлению с таким же ключом действия.
    _invoke_action(transport, 41, "details")
    handler.assert_not_called()


def test_notification_actions_evict_oldest_when_replacement_id_is_lost(
    transport: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = Mock()
    notifications.set_action_handler("details", handler)
    limit = notifications._pending.maxlen
    assert limit is not None
    for notification_id in range(1, limit * 3 + 1):
        # Проверяем границу независимо от штатной очистки при замене уведомления.
        monkeypatch.setattr(notifications, "_last_id", 0)
        transport.reply.arguments.return_value = [notification_id]
        notifications.notify("Заголовок", actions=[("details", "Подробности")])
        assert len(notifications._notification_actions) <= limit

    assert list(notifications._notification_actions) == list(range(limit * 2 + 1, limit * 3 + 1))
    _invoke_action(transport, 1, "details")
    handler.assert_not_called()
    _invoke_action(transport, limit * 3, "details")
    handler.assert_called_once_with()


@pytest.mark.parametrize(
    ("notification_id", "key"), [(0, "details"), (42, "details"), (41, "unknown"), (41, "other")]
)
def test_unknown_id_or_key_ignored(
    transport: Mock, caplog: pytest.LogCaptureFixture, notification_id: int, key: str
) -> None:
    handler = Mock()
    notifications.set_action_handler("details", handler)
    notifications.set_action_handler("other", handler)  # Не объявлен в этом уведомлении.
    notifications.notify("Заголовок", actions=[("details", "Подробности")])
    with caplog.at_level("DEBUG", logger=notifications.__name__):
        _invoke_action(transport, notification_id, key)
    handler.assert_not_called()
    assert any(record.levelname == "DEBUG" for record in caplog.records)
    _invoke_action(transport, 41, "details")
    handler.assert_called_once_with()


@pytest.mark.parametrize("reset", [False, True])
def test_action_handlers_can_be_removed(transport: Mock, reset: bool) -> None:
    handler = Mock()
    notifications.set_action_handler("details", handler)
    notifications.notify("Заголовок", actions=[("details", "Подробности")])
    if reset:
        notifications.reset_state()
        transport.bus.disconnect.assert_called_once_with(*transport.bus.connect.call_args.args)
    else:
        notifications.set_action_handler("details", None)
    _invoke_action(transport, 41, "details")
    # Повторная доставка также не должна восстанавливать снятый обработчик.
    notifications.notify("Заголовок", actions=[("details", "Подробности")])
    _invoke_action(transport, 41, "details")
    handler.assert_not_called()


def test_action_handler_can_be_replaced(transport: Mock) -> None:
    first, second = Mock(), Mock()
    notifications.set_action_handler("details", first)
    notifications.notify("Заголовок", actions=[("details", "Подробности")])
    notifications.set_action_handler("details", second)
    _invoke_action(transport, 41, "details")
    first.assert_not_called()
    second.assert_called_once_with()


def test_action_handler_exception_does_not_escape_or_leak(
    transport: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    handler = Mock(side_effect=RuntimeError("Секретная диктовка"))
    notifications.set_action_handler("details", handler)
    notifications.notify("Заголовок", actions=[("details", "Подробности")])
    _invoke_action(transport, 41, "details")
    _invoke_action(transport, 41, "details")
    handler.assert_called_once_with()
    assert "Секретная диктовка" not in caplog.text
    assert any(record.levelname == "WARNING" for record in caplog.records)


@pytest.mark.parametrize("new_id", [41, 72])
@pytest.mark.parametrize("with_actions", [False, True])
def test_replacement_discards_previous_actions(
    transport: Mock, new_id: int, with_actions: bool
) -> None:
    old, new = Mock(), Mock()
    notifications.set_action_handler("old", old)
    notifications.set_action_handler("new", new)
    notifications.notify("Первое", actions=[("old", "Первое")])
    transport.reply.arguments.return_value = [new_id]
    notifications.notify("Второе", actions=[("new", "Второе")] if with_actions else [])
    _invoke_action(transport, 41, "old")
    _invoke_action(transport, new_id, "old")
    old.assert_not_called()
    if new_id != 41:
        _invoke_action(transport, 41, "new")
        new.assert_not_called()
    _invoke_action(transport, new_id, "new")
    assert new.call_count == int(with_actions)


@pytest.mark.parametrize("repeat_wrapper", [False, True])
def test_pending_actions_survive_failed_retry_and_recovery(
    transport: Mock, repeat_wrapper: bool
) -> None:
    handler = Mock()
    notifications.set_action_handler(notifications.ACTION_SHOW_DETAILS, handler)
    transport.bus.isConnected.return_value = False
    actions = [(notifications.ACTION_SHOW_DETAILS, "Подробности")]
    notifications.notify(
        "Распознавание на этом компьютере не работает",
        "Обратитесь к администратору.",
        urgency="critical",
        actions=actions,
    )
    actions.clear()  # Очередь владеет своей копией списка.
    notifications.notify_selfcheck_failed()
    assert notifications.pending_count() == 1
    transport.bus.connect.assert_not_called()
    transport.bus.isConnected.return_value = True
    transport.reply.type.return_value = QDBusMessage.ErrorMessage
    assert notifications.flush_pending() == 0
    _invoke_action(transport, 41, notifications.ACTION_SHOW_DETAILS)
    handler.assert_not_called()
    transport.reply.type.return_value = QDBusMessage.ReplyMessage
    if repeat_wrapper:
        notifications.notify_selfcheck_failed()
    else:
        assert notifications.flush_pending() == 1
    assert notifications.pending_count() == 0
    assert transport.bus.call.call_count == 2
    for call in transport.bus.call.call_args_list:
        assert _arguments(call.args[0])[5].value() == ["show-details", "Подробности"]
    _invoke_action(transport, 41, notifications.ACTION_SHOW_DETAILS)
    handler.assert_called_once_with()


def test_pending_deduplication_includes_actions_and_drop_removes_them(transport: Mock) -> None:
    transport.bus.isConnected.return_value = False
    notifications.notify("Первое", actions=[("first", "Первое")])
    notifications.notify("Первое", actions=[("second", "Второе")])
    notifications.notify("Первое", actions=[("first", "Первое")])
    notifications.notify("Следующее", actions=[("next", "Далее")])
    assert notifications.pending_count() == 3
    assert notifications.drop_pending("Первое") == 2
    transport.bus.isConnected.return_value = True
    assert notifications.flush_pending() == 1
    assert _arguments(transport.bus.call.call_args.args[0])[5].value() == ["next", "Далее"]


@pytest.mark.parametrize("failure", [False, RuntimeError("Сбой подписки")])
def test_subscription_failure_keeps_delivery_and_retries(transport: Mock, failure: object) -> None:
    if isinstance(failure, Exception):
        transport.bus.connect.side_effect = failure
    else:
        transport.bus.connect.return_value = failure
    notifications.notify_selfcheck_failed()
    assert notifications.last_delivery_ok() is True
    assert notifications.pending_count() == 0
    transport.bus.connect.side_effect = None
    transport.bus.connect.return_value = True
    notifications.notify_selfcheck_failed()
    assert transport.bus.connect.call_count == 2


@pytest.mark.parametrize("backlog", [0, 8])
def test_hung_owner_keeps_whole_notify_within_500_ms(transport: Mock, backlog: int) -> None:
    transport.bus.isConnected.return_value = False
    for index in range(backlog):
        notifications.notify(f"Отложенное {index}")
    transport.bus.isConnected.return_value = True
    owner = Event()  # Владелец существует, но никогда не отвечает; реальной шины нет.

    def call_hung_owner(message: QDBusMessage, mode: object, timeout_ms: int) -> Mock:
        # Даже регрессия таймаута не должна задержать unit-набор на 25 секунд.
        owner.wait(min(timeout_ms, 600) / 1000)
        return cast(Mock, transport.reply)

    transport.reply.type.return_value = QDBusMessage.ErrorMessage
    transport.bus.call.side_effect = call_hung_owner
    started = perf_counter()
    notifications.notify("Зависший владелец")
    elapsed_ms = (perf_counter() - started) * 1000
    assert elapsed_ms <= 500, f"notify() занял {elapsed_ms:.1f} мс"
    transport.factory.assert_not_called()
    transport.bus.call.assert_called_once()
    message, mode, timeout_ms = transport.bus.call.call_args.args
    assert message.member() == "Notify"
    assert mode == QDBus.Block
    assert 0 < timeout_ms <= 500
    assert notifications.last_delivery_ok() is False
    assert notifications.pending_count() == min(backlog + 1, 8)


@pytest.mark.parametrize("preparation_seconds", [0.350, 0.500])
def test_preparation_uses_same_transport_budget(
    transport: Mock, monkeypatch: pytest.MonkeyPatch, preparation_seconds: float
) -> None:
    ticks = iter([0.0, preparation_seconds])
    monkeypatch.setattr(notifications, "monotonic", lambda: next(ticks))
    notifications.notify("Заголовок")
    if preparation_seconds < 0.450:
        assert 0 < transport.bus.call.call_args.args[2] <= 100
    else:
        transport.bus.call.assert_not_called()
        assert notifications.pending_count() == 1


def test_unknown_urgency(transport: Mock) -> None:
    with pytest.raises(ValueError):
        notifications.notify("Заголовок", urgency="urgent")
    transport.factory.assert_not_called()


def test_replaces_last_successful_notification(transport: Mock) -> None:
    notifications.notify("Первое")
    transport.reply.arguments.return_value = [72]
    notifications.notify("Второе")
    notifications.notify("Третье")
    assert [_arguments(call.args[0])[1].value() for call in transport.bus.call.call_args_list] == [
        0,
        41,
        72,
    ]


@pytest.mark.parametrize("delivered", [False, True])
def test_reset_state(transport: Mock, delivered: bool) -> None:
    assert notifications.last_delivery_ok() is False
    transport.bus.isConnected.return_value = False
    notifications.notify("Отложенное")
    assert notifications.pending_count() == 1
    transport.bus.isConnected.return_value = delivered
    notifications.notify("Первое")
    assert notifications.last_delivery_ok() is delivered
    notifications.reset_state()
    assert notifications.pending_count() == 0
    assert notifications.last_delivery_ok() is False
    transport.bus.isConnected.return_value = True
    notifications.notify("Второе")
    assert _arguments(transport.bus.call.call_args.args[0])[1].value() == 0


@pytest.mark.parametrize("failure", ["bus", "reply", "exception"])
def test_transport_failure_resets_id(
    transport: Mock, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    handler = Mock()
    notifications.set_action_handler("details", handler)
    notifications.notify("Первое", actions=[("details", "Подробности")])
    assert 41 in notifications._notification_actions
    if failure == "bus":
        transport.bus.isConnected.return_value = False
    elif failure == "reply":
        transport.reply.type.return_value = QDBusMessage.ErrorMessage
    else:
        transport.bus.call.side_effect = RuntimeError("Секретная диктовка")
    transport.bus.call.reset_mock()
    with caplog.at_level("DEBUG", logger=notifications.__name__):
        notifications.notify("Запись остановлена", "Секретная диктовка")
    assert notifications.last_delivery_ok() is False
    assert 41 not in notifications._notification_actions
    _invoke_action(transport, 41, "details")
    handler.assert_not_called()
    assert notifications.pending_count() == 1
    assert any(
        record.levelname == "WARNING" and "Запись остановлена" in record.getMessage()
        for record in caplog.records
    )
    assert "Секретная диктовка" not in caplog.text
    if failure == "bus":
        transport.bus.call.assert_not_called()
    transport.bus.isConnected.return_value = True
    transport.reply.type.return_value = QDBusMessage.ReplyMessage
    transport.bus.call.side_effect = None
    notifications.notify("Восстановлено")
    assert _arguments(transport.bus.call.call_args.args[0])[1].value() == 0


@pytest.mark.parametrize("arguments", [[], [0], [-1], [2**32], ["41"], [True], [41, 42]])
def test_invalid_reply_resets_id(transport: Mock, arguments: list[object]) -> None:
    notifications.notify("Первое")
    transport.reply.arguments.return_value = arguments
    notifications.notify("Второе")
    assert notifications.last_delivery_ok() is False
    assert notifications.pending_count() == 1
    transport.reply.arguments.return_value = [42]
    notifications.notify("Третье")
    assert _arguments(transport.bus.call.call_args.args[0])[1].value() == 0


def test_flush_pending_after_recovery(transport: Mock) -> None:
    transport.bus.isConnected.return_value = False
    notifications.notify_indicators_lost()
    notifications.notify_hotkey_not_grabbed("Ctrl+Space")
    assert notifications.pending_count() == 2

    transport.bus.isConnected.return_value = True
    assert notifications.flush_pending() == 2
    assert notifications.pending_count() == 0
    assert notifications.last_delivery_ok() is True
    calls = transport.bus.call.call_args_list
    assert [tuple(_arguments(call.args[0])[3:5]) for call in calls] == [
        ("Запись остановлена", "Пропали все указатели записи, поэтому запись остановлена."),
        (
            "Горячая клавиша Ctrl+Space занята другой программой",
            "Как только она освободится, диктовка заработает сама.",
        ),
    ]
    assert [_arguments(call.args[0])[6]["urgency"].value() for call in calls] == [b"\x02", b"\x01"]
    assert [_arguments(call.args[0])[1].value() for call in calls] == [0, 41]
    assert notifications.flush_pending() == 0
    assert notifications.last_delivery_ok() is True
    assert transport.bus.call.call_count == 2


def test_failed_retries_preserve_unique_pending(transport: Mock) -> None:
    transport.bus.isConnected.return_value = False
    for _ in range(12):
        notifications.notify_indicators_lost()
    assert notifications.pending_count() == 1
    for _ in range(2):
        assert notifications.flush_pending() == 0
        assert notifications.pending_count() == 1
        assert notifications.last_delivery_ok() is False


def test_pending_queue_limit(transport: Mock) -> None:
    transport.bus.isConnected.return_value = False
    for index in range(12):
        notifications.notify(f"Тестовый заголовок {index}")
        assert notifications.pending_count() == min(index + 1, 8)
    transport.bus.isConnected.return_value = True
    assert notifications.flush_pending() == 8
    assert notifications.pending_count() == 0
    assert [_arguments(call.args[0])[3] for call in transport.bus.call.call_args_list] == [
        f"Тестовый заголовок {index}" for index in range(4, 12)
    ]


def test_drop_pending_removes_all_matching_summaries_and_keeps_order(transport: Mock) -> None:
    transport.bus.isConnected.return_value = False
    notifications.notify("Первое")
    notifications.notify_tray_unavailable()
    notifications.notify("Второе")
    notifications.notify("Значок не появился на панели", "Другое тело", urgency="critical")
    notifications.notify_tray_unavailable()  # Дубликат не создаёт третью запись.
    notifications.notify("Значок не появился на панели снова")
    assert notifications.pending_count() == 5
    assert notifications.drop_pending("Значок не появился на панели") == 2
    assert notifications.drop_pending("Значок не появился на панели") == 0
    assert notifications.pending_count() == 3
    transport.bus.call.assert_not_called()
    assert notifications.last_delivery_ok() is False
    transport.bus.isConnected.return_value = True
    assert notifications.flush_pending() == 3
    assert [_arguments(call.args[0])[3] for call in transport.bus.call.call_args_list] == [
        "Первое",
        "Второе",
        "Значок не появился на панели снова",
    ]
    assert notifications.drop_pending("Первое") == 0
    assert notifications.last_delivery_ok() is True


def test_drop_pending_keeps_delivery_id_and_queue_limit(transport: Mock) -> None:
    notifications.notify("Доставлено")
    assert notifications.drop_pending("Доставлено") == 0
    notifications.notify("Повтор")
    assert _arguments(transport.bus.call.call_args.args[0])[1].value() == 41
    transport.bus.isConnected.return_value = False
    notifications.notify_tray_unavailable()
    assert notifications.drop_pending("Значок не появился на панели") == 1
    for index in range(12):
        notifications.notify(f"Отложенное {index}")
    assert notifications.pending_count() == 8


def test_successful_repeat_removes_pending(transport: Mock) -> None:
    transport.bus.isConnected.return_value = False
    notifications.notify_indicators_lost()
    transport.bus.isConnected.return_value = True
    notifications.notify_indicators_lost()
    assert notifications.last_delivery_ok() is True
    assert notifications.pending_count() == 0
    assert notifications.flush_pending() == 0
    transport.bus.call.assert_called_once()


def test_flush_pending_partial_delivery(transport: Mock) -> None:
    transport.bus.isConnected.return_value = False
    notifications.notify_indicators_lost()
    notifications.notify_tray_unavailable()
    notifications.notify_hotkey_not_grabbed("Ctrl+Space")
    transport.bus.isConnected.return_value = True
    transport.bus.call.side_effect = [
        RuntimeError("Сбой службы"),
        transport.reply,
        RuntimeError("Сбой службы"),
    ]
    assert notifications.flush_pending() == 1
    assert notifications.pending_count() == 2
    assert notifications.last_delivery_ok() is False
    transport.bus.call.side_effect = None
    transport.bus.call.reset_mock()
    assert notifications.flush_pending() == 2
    assert notifications.pending_count() == 0
    assert notifications.last_delivery_ok() is True
    assert [_arguments(call.args[0])[3] for call in transport.bus.call.call_args_list] == [
        "Запись остановлена",
        "Горячая клавиша Ctrl+Space занята другой программой",
    ]


@pytest.mark.parametrize(
    ("wrapper", "summary", "body", "priority"),
    [
        (
            notifications.notify_tray_unavailable,
            "Значок не появился на панели",
            "Программа работает, но значка на панели нет. Показ можно проверить в настройках.",
            b"\x01",
        ),
        (
            notifications.notify_tray_depends_on_panel,
            "Виден только значок на панели",
            "Если панель перезапустится, показывать запись будет нечем. "
            "Включите указатель записи в настройках.",
            b"\x01",
        ),
        (
            notifications.notify_microphone_lost,
            "Микрофон отключился",
            "Проверьте подключение или выберите микрофон в настройках.",
            b"\x02",
        ),
        (
            notifications.notify_selfcheck_failed,
            "Распознавание на этом компьютере не работает",
            "Обратитесь к администратору.",
            b"\x02",
        ),
        (
            notifications.notify_indicators_lost,
            "Запись остановлена",
            "Пропали все указатели записи, поэтому запись остановлена.",
            b"\x02",
        ),
    ],
)
def test_fixed_messages(
    transport: Mock, wrapper: Callable[[], None], summary: str, body: str, priority: bytes
) -> None:
    wrapper()
    args = _arguments(transport.bus.call.call_args.args[0])
    assert args[3:5] == [summary, body]
    assert args[5].value() == (
        ["show-details", "Подробности"] if wrapper is notifications.notify_selfcheck_failed else []
    )
    assert args[6]["urgency"].value() == priority


@pytest.mark.parametrize("combo", ["Ctrl+Space", "Ctrl+Shift+Space", "Win+Space"])
@pytest.mark.parametrize("regrabbed", [False, True])
def test_hotkey_messages_name_combo_and_actions(
    transport: Mock, combo: str, regrabbed: bool
) -> None:
    if regrabbed:
        notifications.notify_hotkey_regrabbed(combo)
        expected = [f"Горячая клавиша снова работает: {combo}", ""]
    else:
        notifications.notify_hotkey_not_grabbed(combo)
        expected = [
            f"Горячая клавиша {combo} занята другой программой",
            "Как только она освободится, диктовка заработает сама.",
        ]
    transport.bus.call.assert_called_once()
    args = _arguments(transport.bus.call.call_args.args[0])
    assert args[3:5] == expected
    assert args[5].value() == ([] if regrabbed else ["choose-hotkey", "Выбрать другую"])
    assert args[6]["urgency"].value() == b"\x01"


@pytest.mark.parametrize(
    "wrapper", [notifications.notify_hotkey_not_grabbed, notifications.notify_hotkey_regrabbed]
)
def test_hotkey_messages_require_combo(wrapper: Callable[..., None]) -> None:
    with pytest.raises(TypeError):
        wrapper()


@pytest.mark.parametrize("selected", [False, True])
def test_microphone_messages_use_system_description_and_actions(
    transport: Mock, selected: bool
) -> None:
    name = "Встроенный микрофон"
    if selected:
        notifications.notify_microphone_selected(name)
        expected = [f"Микрофон: {name}", ""]
    else:
        notifications.notify_microphone_changed(name)
        expected = [
            "Микрофон сменился",
            f"Сейчас используется: {name}. Выбрать другой можно в настройках.",
        ]
    transport.bus.call.assert_called_once()
    args = _arguments(transport.bus.call.call_args.args[0])
    assert args[3:5] == expected
    assert args[5].value() == ([] if selected else ["choose-microphone", "Выбрать микрофон"])
    assert args[6]["urgency"].value() == b"\x01"


def _qualified_name(node: ast.AST, aliases: dict[str, str]) -> str:
    """Развернуть псевдонимы импортов и простых присваиваний функций."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_qualified_name(node.value, aliases)}.{node.attr}"
    return ""


def _is_notification(name: str) -> bool:
    leaf = name.rsplit(".", 1)[-1]
    return leaf == "notify" or leaf.startswith("notify_")


def _is_static_actions(node: ast.AST, constants: set[str]) -> bool:
    """Разрешены только явные пары постоянных ключей и подписей кнопок."""
    return isinstance(node, (ast.List, ast.Tuple)) and all(
        isinstance(pair, ast.Tuple)
        and len(pair.elts) == 2
        and all(
            (isinstance(item, ast.Constant) and isinstance(item.value, str))
            or (isinstance(item, ast.Name) and item.id in constants)
            for item in pair.elts
        )
        for pair in node.elts
    )


def _notification_violations(source: str, *, implementation: bool = False) -> list[str]:
    """Проверить все вызовы, включая вложенные функции и псевдонимы импортов.

    Прямой notify разрешён только в модуле уведомлений. Для двух обёрток
    горячей клавиши разрешены settings.hotkey снаружи и шаблон с combo внутри.
    Описание микрофона разрешено внутри двух фиксированных шаблонов; runtime
    передаёт обёртки как колбэки. Остальные тексты — строковые константы.
    """
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    aliases: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module or ''}.{item.name}"
        elif isinstance(node, ast.Import):
            for item in node.names:
                if item.asname:
                    aliases[item.asname] = item.name
    # Несколько проходов учитывают цепочки вроде send = show = notify.
    for _ in range(len(nodes)):
        added = False
        for node in nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            name = _qualified_name(node.value, aliases)
            if not _is_notification(name):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases[target.id] = name
                    added = True
        if not added:
            break

    writes = Counter(
        node.id
        for node in nodes
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    )
    writes.update(node.arg for node in nodes if isinstance(node, ast.arg))
    constants: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper() and writes[target.id] == 1:
                constants.add(target.id)

    problems: list[str] = []
    hotkey_templates = {
        "notify_hotkey_not_grabbed": 'f"Горячая клавиша {combo} занята другой программой"',
        "notify_hotkey_regrabbed": 'f"Горячая клавиша снова работает: {combo}"',
    }
    templates = {
        **hotkey_templates,
        "notify_onboarding_ready": 'f"Зажмите {combo} и говорите."',
        "notify_microphone_changed": (
            'f"Сейчас используется: {name}. Выбрать другой можно в настройках."'
        ),
        "notify_microphone_selected": 'f"Микрофон: {name}"',
    }
    allowed_templates = {
        statement.value: ast.dump(ast.parse(templates[function.name], mode="eval").body)
        for function in tree.body
        if isinstance(function, ast.FunctionDef) and function.name in templates
        for statement in function.body
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
    }
    # У онбординга два типизированных перехода: контроллер → host → обёртка.
    # Разрешены только точные вызовы в соответствующих методах этих классов.
    onboarding_calls = {
        ("OnboardingController", "finish"): "self._host.notify_ready(self.hotkey)",
        ("_RuntimeOnboardingHost", "notify_ready"): "notify.notify_onboarding_ready(combo)",
    }
    allowed_onboarding = {
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for method in cls.body
        if isinstance(method, ast.FunctionDef)
        if (cls.name, method.name) in onboarding_calls
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        if ast.dump(node)
        == ast.dump(ast.parse(onboarding_calls[cls.name, method.name], mode="eval").body)
    }
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        name = _qualified_name(node.func, aliases)
        if not _is_notification(name):
            continue
        if not implementation and node in allowed_onboarding:
            continue
        leaf = name.rsplit(".", 1)[-1]
        if (
            not implementation
            and leaf in hotkey_templates
            and len(node.args) == 1
            and not node.keywords
            and ast.dump(node.args[0])
            == ast.dump(ast.parse("self.settings.hotkey", mode="eval").body)
        ):
            continue
        if not implementation and (leaf == "notify" or node.args or node.keywords):
            problems.append(f"{node.lineno}: снаружи допустима только обёртка без аргументов")
        arguments = [
            *node.args,
            *(
                keyword.value
                for keyword in node.keywords
                if not (
                    implementation
                    and leaf == "notify"
                    and keyword.arg == "actions"
                    and _is_static_actions(keyword.value, constants)
                )
            ),
        ]
        for argument in arguments:
            if (
                implementation
                and leaf == "notify"
                and node in allowed_templates
                and ast.dump(argument) == allowed_templates[node]
            ):
                continue
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                continue
            if isinstance(argument, ast.Name) and argument.id in constants:
                continue
            problems.append(f"{node.lineno}: текст уведомления не является строковой константой")
        if any(keyword.arg is None for keyword in node.keywords):
            problems.append(f"{node.lineno}: распаковка аргументов уведомления запрещена")
    return problems


def test_project_notifications_contain_no_dictation() -> None:
    """Гейт распространяется на все будущие модули, в том числе зоны A/C."""
    paths = sorted((ROOT / "src/astra_voice").rglob("*.py"))
    assert NOTIFY_PATH in paths
    problems = [
        f"{path.relative_to(ROOT)}:{problem}"
        for path in paths
        for problem in _notification_violations(
            path.read_text(encoding="utf-8"), implementation=path == NOTIFY_PATH
        )
    ]
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "source",
    [
        "notify(text)",
        'notify("Запись", body=text)',
        'notify(f"Запись: {text}")',
        'notify("Запись: {}".format(text))',
        'notify("Запись: " + text)',
        'notify("Запись: %s" % text)',
        "notify(*args)",
        "notify(**kwargs)",
        "TEXT = text\nnotify(TEXT)",
        'TEXT = "Запись"\nTEXT = text\nnotify(TEXT)',
        'TEXT = "Запись"\ndef show(TEXT):\n    notify(TEXT)',
        'def show():\n    TEXT = "Запись"\n    notify(TEXT)',
        "from astra_voice.ui.notify import notify as show\nshow(text)",
        "from .notify import notify as show\nshow(text)",
        "import astra_voice.ui.notify as n\nn.notify(text)",
        "from astra_voice.ui import notify as n\nn.notify(text)",
        "show = notify\nshow(text)",
        "show = notify\nother = show\nother(text)",
        "n.notify_indicators_lost(text)",
        "n.notify_tray_depends_on_panel(text)",
        "n.notify_hotkey_not_grabbed(text)",
        "n.notify_hotkey_regrabbed(text)",
        "n.notify_microphone_changed(text)",
        "n.notify_microphone_selected(text)",
        'def notify_microphone_changed(name):\n    notify(f"Запись: {text}")',
        'def notify_microphone_selected(name):\n    notify(f"Микрофон: {text}")',
        'def notify_hotkey_regrabbed(combo):\n    notify(f"Запись: {text}")',
        'notify("Запись", actions=text)',
        'notify("Запись", actions=[("details", text)])',
        'notify("Запись", actions=[(text, "Подробности")])',
        'notify("Запись", actions=[("details", f"Подробности: {text}")])',
        'notify("Запись", actions=[("details", "Подробности", text)])',
        'notify("Запись", actions=[*text])',
        'LABEL = text\nnotify("Запись", actions=[("details", LABEL)])',
        'LABEL = "Подробности"\nLABEL = text\nnotify("Запись", actions=[("details", LABEL)])',
    ],
)
def test_ast_rejects_dynamic_text(source: str) -> None:
    assert _notification_violations(source, implementation=True)


@pytest.mark.parametrize(
    "source",
    [
        'notify("Запись", actions=[("details", "Подробности")])',
        'ACTION = "details"\nnotify("Запись", actions=[(ACTION, "Подробности")])',
    ],
)
def test_ast_allows_static_actions_only_inside_module(source: str) -> None:
    assert not _notification_violations(source, implementation=True)
    assert _notification_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        'notify("Запись")',
        'from astra_voice.ui.notify import notify as show\nshow("Запись")',
        'notify_indicators_lost("Запись")',
        'notify_tray_depends_on_panel("Запись")',
        "from astra_voice.ui.notify import notify_tray_unavailable as show\nshow(body=text)",
        "notify_hotkey_not_grabbed(self.last_text)",
        "notify_hotkey_regrabbed(self.settings.hotkey, body=text)",
        "notify_hotkey_regrabbed(*args)",
    ],
)
def test_ast_requires_wrappers_outside_module(source: str) -> None:
    assert _notification_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        'notify("Запись", "Остановлена", urgency="critical")',
        'SUMMARY = "Запись"\nnotify(SUMMARY)',
        'SUMMARY: Final[str] = "Запись"\nnotify(SUMMARY)',
    ],
)
def test_ast_accepts_fixed_text(source: str) -> None:
    assert not _notification_violations(source, implementation=True)


@pytest.mark.parametrize("wrapper", ["notify_indicators_lost", "notify_tray_depends_on_panel"])
def test_ast_accepts_wrapper_alias(wrapper: str) -> None:
    source = (
        f"from astra_voice.ui.notify import {wrapper} as stopped\n"
        "def on_failure():\n    stopped()\n"
    )
    assert not _notification_violations(source)


@pytest.mark.parametrize("combo", ["Ctrl+Space", "Ctrl+Alt+D"])
def test_onboarding_ready_message(transport: Mock, combo: str) -> None:
    notifications.notify_onboarding_ready(combo)
    transport.bus.call.assert_called_once()
    args = _arguments(transport.bus.call.call_args.args[0])
    assert args[3:5] == ["Astra Voice готов", f"Зажмите {combo} и говорите."]
    assert args[5].value() == []
    assert args[6]["urgency"].value() == b"\x01"


@pytest.mark.parametrize(
    "source",
    [
        "notify_onboarding_ready(text)",
        "self._host.notify_ready(self.last_text)",
        "class _RuntimeOnboardingHost:\n    def notify_ready(self, combo):\n"
        "        notify.notify_onboarding_ready(text)",
        "class OnboardingController:\n    def finish(self):\n"
        "        self._host.notify_ready(self.last_text)",
    ],
)
def test_ast_rejects_onboarding_dictation(source: str) -> None:
    assert _notification_violations(source)
