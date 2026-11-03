"""E13.M4 echo gate (D7): synthetic two-track mixes, no devices or files."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest

from astra_voice.meetings import echo_gate

pytestmark = pytest.mark.unit
RATE = echo_gate.SOURCE_RATE
LEAD = echo_gate.LAG_SAMPLES


def speech_like(seconds: float, seed: int) -> npt.NDArray[np.float32]:
    """Band-limited noise with a 4 Hz syllable envelope: correlates only with itself."""
    rng = np.random.default_rng(seed)
    n = round(seconds * RATE)
    noise = rng.standard_normal(n + 64)
    smooth = np.convolve(noise, np.ones(8) / 8, mode="valid")[:n]
    envelope = 0.55 + 0.45 * np.sin(2 * np.pi * 4 * np.arange(n) / RATE)
    signal = smooth * envelope
    result: npt.NDArray[np.float32] = (0.2 * signal / np.abs(signal).max()).astype(np.float32)
    return result


def mix(
    far_full: npt.NDArray[np.float32],
    lag_ms: int,
    gain: float,
    own: npt.NDArray[np.float32] | None,
    seed: int,
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.float32]]:
    """Mic window = delayed, attenuated far (+ own speech, + noise); far starts 400 ms earlier."""
    lag = lag_ms * RATE // 1000
    length = len(far_full) - LEAD
    echo = gain * far_full[LEAD - lag : LEAD - lag + length]
    rng = np.random.default_rng(seed)
    mic = echo + 0.0005 * rng.standard_normal(length)
    if own is not None:
        mic = mic + own[:length]
    return mic.astype(np.float32), far_full


def test_echo_with_120ms_lag_is_dropped() -> None:
    far = speech_like(4.4, 1)
    mic, far_window = mix(far, 120, 0.3, None, 2)
    decision = echo_gate.assess(mic, far_window, 0.9, enabled=True)
    assert decision.echo
    assert decision.max_ncc >= 0.9
    assert decision.lag_ms == pytest.approx(120, abs=1)
    assert decision.own_speech_ms < echo_gate.DOUBLE_TALK_MIN_MS


def test_independent_speech_is_kept() -> None:
    far = speech_like(4.4, 3)
    mic = speech_like(4.0, 4)
    decision = echo_gate.assess(mic, far, 0.9, enabled=True)
    assert not decision.echo and decision.max_ncc < echo_gate.THETA_ECHO


def test_double_talk_six_db_over_echo_is_kept_even_above_theta() -> None:
    far = speech_like(4.4, 5)
    length = len(far) - LEAD
    echo_rms = 0.3 * float(np.sqrt(np.mean(far.astype(np.float64) ** 2)))
    own = speech_like(4.0, 6)
    own[: length // 2] = 0  # owner talks over the echo in the second half only
    active = own[length // 2 :]
    own[length // 2 :] = active * (np.sqrt(8.0) * echo_rms / np.sqrt(np.mean(active**2)))
    mic, far_window = mix(far, 120, 0.3, own, 7)
    decision = echo_gate.assess(mic, far_window, 0.9, enabled=True)
    assert decision.max_ncc >= echo_gate.THETA_ECHO  # NCC alone would drop own speech
    assert decision.own_speech_ms >= echo_gate.DOUBLE_TALK_MIN_MS
    assert not decision.echo


def test_echo_needs_far_speech_overlap() -> None:
    far = speech_like(4.4, 8)
    mic, far_window = mix(far, 50, 0.3, None, 9)
    assert echo_gate.assess(mic, far_window, 0.9, enabled=True).echo
    assert not echo_gate.assess(mic, far_window, 0.4, enabled=True).echo


def test_disabled_flag_keeps_everything_and_default_is_off() -> None:
    far = speech_like(4.4, 10)
    mic, far_window = mix(far, 120, 0.3, None, 11)
    assert not echo_gate.assess(mic, far_window, 1.0, enabled=False).echo
    assert echo_gate.ECHO_GATE_DEFAULT is False  # D7 until M0 block F
    assert echo_gate.config()["theta"] == echo_gate.THETA_ECHO == 0.35


def test_short_and_silent_inputs_are_kept() -> None:
    assert not echo_gate.assess(
        np.zeros(0, dtype=np.float32), np.zeros(LEAD, dtype=np.float32), 1.0, enabled=True
    ).echo
    silent = np.zeros(RATE, dtype=np.float32)
    assert not echo_gate.assess(
        silent, np.zeros(RATE + LEAD, dtype=np.float32), 1.0, enabled=True
    ).echo
    # Missing far audio (shorter window) never correlates.
    assert not echo_gate.assess(
        speech_like(1.0, 12), np.zeros(10, dtype=np.float32), 1.0, enabled=True
    ).echo
