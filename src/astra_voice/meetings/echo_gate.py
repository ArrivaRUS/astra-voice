"""Echo gate over two recorded tracks: NCC mic<->far per utterance (D7, SP2 port).

A pure function over PCM already on disk: no devices, files or calibration
capture (T1 §4.2 п. 2). Every tunable lives here so M0 (laptop block F) can
switch the default or move a threshold with one number. Until block F is
measured the gate is OFF by default (D7: no measurement -> disabled).

Decision for one microphone utterance (16 kHz, aligned on the recorded axis):
both tracks are low-passed and decimated to 4 kHz; normalized cross-correlation
is searched over mic delays 0..400 ms relative to the far track already aligned
by the timeline. ``echo`` requires max NCC >= THETA_ECHO **and** far speech
covering >= FAR_OVERLAP_MIN of the utterance, unless double talk is detected:
at least DOUBLE_TALK_MIN_MS of active microphone frames are >= DOUBLE_TALK_DB
louder than the least-squares echo estimate (own speech integrity, S2.2).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

ECHO_GATE_DEFAULT = False  # D7: switched by M0 block F (own-speech loss <= 2 %)
THETA_ECHO = 0.35  # SP2 starting point; calibrated on the laptop in M0
MAX_LAG_MS = 400
FAR_OVERLAP_MIN = 0.5
DOUBLE_TALK_DB = 6.0
DOUBLE_TALK_MIN_MS = 500
SOURCE_RATE = 16000
NCC_RATE = 4000
FRAME_MS = 100
ACTIVE_FLOOR_DB = -20.0  # frames quieter than p90 - 20 dB never count as own speech
_DECIMATION = SOURCE_RATE // NCC_RATE
_HALF_TAPS = 16
_CUTOFF_HZ = 1700.0
LAG_SAMPLES = MAX_LAG_MS * SOURCE_RATE // 1000  # far lead needed at 16 kHz

Audio = npt.NDArray[np.float32]
Signal = npt.NDArray[np.float64]


def _taps() -> Signal:
    """33-tap Hamming-windowed sinc, cutoff 1.7 kHz before 4:1 decimation (SP2)."""
    k = np.arange(-_HALF_TAPS, _HALF_TAPS + 1, dtype=np.float64)
    cutoff = _CUTOFF_HZ / SOURCE_RATE
    safe = np.where(k == 0, 1.0, k)
    sinc = np.where(k == 0, 2 * cutoff, np.sin(2 * math.pi * cutoff * k) / (math.pi * safe))
    taps = sinc * (0.54 + 0.46 * np.cos(math.pi * k / _HALF_TAPS))
    result: Signal = taps / taps.sum()
    return result


_TAPS = _taps()


def config() -> dict[str, float | int | bool]:
    """Every number that changes decisions; part of processing_config_sha256."""
    return {
        "theta": THETA_ECHO,
        "max_lag_ms": MAX_LAG_MS,
        "far_overlap_min": FAR_OVERLAP_MIN,
        "double_talk_db": DOUBLE_TALK_DB,
        "double_talk_min_ms": DOUBLE_TALK_MIN_MS,
        "frame_ms": FRAME_MS,
        "active_floor_db": ACTIVE_FLOOR_DB,
        "ncc_rate": NCC_RATE,
    }


def decimate(audio: Audio | Signal) -> Signal:
    """Zero-padded FIR keeps the clock origin: output j <-> input 4j."""
    x = np.asarray(audio, dtype=np.float64)
    if not len(x):
        return np.zeros(0, dtype=np.float64)
    padded = np.concatenate((np.zeros(_HALF_TAPS), x, np.zeros(_HALF_TAPS)))
    filtered: Signal = np.convolve(padded, _TAPS, mode="valid")[::_DECIMATION]
    return filtered


def ncc(mic: Signal, far: Signal, max_lag: int) -> tuple[float, int]:
    """Max NCC over mic delays 0..max_lag; ``far`` leads mic by max_lag samples.

    far[i] is the far sample ``max_lag`` positions before mic[i]; it must hold
    len(mic) + max_lag samples (zeros where the far track has no audio).
    """
    x = np.asarray(mic, dtype=np.float64)
    n = len(x)
    if not n or len(far) != n + max_lag:
        return 0.0, 0
    x = x - x.mean()
    xx = float(np.dot(x, x))
    if xx < 1e-12:
        return 0.0, 0
    y = np.asarray(far, dtype=np.float64)
    size = 1 << (len(y) + n - 2).bit_length()
    dots = np.fft.irfft(np.fft.rfft(y, size) * np.fft.rfft(x[::-1], size), size)
    sums = np.concatenate(([0.0], np.cumsum(y)))
    squares = np.concatenate(([0.0], np.cumsum(y * y)))
    offsets = max_lag - np.arange(max_lag + 1)
    sy = sums[offsets + n] - sums[offsets]
    yy = squares[offsets + n] - squares[offsets] - sy * sy / n
    scores = np.zeros(max_lag + 1)
    valid = yy > 1e-12
    scores[valid] = dots[offsets[valid] + n - 1] / np.sqrt(xx * yy[valid])
    lag = int(np.argmax(scores))
    return float(np.clip(scores[lag], 0.0, 1.0)), lag


@dataclass(frozen=True)
class EchoDecision:
    echo: bool
    max_ncc: float
    lag_ms: float
    far_overlap: float
    own_speech_ms: int


def _own_speech_ms(x: Signal, y: Signal, lag: int, max_lag: int) -> int:
    """Active mic frames >= DOUBLE_TALK_DB above the least-squares echo estimate."""
    n = len(x)
    segment = y[max_lag - lag : max_lag - lag + n]
    xc = x - x.mean()
    yc = segment - segment.mean()
    power = float(np.dot(yc, yc))
    echo = yc * (float(np.dot(xc, yc)) / power) if power > 1e-12 else np.zeros(n)
    frame = NCC_RATE * FRAME_MS // 1000
    count = n // frame
    if not count:
        return 0
    mic_energy = (xc[: count * frame] ** 2).reshape(count, frame).sum(axis=1)
    echo_energy = (echo[: count * frame] ** 2).reshape(count, frame).sum(axis=1)
    floor = float(np.percentile(mic_energy, 90)) * 10 ** (ACTIVE_FLOOR_DB / 10)
    active = mic_energy > max(floor, 1e-12)
    own = active & (mic_energy >= echo_energy * 10 ** (DOUBLE_TALK_DB / 10))
    return int(own.sum()) * FRAME_MS


def assess(mic: Audio, far: Audio, far_overlap: float, *, enabled: bool) -> EchoDecision:
    """One mic utterance at 16 kHz; ``far`` starts MAX_LAG_MS earlier on the same axis.

    ``far`` should hold len(mic) + LAG_SAMPLES samples; missing far audio is
    zero (silence never correlates). ``enabled=False`` keeps every utterance.
    """
    if not enabled or not len(mic):
        return EchoDecision(False, 0.0, 0.0, far_overlap, 0)
    x = decimate(mic)
    max_lag = LAG_SAMPLES // _DECIMATION
    wanted = len(mic) + LAG_SAMPLES
    lead = np.zeros(wanted, dtype=np.float64)
    lead[: min(wanted, len(far))] = np.asarray(far[:wanted], dtype=np.float64)
    y = decimate(lead)[: len(x) + max_lag]
    if len(y) < len(x) + max_lag:
        y = np.concatenate((y, np.zeros(len(x) + max_lag - len(y))))
    score, lag = ncc(x, y, max_lag)
    own = _own_speech_ms(x, y, lag, max_lag) if score >= THETA_ECHO else 0
    echo = score >= THETA_ECHO and far_overlap >= FAR_OVERLAP_MIN and own < DOUBLE_TALK_MIN_MS
    return EchoDecision(echo, score, lag * 1000 / NCC_RATE, far_overlap, own)
