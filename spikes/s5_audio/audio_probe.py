#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S5 — спайк «захват звука» (M0.S5 → worker/audio.py).

Проверяет развилку Р1 синтез-плана: захват живёт **в воркере**, PCM не покидает процесс,
`libpulse-simple.so.0` дёргается через `ctypes`.

Что доказывает:
  * `pa_simple_new(PA_STREAM_RECORD, s16le/16 кГц/1, device=…)` открывает конкретный
    источник **по имени** (default source не трогаем);
  * чтение кусками 20 мс, кольцевой буфер в ОЗУ, RMS/пик в дБFS;
  * «тишина» = пик < −60 дБFS непрерывно 2 с;
  * закрытие и повторное открытие устройства (И4: `audio.close` → `audio.ready`);
  * CPU потока чтения (по `/proc/self/task/<tid>/stat`);
  * распознавание **прямо из памяти** (`onnx_asr` принимает `np.float32`), ни одного
    временного файла на диске.

**ГРАБЛИ (урок S1):** у каждой функции `libpulse` прописаны `argtypes`/`restype`.
Без `restype = c_void_p` указатель `pa_simple*` усекается до 32 бит → SIGSEGV.

Запуск (нужен виртуальный источник, см. README):
    ~/.cache/astra-voice-spikes/venv/bin/python audio_probe.py \\
        --device av_test_src --seconds 4 --asr --json out/capture.json
"""

from __future__ import annotations

import argparse
import array
import ctypes
import ctypes.util
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque

# --- константы формата (plans.md M3 / plan-claude §3) -----------------------
RATE = 16000
CHANNELS = 1
SAMPLE_BYTES = 2                       # s16le
CHUNK_MS = 20
CHUNK_BYTES = RATE * CHANNELS * SAMPLE_BYTES * CHUNK_MS // 1000     # 640
SILENCE_DBFS = -60.0
SILENCE_HOLD_S = 2.0
LIMIT_S_DEFAULT = 120.0
OPEN_RETRIES = 3
OPEN_RETRY_MS = 300

PA_SAMPLE_S16LE = 3
PA_STREAM_RECORD = 2
U32_MAX = 0xFFFFFFFF


class PaSampleSpec(ctypes.Structure):
    _fields_ = [("format", ctypes.c_int),
                ("rate", ctypes.c_uint32),
                ("channels", ctypes.c_uint8)]


class PaBufferAttr(ctypes.Structure):
    _fields_ = [("maxlength", ctypes.c_uint32),
                ("tlength", ctypes.c_uint32),
                ("prebuf", ctypes.c_uint32),
                ("minreq", ctypes.c_uint32),
                ("fragsize", ctypes.c_uint32)]


class Pulse:
    """ctypes-обёртка над libpulse-simple. ВСЕ функции с argtypes/restype."""

    def __init__(self) -> None:
        path = "libpulse-simple.so.0"
        self.lib = ctypes.CDLL(path, use_errno=True)
        self.libpulse = ctypes.CDLL("libpulse.so.0", use_errno=True)
        self.so_path = path

        self.lib.pa_simple_new.argtypes = [
            ctypes.c_char_p,                    # server
            ctypes.c_char_p,                    # name
            ctypes.c_int,                       # dir
            ctypes.c_char_p,                    # dev
            ctypes.c_char_p,                    # stream_name
            ctypes.POINTER(PaSampleSpec),       # ss
            ctypes.c_void_p,                    # channel map
            ctypes.POINTER(PaBufferAttr),       # attr
            ctypes.POINTER(ctypes.c_int),       # error
        ]
        self.lib.pa_simple_new.restype = ctypes.c_void_p          # ← без этого SIGSEGV

        self.lib.pa_simple_read.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_size_t,
                                            ctypes.POINTER(ctypes.c_int)]
        self.lib.pa_simple_read.restype = ctypes.c_int

        self.lib.pa_simple_flush.argtypes = [ctypes.c_void_p,
                                             ctypes.POINTER(ctypes.c_int)]
        self.lib.pa_simple_flush.restype = ctypes.c_int

        self.lib.pa_simple_get_latency.argtypes = [ctypes.c_void_p,
                                                   ctypes.POINTER(ctypes.c_int)]
        self.lib.pa_simple_get_latency.restype = ctypes.c_uint64

        self.lib.pa_simple_free.argtypes = [ctypes.c_void_p]
        self.lib.pa_simple_free.restype = None

        self.libpulse.pa_strerror.argtypes = [ctypes.c_int]
        self.libpulse.pa_strerror.restype = ctypes.c_char_p

    def strerror(self, code: int) -> str:
        s = self.libpulse.pa_strerror(code)
        return s.decode("utf-8", "replace") if s else "код %d" % code


class PulseSimpleSource:
    """`AudioSource` из плана #1 §3: открыть по имени, читать 20 мс, закрыть."""

    def __init__(self, pa: Pulse, device: str | None) -> None:
        self.pa = pa
        self.device = device
        self.handle = None
        self.open_ms = None
        self.last_error = None

    def open(self) -> bool:
        ss = PaSampleSpec(PA_SAMPLE_S16LE, RATE, CHANNELS)
        attr = PaBufferAttr(U32_MAX, U32_MAX, U32_MAX, U32_MAX, CHUNK_BYTES)
        err = ctypes.c_int(0)
        t0 = time.monotonic()
        for attempt in range(1, OPEN_RETRIES + 1):
            h = self.pa.lib.pa_simple_new(
                None, b"astra-voice-spike-s5", PA_STREAM_RECORD,
                self.device.encode() if self.device else None,
                b"dictation", ctypes.byref(ss), None, ctypes.byref(attr),
                ctypes.byref(err))
            if h:
                self.handle = ctypes.c_void_p(h)
                self.open_ms = round((time.monotonic() - t0) * 1000, 1)
                self.attempts = attempt
                return True
            self.last_error = self.pa.strerror(err.value)
            time.sleep(OPEN_RETRY_MS / 1000.0)
        self.attempts = OPEN_RETRIES
        self.open_ms = round((time.monotonic() - t0) * 1000, 1)
        return False

    def read(self, buf) -> bool:
        err = ctypes.c_int(0)
        rc = self.pa.lib.pa_simple_read(self.handle, buf, CHUNK_BYTES,
                                        ctypes.byref(err))
        if rc < 0:
            self.last_error = self.pa.strerror(err.value)
            return False
        return True

    def latency_us(self) -> int:
        err = ctypes.c_int(0)
        return int(self.pa.lib.pa_simple_get_latency(self.handle, ctypes.byref(err)))

    def close(self) -> None:
        if self.handle:
            self.pa.lib.pa_simple_free(self.handle)
            self.handle = None


def dbfs(peak: int) -> float:
    if peak <= 0:
        return -120.0
    return round(20.0 * math.log10(peak / 32768.0), 1)


def thread_cpu_s(tid: int) -> float:
    """utime+stime потока в секундах (поля 14/15 /proc/<tid>/stat)."""
    with open("/proc/self/task/%d/stat" % tid, encoding="ascii") as fh:
        parts = fh.read().rsplit(") ", 1)[1].split()
    hz = os.sysconf("SC_CLK_TCK")
    return (int(parts[11]) + int(parts[12])) / hz


class Capture:
    """Поток чтения + кольцевой буфер в ОЗУ. Ни одного файла на диске."""

    def __init__(self, src: PulseSimpleSource, limit_s: float) -> None:
        self.src = src
        self.limit_bytes = int(limit_s * RATE * CHANNELS * SAMPLE_BYTES)
        self.ring = deque()
        self.ring_bytes = 0
        self.levels = []           # (t, rms_dbfs, peak_dbfs)
        self.stop_flag = threading.Event()
        self.silent_at = None
        self.limit_at = None
        self.cpu_s = None
        self.tid = None
        self.chunks = 0
        self.error = None

    def _loop(self) -> None:
        self.tid = threading.get_native_id()
        cpu0 = thread_cpu_s(self.tid)
        buf = (ctypes.c_char * CHUNK_BYTES)()
        t0 = time.monotonic()
        silence_since = t0
        while not self.stop_flag.is_set():
            if not self.src.read(buf):
                self.error = self.src.last_error
                break
            raw = bytes(buf)
            self.chunks += 1
            self.ring.append(raw)
            self.ring_bytes += len(raw)
            while self.ring_bytes > self.limit_bytes:      # кольцо: лимит 120 с
                self.ring_bytes -= len(self.ring.popleft())
            samples = array.array("h")
            samples.frombytes(raw)
            peak = max(abs(s) for s in samples) if samples else 0
            rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0
            now = time.monotonic()
            self.levels.append((round(now - t0, 3), dbfs(int(rms)), dbfs(peak)))
            if dbfs(peak) >= SILENCE_DBFS:
                silence_since = now
            elif self.silent_at is None and now - silence_since >= SILENCE_HOLD_S:
                self.silent_at = round(now - t0, 3)
            if self.limit_at is None and now - t0 >= self.limit_s_guard():
                self.limit_at = round(now - t0, 3)
        self.cpu_s = round(thread_cpu_s(self.tid) - cpu0, 4)
        self.wall_s = round(time.monotonic() - t0, 3)

    def limit_s_guard(self) -> float:
        return self.limit_bytes / (RATE * CHANNELS * SAMPLE_BYTES)

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="pa-read", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_flag.set()
        self.thread.join(timeout=3.0)

    def pcm(self) -> bytes:
        return b"".join(self.ring)


def list_sources() -> list:
    """`pactl list short sources` — как в plans.md M3 (мониторы помечаем явно)."""
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:
        return [{"error": str(exc)}]
    rows = []
    for line in out.strip().splitlines():
        f = line.split("\t")
        if len(f) >= 2:
            rows.append({"index": f[0], "name": f[1],
                         "monitor": f[1].endswith(".monitor")})
    return rows


def assert_device_exists(device: str) -> tuple:
    """НАХОДКА S5 (2026-09-09), критично для У39/T-35.

    `pa_simple_new` с НЕСУЩЕСТВУЮЩИМ именем устройства **не падает**: сервер молча
    подставляет источник по умолчанию — то есть реальный микрофон пользователя.
    В `pa_simple` нет способа спросить, что открылось на самом деле
    (`pa_stream_get_device_name` есть только в асинхронном API).

    Значит `worker/audio.py` обязан проверять имя ДО открытия и отказываться,
    если его нет в списке источников (fail closed).
    """
    names = [s.get("name") for s in list_sources()]
    return (device in names), names


def run_asr(pcm: bytes, model_dir: str, model: str, threads: int,
            quantization: str | None = "int8") -> dict:
    """Распознавание ИЗ ПАМЯТИ: PCM → np.float32 → onnx_asr. Файлов не создаём."""
    import numpy as np
    import onnx_asr
    import onnxruntime as rt

    so = rt.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    t0 = time.perf_counter()
    m = onnx_asr.load_model(model, os.path.expanduser(model_dir),
                            quantization=quantization, sess_options=so)
    load_ms = (time.perf_counter() - t0) * 1000
    wav = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    t1 = time.perf_counter()
    text = m.recognize(wav, sample_rate=RATE)
    infer_ms = (time.perf_counter() - t1) * 1000
    return {"model": model, "quantization": quantization, "load_ms": round(load_ms, 1),
            "infer_ms": round(infer_ms, 1), "samples": int(wav.size),
            "duration_s": round(wav.size / RATE, 3), "text": text}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=None, help="имя источника (default source НЕ трогаем)")
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--limit-s", type=float, default=LIMIT_S_DEFAULT)
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--reopen", action="store_true", help="закрыть и открыть заново")
    ap.add_argument("--asr", action="store_true")
    ap.add_argument("--model-dir", default="~/.cache/astra-voice-spikes/gigaam-v3/e2e_rnnt")
    ap.add_argument("--model", default="gigaam-v3-e2e-rnnt")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--quantization", default="int8",
                    help="в scratch-каталоге S3 лежат только *.int8.onnx")
    ap.add_argument("--json", default="")
    ap.add_argument("--strict-device", dest="strict_device", action="store_true",
                    default=True, help="отказ, если имени нет среди источников (по умолчанию)")
    ap.add_argument("--no-strict-device", dest="strict_device", action="store_false",
                    help="намеренно воспроизвести молчаливую подмену устройства")
    args = ap.parse_args()

    report = {"format": {"rate": RATE, "channels": CHANNELS, "sample": "s16le",
                         "chunk_ms": CHUNK_MS, "chunk_bytes": CHUNK_BYTES},
              "device_requested": args.device,
              "silence_dbfs": SILENCE_DBFS, "silence_hold_s": SILENCE_HOLD_S}

    if args.list_devices:
        report["sources"] = list_sources()
        for s in report["sources"]:
            print("  %-4s %-60s %s" % (s.get("index"), s.get("name"),
                                       "MONITOR" if s.get("monitor") else ""))

    pa = Pulse()
    report["lib"] = pa.so_path

    if args.device and args.strict_device:
        exists, names = assert_device_exists(args.device)
        report["device_check"] = {"exists": exists, "known": len(names)}
        if not exists:
            print("ОТКАЗ: источника %r нет в `pactl list short sources`.\n"
                  "       Открывать нельзя: pa_simple молча подставит источник по "
                  "умолчанию (реальный микрофон). Снять проверку: --no-strict-device"
                  % args.device)
            if args.json:
                json.dump(report, open(args.json, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=2)
            return 3

    src = PulseSimpleSource(pa, args.device)
    if not src.open():
        report["open"] = {"ok": False, "error": src.last_error,
                          "attempts": src.attempts, "ms": src.open_ms}
        print("ОТКРЫТЬ НЕ УДАЛОСЬ: %s (попыток %d)" % (src.last_error, src.attempts))
        if args.json:
            json.dump(report, open(args.json, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        return 2
    report["open"] = {"ok": True, "attempts": src.attempts, "ms": src.open_ms,
                      "latency_us": src.latency_us()}
    print("открыт %r за %.1f мс (попыток %d), латентность %d мкс"
          % (args.device, src.open_ms, src.attempts, report["open"]["latency_us"]))

    cap = Capture(src, args.limit_s)
    cap.start()
    time.sleep(args.seconds)
    cap.stop()
    src.close()

    peaks = [p for _, _, p in cap.levels]
    report["capture"] = {
        "chunks": cap.chunks, "wall_s": cap.wall_s,
        "pcm_bytes": cap.ring_bytes,
        "pcm_seconds": round(cap.ring_bytes / (RATE * SAMPLE_BYTES), 3),
        "peak_dbfs_max": max(peaks) if peaks else None,
        "peak_dbfs_median": sorted(peaks)[len(peaks) // 2] if peaks else None,
        "silent_at_s": cap.silent_at,
        "limit_at_s": cap.limit_at,
        "read_thread_cpu_s": cap.cpu_s,
        "read_thread_cpu_pct": (round(100.0 * cap.cpu_s / cap.wall_s, 2)
                                if cap.cpu_s is not None and cap.wall_s else None),
        "error": cap.error,
    }
    c = report["capture"]
    print("чтений %d за %.2f с · PCM %d Б (%.2f с) · пик max %s дБFS, медиана %s дБFS"
          % (c["chunks"], c["wall_s"], c["pcm_bytes"], c["pcm_seconds"],
             c["peak_dbfs_max"], c["peak_dbfs_median"]))
    print("тишина (пик < %.0f дБFS %.0f с): %s" %
          (SILENCE_DBFS, SILENCE_HOLD_S,
           "ДА, на %.3f с" % c["silent_at_s"] if c["silent_at_s"] is not None else "нет"))
    print("CPU потока чтения: %.4f с = %.2f %% от %.2f с"
          % (c["read_thread_cpu_s"], c["read_thread_cpu_pct"], c["wall_s"]))

    if args.reopen:
        src2 = PulseSimpleSource(pa, args.device)
        ok = src2.open()
        report["reopen"] = {"ok": ok, "ms": src2.open_ms, "attempts": src2.attempts,
                            "error": src2.last_error}
        print("повторное открытие: ok=%s за %.1f мс (попыток %d)"
              % (ok, src2.open_ms, src2.attempts))
        src2.close()

    if args.asr:
        report["asr"] = run_asr(cap.pcm(), args.model_dir, args.model, args.threads,
                                args.quantization)
        print("РАСПОЗНАНО (%.2f с аудио, %.0f мс инференса): %r"
              % (report["asr"]["duration_s"], report["asr"]["infer_ms"],
                 report["asr"]["text"]))

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
