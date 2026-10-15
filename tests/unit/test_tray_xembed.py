"""XEmbed fallback: explicit host result, local evidence and owner lifecycle."""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QRect
from PyQt5.QtDBus import QDBusMessage
from PyQt5.QtGui import QIcon
from test_tray import PLASMA, SERVICE, Harness
from test_tray import harness as harness
from test_tray import qapp as qapp

from astra_voice.platform.session import SessionKind

pytestmark = pytest.mark.unit


@pytest.fixture
def owner() -> Mock:
    return Mock(return_value=0xF00)


@pytest.fixture
def probe(owner: Mock) -> Mock:
    port = Mock(spec=["owner", "is_docked", "close"])
    port.owner = owner
    port.is_docked.return_value = True
    return port


@pytest.fixture
def native_window(harness: Harness) -> Mock:
    widget = Mock()
    widget.objectName.return_value = "QSystemTrayIconSys"
    widget.metaObject.return_value.className.return_value = "QSystemTrayIconSys"
    widget.winId.return_value = 0xCAFE
    widget.isVisible.side_effect = harness.icon.isVisible
    widget.geometry.side_effect = harness.icon.geometry
    return widget


@pytest.fixture
def xembed(
    harness: Harness,
    probe: Mock,
    native_window: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Harness]:
    from astra_voice.ui import tray as module

    h = harness
    original = h.tray
    forbidden = AssertionError("isSystemTrayAvailable poisons the Qt cache")
    h.icon.isSystemTrayAvailable.side_effect = forbidden
    qt_probe = vars(module)["QSystemTrayIcon"].isSystemTrayAvailable
    qt_probe.side_effect = forbidden
    image = Mock(spec=QIcon)
    image.isNull.return_value = False
    h.provider.icon.side_effect = None
    h.provider.icon.return_value = image
    h.icon.icon.return_value = image
    h.icon.geometry.return_value = QRect(0, 0, 16, 16)
    h.bus.host_registered = False
    monkeypatch.setattr(
        module,
        "QApplication",
        SimpleNamespace(topLevelWidgets=Mock(return_value=[native_window])),
    )
    # A missed injection must fail locally, never contact an X server.
    monkeypatch.setattr(
        module, "XEmbedTrayProbe", Mock(side_effect=AssertionError("Real X11 forbidden"))
    )
    h.tray = module.Tray(h.provider, hotkey="Alt+F9", tray_factory=h.factory, xembed_probe=probe)
    try:
        yield h
    finally:
        h.tray.stop()
        if not sip.isdeleted(h.tray):
            h.tray.deleteLater()
        h.tray = original
        h.icon.isSystemTrayAvailable.assert_not_called()
        qt_probe.assert_not_called()


def missing_watcher(h: Harness) -> None:
    original = h.bus.call

    def call(message: QDBusMessage, mode: object, timeout: int) -> QDBusMessage:
        if message.member() == "Get":
            return message.createErrorReply(
                "org.freedesktop.DBus.Error.ServiceUnknown", "watcher absent"
            )
        return original(message, mode, timeout)

    h.bus.transport.call.side_effect = call


def test_xembed_waits_for_local_visibility_and_geometry(xembed: Harness) -> None:
    h = xembed
    missing_watcher(h)
    h.icon.show.side_effect = None
    h.icon.isVisible.return_value = False
    h.icon.geometry.return_value = QRect()
    h.tray.start()
    assert h.icon.show.called
    assert not h.tray.registered
    h.flush.assert_not_called()
    h.icon.isVisible.return_value = True
    h.clock.advance(1000)
    assert not h.tray.registered
    h.icon.geometry.return_value = QRect(0, 0, 16, 16)
    h.clock.advance(1000)
    assert h.tray.registered
    h.flush.assert_called_once_with()
    h.banner.assert_not_called()


@pytest.mark.parametrize("watcher_absent", [True, False])
def test_no_sni_or_xembed_never_shows_and_warns_once_at_30_seconds(
    xembed: Harness, owner: Mock, watcher_absent: bool
) -> None:
    h = xembed
    owner.return_value = None
    if watcher_absent:
        missing_watcher(h)
    h.tray.start()
    h.clock.advance(29_999)
    h.icon.show.assert_not_called()
    h.banner.assert_not_called()
    assert not h.tray.registered
    h.clock.advance(1)
    h.banner.assert_called_once_with()
    h.clock.advance(60_000)
    assert not h.tray.registered
    h.icon.show.assert_not_called()
    h.banner.assert_called_once_with()


def test_sni_ready_has_priority_and_never_calls_xembed_port(
    xembed: Harness, owner: Mock, probe: Mock
) -> None:
    h = xembed
    owner.side_effect = AssertionError("XEmbed probe must not run with SNI ready")
    h.bus.host_registered = True
    h.tray.start()
    assert h.tray.registered
    h.icon.show.assert_called_once_with()
    h.clock.advance(60_000)
    assert h.tray.registered
    owner.assert_not_called()
    probe.is_docked.assert_not_called()
    h.banner.assert_not_called()


@pytest.mark.parametrize("signal", ["duplicate_registration", "owner_replacement"])
def test_healthy_sni_survives_plasma_registration_signal(
    xembed: Harness, owner: Mock, signal: str
) -> None:
    h = xembed
    h.detect.return_value = SessionKind.KDE
    h.bus.host_registered = True
    assert h.bus.plasma_owner
    h.tray.start()
    assert h.tray.registered
    h.icon.hide.reset_mock()
    if signal == "duplicate_registration":
        h.watcher.serviceRegistered.emit(PLASMA)
    else:
        h.watcher.serviceOwnerChanged.emit(PLASMA, h.bus.plasma_owner, ":1.43")
    assert h.tray.registered
    h.clock.advance(30_000)
    assert h.tray.registered
    h.icon.hide.assert_not_called()
    h.banner.assert_not_called()
    owner.assert_not_called()


def test_host_false_reply_allows_xembed_without_watcher_disappearing(xembed: Harness) -> None:
    h = xembed
    h.bus.auto_reply = False
    h.tray.start()
    request = h.bus.calls[-1]
    assert request.message.member() == "Get"
    h.icon.show.assert_not_called()
    request.reply(False)
    assert h.tray.registered
    assert h.icon.show.called


def test_unresolved_sni_reply_fails_closed_until_negative_response(
    xembed: Harness, owner: Mock
) -> None:
    h = xembed
    h.bus.auto_reply = False
    h.tray.start()
    h.clock.advance(5000)
    assert not h.tray.registered
    h.icon.show.assert_not_called()
    owner.assert_not_called()
    h.bus.calls[-1].fail()
    assert not h.tray.registered
    h.icon.show.assert_not_called()
    owner.assert_not_called()
    h.clock.advance(1000)
    h.bus.calls[-1].reply(False)
    assert h.tray.registered
    assert owner.called


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_malformed_host_reply_does_not_authorize_xembed(
    xembed: Harness, owner: Mock, value: object
) -> None:
    h = xembed
    h.bus.auto_reply = False
    h.tray.start()
    h.bus.calls[-1].reply(value)
    assert not h.tray.registered
    h.icon.show.assert_not_called()
    owner.assert_not_called()


def test_xembed_probe_failure_is_unavailable_and_retryable(xembed: Harness, owner: Mock) -> None:
    h = xembed
    owner.side_effect = OSError("probe failed")
    h.tray.start()
    assert not h.tray.registered
    h.icon.show.assert_not_called()
    owner.side_effect = None
    h.clock.advance(2000)
    assert h.tray.registered
    h.banner.assert_not_called()


@pytest.mark.parametrize("next_owner", [None, 0xF01])
def test_owner_loss_or_replacement_invalidates_and_retries(
    xembed: Harness, owner: Mock, next_owner: int | None
) -> None:
    h = xembed
    h.tray.start()
    assert h.tray.registered
    h.icon.hide.reset_mock()
    h.icon.show.reset_mock()
    # The old embedding cannot prove that a replacement panel accepted the icon.
    h.icon.show.side_effect = None
    owner.return_value = next_owner
    h.clock.advance(2000)
    assert not h.tray.registered
    assert h.icon.hide.called
    h.flush.reset_mock()
    if next_owner is None:
        h.icon.show.assert_not_called()
    else:
        h.clock.advance(1000)
        assert h.icon.show.called
        assert not h.tray.registered
    owner.return_value = next_owner if next_owner is not None else 0xF02
    h.icon.show.side_effect = lambda: setattr(h.icon.isVisible, "return_value", True)
    h.clock.advance(3000)
    assert h.tray.registered
    h.flush.assert_called_once_with()


def test_owner_is_monitored_after_success_without_registered_reads(
    xembed: Harness, owner: Mock
) -> None:
    h = xembed
    h.tray.start()
    assert h.tray.registered
    calls = owner.call_count
    owner.return_value = None
    h.icon.hide.reset_mock()
    h.clock.advance(2000)
    assert owner.call_count > calls
    assert h.icon.hide.called
    assert not h.tray.registered


def test_registered_uses_cached_owner_without_x11_or_bus_queries(
    xembed: Harness, owner: Mock, probe: Mock
) -> None:
    h = xembed
    h.tray.start()
    assert h.tray.registered
    owner.reset_mock()
    probe.is_docked.reset_mock()
    h.bus.transport.call.reset_mock()
    for _ in range(10):
        assert h.tray.registered
    owner.assert_not_called()
    probe.is_docked.assert_not_called()
    h.bus.transport.call.assert_not_called()


@pytest.mark.parametrize("identity", ["object_name", "meta_class"])
def test_root_parent_is_not_docked_until_next_retry_tick(
    xembed: Harness, probe: Mock, native_window: Mock, identity: str
) -> None:
    h = xembed
    if identity == "object_name":
        native_window.metaObject.return_value.className.return_value = "QWidget"
    else:
        native_window.objectName.return_value = ""
    parent = 1
    probe.is_docked.side_effect = lambda window_id, owner_id: parent != 1
    h.tray.start()
    assert h.icon.isVisible()
    assert not h.tray.registered
    probe.is_docked.assert_called_once_with(0xCAFE, 0xF00)
    h.flush.assert_not_called()
    parent = 0xF00
    # Reading registered must not query Xlib or accept unobserved reparenting.
    assert not h.tray.registered
    probe.is_docked.assert_called_once_with(0xCAFE, 0xF00)
    h.clock.advance(1000)
    assert h.tray.registered
    h.flush.assert_called_once_with()
    h.banner.assert_not_called()


@pytest.mark.parametrize("windows", ["empty", "unrelated"])
def test_owner_without_native_tray_window_never_registers_and_warns_once(
    xembed: Harness, probe: Mock, native_window: Mock, windows: str
) -> None:
    from astra_voice.ui import tray as module

    if windows == "empty":
        vars(module)["QApplication"].topLevelWidgets.return_value = []
    else:
        native_window.objectName.return_value = "ordinary-window"
        native_window.metaObject.return_value.className.return_value = "QWidget"
    h = xembed
    h.tray.start()
    h.clock.advance(29_999)
    assert not h.tray.registered
    h.banner.assert_not_called()
    h.tray.start()  # Repeated start must retain the original deadline.
    h.clock.advance(1)
    assert not h.tray.registered
    h.banner.assert_called_once_with()
    h.clock.advance(60_000)
    assert not h.tray.registered
    h.banner.assert_called_once_with()
    h.flush.assert_not_called()
    probe.is_docked.assert_not_called()


def test_docking_loss_is_monitored_hides_and_retries_without_registered_reads(
    xembed: Harness, probe: Mock
) -> None:
    h = xembed
    h.tray.start()
    assert h.tray.registered
    h.icon.hide.reset_mock()
    h.icon.show.reset_mock()
    h.flush.reset_mock()
    probe.is_docked.reset_mock()
    probe.is_docked.return_value = False
    h.clock.advance(2000)
    assert h.icon.hide.called
    assert not h.tray.registered
    h.flush.assert_not_called()
    # A failed probe may close its connection: no second query in this callback.
    probe.is_docked.assert_called_once_with(0xCAFE, 0xF00)
    probe.is_docked.return_value = True
    h.clock.advance(999)
    probe.is_docked.assert_called_once_with(0xCAFE, 0xF00)
    assert not h.tray.registered
    h.clock.advance(1)
    assert h.tray.registered
    assert h.icon.show.called
    h.flush.assert_called_once_with()
    h.banner.assert_not_called()


@pytest.mark.parametrize("loss", ["visible", "geometry", "window_id"])
def test_native_window_local_evidence_invalidates_without_xlib(
    xembed: Harness, probe: Mock, native_window: Mock, loss: str
) -> None:
    h = xembed
    h.tray.start()
    assert h.tray.registered
    if loss == "visible":
        native_window.isVisible.side_effect = None
        native_window.isVisible.return_value = False
    elif loss == "geometry":
        native_window.geometry.side_effect = None
        native_window.geometry.return_value = QRect()
    else:
        native_window.winId.return_value = 0xCAFF
    probe.reset_mock()
    h.bus.transport.call.reset_mock()
    h.icon.hide.reset_mock()
    assert not h.tray.registered
    h.icon.hide.assert_called_once_with()
    probe.owner.assert_not_called()
    probe.is_docked.assert_not_called()
    h.bus.transport.call.assert_not_called()


def test_sni_appearance_cannot_override_previous_xembed_registration_proof(
    xembed: Harness, owner: Mock
) -> None:
    h = xembed
    h.tray.start()
    assert h.tray.registered
    h.bus.host_registered = True
    h.watcher.serviceRegistered.emit(SERVICE)
    assert h.tray.registered
    owner.reset_mock()
    owner.return_value = None
    # Qt may retain the generic XEmbed backend after discovering a new SNI host.
    h.icon.hide.side_effect = None
    h.icon.isVisible.return_value = True
    h.icon.geometry.return_value = QRect(0, 0, 16, 16)
    h.icon.hide.reset_mock()
    h.clock.advance(2000)
    assert owner.called
    assert not h.tray.registered
    assert h.icon.hide.called
    h.banner.assert_not_called()


def test_restart_keeps_xembed_local_proof_when_sni_host_appears(
    xembed: Harness, owner: Mock
) -> None:
    h = xembed
    h.tray.start()
    assert h.tray.registered
    h.tray.stop()
    h.bus.host_registered = True
    owner.return_value = None
    h.icon.isVisible.return_value = True
    h.icon.geometry.return_value = QRect(0, 0, 16, 16)
    h.tray.start()
    h.clock.advance(2000)
    assert not h.tray.registered
    h.banner.assert_not_called()


def test_stop_cancels_owner_monitoring_and_late_replies(xembed: Harness, owner: Mock) -> None:
    h = xembed
    h.tray.start()
    assert h.tray.registered
    h.bus.auto_reply = False
    h.watcher.serviceRegistered.emit(SERVICE)
    pending = h.bus.calls[-1]
    h.tray.stop()
    h.icon.show.reset_mock()
    owner.reset_mock()
    pending.reply(False)
    h.clock.advance(60_000)
    assert not h.tray.registered
    owner.assert_not_called()
    h.icon.show.assert_not_called()
    h.banner.assert_not_called()
    assert all(timer.due is None for timer in h.clock.timers)


@pytest.mark.parametrize(
    "geometry",
    [QRect(), QRect(0, 0, 0, 16), QRect(0, 0, 16, 0), QRect(0, 0, -1, 16)],
)
def test_empty_geometry_never_proves_xembed_registration(xembed: Harness, geometry: QRect) -> None:
    h = xembed
    h.icon.geometry.return_value = geometry
    h.tray.start()
    h.clock.advance(2000)
    assert h.icon.show.called
    assert not h.tray.registered
    h.flush.assert_not_called()


def test_null_local_icon_never_proves_xembed_registration(xembed: Harness) -> None:
    h = xembed
    local_icon = Mock(spec=QIcon)
    local_icon.isNull.return_value = True
    h.icon.icon.return_value = local_icon
    h.tray.start()
    h.clock.advance(2000)
    assert h.provider.has_icon.return_value is True
    assert not h.tray.registered
    h.flush.assert_not_called()


@pytest.mark.parametrize("loss", ["geometry", "local_icon", "visible", "provider_icon"])
def test_lost_local_registration_evidence_invalidates_xembed(xembed: Harness, loss: str) -> None:
    h = xembed
    h.tray.start()
    assert h.tray.registered
    if loss == "geometry":
        h.icon.geometry.return_value = QRect()
    elif loss == "local_icon":
        h.icon.icon.return_value.isNull.return_value = True
    elif loss == "visible":
        h.icon.isVisible.return_value = False
    else:
        h.provider.has_icon.return_value = False
    h.icon.hide.reset_mock()
    assert not h.tray.registered
    h.icon.hide.assert_called_once_with()


def test_path_and_owner_changes_do_not_extend_original_deadline(
    xembed: Harness, owner: Mock
) -> None:
    h = xembed
    owner.return_value = None
    h.tray.start()
    for index in range(1, 6):
        h.clock.advance(5000)
        owner.return_value = 0xF00 + index
        h.icon.geometry.return_value = QRect()
        h.watcher.serviceRegistered.emit(SERVICE)
        assert not h.tray.registered
        h.banner.assert_not_called()
    h.clock.advance(4999)
    h.banner.assert_not_called()
    h.clock.advance(1)
    h.banner.assert_called_once_with()


def test_late_negative_host_reply_cannot_restart_expired_registration(
    xembed: Harness, owner: Mock
) -> None:
    h = xembed
    h.bus.auto_reply = False
    h.tray.start()
    pending = h.bus.calls[-1]
    h.clock.advance(30_000)
    h.banner.assert_called_once_with()
    owner.assert_not_called()
    pending.reply(False)
    h.clock.advance(6000)
    assert not h.tray.registered
    h.icon.show.assert_not_called()
    owner.assert_not_called()
    h.banner.assert_called_once_with()


def test_path_info_is_logged_once_per_transition(
    xembed: Harness, owner: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    h = xembed
    owner.return_value = None
    caplog.set_level(logging.INFO, logger="astra_voice.ui.tray")

    def paths() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == "astra_voice.ui.tray"
            and record.getMessage().startswith("трей: путь ")
        ]

    h.tray.start()
    h.clock.advance(3000)
    assert paths() == ["трей: путь нет"]
    owner.return_value = 0xF00
    h.clock.advance(3000)
    assert h.tray.registered
    assert paths() == ["трей: путь нет", "трей: путь XEmbed"]
    h.clock.advance(6000)
    assert paths() == ["трей: путь нет", "трей: путь XEmbed"]
    h.bus.host_registered = True
    h.watcher.serviceRegistered.emit(SERVICE)
    h.clock.advance(4000)
    assert paths() == ["трей: путь нет", "трей: путь XEmbed", "трей: путь SNI"]
    owner.return_value = None
    h.bus.host_registered = False
    h.watcher.serviceUnregistered.emit(SERVICE)
    h.clock.advance(4000)
    assert paths() == ["трей: путь нет", "трей: путь XEmbed", "трей: путь SNI", "трей: путь нет"]
    assert all(
        record.levelno == logging.INFO
        for record in caplog.records
        if record.getMessage() in paths()
    )


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
    assert not port.is_docked(0xCAFE, 0xF00)
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
    assert not port.is_docked(0xCAFE, 0xF00)
    port.close()
    xlib.display.assert_not_called()


def test_probe_reuses_one_connection_for_owner_and_docking_and_closes(
    xlib: SimpleNamespace,
) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    port = XEmbedTrayProbe()
    xlib.display.assert_not_called()  # Construction is lazy.
    try:
        assert port.owner() == 0xF00
        for _ in range(3):
            assert port.owner() == 0xF00
            assert port.is_docked(0xCAFE, 0xF00)
        xlib.display.assert_called_once_with()
        xlib.conn.get_default_screen.assert_called()
        assert all(
            call.args == ("_NET_SYSTEM_TRAY_S2",) and call.kwargs == {"only_if_exists": True}
            for call in xlib.conn.intern_atom.call_args_list
        )
        xlib.conn.get_selection_owner.assert_called_with(42)
        xlib.conn.create_resource_object.assert_called_with("window", 0xCAFE)
        xlib.window.query_tree.assert_called()
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


@pytest.mark.parametrize("parent", [0, 1, 0xF00, 0xF02])
def test_probe_requires_reparenting_away_from_root(xlib: SimpleNamespace, parent: int) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    xlib.window.query_tree.return_value.parent = SimpleNamespace(id=parent)
    port = XEmbedTrayProbe()
    try:
        assert port.is_docked(0xCAFE, 0xF00) is (parent not in (0, 1))
    finally:
        port.close()


def test_probe_uses_the_windows_screen_root_to_reject_undocked_parent(
    xlib: SimpleNamespace,
) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    xlib.conn.screen.return_value = SimpleNamespace(root=SimpleNamespace(id=1))
    xlib.window.query_tree.return_value = SimpleNamespace(
        root=SimpleNamespace(id=2), parent=SimpleNamespace(id=2)
    )
    port = XEmbedTrayProbe()
    try:
        assert port.owner() == 0xF00
        assert not port.is_docked(0xCAFE, 0xF00)
    finally:
        port.close()


@pytest.mark.parametrize("race", ["before_query", "during_query"])
def test_probe_rejects_owner_race_even_with_docked_parent(xlib: SimpleNamespace, race: str) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    port = XEmbedTrayProbe()
    try:
        assert port.owner() == 0xF00
        if race == "before_query":
            xlib.conn.get_selection_owner.return_value = SimpleNamespace(id=0xF01)
        else:

            def replace_owner() -> SimpleNamespace:
                xlib.conn.get_selection_owner.return_value = SimpleNamespace(id=0xF01)
                return SimpleNamespace(parent=SimpleNamespace(id=0xF00), root=xlib.root)

            xlib.window.query_tree.side_effect = replace_owner
        assert not port.is_docked(0xCAFE, 0xF00)
    finally:
        port.close()


@pytest.mark.parametrize("operation", ["owner", "docking"])
def test_probe_disconnect_fails_closed_and_reconnects_next_call(
    xlib: SimpleNamespace, operation: str
) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    broken = xlib.conn
    replacement = Mock()
    replacement.screen.return_value = SimpleNamespace(root=xlib.root)
    replacement.get_default_screen.return_value = 2
    replacement.intern_atom.return_value = 42
    replacement.get_selection_owner.return_value = SimpleNamespace(id=0xF00)
    replacement.create_resource_object.return_value = xlib.window
    xlib.display.side_effect = [broken, replacement]
    port = XEmbedTrayProbe()
    assert port.owner() == 0xF00
    if operation == "owner":
        broken.get_selection_owner.side_effect = OSError("fake disconnect")
        assert port.owner() is None
    else:
        xlib.window.query_tree.side_effect = OSError("fake disconnect")
        assert not port.is_docked(0xCAFE, 0xF00)
        xlib.window.query_tree.side_effect = None
    broken.close.assert_called_once_with()
    assert port.owner() == 0xF00
    assert port.is_docked(0xCAFE, 0xF00)
    assert xlib.display.call_count == 2
    port.close()
    replacement.close.assert_called_once_with()


@pytest.mark.parametrize("operation", ["owner", "docking"])
def test_probe_error_handler_fails_closed_without_stderr(
    xlib: SimpleNamespace, capsys: pytest.CaptureFixture[str], operation: str
) -> None:
    from astra_voice.platform.x11 import XEmbedTrayProbe

    port = XEmbedTrayProbe()
    assert port.owner() == 0xF00
    handler = xlib.conn.set_error_handler.call_args.args[0]

    def async_error() -> None:
        handler(OSError("fake BadWindow"), None)

    xlib.conn.sync.side_effect = async_error
    if operation == "owner":
        assert port.owner() is None
    else:
        assert not port.is_docked(0xCAFE, 0xF00)
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
        assert not port.is_docked(0xCAFE, 0xF00)
    else:
        assert port.owner() == 0xF00
    port.close()


def test_stop_closes_injected_probe(xembed: Harness, probe: Mock) -> None:
    xembed.tray.start()
    assert xembed.tray.registered
    xembed.tray.stop()
    probe.close.assert_called_once_with()
