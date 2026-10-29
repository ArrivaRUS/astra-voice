"""E13 M1 step 2: raw per-key policy, closed gates and user-only opt-in."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from astra_voice.core import policy as core
from astra_voice.core.settings import Settings
from astra_voice.meetings import policy as mp
from astra_voice.meetings.settings_ext import NOTICE_TEXT, effective_settings

pytestmark = pytest.mark.unit

KEYS = (
    "meeting_record",
    "meeting_diarization",
    "meeting_ai_endpoint",
    "meeting_ai_allow_external",
    "meeting_audio_retention_days",
    "meeting_notice_text",
    "meeting_ai_allow_plaintext",
)


def parsed(**raw: str) -> mp.MeetingPolicy:
    return mp.parse_policy(core.Policy(raw=raw, status=core.PolicyStatus.OK))


def policy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, *, uid: int = 0, mode: int = 0o644
) -> core.Policy:
    path = tmp_path / "policy.conf"
    path.write_text("[astra-voice]\n" + body, encoding="utf-8")
    path.chmod(mode)
    identity = path.stat().st_dev, path.stat().st_ino
    original = os.fstat

    def fstat(fd: int) -> os.stat_result:
        info = original(fd)
        if (info.st_dev, info.st_ino) == identity:
            values = list(info)
            values[4] = uid
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "fstat", fstat)
    return core.load(path)


def test_raw_snapshot_precedes_coercion_and_locked_pop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = core._coerce
    spy = Mock(wraps=original)
    monkeypatch.setattr(core, "_coerce", spy)
    policy = policy_file(
        tmp_path, monkeypatch, "autostart=false\nmeeting_record=garbage\nlocked=language\n"
    )
    assert policy.status is core.PolicyStatus.OK
    assert policy.raw == {"autostart": "false", "meeting_record": "garbage", "locked": "language"}
    assert policy.values["autostart"] is False
    assert not core.effective(Settings(autostart=True), policy).autostart
    assert "meeting_record" not in [call.args[0] for call in spy.call_args_list]
    assert not any(key in core._SETTINGS_TYPES for key in KEYS)
    with pytest.raises(TypeError):
        policy.raw["meeting_record"] = "prompt"  # type: ignore[index]


@pytest.mark.parametrize(
    "body,uid,mode,status",
    [
        ("autostart=maybe\n", 0, 0o644, core.PolicyStatus.INVALID),
        ("autostart=true\n", 0, 0o666, core.PolicyStatus.OK),
        ("autostart=true\n", 12345, 0o644, core.PolicyStatus.OK),
    ],
)
def test_invalid_or_unsafe_file_closes_all_meeting_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    uid: int,
    mode: int,
    status: core.PolicyStatus,
) -> None:
    source = policy_file(
        tmp_path, monkeypatch, body + "meeting_record=prompt\n", uid=uid, mode=mode
    )
    assert source.status is status
    result = mp.parse_policy(source)
    expected = (mp.RecordMode.OFF, False, "off", False, 0, NOTICE_TEXT, False)
    assert tuple(getattr(result, key).value for key in KEYS) == expected
    assert all(getattr(result, key).locked for key in KEYS)
    assert all(getattr(result, key).reason for key in KEYS)
    assert not result.detector_allowed and not result.recording_allowed
    if status is core.PolicyStatus.INVALID:
        assert source.raw["autostart"] == "maybe"


def test_hotkey_abc_is_not_invalid_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = policy_file(tmp_path, monkeypatch, "hotkey=abc\nmeeting_record=prompt\n")
    assert source.status is core.PolicyStatus.OK
    assert mp.parse_policy(source).recording_allowed


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("meeting_record", "manual", mp.RecordMode.MANUAL),
        ("meeting_diarization", "off", False),
        ("meeting_ai_endpoint", "https://api.example.org/v1", "https://api.example.org/v1"),
        ("meeting_ai_allow_external", "true", True),
        ("meeting_audio_retention_days", "365", 365),
        ("meeting_notice_text", "е\u0308", "ё"),
        ("meeting_ai_allow_plaintext", "on", True),
    ],
)
def test_seven_keys_parse_lock_reason_code(key: str, value: str, expected: object) -> None:
    policy = parsed(**{key: value})
    decision = getattr(policy, key)
    assert decision.value == expected
    assert decision.locked and decision.reason == mp.REASON_ADMIN
    assert decision.code in mp.PolicyCode
    assert set(policy.decisions) == set(KEYS)


@pytest.mark.parametrize(
    "key,fallback",
    [
        ("meeting_record", mp.RecordMode.OFF),
        ("meeting_diarization", False),
        ("meeting_ai_endpoint", "off"),
        ("meeting_ai_allow_external", False),
        ("meeting_audio_retention_days", 0),
        ("meeting_notice_text", NOTICE_TEXT),
        ("meeting_ai_allow_plaintext", False),
    ],
)
def test_bad_single_key_closes_only_itself(key: str, fallback: object) -> None:
    policy = parsed(**{key: "PRIVATE_MARKER\u202e"})
    for name in KEYS:
        decision = getattr(policy, name)
        assert decision.locked is (name == key)
    bad = getattr(policy, key)
    assert bad.value == fallback and bad.reason == mp.REASON_INVALID
    assert "PRIVATE_MARKER" not in bad.reason and "PRIVATE_MARKER" not in bad.code


@pytest.mark.parametrize("bad_key", KEYS)
def test_bad_key_preserves_six_explicit_neighbor_decisions(bad_key: str) -> None:
    raw = {
        "meeting_record": "manual",
        "meeting_diarization": "off",
        "meeting_ai_endpoint": "https://example.org/v1",
        "meeting_ai_allow_external": "true",
        "meeting_audio_retention_days": "30",
        "meeting_notice_text": "Администратор",
        "meeting_ai_allow_plaintext": "on",
    }
    expected = parsed(**raw)
    result = parsed(**{**raw, bad_key: "PRIVATE_MARKER\u202e"})
    assert result.decisions[bad_key].reason == mp.REASON_INVALID
    for key in KEYS:
        if key != bad_key:
            assert result.decisions[key] == expected.decisions[key], key


@pytest.mark.parametrize("value", ["-1", "366", "1.5", "true", "0x10", "PRIVATE_MARKER"])
def test_bad_retention_range(value: str) -> None:
    policy = parsed(meeting_audio_retention_days=value, meeting_diarization="on")
    assert policy.meeting_audio_retention_days.value == 0
    assert policy.meeting_audio_retention_days.reason == mp.REASON_INVALID
    assert policy.meeting_diarization.value is True


@pytest.mark.parametrize("days", [0, 1, 364, 365])
def test_valid_retention_boundaries_are_locked(days: int) -> None:
    decision = parsed(meeting_audio_retention_days=str(days)).meeting_audio_retention_days
    assert decision.value == days and decision.locked and decision.reason == mp.REASON_ADMIN


@pytest.mark.parametrize("value", ["", " " * 5, "x" * 501, "a\nb", "a\tb", "a\x00b", "a\u200bb"])
def test_notice_invalid_is_fixed_default(value: str) -> None:
    decision = parsed(meeting_notice_text=value).meeting_notice_text
    assert (
        decision.value == NOTICE_TEXT and decision.locked and decision.reason == mp.REASON_INVALID
    )


def test_notice_nfc_before_500_limit() -> None:
    assert parsed(meeting_notice_text="е\u0308" * 500).meeting_notice_text.value == "ё" * 500


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://example.org",
        "https://user:PRIVATE_MARKER@example.org/v1",
        "https://example.org?q=PRIVATE_MARKER",
        "https://example.org#fragment",
        "https://example.org:70000/v1",
        "https://",
        "https://exa mple.org",
        "https://example.org/\u202e",
        "https://example.org/%0a",
        "https://example.org/../v1",
        "https://[::1",
        "https://example.org\\evil",
    ],
)
def test_invalid_endpoint_closes_only_endpoint(endpoint: str) -> None:
    policy = parsed(meeting_ai_endpoint=endpoint, meeting_diarization="on")
    assert policy.meeting_ai_endpoint.value == "off"
    assert policy.meeting_ai_endpoint.reason == mp.REASON_INVALID
    assert policy.meeting_diarization.value is True


@pytest.mark.parametrize(
    "grant,secure,allowed",
    [
        ("on", False, True),
        ("true", False, True),
        ("off", False, False),
        ("false", False, False),
        ("yes", False, False),
        ("1", False, False),
        ("on", True, False),
        ("true", True, False),
    ],
)
@pytest.mark.parametrize(
    "endpoint",
    ["http://10.0.0.4/v1", "http://127.0.0.1/v1", "http://[::1]/v1"],
)
def test_plaintext_requires_explicit_grant_outside_secure(
    grant: str, secure: bool, allowed: bool, endpoint: str
) -> None:
    policy = parsed(
        profile="secure" if secure else "personal",
        meeting_ai_allow_plaintext=grant,
        meeting_ai_endpoint=endpoint,
    )
    assert policy.meeting_ai_endpoint.value == (endpoint if allowed else "off")
    if secure:
        assert policy.meeting_ai_allow_plaintext.value is False
        assert policy.meeting_ai_allow_plaintext.reason == mp.REASON_SECURE


def test_secure_effective_legacy_profile_cannot_be_weakened_by_raw() -> None:
    source = core.Policy(
        status=core.PolicyStatus.OK,
        values={"profile": "secure"},
        raw={"meeting_ai_allow_plaintext": "on", "meeting_ai_endpoint": "http://10.0.0.4"},
    )
    assert not mp.parse_policy(source).meeting_ai_allow_plaintext.value


@pytest.mark.parametrize("profile", ["SECURE", "Secure", " secure "])
def test_secure_profile_matches_legacy_case_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    source = policy_file(
        tmp_path,
        monkeypatch,
        f"profile={profile}\nmeeting_ai_allow_plaintext=on\n"
        "meeting_ai_endpoint=http://10.0.0.4/v1\n",
    )
    policy = mp.parse_policy(source)
    assert policy.meeting_ai_allow_plaintext.value is False
    assert policy.meeting_ai_endpoint.value == "off"
    inconsistent = core.Policy(
        status=core.PolicyStatus.OK,
        values={"profile": profile},
        raw={"meeting_ai_allow_plaintext": "on"},
    )
    assert mp.parse_policy(inconsistent).meeting_ai_allow_plaintext.value is False


def test_effective_controls_preserve_invalid_policy_reason() -> None:
    bad = parsed(meeting_record="garbage")
    result = effective_settings(Settings(meetings_enabled=True), bad)
    assert result.controls["meetings_enabled"].reason == mp.REASON_INVALID
    unsafe = mp.parse_policy(core.Policy(status=core.PolicyStatus.INVALID))
    result = effective_settings(Settings(meetings_enabled=True), unsafe)
    assert result.controls["meetings_enabled"].reason == mp.REASON_UNTRUSTED


@pytest.mark.parametrize(
    "record,detector", [("off", False), ("manual", False), ("prompt", True), ("on", False)]
)
def test_policy_never_enables_user_feature_and_contracts_detector(
    record: str, detector: bool
) -> None:
    policy = parsed(meeting_record=record)
    result = effective_settings(
        Settings(meetings_enabled=False, meeting_offer_on_call=True), policy
    )
    assert result.settings.meetings_enabled is False and not result.detector_allowed
    assert policy.detector_allowed is detector
    enabled = effective_settings(
        Settings(meetings_enabled=True, meeting_offer_on_call=True), policy
    )
    assert enabled.detector_allowed is detector
    if record in {"off", "on"}:
        assert not enabled.settings.meetings_enabled and enabled.controls["meetings_enabled"].locked
    if record == "manual":
        assert enabled.settings.meetings_enabled
        assert not enabled.settings.meeting_offer_on_call
        assert enabled.controls["meeting_offer_on_call"].locked
        assert not enabled.controls["meetings_enabled"].locked


def test_absent_policy_respects_user_values_and_no_side_effects() -> None:
    user = Settings(
        meetings_enabled=True,
        meeting_offer_on_call=True,
        meeting_diarization=False,
        meeting_self_label="Имя",
        extra={"future": 1},
        hotkey="Alt+Space",
    )
    before = user.to_dict()
    result = effective_settings(user, mp.parse_policy(core.Policy()))
    assert result.settings.to_dict() == before and user.to_dict() == before
    assert not any(field.locked for field in result.controls.values())
    assert not result.explanation and result.detector_allowed
    result.settings.extra["future"] = 2
    assert user.extra["future"] == 1


def test_effective_policy_overrides_only_locked_meeting_settings() -> None:
    user = Settings(
        meetings_enabled=True,
        meeting_diarization=False,
        meeting_audio_retention_days=7,
        meeting_notice_text="Пользователь",
        meeting_ai_model="personal-model",
        hotkey="Alt+Space",
    )
    result = effective_settings(
        user,
        parsed(
            meeting_diarization="on",
            meeting_audio_retention_days="30",
            meeting_notice_text="Администратор",
            meeting_ai_endpoint="https://api.example.org/v1",
        ),
    )
    assert (
        result.settings.meeting_diarization and result.settings.meeting_audio_retention_days == 30
    )
    assert result.settings.meeting_notice_text == "Администратор"
    assert result.settings.meeting_ai_endpoint == "https://api.example.org/v1"
    assert (
        result.settings.meeting_ai_model == "personal-model"
        and result.settings.hotkey == "Alt+Space"
    )
    assert result.controls["meeting_notice_text"].locked
    assert not result.controls["meeting_ai_model"].locked and result.key_editable
    assert result.explanation == mp.POLICY_EXPLANATION


def test_user_http_cannot_borrow_plaintext_grant_without_policy_endpoint() -> None:
    result = effective_settings(
        Settings(meeting_ai_endpoint="http://10.0.0.4/v1"), parsed(meeting_ai_allow_plaintext="on")
    )
    assert result.settings.meeting_ai_endpoint == ""


def test_external_warning_exemption_requires_endpoint_in_same_policy() -> None:
    assert not parsed(meeting_ai_allow_external="true").external_without_confirmation
    assert parsed(
        meeting_ai_allow_external="true", meeting_ai_endpoint="https://example.org"
    ).external_without_confirmation


def test_new_policy_logs_only_fixed_codes(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("DEBUG", logger=mp.__name__):
        parsed(**{key: "PRIVATE_MARKER /private/path" for key in KEYS})
        mp.parse_policy(
            core.Policy(status=core.PolicyStatus.INVALID, raw={"secret": "PRIVATE_MARKER"})
        )
    records = [record for record in caplog.records if record.name == mp.__name__]
    assert records
    assert all(record.getMessage() in {code.value for code in mp.PolicyCode} for record in records)
    assert "PRIVATE_MARKER" not in caplog.text and "/private/path" not in caplog.text


@pytest.mark.parametrize(
    "endpoint",
    ["http://example.com", "http://localhost", "http://internal.corp/v1", "http://LOCALHOST/v1"],
)
def test_revision_http_requires_literal_internal_ip(endpoint: str) -> None:
    with pytest.raises(ValueError):
        mp.validate_endpoint(endpoint, allow_http=True)
    result = parsed(meeting_ai_endpoint=endpoint, meeting_ai_allow_plaintext="on")
    assert result.meeting_ai_endpoint.value == "off"
    assert result.meeting_ai_endpoint.reason == mp.REASON_INVALID


@pytest.mark.parametrize(
    "endpoint",
    ["http://8.8.8.8.", "http://10.0.0.4.", "https://example.org.", "http://8.8.8.8.."],
)
def test_revision_trailing_dot_hosts_rejected(endpoint: str) -> None:
    with pytest.raises(ValueError):
        mp.validate_endpoint(endpoint, allow_http=True)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://http://evil.com",
        "https://example.org:",
        "https://example.org:/v1",
        "https://[::1]:/v1",
        "https://example.org//evil.com",
        "https://example.org/%2Fevil.com",
    ],
)
def test_revision_ambiguous_authority_and_path_rejected(endpoint: str) -> None:
    with pytest.raises(ValueError):
        mp.validate_endpoint(endpoint)


@pytest.mark.parametrize(
    "encoded",
    ["%0a", "%00", "%09", "%7f", "%C2%85", "%E2%80%AE", "%E2%80%8B", "%20", "%C2%A0", "e%CC%81"],
)
def test_revision_decoded_path_controls_whitespace_and_non_nfc_rejected(encoded: str) -> None:
    with pytest.raises(ValueError):
        mp.validate_endpoint("https://example.org/v1/" + encoded)


@pytest.mark.parametrize("key", KEYS)
def test_revision_explicit_lock_without_value_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    source = policy_file(tmp_path, monkeypatch, f"autostart=false\nlocked={key}\n")
    assert source.status is core.PolicyStatus.OK and key in source.locked_keys
    assert key not in source.raw
    result = mp.parse_policy(source)
    fallback = (mp.RecordMode.OFF, False, "off", False, 0, NOTICE_TEXT, False)
    assert result.decisions[key].locked
    assert result.decisions[key].value == fallback[KEYS.index(key)]
    assert result.decisions[key].reason == mp.REASON_INVALID
    assert result.decisions[key].code.value.endswith("-invalid")
    assert all(not decision.locked for name, decision in result.decisions.items() if name != key)
    assert core.effective(Settings(autostart=True), source).autostart is False


@pytest.mark.parametrize("locked_only", [False, True])
def test_revision_unknown_meeting_key_closes_meetings_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    locked_only: bool,
) -> None:
    typo = "meeting_recordd_PRIVATE_MARKER".lower()
    original = core._coerce
    spy = Mock(wraps=original)
    monkeypatch.setattr(core, "_coerce", spy)
    body = f"locked={typo}\n" if locked_only else f"{typo}=off\n"
    source = policy_file(
        tmp_path,
        monkeypatch,
        body + "autostart=false\nmeeting_record=prompt\nmeeting_ai_allow_external=true\n",
    )
    assert source.status is core.PolicyStatus.OK
    assert not core.effective(Settings(autostart=True), source).autostart
    assert typo not in source.values
    assert typo not in [call.args[0] for call in spy.call_args_list]
    with caplog.at_level("WARNING", logger=mp.__name__):
        result = mp.parse_policy(source)
    assert tuple(row.value for row in result.decisions.values()) == (
        mp.RecordMode.OFF,
        False,
        "off",
        False,
        0,
        NOTICE_TEXT,
        False,
    )
    assert all(row.locked and row.reason for row in result.decisions.values())
    assert all(row.code.value == "meeting-policy-unknown-key" for row in result.decisions.values())
    records = [record.getMessage() for record in caplog.records if record.name == mp.__name__]
    assert records == ["meeting-policy-unknown-key"]
    assert typo not in caplog.text
    effective = effective_settings(
        Settings(meetings_enabled=True, meeting_offer_on_call=True), result
    )
    assert not effective.recording_allowed and not effective.detector_allowed


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://10.0.0.4/v1",
        "http://[FD00::1]/v1",
        "http://[::ffff:192.168.1.2]/v1",
        "https://EXAMPLE.org/v1/%C3%A9",
    ],
)
def test_revision_canonical_safe_endpoints_still_work(endpoint: str) -> None:
    assert mp.validate_endpoint(endpoint, allow_http=True) == endpoint
