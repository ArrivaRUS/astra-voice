"""MN-18: permissions diagnostics preserve decisions and reach the About row."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import IO, Any, cast
from unittest.mock import Mock

import pytest
from test_app_shutdown import Rig
from test_app_shutdown import rig as rig

from astra_voice.core import policy as pol
from astra_voice.core.settings import Settings
from astra_voice.net.gate import NetworkGate

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]
ALLOW = "[astra-voice]\noffline=false\nappimage=allow\nlocked=language\n"
DENY = "[astra-voice]\noffline=true\nappimage=deny\nlocked=language\n"


def metadata_for(monkeypatch: pytest.MonkeyPatch, path: Path, uid: int, mode: int) -> list[int]:
    """Simulate ownership only for this fixture's inode, never chown real files."""
    original = os.fstat
    identity = path.stat().st_dev, path.stat().st_ino
    descriptors: list[int] = []

    def fstat(fd: int) -> os.stat_result:
        info = original(fd)
        if (info.st_dev, info.st_ino) == identity:
            descriptors.append(fd)
            fields = list(info)
            fields[0] = stat.S_IFREG | mode
            fields[4] = uid
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "fstat", fstat)
    return descriptors


@pytest.mark.parametrize("body", [ALLOW, DENY], ids=["allow", "deny"])
@pytest.mark.parametrize(
    ("uid", "mode", "warn"),
    [(0, 0o644, False), (12345, 0o644, True), (0, 0o664, True), (0, 0o666, True)],
)
def test_warning_preserves_both_allowing_and_denying_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    body: str,
    uid: int,
    mode: int,
    warn: bool,
) -> None:
    path = tmp_path / "rules.conf"
    path.write_text(body)
    metadata_for(monkeypatch, path, uid, mode)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    loaded = pol.load(path)
    restrictive = body == DENY
    assert loaded.status is pol.PolicyStatus.OK
    assert loaded.values == (
        {"offline": True, "appimage": "deny"} if restrictive else {"appimage": "allow"}
    )
    assert loaded.locked_keys == (
        {"offline", "appimage", "language"} if restrictive else {"appimage", "language"}
    )
    assert pol.appimage_denied(loaded) is restrictive
    effective = pol.effective(Settings(language="en"), loaded)
    assert effective.language == "en" and effective.offline is restrictive
    assert NetworkGate(effective, loaded).allowed("download")[0] is not restrictive
    assert bool(getattr(loaded, "permissions_warning", "")) is warn
    assert any(record.levelname == "WARNING" for record in caplog.records) is warn


@pytest.mark.parametrize("unsafe_opened_file", [False, True])
def test_path_replacement_before_metadata_uses_opened_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe_opened_file: bool
) -> None:
    path = tmp_path / "rules.conf"
    path.write_text(DENY)
    replacement = tmp_path / "new.conf"
    replacement.write_text(ALLOW)
    original_open = Path.open
    original_fstat = os.fstat
    old_identity = path.stat().st_dev, path.stat().st_ino
    opened: list[int] = []
    checked: list[int] = []

    def replace_after_open(target: Path, *args: Any, **kwargs: Any) -> IO[Any]:
        stream = cast(IO[Any], original_open(target, *args, **kwargs))
        if target == path:
            opened.append(stream.fileno())
            replacement.replace(path)
        return stream

    def fstat(fd: int) -> os.stat_result:
        info = original_fstat(fd)
        fields = list(info)
        old = (info.st_dev, info.st_ino) == old_identity
        fields[0] = stat.S_IFREG | 0o644
        fields[4] = 12345 if old == unsafe_opened_file else 0
        if old:
            checked.append(fd)
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "open", replace_after_open)
    monkeypatch.setattr(os, "fstat", fstat)
    loaded = pol.load(path)
    assert opened == checked and len(opened) == 1
    assert loaded.status is pol.PolicyStatus.OK and pol.appimage_denied(loaded)
    assert loaded.values["offline"] is True
    assert bool(getattr(loaded, "permissions_warning", "")) is unsafe_opened_file
    with original_open(path) as current:
        assert current.read() == ALLOW


@pytest.mark.parametrize("invalid", [False, True])
def test_main_hands_existing_diagnostic_to_info_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, invalid: bool
) -> None:
    from astra_voice import app as app_mod

    warning = "Fixture permissions warning"
    loaded = pol.Policy(
        status=pol.PolicyStatus.INVALID if invalid else pol.PolicyStatus.OK,
        permissions_warning=warning,
    )
    load = Mock(return_value=loaded)
    monkeypatch.setattr(pol, "load", load)
    assert app_mod.main([]) == 7
    load.assert_called_once_with()
    info = rig.load_qml.call_args.args[0]
    assert info.policyStatus == loaded.status.value
    assert info.policyWarning == warning


@pytest.mark.parametrize(
    ("body", "uid", "status", "prefix", "warn"),
    [
        (ALLOW, 12345, "ok", "Применены:", True),
        ("[broken", 12345, "invalid", "Не применены:", True),
        (ALLOW, 0, "ok", "Применены:", False),
        (None, 0, "absent", "Не заданы", False),
    ],
)
def test_about_existing_row_uses_loaded_diagnostic_without_rereading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str | None,
    uid: int,
    status: str,
    prefix: str,
    warn: bool,
) -> None:
    from PyQt5.QtCore import QObject, QUrl
    from PyQt5.QtQml import QQmlComponent, QQmlEngine

    from astra_voice.app import _make_app_info
    from astra_voice.platform.session import SessionKind
    from helpers.qt_app import get_qapplication

    path = tmp_path / "rules.conf"
    if body is not None:
        path.write_text(body)
        metadata_for(monkeypatch, path, uid, 0o644)
    loaded = pol.load(path)
    warning = loaded.permissions_warning
    assert loaded.status.value == status and bool(warning) is warn
    original_open = Path.open

    def no_policy_reopen(target: Path, *args: Any, **kwargs: Any) -> IO[Any]:
        assert target != path, "About must use the policy already loaded at startup"
        return cast(IO[Any], original_open(target, *args, **kwargs))

    monkeypatch.setattr(Path, "open", no_policy_reopen)
    monkeypatch.setattr(pol, "load", Mock(side_effect=AssertionError("unexpected policy reload")))
    app = get_qapplication()
    assert app is not None
    info = _make_app_info(SessionKind.KDE, status, policy_warning=warning)
    engine = QQmlEngine()
    engine.rootContext().setContextProperty("appInfo", info)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(REPO / "qml/sections/About.qml")))
    root = component.create()
    assert root is not None, [error.toString() for error in component.errors()]
    assert root.property("policyWarning") == warning
    rows = [
        child
        for child in root.findChildren(QObject)
        if child.property("label") == "Правила администратора"
    ]
    assert len(rows) == 1
    line = cast(str, rows[0].property("sub"))
    assert line.startswith(prefix)
    if warn:
        assert line.endswith(warning) and line.count(warning) == 1
    else:
        assert "права" not in line
    root.deleteLater()
    app.processEvents()
