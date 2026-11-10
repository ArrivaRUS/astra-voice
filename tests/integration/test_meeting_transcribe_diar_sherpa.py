"""Full transcriber job with fake ASR and the real isolated diar role (M7).

Absent runtime/corpus/models skip like test_meeting_diar_sherpa; denied IPC
is a failure and must be run by Юрка outside the Codex sandbox.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tools.meeting_transcribe_diar_check import prepare_meeting, run_job

from astra_voice.meetings.model import Transcript

pytestmark = [pytest.mark.engine, pytest.mark.usefixtures("filesystem_euid")]


@pytest.fixture(scope="module")
def inputs() -> tuple[dict[str, str], Path]:
    pytest.importorskip("sherpa_onnx", reason="Для диаризации нужен sherpa-onnx")
    names = ("ASTRA_VOICE_DIAR_MODELS", "ASTRA_VOICE_DIAR_CORPUS")
    if any(not os.environ.get(name) for name in names):
        pytest.skip("Задайте ASTRA_VOICE_DIAR_MODELS и ASTRA_VOICE_DIAR_CORPUS")
    models, corpus = (Path(os.environ[name]) for name in names)
    files = [models / name for name in ("segmentation.onnx", "embedder.onnx")]
    files += [corpus / name / "far.wav" for name in ("dion", "synth")]
    if any(not path.is_file() for path in files):
        pytest.skip("Неполный комплект моделей или корпуса диаризации")
    # Corpus models are symlinks. Resolve in the test driver, preserving the
    # role's production rejection of symlink directories/files.
    return {
        "segmentation_dir": str(files[0].resolve(strict=True).parent),
        "embedder_dir": str(files[1].resolve(strict=True).parent),
    }, corpus


@pytest.mark.parametrize("corpus_name,seconds", [("dion", 300), ("synth", 1260)])
def test_complete_job_labels_and_real_role_exit(
    inputs: tuple[dict[str, str], Path], tmp_path: Path, corpus_name: str, seconds: int
) -> None:
    models, corpus = inputs
    store, mid = prepare_meeting(
        tmp_path, {"far": corpus / corpus_name / "far.wav"}, duration_s=seconds
    )
    result = run_job(store, mid, models, fake_asr=True)
    assert result["diar_status"] == "done", result
    assert result["labels_final"] and result["speakers"] > 0, result
    assert result["blocks"] == (2 if corpus_name == "synth" else 1), result
    transcript = store.read(mid, "transcript.json")
    assert isinstance(transcript, Transcript) and transcript.utterances
    assert all(u.speaker.startswith("S") for u in transcript.utterances)
    assert not (store.root / mid / "checkpoint.json").exists()
    assert result["diar_peak_rss_kb"] > 0
