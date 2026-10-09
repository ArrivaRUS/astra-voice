"""Wire outcomes derived from contract vectors, independently of transport code."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtDBus import QDBusVariant

from astra_voice.platform.cowork import (
    classify_error,
    normalize_text,
    parse_reply,
    resolve_bus_address,
    valid_text,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
VECTORS: dict[str, Any] = json.loads(
    (ROOT / "docs/contracts/astra-cowork-command1-vectors.json").read_text(encoding="utf-8")
)
REQUEST_ID = VECTORS["base"]["options"]["request_id"]
REPLIES = [
    v
    for v in VECTORS["client_outcome_vectors"]
    if "reply" in v.get("input", {}) and "reason" in v["expect"]
]


@pytest.mark.parametrize("vector", REPLIES, ids=lambda v: v["id"])
def test_documented_reply_outcomes(vector: dict[str, Any]) -> None:
    result = parse_reply(vector["input"]["reply"], REQUEST_ID)
    assert (result.outcome, result.reason) == (
        vector["expect"]["kind"],
        vector["expect"]["reason"],
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "accepted",
        [],
        {"status": "accepted", "contract": 1},
        {"status": "accepted", "contract": True, "request_id": REQUEST_ID},
        {"status": "accepted", "contract": "1", "request_id": REQUEST_ID},
        {"status": "accepted", "contract": 1, "request_id": REQUEST_ID, "reason": "secret"},
        {"status": "accepted", "contract": 1, "request_id": REQUEST_ID, "duplicate": 1},
    ],
)
def test_malformed_acceptance_cannot_confirm_delivery(payload: object) -> None:
    result = parse_reply(payload, REQUEST_ID)
    assert (result.outcome, result.reason) == ("unknown", "bad_reply")


def test_qt_variant_and_duplicate_acknowledgement_are_supported() -> None:
    result = parse_reply(
        QDBusVariant(
            {"status": "accepted", "contract": 1, "request_id": REQUEST_ID, "duplicate": True}
        ),
        REQUEST_ID,
    )
    assert (result.outcome, result.reason) == ("delivered", "none")


@pytest.mark.parametrize(
    ("name", "outcome", "reason"),
    [
        ("NoReply", "unknown", "timeout"),
        ("Timeout", "unknown", "timeout"),
        ("TimedOut", "unknown", "timeout"),
        ("Disconnected", "unknown", "disconnected"),
        ("NameHasNoOwner", "undelivered", "not_running"),
        ("ServiceUnknown", "undelivered", "not_running"),
        ("UnknownInterface", "undelivered", "no_contract"),
        ("UnknownMethod", "undelivered", "no_contract"),
        ("UnknownObject", "undelivered", "no_contract"),
        ("InvalidArgs", "undelivered", "refused"),
        ("InvalidSignature", "undelivered", "refused"),
        ("AccessDenied", "undelivered", "refused"),
        ("LimitsExceeded", "undelivered", "refused"),
        ("Failed", "unknown", "bad_reply"),
    ],
)
def test_only_complete_pre_execution_error_names_prove_refusal(
    name: str, outcome: str, reason: str
) -> None:
    result = classify_error("org.freedesktop.DBus.Error." + name)
    assert (result.outcome, result.reason) == (outcome, reason)


@pytest.mark.parametrize("name", ["com.example.Error.NoReply", "NoReply", "secret.NoReply"])
def test_foreign_errors_never_prove_refusal_or_retain_remote_name(name: str) -> None:
    result = classify_error(name)
    assert (result.outcome, result.reason, result.error_name) == (
        "unknown",
        "bad_reply",
        "remote_error",
    )


@pytest.mark.parametrize("connected", [False, True])
def test_rejected_local_enqueue_is_immediate_proven_non_delivery(connected: bool) -> None:
    result = classify_error("org.freedesktop.DBus.Error.Failed", sent=False, connected=connected)
    assert (result.outcome, result.reason) == ("undelivered", "refused" if connected else "no_bus")


def test_remote_reason_does_not_survive_in_public_feedback() -> None:
    marker = "SECRET_SENTENCE_FOR_COMMAND_ACCEPTANCE"
    result = parse_reply(
        {"status": "refused", "contract": 1, "reason": marker, "request_id": REQUEST_ID},
        REQUEST_ID,
    )
    assert result.server_reason == "unknown_reason"
    assert marker not in repr(result)


@pytest.mark.parametrize(
    "address",
    [
        "tcp:host=127.0.0.1,port=1234",
        "autolaunch:",
        "unixexec:path=/bin/sh",
        "unix:path=/tmp/private;tcp:host=127.0.0.1,port=1234",
        "unix:path=/tmp/private;autolaunch:",
        "unix:path=/tmp/private;",
        "unix:",
    ],
)
def test_bus_validation_rejects_whole_unsupported_list(address: str) -> None:
    assert resolve_bus_address({"DBUS_SESSION_BUS_ADDRESS": address}) is None


@pytest.mark.parametrize(
    "address",
    ["unix:path=/tmp/private", "unix:abstract=private", "unix:path=/a;unix:abstract=b"],
)
def test_local_address_list_is_supported(address: str) -> None:
    assert resolve_bus_address({"DBUS_SESSION_BUS_ADDRESS": address}) == address


def test_missing_environment_never_autolaunches_and_file_is_not_a_bus(tmp_path: Path) -> None:
    assert resolve_bus_address({}) is None
    (tmp_path / "bus").write_text("not a socket", encoding="utf-8")
    assert resolve_bus_address({"XDG_RUNTIME_DIR": str(tmp_path)}) is None


def test_runtime_socket_fallback_resolves_symlink(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(actual / "bus"))
        assert resolve_bus_address({"XDG_RUNTIME_DIR": str(link)}) == f"unix:path={actual}/bus"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  открой\r\nпапку\u0085загрузки\u2028сейчас\u2029 ", "открой папку загрузки сейчас"),
        ("от\u00adкрой\u2065 папку\U0001343f\U000e0020", "открой папку"),
        ("👩\u200d💻 \u200cтекст ❤️\ufe0e\ufe0f", "👩\u200d💻 \u200cтекст ❤️"),
        ("\u2800\u3164\u034f\u061c\ufff8", ""),
    ],
)
def test_unicode_normalization_is_explicit_and_preserves_legitimate_joins(
    text: str, expected: str
) -> None:
    assert normalize_text(text) == expected


@pytest.mark.parametrize("text", ["", " \u200c\u200d\ufe0f ", "a" * 4001, "\ud800"])
def test_semantically_empty_oversize_or_invalid_unicode_cannot_be_sent(text: str) -> None:
    assert not valid_text(normalize_text(text))


def test_codepoint_limit_includes_supplementary_emoji_as_one_character() -> None:
    assert valid_text("😀" * 4000)
