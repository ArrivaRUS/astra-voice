"""Политика: таблица истинности absent/ok/invalid, блокировки, наложение."""

from __future__ import annotations

from pathlib import Path

import pytest

from astra_voice.core import policy as pol
from astra_voice.core.settings import Settings

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "policy.conf"
    path.write_text(text, encoding="utf-8")
    return path


def test_absent_file(tmp_path: Path) -> None:
    p = pol.load(tmp_path / "нет-такого.conf")
    assert p.status is pol.PolicyStatus.ABSENT
    assert p.values == {}
    assert p.locked_keys == frozenset()


def test_valid_policy_parsed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "[astra-voice]\ncheck_app_updates = no\nhotkey = Ctrl+F9\nupdates = admin\n",
    )
    p = pol.load(path)
    assert p.status is pol.PolicyStatus.OK
    assert p.values == {"check_app_updates": False, "hotkey": "Ctrl+F9", "updates": "admin"}
    assert p.locked_keys == frozenset({"check_app_updates", "hotkey", "updates"})
    assert p.is_locked("hotkey") and not p.is_locked("language")


@pytest.mark.parametrize(
    "text",
    [
        "не ini вовсе\n",
        "[astra-voice]\ncheck_app_updates = ага\n",
        "[другая-секция]\n",
    ],
)
def test_invalid_policy_behaves_as_absent(tmp_path: Path, text: str) -> None:
    p = pol.load(_write(tmp_path, text))
    assert p.status is pol.PolicyStatus.INVALID
    assert p.values == {}
    assert p.locked_keys == frozenset()


def test_empty_file_is_invalid(tmp_path: Path) -> None:
    assert pol.load(_write(tmp_path, "")).status is pol.PolicyStatus.INVALID


def test_explicit_locked_list_adds_keys(tmp_path: Path) -> None:
    path = _write(tmp_path, "[astra-voice]\nlocked = language, model_id\nhotkey = Ctrl+F9\n")
    p = pol.load(path)
    assert p.locked_keys == frozenset({"hotkey", "language", "model_id"})
    assert "locked" not in p.values


def test_explicit_hotkey_lock_without_value_preserves_user_choice(tmp_path: Path) -> None:
    policy = pol.load(_write(tmp_path, "[astra-voice]\nlocked = hotkey\n"))
    settings = Settings(hotkey="Alt+Space")

    assert policy.status is pol.PolicyStatus.OK
    assert policy.values == {}
    assert policy.locked_keys == frozenset({"hotkey"})
    assert policy.is_locked("hotkey")
    assert pol.effective(settings, policy).hotkey == settings.hotkey


def test_effective_overrides_settings(tmp_path: Path) -> None:
    path = _write(tmp_path, "[astra-voice]\ncheck_model_updates = off\nlanguage = en\n")
    result = pol.effective(Settings(hotkey="Ctrl+Space"), pol.load(path))
    assert result.check_model_updates is False
    assert result.language == "en"
    assert result.hotkey == "Ctrl+Space"


@pytest.mark.parametrize(
    "combo", ["Space", "Return", "A", "Shift+Space", "Ctrl", "Ctrl+Shift", "Ctrl+A+B", ""]
)
@pytest.mark.parametrize("previous", [Settings.hotkey, "Alt+Space"])
def test_invalid_policy_hotkey_keeps_previous_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, combo: str, previous: str
) -> None:
    path = _write(tmp_path, f"[astra-voice]\nhotkey = {combo}\nlanguage = en\n")
    policy = pol.load(path)
    settings = Settings(hotkey=previous)

    with caplog.at_level("WARNING", logger=pol.__name__):
        result = pol.effective(settings, policy)

    assert policy.status is pol.PolicyStatus.OK
    assert policy.values["hotkey"] == combo
    assert result.hotkey == settings.hotkey == previous
    assert result.language == "en"
    assert settings.language == "ru"
    assert any(
        record.name == pol.__name__
        and record.levelname == "WARNING"
        and "hotkey" in record.getMessage()
        and repr(combo) in record.getMessage()
        for record in caplog.records
    )


def test_valid_policy_hotkey_overrides_settings(tmp_path: Path) -> None:
    policy = pol.load(_write(tmp_path, "[astra-voice]\nhotkey = Ctrl+F9\n"))
    assert pol.effective(Settings(hotkey="Alt+Space"), policy).hotkey == "Ctrl+F9"


def test_effective_keeps_unknown_keys_in_extra(tmp_path: Path) -> None:
    path = _write(tmp_path, "[astra-voice]\nupdates = admin\n")
    result = pol.effective(Settings(), pol.load(path))
    assert result.extra["updates"] == "admin"


def test_effective_does_not_mutate_source(tmp_path: Path) -> None:
    path = _write(tmp_path, "[astra-voice]\nlanguage = en\nupdates = admin\n")
    original = Settings()
    pol.effective(original, pol.load(path))
    assert original.language == "ru"
    assert original.extra == {}


def test_absent_policy_changes_nothing() -> None:
    original = Settings(hotkey="Ctrl+J")
    assert pol.effective(original, pol.Policy()) == original


@pytest.mark.parametrize("value", ["deny", "DENY", " no ", "false", "0", "off"])
def test_appimage_denied_values(tmp_path: Path, value: str) -> None:
    policy = pol.load(_write(tmp_path, f"[astra-voice]\n APPIMAGE = {value}\n"))
    assert policy.status is pol.PolicyStatus.OK
    assert pol.appimage_denied(policy)


@pytest.mark.parametrize("value", ["allow", " ALLOW ", "yes", "true", "1", "on"])
def test_appimage_allowed_values(tmp_path: Path, value: str) -> None:
    policy = pol.load(_write(tmp_path, f"[astra-voice]\nappimage = {value}\n"))
    assert not pol.appimage_denied(policy)


@pytest.mark.parametrize("value", ["мусор", "", "deny-extra"])
def test_unknown_appimage_value_warns_and_allows(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    policy = pol.load(_write(tmp_path, f"[astra-voice]\nappimage = {value}\n"))
    with caplog.at_level("WARNING", logger=pol.__name__):
        assert not pol.appimage_denied(policy)
    assert any(
        record.name == pol.__name__
        and record.levelname == "WARNING"
        and "appimage" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "text",
    [
        "[astra-voice]\nlanguage = ru\n",
        "[astra-voice]\nprofile = secure\n",
        "[astra-voice]\nlanguage = ru\n[other]\nappimage = deny\n",
    ],
)
def test_appimage_without_key_is_allowed(tmp_path: Path, text: str) -> None:
    """T-180: secure без appimage=deny не запрещает трек (arch/appimage.md §5)."""
    policy = pol.load(_write(tmp_path, text))
    assert policy.status is pol.PolicyStatus.OK
    assert not pol.appimage_denied(policy)


def test_appimage_absent_policy_is_allowed(tmp_path: Path) -> None:
    assert not pol.appimage_denied(pol.load(tmp_path / "absent.conf"))


def test_appimage_invalid_policy_is_allowed(tmp_path: Path) -> None:
    policy = pol.load(_write(tmp_path, "[astra-voice]\nappimage = deny\n[сломано\n"))
    assert policy.status is pol.PolicyStatus.INVALID
    assert not pol.appimage_denied(policy)
    assert not pol.appimage_denied(
        pol.Policy(values={"appimage": "deny"}, status=pol.PolicyStatus.INVALID)
    )
