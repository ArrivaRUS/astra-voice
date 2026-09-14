"""Уведомления: контракт D-Bus и запрет передачи диктовки по AST всего пакета."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from threading import Event
from time import perf_counter
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QMetaType, QVariant
from PyQt5.QtDBus import QDBus, QDBusMessage

from astra_voice.ui import notify as notifications

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
NOTIFY_PATH = ROOT / "src/astra_voice/ui/notify.py"


@pytest.fixture(autouse=True)
def transport(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Подменить шину до любого вызова, сохранив настоящие типы аргументов Qt."""
    notifications.reset_state()
    bus = Mock()
    bus.isConnected.return_value = True
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
    return Mock(bus=bus, reply=reply, factory=forbidden_interface)


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
    notifications.notify("Первое")
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
    notifications.notify_hotkey_not_grabbed()
    assert notifications.pending_count() == 2

    transport.bus.isConnected.return_value = True
    assert notifications.flush_pending() == 2
    assert notifications.pending_count() == 0
    assert notifications.last_delivery_ok() is True
    calls = transport.bus.call.call_args_list
    assert [tuple(_arguments(call.args[0])[3:5]) for call in calls] == [
        ("Запись остановлена", "Пропали все указатели записи, поэтому запись остановлена."),
        (
            "Горячая клавиша не захвачена",
            "Другая программа уже использует это сочетание. Выберите другое в настройках.",
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
    notifications.notify_hotkey_not_grabbed()
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
        "Горячая клавиша не захвачена",
    ]


@pytest.mark.parametrize(
    ("wrapper", "summary", "body", "priority"),
    [
        (
            notifications.notify_hotkey_not_grabbed,
            "Горячая клавиша не захвачена",
            "Другая программа уже использует это сочетание. Выберите другое в настройках.",
            b"\x01",
        ),
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
    assert args[6]["urgency"].value() == priority


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


def _notification_violations(source: str, *, implementation: bool = False) -> list[str]:
    """Проверить все вызовы, включая вложенные функции и псевдонимы импортов.

    Прямой notify разрешён только в модуле уведомлений. Любая обёртка снаружи
    вызывается без аргументов. Внутри допустимы литералы и строковые константы
    уровня модуля, которые нигде не перезаписаны и не затенены параметром.
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
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        name = _qualified_name(node.func, aliases)
        if not _is_notification(name):
            continue
        leaf = name.rsplit(".", 1)[-1]
        if not implementation and (leaf == "notify" or node.args or node.keywords):
            problems.append(f"{node.lineno}: снаружи допустима только обёртка без аргументов")
        arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
        for argument in arguments:
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
    ],
)
def test_ast_rejects_dynamic_text(source: str) -> None:
    assert _notification_violations(source, implementation=True)


@pytest.mark.parametrize(
    "source",
    [
        'notify("Запись")',
        'from astra_voice.ui.notify import notify as show\nshow("Запись")',
        'notify_indicators_lost("Запись")',
        'notify_tray_depends_on_panel("Запись")',
        "from astra_voice.ui.notify import notify_tray_unavailable as show\nshow(body=text)",
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
