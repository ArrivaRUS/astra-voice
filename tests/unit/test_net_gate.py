"""Сетевой гейт: политика, независимые тумблеры и ограничения окружения."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from astra_voice.core.policy import Policy, PolicyStatus, effective, load
from astra_voice.core.settings import Settings
from astra_voice.net.gate import NetworkGate, NetworkKind

pytestmark = pytest.mark.unit

KINDS: tuple[NetworkKind, ...] = ("download", "check_app", "check_models")
POLICY_FILES = {
    "absent": (None, PolicyStatus.ABSENT),
    "online": ("[astra-voice]\noffline = false\nprofile = personal\n", PolicyStatus.OK),
    "offline": ("[astra-voice]\noffline = true\n", PolicyStatus.OK),
    "secure": ("[astra-voice]\nprofile = secure\n", PolicyStatus.OK),
    "secure-online": (
        "[astra-voice]\nprofile = secure\noffline = false\n",
        PolicyStatus.OK,
    ),
    "checks-disabled": (
        "[astra-voice]\noffline = false\ncheck_app_updates = false\ncheck_model_updates = false\n",
        PolicyStatus.OK,
    ),
    "app-check-disabled": ("[astra-voice]\ncheck_app_updates = false\n", PolicyStatus.OK),
    "model-check-disabled": ("[astra-voice]\ncheck_model_updates = false\n", PolicyStatus.OK),
    "invalid": ("[astra-voice\noffline = false\n", PolicyStatus.INVALID),
}
BLOCKING_POLICIES = ("offline", "secure", "secure-online", "invalid")


@pytest.fixture
def policy(request: pytest.FixtureRequest, tmp_path: Path) -> Policy:
    contents, status = POLICY_FILES[getattr(request, "param", "absent")]
    path = tmp_path / "policy.conf"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    result = load(path)
    assert result.status is status
    return result


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)


def assert_denied(result: tuple[bool, str]) -> None:
    allowed, reason = result
    assert allowed is False
    assert reason.strip()
    assert "HF_" not in reason
    assert "/" not in reason


@pytest.mark.parametrize(
    ("policy_values", "status", "offline_env", "checks", "kind", "expected"),
    [
        ({}, PolicyStatus.INVALID, None, True, "download", "admin"),
        ({"offline": True}, PolicyStatus.OK, None, True, "download", "admin"),
        ({"profile": "secure"}, PolicyStatus.OK, None, True, "download", "admin"),
        ({}, PolicyStatus.OK, "1", True, "download", "offline"),
        ({}, PolicyStatus.OK, None, False, "check_app", "settings"),
        ({}, PolicyStatus.OK, None, False, "check_models", "settings"),
        ({}, PolicyStatus.OK, None, False, "download", ""),
    ],
)
def test_refusal_codes(
    policy_values: dict[str, object],
    status: PolicyStatus,
    offline_env: str | None,
    checks: bool,
    kind: NetworkKind,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if offline_env is not None:
        monkeypatch.setenv("HF_HUB_OFFLINE", offline_env)
    gate = NetworkGate(
        Settings(check_app_updates=checks, check_model_updates=checks),
        Policy(values=policy_values, status=status),
    )
    assert gate.refusal(kind) == expected
    assert gate.allowed(kind)[0] is (expected == "")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("check_app", [False, True])
@pytest.mark.parametrize("check_models", [False, True])
@pytest.mark.parametrize(
    ("policy", "permitted_kinds"),
    [
        ("absent", KINDS),
        ("online", KINDS),
        *((name, ()) for name in BLOCKING_POLICIES),
        ("checks-disabled", ("download",)),
        ("app-check-disabled", ("download", "check_models")),
        ("model-check-disabled", ("download", "check_app")),
    ],
    indirect=["policy"],
)
def test_toggle_matrix(
    kind: NetworkKind,
    check_app: bool,
    check_models: bool,
    policy: Policy,
    permitted_kinds: tuple[NetworkKind, ...],
) -> None:
    settings = Settings(check_app_updates=check_app, check_model_updates=check_models)
    expected = (
        kind in permitted_kinds
        and {
            "download": True,
            "check_app": check_app,
            "check_models": check_models,
        }[kind]
    )
    result = NetworkGate(effective(settings, policy), policy).allowed(kind)
    if expected:
        assert result == (True, "")
    else:
        assert_denied(result)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("policy", BLOCKING_POLICIES, indirect=True)
@pytest.mark.parametrize("offline_env", [None, "0", "1"])
def test_policy_blocks_all(
    kind: NetworkKind, policy: Policy, offline_env: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        check_app_updates=True,
        check_model_updates=True,
        extra={"offline": False, "profile": "personal"},
    )
    expected = NetworkGate(effective(settings, policy), policy).allowed(kind)
    assert_denied(expected)
    if offline_env is not None:
        monkeypatch.setenv("HF_HUB_OFFLINE", offline_env)
    assert NetworkGate(effective(settings, policy), policy).allowed(kind) == expected
    settings.check_app_updates = False
    settings.check_model_updates = False
    assert NetworkGate(effective(settings, policy), policy).allowed(kind) == expected


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "YeS", "On"])
def test_environment_blocks_all(
    kind: NetworkKind, value: str, monkeypatch: pytest.MonkeyPatch, policy: Policy
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", value)
    settings = Settings(check_app_updates=True, check_model_updates=True)
    expected = NetworkGate(settings, policy).allowed(kind)
    assert_denied(expected)
    assert NetworkGate(Settings(), policy).allowed(kind) == expected


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("value", [None, "0", "false", "no", "off", ""])
def test_environment_does_not_grant_permission(
    kind: NetworkKind, value: str | None, monkeypatch: pytest.MonkeyPatch, policy: Policy
) -> None:
    if value is not None:
        monkeypatch.setenv("HF_HUB_OFFLINE", value)
    settings = Settings(check_app_updates=True, check_model_updates=True)
    assert NetworkGate(settings, policy).allowed(kind) == (True, "")
    result = NetworkGate(Settings(), policy).allowed(kind)
    if kind == "download":
        assert result == (True, "")
    else:
        assert_denied(result)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("policy", POLICY_FILES, indirect=True)
@pytest.mark.parametrize("checks_enabled", [False, True])
def test_endpoint_has_no_effect(
    kind: NetworkKind, policy: Policy, checks_enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(check_app_updates=checks_enabled, check_model_updates=checks_enabled)
    gate = NetworkGate(effective(settings, policy), policy)
    expected = gate.allowed(kind)
    monkeypatch.setenv("HF_ENDPOINT", "https://evil.example")
    assert gate.allowed(kind) == expected


@pytest.mark.parametrize("kind", KINDS)
def test_user_extra_is_not_policy(kind: NetworkKind, policy: Policy) -> None:
    settings = Settings(
        check_app_updates=True,
        check_model_updates=True,
        extra={"offline": True, "profile": "secure"},
    )
    assert NetworkGate(settings, policy).allowed(kind) == (True, "")


@pytest.mark.parametrize("policy", POLICY_FILES, indirect=True)
@pytest.mark.parametrize("offline_env", ["0", "1"])
def test_unknown_kind_raises(
    policy: Policy, offline_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", offline_env)
    with pytest.raises(ValueError, match="Неизвестный вид сетевого действия"):
        NetworkGate(Settings(), policy).allowed(cast(NetworkKind, "unknown"))


@pytest.mark.parametrize(
    ("contents", "expected_allowed"),
    [
        ("[astra-voice]\noffline = true\n", False),
        ("[astra-voice]\nprofile = secure\n", False),
        ("[astra-voice]\nprofile = secure\noffline = false\n", False),
        ("[astra-voice]\noffline = false\nprofile = personal\n", True),
        ("[astra-voice]\ncheck_app_updates = false\ncheck_model_updates = false\n", True),
    ],
)
def test_loaded_policy_controls_download(
    tmp_path: Path, contents: str, expected_allowed: bool
) -> None:
    path = tmp_path / "policy.conf"
    path.write_text(contents, encoding="utf-8")
    policy = load(path)
    assert policy.status is PolicyStatus.OK
    expected_reason = "" if expected_allowed else "Задано администратором: работа без сети"
    assert NetworkGate(Settings(), policy).allowed("download") == (
        expected_allowed,
        expected_reason,
    )


@pytest.mark.parametrize(
    ("value", "expected_allowed"),
    [
        ("true", False),
        ("TRUE", False),
        (" yes ", False),
        ("on", False),
        ("1", False),
        ("false", True),
        ("no", True),
        ("off", True),
        ("0", True),
        (" OFF ", True),
        ("мусор", False),
        ("", False),
        ("   ", False),
        (True, False),
        (False, True),
        (None, True),
        (0, False),
        (1, False),
        ([], False),
    ],
)
def test_policy_offline_values(value: object, expected_allowed: bool) -> None:
    policy = Policy(values={"offline": value}, status=PolicyStatus.OK)
    expected_reason = "" if expected_allowed else "Задано администратором: работа без сети"
    assert NetworkGate(Settings(), policy).allowed("download") == (
        expected_allowed,
        expected_reason,
    )


@pytest.mark.parametrize(
    ("value", "expected_allowed"),
    [
        ("secure", False),
        (" SECURE ", False),
        ("странное", False),
        ("personal", True),
        (" PERSONAL ", True),
        ("", True),
        ("   ", True),
        (None, True),
        (False, False),
        (0, False),
        ([], False),
    ],
)
def test_policy_profile_values(value: object, expected_allowed: bool) -> None:
    policy = Policy(values={"profile": value}, status=PolicyStatus.OK)
    expected_reason = "" if expected_allowed else "Задано администратором: работа без сети"
    assert NetworkGate(Settings(), policy).allowed("download") == (
        expected_allowed,
        expected_reason,
    )
