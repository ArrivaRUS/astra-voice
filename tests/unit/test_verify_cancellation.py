"""Cancellation/limits use disposable direct children, never user processes."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from astra_voice.security import verify
from astra_voice.security.verify import Verifier

pytestmark = pytest.mark.unit
FPR = "A" * 40
VALID = f"[GNUPG:] VALIDSIG {FPR} 2026-10-09 1791532800 0 4 0 22 8 00 {FPR}\n"
ERRSIG = f"[GNUPG:] ERRSIG ABC 22 8 00 1 6 {FPR}\n"


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/python3 -I\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


class Rig:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.keyring = directory / "keyring"
        self.data = directory / "SHA256SUMS"
        self.signature = directory / "SHA256SUMS.asc"
        for path in (self.keyring, self.data, self.signature):
            path.write_bytes(b"test")
        self.gpgv = executable(directory / "gpgv", f"print({VALID!r}, end='')\n")
        self.gpg = executable(directory / "gpg", "raise SystemExit(99)\n")
        self.verifier = Verifier(
            "release",
            self.keyring,
            pinned=frozenset({FPR}),
            gpgv_path=self.gpgv,
            gpg_path=self.gpg,
            clock=lambda: 1,
        )

    def block(self, path: Path, *, ignore_term: bool = False, close_pipes: bool = False) -> Path:
        marker = self.directory / (path.name + ".started")
        executable(
            path,
            "import os, signal, time\nfrom pathlib import Path\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
            + f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            + ("os.close(1)\nos.close(2)\n" if close_pipes else "")
            + "time.sleep(10)\n",
        )
        return marker


@pytest.fixture
def children(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen[bytes]]]:
    found: list[subprocess.Popen[bytes]] = []
    real = subprocess.Popen

    def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = real(*args, **kwargs)
        found.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    try:
        yield found
    finally:
        for child in found:
            if child.poll() is None:
                child.kill()
            child.wait()
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()


def assert_reaped(children: list[subprocess.Popen[bytes]]) -> None:
    for child in children:
        assert child.returncode is not None
        assert child.stdout is not None and child.stdout.closed
        assert child.stderr is not None and child.stderr.closed
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)


def test_cancel_before_launch_does_not_spawn(
    tmp_path: Path, children: list[subprocess.Popen[bytes]]
) -> None:
    rig = Rig(tmp_path)
    result = rig.verifier.verify_detached(rig.data, rig.signature, cancel=lambda: True)
    assert not result and result.code == "cancelled" and result.purpose == "release"
    assert children == []


@pytest.mark.parametrize("ignore_term", [False, True])
def test_cancel_gpgv_stops_and_reaps_own_child(
    tmp_path: Path, children: list[subprocess.Popen[bytes]], ignore_term: bool
) -> None:
    rig = Rig(tmp_path)
    marker = rig.block(rig.gpgv, ignore_term=ignore_term)
    started = time.monotonic()
    result = rig.verifier.verify_detached(rig.data, rig.signature, cancel=marker.exists)
    assert time.monotonic() - started < 2
    assert not result and result.code == "cancelled"
    assert len(children) == 1  # no time-diagnosis subprocess after cancellation
    assert children[0].returncode == (-signal.SIGKILL if ignore_term else -signal.SIGTERM)
    assert_reaped(children)


def test_cancel_show_keys_stops_and_reaps_both_children(
    tmp_path: Path, children: list[subprocess.Popen[bytes]]
) -> None:
    rig = Rig(tmp_path)
    executable(rig.gpgv, f"print({ERRSIG!r}, end='')\nraise SystemExit(2)\n")
    marker = rig.block(rig.gpg, ignore_term=True)
    result = rig.verifier.verify_detached(rig.data, rig.signature, cancel=marker.exists)
    assert not result and result.code == "cancelled"
    assert [child.returncode for child in children] == [2, -signal.SIGKILL]
    assert_reaped(children)


@pytest.mark.parametrize("stage", ["gpgv", "gpg"])
def test_deadline_kills_reaps_and_never_accepts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    children: list[subprocess.Popen[bytes]],
    stage: str,
) -> None:
    rig = Rig(tmp_path)
    if stage == "gpg":
        executable(rig.gpgv, f"print({ERRSIG!r}, end='')\nraise SystemExit(2)\n")
    rig.block(rig.gpgv if stage == "gpgv" else rig.gpg, ignore_term=True)
    monkeypatch.setattr(verify, "_GPGV_TIMEOUT_S", 0.3)
    started = time.monotonic()
    result = rig.verifier.verify_detached(rig.data, rig.signature, cancel=lambda: False)
    assert time.monotonic() - started < 2
    assert not result and result.code == ""
    assert len(children) == (1 if stage == "gpgv" else 2)
    assert children[-1].returncode == -signal.SIGKILL
    assert_reaped(children)


def test_closed_output_pipes_do_not_bypass_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    rig = Rig(tmp_path)
    rig.block(rig.gpgv, close_pipes=True)
    monkeypatch.setattr(verify, "_GPGV_TIMEOUT_S", 0.3)
    result = rig.verifier.verify_detached(rig.data, rig.signature, cancel=lambda: False)
    assert not result and "отведённое время" in result.reason
    assert_reaped(children)


@pytest.mark.parametrize("stream", ["stdout", "stderr", "combined"])
def test_output_limit_rejects_even_with_validsig(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    children: list[subprocess.Popen[bytes]],
    stream: str,
) -> None:
    rig = Rig(tmp_path)
    body = f"import sys, time\nprint({VALID!r}, end='', flush=True)\n"
    if stream == "combined":
        body += (
            "sys.stdout.write('x' * 2048)\nsys.stdout.flush()\n"
            "sys.stderr.write('x' * 2048)\nsys.stderr.flush()\n"
        )
    else:
        body += f"sys.{stream}.write('x' * 4097)\nsys.{stream}.flush()\n"
    executable(rig.gpgv, body + "time.sleep(10)\n")
    monkeypatch.setattr(verify, "_GPG_OUTPUT_MAX_BYTES", 4096)
    result = rig.verifier.verify_detached(rig.data, rig.signature, cancel=lambda: False)
    assert not result and result.code == "output-limit"
    assert len(children) == 1
    assert_reaped(children)


def test_overflow_diagnostic_cannot_turn_failure_into_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, children: list[subprocess.Popen[bytes]]
) -> None:
    rig = Rig(tmp_path)
    executable(rig.gpgv, f"print({ERRSIG!r}, end='')\nraise SystemExit(2)\n")
    executable(rig.gpg, "import sys\nsys.stdout.write('x' * 4097)\n")
    monkeypatch.setattr(verify, "_GPG_OUTPUT_MAX_BYTES", 4096)
    result = rig.verifier.verify_detached(rig.data, rig.signature, cancel=lambda: False)
    assert not result and result.code == ""
    assert len(children) == 2
    assert_reaped(children)


@pytest.mark.parametrize(
    "output,code,accepted",
    [
        (VALID, "", True),
        ("[GNUPG:] GOODSIG ABC user\n", "", False),
        (VALID + VALID, "", False),
        (VALID.replace(FPR, "B" * 40), "", False),
    ],
)
def test_cancellable_parser_preserves_signature_rules(
    tmp_path: Path, children: list[subprocess.Popen[bytes]], output: str, code: str, accepted: bool
) -> None:
    rig = Rig(tmp_path)
    executable(rig.gpgv, f"print({output!r}, end='')\n")
    result = rig.verifier.verify_detached(rig.data, rig.signature, cancel=lambda: False)
    assert result.ok is accepted and result.code == code
    assert result.purpose == "release"
    assert_reaped(children)


def test_revoked_key_still_rejected_in_cancellable_path(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.verifier.revoked = frozenset({FPR})
    result = rig.verifier.verify_detached(rig.data, rig.signature, cancel=lambda: False)
    assert not result and "отозван" in result.reason


def test_clock_diagnosis_preserved_with_cancel_callback(
    tmp_path: Path, children: list[subprocess.Popen[bytes]]
) -> None:
    rig = Rig(tmp_path)
    executable(rig.gpgv, f"print({ERRSIG!r}, end='')\nraise SystemExit(2)\n")
    listing = f"pub:-:255:22:ABC:100:::\nfpr:::::::::{FPR}:\n"
    executable(rig.gpg, f"print({listing!r}, end='')\n")
    result = rig.verifier.verify_detached(rig.data, rig.signature, cancel=lambda: False)
    assert not result and result.code == "clock-behind"
    assert len(children) == 2
    assert_reaped(children)


def test_child_environment_cwd_and_fixed_arguments_preserved(
    tmp_path: Path, children: list[subprocess.Popen[bytes]]
) -> None:
    rig = Rig(tmp_path)
    executable(
        rig.gpgv,
        "import os, sys\n"
        "assert set(os.environ) <= {'LC_ALL','GNUPGHOME','LC_CTYPE'}\n"
        "assert os.environ['LC_ALL'] == 'C'\n"
        "assert os.getcwd() == os.environ['GNUPGHOME']\n"
        "assert sys.argv[1:3] == ['--status-fd', '1']\n"
        "assert '--ignore-time-conflict' not in sys.argv\n"
        f"print({VALID!r}, end='')\n",
    )
    result = rig.verifier.verify_detached(rig.data, rig.signature, cancel=lambda: False)
    assert result.ok
    assert_reaped(children)


def test_callback_exception_still_reaps_child(
    tmp_path: Path, children: list[subprocess.Popen[bytes]]
) -> None:
    rig = Rig(tmp_path)
    marker = rig.block(rig.gpgv)

    def cancel() -> bool:
        if marker.exists():
            raise RuntimeError("test callback exception")
        return False

    with pytest.raises(RuntimeError, match="test callback exception"):
        rig.verifier.verify_detached(rig.data, rig.signature, cancel=cancel)
    assert_reaped(children)


def test_missing_cancellable_binary_is_plain_refusal(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.verifier.gpgv_path = tmp_path / "absent"
    result = rig.verifier.verify_detached(rig.data, rig.signature, cancel=lambda: False)
    assert not result and "не найден" in result.reason
