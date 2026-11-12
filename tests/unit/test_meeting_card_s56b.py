"""M7 presentation invariants: PlainText, palette, source-derived geometry and crops."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from test_meeting_lexicon import violations
from test_qml_onboarding_static import unsafe_qml_text_items

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]
FILES = [
    REPO / "qml/components" / f"{name}.qml"
    for name in (
        "MeetingTranscriptRow",
        "MeetingBadge",
        "MeetingParticipants",
        "MeetingSpeakerNumber",
        "MeetingMergePopover",
        "MeetingSpeakerToast",
        "MeetingSaveDialog",
        "MeetingSavePreviewRow",
        "AvSelect",
    )
] + [REPO / "qml/sections/MeetingCard.qml"]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_m7_data_is_plaintext_palette_is_themed_and_lexicon_is_public(path: Path) -> None:
    source = path.read_text()
    assert not unsafe_qml_text_items(source)
    assert not violations(path)
    assert not re.search(r"#[0-9a-fA-F]{3,8}|\bQt\.rgba\s*\(", source)


def test_generated_theme_matches_and_echo_alias_tracks_muted() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/gen_theme.py", "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    theme = (REPO / "qml/Theme.qml").read_text()
    assert "readonly property color transcriptEchoFg: fgMuted" in theme
    # Every original generated property continues to come from the unchanged tokens.
    spec = importlib.util.spec_from_file_location("_s56b_theme", REPO / "scripts/gen_theme.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = json.loads((REPO / "design/tokens.json").read_text())
    assert module.build_theme(data) == theme
    assert data["component"]["transcript"]["col-who-w"]["value"] == "124px"


def test_card_mark_layout_uses_bridge_roles_and_does_not_focus_flags() -> None:
    source = (REPO / "qml/components/MeetingTranscriptRow.qml").read_text()
    assert "width: Theme.transcriptColWhoW" in source
    assert "Math.max(speech.height, whoColumn.height + Theme.transcriptWhoOffsetTop)" in source
    assert "spacing: Theme.spaceStep" in source
    assert "root.textMuted ? Theme.transcriptEchoFg : Theme.fg" in source
    assert "Accessible.name: accessibleName" in source
    assert "Accessible.ignored: true" in source
    assert "wrapMode: Text.WordWrap" in source


def test_snapshot_states_have_real_crops_or_explicit_missing_evidence() -> None:
    sys.path.insert(0, str(REPO))
    from tools.render_meeting_card import MISSING_REFERENCES, REFERENCE_BOUNDS, visual_states

    states = visual_states()
    assert {
        "ready-1024",
        "ready-900",
        "names-edit",
        "merge",
        "merged",
        "transcribing-1024",
        "room",
        "no-diarization",
        "models-missing",
        "policy-off",
        "marks-1024",
        "marks-900",
        "save-echo",
        "save-echo-checked",
        "save-statistics",
    } <= states.keys()
    section_states = {
        "section-group-missing",
        "section-missing",
        "section-downloading",
        "section-installed",
        "section-failed-admin",
        "section-offline",
    }
    assert states.keys() | section_states == REFERENCE_BOUNDS.keys() | MISSING_REFERENCES.keys()
    assert {
        "diar-missing",
        "diar-downloading",
        "diar-installed",
        "diar-failed-admin-900",
    } <= states.keys()
    missing = states["models-missing"]
    assert len(missing["utterances"]) == 7
    assert not missing["meeting"].marks and not missing["meeting"].intervals
    assert all(REFERENCE_BOUNDS[name][0] == "09-diar-install" for name in section_states)
    assert all(
        (REPO / f"design/meetings/refs/{sheet}-light.png").is_file()
        for sheet, *_ in REFERENCE_BOUNDS.values()
    )
