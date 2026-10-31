"""Endpoint presentation roles select approved glyph/color tokens, never DNS."""

from __future__ import annotations

import pytest

from astra_voice.core.policy import Policy, PolicyStatus
from astra_voice.core.settings import Settings
from astra_voice.ui.meeting_bridge import MeetingBridge
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def qt_application() -> None:
    get_qapplication()


@pytest.mark.parametrize(
    "endpoint,raw,offline,kind",
    [
        ("", {}, False, "none"),
        ("https://127.0.0.1/v1", {}, False, "loopback"),
        ("https://10.0.0.4/v1", {}, False, "internal"),
        ("https://service.example/v1", {}, False, "external"),
        ("https://service.example/v1", {}, True, "offline"),
        ("https://service.example/v1", {"profile": "secure"}, False, "offline"),
        (
            "",
            {
                "meeting_ai_endpoint": "https://service.example/v1",
                "meeting_ai_allow_external": "true",
            },
            False,
            "external-admin",
        ),
        (
            "",
            {"meeting_ai_endpoint": "http://10.0.0.4/v1", "meeting_ai_allow_plaintext": "on"},
            False,
            "plaintext",
        ),
    ],
)
def test_endpoint_kind_uses_effective_policy_without_network(
    endpoint: str, raw: dict[str, str], offline: bool, kind: str
) -> None:
    bridge = MeetingBridge(
        Settings(meeting_ai_endpoint=endpoint),
        policy=Policy(raw=raw, status=PolicyStatus.OK),
        mirror=Settings(offline=offline),
        known_apps=(),
    )
    try:
        assert bridge.endpointKind == kind
        assert bool(bridge.endpointCaption) == (kind != "none")
    finally:
        bridge.shutdown()
