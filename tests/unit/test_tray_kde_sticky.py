"""Регрессии KDE: XEmbed не подменяет подтверждённый собственный SNI."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from test_tray import OWN_ITEM, PLASMA, SERVICE, Harness
from test_tray import harness as harness
from test_tray import qapp as qapp

from astra_voice.platform.session import SessionKind
from astra_voice.ui.tray_icons import TrayState

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def forbid_real_display(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Даже с DISPLAY=:99 ни один тест не должен открывать X-соединение."""
    try:
        from Xlib import display
    except ImportError:
        yield
        return
    forbidden = Mock(side_effect=AssertionError("Real Xlib Display forbidden"))
    monkeypatch.setattr(display, "Display", forbidden)
    yield
    forbidden.assert_not_called()


@pytest.mark.parametrize("loss", ["watcher", "host"])
def test_kde_sni_recovers_with_live_xembed_owner_without_sticky_failure(
    harness: Harness, loss: str
) -> None:
    """FT-8: возврат SNI отменяет старый дедлайн после любого вида потери."""
    h = harness
    h.detect.return_value = SessionKind.KDE
    h.probe.owner.return_value = 0xF00
    h.bus.host_registered = True
    h.bus.registered_items = [OWN_ITEM]
    h.tray.start()
    assert h.tray.registered
    assert h.icon.isVisible()

    h.icon.show.reset_mock()
    h.bus.host_registered = False
    if loss == "watcher":
        h.watcher.serviceUnregistered.emit(SERVICE)
    else:
        h.tray._host_changed()
    assert not h.tray.registered
    h.icon.show.assert_not_called()
    assert not h.icon.isVisible()

    h.bus.host_registered = True
    h.bus.registered_items = [OWN_ITEM]
    if loss == "watcher":
        h.watcher.serviceRegistered.emit(SERVICE)
    else:
        h.tray._host_changed()
    assert h.tray.registered
    h.clock.advance(31_000)
    assert h.tray.registered
    h.banner.assert_not_called()


def test_kde_waits_for_own_item_after_host_appears(harness: Harness) -> None:
    """FT-4: существование Qt-значка и host не доказывает регистрацию SNI."""
    h = harness
    h.detect.return_value = SessionKind.KDE
    h.probe.owner.return_value = 0xF00
    h.bus.host_registered = False
    h.bus.registered_items = []
    h.tray.start()
    h.factory.assert_called_once_with()
    assert not h.tray.registered
    h.icon.show.assert_not_called()

    h.bus.host_changed(True)
    assert h.icon.isVisible()
    assert not h.tray.registered
    h.bus.item_registered("org.kde.StatusNotifierItem-0-1/StatusNotifierItem")
    assert not h.tray.registered
    h.bus.item_registered(OWN_ITEM)
    assert h.tray.registered

    h.bus.auto_reply = False
    h.bus.item_unregistered(OWN_ITEM)
    # Потеря должна примениться до любого ответа нового Get.
    assert not h.tray.registered


def test_registered_reads_cache_without_transport_or_xembed(harness: Harness) -> None:
    h = harness
    h.detect.return_value = SessionKind.KDE
    h.bus.registered_items = [OWN_ITEM]
    h.tray.start()
    assert h.tray.registered
    h.send_command.reset_mock()
    h.probe.owner.reset_mock()
    for _ in range(20):
        assert h.tray.registered
    h.send_command.assert_not_called()
    h.probe.owner.assert_not_called()


@pytest.mark.parametrize("loss", ["item", "watcher", "host"])
def test_late_positive_items_reply_cannot_restore_invalidated_sni(
    harness: Harness, loss: str
) -> None:
    """Ответ старого владельца/запроса не отменяет уже доставленную потерю."""
    h = harness
    h.detect.return_value = SessionKind.KDE
    h.probe.owner.return_value = 0xF00
    h.bus.registered_items = [OWN_ITEM]
    h.tray.start()
    assert h.tray.registered

    h.bus.auto_reply = False
    h.clock.advance(10_000)
    pending = next(
        call
        for call in reversed(h.bus.calls)
        if call.message.member() == "Get"
        and call.message.arguments()[1] == "RegisteredStatusNotifierItems"
    )
    if loss == "item":
        h.bus.item_unregistered(OWN_ITEM)
    else:
        h.bus.host_registered = False
        if loss == "watcher":
            h.watcher.serviceUnregistered.emit(SERVICE)
        else:
            h.tray._host_changed()
    assert not h.tray.registered
    pending.reply([OWN_ITEM])
    assert not h.tray.registered


@pytest.mark.parametrize("loss", ["icon", "plasma"])
def test_hidden_sni_requires_fresh_confirmation_before_registering_again(
    harness: Harness, loss: str
) -> None:
    """hide() снимает SNI в Qt даже при отсутствии ItemUnregistered."""
    h = harness
    h.detect.return_value = SessionKind.KDE
    h.bus.registered_items = [OWN_ITEM]
    h.tray.start()
    assert h.tray.registered
    h.icon.hide.reset_mock()
    h.bus.auto_reply = False
    h.clock.advance(10_000)
    stale_items = next(
        call
        for call in reversed(h.bus.calls)
        if call.message.member() == "Get"
        and call.message.arguments()[1] == "RegisteredStatusNotifierItems"
    )

    if loss == "icon":
        h.provider.has_icon.return_value = False
        assert not h.tray.registered
    else:
        h.watcher.serviceUnregistered.emit(PLASMA)
        assert not h.tray.registered
    h.icon.hide.assert_called()
    assert not h.icon.isVisible()

    calls_before_return = len(h.bus.calls)
    if loss == "icon":
        h.provider.has_icon.return_value = True
        h.tray.set_state(TrayState.IDLE)
    else:
        h.watcher.serviceRegistered.emit(PLASMA)
    assert not h.tray.registered
    stale_items.reply([OWN_ITEM])
    assert not h.tray.registered
    pending = next(
        call
        for call in reversed(h.bus.calls[calls_before_return:])
        if call.message.member() == "Get"
        and call.message.arguments()[1] == "RegisteredStatusNotifierItems"
    )
    pending.reply([OWN_ITEM])
    assert h.tray.registered


def test_items_reply_while_hidden_does_not_confirm_sni_after_plasma_returns(
    harness: Harness,
) -> None:
    """Даже новый ответ до show() не подтверждает следующую Qt-регистрацию."""
    h = harness
    h.detect.return_value = SessionKind.KDE
    h.bus.registered_items = [OWN_ITEM]
    h.tray.start()
    assert h.tray.registered

    h.watcher.serviceUnregistered.emit(PLASMA)
    assert not h.tray.registered
    assert not h.icon.isVisible()
    h.bus.auto_reply = False
    calls_before_signal = len(h.bus.calls)
    h.bus.item_registered(OWN_ITEM)
    hidden_reply = next(
        call
        for call in reversed(h.bus.calls[calls_before_signal:])
        if call.message.member() == "Get"
        and call.message.arguments()[1] == "RegisteredStatusNotifierItems"
    )
    hidden_reply.reply([OWN_ITEM])
    assert not h.icon.isVisible()
    assert not h.tray.registered

    calls_before_show = len(h.bus.calls)
    h.watcher.serviceRegistered.emit(PLASMA)
    assert h.icon.isVisible()
    assert not h.tray.registered
    shown_reply = next(
        call
        for call in reversed(h.bus.calls[calls_before_show:])
        if call.message.member() == "Get"
        and call.message.arguments()[1] == "RegisteredStatusNotifierItems"
    )
    shown_reply.reply([OWN_ITEM])
    assert h.tray.registered
