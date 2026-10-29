"""Seven meeting policy keys: pure, per-key, fail-closed interpretation.

Only this parser consumes meeting values. Legacy policy semantics stay intact.
URL validation here is a data contract, never network authorization: M9 must
classify/pin/recheck DNS and gate each request. The current step explicitly
accepts plaintext=on as well as the plan's true; yes/1 are not grants.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Generic, TypeVar
from urllib.parse import unquote, urlsplit

from astra_voice.core.policy import MEETING_POLICY_KEYS, Policy, PolicyStatus
from astra_voice.meetings.model import normalize_text
from astra_voice.meetings.settings_ext import NOTICE_TEXT

log = logging.getLogger(__name__)
REASON_ADMIN = "Задано администратором"
REASON_INVALID = (
    "Задано администратором · значение политики некорректно — обратитесь к администратору"
)
REASON_UNTRUSTED = (
    "Политика администратора недоступна или небезопасна — обратитесь к администратору"
)
REASON_SECURE = "Недоступно в защищённом профиле"
REASON_RECORD_OFF = "Запись встреч отключена администратором"
REASON_MANUAL = "Администратор разрешил только ручную запись"
POLICY_EXPLANATION = "Часть настроек задана администратором."


class RecordMode(StrEnum):
    OFF = "off"
    MANUAL = "manual"
    PROMPT = "prompt"


class PolicyCode(StrEnum):
    RECORD_USER = "meeting-policy-record-user"
    RECORD_ADMIN = "meeting-policy-record-admin"
    RECORD_INVALID = "meeting-policy-record-invalid"
    DIAR_USER = "meeting-policy-diar-user"
    DIAR_ADMIN = "meeting-policy-diar-admin"
    DIAR_INVALID = "meeting-policy-diar-invalid"
    ENDPOINT_USER = "meeting-policy-endpoint-user"
    ENDPOINT_ADMIN = "meeting-policy-endpoint-admin"
    ENDPOINT_INVALID = "meeting-policy-endpoint-invalid"
    EXTERNAL_USER = "meeting-policy-external-user"
    EXTERNAL_ADMIN = "meeting-policy-external-admin"
    EXTERNAL_INVALID = "meeting-policy-external-invalid"
    RETENTION_USER = "meeting-policy-retention-user"
    RETENTION_ADMIN = "meeting-policy-retention-admin"
    RETENTION_INVALID = "meeting-policy-retention-invalid"
    NOTICE_USER = "meeting-policy-notice-user"
    NOTICE_ADMIN = "meeting-policy-notice-admin"
    NOTICE_INVALID = "meeting-policy-notice-invalid"
    PLAINTEXT_USER = "meeting-policy-plaintext-user"
    PLAINTEXT_ADMIN = "meeting-policy-plaintext-admin"
    PLAINTEXT_INVALID = "meeting-policy-plaintext-invalid"
    INVALID = "meeting-policy-invalid"
    UNSAFE = "meeting-policy-unsafe"
    UNKNOWN_KEY = "meeting-policy-unknown-key"
    SECURE = "meeting-policy-plaintext-secure"
    HTTP_DENIED = "meeting-policy-http-denied"
    USER_SETTING = "meeting-policy-user-setting"


T_co = TypeVar("T_co", covariant=True)
T = TypeVar("T")


@dataclass(frozen=True)
class PolicyValue(Generic[T_co]):
    value: T_co
    locked: bool = False
    reason: str = ""
    code: PolicyCode = PolicyCode.USER_SETTING


@dataclass(frozen=True)
class MeetingPolicy:
    meeting_record: PolicyValue[RecordMode]
    meeting_diarization: PolicyValue[bool]
    meeting_ai_endpoint: PolicyValue[str | None]
    meeting_ai_allow_external: PolicyValue[bool]
    meeting_audio_retention_days: PolicyValue[int]
    meeting_notice_text: PolicyValue[str]
    meeting_ai_allow_plaintext: PolicyValue[bool]

    @property
    def decisions(self) -> Mapping[str, PolicyValue[object]]:
        return MappingProxyType(
            {
                "meeting_record": self.meeting_record,
                "meeting_diarization": self.meeting_diarization,
                "meeting_ai_endpoint": self.meeting_ai_endpoint,
                "meeting_ai_allow_external": self.meeting_ai_allow_external,
                "meeting_audio_retention_days": self.meeting_audio_retention_days,
                "meeting_notice_text": self.meeting_notice_text,
                "meeting_ai_allow_plaintext": self.meeting_ai_allow_plaintext,
            }
        )

    @property
    def recording_allowed(self) -> bool:
        return self.meeting_record.value != RecordMode.OFF

    @property
    def detector_allowed(self) -> bool:
        """Policy capability only; user opt-in is additionally required."""
        return self.meeting_record.value == RecordMode.PROMPT

    @property
    def external_without_confirmation(self) -> bool:
        return (
            self.meeting_ai_allow_external.value
            and self.meeting_ai_endpoint.locked
            and self.meeting_ai_endpoint.value not in (None, "off")
        )


def _switch(raw: str) -> bool:
    if raw.strip() not in {"on", "off"}:
        raise ValueError("Недопустимое значение политики встреч")
    return raw.strip() == "on"


def _boolean(raw: str) -> bool:
    if raw.strip() not in {"true", "false"}:
        raise ValueError("Недопустимое значение политики встреч")
    return raw.strip() == "true"


def _plaintext(raw: str) -> bool:
    if raw.strip() in {"on", "off"}:
        return _switch(raw)
    return _boolean(raw)


def _days(raw: str) -> int:
    value = raw.strip()
    if re.fullmatch(r"[0-9]{1,3}", value) is None or int(value) > 365:
        raise ValueError("Недопустимый срок хранения")
    return int(value)


def _notice(raw: str) -> str:
    value = normalize_text(raw, 500, minimum=1)
    if not value.strip():
        raise ValueError("Пустой текст уведомления")
    return value


_HTTP_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    )
)


def validate_endpoint(value: str, *, allow_http: bool = False) -> str:
    """Validate a base URL without DNS or I/O; M9 still owes network checks."""
    value = normalize_text(value, 2048, minimum=1)
    if value == "off":
        return value
    if (
        not value.isascii()
        or any(c.isspace() or c in "\\?#" for c in value)
        or re.search(r"%(?![0-9a-fA-F]{2})", value)
    ):
        raise ValueError("Недопустимый адрес ИИ")
    parts = urlsplit(value)
    host = parts.hostname
    if host is not None:
        host = host.lower()
    if (
        parts.scheme not in {"https", "http"}
        or not host
        or host.endswith(".")
        or "%" in parts.netloc
        or parts.netloc.endswith(":")
        or parts.username is not None
        or parts.password is not None
        or parts.port is not None
        and not 1 <= parts.port <= 65535
    ):
        raise ValueError("Недопустимый адрес ИИ")
    decoded_path = unquote(parts.path, errors="strict")
    path = normalize_text(decoded_path, 2048)
    if (
        path != decoded_path
        or any(c.isspace() for c in path)
        or path.startswith("//")
        or "\\" in path
        or any(component in {".", ".."} for component in path.split("/"))
    ):
        raise ValueError("Недопустимый путь ИИ")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if parts.scheme == "http":
            raise ValueError("HTTP требует внутреннего IP-адреса") from None
        labels = host.split(".")
        if (
            len(host) > 253
            or any(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None
                for label in labels
            )
            or re.fullmatch(
                r"(?:0[xX][0-9A-Fa-f]+|[0-9]+)(?:\.(?:0[xX][0-9A-Fa-f]+|[0-9]+))*", host
            )
        ):
            raise ValueError("Недопустимое имя сервера ИИ") from None
    else:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if address.is_unspecified or address.is_multicast or address.is_link_local:
            raise ValueError("Недопустимый адрес сервера ИИ")
        if parts.scheme == "http" and not any(address in net for net in _HTTP_NETWORKS):
            raise ValueError("HTTP требует внутреннего адреса")
    if parts.scheme == "http" and not allow_http:
        raise ValueError("HTTP не разрешён политикой")
    return value


def _decision(
    raw: Mapping[str, str],
    locked_keys: frozenset[str],
    key: str,
    default: T,
    fallback: T,
    parse: Callable[[str], T],
    codes: tuple[PolicyCode, PolicyCode, PolicyCode],
) -> PolicyValue[T]:
    if key not in raw:
        if key in locked_keys:
            log.warning(codes[2].value)
            return PolicyValue(fallback, True, REASON_INVALID, codes[2])
        return PolicyValue(default, code=codes[0])
    try:
        text = raw[key]
        if not isinstance(text, str):
            raise ValueError("Недопустимый тип политики встреч")
        return PolicyValue(parse(text), True, REASON_ADMIN, codes[1])
    except (ValueError, TypeError, UnicodeError):
        log.warning(codes[2].value)
        return PolicyValue(fallback, True, REASON_INVALID, codes[2])


def _closed(code: PolicyCode) -> MeetingPolicy:
    log.warning(code.value)
    return MeetingPolicy(
        PolicyValue(RecordMode.OFF, True, REASON_UNTRUSTED, code),
        PolicyValue(False, True, REASON_UNTRUSTED, code),
        PolicyValue("off", True, REASON_UNTRUSTED, code),
        PolicyValue(False, True, REASON_UNTRUSTED, code),
        PolicyValue(0, True, REASON_UNTRUSTED, code),
        PolicyValue(NOTICE_TEXT, True, REASON_UNTRUSTED, code),
        PolicyValue(False, True, REASON_UNTRUSTED, code),
    )


def parse_policy(policy: Policy) -> MeetingPolicy:
    """Consume one raw snapshot; no reading a path again or changing settings."""
    if policy.status not in {PolicyStatus.ABSENT, PolicyStatus.OK}:
        return _closed(PolicyCode.INVALID)
    if policy.permissions_warning:
        return _closed(PolicyCode.UNSAFE)
    raw = dict(policy.raw)
    if any(
        key.startswith("meeting_") and key not in MEETING_POLICY_KEYS
        for key in raw.keys() | policy.locked_keys
    ):
        return _closed(PolicyCode.UNKNOWN_KEY)
    record = _decision(
        raw,
        policy.locked_keys,
        "meeting_record",
        RecordMode.PROMPT,
        RecordMode.OFF,
        lambda value: RecordMode(value.strip()),
        (PolicyCode.RECORD_USER, PolicyCode.RECORD_ADMIN, PolicyCode.RECORD_INVALID),
    )
    diar = _decision(
        raw,
        policy.locked_keys,
        "meeting_diarization",
        True,
        False,
        _switch,
        (PolicyCode.DIAR_USER, PolicyCode.DIAR_ADMIN, PolicyCode.DIAR_INVALID),
    )
    external = _decision(
        raw,
        policy.locked_keys,
        "meeting_ai_allow_external",
        False,
        False,
        _boolean,
        (PolicyCode.EXTERNAL_USER, PolicyCode.EXTERNAL_ADMIN, PolicyCode.EXTERNAL_INVALID),
    )
    days = _decision(
        raw,
        policy.locked_keys,
        "meeting_audio_retention_days",
        0,
        0,
        _days,
        (PolicyCode.RETENTION_USER, PolicyCode.RETENTION_ADMIN, PolicyCode.RETENTION_INVALID),
    )
    notice = _decision(
        raw,
        policy.locked_keys,
        "meeting_notice_text",
        NOTICE_TEXT,
        NOTICE_TEXT,
        _notice,
        (PolicyCode.NOTICE_USER, PolicyCode.NOTICE_ADMIN, PolicyCode.NOTICE_INVALID),
    )
    plaintext = _decision(
        raw,
        policy.locked_keys,
        "meeting_ai_allow_plaintext",
        False,
        False,
        _plaintext,
        (PolicyCode.PLAINTEXT_USER, PolicyCode.PLAINTEXT_ADMIN, PolicyCode.PLAINTEXT_INVALID),
    )
    secure = any(
        isinstance(value, str) and value.strip().lower() == "secure"
        for value in (raw.get("profile"), policy.values.get("profile"))
    )
    if secure:
        plaintext = replace(
            plaintext, value=False, locked=True, reason=REASON_SECURE, code=PolicyCode.SECURE
        )
    endpoint: PolicyValue[str | None] = _decision(
        raw,
        policy.locked_keys,
        "meeting_ai_endpoint",
        None,
        "off",
        lambda value: validate_endpoint(value, allow_http=plaintext.value and not secure),
        (PolicyCode.ENDPOINT_USER, PolicyCode.ENDPOINT_ADMIN, PolicyCode.ENDPOINT_INVALID),
    )
    return MeetingPolicy(record, diar, endpoint, external, days, notice, plaintext)
