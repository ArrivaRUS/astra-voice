"""Короткие сигналы записи: один ограниченный дочерний проигрыватель вне GUI.

Ни одного подключения к звуковому серверу до play(). paplay — инструмент хоста;
отсутствие или ошибка отражаются в status. PCM синтезируется в памяти, файлы
записи не используются. Собственный client.conf запрещает autospawn независимо
от чужого PULSE_CLIENTCONFIG; PULSE_SERVER/SINK/COOKIE сохраняются.
"""

from __future__ import annotations

import fcntl
import math
import os
import struct
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from astra_voice.core.childenv import clean_env

PLAYER = "/usr/bin/paplay"
RATE = 48000
DURATION = 0.11
PLAY_TIMEOUT = 1.0
REAP_TIMEOUT = 0.2
MAX_AGE = 0.35
MAX_QUEUE = 4
UNAVAILABLE = "Звуковые сигналы недоступны: проигрыватель звука не найден"
FAILED = "Не удалось воспроизвести сигнал. Проверьте устройство вывода звука"
CLEANUP_FAILED = "Не удалось завершить воспроизведение звукового сигнала"


def cue_pcm(kind: str) -> bytes:
    """110 мс, плавные края, пик не выше −18 dBFS; начало ↑, конец ↓."""
    if kind not in ("start", "stop"):
        raise ValueError("unknown cue")
    count = round(RATE * DURATION)
    low, high = (660.0, 990.0) if kind == "start" else (990.0, 660.0)
    amplitude = 32767 * 10 ** (-18 / 20)
    samples = []
    for index in range(count):
        t = index / RATE
        envelope = min(1.0, index / (RATE * 0.01), (count - 1 - index) / (RATE * 0.01))
        phase = 2 * math.pi * (low * t + (high - low) * t * t / (2 * DURATION))
        samples.append(round(amplitude * envelope * math.sin(phase)))
    return struct.pack(f"<{count}h", *samples)


@contextmanager
def _pulse_config() -> Iterator[int]:
    """Неизменяемый конфиг живёт у child даже после выхода родительского потока."""
    fd = os.memfd_create("astra-voice-cues", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.fchmod(fd, 0o600)
        payload = b"autospawn = no\n"
        if os.write(fd, payload) != len(payload):
            raise OSError("incomplete pulse config")
        fcntl.fcntl(
            fd,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL,
        )
        yield fd
    finally:
        os.close(fd)


@dataclass
class _Cue:
    kind: str
    key: tuple[int, str]
    epoch: int
    expires: float
    admission: tuple[bool, int, int]
    cancelled: bool = False


class RecordingCues:
    """GUI ставит задания; worker владеет Popen/communicate/reap и конфигом.

    Ресурсы не забываются при неудачной остановке: app проверяет их до handoff.
    Popen выполняется без замка. Закрытие во время spawn обнаруживается сразу
    после регистрации child; GUI не ждёт запуска внешней программы.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        popen: Callable[..., Any] = subprocess.Popen,
        available: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        admission: Callable[[], tuple[bool, int, int]] = lambda: (True, 0, 0),
    ) -> None:
        self._popen = popen
        self._available = available or (
            lambda: os.path.isfile(PLAYER) and os.access(PLAYER, os.X_OK)
        )
        self._clock = clock
        self._admission = admission
        self._condition = threading.Condition()
        self._enabled = enabled is True
        self._allowed = True
        self._closed = False
        self._epoch = 0
        self._queue: deque[_Cue] = deque()
        self._current: _Cue | None = None
        self._sessions: dict[tuple[int, str], tuple[int, tuple[bool, int, int]]] = {}
        self._status = "" if self._available() else UNAVAILABLE
        self.thread: threading.Thread | None = None
        self.process: Any = None

    @property
    def status(self) -> str:
        with self._condition:
            return self._status

    def _set_status(self, value: str) -> None:
        with self._condition:
            self._status = value

    def configure(self, *, enabled: bool, allowed: bool = True) -> None:
        with self._condition:
            if (self._enabled, self._allowed) == (enabled is True, allowed):
                return
            self._enabled, self._allowed = enabled is True, allowed
            self._invalidate()

    def _invalidate(self) -> None:
        self._epoch += 1
        self._queue.clear()
        self._sessions.clear()
        self._condition.notify_all()

    def play(self, kind: str, key: tuple[int, str]) -> None:
        if kind not in ("start", "stop"):
            return
        with self._condition:
            if self._closed or not self._enabled or not self._allowed:
                return
            admission = self._admission()
            if not admission[0]:
                return
            if not self._available():
                self._status = UNAVAILABLE
                return
            if kind == "start":
                if key in self._sessions:
                    return
                if len(self._sessions) >= 8:
                    self._sessions.pop(next(iter(self._sessions)))
                self._sessions[key] = (self._epoch, admission)
            if kind == "stop":
                if self._sessions.pop(key, None) != (self._epoch, admission):
                    return
                for queued in self._queue:
                    if queued.key == key and queued.kind == "start":
                        queued.cancelled = True
                if self._current is not None and self.process is None:
                    if self._current.key == key and self._current.kind == "start":
                        self._current.cancelled = True
            self._queue = deque(item for item in self._queue if not item.cancelled)
            if len(self._queue) >= MAX_QUEUE:
                self._queue.popleft()
                self._status = FAILED
            self._queue.append(_Cue(kind, key, self._epoch, self._clock() + MAX_AGE, admission))
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="recording-cues", daemon=True)
                self.thread.start()
            self._condition.notify_all()

    def _valid(self, cue: _Cue) -> bool:
        return (
            not self._closed
            and self._enabled
            and self._allowed
            and cue.epoch == self._epoch
            and not cue.cancelled
            and self._admission() == cue.admission
        )

    def _run(self) -> None:
        try:
            # У запечатанного memfd нет client.conf.d. Чужие конфиги не читаются.
            # Явно наследуем только этот fd: даже неубранный child сохраняет
            # неизменяемый autospawn=no, когда поток уже закрыл свой дескриптор.
            with _pulse_config() as config_fd:
                env = clean_env(keep_pulse_config=True)
                env["PULSE_CLIENTCONFIG"] = f"/proc/self/fd/{config_fd}"
                while True:
                    with self._condition:
                        self._condition.wait_for(lambda: self._closed or bool(self._queue))
                        if self._closed:
                            return
                        cue = self._queue.popleft()
                        if not self._valid(cue) or self._clock() > cue.expires:
                            continue
                        self._current = cue
                    self._play(cue, env, config_fd)
                    with self._condition:
                        self._current = None
                        if self.process is not None:
                            # Unreaped child: fail closed, never overlap players.
                            self._closed = True
                            self._queue.clear()
                            return
        except Exception:
            self._set_status(FAILED)
            with self._condition:
                self._closed = True
                self._queue.clear()

    def _play(self, cue: _Cue, env: dict[str, str], config_fd: int) -> None:
        process = None
        try:
            pcm = cue_pcm(cue.kind)
            with self._condition:
                if not self._valid(cue) or self._clock() > cue.expires:
                    return
            process = self._popen(
                [
                    PLAYER,
                    "--raw",
                    "--format=s16le",
                    "--rate=48000",
                    "--channels=1",
                    "--client-name=Astra Voice",
                    "--stream-name=Recording cue",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                shell=False,
                close_fds=True,
                pass_fds=(config_fd,),
            )
            with self._condition:
                self.process = process
                valid = self._valid(cue) and self._clock() <= cue.expires
            if not valid:
                return
            deadline = self._clock() + PLAY_TIMEOUT
            payload: bytes | None = pcm
            while True:
                with self._condition:
                    if not self._valid(cue):
                        return
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(PLAYER, PLAY_TIMEOUT)
                try:
                    process.communicate(input=payload, timeout=min(0.05, remaining))
                    self._set_status("" if process.returncode == 0 else FAILED)
                    return
                except subprocess.TimeoutExpired:
                    payload = None
        except Exception:
            self._set_status(FAILED)
        finally:
            if process is not None:
                reaped = self._reap(process)
                with self._condition:
                    if reaped:
                        self.process = None
                    else:
                        self._status = CLEANUP_FAILED

    @staticmethod
    def _reap(process: Any) -> bool:
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=REAP_TIMEOUT)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False

    def shutdown(self) -> None:
        with self._condition:
            self._closed = True
            self._invalidate()
            thread = self.thread
        if thread is not None:
            thread.join(timeout=2 * REAP_TIMEOUT + 0.2)
            if thread.is_alive():
                self._set_status(CLEANUP_FAILED)
                raise RuntimeError(CLEANUP_FAILED)
        if self.process is not None:
            raise RuntimeError(CLEANUP_FAILED)
