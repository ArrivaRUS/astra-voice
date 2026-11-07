"""F18.11 / spec §2.8: executable contract shared by pytest and tools/validate.

Each row contains independent expected parser decisions, effective user values,
control locks and bridge presentation. QML assertions use those same controls.
`on` is a supported plaintext alias; `yes` and `1` are not grants (M1 contract).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

NOTICE = "Я веду запись и расшифровку этой встречи для протокола."
URL = "https://api.example.org/v1"
HTTP = "http://10.0.0.4/v1"
USER_URL = "https://user.example.org/v1"
KEYS = (
    "meeting_record",
    "meeting_diarization",
    "meeting_ai_endpoint",
    "meeting_ai_allow_external",
    "meeting_audio_retention_days",
    "meeting_notice_text",
    "meeting_ai_allow_plaintext",
)
PREFIXES = ("record", "diar", "endpoint", "external", "retention", "notice", "plaintext")
DEFAULTS: tuple[object, ...] = ("prompt", True, None, False, 0, NOTICE, False)
CLOSED: tuple[object, ...] = ("off", False, "off", False, 0, NOTICE, False)
USER: dict[str, Any] = {
    "meetings_enabled": True,
    "meeting_offer_on_call": True,
    "meeting_diarization": False,
    "meeting_audio_retention_days": 7,
    "meeting_notice_text": "Текст пользователя",
    "meeting_ai_endpoint": USER_URL,
    "meeting_ai_model": "personal-model",
    "offline": False,
}


@dataclass(frozen=True)
class PolicyCase:
    id: str
    key: str
    value: str
    raw: dict[str, str]
    expect: dict[str, Any]
    mode: int = 0o644
    uid: int = 0
    absent: bool = False
    broken: bool = False
    user: dict[str, Any] = field(default_factory=lambda: dict(USER))


def case(
    name: str,
    key: str,
    value: str,
    raw: dict[str, str],
    changes: dict[str, tuple[object, bool, str]],
    *,
    effective: Mapping[str, object] | None = None,
    locks: tuple[str, ...] = (),
    kind: str = "external",
    invalid: bool = False,
    mode: int = 0o644,
    uid: int = 0,
    absent: bool = False,
    broken: bool = False,
) -> PolicyCase:
    decisions = {
        k: (v, False, f"meeting-policy-{p}-user")
        for k, p, v in zip(KEYS, PREFIXES, DEFAULTS, strict=True)
    }
    decisions.update(changes)
    settings = {**USER, **(effective or {})}
    locked = {k: k in locks for k in USER if k != "offline"}
    endpoint = decisions["meeting_ai_endpoint"]
    qml: dict[str, dict[str, object]] = {
        "meetingPolicyNote": {"visible": bool(locks) and settings["meetings_enabled"]},
        "meetingSettingsGroups": {"visible": settings["meetings_enabled"]},
        "meetingOffEntry": {"visible": not settings["meetings_enabled"]},
        "meetingEnable": {"enabled": decisions["meeting_record"][0] != "off"},
        "meetingPolicyInvalidText": {"visible": invalid},
        "meetingAiKeyInput": {"enabled": True},
    }
    for object_name, setting, property_name in (
        ("meetingOfferToggle", "meeting_offer_on_call", "checked"),
        ("meetingDiarization", "meeting_diarization", "checked"),
        ("meetingNoticeFieldInput", "meeting_notice_text", "text"),
        ("meetingAiEndpointInput", "meeting_ai_endpoint", "text"),
        ("meetingAiModelInput", "meeting_ai_model", "text"),
    ):
        qml[object_name] = {"enabled": not locked[setting], property_name: settings[setting]}
    days = settings["meeting_audio_retention_days"]
    qml["meetingAudioRetention"] = {
        "enabled": not locked["meeting_audio_retention_days"],
        "currentText": "Не хранить"
        if days == 0
        else f"{days} " + ("день" if days == 1 else "дней"),
    }
    return PolicyCase(
        name,
        key,
        value,
        raw,
        {
            "qml": qml,
            "decisions": decisions,
            "settings": settings,
            "locked": locked,
            "recording_allowed": decisions["meeting_record"][0] != "off",
            "detector_allowed": decisions["meeting_record"][0] == "prompt",
            "external_without_confirmation": bool(
                decisions["meeting_ai_allow_external"][0]
                and endpoint[1]
                and endpoint[0] not in (None, "off")
            ),
            "bridge": {
                "enabled": settings["meetings_enabled"],
                "canEnable": decisions["meeting_record"][0] != "off",
                "detectorEnabled": decisions["meeting_record"][0] == "prompt",
                "policyNote": bool(locks),
                "policyInvalidText": invalid,
                "aiDisabled": endpoint[0] == "off",
                "aiAdminAddress": endpoint[1] and endpoint[0] != "off",
                "endpointKind": kind,
            },
        },
        mode=mode,
        uid=uid,
        absent=absent,
        broken=broken,
    )


CASES: list[PolicyCase] = []
for key, prefix, _default, closed in zip(KEYS, PREFIXES, DEFAULTS, CLOSED, strict=True):
    CASES.append(case(f"{key}-absent", key, "<absent>", {}, {}, absent=True))
    if key == "meeting_record":
        for value in ("off", "manual", "prompt", "garbage"):
            bad = value == "garbage"
            decision = "off" if bad else value
            locked = (
                ("meetings_enabled", "meeting_offer_on_call")
                if decision == "off"
                else (("meeting_offer_on_call",) if decision == "manual" else ())
            )
            effective = {k: False for k in locked}
            CASES.append(
                case(
                    f"record-{value}",
                    key,
                    value,
                    {key: value},
                    {
                        key: (
                            decision,
                            True,
                            f"meeting-policy-record-{'invalid' if bad else 'admin'}",
                        )
                    },
                    effective=effective,
                    locks=locked,
                    invalid=bad,
                )
            )
        continue
    valid: dict[str, list[tuple[str, object]]] = {
        "meeting_diarization": [("on", True), ("off", False)],
        "meeting_ai_endpoint": [(URL, URL), ("off", "off")],
        "meeting_ai_allow_external": [("true", True), ("false", False)],
        "meeting_audio_retention_days": [(str(n), n) for n in (0, 1, 7, 14, 30, 365)],
        "meeting_notice_text": [("Администратор", "Администратор"), ("е\u0308", "ё")],
        "meeting_ai_allow_plaintext": [
            ("true", True),
            ("on", True),
            ("false", False),
            ("off", False),
        ],
    }
    for value, parsed in [*valid[key], ("garbage", closed)]:
        bad = value == "garbage"
        control = key in {
            "meeting_diarization",
            "meeting_ai_endpoint",
            "meeting_audio_retention_days",
            "meeting_notice_text",
        }
        result = "" if key == "meeting_ai_endpoint" and parsed == "off" else parsed
        CASES.append(
            case(
                f"{prefix}-{value}",
                key,
                value,
                {key: "" if bad and key == "meeting_notice_text" else value},
                {key: (parsed, True, f"meeting-policy-{prefix}-{'invalid' if bad else 'admin'}")},
                effective={key: result} if control else {},
                locks=(key,) if control else (),
                kind="none" if key == "meeting_ai_endpoint" and parsed == "off" else "external",
            )
        )

for grant in ("true", "on", "false", "off", "yes", "1"):
    for secure in (False, True):
        allowed = grant in {"true", "on"} and not secure
        plaintext_code = "secure" if secure else ("invalid" if grant in {"yes", "1"} else "admin")
        CASES.append(
            case(
                f"http-{grant}-{'secure' if secure else 'personal'}",
                "meeting_ai_allow_plaintext",
                grant,
                {
                    "meeting_ai_allow_plaintext": grant,
                    "meeting_ai_endpoint": HTTP,
                    "profile": "secure" if secure else "personal",
                },
                {
                    "meeting_ai_allow_plaintext": (
                        allowed,
                        True,
                        f"meeting-policy-plaintext-{plaintext_code}",
                    ),
                    "meeting_ai_endpoint": (
                        HTTP if allowed else "off",
                        True,
                        f"meeting-policy-endpoint-{'admin' if allowed else 'invalid'}",
                    ),
                },
                effective={"meeting_ai_endpoint": HTTP if allowed else ""},
                locks=("meeting_ai_endpoint",),
                kind="plaintext" if allowed else "none",
            )
        )

for grant in ("true", "on", "yes", "1"):
    item = case(
        f"user-http-no-policy-address-{grant}",
        "meeting_ai_allow_plaintext",
        grant,
        {"meeting_ai_allow_plaintext": grant},
        {
            "meeting_ai_allow_plaintext": (
                grant in {"true", "on"},
                True,
                f"meeting-policy-plaintext-{'admin' if grant in {'true', 'on'} else 'invalid'}",
            )
        },
        effective={"meeting_ai_endpoint": ""},
        kind="none",
    )
    item.user["meeting_ai_endpoint"] = HTTP
    CASES.append(item)

for grant in ("true", "false", "garbage"):
    CASES.append(
        case(
            f"external-pair-{grant}",
            "meeting_ai_allow_external",
            grant,
            {"meeting_ai_allow_external": grant, "meeting_ai_endpoint": URL},
            {
                "meeting_ai_allow_external": (
                    grant == "true",
                    True,
                    f"meeting-policy-external-{'invalid' if grant == 'garbage' else 'admin'}",
                ),
                "meeting_ai_endpoint": (URL, True, "meeting-policy-endpoint-admin"),
            },
            effective={"meeting_ai_endpoint": URL},
            locks=("meeting_ai_endpoint",),
            kind="external-admin" if grant == "true" else "external",
        )
    )

for name, mode, uid, broken, code in (
    ("invalid-file", 0o644, 0, True, "invalid"),
    ("unsafe-mode-0666", 0o666, 0, False, "unsafe"),
    ("unsafe-nonroot-owner", 0o644, 12345, False, "unsafe"),
):
    CASES.append(
        case(
            name,
            "<file>",
            name,
            {"meeting_record": "prompt", "autostart": "maybe" if broken else "false"},
            {k: (v, True, f"meeting-policy-{code}") for k, v in zip(KEYS, CLOSED, strict=True)},
            effective={
                "meetings_enabled": False,
                "meeting_offer_on_call": False,
                "meeting_diarization": False,
                "meeting_audio_retention_days": 0,
                "meeting_notice_text": NOTICE,
                "meeting_ai_endpoint": "",
            },
            locks=(
                "meetings_enabled",
                "meeting_offer_on_call",
                "meeting_diarization",
                "meeting_audio_retention_days",
                "meeting_notice_text",
                "meeting_ai_endpoint",
            ),
            kind="none",
            invalid=True,
            mode=mode,
            uid=uid,
            broken=broken,
        )
    )

# Each malformed key leaves six explicitly configured neighbours intact.
NEIGHBORS = {
    "meeting_record": "manual",
    "meeting_diarization": "on",
    "meeting_ai_endpoint": URL,
    "meeting_ai_allow_external": "true",
    "meeting_audio_retention_days": "14",
    "meeting_notice_text": "Администратор",
    "meeting_ai_allow_plaintext": "on",
}
VALUES: tuple[object, ...] = ("manual", True, URL, True, 14, "Администратор", True)
for key, fallback in zip(KEYS, CLOSED, strict=True):
    changes = {
        k: (v, True, f"meeting-policy-{p}-admin")
        for k, p, v in zip(KEYS, PREFIXES, VALUES, strict=True)
    }
    prefix = PREFIXES[KEYS.index(key)]
    changes[key] = (fallback, True, f"meeting-policy-{prefix}-invalid")
    effective_values = {
        k: changes[k][0]
        for k in (
            "meeting_diarization",
            "meeting_ai_endpoint",
            "meeting_notice_text",
            "meeting_audio_retention_days",
        )
    }
    if key == "meeting_ai_endpoint":
        effective_values[key] = ""
    effective_values["meeting_offer_on_call"] = False
    if key == "meeting_record":
        effective_values["meetings_enabled"] = False
    locks = (
        "meeting_offer_on_call",
        "meeting_diarization",
        "meeting_ai_endpoint",
        "meeting_notice_text",
        "meeting_audio_retention_days",
    )
    CASES.append(
        case(
            f"bad-key-preserves-neighbors-{key}",
            key,
            "<invalid>",
            {**NEIGHBORS, key: "" if key == "meeting_notice_text" else "bad"},
            changes,
            effective=effective_values,
            locks=(*locks, "meetings_enabled") if key == "meeting_record" else locks,
            invalid=key == "meeting_record",
            kind="none"
            if key == "meeting_ai_endpoint"
            else "external"
            if key == "meeting_ai_allow_external"
            else "external-admin",
        )
    )


# Restrictions do not imply human opt-in: the entry card has no settings groups.
disabled_diarization = case(
    "diar-off-user-disabled",
    "meeting_diarization",
    "off",
    {"meeting_diarization": "off"},
    {"meeting_diarization": (False, True, "meeting-policy-diar-admin")},
    effective={"meetings_enabled": False},
    locks=("meeting_diarization",),
)
disabled_diarization.user["meetings_enabled"] = False
CASES.append(disabled_diarization)
