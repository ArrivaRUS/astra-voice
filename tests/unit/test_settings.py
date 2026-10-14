"""Настройки: дефолты, права 0600, атомарная запись, миграции, порча файла."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, Mock, call

import pytest

from astra_voice.core import settings as st

pytestmark = pytest.mark.unit


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


@pytest.mark.parametrize("appimage", [False, True], ids=["deb", "appimage"])
def test_defaults_when_file_absent(path: Path, appimage: bool) -> None:
    s = st.load(path, appimage=appimage)
    assert s.hotkey == "Ctrl+Space"
    assert s.hotkey_mode == "ptt"
    assert s.pill_enabled is True
    assert s.autostart is True
    assert s.model_id is None
    assert s.language == "ru"
    assert s.check_app_updates is appimage
    assert s.check_model_updates is appimage
    assert s.extra == ({"update_defaults_migrated": True} if appimage else {})
    assert not path.exists()


def test_dataclass_defaults_do_not_enable_network() -> None:
    assert st.Settings().check_app_updates is st.Settings().check_model_updates is False


@pytest.mark.parametrize("appimage", [False, True], ids=["deb", "appimage"])
def test_load_uses_existing_install_kind(
    path: Path, appimage: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astra_voice.core import paths

    detect = Mock(return_value=SimpleNamespace(is_appimage=appimage))
    monkeypatch.setattr(paths, "install_kind", detect)
    loaded = st.load(path)
    assert loaded.check_app_updates is loaded.check_model_updates is appimage
    detect.assert_called_once_with()
    st.load(path, appimage=not appimage)
    detect.assert_called_once_with()  # Внедрённый трек не читает файловую систему.


@pytest.mark.parametrize("appimage", [False, True], ids=["deb", "appimage"])
@pytest.mark.parametrize("contents", ["{", "[]", '{"schema_version": 2}'])
def test_fallback_defaults_follow_track(path: Path, appimage: bool, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    loaded = st.load(path, appimage=appimage)
    assert loaded.check_app_updates is loaded.check_model_updates is appimage
    assert loaded.extra == ({"update_defaults_migrated": True} if appimage else {})


@pytest.mark.parametrize("appimage", [False, True], ids=["deb", "appimage"])
def test_unreadable_file_defaults_follow_track(path: Path, appimage: bool) -> None:
    path.mkdir()
    loaded = st.load(path, appimage=appimage)
    assert loaded.check_app_updates is loaded.check_model_updates is appimage


@pytest.mark.parametrize("appimage", [False, True], ids=["deb", "appimage"])
@pytest.mark.parametrize("locked", [("check_app_updates",), ("check_model_updates",)])
def test_fresh_defaults_respect_explicit_policy_locks(
    path: Path, appimage: bool, locked: tuple[str, ...]
) -> None:
    loaded = st.load(path, appimage=appimage, locked=locked)
    for key in ("check_app_updates", "check_model_updates"):
        assert getattr(loaded, key) is (appimage and key not in locked)


def test_save_creates_0600_and_roundtrips(path: Path) -> None:
    s = st.load(path)
    s.hotkey = "Ctrl+Alt+D"
    s.model_id = "gigaam-v3-rnnt"
    st.save(s, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == st.SCHEMA_VERSION
    again = st.load(path)
    assert again.hotkey == "Ctrl+Alt+D"
    assert again.model_id == "gigaam-v3-rnnt"


def test_save_leaves_no_temp_file(path: Path) -> None:
    st.save(st.Settings(), path)
    assert [p.name for p in path.parent.iterdir()] == ["settings.json"]


def test_save_syncs_directory_after_replace(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path.write_text("old settings", encoding="utf-8")
    path.chmod(0o644)
    operations = Mock()
    for name in ("open", "fsync", "replace", "close"):
        spy = Mock(wraps=getattr(os, name))
        operations.attach_mock(spy, name)
        monkeypatch.setattr(os, name, spy)
    settings = st.Settings(hotkey="Ctrl+Alt+D", extra={"device": "Микрофон"})

    st.save(settings, path)

    directory_fd = operations.fsync.call_args.args[0]
    assert operations.mock_calls == [
        call.open(
            path.with_name(".settings.json.tmp"),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            0o600,
        ),
        call.fsync(ANY),
        call.replace(path.with_name(".settings.json.tmp"), path),
        call.open(path.parent, os.O_RDONLY | os.O_DIRECTORY),
        call.fsync(directory_fd),
        call.close(directory_fd),
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == settings.to_dict()
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("stage", ["chmod", "replace", "fsync"])
def test_save_failure_cleans_temp_file(
    path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    st.save(st.Settings(hotkey="Ctrl+Q"), path)
    monkeypatch.setattr(os, stage, Mock(side_effect=OSError("save failed")))

    with pytest.raises(OSError, match="save failed"):
        st.save(st.Settings(hotkey="Ctrl+W"), path)

    assert list(path.parent.iterdir()) == [path]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert st.load(path).hotkey == "Ctrl+Q"


@pytest.mark.parametrize("stage", ["directory-open", "directory-fsync"])
def test_save_directory_failure_warns_after_successful_replace(
    path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, stage: str
) -> None:
    st.save(st.Settings(hotkey="Ctrl+Q"), path)
    error = OSError("save failed")
    close = Mock(wraps=os.close)
    monkeypatch.setattr(os, "close", close)
    if stage == "directory-open":
        temporary_fd = os.open(
            path.with_name(".settings.json.tmp"), os.O_WRONLY | os.O_CREAT, 0o600
        )
        monkeypatch.setattr(os, "open", Mock(side_effect=[temporary_fd, error]))
    else:
        monkeypatch.setattr(os, "fsync", Mock(side_effect=[None, error]))

    with caplog.at_level("WARNING", logger=st.__name__):
        st.save(st.Settings(hotkey="Ctrl+W"), path)

    assert list(path.parent.iterdir()) == [path]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert st.load(path).hotkey == "Ctrl+W"
    assert any(
        record.name == st.__name__
        and record.levelname == "WARNING"
        and "save failed" in record.getMessage()
        and str(path.parent) in record.getMessage()
        for record in caplog.records
    )
    if stage == "directory-fsync":
        close.assert_called_once()
        with pytest.raises(OSError):
            os.fstat(close.call_args.args[0])


def test_broken_json_is_quarantined(path: Path) -> None:
    path.write_text("{это не json", encoding="utf-8")
    s = st.load(path)
    assert s.hotkey == "Ctrl+Space"
    backups = list(path.parent.glob("settings.json.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{это не json"
    assert not path.exists()


@pytest.mark.parametrize("raw", ["{это не json", "[1, 2, 3]"])
@pytest.mark.parametrize("appimage", [False, True], ids=["deb", "appimage"])
def test_broken_settings_are_not_quarantined_when_disabled(
    path: Path, raw: str, appimage: bool
) -> None:
    path.write_text(raw, encoding="utf-8")
    assert st.load(path, quarantine=False, appimage=appimage) == st.defaults(appimage=appimage)
    assert path.read_text(encoding="utf-8") == raw
    assert not list(path.parent.glob("settings.json.bak-*"))


def test_non_object_json_is_quarantined(path: Path) -> None:
    path.write_text("[1, 2, 3]", encoding="utf-8")
    st.load(path)
    assert list(path.parent.glob("settings.json.bak-*"))


def test_unknown_keys_are_preserved(path: Path) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "будущий_ключ": 5, "hotkey": "Ctrl+Q"}),
        encoding="utf-8",
    )
    s = st.load(path)
    assert s.extra["будущий_ключ"] == 5
    st.save(s, path)
    assert json.loads(path.read_text(encoding="utf-8"))["будущий_ключ"] == 5


def test_bad_types_fall_back_to_defaults(path: Path) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "pill_enabled": "да", "hotkey": 42, "model_id": 7}),
        encoding="utf-8",
    )
    s = st.load(path)
    assert s.pill_enabled is True
    assert s.hotkey == "Ctrl+Space"
    assert s.model_id is None


@pytest.mark.parametrize(
    "combo",
    [
        "Ctrl+Space",
        "Ctrl+Shift+Space",
        "Ctrl+Alt+D",
        " cTrL + A ",
        " CONTROL + A ",
        " aLt + A ",
        " MeTa + A ",
        " SuPeR + A ",
        " WIN + A ",
        "Ctrl++Space",
        "+Ctrl+Space+",
        "Ctrl+Ctrl+Space",
    ],
)
def test_valid_combo_preserves_gui_validation_rules(combo: str) -> None:
    assert st.is_valid_combo(combo)
    assert st.from_dict({"hotkey": combo}).hotkey == combo


@pytest.mark.parametrize(
    "combo",
    [
        "Space",
        "Return",
        "A",
        "Shift+Space",
        "",
        "unknown",
        "  sHiFt + A  ",
        "ControlKey+A",
        "Ctrl",
        "Ctrl+Shift",
        "Alt+Super",
        "Ctrl+A+B",
    ],
)
@pytest.mark.parametrize("source", ["file", "dict"])
def test_invalid_hotkey_falls_back_with_warning(
    path: Path, caplog: pytest.LogCaptureFixture, combo: str, source: str
) -> None:
    data = {"schema_version": st.SCHEMA_VERSION, "hotkey": combo, "hotkey_mode": "toggle"}
    assert not st.is_valid_combo(combo)

    with caplog.at_level("WARNING", logger=st.__name__):
        if source == "file":
            path.write_text(json.dumps(data), encoding="utf-8")
            settings = st.load(path)
        else:
            settings = st.from_dict(data)

    assert settings.hotkey == st.Settings.hotkey == "Ctrl+Space"
    assert settings.hotkey_mode == "toggle"
    assert any(
        record.name == st.__name__
        and record.levelname == "WARNING"
        and "hotkey" in record.getMessage()
        and repr(combo) in record.getMessage()
        for record in caplog.records
    )


def test_unknown_hotkey_mode_falls_back(path: Path) -> None:
    path.write_text(json.dumps({"schema_version": 1, "hotkey_mode": "тыр-пыр"}), encoding="utf-8")
    assert st.load(path).hotkey_mode == "ptt"


def test_forward_migration_runs(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def v0_to_v1(data: dict[str, Any]) -> dict[str, Any]:
        data["hotkey"] = data.pop("shortcut", "Ctrl+Space")
        return data

    monkeypatch.setattr(st, "MIGRATIONS", {0: v0_to_v1})
    path.write_text(json.dumps({"schema_version": 0, "shortcut": "Ctrl+Shift+V"}), encoding="utf-8")
    s = st.load(path)
    assert s.hotkey == "Ctrl+Shift+V"
    assert "shortcut" not in s.extra


def test_missing_schema_version_is_zero(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def v0_to_v1(data: dict[str, Any]) -> dict[str, Any]:
        calls.append(0)
        return data

    monkeypatch.setattr(st, "MIGRATIONS", {0: v0_to_v1})
    path.write_text(json.dumps({"hotkey": "Ctrl+Space"}), encoding="utf-8")
    st.load(path)
    assert calls == [0]


def test_future_schema_version_kept(path: Path) -> None:
    path.write_text(json.dumps({"schema_version": 99, "hotkey": "Ctrl+J"}), encoding="utf-8")
    assert st.load(path).hotkey == "Ctrl+J"


@pytest.mark.parametrize("schema", [None, 0, 1, 2])
@pytest.mark.parametrize("appimage", [False, True], ids=["deb", "appimage"])
def test_legacy_update_preferences_migrate_once(
    path: Path, schema: int | None, appimage: bool
) -> None:
    """Только AppImage включает старое off; маркер сохраняет следующий выбор."""
    data = {"check_app_updates": False, "check_model_updates": False, "device": "microphone"}
    if schema is not None:
        data["schema_version"] = schema
    path.write_text(json.dumps(data), encoding="utf-8")
    migrated = st.load(path, appimage=appimage)
    assert migrated.check_app_updates is migrated.check_model_updates is appimage
    assert migrated.extra["device"] == "microphone"
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] > 1
    st.save(migrated, path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] > 1
    assert saved.get("update_defaults_migrated") is (True if appimage else None)
    migrated.check_app_updates = migrated.check_model_updates = False
    st.save(migrated, path)
    again = st.load(path, appimage=appimage)
    assert again.check_app_updates is again.check_model_updates is False
    assert again.extra["device"] == "microphone"


@pytest.mark.parametrize("schema", [1, 2])
@pytest.mark.parametrize("appimage", [False, True], ids=["deb", "appimage"])
def test_marker_preserves_disabled_updates_after_schema_roundtrip(
    path: Path, schema: int, appimage: bool
) -> None:
    st.save(
        st.Settings(
            check_app_updates=False,
            check_model_updates=False,
            extra={"update_defaults_migrated": True, "future_key": {"value": 5}},
        ),
        path,
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = schema
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = st.load(path, appimage=appimage)

    assert loaded.check_app_updates is loaded.check_model_updates is False
    assert loaded.extra["update_defaults_migrated"] is True
    assert loaded.extra["future_key"] == {"value": 5}
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["update_defaults_migrated"] is True
    assert saved["check_app_updates"] is saved["check_model_updates"] is False


def test_fresh_deb_then_appimage_migrates_and_saves_marker(path: Path) -> None:
    deb = st.load(path, appimage=False)
    st.save(deb, path)
    saved_deb = json.loads(path.read_text(encoding="utf-8"))
    assert saved_deb["schema_version"] == 2
    assert "update_defaults_migrated" not in saved_deb
    assert saved_deb["check_app_updates"] is saved_deb["check_model_updates"] is False

    appimage = st.load(path, appimage=True)
    assert appimage.check_app_updates is appimage.check_model_updates is True
    saved_appimage = json.loads(path.read_text(encoding="utf-8"))
    assert saved_appimage["update_defaults_migrated"] is True
    assert saved_appimage["check_app_updates"] is saved_appimage["check_model_updates"] is True
    appimage.check_app_updates = appimage.check_model_updates = False
    st.save(appimage, path)

    assert st.load(path, appimage=True).check_app_updates is False
    assert st.load(path, appimage=True).check_model_updates is False


@pytest.mark.parametrize("appimage", [False, True], ids=["deb", "appimage"])
def test_marker_and_unknown_keys_roundtrip(path: Path, appimage: bool) -> None:
    data = {
        "update_defaults_migrated": True,
        "future_key": ["value", {"nested": False}],
        "check_app_updates": False,
        "check_model_updates": False,
    }
    settings = st.from_dict(data, appimage=appimage)
    assert settings.extra == {
        "update_defaults_migrated": True,
        "future_key": ["value", {"nested": False}],
    }
    for key, value in data.items():
        assert settings.to_dict()[key] == value
    st.save(settings, path)
    assert st.load(path, appimage=appimage) == settings


@pytest.mark.parametrize("schema", [1, 2])
def test_read_only_load_does_not_save_schema_or_marker(path: Path, schema: int) -> None:
    raw = json.dumps(
        {"schema_version": schema, "check_app_updates": False, "check_model_updates": False}
    )
    path.write_text(raw, encoding="utf-8")
    loaded = st.load(path, appimage=True, quarantine=False)
    assert loaded.check_app_updates is loaded.check_model_updates is True
    assert loaded.extra["update_defaults_migrated"] is True
    assert path.read_text(encoding="utf-8") == raw
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("schema", [1, 2])
@pytest.mark.parametrize("stored", [None, False, True], ids=["absent", "off", "on"])
def test_appimage_migration_preserves_locked_value(
    path: Path, schema: int, stored: bool | None
) -> None:
    data: dict[str, Any] = {"schema_version": schema, "check_model_updates": False}
    if stored is not None:
        data["check_app_updates"] = stored
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = st.load(path, appimage=True, locked=("check_app_updates",))

    assert loaded.check_app_updates is (stored is True)
    assert loaded.check_model_updates is True
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["update_defaults_migrated"] is True
    assert saved["check_app_updates"] is (stored is True)


@pytest.mark.parametrize("app,models", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("appimage", [False, True], ids=["deb", "appimage"])
def test_legacy_individual_update_choices_migrate_to_enabled(
    path: Path, app: bool, models: bool, appimage: bool
) -> None:
    path.write_text(
        json.dumps({"schema_version": 1, "check_app_updates": app, "check_model_updates": models}),
        encoding="utf-8",
    )
    loaded = st.load(path, appimage=appimage)
    assert loaded.check_app_updates is (appimage or app)
    assert loaded.check_model_updates is (appimage or models)


@pytest.mark.parametrize(
    "values",
    [
        {"check_app_updates": False, "check_model_updates": False},
        {"offline": True},
        {"profile": "secure"},
    ],
)
@pytest.mark.parametrize("appimage", [False, True], ids=["deb", "appimage"])
def test_migrated_defaults_respect_administrator_network_limits(
    path: Path, values: dict[str, Any], monkeypatch: pytest.MonkeyPatch, appimage: bool
) -> None:
    from astra_voice.core.policy import Policy, effective
    from astra_voice.net.gate import NetworkGate

    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    path.write_text(
        json.dumps({"schema_version": 1, "check_app_updates": False, "check_model_updates": False}),
        encoding="utf-8",
    )
    policy = Policy(values=values, locked_keys=frozenset(values))
    locked = policy.locked_keys
    if NetworkGate(st.Settings(), policy).refusal("check_app_manual") == "admin":
        locked |= {"check_app_updates", "check_model_updates"}
    loaded = st.load(path, appimage=appimage, locked=locked)
    assert loaded.check_app_updates is loaded.check_model_updates is False
    configured = effective(loaded, policy)
    gate = NetworkGate(configured, policy)
    for kind in ("check_app", "check_app_manual", "check_models"):
        assert gate.allowed(kind)[0] is False
    assert loaded.check_app_updates is loaded.check_model_updates is False


@pytest.mark.parametrize(
    "locked",
    [
        ("check_app_updates",),
        ("check_model_updates",),
        ("check_app_updates", "check_model_updates"),
    ],
)
@pytest.mark.parametrize("appimage", [False, True], ids=["deb", "appimage"])
@pytest.mark.parametrize("schema", [1, 2])
def test_legacy_locked_update_preferences_are_not_enabled(
    path: Path,
    locked: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
    appimage: bool,
    schema: int,
) -> None:
    from astra_voice.core.policy import Policy, effective
    from astra_voice.net.gate import NetworkGate

    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    path.write_text(
        json.dumps(
            {"schema_version": schema, "check_app_updates": False, "check_model_updates": False}
        ),
        encoding="utf-8",
    )
    policy = Policy(locked_keys=frozenset(locked))
    loaded = st.load(path, appimage=appimage, locked=policy.locked_keys)
    for field in ("check_app_updates", "check_model_updates"):
        assert getattr(loaded, field) is (appimage and field not in locked)
    gate = NetworkGate(effective(loaded, policy), policy)
    if "check_app_updates" in locked:
        assert gate.refusal("check_app") == gate.refusal("check_app_manual") == "policy"
    if "check_model_updates" in locked:
        assert gate.allowed("check_models")[0] is False
    st.save(loaded, path)
    again = st.load(path, appimage=appimage, locked=policy.locked_keys)
    assert (again.check_app_updates, again.check_model_updates) == (
        loaded.check_app_updates,
        loaded.check_model_updates,
    )


def test_device_label_is_optional_without_schema_or_migration_change() -> None:
    legacy = st.from_dict({"schema_version": 2, "device": "legacy-mic"})
    assert legacy.device_label is None and legacy.extra["device"] == "legacy-mic"
    labelled = st.from_dict({"schema_version": 2, "device": "usb", "device_label": "USB-гарнитура"})
    assert labelled.device_label == "USB-гарнитура"
    assert labelled.to_dict()["device_label"] == "USB-гарнитура"
    assert labelled.to_dict()["schema_version"] == st.SCHEMA_VERSION == 2
    assert st.from_dict(labelled.to_dict()).to_dict() == labelled.to_dict()


@pytest.mark.parametrize("value", [False, 1, [], {}])
def test_device_label_rejects_non_string(value: object) -> None:
    assert st.from_dict({"device_label": value}).device_label is None
