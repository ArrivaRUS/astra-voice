"""Real signed fixture acceptance, revocation by bytes, and child-process access."""

from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

import pytest

from helpers.meeting_trust import RAW_MODEL, ROOT, SignedDiarPair

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("filesystem_euid")]


def test_signed_pair_fixture_checks_signature_and_model_bytes(signed_diar_pair: Any) -> None:
    pair: SignedDiarPair = signed_diar_pair
    models = pair.models()
    assert models is not None
    model = Path(models["segmentation_dir"]) / "model.onnx"
    model.write_bytes(b"\x08\x02")
    assert pair.models() is None
    model.write_bytes(RAW_MODEL)
    assert pair.models() == models
    catalog = pair.root / "catalog/catalog.json"
    catalog.write_bytes(catalog.read_bytes() + b"\n")
    assert pair.models() is None


def test_subprocess_uses_real_signed_pair_provider(signed_diar_pair: Any) -> None:
    pair: SignedDiarPair = signed_diar_pair
    script = """
import json, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1] + '/src', sys.argv[1] + '/tests']
from astra_voice.worker.meeting_transcribe import MeetingTranscribeState
from astra_voice.meetings.store import MeetingStore
from helpers.meeting_trust import SignedDiarPair
pair = SignedDiarPair(Path(sys.argv[2]))
state = MeetingTranscribeState(store=MeetingStore(pair.root / 'meetings'),
                               diar_models_provider=pair.models)
try:
    print(json.dumps(state.diar_models_provider(), sort_keys=True))
finally:
    state.cleanup()
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(ROOT), str(pair.root)],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert json.loads(result.stdout) == pair.models()
    assert json.loads(result.stdout) is not None


def test_cli_pipeline_with_real_signed_pair_provider(
    signed_diar_pair: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import meeting_transcribe_diar_check as check

    from astra_voice.worker.meeting_transcribe import MeetingTranscribeState

    pair: SignedDiarPair = signed_diar_pair
    models = pair.models()
    assert models is not None
    loaded: list[dict[str, str]] = []

    class Peer:
        peak_rss_kb = 1234
        overall_deadline = float("inf")

        def load(self, selected: dict[str, str]) -> None:
            assert selected == pair.models() == models
            loaded.append(dict(selected))

        def block(self, audio: Any, index: int, offset: int) -> list[list[Any]]:
            return [[offset, offset + len(audio) // 16, "S1"]]

        def finish(self) -> dict[str, Any]:
            return {"label_map": {"S1": "S1"}, "speakers_total": 1}

        def close(self, *, completed: bool = False) -> bool:
            return completed

    def factory(**options: Any) -> MeetingTranscribeState:
        return MeetingTranscribeState(
            **options,
            diar_models_provider=pair.models,
            diar_factory=lambda *args, **kwargs: Peer(),
            mem_available_mb=lambda: 100_000,
        )

    monkeypatch.setattr(check, "MeetingTranscribeState", factory)
    wav = tmp_path / "room.wav"
    with wave.open(str(wav), "wb") as output:
        output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x40" * 16000)
    store, mid = check.prepare_meeting(tmp_path / "run", {"mic": wav})
    result = check.run_job(store, mid, models, fake_asr=True)
    assert result["diar_status"] == "done" and result["labels_final"]
    assert loaded == [models]
