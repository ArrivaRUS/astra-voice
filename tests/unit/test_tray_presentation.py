"""Показ Qt не равен доказательству SNI; ответы PID приходят асинхронно."""

from __future__ import annotations

import os

import pytest
from test_tray import OWN_ITEM, PLASMA, SERVICE, Harness, PendingCall
from test_tray import forbid_real_x as forbid_real_x
from test_tray import harness as harness
from test_tray import qapp as qapp

from astra_voice.platform.session import SessionKind

pytestmark = pytest.mark.unit
UNIQUE = ":1.987/StatusNotifierItem"


def pid_calls(h: Harness) -> list[PendingCall]:
    return [c for c in h.bus.calls if c.message.member() == "GetConnectionUnixProcessID"]


@pytest.mark.parametrize("kind", [SessionKind.FLY, SessionKind.KDE])
def test_shown_icon_survives_unconfirmed_host_and_late_own_item(
    harness: Harness, kind: SessionKind
) -> None:
    h = harness
    h.detect.return_value = kind
    h.bus.registered_items = []
    h.tray.start()
    assert h.icon.isVisible()
    assert h.tray.visible_for_close
    assert not h.tray.registered
    h.clock.advance(31_000)
    assert h.icon.isVisible()
    assert h.tray.visible_for_close
    assert not h.tray.registered
    h.icon.hide.assert_not_called()
    h.banner.assert_not_called()
    h.flush.assert_not_called()

    h.bus.item_registered(OWN_ITEM)
    assert h.tray.registered
    assert h.tray.visible_for_close
    h.bus.item_unregistered(OWN_ITEM)
    assert not h.tray.registered
    assert not h.tray.visible_for_close
    h.clock.advance(31_000)
    assert not h.icon.isVisible()
    assert not h.tray.visible_for_close
    h.banner.assert_called_once_with()
    h.bus.item_registered(OWN_ITEM)
    h.clock.advance(1000)
    assert h.tray.registered
    assert h.tray.visible_for_close


@pytest.mark.parametrize("kind", [SessionKind.FLY, SessionKind.KDE])
@pytest.mark.parametrize("loss", ["host", "watcher", "watcher-owner", "item", "items"])
def test_proven_sni_loss_retries_without_regaining_close_visibility(
    harness: Harness, kind: SessionKind, loss: str
) -> None:
    h = harness
    h.detect.return_value = kind
    # Даже живой XEmbed owner не отменяет уже доказанный backend этого icon.
    h.probe.owner.return_value = 0xF00
    h.tray.start()
    assert h.tray.registered
    assert h.tray.visible_for_close
    h.bus.registered_items = []
    if loss == "host":
        h.bus.host_changed(False)
    elif loss == "watcher":
        h.bus.host_registered = False
        h.watcher.serviceUnregistered.emit(SERVICE)
    elif loss == "watcher-owner":
        h.bus.auto_reply = False
        h.watcher.serviceOwnerChanged.emit(SERVICE, ":1.42", ":1.43")
    elif loss == "item":
        h.bus.item_unregistered(OWN_ITEM)
    else:
        h.bus.signals["PropertiesChanged"](SERVICE, {"RegisteredStatusNotifierItems": []}, [])
    assert not h.tray.registered
    assert not h.tray.visible_for_close
    if loss == "watcher-owner":
        # Старый item не годится уже в момент смены owner, до нового Get.
        pending = h.bus.calls[-1]
        assert pending.message.arguments()[1] == "IsStatusNotifierHostRegistered"
        h.bus.auto_reply = True
        pending.reply(True)
        assert not h.tray.registered
        assert not h.tray.visible_for_close
    calls = len(h.bus.calls)
    for _ in range(5):
        h.clock.advance(1000)
        assert not h.tray.registered
        assert not h.tray.visible_for_close
    assert len(h.bus.calls) > calls
    h.banner.assert_not_called()
    h.clock.advance(26_000)
    assert not h.tray.registered
    assert not h.tray.visible_for_close
    assert not h.icon.isVisible()
    h.banner.assert_called_once_with()
    h.clock.advance(60_000)
    h.banner.assert_called_once_with()

    h.bus.registered_items = [OWN_ITEM]
    h.bus.host_registered = True
    if loss == "host":
        h.bus.host_changed(True)
    elif loss.startswith("watcher"):
        h.watcher.serviceRegistered.emit(SERVICE)
    else:
        h.bus.item_registered(OWN_ITEM)
    h.clock.advance(1000)
    assert h.tray.registered
    assert h.tray.visible_for_close
    h.clock.advance(31_000)
    h.banner.assert_called_once_with()


@pytest.mark.parametrize("loss", ["gone", "owner"])
def test_proven_sni_plasma_loss_requires_fresh_item_after_panel_returns(
    harness: Harness, loss: str
) -> None:
    h = harness
    h.detect.return_value = SessionKind.KDE
    h.probe.owner.return_value = 0xF00
    h.tray.start()
    assert h.tray.registered
    h.bus.registered_items = []
    if loss == "gone":
        h.watcher.serviceUnregistered.emit(PLASMA)
    else:
        h.bus.auto_reply = False
        h.watcher.serviceOwnerChanged.emit(PLASMA, ":1.42", ":1.43")
    assert not h.tray.registered
    assert not h.tray.visible_for_close
    if loss == "owner":
        pending = h.bus.calls[-1]
        assert pending.message.arguments()[1] == "RegisteredStatusNotifierItems"
        h.bus.auto_reply = True
        pending.reply([])
        assert not h.tray.registered
        assert not h.tray.visible_for_close
    h.clock.advance(31_000)
    assert not h.tray.visible_for_close
    h.banner.assert_called_once_with()
    h.watcher.serviceRegistered.emit(PLASMA)
    h.clock.advance(5000)
    assert not h.tray.registered
    assert not h.tray.visible_for_close
    h.bus.item_registered(OWN_ITEM)
    assert h.tray.registered
    assert h.tray.visible_for_close
    h.banner.assert_called_once_with()


@pytest.mark.parametrize("kind", [SessionKind.FLY, SessionKind.KDE])
def test_same_icon_keeps_proven_sni_backend_across_stop_start(
    harness: Harness, kind: SessionKind
) -> None:
    h = harness
    h.detect.return_value = kind
    h.tray.start()
    assert h.tray.registered
    h.tray.stop()
    h.bus.registered_items = []
    h.tray.start()
    assert not h.tray.registered
    assert not h.tray.visible_for_close
    for _ in range(5):
        h.clock.advance(1000)
        assert not h.tray.registered
        assert not h.tray.visible_for_close
    # Повторный показ того же Qt icon не становится новым unconfirmed backend.
    assert h.icon.show.call_count >= 2
    h.factory.assert_called_once()
    h.clock.advance(26_000)
    assert not h.icon.isVisible()
    assert not h.tray.visible_for_close
    h.banner.assert_called_once_with()
    h.bus.item_registered(OWN_ITEM)
    h.clock.advance(1000)
    assert h.tray.registered
    assert h.tray.visible_for_close


def test_deferred_items_snapshot_reconfirms_sni_without_recreating_qt_item(
    harness: Harness,
) -> None:
    h = harness
    h.tray.start()
    assert h.tray.registered
    assert h.tray.visible_for_close
    show_count, hide_count = h.icon.show.call_count, h.icon.hide.call_count
    foreign = "org.kde.StatusNotifierItem-0-1/StatusNotifierItem"
    h.bus.auto_reply = False
    h.bus.signals["PropertiesChanged"](
        SERVICE, {"RegisteredStatusNotifierItems": [OWN_ITEM, foreign]}, []
    )
    assert not h.tray.registered
    assert not h.tray.visible_for_close
    assert h.icon.show.call_count == show_count
    assert h.icon.hide.call_count == hide_count
    pending = h.bus.calls[-1]
    assert pending.message.arguments()[1] == "RegisteredStatusNotifierItems"
    pending.reply([OWN_ITEM, foreign])
    assert h.tray.registered
    assert h.tray.visible_for_close
    assert h.icon.show.call_count == show_count
    assert h.icon.hide.call_count == hide_count
    h.clock.advance(5000)
    h.banner.assert_not_called()


def test_fly_xembed_survives_late_host_without_own_sni(harness: Harness) -> None:
    h = harness
    h.detect.return_value = SessionKind.FLY
    h.probe.owner.return_value = 0xF00
    h.bus.host_registered = False
    h.bus.registered_items = []
    h.tray.start()
    assert h.icon.isVisible()
    assert h.tray.visible_for_close
    h.clock.advance(5000)
    h.bus.host_changed(True)
    h.clock.advance(31_000)
    assert h.icon.isVisible()
    assert h.tray.visible_for_close
    assert not h.tray.registered
    h.icon.hide.assert_not_called()
    h.icon.show.assert_called_once_with()
    h.banner.assert_not_called()
    h.bus.item_registered(OWN_ITEM)
    assert h.tray.registered


def test_kde_shown_before_late_host_stays_unconfirmed_without_our_item(
    harness: Harness,
) -> None:
    h = harness
    h.detect.return_value = SessionKind.KDE
    h.bus.registered_items = []
    h.tray.start()
    assert h.icon.isVisible()
    assert h.tray.visible_for_close
    assert not h.tray.registered
    # Обычный показ до позднего host signal, без внутренних полей Tray.
    h.clock.advance(5000)
    h.bus.host_changed(True)
    h.clock.advance(31_000)
    assert h.icon.isVisible()
    assert h.tray.visible_for_close
    assert not h.tray.registered
    h.icon.hide.assert_not_called()
    h.banner.assert_not_called()


@pytest.mark.parametrize("failure", ["not-started", "no-host", "show-failed", "stopped"])
def test_close_visibility_requires_an_actually_shown_icon(harness: Harness, failure: str) -> None:
    h = harness
    if failure == "no-host":
        h.bus.host_registered = False
    elif failure == "show-failed":
        h.icon.show.side_effect = None
    if failure != "not-started":
        h.tray.start()
    if failure == "stopped":
        assert h.tray.visible_for_close
        h.tray.stop()
    assert not h.tray.visible_for_close


@pytest.mark.parametrize("answer", ["own", "foreign", "error", "timeout"])
def test_unique_item_requires_our_pid_from_async_bus_reply(harness: Harness, answer: str) -> None:
    h = harness
    h.bus.registered_items = [UNIQUE]
    h.tray.start()
    assert h.icon.isVisible()
    assert not h.tray.registered
    (pending,) = pid_calls(h)
    message = pending.message
    assert message.service() == message.interface() == "org.freedesktop.DBus"
    assert message.path() == "/org/freedesktop/DBus"
    assert message.arguments() == [":1.987"]
    # PID идёт через ту же очередь, что host/items, без второго подключения.
    assert any(
        c.args[1] == "request" and c.args[2][1] is message for c in h.send_command.call_args_list
    )
    h.connection.connectToBus.assert_called_once()
    h.bus.transport.interface.assert_not_called()
    h.bus.transport.callWithCallback.assert_not_called()
    if answer in {"own", "foreign"}:
        pending.reply(os.getpid() if answer == "own" else os.getpid() + 1)
    elif answer == "error":
        pending.fail()
    else:
        pending.success(message.createErrorReply("org.freedesktop.DBus.Error.NoReply", "timeout"))
    assert h.tray.registered is (answer == "own")
    assert h.tray.visible_for_close
    assert h.icon.isVisible()
    h.clock.advance(31_000)
    assert h.tray.registered is (answer == "own")
    h.banner.assert_not_called()


@pytest.mark.parametrize("own", [False, True])
def test_unique_pid_uses_existing_transport_and_caches_for_its_lifetime(
    harness: Harness, own: bool
) -> None:
    h = harness
    h.bus.registered_items = [UNIQUE]
    h.bus.process_ids[":1.987"] = os.getpid() if own else os.getpid() + 1
    h.bus.pid_auto_reply = True
    h.tray.start()
    assert h.tray.registered is own
    pid_requests = [
        c
        for c in h.bus.transport.call.call_args_list
        if c.args[0].member() == "GetConnectionUnixProcessID"
    ]
    assert len(pid_requests) == 1
    h.clock.advance(60_000)
    assert len(pid_calls(h)) == 1
    h.send_command.reset_mock()
    h.probe.owner.reset_mock()
    for _ in range(20):
        assert h.tray.registered is own
        assert h.tray.visible_for_close
    h.send_command.assert_not_called()
    h.probe.owner.assert_not_called()
    h.connection.connectToBus.assert_called_once()


def test_late_unique_pid_cannot_cross_stop_and_start(harness: Harness) -> None:
    h = harness
    h.bus.registered_items = [UNIQUE]
    h.tray.start()
    (stale,) = pid_calls(h)
    h.tray.stop()
    h.tray.start()
    assert not h.tray.registered
    stale.reply(os.getpid())
    assert not h.tray.registered
    current = pid_calls(h)[-1]
    assert current is not stale
    current.reply(os.getpid())
    assert h.tray.registered


@pytest.mark.parametrize("value", [True, "own-pid", 0, -1, None])
def test_unique_pid_reply_requires_a_positive_integer_for_this_process(
    harness: Harness, value: object
) -> None:
    h = harness
    h.bus.registered_items = [UNIQUE]
    h.tray.start()
    (pending,) = pid_calls(h)
    pending.reply(str(os.getpid()) if value == "own-pid" else value)
    assert not h.tray.registered
    assert h.icon.isVisible()


def test_late_pid_for_removed_item_does_not_restore_registration(harness: Harness) -> None:
    h = harness
    h.bus.registered_items = [UNIQUE]
    h.tray.start()
    (pending,) = pid_calls(h)
    h.bus.item_unregistered(UNIQUE)
    pending.reply(os.getpid())
    assert not h.tray.registered
    assert h.icon.isVisible()
    h.clock.advance(31_000)
    assert not h.tray.registered
    h.banner.assert_not_called()


def test_current_empty_items_list_wins_over_late_pid_reply(harness: Harness) -> None:
    h = harness
    h.bus.registered_items = [UNIQUE]
    h.tray.start()
    (pending,) = pid_calls(h)
    h.bus.registered_items = []
    h.clock.advance(10_000)
    pending.reply(os.getpid())
    assert not h.tray.registered
    assert h.icon.isVisible()


@pytest.mark.parametrize("change", ["host", "watcher"])
def test_late_pending_pid_cannot_cross_host_or_watcher_change(
    harness: Harness, change: str
) -> None:
    h = harness
    h.bus.registered_items = [UNIQUE]
    h.tray.start()
    (stale,) = pid_calls(h)
    if change == "host":
        h.bus.host_changed(True)
    else:
        h.watcher.serviceOwnerChanged.emit(SERVICE, ":1.42", ":1.43")
    stale.reply(os.getpid())
    assert not h.tray.registered
    current = pid_calls(h)[-1]
    assert current is not stale
    current.reply(os.getpid())
    assert h.tray.registered


def test_removed_unique_item_rejects_both_stale_items_and_pid_replies(harness: Harness) -> None:
    h = harness
    h.bus.registered_items = [UNIQUE]
    h.tray.start()
    (pid,) = pid_calls(h)
    h.bus.auto_reply = False
    h.bus.signals["PropertiesChanged"](SERVICE, {"RegisteredStatusNotifierItems": []}, [])
    stale_items = h.bus.calls[-1]
    assert stale_items.message.arguments()[1] == "RegisteredStatusNotifierItems"
    h.bus.item_unregistered(UNIQUE)
    current_items = h.bus.calls[-1]
    assert current_items is not stale_items
    current_items.reply([])
    pid.reply(os.getpid())
    stale_items.reply([UNIQUE])
    assert not h.tray.registered
    assert h.icon.isVisible()


def test_plasma_restart_reopens_expired_retry_once(harness: Harness) -> None:
    h = harness
    h.detect.return_value = SessionKind.KDE
    h.bus.host_registered = False
    h.tray.start()
    h.clock.advance(31_000)
    h.banner.assert_called_once_with()
    h.send_command.reset_mock()
    h.probe.owner.reset_mock()
    h.watcher.serviceOwnerChanged.emit("org.kde.plasmashell", ":1.42", ":1.43")
    h.send_command.assert_called()
    assert any(
        c.args[1] == "request"
        and c.args[2][1].member() == "Get"
        and c.args[2][1].arguments() == [SERVICE, "IsStatusNotifierHostRegistered"]
        for c in h.send_command.call_args_list
    )
    assert not h.tray.registered
    assert not h.tray.visible_for_close
    h.clock.advance(31_000)
    h.banner.assert_called_once_with()
    assert not h.tray.registered
    assert not h.tray.visible_for_close
    h.send_command.reset_mock()
    h.probe.owner.reset_mock()
    h.clock.advance(60_000)
    h.send_command.assert_not_called()
    h.probe.owner.assert_not_called()
    h.banner.assert_called_once_with()


@pytest.mark.parametrize("answer", ["pending", "own", "foreign"])
def test_unique_name_disappearance_invalidates_its_pid_evidence(
    harness: Harness, answer: str
) -> None:
    h = harness
    h.bus.registered_items = [UNIQUE]
    h.tray.start()
    (pending,) = pid_calls(h)
    if answer != "pending":
        pending.reply(os.getpid() if answer == "own" else os.getpid() + 1)
        assert h.tray.registered is (answer == "own")
    # Даже если watcher пока держит устаревший список, gone connection уже не наш.
    h.watcher.serviceOwnerChanged.emit(":1.987", ":1.987", "")
    assert not h.tray.registered
    if answer == "pending":
        pending.reply(os.getpid())
        assert not h.tray.registered
    assert h.tray.visible_for_close is (answer != "own")
    h.clock.advance(5000)
    assert not h.tray.registered
    assert h.tray.visible_for_close is (answer != "own")
    h.clock.advance(26_000)
    assert not h.tray.registered
    assert h.tray.visible_for_close is (answer != "own")
    if answer == "own":
        assert not h.icon.isVisible()
        h.banner.assert_called_once_with()
        replacement = ":1.988/StatusNotifierItem"
        h.bus.registered_items = [replacement]
        h.bus.item_registered(replacement)
        current = pid_calls(h)[-1]
        assert current.message.arguments() == [":1.988"]
        current.reply(os.getpid())
        h.clock.advance(1000)
        assert h.tray.registered
        assert h.tray.visible_for_close
        h.clock.advance(31_000)
        h.banner.assert_called_once_with()
    else:
        assert h.icon.isVisible()
        h.banner.assert_not_called()


@pytest.mark.parametrize(
    "item",
    [
        OWN_ITEM.replace("-1/", "-١/"),
        ":١.987/StatusNotifierItem",
        ":1.٩٨٧/StatusNotifierItem",
    ],
)
def test_non_ascii_bus_digits_never_confirm_our_item(harness: Harness, item: str) -> None:
    h = harness
    h.bus.registered_items = [item]
    h.tray.start()
    assert not h.tray.registered
    assert not pid_calls(h)
    h.clock.advance(31_000)
    assert not h.tray.registered
    assert h.icon.isVisible()
    h.banner.assert_not_called()


@pytest.mark.parametrize("event", ["registered", "unregistered"])
def test_foreign_item_signal_cannot_reopen_expired_retry(harness: Harness, event: str) -> None:
    h = harness
    h.bus.host_registered = False
    h.tray.start()
    h.clock.advance(31_000)
    h.banner.assert_called_once_with()
    h.send_command.reset_mock()
    h.probe.owner.reset_mock()
    foreign = "org.kde.StatusNotifierItem-0-1/StatusNotifierItem"
    signal = (
        "StatusNotifierItemRegistered"
        if event == "registered"
        else ("StatusNotifierItemUnregistered")
    )
    h.bus.signals[signal](foreign)
    h.clock.advance(60_000)
    assert not h.tray.registered
    assert not h.icon.isVisible()
    h.send_command.assert_not_called()
    h.probe.owner.assert_not_called()
    h.banner.assert_called_once_with()


@pytest.mark.parametrize("event", ["registered", "unregistered"])
@pytest.mark.parametrize("own", [False, True])
def test_unknown_unique_item_after_expiry_only_our_pid_can_restart_polling(
    harness: Harness, event: str, own: bool
) -> None:
    h = harness
    h.bus.host_registered = False
    h.tray.start()
    h.clock.advance(31_000)
    h.banner.assert_called_once_with()
    assert not h.icon.isVisible()
    h.send_command.reset_mock()
    h.probe.owner.reset_mock()
    h.bus.host_registered = True
    h.bus.registered_items = [UNIQUE] if event == "registered" else []
    signal = (
        "StatusNotifierItemRegistered"
        if event == "registered"
        else "StatusNotifierItemUnregistered"
    )
    h.bus.signals[signal](UNIQUE)
    (pending,) = pid_calls(h)
    assert h.send_command.call_count == 1
    assert not h.tray.registered
    assert not h.icon.isVisible()
    pending.reply(os.getpid() if own else os.getpid() + 1)
    h.clock.advance(60_000)
    assert h.tray.registered is (own and event == "registered")
    assert h.tray.visible_for_close is own
    assert len(pid_calls(h)) == 1
    h.banner.assert_called_once_with()
    if own:
        assert h.send_command.call_count > 1
    else:
        # Оба вида повторных сигналов используют negative cache, не новый retry.
        h.bus.signals["StatusNotifierItemRegistered"](UNIQUE)
        h.bus.signals["StatusNotifierItemUnregistered"](UNIQUE)
        h.clock.advance(60_000)
        assert h.send_command.call_count == 1
        assert len(pid_calls(h)) == 1
        h.probe.owner.assert_not_called()
        h.icon.show.assert_not_called()


@pytest.mark.parametrize("event", ["host", "watcher", "own-item"])
def test_relevant_signal_can_reopen_expired_retry(harness: Harness, event: str) -> None:
    h = harness
    h.bus.host_registered = False
    h.tray.start()
    h.clock.advance(31_000)
    assert not h.tray.visible_for_close
    h.bus.host_registered = True
    if event == "host":
        h.bus.host_changed(True)
    elif event == "watcher":
        h.watcher.serviceRegistered.emit(SERVICE)
    else:
        h.bus.item_registered(OWN_ITEM)
    h.clock.advance(1000)
    assert h.tray.registered
    assert h.tray.visible_for_close
    h.banner.assert_called_once_with()
