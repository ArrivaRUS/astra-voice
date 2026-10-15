"""XEmbed is best-effort presentation, never O4 registration evidence."""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt5.QtDBus import QDBusMessage
from test_tray import OWN_ITEM, SERVICE, Harness
from test_tray import forbid_real_x as forbid_real_x
from test_tray import harness as harness
from test_tray import qapp as qapp

from astra_voice.platform.session import SessionKind
from astra_voice.ui.tray_icons import TrayState

pytestmark = pytest.mark.unit


@pytest.fixture
def xembed(harness: Harness) -> Harness:
    harness.bus.host_registered = False
    harness.probe.owner.return_value = 0xF00
    harness.icon.isSystemTrayAvailable.side_effect = AssertionError("Qt probe forbidden")
    return harness


def missing_watcher(h: Harness) -> None:
    original = h.bus.call

    def call(message: QDBusMessage, mode: object, timeout: int) -> QDBusMessage:
        if message.member() == "Get":
            return message.createErrorReply(
                "org.freedesktop.DBus.Error.ServiceUnknown", "watcher absent"
            )
        return original(message, mode, timeout)

    h.bus.transport.call.side_effect = call


@pytest.mark.parametrize("missing", [False, True])
def test_xembed_show_never_proves_registration_or_warns(xembed: Harness, missing: bool) -> None:
    h = xembed
    if missing:
        missing_watcher(h)
    h.tray.start()
    h.icon.show.assert_called_once_with()
    assert not h.tray.registered
    h.clock.advance(60_000)
    assert not h.tray.registered
    h.banner.assert_not_called()
    h.flush.assert_not_called()
    h.icon.isSystemTrayAvailable.assert_not_called()


def test_kde_never_uses_xembed(xembed: Harness) -> None:
    h = xembed
    h.detect.return_value = SessionKind.KDE
    h.tray.start()
    h.clock.advance(30_000)
    assert not h.tray.registered
    assert h.tray._path == "нет"
    h.probe.owner.assert_not_called()
    h.icon.show.assert_not_called()
    h.banner.assert_called_once_with()


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_ambiguous_host_reply_does_not_enable_xembed(xembed: Harness, value: object) -> None:
    xembed.bus.host_registered = value
    xembed.tray.start()
    xembed.icon.show.assert_not_called()
    xembed.probe.owner.assert_not_called()
    assert not xembed.tray.registered


def test_registered_never_queries_x_or_bus(xembed: Harness) -> None:
    h = xembed
    h.tray.start()
    h.probe.owner.reset_mock()
    h.bus.transport.call.reset_mock()
    for _ in range(20):
        assert not h.tray.registered
    h.probe.owner.assert_not_called()
    h.bus.transport.call.assert_not_called()


def test_owner_poll_is_at_most_once_per_ten_seconds(xembed: Harness) -> None:
    h = xembed
    h.tray.start()
    h.probe.owner.assert_called_once_with()
    for _ in range(9):
        h.clock.advance(1000)
        h.tray._host_changed()
    h.probe.owner.assert_called_once_with()
    h.clock.advance(1000)
    assert h.probe.owner.call_count == 2


def test_owner_loss_hides_and_return_retries(xembed: Harness) -> None:
    h = xembed
    h.tray.start()
    h.probe.owner.return_value = None
    h.clock.advance(10_000)
    assert h.tray._path == "нет"
    assert not h.icon.isVisible()
    assert not h.tray.registered
    h.probe.owner.return_value = 0xF01
    h.clock.advance(10_000)
    assert h.tray._path == "XEmbed"
    assert h.icon.isVisible()
    assert not h.tray.registered
    h.clock.advance(31_000)
    h.banner.assert_not_called()


def test_sni_requires_our_item_after_xembed_show(xembed: Harness) -> None:
    h = xembed
    h.tray.start()
    h.bus.host_registered = True
    h.bus.registered_items = []
    h.watcher.serviceRegistered.emit(SERVICE)
    assert not h.tray.registered
    h.bus.item_registered(OWN_ITEM)
    assert h.tray.registered
    h.probe.owner.return_value = None
    h.clock.advance(31_000)
    assert h.tray.registered
    h.banner.assert_not_called()


def test_restart_does_not_carry_registration_proof(xembed: Harness) -> None:
    h = xembed
    h.tray.start()
    h.tray.stop()
    h.bus.host_registered = True
    h.bus.registered_items = []
    h.tray.start()
    assert not h.tray.registered
    h.bus.item_registered()
    assert h.tray.registered


def test_stop_cancels_owner_monitoring_and_late_replies(xembed: Harness) -> None:
    h = xembed
    h.tray.start()
    h.bus.auto_reply = False
    h.watcher.serviceRegistered.emit(SERVICE)
    pending = h.bus.calls[-1]
    h.tray.stop()
    h.icon.show.reset_mock()
    h.probe.owner.reset_mock()
    pending.reply(False)
    h.clock.advance(60_000)
    assert not h.tray.registered
    h.probe.owner.assert_not_called()
    h.icon.show.assert_not_called()
    h.banner.assert_not_called()
    h.probe.close.assert_called_once_with()
    assert all(timer.due is None for timer in h.clock.timers)


@pytest.mark.parametrize("loss", ["visible", "provider_icon"])
def test_xembed_show_failure_or_empty_icon_cannot_register(xembed: Harness, loss: str) -> None:
    h = xembed
    if loss == "visible":
        h.icon.show.side_effect = None
    else:
        h.provider.has_icon.return_value = False
    h.tray.start()
    h.clock.advance(30_000)
    assert not h.tray.registered
    h.banner.assert_called_once_with()
    h.flush.assert_not_called()


@pytest.mark.parametrize("missing", [False, True])
def test_no_owner_warns_once_without_extending_deadline(xembed: Harness, missing: bool) -> None:
    h = xembed
    h.probe.owner.return_value = None
    if missing:
        missing_watcher(h)
    h.tray.start()
    h.clock.advance(29_999)
    h.tray.start()
    h.banner.assert_not_called()
    h.clock.advance(1)
    h.banner.assert_called_once_with()
    h.clock.advance(60_000)
    h.banner.assert_called_once_with()
    h.icon.show.assert_not_called()


@pytest.mark.parametrize("kind", list(SessionKind))
@pytest.mark.parametrize("missing", ["host", "item"])
def test_final_failure_stops_both_ports_until_host_signal(
    harness: Harness, kind: SessionKind, missing: str
) -> None:
    """После 30 с отказ окончателен; новый host начинает отдельный цикл."""
    h = harness
    h.detect.return_value = kind
    h.probe.owner.return_value = None
    h.bus.host_registered = missing != "host"
    h.bus.registered_items = []
    h.tray.start()
    h.clock.advance(29_999)
    h.banner.assert_not_called()
    h.clock.advance(1)
    h.banner.assert_called_once_with()
    assert not h.tray.registered
    assert not h.icon.isVisible()

    counts = (
        h.bus.transport.call.call_count,
        h.send_command.call_count,
        h.probe.owner.call_count,
        h.probe.close.call_count,
    )
    h.clock.advance(60_000)
    assert (
        h.bus.transport.call.call_count,
        h.send_command.call_count,
        h.probe.owner.call_count,
        h.probe.close.call_count,
    ) == counts
    assert not h.tray.registered
    h.banner.assert_called_once_with()

    # Сигнал хоста и чужой элемент не подтверждают нашу регистрацию.
    h.bus.registered_items = ["org.kde.StatusNotifierItem-0-1/StatusNotifierItem"]
    h.bus.host_changed(True)
    assert h.bus.transport.call.call_count > counts[0]
    assert not h.tray.registered
    assert h.icon.isVisible()
    h.clock.advance(1000)
    h.bus.signals["StatusNotifierItemRegistered"](OWN_ITEM)
    assert not h.tray.registered
    h.bus.item_registered(OWN_ITEM)
    assert h.tray.registered
    h.flush.assert_called_once_with()
    h.clock.advance(31_000)
    assert h.tray.registered
    h.banner.assert_called_once_with()


@pytest.mark.parametrize("kind", list(SessionKind))
def test_registered_sni_poll_reads_host_and_items_once_without_xembed(
    harness: Harness, kind: SessionKind
) -> None:
    h = harness
    h.detect.return_value = kind
    h.tray.start()
    assert h.tray.registered
    h.bus.transport.call.reset_mock()
    h.probe.owner.reset_mock()
    h.clock.advance(9999)
    h.bus.transport.call.assert_not_called()
    h.clock.advance(1)
    properties = [
        entry.args[0].arguments()[1]
        for entry in h.bus.transport.call.call_args_list
        if entry.args[0].member() == "Get"
    ]
    assert properties == ["IsStatusNotifierHostRegistered", "RegisteredStatusNotifierItems"]
    assert h.bus.transport.call.call_count == 2
    h.probe.owner.assert_not_called()
    assert h.tray.registered


@pytest.mark.parametrize("kind", list(SessionKind))
@pytest.mark.parametrize("lost", ["host", "item"])
def test_registered_sni_poll_invalidates_missing_confirmation(
    harness: Harness, kind: SessionKind, lost: str
) -> None:
    h = harness
    h.detect.return_value = kind
    h.tray.start()
    assert h.tray.registered
    if lost == "host":
        h.bus.host_registered = False
    else:
        h.bus.registered_items = []
    h.clock.advance(9999)
    assert h.tray.registered
    h.clock.advance(1)
    assert not h.tray.registered
    h.clock.advance(29_999)
    h.banner.assert_not_called()
    h.clock.advance(1)
    h.banner.assert_called_once_with()


@pytest.mark.parametrize("kind", [SessionKind.FLY, SessionKind.OTHER])
def test_shown_xembed_poll_reads_owner_and_host_then_requires_sni_confirmation(
    xembed: Harness, kind: SessionKind
) -> None:
    h = xembed
    h.detect.return_value = kind
    h.bus.registered_items = []
    h.tray.start()
    assert h.icon.isVisible()
    assert not h.tray.registered
    h.bus.transport.call.reset_mock()
    h.probe.owner.reset_mock()
    h.clock.advance(9999)
    h.bus.transport.call.assert_not_called()
    h.probe.owner.assert_not_called()
    h.clock.advance(1)
    h.probe.owner.assert_called_once_with()
    h.bus.transport.call.assert_called_once()
    assert h.bus.transport.call.call_args.args[0].arguments()[1] == (
        "IsStatusNotifierHostRegistered"
    )
    assert not h.tray.registered

    h.bus.host_registered = True
    h.bus.transport.call.reset_mock()
    h.clock.advance(10_000)
    host_reads = [
        entry
        for entry in h.bus.transport.call.call_args_list
        if entry.args[0].member() == "Get"
        and entry.args[0].arguments()[1] == "IsStatusNotifierHostRegistered"
    ]
    assert len(host_reads) == 1
    assert not h.tray.registered
    h.clock.advance(1000)
    assert not h.tray.registered
    h.bus.item_registered(OWN_ITEM)
    assert h.tray.registered
    h.clock.advance(31_000)
    h.banner.assert_not_called()


@pytest.mark.parametrize("kind", [SessionKind.FLY, SessionKind.OTHER])
def test_shown_xembed_state_changes_keep_ten_second_poll_without_retry(
    xembed: Harness, kind: SessionKind
) -> None:
    h = xembed
    h.detect.return_value = kind
    h.tray.start()
    h.bus.transport.call.reset_mock()
    h.probe.owner.reset_mock()
    for state in (TrayState.LISTENING, TrayState.PROCESSING, TrayState.IDLE):
        h.tray.set_state(state)
        assert h.tray._deadline is None
        assert not h.tray.registered
        assert h.icon.isVisible()
        h.clock.advance(3000)
        assert h.tray._deadline is None
    h.bus.transport.call.assert_not_called()
    h.probe.owner.assert_not_called()
    h.clock.advance(1000)
    h.bus.transport.call.assert_called_once()
    assert h.bus.transport.call.call_args.args[0].arguments()[1] == (
        "IsStatusNotifierHostRegistered"
    )
    h.probe.owner.assert_called_once_with()
    assert h.tray._deadline is None
    assert not h.tray.registered
    h.clock.advance(50_000)
    assert h.bus.transport.call.call_count == 6
    assert h.probe.owner.call_count == 6
    assert h.tray._deadline is None
    h.banner.assert_not_called()


@pytest.mark.parametrize("kind", [SessionKind.FLY, SessionKind.OTHER])
@pytest.mark.parametrize("event", ["item", "properties"])
def test_shown_xembed_item_changes_keep_ten_second_poll_without_retry(
    xembed: Harness, kind: SessionKind, event: str
) -> None:
    h = xembed
    h.detect.return_value = kind
    h.tray.start()
    h.bus.transport.call.reset_mock()
    h.probe.owner.reset_mock()
    if event == "item":
        h.bus.item_registered("org.kde.StatusNotifierItem-0-1/StatusNotifierItem")
    else:
        h.bus.signals["PropertiesChanged"](SERVICE, {"RegisteredStatusNotifierItems": []}, [])
    assert h.tray._deadline is None
    h.bus.transport.call.assert_not_called()
    assert not h.tray.registered
    assert h.icon.isVisible()
    h.clock.advance(60_000)
    assert h.bus.transport.call.call_count == 6
    assert all(
        entry.args[0].member() == "Get"
        and entry.args[0].arguments()[1] == "IsStatusNotifierHostRegistered"
        for entry in h.bus.transport.call.call_args_list
    )
    assert h.probe.owner.call_count == 6
    assert h.tray._deadline is None
    assert not h.tray.registered
    assert h.icon.isVisible()
    h.banner.assert_not_called()


def test_late_negative_host_reply_cannot_restart_expired_registration(xembed: Harness) -> None:
    h = xembed
    h.bus.auto_reply = False
    h.tray.start()
    pending = h.bus.calls[-1]
    h.clock.advance(30_000)
    h.banner.assert_called_once_with()
    pending.reply(False)
    h.clock.advance(6000)
    assert not h.tray.registered
    h.icon.show.assert_not_called()
    h.probe.owner.assert_not_called()
    h.banner.assert_called_once_with()


def test_path_info_is_logged_once_per_transition(
    xembed: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    h = xembed
    h.probe.owner.return_value = None
    caplog.set_level(logging.INFO, logger="astra_voice.ui.tray")
    h.tray.start()
    h.clock.advance(9000)
    h.probe.owner.return_value = 0xF00
    h.clock.advance(1000)
    h.clock.advance(20_000)
    h.bus.host_registered = True
    h.watcher.serviceRegistered.emit(SERVICE)
    h.clock.advance(20_000)
    paths = [r.getMessage() for r in caplog.records if r.getMessage().startswith("трей: путь ")]
    assert paths == [
        "трей: путь нет",
        "трей: путь XEmbed (без подтверждения видимости — индикация через пилюлю)",
        "трей: путь SNI",
    ]


@pytest.fixture
def xlib(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Only fake X resources: no real display or subprocess may be reached."""
    spawn = Mock(side_effect=AssertionError("XEmbed must not spawn processes"))
    monkeypatch.setattr(subprocess, "run", spawn)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setenv("DISPLAY", "unit-test-fake-display")
    root = SimpleNamespace(id=1)
    window = Mock()
    window.query_tree.return_value = SimpleNamespace(parent=SimpleNamespace(id=0xF00), root=root)
    conn = Mock()
    conn.get_default_screen.return_value = 2
    conn.screen.return_value = SimpleNamespace(root=root)
    conn.intern_atom.return_value = 42
    conn.get_selection_owner.return_value = SimpleNamespace(id=0xF00)
    conn.create_resource_object.return_value = window
    display = Mock(return_value=conn)
    fake_xlib = ModuleType("Xlib")
    fake_display = ModuleType("Xlib.display")
    vars(fake_display)["Display"] = display
    vars(fake_xlib)["display"] = fake_display
    monkeypatch.setitem(sys.modules, "Xlib", fake_xlib)
    monkeypatch.setitem(sys.modules, "Xlib.display", fake_display)
    yield SimpleNamespace(conn=conn, display=display, window=window, root=root, spawn=spawn)
    spawn.assert_not_called()


def test_probe_without_display_never_connects_or_spawns(
    xlib: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    monkeypatch.delenv("DISPLAY")
    port = XEmbedTrayProbe()
    assert port.owner() is None
    port.close()
    xlib.display.assert_not_called()


def test_probe_missing_xlib_is_unavailable(
    xlib: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    monkeypatch.setitem(sys.modules, "Xlib", None)
    monkeypatch.setitem(sys.modules, "Xlib.display", None)
    port = XEmbedTrayProbe()
    assert port.owner() is None
    port.close()
    xlib.display.assert_not_called()


def test_probe_reuses_one_connection_for_owner_and_closes(
    xlib: SimpleNamespace,
) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    port = XEmbedTrayProbe()
    xlib.display.assert_not_called()  # Construction is lazy.
    try:
        assert port.owner() == 0xF00
        for _ in range(3):
            assert port.owner() == 0xF00
        xlib.display.assert_called_once_with()
        xlib.conn.get_default_screen.assert_called()
        assert all(
            call.args == ("_NET_SYSTEM_TRAY_S2",) and call.kwargs == {"only_if_exists": True}
            for call in xlib.conn.intern_atom.call_args_list
        )
        xlib.conn.get_selection_owner.assert_called_with(42)
        xlib.conn.set_error_handler.assert_called_once()
        xlib.conn.close.assert_not_called()
    finally:
        port.close()
    port.close()
    xlib.conn.close.assert_called_once_with()


@pytest.mark.parametrize("owner_id", [1, 0xF00, 0xFFFFFFFF])
def test_probe_accepts_32bit_owner(xlib: SimpleNamespace, owner_id: int) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    xlib.conn.get_selection_owner.return_value = SimpleNamespace(id=owner_id)
    port = XEmbedTrayProbe()
    try:
        assert port.owner() == owner_id
    finally:
        port.close()


@pytest.mark.parametrize("owner_id", [None, 0, -1, 0x100000000])
def test_probe_rejects_missing_or_out_of_range_owner(
    xlib: SimpleNamespace, owner_id: int | None
) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    xlib.conn.get_selection_owner.return_value = (
        None if owner_id is None else SimpleNamespace(id=owner_id)
    )
    port = XEmbedTrayProbe()
    try:
        assert port.owner() is None
    finally:
        port.close()


def test_probe_missing_selection_atom_is_unavailable(xlib: SimpleNamespace) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    xlib.conn.intern_atom.return_value = 0
    port = XEmbedTrayProbe()
    try:
        assert port.owner() is None
        xlib.conn.get_selection_owner.assert_not_called()
    finally:
        port.close()


def test_probe_disconnect_fails_closed_and_reconnects_next_call(xlib: SimpleNamespace) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    broken = xlib.conn
    replacement = Mock()
    replacement.get_default_screen.return_value = 2
    replacement.intern_atom.return_value = 42
    replacement.get_selection_owner.return_value = SimpleNamespace(id=0xF00)
    xlib.display.side_effect = [broken, replacement]
    port = XEmbedTrayProbe()
    assert port.owner() == 0xF00
    broken.get_selection_owner.side_effect = OSError("fake disconnect")
    assert port.owner() is None
    broken.close.assert_called_once_with()
    assert port.owner() == 0xF00
    assert xlib.display.call_count == 2
    port.close()
    replacement.close.assert_called_once_with()


def test_probe_error_handler_fails_closed_without_stderr(
    xlib: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    port = XEmbedTrayProbe()
    assert port.owner() == 0xF00
    handler = xlib.conn.set_error_handler.call_args.args[0]
    xlib.conn.sync.side_effect = lambda: handler(OSError("fake BadWindow"), None)
    assert port.owner() is None
    xlib.conn.close.assert_called_once_with()
    port.close()
    assert not capsys.readouterr().err


@pytest.mark.parametrize("failure", ["connect", "close"])
def test_probe_connection_and_cleanup_errors_do_not_escape(
    xlib: SimpleNamespace, failure: str
) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    if failure == "connect":
        xlib.display.side_effect = OSError("fake server unavailable")
    else:
        xlib.conn.close.side_effect = OSError("fake close failure")
    port = XEmbedTrayProbe()
    if failure == "connect":
        assert port.owner() is None
    else:
        assert port.owner() == 0xF00
    port.close()


@pytest.mark.parametrize("signal", ["duplicate_registration", "owner_replacement"])
def test_healthy_sni_survives_plasma_registration_signal(harness: Harness, signal: str) -> None:
    h = harness
    h.detect.return_value = SessionKind.KDE
    h.tray.start()
    assert h.tray.registered
    h.icon.hide.reset_mock()
    if signal == "duplicate_registration":
        h.watcher.serviceRegistered.emit("org.kde.plasmashell")
    else:
        h.watcher.serviceOwnerChanged.emit("org.kde.plasmashell", ":1.42", ":1.43")
    assert h.tray.registered
    h.icon.hide.assert_not_called()
    h.icon.show.assert_called_once_with()


def test_sni_ready_has_priority_and_never_calls_xembed_port(harness: Harness) -> None:
    h = harness
    h.probe.owner.return_value = 0xF00
    h.tray.start()
    h.clock.advance(31_000)
    assert h.tray.registered
    assert h.tray._path == "SNI"
    h.probe.owner.assert_not_called()
    h.icon.show.assert_called_once_with()
    h.banner.assert_not_called()


def test_unresolved_sni_reply_fails_closed_until_negative_response(xembed: Harness) -> None:
    h = xembed
    h.bus.auto_reply = False
    h.tray.start()
    h.clock.advance(1000)
    h.icon.show.assert_not_called()
    h.probe.owner.assert_not_called()
    h.bus.calls[-1].fail()
    h.icon.show.assert_not_called()
    h.probe.owner.assert_not_called()
    h.clock.advance(1000)
    h.bus.calls[-1].reply(False)
    h.icon.show.assert_called_once_with()
    assert not h.tray.registered


def test_xembed_probe_failure_is_unavailable_and_retryable(xembed: Harness) -> None:
    h = xembed
    h.probe.owner.side_effect = OSError("fake unavailable display")
    h.tray.start()
    h.probe.close.assert_called_once_with()
    h.icon.show.assert_not_called()
    h.probe.owner.side_effect = None
    h.clock.advance(9999)
    h.probe.owner.assert_called_once_with()
    h.clock.advance(1)
    assert h.probe.owner.call_count == 2
    h.icon.show.assert_called_once_with()
    assert not h.tray.registered
    h.banner.assert_not_called()


def test_owner_replacement_retries_xembed_without_proving_registration(xembed: Harness) -> None:
    h = xembed
    h.tray.start()
    h.probe.owner.return_value = 0xF01
    h.clock.advance(10_000)
    assert h.tray._path == "XEmbed"
    assert h.icon.show.call_count == 2
    h.icon.hide.assert_called_once_with()
    assert not h.tray.registered
    h.banner.assert_not_called()


def test_path_and_owner_changes_do_not_extend_original_deadline(xembed: Harness) -> None:
    h = xembed
    h.icon.show.side_effect = None
    h.tray.start()
    for index in range(1, 6):
        h.clock.advance(5000)
        h.probe.owner.return_value = 0xF00 + index
        h.watcher.serviceRegistered.emit(SERVICE)
        assert not h.tray.registered
        h.banner.assert_not_called()
    h.clock.advance(4999)
    h.banner.assert_not_called()
    h.clock.advance(1)
    h.banner.assert_called_once_with()
