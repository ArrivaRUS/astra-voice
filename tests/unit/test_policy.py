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


def test_effective_overrides_settings(tmp_path: Path) -> None:
    path = _write(tmp_path, "[astra-voice]\ncheck_model_updates = off\nlanguage = en\n")
    result = pol.effective(Settings(hotkey="Ctrl+Space"), pol.load(path))
    assert result.check_model_updates is False
    assert result.language == "en"
    assert result.hotkey == "Ctrl+Space"


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
