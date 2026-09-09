#!/usr/bin/env python3
"""Замер модели распознавания (onnx-asr + onnxruntime).

Холодная загрузка, p50/p95, RTFx, RSS, отмена.

Написан для спайка M0.S3 и переиспользуется в M2 (`tools/benchmark`, `worker/measure.py`).
Зависимости: только `onnxruntime`, `onnx-asr`, системный `numpy` — ничего сверх venv рантайма.

Примеры:
    python3 scripts/measure_model.py --model-dir ~/.cache/astra-voice-spike/gigaam-v3/e2e_rnnt \
        --model gigaam-v3-e2e-rnnt --wav data/test/test-ru-6s.wav --runs 10 --threads 1,2,4
    python3 scripts/measure_model.py --model-dir ... --model gigaam-v3-e2e-rnnt \
        --wav data/test/test-ru-20s.wav --cancel-after-ms 300
    python3 scripts/measure_model.py --model-dir ... --model gigaam-v3-e2e-rnnt \
        --wav ... --rlimit-as-mb 1024 --runs 1

Выход: человекочитаемая таблица в stderr/stdout + JSON (`--json PATH` или `--print-json`).
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------
# Память процесса
# --------------------------------------------------------------------------------------


def proc_status_kb(*keys: str) -> dict[str, int]:
    """Значения из /proc/self/status (в КиБ)."""
    out: dict[str, int] = {}
    wanted = set(keys)
    with open("/proc/self/status", encoding="ascii") as f:
        for line in f:
            name, _, rest = line.partition(":")
            if name in wanted:
                out[name] = int(rest.strip().split()[0])
    return out


def smaps_rollup_kb(*keys: str) -> dict[str, int]:
    """Значения из /proc/self/smaps_rollup (в КиБ). Pss доступен не во всех ядрах/политиках."""
    out: dict[str, int] = {}
    wanted = set(keys)
    try:
        with open("/proc/self/smaps_rollup", encoding="ascii") as f:
            for line in f:
                name, _, rest = line.partition(":")
                if name in wanted:
                    out[name] = int(rest.strip().split()[0])
    except OSError:
        pass
    return out


def mem_snapshot() -> dict[str, int]:
    """Снимок памяти процесса в МиБ (десятичных нет — считаем в МиБ, как /proc)."""
    st = proc_status_kb("VmRSS", "VmHWM", "VmSize", "VmPeak")
    sm = smaps_rollup_kb("Pss", "Rss", "Private_Clean", "Private_Dirty")
    snap = {f"{k}_mib": round(v / 1024, 1) for k, v in st.items()}
    snap.update({f"{k}_mib": round(v / 1024, 1) for k, v in sm.items()})
    return snap


# --------------------------------------------------------------------------------------
# Аудио
# --------------------------------------------------------------------------------------


def wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


# --------------------------------------------------------------------------------------
# Отмена: RunOptions.terminate (Р4 / контракт И1)
# --------------------------------------------------------------------------------------


class Canceller:
    """Прокидывает `RunOptions` во все сессии модели и умеет прервать текущий инференс.

    onnx-asr 0.12.0 не принимает `run_options` в `session.run(...)`, поэтому обёртка ставится
    на экземпляры `InferenceSession` уже загруженной модели. Для продакшна тот же приём
    оформляется как один метод адаптера (см. `arch/spikes/S3.md` §5, минимальный патч).
    """

    def __init__(self, model: Any) -> None:
        import onnxruntime as rt

        self._rt = rt
        self._run_options: Any | None = None
        self._cancelled = False
        self._sessions: list[Any] = []
        self._patch(model)

    def _patch(self, obj: Any, depth: int = 0) -> None:
        """Найти все InferenceSession в объекте модели и обернуть их .run()."""
        if depth > 4:
            return
        seen = getattr(self, "_seen", None)
        if seen is None:
            seen = self._seen = set()
        if id(obj) in seen:
            return
        seen.add(id(obj))

        for name in dir(obj):
            if name.startswith("__"):
                continue
            try:
                attr = getattr(obj, name)
            except Exception:  # noqa: BLE001 - property может кидать
                continue
            if isinstance(attr, self._rt.InferenceSession):
                self._wrap(attr)
            elif (
                hasattr(attr, "__dict__")
                and not callable(attr)
                and not isinstance(attr, (str, bytes, Path))
            ):
                self._patch(attr, depth + 1)

    def _wrap(self, sess: Any) -> None:
        if getattr(sess, "_av_wrapped", False):
            return
        original = sess.run
        canceller = self

        def run(output_names, input_feed, run_options=None):  # noqa: ANN001, ANN202
            if canceller._cancelled:
                raise RuntimeError("astra-voice: cancelled")
            return original(output_names, input_feed, run_options or canceller._run_options)

        sess.run = run
        sess._av_wrapped = True
        self._sessions.append(sess)

    def begin(self) -> None:
        self._cancelled = False
        self._run_options = self._rt.RunOptions()

    def cancel(self) -> None:
        """Вызывается из другого потока во время инференса."""
        self._cancelled = True
        ro = self._run_options
        if ro is not None:
            ro.terminate = True

    @property
    def sessions(self) -> int:
        return len(self._sessions)


# --------------------------------------------------------------------------------------
# Основной замер (одно значение --threads на процесс)
# --------------------------------------------------------------------------------------


def measure_once(args: argparse.Namespace, threads: int) -> dict[str, Any]:
    import onnx_asr
    import onnxruntime as rt

    wav = Path(args.wav).expanduser()
    duration = wav_duration_s(wav)

    sess_options = rt.SessionOptions()
    sess_options.intra_op_num_threads = threads
    sess_options.inter_op_num_threads = 1

    mem_before = mem_snapshot()

    t0 = time.perf_counter()
    model = onnx_asr.load_model(
        args.model,
        str(Path(args.model_dir).expanduser()),
        quantization=args.quantization,
        sess_options=sess_options,
    )
    load_ms = (time.perf_counter() - t0) * 1000

    result: dict[str, Any] = {
        "model": args.model,
        "model_dir": str(args.model_dir),
        "quantization": args.quantization,
        "wav": str(wav),
        "wav_duration_s": round(duration, 3),
        "threads_intra_op": threads,
        "ort_version": rt.__version__,
        "cold_load_ms": round(load_ms, 1),
        "mem_before_load": mem_before,
    }

    # --- режим отмены -------------------------------------------------------------
    if args.cancel_after_ms is not None:
        canceller = Canceller(model)
        result["sessions_wrapped"] = canceller.sessions
        # прогрев, чтобы мерить отмену на «горячей» модели
        model.recognize(str(wav))
        canceller.begin()
        fired = threading.Event()

        def fire() -> None:
            time.sleep(args.cancel_after_ms / 1000)
            fired.set()
            canceller.cancel()

        th = threading.Thread(target=fire, daemon=True)
        t_start = time.perf_counter()
        th.start()
        err = None
        try:
            model.recognize(str(wav))
            outcome = "завершился сам (отмена не успела)"
        except Exception as exc:  # noqa: BLE001 - интересен любой способ выхода
            outcome = type(exc).__name__
            err = str(exc).splitlines()[0][:200]
        t_end = time.perf_counter()
        result["cancel"] = {
            "requested_after_ms": args.cancel_after_ms,
            "fired": fired.is_set(),
            "total_ms": round((t_end - t_start) * 1000, 1),
            "return_after_cancel_ms": round((t_end - t_start) * 1000 - args.cancel_after_ms, 1),
            "outcome": outcome,
            "error": err,
        }
        result["mem_after"] = mem_snapshot()
        return result

    # --- обычный замер ------------------------------------------------------------
    t0 = time.perf_counter()
    text = model.recognize(str(wav))
    warmup_ms = (time.perf_counter() - t0) * 1000
    result["warmup_ms"] = round(warmup_ms, 1)
    result["text"] = text
    result["mem_after_first_run"] = mem_snapshot()

    times: list[float] = []
    for _ in range(args.runs):
        t0 = time.perf_counter()
        model.recognize(str(wav))
        times.append((time.perf_counter() - t0) * 1000)

    times_sorted = sorted(times)
    p50 = statistics.median(times)
    # p95 по «ближайшему рангу» (nearest-rank) — на 10 прогонах это 10-й, т.е. максимум
    p95 = times_sorted[min(len(times_sorted) - 1, max(0, -(-95 * len(times_sorted) // 100) - 1))]
    result.update(
        {
            "runs": args.runs,
            "times_ms": [round(t, 1) for t in times],
            "min_ms": round(min(times), 1),
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "max_ms": round(max(times), 1),
            "mean_ms": round(statistics.fmean(times), 1),
            "rtfx_p50": round(duration * 1000 / p50, 1),
            "rtfx_p95": round(duration * 1000 / p95, 1),
            "mem_after": mem_snapshot(),
        }
    )
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Замер модели распознавания (onnx-asr).")
    ap.add_argument(
        "--model-dir", required=True, help="каталог с файлами модели (offline-режим onnx-asr)"
    )
    ap.add_argument("--model", required=True, help="имя модели onnx-asr, напр. gigaam-v3-e2e-rnnt")
    ap.add_argument("--wav", required=True, help="wav 16 кГц моно")
    ap.add_argument("--runs", type=int, default=10, help="число тёплых прогонов (по умолчанию 10)")
    ap.add_argument(
        "--threads", default="4", help="intra-op потоки ORT, список через запятую: 1,2,4"
    )
    ap.add_argument("--quantization", default="int8", help="int8 | None")
    ap.add_argument(
        "--cancel-after-ms",
        type=int,
        default=None,
        help="прервать инференс через N мс из другого потока",
    )
    ap.add_argument(
        "--rlimit-as-mb", type=int, default=None, help="выставить RLIMIT_AS перед загрузкой (МиБ)"
    )
    ap.add_argument("--json", default=None, help="записать JSON-результат в файл")
    ap.add_argument("--print-json", action="store_true", help="печатать JSON в stdout")
    ap.add_argument("--_single", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.quantization in ("None", "none", ""):
        args.quantization = None

    if args.rlimit_as_mb is not None:
        limit = args.rlimit_as_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    thread_list = [int(t) for t in str(args.threads).split(",") if t.strip()]

    # Несколько значений --threads: каждый в своём процессе, чтобы VmHWM был чистым.
    if len(thread_list) > 1 and not args._single:
        results = []
        for t in thread_list:
            cmd = [
                sys.executable,
                os.path.abspath(__file__),
                *sys.argv[1:],
                "--_single",
                "--print-json",
            ]
            # выкинуть прежний --threads (в форме `--threads V` и `--threads=V`)
            cleaned: list[str] = []
            skip = False
            for c in cmd:
                if skip:
                    skip = False
                    continue
                if c in ("--threads", "--json"):
                    skip = True
                    continue
                if c.startswith(("--threads=", "--json=")):
                    continue
                cleaned.append(c)
            cleaned += ["--threads", str(t)]
            proc = subprocess.run(cleaned, capture_output=True, text=True)
            if proc.returncode != 0:
                print(proc.stdout, proc.stderr, file=sys.stderr)
                return proc.returncode
            payload = proc.stdout[proc.stdout.index("{") :]
            results.append(json.loads(payload))
        out: Any = results
    else:
        out = measure_once(args, thread_list[0])

    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.print_json or not args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
