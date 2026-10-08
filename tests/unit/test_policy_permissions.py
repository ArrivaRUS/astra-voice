"""MN-18: policy permissions are diagnostic and belong to the opened file."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import IO, Any, cast

import pytest

from astra_voice.core import policy as pol
from astra_voice.core.settings import Settings
from astra_voice.net.gate import NetworkGate

pytestmark = pytest.mark.unit
BODY = "[astra-voice]\nlanguage=en\nlocked=model_id\noffline=true\nappimage=deny\n"


def _metadata(info: os.stat_result, uid: int, mode: int) -> os.stat_result:
    values = list(info)
    values[4] = uid
    values[0] = stat.S_IFREG | mode
    return os.stat_result(values)


def _file(tmp_path: Path, body: str = BODY) -> Path:
    path = tmp_path / "policy.conf"
    path.write_text(body, encoding="utf-8")
    return path


def _owner_mode(monkeypatch: pytest.MonkeyPatch, path: Path, uid: int, mode: int) -> None:
    inode = path.stat().st_ino
    fstat = os.fstat

    def metadata(fd: int) -> os.stat_result:
        info = fstat(fd)
        return _metadata(info, uid, mode) if info.st_ino == inode else info

    monkeypatch.setattr(os, "fstat", metadata)


@pytest.mark.parametrize(
    ("uid", "mode", "warn"),
    [
        (0, 0o600, False),
        (0, 0o644, False),
        (0, 0o444, False),
        (0, 0o640, False),
        (1000, 0o600, True),
        (0, 0o620, True),
        (0, 0o602, True),
        (0, 0o666, True),
    ],
)
def test_permissions_do_not_change_policy_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    uid: int,
    mode: int,
    warn: bool,
) -> None:
    path = _file(tmp_path)
    before = path.read_bytes(), path.stat().st_mode
    _owner_mode(monkeypatch, path, uid, mode)
    loaded = pol.load(path)
    assert loaded.status is pol.PolicyStatus.OK
    assert loaded.values == {"language": "en", "offline": True, "appimage": "deny"}
    assert loaded.locked_keys == {"language", "offline", "appimage", "model_id"}
    assert bool(loaded.permissions_warning) is warn
    settings = pol.effective(Settings(), loaded)
    assert settings.language == "en" and settings.offline is True
    assert NetworkGate(settings, loaded).refusal("download") == "admin"
    assert pol.appimage_denied(loaded)
    records = [record for record in caplog.records if "небезопасные права" in record.message]
    assert bool(records) is warn
    assert all(record.levelname == "WARNING" for record in records)
    assert (path.read_bytes(), path.stat().st_mode) == before


def test_warning_does_not_introduce_offline_or_appimage_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _file(tmp_path, "[astra-voice]\noffline=false\nappimage=allow\n")
    _owner_mode(monkeypatch, path, 1000, 0o666)
    loaded = pol.load(path)
    assert loaded.permissions_warning
    assert "offline" not in loaded.values and not loaded.is_locked("offline")
    assert not pol.appimage_denied(loaded)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    assert NetworkGate(Settings(), loaded).allowed("download") == (True, "")


@pytest.mark.parametrize("body", [b"[broken", b"", b"\xff", b"[astra-voice]\nautostart=oops\n"])
def test_invalid_content_retains_permission_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    path = _file(tmp_path)
    path.write_bytes(body)
    _owner_mode(monkeypatch, path, 1000, 0o644)
    loaded = pol.load(path)
    assert loaded.status is pol.PolicyStatus.INVALID
    assert loaded.values == {} and not loaded.locked_keys
    assert loaded.permissions_warning
    assert NetworkGate(Settings(), loaded).refusal("download") == "admin"
    assert not pol.appimage_denied(loaded)


def test_absent_policy_has_no_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    assert pol.load(tmp_path / "absent.conf") == pol.Policy()
    assert not caplog.records


def test_failed_open_remains_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _file(tmp_path)
    original_open = Path.open

    def fail_open(target: Path, *args: Any, **kwargs: Any) -> IO[str]:
        if target == path:
            raise PermissionError("fixture unreadable")
        return cast(IO[str], original_open(target, *args, **kwargs))

    monkeypatch.setattr(Path, "open", fail_open)
    loaded = pol.load(path)
    assert loaded.status is pol.PolicyStatus.INVALID
    assert loaded.values == {} and not loaded.locked_keys


def test_diagnostic_failure_does_not_discard_readable_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _file(tmp_path)
    inode = path.stat().st_ino
    fstat = os.fstat

    def fail_metadata(fd: int) -> os.stat_result:
        info = fstat(fd)
        if info.st_ino == inode:
            raise OSError("fixture metadata failure")
        return info

    monkeypatch.setattr(os, "fstat", fail_metadata)
    loaded = pol.load(path)
    assert loaded.status is pol.PolicyStatus.OK and pol.appimage_denied(loaded)
    assert "Не удалось проверить" in loaded.permissions_warning


def test_replaced_path_does_not_replace_permissions_or_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _file(tmp_path)
    replacement = tmp_path / "replacement"
    replacement.write_text("[astra-voice]\nappimage=allow\n")
    inode = path.stat().st_ino
    fstat = os.fstat

    def replace_after_open(fd: int) -> os.stat_result:
        info = fstat(fd)
        if info.st_ino == inode:
            replacement.replace(path)
            return _metadata(info, 1000, 0o666)
        return _metadata(info, 0, 0o644)

    monkeypatch.setattr(os, "fstat", replace_after_open)
    loaded = pol.load(path)
    assert loaded.permissions_warning
    assert pol.appimage_denied(loaded)
    assert "appimage=allow" in path.read_text()


def test_about_line_preserves_status_and_shows_warning() -> None:
    # Evaluate only the existing JS function: no QML window, GUI or app startup.
    from PyQt5.QtQml import QJSEngine

    from astra_voice.app import _make_app_info
    from astra_voice.platform.session import SessionKind
    from helpers.qt_app import get_qapplication

    app = get_qapplication()
    assert app is not None
    warning = "Небезопасные права файла правил: обратитесь к администратору."
    info = _make_app_info(SessionKind.KDE, "ok", policy_warning=warning)
    assert info.policyStatus == "ok" and info.policyWarning == warning
    assert _make_app_info(SessionKind.KDE, "ok").policyWarning == ""
    text = (Path(__file__).resolve().parents[2] / "qml/sections/About.qml").read_text()
    function = re.search(r"    function policyLine\(\) \{.*?\n    \}", text, re.DOTALL)
    assert function is not None
    engine = QJSEngine()
    for state, prefix in (("ok", "Применены:"), ("invalid", "Не применены:")):
        root = json.dumps({"policyStatus": state, "policyWarning": warning})
        result = engine.evaluate(
            "var root = "
            + root
            + "; function qsTr(text) { return text; }\n"
            + function[0]
            + "\npolicyLine()"
        )
        assert not result.isError()
        assert result.toString().startswith(prefix)
        assert result.toString().endswith(warning)
