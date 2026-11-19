"""D4 unload RSS regression in a fresh process, with real ASR/VAD and a 27 s deadline."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from astra_voice.worker.engine import LAYOUTS

pytestmark = [pytest.mark.engine, pytest.mark.usefixtures("real_heap_trim")]
ROOT = Path(__file__).resolve().parents[2]
LAYOUT = "onnx-asr-gigaam-v3"
VARIANT = "gigaam-v3-e2e-rnnt"

# A new process prevents an earlier engine test's resident allocator/imports
# from inflating the baseline and hiding a failed unload. No threshold warm-up.
PROBE = r"""
import json, sys, time, wave
from pathlib import Path
import numpy as np
from astra_voice.core import paths
from astra_voice.worker import vad
from astra_voice.worker.engine import CancelToken, make_engine, active_sessions
from astra_voice.worker.meeting_transcribe import MeetingTranscribeState, _status_kb
paths.data_dir_static = lambda: Path(sys.argv[2]) / 'data'
state = MeetingTranscribeState(engine_factory=make_engine)
def send(kind, **fields):
    state.handle(dict(type=kind, req=kind.replace('.', '_'), request_type=kind, **fields))
def wait(kind):
    until = time.monotonic() + 24
    while time.monotonic() < until:
        for event in state.poll(16):
            assert event['type'] != 'error', event
            if event['type'] == kind:
                return
        time.sleep(.001)
    raise TimeoutError(kind)
try:
    before = _status_kb('VmRSS')
    send('model.load', id='gigaam-v3', revision='rss', dir=sys.argv[1],
         layout='onnx-asr-gigaam-v3', variant='gigaam-v3-e2e-rnnt', threads=2, min_ram_mb=700)
    wait('model.loaded')
    with wave.open(str(Path(sys.argv[2]) / 'data/test/test-ru-6s.wav'), 'rb') as source:
        raw = source.readframes(source.getnframes())
        audio = np.frombuffer(raw, dtype='<i2').astype(np.float32)
        del raw
    audio /= 32768
    assert vad.vad_available()
    assert vad.speech_probabilities(audio) is not None
    assert state.engine is not None
    result = state.engine.transcribe(audio, CancelToken())
    assert not result.cancelled
    loaded = _status_kb('VmRSS')
    del audio, result
    send('model.unload')
    wait('model.unloaded')
    after = _status_kb('VmRSS')
    assert state.engine is None and vad._session is None and active_sessions() == 0
    print(json.dumps(dict(before_kb=before, loaded_kb=loaded, after_kb=after)), flush=True)
finally:
    state.cleanup()
"""


def test_unload_ack_rss_with_real_gigaam() -> None:
    pytest.importorskip("onnxruntime")
    pytest.importorskip("onnx_asr")
    if not Path("/proc/self/status").is_file():
        pytest.skip("VmRSS requires Linux /proc")
    model = Path(
        os.environ.get(
            "ASTRA_VOICE_TEST_MODEL_DIR", "~/.cache/astra-voice-spike/gigaam-v3/e2e_rnnt"
        )
    ).expanduser()
    for name in LAYOUTS[LAYOUT][VARIANT].required:
        if not (model / name).is_file():
            pytest.skip(f"GigaAM model file missing: {name}")
    for relative in ("data/vad/silero_vad.onnx", "data/test/test-ru-6s.wav"):
        if not (ROOT / relative).is_file():
            pytest.skip(f"Engine fixture missing: {relative}")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), env.get("PYTHONPATH", "")))
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["OMP_NUM_THREADS"] = "2"
    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(model), str(ROOT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=27,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    values = json.loads(result.stdout.splitlines()[-1])
    print(json.dumps(values))
    assert values["loaded_kb"] > values["before_kb"] + 150 * 1024, values
    assert 0 < values["after_kb"] <= values["before_kb"] + 150 * 1024, values
